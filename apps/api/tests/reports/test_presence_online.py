"""Online-time interval → per-UTC-hour split (plan 附錄 B.4). Pure."""
from __future__ import annotations

from datetime import UTC, datetime

from apps.api.app.analytics.rollup import split_presence_seconds


def _t(h, m=0, s=0):
    return datetime(2026, 1, 1, h, m, s, tzinfo=UTC)


WIN_START = _t(0)
WIN_END = _t(23, 59, 59)
NOW = _t(23)


def test_interval_splits_across_hours():
    out = split_presence_seconds(_t(10, 30), _t(12, 15), now=NOW, window_start=WIN_START, window_end=WIN_END)
    assert out[_t(10)] == 1800  # 30 min
    assert out[_t(11)] == 3600
    assert out[_t(12)] == 900
    assert sum(out.values()) == 6300


def test_open_interval_runs_to_now():
    out = split_presence_seconds(_t(9), None, now=_t(9, 40), window_start=WIN_START, window_end=WIN_END)
    assert out == {_t(9): 2400}


def test_window_clamps_interval():
    # session 08:00→12:00, but only count the [10:00, 11:30) window
    out = split_presence_seconds(
        _t(8), _t(12), now=NOW, window_start=_t(10), window_end=_t(11, 30)
    )
    assert out == {_t(10): 3600, _t(11): 1800}


def test_interval_entirely_outside_window_is_empty():
    out = split_presence_seconds(_t(1), _t(2), now=NOW, window_start=_t(10), window_end=_t(11))
    assert out == {}


def test_single_hour_partial():
    out = split_presence_seconds(_t(14, 10), _t(14, 40), now=NOW, window_start=WIN_START, window_end=WIN_END)
    assert out == {_t(14): 1800}
