"""points_enforce: unit price/units math + hard-stop behaviors (no DB)."""
from __future__ import annotations

import uuid

from apps.api.app.ai import points_enforce as pe
from apps.api.app.services.points import SpendResult


def test_units_for_metered_and_flat():
    assert pe.units_for("translate_llm_per500", 0) == 1
    assert pe.units_for("translate_llm_per500", 1) == 1
    assert pe.units_for("translate_llm_per500", 500) == 1
    assert pe.units_for("translate_llm_per500", 501) == 2
    assert pe.units_for("translate_llm_per500", 1000) == 2
    assert pe.units_for("embed_per10k", 10_000) == 1
    assert pe.units_for("embed_per10k", 10_001) == 2
    # flat feature ignores amount
    assert pe.units_for("ai_reply", 999) == 1
    assert pe.units_for("composer", 0) == 1


def test_hardstop_mapping():
    assert pe.hardstop_for("ai_reply") == pe.HANDOFF
    assert pe.hardstop_for("intent") == pe.SKIP
    assert pe.hardstop_for("translate_llm_per500") == pe.FALLBACK
    assert pe.hardstop_for("composer") == pe.ERROR
    assert pe.hardstop_for("summary") == pe.SKIP
    assert pe.hardstop_for("unknown_feature") == pe.ERROR


def test_default_prices_cover_seed():
    for key in ("ai_reply", "intent", "translate_llm_per500", "composer",
                "embed_per10k", "summary", "report_summary"):
        assert key in pe.DEFAULT_PRICES


class _NoneExecSession:
    async def execute(self, *a, **k):
        class _R:
            def scalar_one_or_none(self_inner):
                return None
        return _R()


async def test_price_for_default_when_missing():
    pe.clear_price_cache()
    price = await pe.price_for(_NoneExecSession(), "ai_reply")
    assert price == pe.DEFAULT_PRICES["ai_reply"] == 10
    pe.clear_price_cache()


class _ValueExecSession:
    def __init__(self, value):
        self.value = value

    async def execute(self, *a, **k):
        v = self.value

        class _R:
            def scalar_one_or_none(self_inner):
                return v
        return _R()


async def test_price_for_uses_config_row():
    pe.clear_price_cache()
    price = await pe.price_for(_ValueExecSession(42), "ai_reply")
    assert price == 42
    pe.clear_price_cache()


async def test_spend_blocked_returns_feature_hardstop(monkeypatch):
    async def fake_price(session, feature_key):
        return 10

    async def fake_cad(session, redis, *, workspace_id, cost, reason, ref_type=None, ref_id=None):
        assert cost == 10
        return SpendResult(ok=False, balance_after=3, reason="insufficient")

    monkeypatch.setattr(pe, "price_for", fake_price)
    monkeypatch.setattr(pe.points, "check_and_decr", fake_cad)
    res = await pe.spend(None, None, workspace_id=uuid.uuid4(), feature_key="ai_reply")
    assert res.blocked is True
    assert res.ok is False
    assert res.hardstop == pe.HANDOFF
    assert res.points_charged == 0
    assert res.balance_after == 3


async def test_spend_ok_charges_price_times_units(monkeypatch):
    async def fake_price(session, feature_key):
        return 1  # translate_llm_per500

    captured = {}

    async def fake_cad(session, redis, *, workspace_id, cost, reason, ref_type=None, ref_id=None):
        captured["cost"] = cost
        return SpendResult(ok=True, balance_after=100 - cost, reason="ok")

    monkeypatch.setattr(pe, "price_for", fake_price)
    monkeypatch.setattr(pe.points, "check_and_decr", fake_cad)
    res = await pe.spend(None, None, workspace_id=uuid.uuid4(),
                         feature_key="translate_llm_per500", amount=1200)
    assert captured["cost"] == 3  # ceil(1200/500) * 1
    assert res.ok is True
    assert res.hardstop == ""
    assert res.points_charged == 3
