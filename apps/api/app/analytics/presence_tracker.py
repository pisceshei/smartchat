"""Agent online-time tracker (plan 附錄 B.4 / A.8).

Online time is *measured*, not event-derived: the ws-gateway keeps one Redis
key ``presence:m:{ws}:{member_id}`` per online member (SET EX 60, refreshed by
25s heartbeats; ``away`` still counts as connected). Those keys are the single
source of truth for routability AND for reporting.

This tracker reconciles ``agent_presence_sessions`` against those keys on a
short cron:

- present + no open session      → open a session (started now)
- present + open session         → refresh ``last_heartbeat_at``
- open session + key gone         → close it at ``last_heartbeat_at + grace``
  (bounded overestimate ≤ one TTL), so a disconnect that the client never got
  to signal still closes within a minute.

Because it derives purely from the live presence keys it is self-healing:
a tracker outage just leaves sessions open, and the next pass closes the stale
ones. The rollup then overlap-splits each ``[started_at, ended_at]`` interval
into ``agg_agent_hourly.online_seconds`` (see ``rollup.fold_presence``).
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.members import WorkspaceMember
from ..models.reports import AgentPresenceSession
from ..realtime.presence import PRESENCE_TTL, parse_presence_key

log = logging.getLogger("smartchat.analytics.presence_tracker")

GRACE_S = PRESENCE_TTL  # a vanished key means offline within the last ≤TTL seconds
_SCAN_MATCH = "presence:m:*"


async def _present_members(redis: aioredis.Redis) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """(workspace_id, member_id) pairs with a live presence key right now."""
    present: set[tuple[uuid.UUID, uuid.UUID]] = set()
    async for key in redis.scan_iter(match=_SCAN_MATCH, count=500):
        if isinstance(key, bytes):
            key = key.decode()
        parsed = parse_presence_key(key)
        if parsed is None or parsed[0] != "member":
            continue
        try:
            present.add((uuid.UUID(parsed[1]), uuid.UUID(parsed[2])))
        except ValueError:
            continue
    return present


async def reconcile(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """One reconcile pass. Returns {opened, refreshed, closed} counts."""
    now = now or datetime.now(UTC)
    present = await _present_members(redis)
    opened = refreshed = closed = 0

    async with session_factory() as session:
        async with session.begin():
            open_rows = (
                await session.execute(
                    select(AgentPresenceSession).where(AgentPresenceSession.ended_at.is_(None))
                )
            ).scalars().all()
            open_by_member: dict[tuple[uuid.UUID, uuid.UUID], list[AgentPresenceSession]] = {}
            for r in open_rows:
                open_by_member.setdefault((r.workspace_id, r.agent_id), []).append(r)

            # close sessions whose member is no longer present (+ dedupe extras)
            for pair, rows in open_by_member.items():
                rows.sort(key=lambda r: r.started_at, reverse=True)
                keep = rows[0]
                extras = rows[1:]
                if pair in present:
                    keep.last_heartbeat_at = now
                    refreshed += 1
                else:
                    keep.ended_at = _close_at(keep, now)
                    closed += 1
                for extra in extras:  # never leave >1 open session per member
                    extra.ended_at = _close_at(extra, now)
                    closed += 1

            # open sessions for freshly-present members
            new_pairs = [p for p in present if p not in open_by_member]
            valid = await _valid_members(session, [m for _, m in new_pairs])
            for ws_id, member_id in new_pairs:
                if member_id not in valid:
                    continue  # stale key for a deleted member
                session.add(
                    AgentPresenceSession(
                        workspace_id=ws_id,
                        agent_id=member_id,
                        started_at=now,
                        last_heartbeat_at=now,
                    )
                )
                opened += 1

    return {"opened": opened, "refreshed": refreshed, "closed": closed}


def _close_at(row: AgentPresenceSession, now: datetime) -> datetime:
    base = row.last_heartbeat_at or row.started_at
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return min(now, base + timedelta(seconds=GRACE_S))


async def _valid_members(session: AsyncSession, member_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not member_ids:
        return set()
    rows = (
        await session.execute(
            select(WorkspaceMember.id).where(WorkspaceMember.id.in_(set(member_ids)))
        )
    ).scalars().all()
    return set(rows)


async def close_all_open(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> int:
    """Close every open presence session (graceful shutdown / maintenance)."""
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(AgentPresenceSession).where(AgentPresenceSession.ended_at.is_(None))
                )
            ).scalars().all()
            for r in rows:
                r.ended_at = _close_at(r, now)
    return len(rows)
