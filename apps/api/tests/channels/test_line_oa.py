"""LINE OA adapter — webhook signature verify, inbound parse, outbound send
shape, and set_webhook (the connect path now auto-registers the endpoint). All
faked — no network (httpx.MockTransport), no real LINE API."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
from py_contracts.content import MessageContent, TextBlock

from apps.api.app.channels.adapters.line_oa import LineAdapter
from apps.api.app.channels.base import ContactUpdate, MessageIn, OptOut


def _adapter(handler=None) -> LineAdapter:
    if handler is None:
        return LineAdapter()
    return LineAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _sig(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


# -- webhook signature -----------------------------------------------------
def test_verify_webhook_accepts_valid_and_rejects_tampered():
    a = _adapter()
    body = b'{"events":[]}'
    good = _sig("SEC", body)
    assert a.verify_webhook(headers={"x-line-signature": good}, body=body, secret="SEC")
    assert not a.verify_webhook(headers={"x-line-signature": good}, body=b'{"x":1}', secret="SEC")
    assert not a.verify_webhook(headers={}, body=body, secret="SEC")


# -- inbound parse ---------------------------------------------------------
def test_parse_text_message():
    payload = {
        "events": [
            {
                "type": "message",
                "timestamp": 1600000000000,
                "source": {"userId": "U1"},
                "replyToken": "R1",
                "message": {"id": "M1", "type": "text", "text": "hello"},
            }
        ]
    }
    ev = _adapter().parse_inbound(payload)[0]
    assert isinstance(ev, MessageIn)
    assert ev.external_user_id == "U1" and ev.external_message_id == "M1"
    assert ev.content.blocks[0].text == "hello"
    assert ev.meta["reply_token"] == "R1"


def test_parse_follow_and_unfollow():
    a = _adapter()
    follow = a.parse_inbound({"events": [{"type": "follow", "source": {"userId": "U7"}}]})
    assert isinstance(follow[0], ContactUpdate)
    unfollow = a.parse_inbound({"events": [{"type": "unfollow", "source": {"userId": "U7"}}]})
    assert isinstance(unfollow[0], OptOut) and unfollow[0].reason == "unfollow"


def test_parse_skips_events_without_user():
    assert _adapter().parse_inbound({"events": [{"type": "message", "message": {"text": "x"}}]}) == []


# -- outbound send ---------------------------------------------------------
async def test_send_push_shape_and_token():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"sentMessages": [{"id": "S1"}]})

    res = await _adapter(handler).send(
        SimpleNamespace(external_id="ch"), {"access_token": "TOK"}, "U1", {"type": "text", "text": "hi"}
    )
    assert res.ok and res.external_message_id == "S1"
    assert captured["url"].endswith("/v2/bot/message/push")
    assert captured["auth"] == "Bearer TOK"
    assert captured["body"] == {"to": "U1", "messages": [{"type": "text", "text": "hi"}]}


async def test_send_maps_401_to_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid token"})

    res = await _adapter(handler).send(
        SimpleNamespace(external_id="ch"), {"access_token": "bad"}, "U1", {"type": "text", "text": "x"}
    )
    assert not res.ok and res.error_code == "AUTH"


def test_render_text():
    assert _adapter().render(MessageContent(blocks=[TextBlock(text="hi")])) == [
        {"type": "text", "text": "hi"}
    ]


# -- set_webhook (connect auto-registration) -------------------------------
async def test_set_webhook_puts_endpoint():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    ok = await _adapter(handler).set_webhook("TOK", "https://x.test/hooks/line/s3cret")
    assert ok is True
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/v2/bot/channel/webhook/endpoint")
    assert captured["auth"] == "Bearer TOK"
    assert captured["body"] == {"endpoint": "https://x.test/hooks/line/s3cret"}


async def test_set_webhook_returns_false_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad"})

    assert await _adapter(handler).set_webhook("TOK", "https://x/y") is False
