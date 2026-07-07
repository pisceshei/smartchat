"""Broadcast scheduling math — recurrence (rrule subset) + send windows.

All functions are PURE and unit-tested. Timestamps are timezone-aware UTC; the
send-window check localises to the workspace/broadcast timezone at evaluation
time (plan: aggregate in UTC, localise at query time).

``schedule`` jsonb shapes (plan B.3):
  one_time : {}  → send immediately   |  {"send_at": "<iso>"}  → scheduled
  recurring: {"rrule": {"freq": "hourly|daily|weekly|monthly", "interval": 1,
             "byhour": [9], "byweekday": [0,2,4], "dtstart": "<iso>"},
             "until": "<iso>", "count": 10}

``send_rules`` jsonb shape:
  {"allowed_hours": {"start": 9, "end": 21}, "allowed_weekdays": [0..6],
   "tz": "Asia/Hong_Kong", "spillover": true, "interval_seconds": 3}
An absent / empty rule means "always allowed". ``allowed_hours`` is a local
[start, end) hour range; end<=start means it wraps past midnight.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FREQ_STEP = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
}
MAX_CATCHUP = 200  # bound the catch-up loop so a long-idle recurring never explodes


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def get_tz(name: str | None) -> ZoneInfo:
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")


# --------------------------------------------------------------------------
# one_time
# --------------------------------------------------------------------------
def one_time_due_at(schedule: dict[str, Any], *, now: datetime) -> datetime:
    """When a one_time broadcast should fire. Absent send_at ⇒ immediately."""
    send_at = parse_iso(schedule.get("send_at"))
    return send_at or now


def is_one_time_due(schedule: dict[str, Any], *, now: datetime) -> bool:
    return one_time_due_at(schedule, now=now) <= _aware(now)


# --------------------------------------------------------------------------
# recurring (rrule subset)
# --------------------------------------------------------------------------
def _matches_filters(dt_local: datetime, rrule: dict[str, Any]) -> bool:
    byhour = rrule.get("byhour")
    if byhour and dt_local.hour not in [int(h) for h in byhour]:
        return False
    byweekday = rrule.get("byweekday")
    if byweekday and dt_local.weekday() not in [int(d) for d in byweekday]:
        return False
    return True


def due_occurrences(
    schedule: dict[str, Any],
    *,
    now: datetime,
    after: datetime | None = None,
    tz: str | None = None,
    limit: int = MAX_CATCHUP,
) -> list[datetime]:
    """Recurring occurrences that are due to fire: strictly after ``after``
    (the last one already run) and <= ``now``. Returns UTC datetimes, ascending.

    Supports freq hourly/daily/weekly (stepped by interval) with optional
    byhour/byweekday filters, plus a coarse monthly (calendar-month step). The
    stream stops at ``until`` / ``count`` and is bounded by ``limit``.
    """
    now = _aware(now)
    rrule = schedule.get("rrule") or {}
    freq = str(rrule.get("freq") or "daily").lower()
    interval = max(1, int(rrule.get("interval") or 1))
    dtstart = parse_iso(rrule.get("dtstart")) or now
    until = parse_iso(schedule.get("until") or rrule.get("until"))
    count = rrule.get("count")
    count = int(count) if count is not None else None
    tzinfo = get_tz(tz or schedule.get("tz"))

    out: list[datetime] = []
    emitted = 0
    if freq == "monthly":
        cur = dtstart
        guard = 0
        while cur <= now and guard < limit * 2:
            guard += 1
            if until and cur > until:
                break
            if count is not None and emitted >= count:
                break
            emitted += 1
            if (after is None or cur > after) and cur <= now:
                out.append(cur)
            cur = _add_months(cur, interval)
        return out[-limit:]

    step = _FREQ_STEP.get(freq, _FREQ_STEP["daily"])
    cur = dtstart
    guard = 0
    while cur <= now and guard < limit * 4:
        guard += 1
        if until and cur > until:
            break
        if count is not None and emitted >= count:
            break
        local = cur.astimezone(tzinfo)
        if _matches_filters(local, rrule):
            emitted += 1
            if after is None or cur > after:
                out.append(cur)
        cur = cur + step * interval
    return out[-limit:]


def next_occurrence(
    schedule: dict[str, Any], *, after: datetime, tz: str | None = None, horizon_days: int = 400
) -> datetime | None:
    """The first recurring occurrence strictly after ``after`` (for arming a
    timer). None when the series has ended."""
    after = _aware(after)
    rrule = schedule.get("rrule") or {}
    freq = str(rrule.get("freq") or "daily").lower()
    interval = max(1, int(rrule.get("interval") or 1))
    dtstart = parse_iso(rrule.get("dtstart")) or after
    until = parse_iso(schedule.get("until") or rrule.get("until"))
    tzinfo = get_tz(tz or schedule.get("tz"))
    horizon = after + timedelta(days=horizon_days)

    if freq == "monthly":
        cur = dtstart
        guard = 0
        while cur <= horizon and guard < 10_000:
            guard += 1
            if until and cur > until:
                return None
            if cur > after:
                return cur
            cur = _add_months(cur, interval)
        return None

    step = _FREQ_STEP.get(freq, _FREQ_STEP["daily"])
    cur = dtstart if dtstart > after else after
    # rewind cur onto the dtstart grid is not required for correctness of ">after";
    # step forward from dtstart until we pass `after`, then keep filter-matching.
    cur = dtstart
    guard = 0
    while cur <= horizon and guard < 100_000:
        guard += 1
        if until and cur > until:
            return None
        if cur > after and _matches_filters(cur.astimezone(tzinfo), rrule):
            return cur
        cur = cur + step * interval
    return None


def _add_months(dt: datetime, months: int) -> datetime:
    m = dt.month - 1 + months
    year = dt.year + m // 12
    month = m % 12 + 1
    # clamp day to the month length
    day = min(dt.day, _days_in_month(year, month))
    return dt.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        nxt = datetime(year, month + 1, 1, tzinfo=UTC)
    return (nxt - timedelta(days=1)).day


# --------------------------------------------------------------------------
# send window
# --------------------------------------------------------------------------
def _hour_in_range(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True  # full-day
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def within_send_window(send_rules: dict[str, Any] | None, now: datetime) -> bool:
    """True when ``now`` (UTC) falls inside the allowed send window after
    localising to the rule timezone. No rules ⇒ always True."""
    rules = send_rules or {}
    tzinfo = get_tz(rules.get("tz"))
    local = _aware(now).astimezone(tzinfo)
    weekdays = rules.get("allowed_weekdays")
    if weekdays and local.weekday() not in [int(d) for d in weekdays]:
        return False
    hours = rules.get("allowed_hours")
    if hours:
        start = int(hours.get("start", 0))
        end = int(hours.get("end", 24)) % 24 if int(hours.get("end", 24)) != 24 else 24
        if end == 24:
            end = 0  # 24 → wrap sentinel handled as full-day when start==0
            if start == 0:
                return True
        if not _hour_in_range(local.hour, start, end):
            return False
    return True


def next_window_start(
    send_rules: dict[str, Any] | None, now: datetime, *, max_hours: int = 24 * 8
) -> datetime:
    """The next UTC instant that enters the send window (top of the next
    allowed local hour). Steps hour-by-hour; bounded by ``max_hours``. If no
    window opens within the bound, returns ``now + 1h`` as a safe fallback."""
    now = _aware(now)
    probe = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(max_hours):
        if within_send_window(send_rules, probe):
            return probe
        probe += timedelta(hours=1)
    return now + timedelta(hours=1)


def send_interval_seconds(send_rules: dict[str, Any] | None, *, floor: int, ceil: int) -> int:
    """Per-message pacing interval clamped to the configured floor/ceiling."""
    rules = send_rules or {}
    try:
        val = int(rules.get("interval_seconds", floor))
    except (TypeError, ValueError):
        val = floor
    return max(floor, min(ceil, val))
