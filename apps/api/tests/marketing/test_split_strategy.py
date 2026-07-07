"""Split-link target selection: random(weighted)/sequential/time_period,
disabled + daily caps + time-window matching + wraparound."""
from __future__ import annotations

from datetime import UTC, datetime

from apps.api.app.marketing import split_strategy as ss

MON_10 = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)  # Monday 10:00 UTC


def _t(**kw):
    base = {"channel_account_id": None, "phone": "1", "weight": 1, "enabled": True}
    base.update(kw)
    return base


def test_sequential_wraparound():
    targets = [_t(phone="a"), _t(phone="b"), _t(phone="c")]
    picks = [ss.choose_target(targets, strategy="sequential", cursor=i, now=MON_10)[0] for i in range(7)]
    assert picks == [0, 1, 2, 0, 1, 2, 0]


def test_sequential_next_cursor_advances():
    targets = [_t(), _t()]
    idx, nxt = ss.choose_target(targets, strategy="sequential", cursor=5, now=MON_10)
    assert idx == 1 and nxt == 6


def test_sequential_skips_disabled():
    targets = [_t(enabled=False), _t(phone="b"), _t(phone="c")]
    picks = [ss.choose_target(targets, strategy="sequential", cursor=i, now=MON_10)[0] for i in range(4)]
    assert set(picks) == {1, 2}
    assert 0 not in picks


def test_weighted_random_respects_weights():
    targets = [_t(weight=1), _t(weight=9)]
    # draw just below the 0.1 boundary → first bucket; above → second
    assert ss.choose_target(targets, strategy="random", now=MON_10, rng=lambda: 0.05)[0] == 0
    assert ss.choose_target(targets, strategy="random", now=MON_10, rng=lambda: 0.5)[0] == 1


def test_daily_cap_excludes_capped_target():
    targets = [_t(daily_cap=10), _t(phone="b")]
    idx, _ = ss.choose_target(
        targets, strategy="sequential", cursor=0, now=MON_10, daily_counts={0: 10},
    )
    assert idx == 1


def test_all_capped_returns_none():
    targets = [_t(daily_cap=1)]
    idx, _ = ss.choose_target(targets, strategy="random", now=MON_10, daily_counts={0: 1})
    assert idx is None


def test_time_period_only_open_targets():
    inside = _t(phone="in", time_windows=[{"days": [0], "start": "09:00", "end": "18:00"}])
    outside = _t(phone="out", time_windows=[{"days": [0], "start": "19:00", "end": "23:00"}])
    targets = [inside, outside]
    idx, _ = ss.choose_target(targets, strategy="time_period", now=MON_10)
    assert idx == 0


def test_time_period_none_when_all_closed():
    closed = _t(time_windows=[{"days": [0], "start": "19:00", "end": "23:00"}])
    idx, _ = ss.choose_target([closed], strategy="time_period", now=MON_10)
    assert idx is None


def test_target_open_now_wraps_midnight():
    t = _t(time_windows=[{"days": [0], "start": "22:00", "end": "02:00"}])
    assert ss.target_open_now(t, datetime(2026, 7, 6, 23, 30, tzinfo=UTC)) is True
    assert ss.target_open_now(t, datetime(2026, 7, 6, 12, 0, tzinfo=UTC)) is False


def test_no_windows_always_open():
    assert ss.target_open_now(_t(), MON_10) is True


def test_empty_targets():
    assert ss.choose_target([], strategy="random", now=MON_10) == (None, 0)
