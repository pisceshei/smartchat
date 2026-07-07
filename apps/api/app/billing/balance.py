"""Prepaid account balance (餘額系統, plan P3 計費模型實測).

``workspace_balance`` is the authoritative total (one row per workspace); every
mutation is an atomic ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` so the
new balance is read back under the row lock, and mirrored into the append-only
``balance_ledger``. All amounts are integer **cents**.

Reasons: ``topup`` (+, Stripe balance purchase), ``order_apply`` (−, balance
折抵 applied to a paid order), ``refund`` (+, Stripe refund credited back to
balance), ``adjust`` (±, manual/admin).
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.billing import BalanceLedger, WorkspaceBalance


async def get_balance(session: AsyncSession, workspace_id: uuid.UUID) -> int:
    """Current balance in cents (0 if the workspace has never had one)."""
    val = (
        await session.execute(
            select(WorkspaceBalance.balance_cents).where(
                WorkspaceBalance.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()
    return int(val or 0)


async def _apply_delta(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    delta_cents: int,
    *,
    reason: str,
    ref: str | None,
    currency: str = "usd",
) -> int:
    """Atomically move the balance by ``delta_cents`` and ledger it. Returns the
    new balance. Caller commits."""
    stmt = (
        pg_insert(WorkspaceBalance)
        .values(workspace_id=workspace_id, balance_cents=delta_cents, currency=currency)
        .on_conflict_do_update(
            index_elements=["workspace_id"],
            set_={
                "balance_cents": WorkspaceBalance.balance_cents + delta_cents,
                "updated_at": func.now(),
            },
        )
        .returning(WorkspaceBalance.balance_cents)
    )
    new_balance = int((await session.execute(stmt)).scalar_one())
    session.add(
        BalanceLedger(
            workspace_id=workspace_id,
            delta_cents=delta_cents,
            reason=reason,
            ref=ref,
            balance_after_cents=new_balance,
        )
    )
    return new_balance


async def topup(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    amount_cents: int,
    *,
    reason: str = "topup",
    ref: str | None = None,
    currency: str = "usd",
) -> int:
    """Credit the balance (Stripe balance purchase / admin grant)."""
    if amount_cents <= 0:
        return await get_balance(session, workspace_id)
    return await _apply_delta(
        session, workspace_id, amount_cents, reason=reason, ref=ref, currency=currency
    )


async def deduct_for_order(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    applied_cents: int,
    *,
    ref: str | None = None,
) -> int:
    """Apply the balance 折抵 recorded on a paid order. Clamps to the balance
    actually available at settle time (the order froze a snapshot; the balance
    may have moved between checkout and webhook) so the ledger never goes
    negative. Returns the amount actually deducted."""
    if applied_cents <= 0:
        return 0
    current = await get_balance(session, workspace_id)
    actual = min(applied_cents, max(current, 0))
    if actual <= 0:
        return 0
    await _apply_delta(session, workspace_id, -actual, reason="order_apply", ref=ref)
    return actual


async def refund_to_balance(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    amount_cents: int,
    *,
    ref: str | None = None,
    reason: str = "refund",
    currency: str = "usd",
) -> int:
    """Credit a Stripe refund back to the prepaid balance."""
    if amount_cents <= 0:
        return await get_balance(session, workspace_id)
    return await _apply_delta(
        session, workspace_id, amount_cents, reason=reason, ref=ref, currency=currency
    )
