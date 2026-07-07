"""Feature gating matrix + the shared ``require_feature`` dependency (plan §2.3).

Every Pro/Max feature is a boolean key in ``plans.limits``; ``quotas.require_feature``
returns a FastAPI dependency that 403s ``upgrade_required`` unless the workspace's
effective limits enable it. The P3 modules gate themselves with it:

    from apps.api.app.services.quotas import require_feature
    @router.post(..., dependencies=[Depends(require_feature("broadcast"))])

This module owns the *matrix* (which feature needs which plan) and asserts the
plans fixture actually carries every gate key — a missing key silently disables a
paywall, so ``audit_plan_gates`` is unit-tested against the seeded plans.

Gate keys (min plan):
- ``broadcast``      Pro   — 群發
- ``split_link``     Pro   — 分流連結
- ``reports``        Pro   — 報表/分析
- ``brand_removal``  Pro   — widget 去品牌
- ``openapi``        Max   — 開放 API
- ``webhook``        Max   — Webhook 推送
"""
from __future__ import annotations

from typing import Any

from ..services.quotas import limit_allows, require_feature

__all__ = ["require_feature", "feature_enabled", "GATE_KEYS", "FEATURE_MIN_PLAN",
           "PLAN_RANK", "audit_plan_gates", "expected_gate"]

# feature → minimum plan code that unlocks it
FEATURE_MIN_PLAN: dict[str, str] = {
    "broadcast": "pro",
    "split_link": "pro",
    "reports": "pro",
    "brand_removal": "pro",
    "openapi": "max",
    "webhook": "max",
}

GATE_KEYS: tuple[str, ...] = tuple(FEATURE_MIN_PLAN)

# ordering for "at least this plan" comparisons; custom is a superset of max.
PLAN_RANK: dict[str, int] = {"free": 0, "pro": 1, "max": 2, "custom": 3}


def feature_enabled(limits: dict[str, Any], feature: str) -> bool:
    """Boolean gate check against a workspace's effective limits."""
    return limit_allows(limits, feature)


def expected_gate(plan_code: str, feature: str) -> bool:
    """Whether ``plan_code`` should have ``feature`` unlocked per the matrix."""
    need = FEATURE_MIN_PLAN.get(feature)
    if need is None:
        return False
    return PLAN_RANK.get(plan_code, -1) >= PLAN_RANK.get(need, 99)


def audit_plan_gates(plans_limits: dict[str, dict[str, Any]]) -> list[str]:
    """Return ``"<plan>:<key>"`` for every gate key missing from a plan's limits,
    or whose value disagrees with the matrix. Empty list = fixture is correct."""
    problems: list[str] = []
    for code, limits in plans_limits.items():
        if code not in PLAN_RANK:
            continue
        for key in GATE_KEYS:
            if key not in limits:
                problems.append(f"{code}:{key}:missing")
                continue
            if bool(limits[key]) != expected_gate(code, key):
                problems.append(f"{code}:{key}:mismatch")
    return problems
