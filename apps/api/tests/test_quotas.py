"""Quota logic: plan ⊕ overrides merge, limit gating semantics."""
from __future__ import annotations

import json
from pathlib import Path

from apps.api.app.services.quotas import limit_allows, merge_limits, usage_key

FIXTURE = Path(__file__).resolve().parents[1] / "app" / "fixtures" / "plans.json"


def test_merge_overrides_win():
    plan = {"seats": 3, "openapi": False, "ai_points_monthly": 10000}
    over = {"seats": 50, "openapi": True}
    merged = merge_limits(plan, over)
    assert merged == {"seats": 50, "openapi": True, "ai_points_monthly": 10000}


def test_merge_none_override_ignored():
    assert merge_limits({"seats": 3}, {"seats": None}) == {"seats": 3}
    assert merge_limits({"seats": 3}, None) == {"seats": 3}


def test_merge_does_not_mutate_plan():
    plan = {"seats": 3}
    merge_limits(plan, {"seats": 9})
    assert plan == {"seats": 3}


def test_limit_allows_booleans():
    assert limit_allows({"broadcast": True}, "broadcast")
    assert not limit_allows({"broadcast": False}, "broadcast")
    assert not limit_allows({}, "broadcast")


def test_limit_allows_numbers():
    limits = {"seats": 3, "mac_monthly": -1, "widgets": 0}
    assert limit_allows(limits, "seats", current=2)
    assert not limit_allows(limits, "seats", current=3)  # at cap
    assert limit_allows(limits, "mac_monthly", current=10**9)  # -1 unlimited
    assert not limit_allows(limits, "widgets")  # 0 = none


def test_plans_fixture_gating_matrix():
    """Plan §2.3: broadcast+brand removal ≥Pro; OpenAPI+Webhook ≥Max."""
    plans = {p["code"]: p["limits"] for p in json.loads(FIXTURE.read_text())["plans"]}
    assert not plans["free"]["broadcast"] and not plans["free"]["brand_removal"]
    assert plans["pro"]["broadcast"] and plans["pro"]["brand_removal"]
    assert not plans["pro"]["openapi"] and not plans["pro"]["webhook"]
    assert plans["max"]["openapi"] and plans["max"]["webhook"]
    assert plans["free"]["ai_points_monthly"] == 10_000
    assert plans["pro"]["ai_points_monthly"] == 100_000
    assert plans["max"]["ai_points_monthly"] == 1_000_000
    assert plans["free"]["official_channels"] == 1
    assert plans["free"]["hosted_devices"] == 1
    assert plans["free"]["mac_monthly"] == 100


def test_usage_key_shape():
    assert usage_key("ws1", "2026-07") == "usage:ws1:2026-07"
    # rsplit(':', 2) in the flusher must recover ws + period
    _, ws, period = usage_key("ws1", "2026-07").rsplit(":", 2)
    assert (ws, period) == ("ws1", "2026-07")
