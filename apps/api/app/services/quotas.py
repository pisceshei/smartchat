"""Effective plan limits (plan ⊕ subscription overrides) with a 60s Redis
cache, feature gating dependencies, and monthly usage counters
(Redis-hot, flushed to PG every 30s by beat).

Limit semantics: numbers (-1 = unlimited, 0 = none), booleans for feature
flags. Keys: seats, official_channels, hosted_devices, widgets, mac_monthly,
monthly_replies, ai_points_monthly, broadcast, brand_removal, openapi,
webhook, history_days, translation_chars_monthly.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db import get_session
from ..models.tenancy import Plan, Subscription, UsageCounter, Workspace
from .redis_client import get_redis

log = logging.getLogger("smartchat.quotas")

LIMITS_CACHE_TTL = 60
USAGE_KEY_PREFIX = "usage:"


# --------------------------------------------------------------------------
# pure merge logic (unit-tested)
# --------------------------------------------------------------------------
def merge_limits(plan_limits: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """plan ⊕ overrides: an override key always wins (Custom plan support).
    None override values are ignored (means "no override")."""
    merged = dict(plan_limits or {})
    for k, v in (overrides or {}).items():
        if v is not None:
            merged[k] = v
    return merged


def limit_allows(limits: dict[str, Any], key: str, *, current: int | None = None) -> bool:
    """Boolean features: truthy = allowed. Numeric limits: -1 unlimited,
    otherwise current usage must stay below the cap."""
    val = limits.get(key)
    if val is None or val is False or val == 0:
        return False
    if val is True:
        return True
    if isinstance(val, (int, float)):
        if val < 0:
            return True
        return current is None or current < val
    return bool(val)


def limits_cache_key(workspace_id: uuid.UUID | str) -> str:
    return f"limits:{workspace_id}"


# --------------------------------------------------------------------------
# effective limits
# --------------------------------------------------------------------------
async def effective_limits(
    session: AsyncSession,
    redis: aioredis.Redis,
    workspace_id: uuid.UUID,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    key = limits_cache_key(workspace_id)
    if use_cache:
        cached = await redis.get(key)
        if cached:
            try:
                return json.loads(cached)
            except ValueError:
                pass
    row = (
        await session.execute(
            select(Plan.limits, Workspace.plan_code)
            .join(Workspace, Workspace.plan_code == Plan.code)
            .where(Workspace.id == workspace_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    plan_limits, plan_code = row
    overrides = (
        await session.execute(
            select(Subscription.plan_overrides)
            .where(
                Subscription.workspace_id == workspace_id,
                Subscription.status.in_(["trialing", "active", "past_due"]),
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    merged = merge_limits(plan_limits or {}, overrides)
    merged["_plan_code"] = plan_code
    await redis.set(key, json.dumps(merged), ex=LIMITS_CACHE_TTL)
    return merged


async def invalidate_limits(redis: aioredis.Redis, workspace_id: uuid.UUID) -> None:
    await redis.delete(limits_cache_key(workspace_id))


# --------------------------------------------------------------------------
# FastAPI gating dependencies
# --------------------------------------------------------------------------
def require_feature(feature_key: str):
    """Dependency factory: 403 upgrade_required unless the workspace's
    effective limits enable feature_key. Usage:
        @router.post(..., dependencies=[Depends(require_feature("openapi"))])
    """
    from ..deps import MemberContext, current_member  # local import: deps imports services

    async def dep(
        member: MemberContext = Depends(current_member),
        session: AsyncSession = Depends(get_session),
    ) -> dict[str, Any]:
        limits = await effective_limits(session, get_redis(), member.workspace_id)
        if not limit_allows(limits, feature_key):
            raise HTTPException(
                status_code=403,
                detail={"code": "upgrade_required", "feature": feature_key,
                        "plan": limits.get("_plan_code")},
            )
        return limits

    return dep


# --------------------------------------------------------------------------
# usage counters (Redis hot → PG flush)
# --------------------------------------------------------------------------
def current_period(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


def usage_key(workspace_id: uuid.UUID | str, period_month: str | None = None) -> str:
    return f"{USAGE_KEY_PREFIX}{workspace_id}:{period_month or current_period()}"


async def incr_usage(
    redis: aioredis.Redis,
    workspace_id: uuid.UUID,
    metric: str,
    n: int = 1,
    *,
    period_month: str | None = None,
) -> int:
    return int(await redis.hincrby(usage_key(workspace_id, period_month), metric, n))


async def get_usage(
    session: AsyncSession,
    redis: aioredis.Redis,
    workspace_id: uuid.UUID,
    metric: str,
    *,
    period_month: str | None = None,
) -> int:
    """PG flushed value + Redis pending delta = live usage."""
    period = period_month or current_period()
    flushed = (
        await session.execute(
            select(UsageCounter.value).where(
                UsageCounter.workspace_id == workspace_id,
                UsageCounter.metric == metric,
                UsageCounter.period_month == period,
            )
        )
    ).scalar_one_or_none()
    pending = await redis.hget(usage_key(workspace_id, period), metric)
    return int(flushed or 0) + int(pending or 0)


async def flush_usage_counters(
    session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis
) -> int:
    """Move Redis usage deltas into usage_counters (additive upsert). Uses
    negative HINCRBY after the upsert so increments racing the flush are never
    lost. Runs every 30s in beat."""
    flushed = 0
    async for key in redis.scan_iter(match=f"{USAGE_KEY_PREFIX}*", count=200):
        try:
            _, ws, period = key.rsplit(":", 2) if key.count(":") >= 2 else (None, None, None)
            if not ws or not period:
                continue
            fields: dict[str, str] = await redis.hgetall(key)
            deltas = {m: int(v) for m, v in fields.items() if int(v) != 0}
            if not deltas:
                continue
            async with session_factory() as session:
                async with session.begin():
                    for metric, delta in deltas.items():
                        stmt = pg_insert(UsageCounter).values(
                            workspace_id=uuid.UUID(ws),
                            metric=metric,
                            period_month=period,
                            value=delta,
                        )
                        stmt = stmt.on_conflict_do_update(
                            index_elements=["workspace_id", "metric", "period_month"],
                            set_={"value": UsageCounter.value + stmt.excluded.value},
                        )
                        await session.execute(stmt)
            pipe = redis.pipeline(transaction=False)
            for metric, delta in deltas.items():
                pipe.hincrby(key, metric, -delta)
            await pipe.execute()
            flushed += len(deltas)
        except IntegrityError:
            # Orphan counter for a workspace that no longer exists (e.g. a
            # rolled-back/deleted tenant). Drop the stale key quietly instead
            # of re-erroring every 30s.
            await redis.delete(key)
            log.warning("dropped orphan usage counter key %s", key)
        except Exception:  # noqa: BLE001 — one bad key must not stop the flush
            log.exception("usage flush failed for key %s", key)
    return flushed
