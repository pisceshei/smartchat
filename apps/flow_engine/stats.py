"""Flow analytics accumulation + rollup (plan B.1 統計口徑).

- 觸發次數 = sessions created         → Redis hash counter, flushed additively
- 觸發人數 = distinct contacts        → flow_stats_users rows (exact, never
                                         COUNT(DISTINCT) at query time)
- 參與度   = engaged users / triggered users  (engaged = ≥1 interactive step)
- 完成度   = completed sessions / total sessions

Hot path writes Redis hash `flowstats:{ws}:{day}` (day = workspace-tz boundary)
plus per-user rows; a 60s cron (registered in jobs/worker.py) flushes the hash
into flow_stats_daily and re-materialises the distinct-user counts.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.models.flows import FlowSession, FlowStatsDaily, FlowStatsUser

log = logging.getLogger("smartchat.flow.stats")

KEY_PREFIX = "flowstats:"


def day_for(now: datetime, tz_name: str | None) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        local = now.astimezone(ZoneInfo(tz_name)) if tz_name and tz_name != "UTC" else now.astimezone(UTC)
    except Exception:  # noqa: BLE001
        local = now.astimezone(UTC)
    return local.date().isoformat()


def stats_key(workspace_id: uuid.UUID | str, day: str) -> str:
    return f"{KEY_PREFIX}{workspace_id}:{day}"


def _field(flow_id: uuid.UUID | str, metric: str) -> str:
    return f"{flow_id}:{metric}"


# ==========================================================================
# hot-path writes (in the interpreter's transaction)
# ==========================================================================
async def record_triggered(
    session: AsyncSession,
    redis: aioredis.Redis | None,
    fs: FlowSession,
    tz_name: str | None,
    *,
    now: datetime,
) -> None:
    day = day_for(now, tz_name)
    variables = dict(fs.variables or {})
    variables["_stat_day"] = day
    fs.variables = variables
    if redis is not None:
        try:
            await redis.hincrby(stats_key(fs.workspace_id, day), _field(fs.flow_id, "triggered_sessions"), 1)
        except Exception:  # noqa: BLE001 — metering never blocks a run
            log.debug("flow triggered counter failed", exc_info=True)
    if fs.contact_id is not None:
        await session.execute(
            pg_insert(FlowStatsUser)
            .values(
                workspace_id=fs.workspace_id,
                flow_id=fs.flow_id,
                day=date.fromisoformat(day),
                contact_id=fs.contact_id,
            )
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "flow_id", "day", "contact_id"]
            )
        )


async def record_engaged(session: AsyncSession, fs: FlowSession) -> None:
    fs.engaged = True
    day = (fs.variables or {}).get("_stat_day")
    if not day or fs.contact_id is None:
        return
    await session.execute(
        pg_insert(FlowStatsUser)
        .values(
            workspace_id=fs.workspace_id,
            flow_id=fs.flow_id,
            day=date.fromisoformat(day),
            contact_id=fs.contact_id,
            engaged=True,
        )
        .on_conflict_do_update(
            index_elements=["workspace_id", "flow_id", "day", "contact_id"],
            set_={"engaged": True},
        )
    )


async def record_completed(
    session: AsyncSession,
    redis: aioredis.Redis | None,
    fs: FlowSession,
    tz_name: str | None,
    *,
    now: datetime,
) -> None:
    day = (fs.variables or {}).get("_stat_day") or day_for(now, tz_name)
    if redis is not None:
        try:
            await redis.hincrby(stats_key(fs.workspace_id, day), _field(fs.flow_id, "completed_sessions"), 1)
        except Exception:  # noqa: BLE001
            log.debug("flow completed counter failed", exc_info=True)
    if fs.contact_id is not None:
        await session.execute(
            pg_insert(FlowStatsUser)
            .values(
                workspace_id=fs.workspace_id,
                flow_id=fs.flow_id,
                day=date.fromisoformat(day),
                contact_id=fs.contact_id,
                completed=True,
            )
            .on_conflict_do_update(
                index_elements=["workspace_id", "flow_id", "day", "contact_id"],
                set_={"completed": True},
            )
        )


# ==========================================================================
# 60s flush (registered cron in jobs/worker.py)
# ==========================================================================
def _parse_key(key: str) -> tuple[uuid.UUID, str] | None:
    if not key.startswith(KEY_PREFIX):
        return None
    rest = key[len(KEY_PREFIX) :]
    parts = rest.split(":")
    if len(parts) != 2:
        return None
    try:
        return uuid.UUID(parts[0]), parts[1]
    except ValueError:
        return None


async def flush(
    session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis
) -> int:
    """Move Redis session counters into flow_stats_daily (additive) and
    re-materialise triggered/engaged user counts from flow_stats_users. Returns
    number of (flow, day) rows touched."""
    touched = 0
    async for key in redis.scan_iter(match=f"{KEY_PREFIX}*", count=200):
        parsed = _parse_key(key)
        if parsed is None:
            continue
        workspace_id, day = parsed
        try:
            fields: dict[str, str] = await redis.hgetall(key)
            per_flow: dict[str, dict[str, int]] = {}
            for field_name, raw in fields.items():
                if ":" not in field_name:
                    continue
                flow_s, metric = field_name.rsplit(":", 1)
                val = int(raw)
                if val == 0:
                    continue
                per_flow.setdefault(flow_s, {})[metric] = val
            if not per_flow:
                continue
            day_d = date.fromisoformat(day)
            async with session_factory() as session:
                async with session.begin():
                    for flow_s, metrics in per_flow.items():
                        try:
                            flow_id = uuid.UUID(flow_s)
                        except ValueError:
                            continue
                        tu = (
                            await session.execute(
                                select(func.count())
                                .select_from(FlowStatsUser)
                                .where(
                                    FlowStatsUser.workspace_id == workspace_id,
                                    FlowStatsUser.flow_id == flow_id,
                                    FlowStatsUser.day == day_d,
                                )
                            )
                        ).scalar_one()
                        eu = (
                            await session.execute(
                                select(func.count())
                                .select_from(FlowStatsUser)
                                .where(
                                    FlowStatsUser.workspace_id == workspace_id,
                                    FlowStatsUser.flow_id == flow_id,
                                    FlowStatsUser.day == day_d,
                                    FlowStatsUser.engaged.is_(True),
                                )
                            )
                        ).scalar_one()
                        stmt = pg_insert(FlowStatsDaily).values(
                            workspace_id=workspace_id,
                            flow_id=flow_id,
                            day=day_d,
                            triggered_sessions=metrics.get("triggered_sessions", 0),
                            completed_sessions=metrics.get("completed_sessions", 0),
                            triggered_users=int(tu),
                            engaged_users=int(eu),
                        )
                        stmt = stmt.on_conflict_do_update(
                            index_elements=["workspace_id", "flow_id", "day"],
                            set_={
                                "triggered_sessions": FlowStatsDaily.triggered_sessions
                                + stmt.excluded.triggered_sessions,
                                "completed_sessions": FlowStatsDaily.completed_sessions
                                + stmt.excluded.completed_sessions,
                                "triggered_users": stmt.excluded.triggered_users,
                                "engaged_users": stmt.excluded.engaged_users,
                                "updated_at": datetime.now(UTC),
                            },
                        )
                        await session.execute(stmt)
                        touched += 1
            # subtract only what we consumed (concurrent incrs survive)
            pipe = redis.pipeline(transaction=False)
            for flow_s, metrics in per_flow.items():
                for metric, val in metrics.items():
                    pipe.hincrby(key, _field(flow_s, metric), -val)
            await pipe.execute()
        except Exception:  # noqa: BLE001 — one bad key must not stop the flush
            log.exception("flow stats flush failed for %s", key)
    return touched
