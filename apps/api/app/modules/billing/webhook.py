"""Stripe webhook processing (plan P3: 賬單 webhook 驅動方案變更).

Two layers of idempotency:
1. ``stripe_events`` — the Stripe event id is a PK; a replayed webhook inserts a
   conflict and is a no-op (``INSERT … ON CONFLICT DO NOTHING RETURNING``).
2. ``billing_orders.status`` — even if an event is seen fresh, effects are only
   applied when the order is still ``pending`` (``apply_paid_order`` guards on
   ``status == "paid"``).

``process_stripe_event`` takes an already-parsed event dict so it is unit-testable
without a Stripe signature; the router verifies the signature first, then calls it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.billing import BillingOrder, StripeEvent
from . import service

# Stripe event types we act on.
PAID_TYPES = {"payment_intent.succeeded", "checkout.session.completed"}
REFUND_TYPES = {"charge.refunded", "refund.created"}
FAILED_TYPES = {"payment_intent.payment_failed", "checkout.session.expired"}


async def _dedupe(session: AsyncSession, event_id: str, event_type: str) -> bool:
    """Insert the event id; return True if this is the first time we see it."""
    stmt = (
        pg_insert(StripeEvent)
        .values(event_id=event_id, type=event_type)
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(StripeEvent.event_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _order_by_id(session: AsyncSession, order_id: str | None) -> BillingOrder | None:
    if not order_id:
        return None
    try:
        import uuid

        oid = uuid.UUID(str(order_id))
    except (ValueError, TypeError):
        return None
    return await session.get(BillingOrder, oid)


async def _order_by_stripe_ref(session: AsyncSession, ref: str | None) -> BillingOrder | None:
    if not ref:
        return None
    return (
        await session.execute(
            select(BillingOrder).where(BillingOrder.stripe_ref == ref).limit(1)
        )
    ).scalar_one_or_none()


async def process_stripe_event(
    session: AsyncSession,
    redis: aioredis.Redis,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply a Stripe event's effects idempotently. Caller commits."""
    now = now or datetime.now(UTC)
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        return {"status": "ignored", "reason": "no event id"}

    if not await _dedupe(session, event_id, event_type):
        return {"status": "duplicate", "event_id": event_id}

    obj = (event.get("data") or {}).get("object") or {}
    metadata = obj.get("metadata") or {}
    result: dict[str, Any]

    if event_type in PAID_TYPES:
        order = await _order_by_id(session, metadata.get("order_id"))
        if order is None:
            order = await _order_by_stripe_ref(session, obj.get("id"))
        if order is None:
            result = {"status": "order_not_found", "event_id": event_id}
        else:
            result = await service.apply_paid_order(
                session, redis, order, stripe_ref=obj.get("id"), now=now
            )
    elif event_type in REFUND_TYPES:
        result = await _handle_refund(session, obj, metadata)
    elif event_type in FAILED_TYPES:
        order = await _order_by_id(session, metadata.get("order_id"))
        if order is None:
            order = await _order_by_stripe_ref(session, obj.get("payment_intent") or obj.get("id"))
        if order is not None:
            await service.mark_order_failed(session, order)
        result = {"status": "failed_recorded", "event_id": event_id}
    else:
        result = {"status": "ignored", "type": event_type}

    await session.execute(
        StripeEvent.__table__.update()
        .where(StripeEvent.event_id == event_id)
        .values(processed_at=now)
    )
    return result


async def _handle_refund(
    session: AsyncSession, obj: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    """A refund credits the workspace's prepaid balance (餘額) so it can be
    re-applied to a future order."""
    from ...billing import balance as balance_svc

    pi = obj.get("payment_intent") or obj.get("id")
    order = await _order_by_id(session, metadata.get("order_id"))
    if order is None:
        order = await _order_by_stripe_ref(session, pi)
    amount = int(obj.get("amount_refunded") or obj.get("amount") or 0)
    if order is None or amount <= 0:
        return {"status": "refund_ignored"}
    bal = await balance_svc.refund_to_balance(
        session, order.workspace_id, amount, ref=str(order.id)
    )
    order.status = "refunded"
    return {"status": "refunded", "order_id": str(order.id), "balance": bal}
