"""Timer service pure math: hot-window membership, ZSET scores."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.api.app.services.timers import (
    HOT_WINDOW_S,
    next_refill_horizon,
    within_hot_window,
    zscore,
)

NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def test_zscore_is_epoch_seconds():
    assert zscore(datetime(1970, 1, 1, tzinfo=UTC)) == 0.0
    assert zscore(NOW) == NOW.timestamp()


def test_zscore_naive_treated_as_utc():
    naive = datetime(2026, 7, 7, 12, 0, 0)
    assert zscore(naive) == zscore(NOW)


def test_past_due_is_in_window():
    assert within_hot_window(NOW - timedelta(days=3), NOW)


def test_boundary_inclusive():
    assert within_hot_window(NOW + timedelta(seconds=HOT_WINDOW_S), NOW)
    assert not within_hot_window(NOW + timedelta(seconds=HOT_WINDOW_S + 1), NOW)


def test_far_future_out_of_window():
    assert not within_hot_window(NOW + timedelta(days=30), NOW)


def test_custom_window():
    assert within_hot_window(NOW + timedelta(seconds=59), NOW, window_s=60)
    assert not within_hot_window(NOW + timedelta(seconds=61), NOW, window_s=60)


def test_refill_horizon():
    assert next_refill_horizon(NOW) == NOW + timedelta(seconds=HOT_WINDOW_S)
