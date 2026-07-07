"""Event → aggregate classification + timezone bucketing (plan 附錄 B.4).

Pure functions only — no DB, no Redis, no clock. The rollup (``rollup.py``)
decodes each ``events`` row into a ``py_contracts.events.Event`` and asks these
helpers *which* aggregate slots it touches; the query layer (``queries.py``)
uses the bucketing helpers to fold UTC-hour aggregate rows into workspace-local
day/week/month/hour buckets **at read time** (so a DST change or timezone edit
never forces a recompute).

Golden rules mirrored here:
- Aggregate in UTC hours; localise to the workspace tz only at query time.
- Message volume counts *channel* messages: internal notes and grey
  system-event chips are excluded (they never leave SmartChat).
- Distinct-count customer metrics are NOT derived here (they live in the
  nightly ``agg_customers_daily`` job) — hourly buckets can't compose distincts.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from py_contracts.events import Event

from ..models.reports import NIL_UUID

NIL_UUID_OBJ = UUID(NIL_UUID)

# event types that open a new service cycle (both count toward "new conversations")
_OPENED_TYPES = frozenset({"conversation.created", "conversation.reopened"})


# ==========================================================================
# time helpers (DST-safe: all localisation goes through zoneinfo)
# ==========================================================================
def ensure_utc(dt: datetime) -> datetime:
    """Coerce naive → UTC, aware → UTC. The events table is timestamptz so
    rows come back aware; callers passing wall-clock strings get UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def floor_hour(dt: datetime) -> datetime:
    """Truncate to the start of the UTC hour (the aggregate bucket key)."""
    dt = ensure_utc(dt)
    return dt.replace(minute=0, second=0, microsecond=0)


def zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001 — unknown tz string → fail open to UTC
        return ZoneInfo("UTC")


def iter_hours(start: datetime, end: datetime) -> Iterator[datetime]:
    """Yield each UTC hour boundary in [floor_hour(start), end)."""
    cur = floor_hour(start)
    end = ensure_utc(end)
    while cur < end:
        yield cur
        cur += timedelta(hours=1)


def local_day_of_hour(hour_utc: datetime, tz_name: str | None) -> date:
    """The workspace-local calendar day a given UTC hour falls in."""
    return ensure_utc(hour_utc).astimezone(zone(tz_name)).date()


