"""Grant purchased AI points (加購, plan P3).

A points top-up order, once paid, credits the workspace's points balance:
+delta into the Redis counter and a ``purchase`` row in the append-only
``ai_points_ledger``. Idempotent by the order ref (a replayed webhook must not
double-grant), and the ledger ``balance_after`` is derived from ledger truth
(not the possibly-evicted Redis counter) so it stays authoritative.
"""
from __future__ import annotations

import uuid

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.tenancy import AIPointsLedger
from ..services import points

PURCHASE_REASON = "purchase"


async def already_granted(
    session: AsyncSession, workspace_id: uuid.UUID, ref: str
) -> bool:
    n = (
        await session.execute(
            select(func.count())
            .select_from(AIPointsLedger)
            .where(
                AIPointsLedger.workspace_id == workspace_id,
                AIPointsLedger.reason == PURCHASE_REASON,
                AIPointsLedger.ref_id == ref,
            )
        )
    ).scalar_one()
    return bool(n)


async def grant_purchase(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    points_amount: int,
    ref: str,
) -> int:
    """Credit ``points_amount`` purchased points. Idempotent per ``ref``.
    Returns the resulting balance. Caller commits."""
    if points_amount <= 0:
        return await points.load_balance(session, workspace_id)
    if await already_granted(session, workspace_id, ref):
        return await points.load_balance(session, workspace_id)

    # ledger truth → balance_after; keep the Redis counter consistent (seed from
    # ledger if it was never loaded, then increment) so live spends stay correct.
    current = await points.load_balance(session, workspace_id)
    new_balance = current + points_amount
    key = points.balance_key(workspace_id)
    await redis.set(key, current, nx=True)
    await redis.incrby(key, points_amount)

    await session.execute(
        AIPointsLedger.__table__.insert().values(
            workspace_id=workspace_id,
            delta=points_amount,
            balance_after=new_balance,
            reason=PURCHASE_REASON,
            ref_type="order",
            ref_id=ref,
        )
    )
    return new_balance
