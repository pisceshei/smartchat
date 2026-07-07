"""'today' live-merge + share/export config freeze (plan 附錄 B.4). Pure."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from apps.api.app.modules.reports import queries


# --------------------------------------------------------------------------
# today live-merge (agg-so-far + live current bucket)
# --------------------------------------------------------------------------
def test_merge_today_overrides_current_bucket_only():
    series = {
        "2026-07-07": ("2026-07-07T00:00:00+00:00", 3),
        "2026-07-08": ("2026-07-08T00:00:00+00:00", 5),  # stale agg value
    }
    merged = queries.merge_today_series(series, "2026-07-08", "2026-07-08T00:00:00+00:00", 9)
    assert merged["2026-07-08"] == ("2026-07-08T00:00:00+00:00", 9)  # live wins
    assert merged["2026-07-07"] == ("2026-07-07T00:00:00+00:00", 3)  # past untouched


def test_merge_today_inserts_missing_current_bucket():
    merged = queries.merge_today_series({}, "2026-07-08", "2026-07-08T00:00:00+00:00", 2)
    assert merged == {"2026-07-08": ("2026-07-08T00:00:00+00:00", 2)}


# --------------------------------------------------------------------------
# filter parse + config freeze roundtrip
# --------------------------------------------------------------------------
def test_config_freeze_roundtrip_preserves_filters():
    acct = uuid4()
    member = uuid4()
    f = queries.parse_filters(
        from_="2026-07-01T00:00:00+00:00",
        to="2026-07-08T00:00:00+00:00",
        interval="week",
        channel_type="whatsapp",
        channel_account_id=str(acct),
        member_id=str(member),
    )
    cfg = queries.config_dict(f, dimension="member")
    f2 = queries.filters_from_config(cfg)
    assert f2.from_ == f.from_
    assert f2.to == f.to
    assert f2.interval == "week"
    assert f2.channel_type == "whatsapp"
    assert f2.channel_account_id == acct
    assert f2.member_id == member
    assert cfg["dimension"] == "member"


def test_parse_filters_defaults_and_swaps():
    now = datetime(2026, 7, 8, 12, tzinfo=UTC)
    f = queries.parse_filters(
        from_=None, to=None, interval="bogus", channel_type=None,
        channel_account_id=None, member_id=None, now=now,
    )
    assert f.interval == "day"  # bad interval → default
    assert f.to == now
    assert (now - f.from_).days == queries.DEFAULT_WINDOW_DAYS
    # reversed range is normalised
    rev = queries.parse_filters(
        from_="2026-07-08T00:00:00+00:00", to="2026-07-01T00:00:00+00:00",
        interval="day", channel_type=None, channel_account_id=None, member_id=None,
    )
    assert rev.from_ < rev.to


def test_window_hour_bounds_are_half_open_by_hour():
    f = queries.parse_filters(
        from_="2026-07-08T10:30:00+00:00", to="2026-07-08T12:15:00+00:00",
        interval="hour", channel_type=None, channel_account_id=None, member_id=None,
    )
    assert f.start_hour == datetime(2026, 7, 8, 10, tzinfo=UTC)
    assert f.end_hour == datetime(2026, 7, 8, 13, tzinfo=UTC)  # covers the 12:xx hour
