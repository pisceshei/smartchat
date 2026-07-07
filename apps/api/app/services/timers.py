"""Generic timer service (plan B.0): PG `timers` is the source of truth, a
Redis ZSET holds the 24h hot window, a 1s poller fires due timers, and boot
reseeds the ZSET from PG. Flow delays / timeout triggers / recurring
broadcasts / auto-close all share this — no in-worker-memory ETAs.

Firing a timer = marking the row fired and emitting an Event(type=timer.kind)
through the transactional outbox, so consumers get timers like any other bus
event (e.g. kind="conversation.timeout.agent" lands on events:conversation).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from py_contracts.events import Actor, Event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.misc import Timer
from . import event_bus

log = logging.getLogger("smartchat.timers")

ZSET_KEY = "timers:hot"
HOT_WINDOW_S = 24 * 3600
FIRE_BATCH = 200
MISS_RETRY_S = 2.0
MISS_MAX_ATTEMPTS = 3


# --------------------------------------------------------------------------
# pure timer math (unit-tested)
# --------------------------------------------------------------------------
def zscore(fire_at: datetime) -> float:
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=UTC)
    return fire_at.timestamp()


def within_hot_window(fire_at: datetime, now: datetime, window_s: int = HOT_WINDOW_S) -> bool:
    """A timer belongs in the Redis hot ZSET iff it fires within the window
    (past-due counts — it should fire on the next tick)."""
    return zscore(fire_at) <= now.timestamp() + window_s


def next_refill_horizon(now: datetime, window_s: int = HOT_WINDOW_S) -> datetime:
    return now + timedelta(seconds=window_s)


# --------------------------------------------------------------------------
# API used by business code
# --------------------------------------------------------------------------
async def schedule(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    kind: str,
    fire_at: datetime,
    ref_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    redis: aioredis.Redis | None = None,
) -> Timer:
    """Insert the PG row (caller's transaction) and, if a redis handle is
    given and fire_at is inside the hot window, ZADD immediately so short
    delays keep 1s precision. Without redis, the poller's refill pass picks
    it up within its refill interval."""
    timer = Timer(
        workspace_id=workspace_id,
        kind=kind,
        ref_id=ref_id,
        fire_at=fire_at,
        payload=payload or {},
        status="pending",
    )
    session.add(timer)
    await session.flush()  # assign id
    if redis is not None and within_hot_window(fire_at, datetime.now(UTC)):
        await redis.zadd(ZSET_KEY, {str(timer.id): zscore(fire_at)})
    return timer


async def cancel(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    kind: str | None = None,
    ref_id: uuid.UUID | None = None,
    timer_id: uuid.UUID | None = None,
    redis: aioredis.Redis | None = None,
) -> int:
    """Cancel pending timers by id, or by (kind, ref_id) — e.g. re-arming a
    conversation timeout cancels the previous one. Returns count cancelled."""
    q = select(Timer).where(Timer.workspace_id == workspace_id, Timer.status == "pending")
    if timer_id is not None:
        q = q.where(Timer.id == timer_id)
    else:
        if kind is None and ref_id is None:
            raise ValueError("cancel() needs timer_id or kind/ref_id")
        if kind is not None:
            q = q.where(Timer.kind == kind)
        if ref_id is not None:
            q = q.where(Timer.ref_id == ref_id)
    rows = (await session.execute(q.with_for_update(skip_locked=True))).scalars().all()
    for row in rows:
        row.status = "cancelled"
    if redis is not None and rows:
        await redis.zrem(ZSET_KEY, *[str(r.id) for r in rows])
    return len(rows)


# --------------------------------------------------------------------------
# poller (runs in beat)
# --------------------------------------------------------------------------
async def refill(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    window_s: int = HOT_WINDOW_S,
) -> int:
    """Load all pending timers inside the hot window into the ZSET. Doubles as
    the boot reseed (ZADD is idempotent — same member, same score)."""
    horizon = next_refill_horizon(datetime.now(UTC), window_s)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Timer.id, Timer.fire_at).where(
                    Timer.status == "pending", Timer.fire_at <= horizon
                )
            )
        ).all()
    if rows:
        await redis.zadd(ZSET_KEY, {str(tid): zscore(fa) for tid, fa in rows})
    return len(rows)


async def fire_due(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    now: datetime | None = None,
    miss_attempts: dict[str, int] | None = None,
) -> int:
    """Fire everything due in the ZSET. Row-locked pending→fired transition
    makes double-firing impossible even with concurrent pollers."""
    now = now or datetime.now(UTC)
    due: list[str] = await redis.zrangebyscore(ZSET_KEY, "-inf", now.timestamp(), start=0, num=FIRE_BATCH)
    if not due:
        return 0
    fired = 0
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(Timer)
                    .where(Timer.id.in_([uuid.UUID(d) for d in due]), Timer.status == "pending")
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            found = {str(r.id) for r in rows}
            for timer in rows:
                timer.status = "fired"
                timer.fired_at = now
                await event_bus.emit(
                    session,
                    Event(
                        workspace_id=timer.workspace_id,
                        type=timer.kind,
                        actor=Actor(type="system"),
                        conversation_id=_maybe_uuid(timer.payload.get("conversation_id"))
                        if timer.payload
                        else None,
                        payload={
                            **(timer.payload or {}),
                            "timer_id": str(timer.id),
                            "ref_id": str(timer.ref_id) if timer.ref_id else None,
                            "kind": timer.kind,
                        },
                    ),
                )
                fired += 1
    # ZSET cleanup: processed ids leave; unseen ids (uncommitted schedule or
    # already fired/cancelled elsewhere) get a bounded retry then drop —
    # the refill pass re-adds anything still legitimately pending.
    to_remove = list(found)
    if miss_attempts is not None:
        for d in due:
            if d in found:
                miss_attempts.pop(d, None)
                continue
            n = miss_attempts.get(d, 0) + 1
            if n >= MISS_MAX_ATTEMPTS:
                to_remove.append(d)
                miss_attempts.pop(d, None)
            else:
                miss_attempts[d] = n
                await redis.zadd(ZSET_KEY, {d: now.timestamp() + MISS_RETRY_S})
    else:
        to_remove = due
    if to_remove:
        await redis.zrem(ZSET_KEY, *to_remove)
    return fired


def _maybe_uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except (ValueError, AttributeError, TypeError):
        return None


async def poller(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    tick: float = 1.0,
    refill_every_ticks: int = 60,
    stop: asyncio.Event | None = None,
) -> None:
    """1s loop: fire due timers; every refill_every_ticks also sync ZSET from
    PG (catches schedules made without a redis handle and self-heals). Boot
    reseed = the immediate first refill."""
    miss_attempts: dict[str, int] = {}
    ticks = 0
    try:
        await refill(session_factory, redis)
    except Exception:  # noqa: BLE001
        log.exception("timer boot reseed failed")
    while stop is None or not stop.is_set():
        try:
            await fire_due(session_factory, redis, miss_attempts=miss_attempts)
        except Exception:  # noqa: BLE001
            log.exception("timer fire pass failed")
        ticks += 1
        if ticks % refill_every_ticks == 0:
            try:
                await refill(session_factory, redis)
            except Exception:  # noqa: BLE001
                log.exception("timer refill failed")
        try:
            if stop is not None:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            else:
                await asyncio.sleep(tick)
        except TimeoutError:
            pass
