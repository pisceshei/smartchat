"""translation: hashing, numbered-parse, chain building, cache hit + fallback."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from apps.api.app.ai import translation as tr

from .fakes import FakeResult, FakeSession


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def test_content_hash_stable_and_discriminating():
    h = tr.content_hash("hi", "en", "fr", "llm")
    assert h == tr.content_hash("hi", "en", "fr", "llm")
    assert h != tr.content_hash("hi", "en", "de", "llm")  # dst differs
    assert h != tr.content_hash("hi", "en", "fr", "google")  # engine differs
    assert h != tr.content_hash("hello", "en", "fr", "llm")  # text differs


def test_parse_numbered_roundtrip_and_fallback():
    assert tr._parse_numbered("1. hola\n2. mundo", 2, ["hi", "world"]) == ["hola", "mundo"]
    # missing slot 2 → fall back to source for that slot
    assert tr._parse_numbered("1. hola", 2, ["hi", "world"]) == ["hola", "world"]
    # single line, unparseable → whole output is the translation
    assert tr._parse_numbered("hola", 1, ["hi"]) == ["hola"]


def test_build_chain_drops_unavailable_keeps_llm():
    chain = tr.build_chain(["google", "deepl"])  # no API keys in test env
    assert [e.name for e in chain] == ["llm"]  # both dropped, llm safety net
    chain2 = tr.build_chain(["llm"])
    assert [e.name for e in chain2] == ["llm"]
    chain3 = tr.build_chain(None)
    assert chain3 and chain3[-1].name == "llm"


# --------------------------------------------------------------------------
# cache + fallback (fakes, no DB / network)
# --------------------------------------------------------------------------
class _GoodEngine:
    name = "good"
    available = True
    bills_points = False

    def __init__(self, out: str):
        self.out = out
        self.calls = 0

    async def translate(self, texts, *, src_lang, dst_lang):
        self.calls += 1
        return [self.out for _ in texts]


class _FailEngine:
    name = "fail"
    available = True
    bills_points = False

    def __init__(self):
        self.calls = 0

    async def translate(self, texts, *, src_lang, dst_lang):
        self.calls += 1
        raise RuntimeError("engine down")


async def test_translate_cache_hit_skips_engine():
    cache_row = SimpleNamespace(translated_text="CACHED", hit_count=0, last_used_at=None)
    session = FakeSession(get=lambda model, key: cache_row)
    engine = _GoodEngine("FRESH")
    result = await tr.translate_text(
        session, None, workspace_id=uuid.uuid4(), text="Bonjour", dst_lang="en",
        src_lang="fr", chain=[engine],
    )
    assert result.cached is True
    assert result.text == "CACHED"
    assert engine.calls == 0
    assert cache_row.hit_count == 1


async def test_translate_falls_through_failing_engine():
    session = FakeSession(get=lambda model, key: None, execute=lambda stmt: FakeResult([]))
    fail, good = _FailEngine(), _GoodEngine("HELLO")
    result = await tr.translate_text(
        session, None, workspace_id=uuid.uuid4(), text="Bonjour", dst_lang="en",
        src_lang="fr", chain=[fail, good],
    )
    assert fail.calls == 1
    assert good.calls == 1
    assert result.ok is True
    assert result.engine == "good"
    assert result.text == "HELLO"
    assert result.cached is False


async def test_translate_same_language_passthrough():
    session = FakeSession(get=lambda model, key: None)
    engine = _GoodEngine("X")
    result = await tr.translate_text(
        session, None, workspace_id=uuid.uuid4(), text="hello", dst_lang="en",
        src_lang="en", chain=[engine],
    )
    assert result.text == "hello"
    assert engine.calls == 0  # src == dst → no engine call


async def test_translate_all_engines_fail_returns_original():
    session = FakeSession(get=lambda model, key: None)
    result = await tr.translate_text(
        session, None, workspace_id=uuid.uuid4(), text="Bonjour", dst_lang="en",
        src_lang="fr", chain=[_FailEngine()],
    )
    assert result.ok is False
    assert result.text == "Bonjour"


def test_conversation_translation_state():
    conv = SimpleNamespace(translation={"enabled": True, "agent_lang": "en", "customer_lang": "fr"})
    st = tr.conversation_translation_state(conv)
    assert st == {"enabled": True, "agent_lang": "en", "customer_lang": "fr"}
    assert tr.conversation_translation_state(SimpleNamespace(translation=None))["enabled"] is False


# --------------------------------------------------------------------------
# language detection (lingua)
# --------------------------------------------------------------------------
def test_detect_language():
    assert tr.detect_language("Bonjour, comment allez-vous aujourd'hui ?") == "fr"
    assert tr.detect_language("") is None
    assert tr.detect_language("a") is None
