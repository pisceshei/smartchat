"""Billing HTTP surface (/api/v1/billing, plan P3 計費模型實測 + CONTRACT).

Endpoints:
- GET  /billing/plans                 — plan catalogue
- GET  /billing/subscription          — effective limits + balance + points + add-ons
- POST /billing/checkout              — order math → Stripe PaymentIntent (or disabled)
- POST /billing/points/topup          — $0.375 / 10k points → Stripe handle
- GET  /billing/points/ledger         — AI-points flow (cursor paged)
- GET  /billing/invoices              — issued invoices
- POST /billing/webhook               — Stripe webhook (no auth, signature-verified)
- POST /billing/admin/change-plan     — super_admin no-charge plan switch (self-use)

A missing Stripe key degrades to ``billing_disabled`` (breakdown returned,
``stripe: null``) — never a crash; the admin path still works offline.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, current_member, require_permission
from ...models.billing import BillingOrder, Invoice
from ...models.tenancy import AIPointsLedger
from ...services import stripe_client
from ...services.redis_client import get_redis
from ...services.stripe_client import BillingDisabledError
from . import service, webhook

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


# --------------------------------------------------------------------------
# super-admin gate (self-use path)
# --------------------------------------------------------------------------
async def require_super_admin(
    member: MemberContext = Depends(current_member),
) -> MemberContext:
    if "*" not in member.permissions:
        raise HTTPException(
            status_code=403, detail={"code": "super_admin_required"}
        )
    return member


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------
class AddonsIn(BaseModel):
    seats: int = Field(default=0, ge=0)
    official_channels: int = Field(default=0, ge=0)
    hosted_devices: int = Field(default=0, ge=0)


class CheckoutIn(BaseModel):
    plan_code: str
    duration_days: int
    addons: AddonsIn = Field(default_factory=AddonsIn)
    use_balance: bool = False

    @field_validator("duration_days")
    @classmethod
    def _valid_duration(cls, v: int) -> int:
        if v not in service.VALID_DURATIONS:
            raise ValueError(f"duration_days must be one of {service.VALID_DURATIONS}")
        return v


class TopupIn(BaseModel):
    points: int = Field(..., gt=0)


class ChangePlanIn(BaseModel):
    plan_code: str
    duration_days: int = Field(default=30, gt=0)
    addons: AddonsIn = Field(default_factory=AddonsIn)


# --------------------------------------------------------------------------
# plans + subscription
# --------------------------------------------------------------------------
@router.get("/plans")
async def get_plans(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    return await service.list_plans(session)


@router.get("/subscription")
async def get_subscription(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await service.subscription_view(session, member.workspace_id)


# --------------------------------------------------------------------------
# checkout (subscription)
# --------------------------------------------------------------------------
@router.post("/checkout")
async def checkout(
    body: CheckoutIn,
    member: MemberContext = Depends(require_permission("billing.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    redis = get_redis()
    try:
        order, breakdown = await service.build_subscription_order(
            session,
            workspace_id=member.workspace_id,
            plan_code=body.plan_code,
            duration_days=body.duration_days,
            addons=body.addons.model_dump(),
            use_balance=body.use_balance,
        )
    except service.BillingError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message}) from e

    order_dict = service.order_breakdown_dict(breakdown, order.id)
    result = await _finalise_order(
        session,
        redis,
        order,
        amount_due=breakdown.amount_due_cents,
        product_name=f"SmartChat {body.plan_code} ({body.duration_days}d)",
        order_response=order_dict,
    )
    await session.commit()
    return result


# --------------------------------------------------------------------------
# points top-up
# --------------------------------------------------------------------------
@router.post("/points/topup")
async def points_topup(
    body: TopupIn,
    member: MemberContext = Depends(require_permission("billing.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    redis = get_redis()
    try:
        order, price = await service.build_points_order(
            session, workspace_id=member.workspace_id, points_amount=body.points
        )
    except service.BillingError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message}) from e

    result = await _finalise_order(
        session,
        redis,
        order,
        amount_due=price,
        product_name=f"SmartChat {body.points} AI points",
        order_response=None,
    )
    result["price"] = price
    await session.commit()
    return result


async def _finalise_order(
    session: AsyncSession,
    redis: Any,
    order: BillingOrder,
    *,
    amount_due: int,
    product_name: str,
    order_response: dict[str, Any] | None,
) -> dict[str, Any]:
    """Shared checkout tail: $0 orders settle immediately (balance-covered),
    otherwise mint a Stripe PaymentIntent — or return ``billing_disabled`` when
    no key is configured (self-use via the admin path still works)."""
    out: dict[str, Any] = {}
    if order_response is not None:
        out["order"] = order_response

    if amount_due <= 0:
        # fully covered by balance (or free) → apply effects now, no Stripe.
        await service.apply_paid_order(session, redis, order)
        out["stripe"] = None
        out["status"] = "paid"
        return out

    stripe = stripe_client.get_stripe()
    if stripe is None:
        out["stripe"] = None
        out["billing_disabled"] = True
        out["status"] = "pending"
        return out

    intent = await stripe_client.create_payment_intent(
        amount_cents=amount_due,
        currency=order.currency,
        metadata={
            "order_id": str(order.id),
            "workspace_id": str(order.workspace_id),
            "kind": order.kind,
        },
        idempotency_key=f"order:{order.id}",
    )
    order.stripe_ref = intent["id"]
    out["stripe"] = {"client_secret": intent["client_secret"], "payment_intent_id": intent["id"]}
    out["status"] = "pending"
    return out


# --------------------------------------------------------------------------
# points ledger + invoices
# --------------------------------------------------------------------------
@router.get("/points/ledger")
async def points_ledger(
    member: MemberContext = Depends(require_permission("billing.manage")),
    session: AsyncSession = Depends(get_session),
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    q = select(AIPointsLedger).where(AIPointsLedger.workspace_id == member.workspace_id)
    if cursor:
        try:
            q = q.where(AIPointsLedger.id < uuid.UUID(cursor))
        except ValueError as e:
            raise HTTPException(status_code=400, detail="invalid cursor") from e
    rows = (
        await session.execute(q.order_by(AIPointsLedger.id.desc()).limit(limit + 1))
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "delta": int(r.delta),
            "reason": r.reason,
            "ref": r.ref_id,
            "balance_after": int(r.balance_after),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"items": items, "next_cursor": str(rows[-1].id) if has_more and rows else None}


@router.get("/invoices")
async def list_invoices(
    member: MemberContext = Depends(require_permission("billing.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Invoice)
            .where(Invoice.workspace_id == member.workspace_id)
            .order_by(Invoice.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return [
        {
            "id": str(inv.id),
            "number": inv.number,
            "amount_cents": int(inv.amount_cents),
            "currency": inv.currency,
            "status": inv.status,
            "order_id": str(inv.order_id) if inv.order_id else None,
            "hosted_invoice_url": inv.hosted_invoice_url,
            "pdf_url": inv.pdf_url,
            "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        }
        for inv in rows
    ]


# --------------------------------------------------------------------------
# Stripe webhook (no auth, signature-verified)
# --------------------------------------------------------------------------
@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict[str, Any]:
    payload = await request.body()
    try:
        event = stripe_client.construct_event(payload, stripe_signature or "")
    except BillingDisabledError as e:
        raise HTTPException(status_code=503, detail="billing webhook disabled") from e
    except Exception as e:  # noqa: BLE001 — bad signature / malformed payload → 400
        raise HTTPException(status_code=400, detail=f"invalid webhook: {e}") from e

    # normalise the Stripe event object into a plain dict for the processor
    event_dict = {
        "id": event["id"],
        "type": event["type"],
        "data": {"object": dict(event["data"]["object"])},
    }
    result = await webhook.process_stripe_event(session, get_redis(), event_dict)
    await session.commit()
    return result


# --------------------------------------------------------------------------
# admin no-charge plan switch (super_admin — self-use path)
# --------------------------------------------------------------------------
@router.post("/admin/change-plan")
async def admin_change_plan(
    body: ChangePlanIn,
    member: MemberContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    redis = get_redis()
    try:
        await service.admin_change_plan(
            session,
            redis,
            workspace_id=member.workspace_id,
            plan_code=body.plan_code,
            duration_days=body.duration_days,
            addons=body.addons.model_dump(),
        )
    except service.BillingError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message}) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await session.commit()
    return await service.subscription_view(session, member.workspace_id)
