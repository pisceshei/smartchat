"""Billing orchestration (plan P3 計費模型實測).

Ties the pure order math (``services.stripe_client.compute_order``) to the DB
(``billing_orders`` / ``invoices``) and the domain effects (``app.billing.*``).
Both the HTTP router and the Stripe webhook call in here; the admin no-charge
path reuses ``apply_paid_order`` directly.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...billing import balance as balance_svc
from ...billing import points_topup
from ...billing import subscription as sub_svc
from ...models.billing import BillingOrder, Invoice
from ...models.tenancy import Plan, Subscription
from ...services import points, quotas
from ...services.redis_client import get_redis
from ...services.stripe_client import (
    OrderBreakdown,
    compute_order,
    compute_points_topup,
)
from ...settings import get_settings

VALID_DURATIONS: tuple[int, ...] = (7, 30, 90, 180, 360, 720)
ADDON_KEYS: tuple[str, ...] = sub_svc.ADDON_KEYS


class BillingError(Exception):
    """400-class business error (unknown plan, bad duration, …)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# pricing helpers
# --------------------------------------------------------------------------
def plan_monthly_cents(plan: Plan) -> int | None:
    """Plan monthly price in integer cents, or None for a price-less (custom)
    plan that can only be assigned via the admin path."""
    if plan.price_usd_month is None:
        return None
    return int((Decimal(str(plan.price_usd_month)) * 100).to_integral_value())


def order_breakdown_dict(o: OrderBreakdown, order_id: uuid.UUID) -> dict[str, Any]:
    """CONTRACT order shape (all integer cents)."""
    return {
        "order_id": str(order_id),
        "base_price": o.base_cents,
        "addons_price": o.addons_cents,
        "discount": o.discount_cents,
        "handling_fee": o.handling_fee_cents,
        "balance_applied": o.balance_applied_cents,
        "amount_due": o.amount_due_cents,
        "currency": o.currency,
    }


def _clean_addons(addons: dict[str, int] | None) -> dict[str, int]:
    return {k: int(addons[k]) for k in ADDON_KEYS if int((addons or {}).get(k, 0) or 0) > 0}


# --------------------------------------------------------------------------
# order creation
# --------------------------------------------------------------------------
async def build_subscription_order(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    plan_code: str,
    duration_days: int,
    addons: dict[str, int] | None,
    use_balance: bool,
) -> tuple[BillingOrder, OrderBreakdown]:
    """Compute + persist a pending subscription order. Caller commits."""
    if duration_days not in VALID_DURATIONS:
        raise BillingError("invalid_duration", f"duration_days must be one of {VALID_DURATIONS}")
    plan = await session.get(Plan, plan_code)
    if plan is None or not plan.is_active:
        raise BillingError("unknown_plan", f"plan {plan_code!r} not found")
    monthly = plan_monthly_cents(plan)
    if monthly is None:
        raise BillingError("plan_not_purchasable", f"plan {plan_code!r} has no price")

    clean = _clean_addons(addons)
    bal = await balance_svc.get_balance(session, workspace_id) if use_balance else 0
    settings = get_settings()
    try:
        breakdown = compute_order(
            monthly,
            duration_days,
            clean or None,
            balance_cents=bal,
            handling_fee_pct=settings.billing_handling_fee_pct,
            currency=settings.stripe_currency,
        )
    except ValueError as e:
        raise BillingError("bad_order", str(e)) from e

    order = BillingOrder(
        workspace_id=workspace_id,
        kind="subscription",
        plan_code=plan_code,
        duration_days=duration_days,
        addons=clean,
        base_cents=breakdown.base_cents,
        addons_cents=breakdown.addons_cents,
        discount_cents=breakdown.discount_cents,
        handling_fee_cents=breakdown.handling_fee_cents,
        balance_applied_cents=breakdown.balance_applied_cents,
        amount_due_cents=breakdown.amount_due_cents,
        currency=breakdown.currency,
        status="pending",
    )
    session.add(order)
    await session.flush()
    return order, breakdown


async def build_points_order(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    points_amount: int,
) -> tuple[BillingOrder, int]:
    """Compute + persist a pending points top-up order. Returns (order, price_cents)."""
    if points_amount <= 0:
        raise BillingError("invalid_points", "points must be positive")
    settings = get_settings()
    price = compute_points_topup(points_amount)
    order = BillingOrder(
        workspace_id=workspace_id,
        kind="points_topup",
        addons={},
        points=points_amount,
        base_cents=price,
        addons_cents=0,
        discount_cents=0,
        handling_fee_cents=0,
        balance_applied_cents=0,
        amount_due_cents=price,
        currency=settings.stripe_currency,
        status="pending",
    )
    session.add(order)
    await session.flush()
    return order, price


