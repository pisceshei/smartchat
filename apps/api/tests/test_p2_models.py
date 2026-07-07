"""P2 model registration + LLM client wiring + embeddings (no live LLM)."""
from __future__ import annotations

import pytest
from py_contracts.llm import LLMProfile

from apps.api.app import models
from apps.api.app.models.ai import EMBED_DIM
from apps.api.app.services import embeddings
from apps.api.app.services import llm_client as lc


# --------------------------------------------------------------------------
# model metadata
# --------------------------------------------------------------------------
def test_all_p2_tables_registered():
    tables = set(models.Base.metadata.tables)
    expected = {
        # flows
        "flow_categories", "flows", "flow_versions", "flow_triggers",
        "keyword_dicts", "keyword_dict_items", "flow_sessions", "flow_session_steps",
        "flow_trigger_log", "flow_stats_daily", "flow_stats_users", "flow_templates",
        # ai
        "ai_agents", "ai_agent_usage", "intents", "ai_point_prices",
        "ai_point_balances", "translation_usage",
        "kb_collections", "kb_documents", "kb_chunks",
    }
    assert expected <= tables


def test_p1_translation_tables_not_redefined():
    # message_translations / translation_cache are P1; translation_usage is the
    # only new translation table. (A duplicate __tablename__ would raise on
    # import, so reaching here already proves no clash.)
    assert "message_translations" in models.Base.metadata.tables
    assert "translation_cache" in models.Base.metadata.tables
    assert "translation_usage" in models.Base.metadata.tables


def test_flow_templates_is_global():
    cols = set(models.FlowTemplate.__table__.columns.keys())
    assert "workspace_id" not in cols  # global gallery, no tenant scope


def test_flow_triggers_route_index():
    idx = {i.name: [c.name for c in i.columns] for i in models.FlowTrigger.__table__.indexes}
    assert idx["ix_flow_triggers_route"] == ["workspace_id", "channel_type", "trigger_type", "enabled"]


def test_kb_chunk_embedding_dim():
    col = models.KBChunk.__table__.columns["embedding"]
    assert EMBED_DIM == 1024
    assert getattr(col.type, "dim", None) == 1024


def test_ai_agent_unique_member():
    uniques = {c.name for c in models.AIAgent.__table__.constraints if c.name}
    assert "uq_ai_agents_member" in uniques


def test_flow_session_status_columns():
    cols = set(models.FlowSession.__table__.columns.keys())
    assert {"current_node_id", "variables", "waiting", "wakeup_at",
            "expires_at", "ended_at", "end_reason", "mode", "status"} <= cols


# --------------------------------------------------------------------------
# LLM client wiring
# --------------------------------------------------------------------------
class FakeLLM:
    """Injectable stand-in — never touches a network."""

    def __init__(self, dim: int = EMBED_DIM):
        self.dim = dim
        self.embed_calls: list[list[str]] = []
        self.complete_calls = 0

    async def complete(self, *, tier, system, messages, max_tokens=1024, temperature=0.3) -> str:
        self.complete_calls += 1
        return f"[{tier}] {messages[-1].content}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [[0.1] * self.dim for _ in texts]

    async def aclose(self) -> None:
        pass


def test_build_profile_from_settings():
    prof = lc.build_profile()
    assert isinstance(prof, LLMProfile)
    assert set(prof.model_map) == {"fast", "smart", "embed"}
    assert prof.provider in ("anthropic", "openai_compat")


def test_fake_satisfies_protocol():
    assert isinstance(FakeLLM(), lc.LLMClientProtocol)


def test_set_get_reset_default_llm():
    fake = FakeLLM()
    lc.set_default_llm(fake)
    try:
        assert lc.get_default_llm() is fake
    finally:
        lc.reset_default_llm()


def test_profile_from_row():
    class Row:
        provider = "openai_compat"
        base_url = "https://relay.example/v1"
        model_map = {"fast": "m-fast", "smart": "m-smart", "embed": "m-embed"}
        timeout_s = 30
        max_concurrency = 4

    prof = lc.profile_from_row(Row(), api_key="sk-test")
    assert prof.provider == "openai_compat"
    assert prof.api_key == "sk-test"
    assert prof.timeout_s == 30.0
    assert prof.max_concurrency == 4


# --------------------------------------------------------------------------
# embeddings
# --------------------------------------------------------------------------
def test_batched():
    assert list(embeddings.batched(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]
    with pytest.raises(ValueError):
        list(embeddings.batched(["a"], 0))


async def test_embed_texts_batches_and_orders():
    fake = FakeLLM()
    vecs = await embeddings.embed_texts([f"t{i}" for i in range(5)], client=fake, batch_size=2)
    assert len(vecs) == 5
    assert all(len(v) == EMBED_DIM for v in vecs)
    assert [len(b) for b in fake.embed_calls] == [2, 2, 1]  # batched


async def test_embed_texts_empty():
    assert await embeddings.embed_texts([], client=FakeLLM()) == []


async def test_embed_texts_rejects_wrong_dim():
    with pytest.raises(ValueError, match="dimension"):
        await embeddings.embed_texts(["x"], client=FakeLLM(dim=8))


async def test_embed_query_single_vector():
    v = await embeddings.embed_query("hello", client=FakeLLM())
    assert len(v) == EMBED_DIM


async def test_embed_texts_uses_default_when_no_client():
    fake = FakeLLM()
    lc.set_default_llm(fake)
    try:
        vecs = await embeddings.embed_texts(["a", "b"])
    finally:
        lc.reset_default_llm()
    assert len(vecs) == 2
    assert fake.embed_calls == [["a", "b"]]


# --------------------------------------------------------------------------
# marker protocol integration (shared contract)
# --------------------------------------------------------------------------
def test_marker_parse_roundtrip():
    from py_contracts.llm import parse_markers

    parsed = parse_markers("Sure! [CARD:sku-1,sku-2] [HANDOFF:pricing] [LEAD:email=a@b.com]")
    assert parsed.card_handles == ["sku-1", "sku-2"]
    assert parsed.handoff_reason == "pricing"
    assert parsed.lead_fields == {"email": "a@b.com"}
    assert "[CARD" not in parsed.text
