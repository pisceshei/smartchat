"""intent: prompt building, choice parsing, and 24h cache behavior."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from apps.api.app.ai import intent as intent_mod
from apps.api.app.ai import points_enforce
from apps.api.app.ai.points_enforce import EnforceResult

from .fakes import FakeLLM, FakeRedis, FakeResult, FakeSession


def _intent(name, desc="", examples=None):
    return SimpleNamespace(id=uuid.uuid4(), name=name, description=desc,
                           examples=examples or [], enabled=True)


def test_normalize_text():
    assert intent_mod.normalize_text("  Hello   World  ") == "hello world"
    assert intent_mod.normalize_text("") == ""


def test_cache_key_stable_and_scoped():
    ws = uuid.uuid4()
    k1 = intent_mod.cache_key(ws, "Where is my order?")
    k2 = intent_mod.cache_key(ws, "where is   my order?")  # normalized identical
    assert k1 == k2
    assert intent_mod.cache_key(uuid.uuid4(), "Where is my order?") != k1


def test_build_intent_prompt_numbered():
    intents = [_intent("Order status", "track", ["where is my order", "order status"]),
               _intent("Refund", "money back")]
    prompt = intent_mod.build_intent_prompt(intents, "where is my stuff")
    assert "1. Order status" in prompt
    assert "2. Refund" in prompt
    assert "example: where is my order" in prompt
    assert "0 for none" in prompt


def test_parse_choice_clamps():
    assert intent_mod.parse_choice("1", 3) == 1
    assert intent_mod.parse_choice("Intent 2", 3) == 2
    assert intent_mod.parse_choice("0", 3) == 0
    assert intent_mod.parse_choice("9", 3) == 0  # out of range → none
    assert intent_mod.parse_choice("nonsense", 3) == 0
    assert intent_mod.parse_choice("-1", 3) == 0


async def test_classify_caches_and_bills_once(monkeypatch):
    intents = [_intent("Order status"), _intent("Refund")]
    session = FakeSession(execute=lambda stmt: FakeResult(intents))
    redis = FakeRedis()
    llm = FakeLLM(intent_choice="1")
    ws = uuid.uuid4()

    spend_calls = {"n": 0}

    async def fake_spend(sess, r, *, workspace_id, feature_key, amount=1, **k):
        spend_calls["n"] += 1
        return EnforceResult(ok=True, feature_key=feature_key, points_charged=1,
                             balance_after=99, hardstop="")

    monkeypatch.setattr(points_enforce, "spend", fake_spend)

    first = await intent_mod.classify_intent(session, redis, workspace_id=ws,
                                             text="where is my order", client=llm, emit_event=False)
    assert first == intents[0].id
    assert llm.complete_calls == 1
    assert spend_calls["n"] == 1

    # identical text → cache hit: no LLM, no spend
    second = await intent_mod.classify_intent(session, redis, workspace_id=ws,
                                              text="where is  my order", client=llm, emit_event=False)
    assert second == intents[0].id
    assert llm.complete_calls == 1
    assert spend_calls["n"] == 1


async def test_classify_no_intents_returns_none(monkeypatch):
    session = FakeSession(execute=lambda stmt: FakeResult([]))
    redis = FakeRedis()
    llm = FakeLLM(intent_choice="1")
    called = {"n": 0}

    async def fake_spend(*a, **k):
        called["n"] += 1
        return EnforceResult(ok=True, feature_key="intent", points_charged=1, balance_after=1, hardstop="")

    monkeypatch.setattr(points_enforce, "spend", fake_spend)
    out = await intent_mod.classify_intent(session, redis, workspace_id=uuid.uuid4(),
                                           text="hello", client=llm, emit_event=False)
    assert out is None
    assert called["n"] == 0  # no intents → never charge
    assert llm.complete_calls == 0


async def test_classify_points_hardstop_skips(monkeypatch):
    intents = [_intent("Order status")]
    session = FakeSession(execute=lambda stmt: FakeResult(intents))
    redis = FakeRedis()
    llm = FakeLLM(intent_choice="1")

    async def blocked_spend(*a, **k):
        return EnforceResult(ok=False, feature_key="intent", points_charged=0,
                             balance_after=0, hardstop="skip")

    monkeypatch.setattr(points_enforce, "spend", blocked_spend)
    out = await intent_mod.classify_intent(session, redis, workspace_id=uuid.uuid4(),
                                           text="where is my order", client=llm, emit_event=False)
    assert out is None
    assert llm.complete_calls == 0  # hard-stop before the LLM call


async def test_classify_none_choice_cached(monkeypatch):
    intents = [_intent("Order status")]
    session = FakeSession(execute=lambda stmt: FakeResult(intents))
    redis = FakeRedis()
    llm = FakeLLM(intent_choice="0")  # model says none

    async def fake_spend(*a, **k):
        return EnforceResult(ok=True, feature_key="intent", points_charged=1, balance_after=9, hardstop="")

    monkeypatch.setattr(points_enforce, "spend", fake_spend)
    out = await intent_mod.classify_intent(session, redis, workspace_id=uuid.uuid4(),
                                           text="random chatter", client=llm, emit_event=False)
    assert out is None
    # sentinel cached so a repeat doesn't re-hit the LLM
    assert any(v == intent_mod.NONE_SENTINEL for v in redis.store.values())
