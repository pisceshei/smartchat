"""AI points metering (plan A.1 / B.2).

Balance = Redis counter (aipoints:{ws}); the hot path is an atomic Lua
check-and-decrement with a floor-0 hard reject. The append-only ledger row +
points.consumed outbox event are written in the caller's DB transaction.
Nightly reconcile re-derives the Redis counter from the ledger, so a rolled-
back business transaction can only under-count points temporarily, never
over-spend them.

Price list is config (plans fixture / settings), not code — callers pass the
cost they looked up.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from py_contracts.events import Actor, Event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.tenancy import AIPointsLedger, Plan, Workspace
from . import event_bus

log = logging.getLogger("smartchat.points")

# KEYS[1] = balance key, ARGV[1] = cost.
# Returns: new balance (>=0) on success, -1 = insufficient (floor-0 reject),
# -2 = balance not loaded (caller loads from ledger then retries).
CHECK_AND_DECR_LUA = """\
local bal = redis.call('GET', KEYS[1])
if not bal then return -2 end
bal = tonumber(bal)
local cost = tonumber(ARGV[1])
if cost < 0 then return redis.error_reply('negative cost') end
if bal < cost then return -1 end
return redis.call('DECRBY', KEYS[1], cost)
"""


def balance_key(workspace_id: uuid.UUID | str) -> str:
    return f"aipoints:{workspace_id}"


def grant_ref(period_month: str) -> str:
    """Idempotency ref for a monthly grant."""
    return f"grant:{period_month}"


@dataclass
class SpendResult:
    ok: bool
    balance_after: int
    reason: str  # "ok" | "insufficient"


async def load_balance(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Authoritative balance = latest ledger balance_after (0 if no rows)."""
    row = (
        await session.execute(
            select(AIPointsLedger.balance_after)
            .where(AIPointsLedger.workspace_id == workspace_id)
            .order_by(AIPointsLedger.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row or 0)


async def reconcile_balance(
    session: AsyncSession, redis: aioredis.Redis, workspace_id: uuid.UUID
) -> int:
    """Nightly / on-demand: force Redis counter to the ledger truth."""
    bal = await load_balance(session, workspace_id)
    await redis.set(balance_key(workspace_id), bal)
    return bal


async def check_and_decr(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    cost: int,
    reason: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> SpendResult:
    """Atomically spend `cost` points. On success writes the ledger row and a
    points.consumed outbox event into the caller's session (commit together
    with the business write). On floor-0 reject emits nothing and returns
    ok=False — the caller applies its feature-specific hard-stop behavior."""
    if cost <= 0:
        bal = int(await redis.get(balance_key(workspace_id)) or 0)
        return SpendResult(ok=True, balance_after=bal, reason="ok")
    key = balance_key(workspace_id)
    result = int(await redis.eval(CHECK_AND_DECR_LUA, 1, key, cost))
    if result == -2:  # not loaded — seed from ledger, retry once
        bal = await load_balance(session, workspace_id)
        await redis.set(key, bal, nx=True)
        result = int(await redis.eval(CHECK_AND_DECR_LUA, 1, key, cost))
    if result == -1:
        current = int(await redis.get(key) or 0)
        return SpendResult(ok=False, balance_after=current, reason="insufficient")
    await session.execute(
        AIPointsLedger.__table__.insert().values(
            workspace_id=workspace_id,
            delta=-cost,
            balance_after=result,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    )
    await event_bus.emit(
        session,
        Event(
            workspace_id=workspace_id,
            type="points.consumed",
            actor=Actor(type="system"),
            payload={"cost": cost, "reason": reason, "ref_type": ref_type, "ref_id": ref_id,
                     "balance_after": result},
        ),
    )
    return SpendResult(ok=True, balance_after=result, reason="ok")


async def refund(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    points: int,
    reason: str = "refund",
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> int:
    """Reserve-then-settle helper: give points back (e.g. long operation
    reserved more than it used, or the business transaction failed)."""
    new_bal = int(await redis.incrby(balance_key(workspace_id), points))
    await session.execute(
        AIPointsLedger.__table__.insert().values(
            workspace_id=workspace_id,
            delta=points,
            balance_after=new_bal,
            reason=reason,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    )
    return new_bal


def current_period(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


async def grant_monthly(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    points: int,
    period_month: str,
    expires_at: datetime | None = None,
) -> bool:
    """Idempotent monthly grant (one per workspace per period). Returns True
    if the grant was applied now, False if it already existed."""
    if points <= 0:
        return False
    exists = (
        await session.execute(
            select(func.count())
            .select_from(AIPointsLedger)
            .where(
                AIPointsLedger.workspace_id == workspace_id,
                AIPointsLedger.reason == "monthly_grant",
                AIPointsLedger.ref_id == grant_ref(period_month),
            )
        )
    ).scalar_one()
    if exists:
        return False
    new_bal = int(await redis.incrby(balance_key(workspace_id), points))
    await session.execute(
        AIPointsLedger.__table__.insert().values(
            workspace_id=workspace_id,
            delta=points,
            balance_after=new_bal,
            reason="monthly_grant",
            ref_type="period",
            ref_id=grant_ref(period_month),
            expires_at=expires_at,
        )
    )
    return True


async def run_monthly_grants(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    now: datetime | None = None,
) -> int:
    """Grant this month's plan points to every active workspace that hasn't
    received them yet (idempotent — safe to run hourly)."""
    period = current_period(now)
    granted = 0
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Workspace.id, Plan.limits)
                .join(Plan, Plan.code == Workspace.plan_code)
                .where(Workspace.status == "active")
            )
        ).all()
        for ws_id, limits in rows:
            points = int((limits or {}).get("ai_points_monthly") or 0)
            if points <= 0:
                continue
            try:
                if await grant_monthly(
                    session, redis, workspace_id=ws_id, points=points, period_month=period
                ):
                    granted += 1
            except Exception:  # noqa: BLE001
                log.exception("monthly grant failed for %s", ws_id)
        await session.commit()
    return granted


def spend_payload(result: SpendResult) -> dict[str, Any]:
    return {"ok": result.ok, "balance_after": result.balance_after, "reason": result.reason}
