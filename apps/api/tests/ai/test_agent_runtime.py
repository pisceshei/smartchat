"""agent_runtime: pure marker/context/handoff helpers (no DB / LLM)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from py_contracts.llm import parse_markers

from apps.api.app.ai import agent_runtime as ar
from apps.api.app.ai.rag import Retrieved, RetrievedChunk


def test_keyword_hit():
    assert ar.keyword_hit("I want a REFUND now", ["refund", "cancel"]) is True
    assert ar.keyword_hit("just browsing", ["refund"]) is False
    assert ar.keyword_hit("anything", None) is False
    assert ar.keyword_hit("anything", []) is False


def test_validate_cards_drops_hallucinated_and_dedupes():
    catalog = {"sku-1": {}, "sku-2": {}}
    assert ar.validate_cards(["sku-1", "ghost", "sku-2", "sku-1"], catalog) == ["sku-1", "sku-2"]
    assert ar.validate_cards(["nope"], catalog) == []


def test_build_card_blocks():
    catalog = {"sku-1": {"title": "Blue Widget", "price": "9.99", "currency": "USD",
                         "url": "https://x/y", "image_url": "https://img"}}
    blocks = ar.build_card_blocks(["sku-1"], catalog)
    assert blocks[0]["kind"] == "product_card"
    assert blocks[0]["title"] == "Blue Widget"
    assert blocks[0]["price"] == "9.99"
    assert blocks[0]["url"] == "https://x/y"


def test_split_lead_fields_whitelist_and_custom():
    scalars, custom = ar.split_lead_fields({
        "email": "a@b.com", "phone": "+123", "vip": "yes",
        "custom.tier": "gold", "display_name": "  Ann  ", "blank": "",
    })
    assert scalars == {"email": "a@b.com", "phone": "+123", "display_name": "Ann"}
    assert custom == {"vip": "yes", "tier": "gold"}
    assert "blank" not in scalars and "blank" not in custom


def test_persona_system_prompt_with_context_and_cards():
    retrieved = Retrieved(chunks=[
        RetrievedChunk(id=uuid.uuid4(), document_id=uuid.uuid4(),
                       text="Returns accepted within 30 days.", meta={}, score=1.0)
    ])
    persona = {"role": "You are Acme support.", "tone": "friendly", "languages": ["en", "fr"]}
    prompt = ar.persona_system_prompt(persona, retrieved, ["sku-1", "sku-2"], allow_cards=True)
    assert "Acme support" in prompt
    assert "friendly" in prompt
    assert "ONLY the CONTEXT" in prompt
    assert "Returns accepted within 30 days." in prompt
    assert "[CARD:handle1,handle2]" in prompt
    assert "sku-1" in prompt
    assert "[HANDOFF:reason]" in prompt


def test_persona_system_prompt_no_context_hints_handoff():
    prompt = ar.persona_system_prompt({}, Retrieved(), [], allow_cards=False)
    assert "no knowledge-base context" in prompt
    assert "[HANDOFF:no_context]" in prompt
    assert "[CARD:" not in prompt  # cards not offered when disabled


def test_history_to_messages_roles_and_skips():
    rows = [
        SimpleNamespace(is_note=False, msg_type="text", sender_type="contact", text_plain="hi"),
        SimpleNamespace(is_note=False, msg_type="text", sender_type="ai_agent", text_plain="hello!"),
        SimpleNamespace(is_note=True, msg_type="text", sender_type="ai_agent", text_plain="note"),
        SimpleNamespace(is_note=False, msg_type="system_event", sender_type="system", text_plain="x"),
        SimpleNamespace(is_note=False, msg_type="text", sender_type="contact", text_plain="  "),
        SimpleNamespace(is_note=False, msg_type="text", sender_type="member", text_plain="human reply"),
    ]
    msgs = ar.history_to_messages(rows)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "hi"), ("assistant", "hello!"), ("assistant", "human reply"),
    ]


def test_marker_pipeline_reply_with_valid_card():
    parsed = parse_markers("Sure, here it is! [CARD:sku-1,ghost] [LEAD:email=a@b.com]")
    catalog = {"sku-1": {"title": "Widget"}}
    kept = ar.validate_cards(parsed.card_handles, catalog)
    assert kept == ["sku-1"]  # ghost dropped
    assert parsed.lead_fields == {"email": "a@b.com"}
    assert "[CARD" not in parsed.text


def test_marker_pipeline_handoff():
    parsed = parse_markers("I'll connect you to a specialist. [HANDOFF:pricing]")
    assert parsed.handoff_reason == "pricing"
    assert "specialist" in parsed.text
    assert "[HANDOFF" not in parsed.text
