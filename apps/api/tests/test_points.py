"""AI points: Lua script semantics (documented via a Python simulation of the
exact script logic) + key/idempotency helpers."""
from __future__ import annotations

from datetime import UTC, datetime

from apps.api.app.services.points import (
    CHECK_AND_DECR_LUA,
    balance_key,
    current_period,
    grant_ref,
)


def simulate_lua(balance: int | None, cost: int) -> tuple[int, int | None]:
    """Mirror of CHECK_AND_DECR_LUA: returns (result, new_balance)."""
    if balance is None:
        return -2, None
    if balance < cost:
        return -1, balance
    return balance - cost, balance - cost


def test_script_text_contains_expected_ops():
    # GET → nil sentinel (-2), floor check (-1), atomic DECRBY on success
    assert "redis.call('GET', KEYS[1])" in CHECK_AND_DECR_LUA
    assert "return -2" in CHECK_AND_DECR_LUA
    assert "return -1" in CHECK_AND_DECR_LUA
    assert "redis.call('DECRBY', KEYS[1], cost)" in CHECK_AND_DECR_LUA
    # ordering: the -2 (not loaded) check must precede the balance compare
    assert CHECK_AND_DECR_LUA.index("return -2") < CHECK_AND_DECR_LUA.index("return -1")
    # rejects negative costs (never a sneaky refund path through decr)
    assert "cost < 0" in CHECK_AND_DECR_LUA


def test_floor_zero_semantics():
    assert simulate_lua(None, 5) == (-2, None)  # unloaded → load then retry
    assert simulate_lua(4, 5) == (-1, 4)  # hard reject, balance untouched
    assert simulate_lua(5, 5) == (0, 0)  # exact spend to zero allowed
    assert simulate_lua(0, 1) == (-1, 0)  # zero never goes negative
    assert simulate_lua(100, 10) == (90, 90)


def test_balance_key_scoped_per_workspace():
    assert balance_key("ws-a") != balance_key("ws-b")
    assert balance_key("ws-a") == "aipoints:ws-a"


def test_grant_ref_idempotency_key():
    assert grant_ref("2026-07") == "grant:2026-07"
    assert grant_ref("2026-07") == grant_ref("2026-07")
    assert grant_ref("2026-07") != grant_ref("2026-08")


def test_current_period_format():
    assert current_period(datetime(2026, 1, 3, tzinfo=UTC)) == "2026-01"
    assert current_period(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == "2026-12"
