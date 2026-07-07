"""Live billing smoke (pg 5433 + redis 6380). Runs as ONE pytest so the async
engine + redis client share a single event loop (asyncpg pools are loop-bound).
Auto-skips if the infra is down.

Covers the P3 計費模型 end-to-end with a MOCKED Stripe (never network):
- admin/change-plan Free→Pro flips effective_limits (broadcast gate opens) + add-on expansion
- subscription webhook: order paid → plan change + invoice; replay is idempotent
- points top-up webhook: purchase ledger + balance; replay idempotent
- balance 折抵: use_balance order applies + deducts on settle
- Stripe-enabled checkout tail mints a PaymentIntent (mocked); disabled degrades
- subscription expiry sweep downgrades a lapsed subscription to Free

Scenarios A–E run in one uncommitted transaction that is rolled back; the expiry
scenario is committed (its own session) and cleaned up, guarded so it never
mutates another workspace's data.

Run standalone:
    DATABASE_URL=postgresql+asyncpg://smartchat:smartchat@localhost:5433/smartchat \
    REDIS_URL=redis://localhost:6380/0 \
    .venv/Scripts/python -m pytest apps/api/tests/billing/test_live_billing.py -s
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select

import apps.api.app.db as dbmod
import apps.api.app.services.stripe_client as stripe_client
from apps.api.app.billing import balance as balance_svc
from apps.api.app.billing import subscription as sub_svc
from apps.api.app.models.billing import Invoice
from apps.api.app.models.tenancy import AIPointsLedger, Subscription, Workspace
from apps.api.app.modules.billing import router as billing_router
from apps.api.app.modules.billing import service, webhook
from apps.api.app.services import points, quotas
from apps.api.app.services.quotas import limit_allows
from apps.api.app.services.redis_client import close_redis, get_redis


async def _db_available() -> bool:
    try:
        async with dbmod.session_factory()() as s:
            await s.execute(sa.text("SELECT 1"))
        await get_redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _evt(event_id: str, event_type: str, obj: dict[str, Any]) -> dict[str, Any]:
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


async def _new_ws(session, plan_code: str = "free") -> Workspace:
    ws = Workspace(name="bill-test", plan_code=plan_code, status="active", settings={})
    session.add(ws)
    await session.flush()
    return ws


async def test_live_billing() -> None:
    if not await _db_available():
        pytest.skip("pg/redis not available")
    redis = get_redis()
    sf = dbmod.session_factory()
    touched_ws: list[uuid.UUID] = []

    async with sf() as session:
        try:
            # ---------------------------------------------------------- A: admin change-plan
            ws = await _new_ws(session)
            touched_ws.append(ws.id)
            l0 = await quotas.effective_limits(session, redis, ws.id)
            assert not limit_allows(l0, "broadcast"), "free must not have broadcast"
            assert not limit_allows(l0, "reports")

            await service.admin_change_plan(
                session, redis, workspace_id=ws.id, plan_code="pro",
                duration_days=90, addons={"seats": 2},
            )
            l1 = await quotas.effective_limits(session, redis, ws.id)
            assert limit_allows(l1, "broadcast"), "pro must unlock broadcast"
            assert limit_allows(l1, "reports") and limit_allows(l1, "split_link")
            assert not limit_allows(l1, "openapi"), "pro must NOT unlock openapi"
            assert l1["_plan_code"] == "pro"
            assert l1["seats"] == 12, "10 base + 2 add-on seats"

            view = await service.subscription_view(session, ws.id)
            assert view["plan_code"] == "pro"
            assert view["status"] == "active"
            assert view["addons"]["seats"] == 2
            assert view["current_period_end"] is not None
            assert "_addons" not in view["limits_effective"]
            assert "_plan_code" not in view["limits_effective"]

            # ---------------------------------------------------------- B: subscription webhook
            ws2 = await _new_ws(session)
            touched_ws.append(ws2.id)
            order, bd = await service.build_subscription_order(
                session, workspace_id=ws2.id, plan_code="pro", duration_days=90,
                addons={"official_channels": 1}, use_balance=False,
            )
            expected = stripe_client.compute_order(
                1590, 90, {"official_channels": 1}, handling_fee_pct=0.07,
            )
            assert bd.amount_due_cents == expected.amount_due_cents
            assert order.status == "pending"

            ev_b = _evt("evt_sub_b", "payment_intent.succeeded",
                        {"id": "pi_b", "metadata": {"order_id": str(order.id)}})
            r1 = await webhook.process_stripe_event(session, redis, ev_b)
            assert r1["status"] == "paid"
            await session.refresh(order)
            assert order.status == "paid" and order.stripe_ref == "pi_b"
            ws2_row = await session.get(Workspace, ws2.id)
            assert ws2_row.plan_code == "pro"
            inv_count = (await session.execute(
                select(func.count()).select_from(Invoice).where(Invoice.workspace_id == ws2.id)
            )).scalar_one()
            assert inv_count == 1

            # replay the SAME event → deduped, no second invoice / effect
            r2 = await webhook.process_stripe_event(session, redis, ev_b)
            assert r2["status"] == "duplicate"
            inv_count2 = (await session.execute(
                select(func.count()).select_from(Invoice).where(Invoice.workspace_id == ws2.id)
            )).scalar_one()
            assert inv_count2 == 1, "idempotent: replay must not double-invoice"

            # a DIFFERENT event id for an already-paid order is also a no-op
            ev_b2 = _evt("evt_sub_b2", "payment_intent.succeeded",
                         {"id": "pi_b", "metadata": {"order_id": str(order.id)}})
            r3 = await webhook.process_stripe_event(session, redis, ev_b2)
            assert r3["status"] == "already_paid"

            # ---------------------------------------------------------- C: points top-up webhook
            ws3 = await _new_ws(session)
            touched_ws.append(ws3.id)
            porder, price = await service.build_points_order(
                session, workspace_id=ws3.id, points_amount=20_000
            )
            assert price == 75 and porder.amount_due_cents == 75
            ev_c = _evt("evt_pts_c", "checkout.session.completed",
                        {"id": "cs_c", "metadata": {"order_id": str(porder.id)}})
            rc = await webhook.process_stripe_event(session, redis, ev_c)
            assert rc["status"] == "paid" and rc["points_balance"] == 20_000
            n_purchase = (await session.execute(
                select(func.count()).select_from(AIPointsLedger).where(
                    AIPointsLedger.workspace_id == ws3.id, AIPointsLedger.reason == "purchase"
                )
            )).scalar_one()
            assert n_purchase == 1
            assert int(await redis.get(points.balance_key(ws3.id)) or 0) == 20_000
            # replay → idempotent grant
            rc2 = await webhook.process_stripe_event(session, redis, ev_c)
            assert rc2["status"] == "duplicate"
            n_purchase2 = (await session.execute(
                select(func.count()).select_from(AIPointsLedger).where(
                    AIPointsLedger.workspace_id == ws3.id, AIPointsLedger.reason == "purchase"
                )
            )).scalar_one()
            assert n_purchase2 == 1, "idempotent: replay must not double-grant points"

            # ---------------------------------------------------------- D: balance 折抵
            ws4 = await _new_ws(session)
            touched_ws.append(ws4.id)
            assert await balance_svc.topup(session, ws4.id, 500) == 500
            dorder, dbd = await service.build_subscription_order(
                session, workspace_id=ws4.id, plan_code="pro", duration_days=30,
                addons=None, use_balance=True,
            )
            assert dbd.balance_applied_cents == 500
            assert dbd.amount_due_cents == dbd.base_cents + dbd.handling_fee_cents - 500
            ev_d = _evt("evt_bal_d", "payment_intent.succeeded",
                        {"id": "pi_d", "metadata": {"order_id": str(dorder.id)}})
            rd = await webhook.process_stripe_event(session, redis, ev_d)
            assert rd["status"] == "paid"
            assert await balance_svc.get_balance(session, ws4.id) == 0, "balance drained on settle"

            # ---------------------------------------------------------- E: Stripe tail (mocked)
            ws5 = await _new_ws(session)
            touched_ws.append(ws5.id)
            eorder, eprice = await service.build_points_order(
                session, workspace_id=ws5.id, points_amount=10_000
            )

            calls: dict[str, Any] = {}

            async def _fake_intent(**kw: Any) -> dict[str, Any]:
                calls.update(kw)
                return {"id": "pi_mock", "client_secret": "cs_mock_secret",
                        "amount": kw["amount_cents"], "currency": "usd", "status": "requires_payment_method"}

            orig_get, orig_pi = stripe_client.get_stripe, stripe_client.create_payment_intent
            stripe_client.get_stripe = lambda *a, **k: object()  # type: ignore[assignment]
            stripe_client.create_payment_intent = _fake_intent  # type: ignore[assignment]
            try:
                out = await billing_router._finalise_order(
                    session, redis, eorder, amount_due=eprice,
                    product_name="pts", order_response=None,
                )
            finally:
                stripe_client.get_stripe, stripe_client.create_payment_intent = orig_get, orig_pi
            assert out["stripe"]["client_secret"] == "cs_mock_secret"
            assert eorder.stripe_ref == "pi_mock"
            assert calls["amount_cents"] == eprice
            assert calls["idempotency_key"] == f"order:{eorder.id}"

            # billing-disabled degradation (real settings: no key → get_stripe None)
            assert stripe_client.get_stripe() is None
            ws6 = await _new_ws(session)
            touched_ws.append(ws6.id)
            forder, fbd = await service.build_subscription_order(
                session, workspace_id=ws6.id, plan_code="max", duration_days=30,
                addons=None, use_balance=False,
            )
            out2 = await billing_router._finalise_order(
                session, redis, forder, amount_due=fbd.amount_due_cents,
                product_name="max", order_response=service.order_breakdown_dict(fbd, forder.id),
            )
            assert out2["stripe"] is None and out2["billing_disabled"] is True
            assert out2["status"] == "pending" and forder.status == "pending"

            print("live billing scenarios A-E: PASS")
        finally:
            await session.rollback()

    # clean the redis keys the rolled-back scenarios seeded
    for wid in touched_ws:
        await redis.delete(quotas.limits_cache_key(wid), points.balance_key(wid))

    # ---------------------------------------------------------------- F: expiry sweep (committed)
    await _expiry_scenario(sf, redis)
    await close_redis()


async def _expiry_scenario(sf, redis) -> None:
    """Committed mini-scenario: a lapsed Pro subscription is downgraded to Free.
    Guarded so the global sweep never touches a foreign workspace's data."""
    now = datetime.now(UTC)
    async with sf() as session:
        ws = Workspace(name="bill-expire", plan_code="pro", status="active", settings={})
        session.add(ws)
        await session.flush()
        sub = Subscription(
            workspace_id=ws.id, plan_code="pro", status="active",
            current_period_start=now - timedelta(days=40),
            current_period_end=now - timedelta(days=1),  # lapsed
        )
        session.add(sub)
        await session.commit()
        ws_id, sub_id = ws.id, sub.id

    try:
        async with sf() as session:
            foreign = (await session.execute(
                select(func.count()).select_from(Subscription).where(
                    Subscription.status.in_(sub_svc._ACTIVE_STATES),
                    Subscription.current_period_end.is_not(None),
                    Subscription.current_period_end < now,
                    Subscription.workspace_id != ws_id,
                )
            )).scalar_one()
        if foreign:
            pytest.skip(f"{foreign} foreign expired subs present; skipping destructive sweep")

        n = await sub_svc.expire_due_subscriptions(sf, redis, now=now)
        assert n >= 1
        async with sf() as session:
            sub = await session.get(Subscription, sub_id)
            ws = await session.get(Workspace, ws_id)
            assert sub.status == "canceled"
            assert ws.plan_code == "free", "lapsed subscription downgraded to Free"
        print("live billing scenario F (expiry): PASS")
    finally:
        async with sf() as session:
            ws = await session.get(Workspace, ws_id)
            if ws is not None:
                await session.delete(ws)  # cascade drops the subscription
                await session.commit()
        await redis.delete(quotas.limits_cache_key(ws_id))