# --------------------------------------------------------------------------
# effects (webhook-paid, admin no-charge, or $0 balance-covered)
# --------------------------------------------------------------------------
async def apply_paid_order(
    session: AsyncSession,
    redis: aioredis.Redis,
    order: BillingOrder,
    *,
    stripe_ref: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mark ``order`` paid and apply its effects EXACTLY once (idempotent on
    ``order.status``). Deducts any balance 折抵, then either switches the plan or
    grants points, then issues an invoice and invalidates the limits cache."""
    now = now or datetime.now(UTC)
    if order.status == "paid":
        return {"status": "already_paid", "order_id": str(order.id)}

    order.status = "paid"
    order.paid_at = now
    if stripe_ref:
        order.stripe_ref = stripe_ref

    if order.balance_applied_cents > 0:
        await balance_svc.deduct_for_order(
            session, order.workspace_id, order.balance_applied_cents, ref=str(order.id)
        )

    effect: dict[str, Any] = {"status": "paid", "order_id": str(order.id), "kind": order.kind}
    if order.kind == "subscription":
        sub = await sub_svc.apply_plan_change(
            session,
            redis,
            workspace_id=order.workspace_id,
            plan_code=order.plan_code or "free",
            duration_days=order.duration_days,
            addons=order.addons,
            now=now,
        )
        effect["plan_code"] = sub.plan_code
        effect["current_period_end"] = (
            sub.current_period_end.isoformat() if sub.current_period_end else None
        )
    elif order.kind == "points_topup":
        bal = await points_topup.grant_purchase(
            session,
            redis,
            workspace_id=order.workspace_id,
            points_amount=int(order.points or 0),
            ref=str(order.id),
        )
        effect["points_balance"] = bal

    await _issue_invoice(session, order, now=now)
    await quotas.invalidate_limits(redis, order.workspace_id)
    return effect


async def _issue_invoice(
    session: AsyncSession, order: BillingOrder, *, now: datetime
) -> Invoice:
    number = f"INV-{now:%Y%m}-{str(order.id)[:8]}"
    invoice = Invoice(
        workspace_id=order.workspace_id,
        order_id=order.id,
        number=number,
        amount_cents=order.amount_due_cents,
        currency=order.currency,
        status="paid",
        issued_at=now,
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def mark_order_failed(session: AsyncSession, order: BillingOrder) -> None:
    if order.status == "pending":
        order.status = "failed"


# --------------------------------------------------------------------------
# admin self-use path
# --------------------------------------------------------------------------
async def admin_change_plan(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    plan_code: str,
    duration_days: int | None,
    addons: dict[str, int] | None,
) -> Subscription:
    """No-charge plan switch (self-use; chilling.com.hk → Max without paying).
    Applies the same subscription effects as a paid webhook. Caller commits."""
    plan = await session.get(Plan, plan_code)
    if plan is None:
        raise BillingError("unknown_plan", f"plan {plan_code!r} not found")
    return await sub_svc.apply_plan_change(
        session,
        redis,
        workspace_id=workspace_id,
        plan_code=plan_code,
        duration_days=duration_days,
        addons=_clean_addons(addons),
    )


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------
async def subscription_view(
    session: AsyncSession, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """CONTRACT GET /billing/subscription payload."""
    redis = get_redis()
    limits = dict(await quotas.effective_limits(session, redis, workspace_id))
    plan_code = limits.pop("_plan_code", None)
    addons = limits.pop("_addons", {}) or {}
    effective = {k: v for k, v in limits.items() if not k.startswith("_")}

    sub = await sub_svc.latest_subscription(session, workspace_id)
    bal = await balance_svc.get_balance(session, workspace_id)
    ai_points = await points.load_balance(session, workspace_id)

    return {
        "plan_code": plan_code,
        "status": sub.status if sub else "none",
        "current_period_end": (
            sub.current_period_end.isoformat() if sub and sub.current_period_end else None
        ),
        "limits_effective": effective,
        "balance": bal,
        "ai_points_balance": int(ai_points),
        "addons": {
            "seats": int(addons.get("seats", 0)),
            "official_channels": int(addons.get("official_channels", 0)),
            "hosted_devices": int(addons.get("hosted_devices", 0)),
        },
    }


async def list_plans(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Plan).where(Plan.is_active).order_by(Plan.sort_order)
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for p in rows:
        cents = plan_monthly_cents(p)
        out.append(
            {
                "code": p.code,
                "name": p.name,
                "price_monthly": cents,
                "limits": p.limits,
                "is_public": p.code != "custom",
            }
        )
    return out
