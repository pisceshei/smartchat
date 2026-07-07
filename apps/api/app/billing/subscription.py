"""Subscription plan/duration change effects (plan P3 計費模型實測).

A paid subscription order (or the admin no-charge path) applies here:

- **Plan switch** — ``workspaces.plan_code`` is updated so
  ``quotas.effective_limits`` reads the new plan.
- **Duration** — ``current_period_end`` is extended by ``duration_days``,
  anchored at the later of *now* and the existing end (so a renewal of the same
  plan stacks time rather than truncating it).
- **Add-on expansion** — extra seats / official_channels / hosted_devices are
  written as ``plan_overrides`` (= plan base + purchased qty) so effective
  limits reflect the expanded quota. The raw purchased counts are kept under the
  ``_addons`` override key for the subscription view.

Every change invalidates the 60s effective-limits cache.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.tenancy import Plan, Subscription, Workspace
from ..services import quotas

ADDON_KEYS: tuple[str, ...] = ("seats", "official_channels", "hosted_devices")
DEFAULT_DURATION_DAYS = 30
_ACTIVE_STATES = ("trialing", "active", "past_due")


def compute_overrides(plan_limits: dict[str, Any], addons: dict[str, int] | None) -> dict[str, Any]:
    """plan_overrides for the purchased add-ons: expanded numeric caps + the raw
    ``_addons`` counts. An unlimited (-1) base is left unbounded."""
    overrides: dict[str, Any] = {}
    raw: dict[str, int] = {}
    for key in ADDON_KEYS:
        qty = int((addons or {}).get(key, 0) or 0)
        if qty <= 0:
            continue
        raw[key] = qty
        base = plan_limits.get(key)
        if isinstance(base, (int, float)) and base >= 0:
            overrides[key] = int(base) + qty
    if raw:
        overrides["_addons"] = raw
    return overrides


async def latest_subscription(
    session: AsyncSession, workspace_id: uuid.UUID
) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription)
            .where(Subscription.workspace_id == workspace_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def apply_plan_change(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    plan_code: str,
    duration_days: int | None,
    addons: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Subscription:
    """Switch plan + extend period + expand add-on quota. Idempotent-safe to call
    from either the webhook (paid) or the admin path (no charge). Caller commits."""
    now = now or datetime.now(UTC)
    plan = await session.get(Plan, plan_code)
    if plan is None:
        raise ValueError(f"unknown plan: {plan_code!r}")
    ws = await session.get(Workspace, workspace_id)
    if ws is None:
        raise ValueError(f"unknown workspace: {workspace_id!r}")

    duration = int(duration_days or DEFAULT_DURATION_DAYS)
    overrides = compute_overrides(plan.limits or {}, addons)
    sub = await latest_subscription(session, workspace_id)

    if sub is None:
        sub = Subscription(
            workspace_id=workspace_id,
            plan_code=plan_code,
            status="active",
            plan_overrides=overrides,
            current_period_start=now,
            current_period_end=now + timedelta(days=duration),
        )
        session.add(sub)
    else:
        # renewal of the SAME plan stacks onto the remaining time; a switch
        # (or a lapsed period) starts a fresh window at now.
        prev_end = sub.current_period_end
        same_plan = sub.plan_code == plan_code
        anchor = prev_end if (same_plan and prev_end is not None and prev_end > now) else now
        sub.plan_code = plan_code
        sub.status = "active"
        sub.plan_overrides = overrides
        sub.current_period_start = now
        sub.current_period_end = anchor + timedelta(days=duration)

    ws.plan_code = plan_code
    await session.flush()
    await quotas.invalidate_limits(redis, workspace_id)
    return sub


async def expire_due_subscriptions(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    now: datetime | None = None,
) -> int:
    """Downgrade every active subscription whose ``current_period_end`` has
    passed to Free (status→canceled, workspace→free plan) and invalidate its
    limits cache. Idempotent; safe to run hourly. Returns the count expired."""
    now = now or datetime.now(UTC)
    expired = 0
    async with session_factory() as session:
        subs = (
            await session.execute(
                select(Subscription).where(
                    Subscription.status.in_(_ACTIVE_STATES),
                    Subscription.current_period_end.is_not(None),
                    Subscription.current_period_end < now,
                )
            )
        ).scalars().all()
        for sub in subs:
            sub.status = "canceled"
            ws = await session.get(Workspace, sub.workspace_id)
            if ws is not None and ws.plan_code != "free":
                ws.plan_code = "free"
            await quotas.invalidate_limits(redis, sub.workspace_id)
            expired += 1
        await session.commit()
    return expired
