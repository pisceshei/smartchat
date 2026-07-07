"""TikTok Business adapter — comment webhook parse, text-only render, comment
reply send shape + error mapping, target split, connect/health. Pure/faked
(httpx.MockTransport). Honest scope: DM is gated (send_dm returns unsupported);
the working path is video comment reply.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from py_contracts.content import CardButton, MessageContent, ProductCardBlock, TextBlock

from apps.api.app.channels.adapters.tiktok_business import (
    TikTokBusinessAdapter,
    _split_target,
)
from apps.api.app.channels.base import MessageIn


def _adapter(handler=None) -> TikTokBusinessAdapter:
    if handler is None:
        return TikTokBusinessAdapter()
    return TikTokBusinessAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --------------------------------------------------------------------------
# 1. target key helper
# --------------------------------------------------------------------------
def test_split_target():
    assert _split_target("VID1:CMT1") == ("VID1", "CMT1")
    assert _split_target("only") == ("only", "")


# --------------------------------------------------------------------------
# 2. inbound parse (webhook comment event)
# --------------------------------------------------------------------------
def test_parse_comment_event():
    payload = {
        "event": "comment.create",
        "create_time": 1600000000,
        "content": {
            "comment_id": "CMT1",
            "video_id": "VID1",
            "text": "nice",
            "nickname": "Bob",
            "unique_id": "bob",
            "user_id": "U9",
        },
    }
    ev = _adapter().parse_inbound(payload)[0]
    assert isinstance(ev, MessageIn)
    assert ev.external_message_id == "CMT1"
    assert ev.external_user_id == "VID1:CMT1"  # encodes reply target
    assert ev.content.blocks[0].text == "nice"
    assert ev.profile.display_name == "Bob"
    assert ev.meta == {"video_id": "VID1", "comment_id": "CMT1"}
    assert ev.external_timestamp is not None


def test_parse_events_list_with_stringified_content():
    payload = {
        "events": [
            {"content": json.dumps({"comment_id": "C2", "video_id": "V2", "text": "hi"})},
            {"content": {"video_id": "V3"}},  # no comment_id/text → dropped
        ]
    }
    events = _adapter().parse_inbound(payload)
    assert len(events) == 1
    assert events[0].external_user_id == "V2:C2"


# --------------------------------------------------------------------------
# 3. text-only render
# --------------------------------------------------------------------------
def test_render_card_degrades_to_text():
    card = ProductCardBlock(
        title="Prod", url="https://s/p", buttons=[CardButton(text="Buy", action="url", value="https://s/b")]
    )
    payloads = _adapter().render(MessageContent(blocks=[card]))
    assert all(set(p.keys()) == {"text"} for p in payloads)
    assert "Prod" in payloads[0]["text"]


def test_render_text_passthrough():
    assert _adapter().render(MessageContent(blocks=[TextBlock(text="thanks")])) == [{"text": "thanks"}]


# --------------------------------------------------------------------------
# 4. send (comment reply) shape + errors
# --------------------------------------------------------------------------
async def test_send_reply_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["token"] = request.headers.get("access-token")
        return httpx.Response(200, json={"code": 0, "message": "OK", "data": {"comment_id": "REPLY1"}})

    acct = SimpleNamespace(external_id="BIZ1", config={})
    res = await _adapter(handler).send(acct, {"access_token": "TOK"}, "VID1:CMT1", {"text": "thank you"})
    assert res.ok and res.external_message_id == "REPLY1"
    assert captured["url"].endswith("/business/comment/reply/create/")
    assert captured["body"] == {
        "business_id": "BIZ1",
        "video_id": "VID1",
        "comment_id": "CMT1",
        "text": "thank you",
    }
    assert captured["token"] == "TOK"


async def test_send_rejects_bad_target():
    acct = SimpleNamespace(external_id="BIZ1", config={})
    res = await _adapter().send(acct, {}, "only-comment", {"text": "x"})
    assert not res.ok and res.error_code == "INVALID_RECIPIENT"


async def test_send_maps_code_to_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40100, "message": "access token invalid"})

    res = await _adapter(handler).send(
        SimpleNamespace(external_id="BIZ1", config={}), {}, "VID1:CMT1", {"text": "x"}
    )
    assert not res.ok and res.error_code == "AUTH"


def test_classify_error_table():
    assert TikTokBusinessAdapter.classify_error(401, None) == "AUTH"
    assert TikTokBusinessAdapter.classify_error(200, 40100) == "AUTH"
    assert TikTokBusinessAdapter.classify_error(200, 50002) == "RATE_LIMITED"
    assert TikTokBusinessAdapter.classify_error(500, None) == "RETRYABLE"


async def test_send_dm_is_gated():
    res = await _adapter().send_dm(SimpleNamespace(external_id="BIZ1"), {}, "U1", "hi")
    assert not res.ok and res.error_code == "UNSUPPORTED_CONTENT"


# --------------------------------------------------------------------------
# 5. connect_validate / health
# --------------------------------------------------------------------------
async def test_connect_validate_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/business/get/")
        return httpx.Response(200, json={"code": 0, "data": {"username": "shop", "display_name": "Shop"}})

    cr = await _adapter(handler).connect_validate({"business_id": "BIZ1"}, {"access_token": "T"})
    assert cr.external_id == "BIZ1"
    assert cr.name == "Shop"
    assert cr.health.ok
    assert cr.needs_webhook_secret is True
    assert cr.config_patch["username"] == "shop"


async def test_connect_validate_missing_fields():
    cr = await _adapter().connect_validate({}, {})
    assert not cr.health.ok


async def test_check_health_token_invalid():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40100, "message": "invalid"})

    acct = SimpleNamespace(external_id="BIZ1", config={})
    hr = await _adapter(handler).check_health(acct, {"access_token": "x"})
    assert not hr.ok and hr.status == "token_expired"
