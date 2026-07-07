"""assist: prompt building, SSE chunking, points gating (fakes, no DB)."""
from __future__ import annotations

import uuid

import pytest

from apps.api.app.ai import assist, points_enforce
from apps.api.app.ai.points_enforce import EnforceResult

from .fakes import FakeLLM


def test_build_assist_messages_ops():
    for op in ("rewrite", "expand", "shorten", "fix_grammar"):
        system, messages = assist.build_assist_messages(op, "hello there")
        assert messages[0].content.endswith("Draft:\nhello there")
        assert "polish a draft" in system


def test_build_assist_messages_tone_and_translate():
    _, msgs = assist.build_assist_messages("tone", "hi", {"tone": "formal"})
    assert "formal tone" in msgs[0].content
    _, msgs2 = assist.build_assist_messages("translate_draft", "hi", {"target_lang": "French"})
    assert "into French" in msgs2[0].content


def test_build_assist_messages_errors():
    with pytest.raises(assist.AssistError) as e1:
        assist.build_assist_messages("nope", "hi")
    assert e1.value.code == "unknown_op"
    with pytest.raises(assist.AssistError) as e2:
        assist.build_assist_messages("translate_draft", "hi", {})
    assert e2.value.code == "missing_param"


def test_sse_chunks():
    assert assist.sse_chunks("") == []
    assert assist.sse_chunks("hello world") == ["hello", " world"]
    assert "".join(assist.sse_chunks("a b c")) == "a b c"


async def test_run_assist_ok(monkeypatch):
    async def ok_spend(*a, **k):
        return EnforceResult(ok=True, feature_key="composer", points_charged=2,
                             balance_after=48, hardstop="")

    monkeypatch.setattr(points_enforce, "spend", ok_spend)
    res = await assist.run_assist(None, None, workspace_id=uuid.uuid4(), op="rewrite",
                                  text="pls fix", client=FakeLLM(reply="Please fix this."))
    assert res.ok is True
    assert res.text == "Please fix this."
    assert res.balance_after == 48


async def test_run_assist_insufficient_points_raises(monkeypatch):
    async def blocked_spend(*a, **k):
        return EnforceResult(ok=False, feature_key="composer", points_charged=0,
                             balance_after=0, hardstop="error")

    monkeypatch.setattr(points_enforce, "spend", blocked_spend)
    with pytest.raises(assist.AssistError) as e:
        await assist.run_assist(None, None, workspace_id=uuid.uuid4(), op="rewrite",
                                text="x", client=FakeLLM())
    assert e.value.code == "insufficient_points"


async def test_run_assist_empty_draft_raises():
    with pytest.raises(assist.AssistError) as e:
        await assist.run_assist(None, None, workspace_id=uuid.uuid4(), op="rewrite",
                                text="   ", client=FakeLLM())
    assert e.value.code == "empty_draft"


async def test_stream_assist_ok(monkeypatch):
    async def ok_spend(*a, **k):
        return EnforceResult(ok=True, feature_key="composer", points_charged=2,
                             balance_after=10, hardstop="")

    monkeypatch.setattr(points_enforce, "spend", ok_spend)
    out = [
        chunk
        async for chunk in assist.stream_assist(
            None, None, workspace_id=uuid.uuid4(), op="shorten", text="long text",
            client=FakeLLM(reply="short"),
        )
    ]
    assert out[-1]["type"] == "done"
    assert out[-1]["text"] == "short"
    assert any(c["type"] == "delta" for c in out)


async def test_stream_assist_error_on_block(monkeypatch):
    async def blocked_spend(*a, **k):
        return EnforceResult(ok=False, feature_key="composer", points_charged=0,
                             balance_after=0, hardstop="error")

    monkeypatch.setattr(points_enforce, "spend", blocked_spend)
    out = [
        chunk
        async for chunk in assist.stream_assist(
            None, None, workspace_id=uuid.uuid4(), op="rewrite", text="x", client=FakeLLM()
        )
    ]
    assert out == [{"type": "error", "code": "insufficient_points",
                    "detail": "not enough AI points for composer assist"}]
