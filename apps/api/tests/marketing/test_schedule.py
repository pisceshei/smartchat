"""Recurrence (rrule subset) + send-window math."""
from __future__ import annotations

from datetime import UTC, datetime

from apps.api.app.marketing import schedule as s


def test_one_time_immediate_when_no_send_at():
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    assert s.is_one_time_due({}, now=now) is True
    assert s.one_time_due_at({}, now=now) == now


def test_one_time_future_not_due():
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    sched = {"send_at": "2026-07-08T12:00:00+00:00"}
    assert s.is_one_time_due(sched, now=now) is False


def test_daily_due_occurrences_catch_up():
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    sched = {"rrule": {"freq": "daily", "interval": 1, "dtstart": "2026-07-05T09:00:00+00:00"}}
    occ = s.due_occurrences(sched, now=now, after=None)
    # 05,06,07,08 at 09:00 are <= now
    assert len(occ) == 4
    assert all(o.hour == 9 for o in occ)


def test_due_occurrences_after_excludes_already_run():
    now = datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    sched = {"rrule": {"freq": "daily", "interval": 1, "dtstart": "2026-07-05T09:00:00+00:00"}}
    after = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)
    occ = s.due_occurrences(sched, now=now, after=after)
    assert len(occ) == 2  # 07 and 08


def test_weekly_byweekday_filter():
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)  # Wed
    # every Monday
    sched = {"rrule": {"freq": "daily", "byweekday": [0], "dtstart": "2026-07-01T08:00:00+00:00"}}
    occ = s.due_occurrences(sched, now=now, after=None)
    assert all(o.weekday() == 0 for o in occ)
    assert len(occ) >= 2  # Jul 6, Jul 13


def test_until_stops_series():
    now = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)
    sched = {"rrule": {"freq": "daily", "dtstart": "2026-07-01T09:00:00+00:00"},
             "until": "2026-07-05T00:00:00+00:00"}
    occ = s.due_occurrences(sched, now=now, after=None)
    assert all(o <= datetime(2026, 7, 5, tzinfo=UTC) for o in occ)


def test_next_occurrence():
    sched = {"rrule": {"freq": "daily", "dtstart": "2026-07-01T09:00:00+00:00"}}
    nxt = s.next_occurrence(sched, after=datetime(2026, 7, 8, 10, 0, tzinfo=UTC))
    assert nxt == datetime(2026, 7, 9, 9, 0, tzinfo=UTC)


def test_within_send_window_hours():
    rules = {"allowed_hours": {"start": 9, "end": 18}, "tz": "UTC"}
    assert s.within_send_window(rules, datetime(2026, 7, 8, 10, 0, tzinfo=UTC)) is True
    assert s.within_send_window(rules, datetime(2026, 7, 8, 20, 0, tzinfo=UTC)) is False


def test_within_send_window_weekday():
    rules = {"allowed_weekdays": [0, 1, 2, 3, 4], "tz": "UTC"}  # weekdays only
    assert s.within_send_window(rules, datetime(2026, 7, 11, 10, 0, tzinfo=UTC)) is False  # Sat
    assert s.within_send_window(rules, datetime(2026, 7, 8, 10, 0, tzinfo=UTC)) is True  # Wed


def test_within_send_window_timezone_localizes():
    rules = {"allowed_hours": {"start": 9, "end": 18}, "tz": "Asia/Hong_Kong"}  # UTC+8
    # 02:00 UTC = 10:00 HKT → inside
    assert s.within_send_window(rules, datetime(2026, 7, 8, 2, 0, tzinfo=UTC)) is True
    # 13:00 UTC = 21:00 HKT → outside
    assert s.within_send_window(rules, datetime(2026, 7, 8, 13, 0, tzinfo=UTC)) is False


def test_no_rules_always_allowed():
    assert s.within_send_window({}, datetime(2026, 7, 8, 3, 0, tzinfo=UTC)) is True
    assert s.within_send_window(None, datetime(2026, 7, 8, 3, 0, tzinfo=UTC)) is True


def test_next_window_start_finds_open_hour():
    rules = {"allowed_hours": {"start": 9, "end": 18}, "tz": "UTC"}
    out = s.next_window_start(rules, datetime(2026, 7, 8, 20, 0, tzinfo=UTC))
    assert out.hour == 9 and out.day == 9


def test_send_interval_clamped():
    assert s.send_interval_seconds({"interval_seconds": 1}, floor=3, ceil=600) == 3
    assert s.send_interval_seconds({"interval_seconds": 5000}, floor=3, ceil=600) == 600
    assert s.send_interval_seconds({}, floor=3, ceil=600) == 3
