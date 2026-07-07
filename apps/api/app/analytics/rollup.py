"""Streaming-incremental + catch-up rollup (plan 附錄 B.4).

The P1/P2 ``events`` table is the single raw store — there is **no** second
write path. This module folds those events into the hourly aggregate tables:

- ``agg_messages_hourly``       message volume  (channel × agent × direction × ai)
- ``agg_conversations_hourly``  lifecycle + first-response/resolution sums
- ``agg_agent_hourly``          per-agent productivity (+ online_seconds)

Design (exactly-once, resumable, late-event-safe):

1. **Incremental pass** (``run_rollup_pass``) reads the events table ordered by
   ``(occurred_at, id)`` past a single ``rollup_watermark`` row (aggregate=
   ``"hourly"``), folds each event, upserts additive deltas, and advances the
   watermark — all in one transaction, so a crash re-folds nothing twice.
2. **Stream consumer** (``run_rollup_consumer``) joins the ``rollup`` consumer
   group on events:conversation/visitor/broadcast purely as a *low-latency
   wake signal*: on new entries it drains the table (path 1) and acks. The
   table+watermark stays the source of truth, so the stream can trim / lose
   entries without affecting correctness.
3. **Nightly catch-up** (``reaggregate_window``) recomputes the trailing 48h of
   hour buckets from scratch (late/back-dated events land here) and re-pins the
   watermark. A Redis lock serialises it against the incremental pass.

Online time is not event-derived — it is folded from ``agent_presence_sessions``
by overlap-splitting each interval across UTC hours (see ``fold_presence``).
Distinct-count customer/ad day tables are computed nightly (``daily`` module).

Sums + counts only, never averages: buckets stay additively mergeable and the
query layer derives ``avg = sum ÷ n`` at read time.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from py_contracts.events import Event
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.conversations import ConversationSession
from ..models.misc import EventRow  # the outbox table doubles as the raw store
from ..models.reports import (
    AggAgentHourly,
    AggConversationsHourly,
    AggMessagesHourly,
    RollupWatermark,
)
from ..services import event_bus
from . import collectors

log = logging.getLogger("smartchat.analytics.rollup")

HOURLY_WATERMARK = "hourly"
ROLLUP_STREAMS = ["events:conversation", "events:visitor", "events:broadcast"]
ROLLUP_GROUP = "rollup"
REAGG_WINDOW_H = 48
PRESENCE_HOT_WINDOW_H = 3
_LOCK_KEY = "rollup:lock"
_LOCK_TTL_S = 300


# ==========================================================================
# in-memory accumulator (a batch of events → additive deltas)
# ==========================================================================
@dataclass
class _ConvAgg:
    opened: int = 0
    resolved: int = 0
    reopened: int = 0
    frt_sum_s: int = 0
    frt_n: int = 0
    resolution_sum_s: int = 0
    resolution_n: int = 0


@dataclass
class _AgentAgg:
    msgs: int = 0
    convs: int = 0
    frt_sum_s: int = 0
    frt_n: int = 0
    csat_sum: int = 0
    csat_n: int = 0


@dataclass
class Accumulator:
    """Keyed additive deltas for one fold batch. Keys embed workspace_id +
    UTC hour so a single global scan folds every tenant at once."""

    messages: dict[tuple, int] = field(default_factory=lambda: defaultdict(int))
    convs: dict[tuple, _ConvAgg] = field(default_factory=lambda: defaultdict(_ConvAgg))
    agents: dict[tuple, _AgentAgg] = field(default_factory=lambda: defaultdict(_AgentAgg))

    def empty(self) -> bool:
        return not (self.messages or self.convs or self.agents)


def _delta_seconds(a: datetime | None, b: datetime | None) -> int | None:
    """max(0, a-b) seconds; None if either endpoint is missing."""
    if a is None or b is None:
        return None
    return max(0, int((collectors.ensure_utc(a) - collectors.ensure_utc(b)).total_seconds()))


def fold_event(
    acc: Accumulator,
    ev: Event,
    *,
    session_started_at: dict[uuid.UUID, datetime] | None = None,
) -> None:
    """Fold one decoded event into the accumulator. Pure except for the
    (already-materialised) session-start lookup used by FRT / resolution."""
    ws = ev.workspace_id
    hour = collectors.floor_hour(ev.occurred_at)
    starts = session_started_at or {}

    mv = collectors.message_volume(ev)
    if mv is not None:
        acc.messages[(ws, hour, mv.channel_type, mv.agent_id, mv.direction, mv.ai_flag)] += 1

    amid = collectors.agent_message_id(ev)
    if amid is not None:
        acc.agents[(ws, hour, amid)].msgs += 1

    opened_ch = collectors.opened_channel(ev)
    if opened_ch is not None:
        ca = acc.convs[(ws, hour, opened_ch)]
        if collectors.reopened(ev):
            ca.reopened += 1
        else:
            ca.opened += 1

    aa = collectors.assigned_agent_id(ev)
    if aa is not None:
        acc.agents[(ws, hour, aa)].convs += 1

    res = collectors.resolved(ev)
    if res is not None:
        ca = acc.convs[(ws, hour, res.channel_type)]
        ca.resolved += 1
        started = starts.get(res.session_id) if res.session_id else None
        secs = _delta_seconds(res.closed_at, started)
        if secs is not None:
            ca.resolution_sum_s += secs
            ca.resolution_n += 1

    fr = collectors.first_responded(ev)
    if fr is not None:
        started = starts.get(fr.session_id) if fr.session_id else None
        secs = _delta_seconds(fr.first_response_at, started)
        if secs is not None:
            acc.convs[(ws, hour, fr.channel_type)].frt_sum_s += secs
            acc.convs[(ws, hour, fr.channel_type)].frt_n += 1
            if fr.agent_id is not None:
                ag = acc.agents[(ws, hour, fr.agent_id)]
                ag.frt_sum_s += secs
                ag.frt_n += 1

    cs = collectors.csat(ev)
    if cs is not None and cs.agent_id is not None:
        ag = acc.agents[(ws, hour, cs.agent_id)]
        ag.csat_sum += cs.score
        ag.csat_n += 1


async def _collect_session_starts(
    session: AsyncSession, events: list[Event]
) -> dict[uuid.UUID, datetime]:
    """Batch-load conversation_session.started_at for the resolved /
    first-responded events in this fold batch (FRT / resolution need it)."""
    ids: set[uuid.UUID] = set()
    for ev in events:
        r = collectors.resolved(ev)
        if r is not None and r.session_id is not None:
            ids.add(r.session_id)
        f = collectors.first_responded(ev)
        if f is not None and f.session_id is not None:
            ids.add(f.session_id)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ConversationSession.id, ConversationSession.started_at).where(
                ConversationSession.id.in_(ids)
            )
        )
    ).all()
    return {rid: started for rid, started in rows}


# ==========================================================================
# apply accumulator → additive upserts
# ==========================================================================
async def apply_accumulator(session: AsyncSession, acc: Accumulator) -> None:
    if acc.messages:
        rows = [
            {
                "workspace_id": ws,
                "hour": hour,
                "channel_type": ch,
                "agent_id": agent,
                "direction": direction,
                "ai_flag": ai,
                "count": n,
            }
            for (ws, hour, ch, agent, direction, ai), n in acc.messages.items()
        ]
        stmt = pg_insert(AggMessagesHourly).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["workspace_id", "hour", "channel_type", "agent_id", "direction", "ai_flag"],
            set_={"count": AggMessagesHourly.count + stmt.excluded.count},
        )
        await session.execute(stmt)

    if acc.convs:
        rows = [
            {
                "workspace_id": ws,
                "hour": hour,
                "channel_type": ch,
                "opened": a.opened,
                "resolved": a.resolved,
                "reopened": a.reopened,
                "frt_sum_s": a.frt_sum_s,
                "frt_n": a.frt_n,
                "resolution_sum_s": a.resolution_sum_s,
                "resolution_n": a.resolution_n,
            }
            for (ws, hour, ch), a in acc.convs.items()
        ]
        stmt = pg_insert(AggConversationsHourly).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["workspace_id", "hour", "channel_type"],
            set_={
                "opened": AggConversationsHourly.opened + stmt.excluded.opened,
                "resolved": AggConversationsHourly.resolved + stmt.excluded.resolved,
                "reopened": AggConversationsHourly.reopened + stmt.excluded.reopened,
                "frt_sum_s": AggConversationsHourly.frt_sum_s + stmt.excluded.frt_sum_s,
                "frt_n": AggConversationsHourly.frt_n + stmt.excluded.frt_n,
                "resolution_sum_s": AggConversationsHourly.resolution_sum_s
                + stmt.excluded.resolution_sum_s,
                "resolution_n": AggConversationsHourly.resolution_n + stmt.excluded.resolution_n,
            },
        )
        await session.execute(stmt)

    if acc.agents:
        rows = [
            {
                "workspace_id": ws,
                "hour": hour,
                "agent_id": agent,
                "msgs": a.msgs,
                "convs": a.convs,
                "frt_sum_s": a.frt_sum_s,
                "frt_n": a.frt_n,
                "csat_sum": a.csat_sum,
                "csat_n": a.csat_n,
                "online_seconds": 0,
            }
            for (ws, hour, agent), a in acc.agents.items()
        ]
        stmt = pg_insert(AggAgentHourly).values(rows)
        # NB: online_seconds is owned by the presence fold — never touched here.
        stmt = stmt.on_conflict_do_update(
            index_elements=["workspace_id", "hour", "agent_id"],
            set_={
                "msgs": AggAgentHourly.msgs + stmt.excluded.msgs,
                "convs": AggAgentHourly.convs + stmt.excluded.convs,
                "frt_sum_s": AggAgentHourly.frt_sum_s + stmt.excluded.frt_sum_s,
                "frt_n": AggAgentHourly.frt_n + stmt.excluded.frt_n,
                "csat_sum": AggAgentHourly.csat_sum + stmt.excluded.csat_sum,
                "csat_n": AggAgentHourly.csat_n + stmt.excluded.csat_n,
            },
        )
        await session.execute(stmt)


# ==========================================================================
# watermark helpers
# ==========================================================================
async def _get_watermark(session: AsyncSession, key: str) -> tuple[datetime | None, uuid.UUID | None]:
    row = await session.get(RollupWatermark, key)
    if row is None:
        return None, None
    return row.last_occurred_at, row.last_event_id


async def _set_watermark(
    session: AsyncSession, key: str, occurred_at: datetime | None, event_id: uuid.UUID | None
) -> None:
    stmt = pg_insert(RollupWatermark).values(
        aggregate=key,
        last_occurred_at=occurred_at,
        last_event_id=event_id,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["aggregate"],
        set_={
            "last_occurred_at": stmt.excluded.last_occurred_at,
            "last_event_id": stmt.excluded.last_event_id,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)


def _after_watermark(last_ts: datetime | None, last_id: uuid.UUID | None):
    """SQL predicate for events strictly after (last_ts, last_id)."""
    if last_ts is None:
        return EventRow.id.isnot(None)  # everything
    return or_(
        EventRow.occurred_at > last_ts,
        and_(EventRow.occurred_at == last_ts, EventRow.id > last_id),
    )


# ==========================================================================
# incremental pass
# ==========================================================================
async def run_rollup_pass(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch: int = 500,
    max_batches: int = 200,
) -> int:
    """Fold every event past the watermark into the hourly aggregates. One
    transaction per batch (fold + upsert + watermark advance = exactly-once).
    Returns the number of events consumed."""
    total = 0
    for _ in range(max_batches):
        async with session_factory() as session:
            async with session.begin():
                last_ts, last_id = await _get_watermark(session, HOURLY_WATERMARK)
                rows = (
                    await session.execute(
                        select(EventRow)
                        .where(_after_watermark(last_ts, last_id))
                        .order_by(EventRow.occurred_at, EventRow.id)
                        .limit(batch)
                    )
                ).scalars().all()
                if not rows:
                    return total
                events = [event_bus.row_to_event(r) for r in rows]
                starts = await _collect_session_starts(session, events)
                acc = Accumulator()
                for ev in events:
                    fold_event(acc, ev, session_started_at=starts)
                await apply_accumulator(session, acc)
                await _set_watermark(session, HOURLY_WATERMARK, rows[-1].occurred_at, rows[-1].id)
                total += len(rows)
        if len(rows) < batch:
            break
    return total


# ==========================================================================
# presence → online_seconds (overlap-split across UTC hours)
# ==========================================================================
def split_presence_seconds(
    started_at: datetime,
    ended_at: datetime | None,
    *,
    now: datetime,
    window_start: datetime,
    window_end: datetime,
) -> dict[datetime, int]:
    """Seconds an online interval contributes to each UTC hour it overlaps,
    clamped to [window_start, window_end]. Open intervals (ended_at=None) run
    to ``now``. DST-agnostic: UTC hours are fixed 3600s slices."""
    start = collectors.ensure_utc(started_at)
    end = collectors.ensure_utc(ended_at) if ended_at is not None else collectors.ensure_utc(now)
    start = max(start, collectors.ensure_utc(window_start))
    end = min(end, collectors.ensure_utc(window_end))
    out: dict[datetime, int] = {}
    if end <= start:
        return out
    cur = collectors.floor_hour(start)
    while cur < end:
        nxt = cur + timedelta(hours=1)
        seg = (min(end, nxt) - max(start, cur)).total_seconds()
        if seg > 0:
            out[cur] = out.get(cur, 0) + int(round(seg))
        cur = nxt
    return out


async def fold_presence(
    session: AsyncSession, *, window_start: datetime, window_end: datetime, now: datetime | None = None
) -> int:
    """Recompute agg_agent_hourly.online_seconds for every UTC hour in the
    window from agent_presence_sessions (SET, not add — each pass fully
    recomputes the window so open/closing sessions converge). Returns bucket
    rows written."""
    from ..models.reports import AgentPresenceSession

    now = now or datetime.now(UTC)
    window_start = collectors.floor_hour(window_start)
    window_end = collectors.ensure_utc(window_end)
    rows = (
        await session.execute(
            select(AgentPresenceSession).where(
                AgentPresenceSession.started_at < window_end,
                or_(
                    AgentPresenceSession.ended_at.is_(None),
                    AgentPresenceSession.ended_at > window_start,
                ),
            )
        )
    ).scalars().all()
    buckets: dict[tuple[uuid.UUID, uuid.UUID, datetime], int] = defaultdict(int)
    for s in rows:
        per_hour = split_presence_seconds(
            s.started_at, s.ended_at, now=now, window_start=window_start, window_end=window_end
        )
        for hour, secs in per_hour.items():
            buckets[(s.workspace_id, s.agent_id, hour)] += secs
    if not buckets:
        return 0
    values = [
        {
            "workspace_id": ws,
            "hour": hour,
            "agent_id": agent,
            "msgs": 0,
            "convs": 0,
            "frt_sum_s": 0,
            "frt_n": 0,
            "csat_sum": 0,
            "csat_n": 0,
            "online_seconds": secs,
        }
        for (ws, agent, hour), secs in buckets.items()
    ]
    stmt = pg_insert(AggAgentHourly).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["workspace_id", "hour", "agent_id"],
        set_={"online_seconds": stmt.excluded.online_seconds},
    )
    await session.execute(stmt)
    return len(values)


# ==========================================================================
# nightly catch-up (trailing 48h re-aggregation for late arrivals)
# ==========================================================================
async def reaggregate_window(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    hours: int = REAGG_WINDOW_H,
    now: datetime | None = None,
) -> int:
    """Recompute the trailing ``hours`` of hourly buckets from scratch, folding
    late / back-dated events the incremental pass skipped. Serialised against
    the incremental pass by the caller (Redis lock). Re-pins the watermark to a
    fresh event-table high-water so the incremental resumes cleanly."""
    now = now or datetime.now(UTC)
    start = collectors.floor_hour(now - timedelta(hours=hours))
    async with session_factory() as session:
        async with session.begin():
            hi = (
                await session.execute(
                    select(EventRow.occurred_at, EventRow.id)
                    .order_by(EventRow.occurred_at.desc(), EventRow.id.desc())
                    .limit(1)
                )
            ).first()
            hi_ts, hi_id = (hi[0], hi[1]) if hi else (None, None)

            # wipe event-derived buckets in the window (online_seconds preserved)
            await session.execute(delete(AggMessagesHourly).where(AggMessagesHourly.hour >= start))
            await session.execute(
                delete(AggConversationsHourly).where(AggConversationsHourly.hour >= start)
            )
            await session.execute(
                update(AggAgentHourly)
                .where(AggAgentHourly.hour >= start)
                .values(msgs=0, convs=0, frt_sum_s=0, frt_n=0, csat_sum=0, csat_n=0)
            )

            folded = 0
            if hi_ts is not None:
                rows = (
                    await session.execute(
                        select(EventRow)
                        .where(
                            EventRow.occurred_at >= start,
                            or_(
                                EventRow.occurred_at < hi_ts,
                                and_(EventRow.occurred_at == hi_ts, EventRow.id <= hi_id),
                            ),
                        )
                        .order_by(EventRow.occurred_at, EventRow.id)
                    )
                ).scalars().all()
                events = [event_bus.row_to_event(r) for r in rows]
                starts = await _collect_session_starts(session, events)
                acc = Accumulator()
                for ev in events:
                    fold_event(acc, ev, session_started_at=starts)
                await apply_accumulator(session, acc)
                folded = len(rows)

            await fold_presence(session, window_start=start, window_end=now, now=now)
            await _set_watermark(session, HOURLY_WATERMARK, hi_ts, hi_id)
    return folded


# ==========================================================================
# lock + orchestration
# ==========================================================================
async def _with_lock(redis: aioredis.Redis, coro_factory) -> int:
    """Serialise rollup passes across processes with a Redis NX lock."""
    token = uuid.uuid4().hex
    if not await redis.set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL_S):
        return 0
    try:
        return await coro_factory()
    finally:
        try:
            if await redis.get(_LOCK_KEY) == token:
                await redis.delete(_LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass


async def run_incremental(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    now: datetime | None = None,
) -> int:
    """Frequent pass: fold new events + refresh the hot (3h) presence window."""
    async def _do() -> int:
        n = await run_rollup_pass(session_factory)
        _now = now or datetime.now(UTC)
        async with session_factory() as session:
            async with session.begin():
                await fold_presence(
                    session,
                    window_start=_now - timedelta(hours=PRESENCE_HOT_WINDOW_H),
                    window_end=_now,
                    now=_now,
                )
        return n

    return await _with_lock(redis, _do)


async def run_nightly(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    now: datetime | None = None,
) -> int:
    async def _do() -> int:
        return await reaggregate_window(session_factory, now=now)

    return await _with_lock(redis, _do)


# ==========================================================================
# stream consumer group 'rollup' (low-latency wake signal only)
# ==========================================================================
async def run_rollup_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    consumer: str = "rollup-1",
    count: int = 256,
    block_ms: int = 5000,
) -> int:
    """Read the rollup consumer group as a wake signal, then drain the events
    table (the authoritative watermark path) and ack. Correctness never depends
    on stream retention."""
    for stream in ROLLUP_STREAMS:
        await event_bus.ensure_group(redis, stream, ROLLUP_GROUP)
    resp = await redis.xreadgroup(
        ROLLUP_GROUP, consumer, {s: ">" for s in ROLLUP_STREAMS}, count=count, block=block_ms
    )
    entry_ids: list[tuple[str, str]] = []
    for stream, entries in resp or []:
        for entry_id, _fields in entries:
            entry_ids.append((stream, entry_id))
    if not entry_ids:
        return 0
    await run_incremental(session_factory, redis)
    pipe = redis.pipeline(transaction=False)
    for stream, entry_id in entry_ids:
        pipe.xack(stream, ROLLUP_GROUP, entry_id)
    await pipe.execute()
    return len(entry_ids)
