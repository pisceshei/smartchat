"""P3 model registration + Stripe-disabled degradation + bge embed swap.

Pure/metadata + monkeypatched unit tests — no DB, no network. The live
migration up/down and ORM round-trip are exercised by the verification script.
"""
from __future__ import annotations

import types

import pytest

from apps.api.app import models
from apps.api.app.services import embeddings, embeddings_bge, stripe_client
from apps.api.app.services import llm_client as lc


# --------------------------------------------------------------------------
# model registration
# --------------------------------------------------------------------------
def test_all_p3_tables_registered():
    tables = set(models.Base.metadata.tables)
    expected = {
        # marketing
        "segments", "broadcasts", "broadcast_runs", "broadcast_recipients",
        "msg_templates", "sms_signatures", "split_links", "split_link_clicks",
        "edm_campaigns",
        # reports
        "agg_messages_hourly", "agg_conversations_hourly", "agg_agent_hourly",
        "agg_customers_daily", "agg_ads_daily", "agent_presence_sessions",
        "conversation_attribution", "report_shares", "report_exports",
        "report_ai_summaries", "rollup_watermark",
        # billing
        "workspace_balance", "balance_ledger", "billing_orders", "stripe_events",
        "invoices",
    }
    assert expected <= tables


def test_p1_tenancy_billing_tables_not_redefined():
    # plans/subscriptions/usage_counters/ai_points_ledger stay in tenancy.py.
    assert {"plans", "subscriptions", "usage_counters", "ai_points_ledger"} <= set(
        models.Base.metadata.tables
    )


def test_partitioned_tables_declared():
    assert models.MARKETING_PARTITIONED_TABLES == {"broadcast_recipients", "split_link_clicks"}


def test_broadcast_recipients_partition_pk_includes_created_at():
    pk = [c.name for c in models.BroadcastRecipient.__table__.primary_key.columns]
    assert pk == ["id", "created_at"]
    state = models.BroadcastRecipient.__table__.columns["state"]
    assert state.type.length == 12


def test_split_link_clicks_partition_pk_includes_ts():
    pk = [c.name for c in models.SplitLinkClick.__table__.primary_key.columns]
    assert pk == ["id", "ts"]


def test_agg_messages_hourly_composite_pk():
    pk = [c.name for c in models.AggMessagesHourly.__table__.primary_key.columns]
    assert pk == ["workspace_id", "hour", "channel_type", "agent_id", "direction", "ai_flag"]


def test_split_links_slug_unique():
    uniques = {c.name for c in models.SplitLink.__table__.constraints if c.name}
    assert "uq_split_links_slug" in uniques


def test_stripe_events_pk_is_event_id():
    pk = [c.name for c in models.StripeEvent.__table__.primary_key.columns]
    assert pk == ["event_id"]


def test_workspace_balance_pk_is_workspace_id():
    pk = [c.name for c in models.WorkspaceBalance.__table__.primary_key.columns]
    assert pk == ["workspace_id"]


def test_billing_order_money_columns_present():
    cols = set(models.BillingOrder.__table__.columns.keys())
    assert {
        "base_cents", "addons_cents", "discount_cents", "handling_fee_cents",
        "balance_applied_cents", "amount_due_cents", "currency", "stripe_ref", "kind",
    } <= cols


def test_broadcast_recipient_workspace_first_on_indexes():
    idx = {i.name: [c.name for c in i.columns] for i in models.BroadcastRecipient.__table__.indexes}
    assert idx["ix_broadcast_recipients_ws_contact"][0] == "workspace_id"


# --------------------------------------------------------------------------
# Stripe wrapper degrades to "billing disabled" (no key, no network)
# --------------------------------------------------------------------------
def _settings(**over):
    base = dict(stripe_secret_key="", stripe_webhook_secret="", stripe_currency="usd")
    base.update(over)
    return types.SimpleNamespace(**base)


def test_get_stripe_none_without_key():
    assert stripe_client.get_stripe(_settings()) is None
    assert stripe_client.billing_enabled(_settings()) is False


async def test_create_payment_intent_raises_when_disabled():
    with pytest.raises(stripe_client.BillingDisabledError):
        await stripe_client.create_payment_intent(
            amount_cents=1000, metadata={}, settings=_settings()
        )


def test_construct_event_raises_without_secret():
    with pytest.raises(stripe_client.BillingDisabledError):
        stripe_client.construct_event(b"{}", "sig", settings=_settings())


def test_verify_webhook_signature_is_construct_event_alias():
    assert stripe_client.verify_webhook_signature is stripe_client.construct_event


# --------------------------------------------------------------------------
# bge-m3 embed client
# --------------------------------------------------------------------------
async def test_embeddings_bge_unavailable_without_base_url(monkeypatch):
    monkeypatch.setattr(
        embeddings_bge, "get_settings", lambda: _settings(embed_base_url="", embed_timeout_s=5.0)
    )
    with pytest.raises(embeddings_bge.EmbeddingsUnavailableError):
        await embeddings_bge.embed_texts(["hello"])


async def test_embeddings_bge_empty_input_is_noop(monkeypatch):
    monkeypatch.setattr(
        embeddings_bge, "get_settings", lambda: _settings(embed_base_url="", embed_timeout_s=5.0)
    )
    assert await embeddings_bge.embed_texts([]) == []


def test_embeddings_bge_dim_check_rejects_wrong_width():
    with pytest.raises(ValueError, match="dimension"):
        embeddings_bge._check_dim([[0.1] * 8])


# --------------------------------------------------------------------------
# the RAG production swap in services/embeddings
# --------------------------------------------------------------------------
class _FakeLLM:
    def __init__(self):
        self.embed_calls: list[list[str]] = []

    async def complete(self, *, tier, system, messages, max_tokens=1024, temperature=0.3):
        return ""

    async def embed(self, texts):
        self.embed_calls.append(list(texts))
        return [[0.2] * embeddings.EMBED_DIM for _ in texts]

    async def aclose(self):
        pass


async def test_swap_prefers_injected_default_over_bge(monkeypatch):
    """An explicitly injected default (test/DI) wins even when EMBED_BASE_URL is
    set — it must not route to the sidecar."""
    called = {"bge": 0}

    async def _bge(texts, *, batch_size=64):  # pragma: no cover — must NOT run
        called["bge"] += 1
        return [[0.0] * embeddings.EMBED_DIM for _ in texts]

    monkeypatch.setattr(embeddings_bge, "embed_texts", _bge)
    fake = _FakeLLM()
    lc.set_default_llm(fake)
    try:
        out = await embeddings.embed_texts(["a", "b"])
    finally:
        lc.reset_default_llm()
    assert called["bge"] == 0
    assert fake.embed_calls == [["a", "b"]]
    assert len(out) == 2


async def test_swap_routes_to_bge_when_configured(monkeypatch):
    """No injected default + EMBED_BASE_URL set → route to the bge sidecar."""
    lc.reset_default_llm()
    monkeypatch.setattr(
        "apps.api.app.settings.get_settings",
        lambda: _settings(embed_base_url="http://embed:8090", embed_timeout_s=5.0),
    )
    recorded: list[list[str]] = []

    async def _bge(texts, *, batch_size=64):
        recorded.append(list(texts))
        return [[0.5] * embeddings.EMBED_DIM for _ in texts]

    monkeypatch.setattr(embeddings_bge, "embed_texts", _bge)
    out = await embeddings.embed_texts(["x"])
    assert recorded == [["x"]]
    assert len(out) == 1 and len(out[0]) == embeddings.EMBED_DIM
