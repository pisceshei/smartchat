"""Feature-gate matrix: the seeded plans must carry every gate key with the
value the paywall matrix demands (a missing key silently disables a gate)."""
from __future__ import annotations

import json
from pathlib import Path

from apps.api.app.billing.gating import (
    FEATURE_MIN_PLAN,
    GATE_KEYS,
    audit_plan_gates,
    expected_gate,
    feature_enabled,
)

FIXTURE = Path(__file__).resolve().parents[2] / "app" / "fixtures" / "plans.json"


def _plans() -> dict[str, dict]:
    return {p["code"]: p["limits"] for p in json.loads(FIXTURE.read_text())["plans"]}


def test_all_gate_keys_present_and_correct_in_fixture():
    """The whole point: no missing / mismatched gate key in the seeded plans."""
    assert audit_plan_gates(_plans()) == []


def test_gate_keys_cover_the_p3_features():
    # broadcast + split_link + reports + brand_removal (Pro); openapi + webhook (Max)
    assert set(GATE_KEYS) == {
        "broadcast", "split_link", "reports", "brand_removal", "openapi", "webhook"
    }


def test_free_is_blocked_from_pro_features():
    plans = _plans()
    for key in ("broadcast", "split_link", "reports", "brand_removal", "openapi", "webhook"):
        assert not feature_enabled(plans["free"], key), key


def test_pro_unlocks_pro_features_but_not_max():
    plans = _plans()
    for key in ("broadcast", "split_link", "reports", "brand_removal"):
        assert feature_enabled(plans["pro"], key), key
    assert not feature_enabled(plans["pro"], "openapi")
    assert not feature_enabled(plans["pro"], "webhook")


def test_max_unlocks_api_and_webhook():
    plans = _plans()
    assert feature_enabled(plans["max"], "openapi")
    assert feature_enabled(plans["max"], "webhook")


def test_expected_gate_matrix_monotonic_by_plan_rank():
    # every feature enabled on a plan stays enabled on a higher plan
    order = ["free", "pro", "max", "custom"]
    for feature in FEATURE_MIN_PLAN:
        seen_true = False
        for code in order:
            v = expected_gate(code, feature)
            if seen_true:
                assert v, f"{feature} regressed at {code}"
            seen_true = seen_true or v


def test_custom_is_a_superset_of_max():
    plans = _plans()
    for key in GATE_KEYS:
        assert feature_enabled(plans["custom"], key), key


def test_unknown_feature_never_gated_open():
    assert not expected_gate("max", "does_not_exist")
