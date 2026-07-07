"""UTC-hour → workspace-local bucketing, DST-safe (plan 附錄 B.4).

Ground truth: bucketing must localise via zoneinfo so a spring-forward day is
23h and a fall-back day is 25h, and a UTC hour lands in the correct *local*
calendar day / week / month regardless of offset.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from apps.api.app.analytics import collectors

NY = "America/New_York"


def _utc(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=UTC)


# --------------------------------------------------------------------------
# local-day bounds across DST
# --------------------------------------------------------------------------
def test_spring_forward_day_is_23_hours():
    start, end = collectors.local_day_bounds_utc(date(2026, 3, 8), NY)  # spring forward
    assert (end - start) == timedelta(hours=23)
    # local midnight EST (-05:00) → 05:00 UTC
    assert start == _utc(2026, 3, 8, 5)


def test_fall_back_day_is_25_hours():
    start, end = collectors.local_day_bounds_utc(date(2026, 11, 1), NY)  # fall back
    assert (end - start) == timedelta(hours=25)
    assert start == _utc(2026, 11, 1, 4)  # local midnight EDT (-04:00) → 04:00 UTC


def test_utc_hour_maps_to_correct_local_day_around_dst():
    # 04:00Z on the spring-forward morning is still 2026-03-07 23:00 EST
    assert collectors.local_day_of_hour(_utc(2026, 3, 8, 4), NY) == date(2026, 3, 7)
    # 05:00Z is 2026-03-08 00:00 EST
    assert collectors.local_day_of_hour(_utc(2026, 3, 8, 5), NY) == date(2026, 3, 8)


# --------------------------------------------------------------------------
# bucket_of_hour label / ts
# --------------------------------------------------------------------------
def test_bucket_day_label_is_local_not_utc():
    # 03:00Z Jan 2 in New York (-05:00) is still Jan 1 22:00 local
    b = collectors.bucket_of_hour(_utc(2026, 1, 2, 3), NY, "day")
    assert b.key == "2026-01-01"
    # ts is the local-midnight instant expressed in UTC (05:00Z)
    assert b.ts == _utc(2026, 1, 1, 5).isoformat()


def test_bucket_hour_label_is_local_wall_clock():
    b = collectors.bucket_of_hour(_utc(2026, 1, 2, 3), NY, "hour")
    assert b.key == "2026-01-01 22:00"


def test_bucket_month_and_week():
    bm = collectors.bucket_of_hour(_utc(2026, 6, 15, 12), "UTC", "month")
    assert bm.key == "2026-06"
    bw = collectors.bucket_of_hour(_utc(2026, 6, 17, 0), "UTC", "week")  # a Wednesday
    assert bw.key.startswith("2026-W")


def test_utc_zone_is_identity():
    b = collectors.bucket_of_hour(_utc(2026, 2, 3, 14), "UTC", "day")
    assert b.key == "2026-02-03"


def test_unknown_tz_falls_open_to_utc():
    b = collectors.bucket_of_hour(_utc(2026, 2, 3, 14), "Not/AZone", "day")
    assert b.key == "2026-02-03"


# --------------------------------------------------------------------------
# hour flooring + iteration
# --------------------------------------------------------------------------
def test_floor_hour_and_naive_coercion():
    assert collectors.floor_hour(datetime(2026, 1, 1, 5, 42, 7, tzinfo=UTC)) == _utc(2026, 1, 1, 5)
    # naive input is coerced to UTC
    assert collectors.floor_hour(datetime(2026, 1, 1, 5, 42)) == _utc(2026, 1, 1, 5)


def test_iter_hours_is_half_open():
    start = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    hours = list(collectors.iter_hours(start, _utc(2026, 1, 1, 3)))
    assert hours == [_utc(2026, 1, 1, 0), _utc(2026, 1, 1, 1), _utc(2026, 1, 1, 2)]