def local_day_bounds_utc(day_local: date, tz_name: str | None) -> tuple[datetime, datetime]:
    """[start, end) UTC instants that bound one workspace-local calendar day.
    DST-safe: midnight is materialised *in the local zone* then converted."""
    tz = zone(tz_name)
    start_local = datetime(day_local.year, day_local.month, day_local.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


@dataclass(frozen=True)
class Bucket:
    key: str  # stable group key (also the human label the UI shows)
    ts: str  # representative instant (ISO-8601 UTC) for time-series x-axis
    sort: str  # lexicographically sortable ordering key


def bucket_of_hour(hour_utc: datetime, tz_name: str | None, interval: str) -> Bucket:
    """Map a UTC hour to its workspace-local {hour|day|week|month} bucket.

    The label is what the merchant reads; ``ts`` is the bucket's start instant
    (UTC ISO) so the frontend can format it in the viewer's locale.
    """
    tz = zone(tz_name)
    local = ensure_utc(hour_utc).astimezone(tz)
    if interval == "hour":
        start_local = local.replace(minute=0, second=0, microsecond=0)
        key = start_local.strftime("%Y-%m-%d %H:00")
    elif interval == "week":
        iso = local.isocalendar()
        # Monday of the ISO week, local midnight
        monday = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        key = f"{iso.year:04d}-W{iso.week:02d}"
        start_local = monday
    elif interval == "month":
        start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        key = start_local.strftime("%Y-%m")
    else:  # day (default)
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        key = start_local.strftime("%Y-%m-%d")
    start_utc = start_local.astimezone(UTC)
    return Bucket(key=key, ts=start_utc.isoformat(), sort=start_utc.isoformat())


# ==========================================================================
# event classification (pure)
# ==========================================================================
@dataclass(frozen=True)
class MsgVolume:
    channel_type: str
    agent_id: UUID  # NIL_UUID_OBJ for inbound / non-agent (bot/flow) sends
    direction: str  # in | out
    ai_flag: bool


def _is_channel_message(ev: Event) -> bool:
    """True for a real channel message (excludes internal notes + grey chips)."""
    if ev.type != "message.created":
        return False
    p = ev.payload or {}
    if p.get("is_note"):
        return False
    if p.get("msg_type") == "system_event":
        return False
    return p.get("direction") in ("in", "out")


def message_volume(ev: Event) -> MsgVolume | None:
    """agg_messages_hourly slot for a channel message, else None."""
    if not _is_channel_message(ev):
        return None
    p = ev.payload or {}
    direction = p["direction"]
    channel = ev.channel_type or ""
    actor_type = ev.actor.type
    is_agent_out = direction == "out" and actor_type in ("member", "ai_agent")
    agent_id = ev.actor.id if (is_agent_out and ev.actor.id) else NIL_UUID_OBJ
    ai_flag = actor_type == "ai_agent"
    return MsgVolume(channel_type=channel, agent_id=agent_id, direction=direction, ai_flag=ai_flag)


def agent_message_id(ev: Event) -> UUID | None:
    """The member/ai_agent an *outbound* channel message counts toward (agg_agent
    .msgs), else None (inbound / bot / flow / system)."""
    if not _is_channel_message(ev):
        return None
    p = ev.payload or {}
    if p.get("direction") != "out":
        return None
    if ev.actor.type in ("member", "ai_agent") and ev.actor.id:
        return ev.actor.id
    return None


def opened_channel(ev: Event) -> str | None:
    """Channel of a newly-opened service cycle (created OR reopened), else None."""
    if ev.type in _OPENED_TYPES:
        return ev.channel_type or ""
    return None


def reopened(ev: Event) -> bool:
    return ev.type == "conversation.reopened"


def assigned_agent_id(ev: Event) -> UUID | None:
    """Member/ai_agent that just took a conversation (agg_agent.convs), else None."""
    if ev.type != "conversation.assigned":
        return None
    p = ev.payload or {}
    if p.get("handler") not in ("member", "ai_agent"):
        return None
    raw = p.get("assignee_member_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Resolved:
    channel_type: str
    session_id: UUID | None
    closed_at: datetime | None


def resolved(ev: Event) -> Resolved | None:
    if ev.type != "conversation.resolved":
        return None
    p = ev.payload or {}
    sid = _opt_uuid(p.get("session_id"))
    closed = _opt_dt(p.get("closed_at"))
    return Resolved(channel_type=ev.channel_type or "", session_id=sid, closed_at=closed)


@dataclass(frozen=True)
class FirstResponse:
    channel_type: str
    agent_id: UUID | None
    session_id: UUID | None
    first_response_at: datetime | None


def first_responded(ev: Event) -> FirstResponse | None:
    if ev.type != "conversation.first_responded":
        return None
    p = ev.payload or {}
    return FirstResponse(
        channel_type=ev.channel_type or "",
        agent_id=ev.actor.id if ev.actor.type in ("member", "ai_agent") else None,
        session_id=_opt_uuid(p.get("session_id")),
        first_response_at=_opt_dt(p.get("first_response_at")),
    )


@dataclass(frozen=True)
class Csat:
    agent_id: UUID | None
    score: int  # 1..5 stars


def csat(ev: Event) -> Csat | None:
    """csat.submitted → (agent, 1..5 score). The CSAT-collection feature stamps
    ``agent_id``/``score`` in the payload; unattributed CSAT is dropped (an
    agent row can't have a NULL agent_id PK)."""
    if ev.type != "csat.submitted":
        return None
    p = ev.payload or {}
    try:
        score = int(p.get("score"))
    except (TypeError, ValueError):
        return None
    if not 1 <= score <= 5:
        return None
    return Csat(agent_id=_opt_uuid(p.get("agent_id")), score=score)


# ==========================================================================
# small parse helpers
# ==========================================================================
def _opt_uuid(raw: object) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _opt_dt(raw: object) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return ensure_utc(raw)
    try:
        return ensure_utc(datetime.fromisoformat(str(raw)))
    except (ValueError, TypeError):
        return None
