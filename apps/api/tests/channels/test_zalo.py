"""Zalo OA adapter — signature verification, inbound event parse, outbound
render shapes, send/error mapping, OAuth-v4 refresh, connect. All pure/faked —
no network (httpx.MockTransport), no real Zalo API.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
from py_contracts.content import (
    CardButton,
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButton,
    QuickButtonsBlock,
    TextBlock,
)

from apps.api.app.channels.adapters.zalo import (
    ZaloAdapter,
    verify_zalo_signature,
    zalo_mac,
)
from apps.api.app.channels.base import ContactUpdate, MessageIn, OptOut


def _adapter(handler=None) -> ZaloAdapter:
    if handler is None:
        return ZaloAdapter()
    return ZaloAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --------------------------------------------------------------------------
# 1. X-ZEvent-Signature
# --------------------------------------------------------------------------
def test_zalo_mac_matches_spec():
    app_id, body, ts, secret = "app123", b'{"event_name":"user_send_text"}', "1600000000000", "S3C"
    expected = "mac=" + hashlib.sha256((app_id + body.decode() + ts + secret).encode()).hexdigest()
    assert zalo_mac(app_id, body, ts, secret) == expected


def test_verify_signature_with_and_without_prefix():
    app_id, body, ts, secret = "app123", b'{"a":1}', "1700000000000", "S3C"
    full = zalo_mac(app_id, body, ts, secret)  # "mac=<hex>"
    assert verify_zalo_signature(app_id, body, ts, secret, full)
    assert verify_zalo_signature(app_id, body, ts, secret, full[4:])  # bare hex


def test_verify_signature_rejects_tampered_body_or_missing():
    app_id, body, ts, secret = "app123", b'{"a":1}', "1700000000000", "S3C"
    good = zalo_mac(app_id, body, ts, secret)
    assert not verify_zalo_signature(app_id, b'{"a":2}', ts, secret, good)
    assert not verify_zalo_signature(app_id, body, ts, secret, None)


# --------------------------------------------------------------------------
# 2. inbound parse
# --------------------------------------------------------------------------
def test_parse_user_send_text():
    payload = {
        "event_name": "user_send_text",
        "timestamp": "1600000000000",
        "sender": {"id": "U1"},
        "message": {"msg_id": "M1", "text": "hello"},
    }
    events = _adapter().parse_inbound(payload)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, MessageIn)
    assert ev.external_user_id == "U1"
    assert ev.external_message_id == "M1"
    assert ev.content.blocks[0].text == "hello"
    assert ev.external_timestamp is not None


def test_parse_user_send_image_makes_url_media_ref():
    payload = {
        "event_name": "user_send_image",
        "sender": {"id": "U1"},
        "message": {"msg_id": "M2", "attachments": [{"type": "image", "payload": {"url": "https://img"}}]},
    }
    ev = _adapter().parse_inbound(payload)[0]
    assert ev.content.blocks[0].media_type == "image"
    assert ev.media_refs[0].ref == {"kind": "url", "url": "https://img"}
    assert ev.media_refs[0].block_index == 0


def test_parse_follow_and_unfollow():
    a = _adapter()
    follow = a.parse_inbound({"event_name": "follow", "follower": {"id": "U7"}})
    assert isinstance(follow[0], ContactUpdate) and follow[0].external_user_id == "U7"
    unfollow = a.parse_inbound({"event_name": "unfollow", "follower": {"id": "U7"}})
    assert isinstance(unfollow[0], OptOut) and unfollow[0].reason == "unfollow"


def test_parse_ignores_oa_echo_and_incomplete():
    a = _adapter()
    assert a.parse_inbound({"event_name": "oa_send_text", "message": {"text": "x"}}) == []
    # missing sender / msg_id → dropped
    assert a.parse_inbound({"event_name": "user_send_text", "message": {"text": "x"}}) == []


# --------------------------------------------------------------------------
# 3. outbound render
# --------------------------------------------------------------------------
def test_render_text():
    assert _adapter().render(MessageContent(blocks=[TextBlock(text="hi")])) == [{"text": "hi"}]


def test_render_quick_buttons_list_template_uses_button_id():
    block = QuickButtonsBlock(
        text="Pick", buttons=[QuickButton(id="a", text="A"), QuickButton(id="b", text="B")]
    )
    payload = _adapter().render(MessageContent(blocks=[block]))[0]
    tpl = payload["attachment"]["payload"]
    assert tpl["template_type"] == "list"
    assert tpl["buttons"][0] == {"title": "A", "type": "oa.query.show", "payload": "a"}


def test_render_product_card_with_url_button():
    card = ProductCardBlock(
        title="Prod",
        url="https://shop/p",
        buttons=[CardButton(text="Buy", action="url", value="https://shop/buy")],
    )
    tpl = _adapter().render(MessageContent(blocks=[card]))[0]["attachment"]["payload"]
    assert tpl["template_type"] == "list"
    assert tpl["elements"][0]["default_action"] == {"type": "oa.open.url", "url": "https://shop/p"}
    assert tpl["buttons"][0] == {"title": "Buy", "type": "oa.open.url", "payload": {"url": "https://shop/buy"}}


def test_render_image_media_template():
    fid = "00000000-0000-0000-0000-000000000123"
    payload = _adapter().render(MessageContent(blocks=[MediaBlock(media_type="image", file_id=fid)]))[0]
    tpl = payload["attachment"]["payload"]
    assert tpl["template_type"] == "media"
    assert tpl["elements"][0]["media_type"] == "image"
    assert str(fid) in tpl["elements"][0]["url"]


# --------------------------------------------------------------------------
# 4. send shape + error mapping
# --------------------------------------------------------------------------
async def test_send_text_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["token"] = request.headers.get("access_token")
        return httpx.Response(200, json={"error": 0, "message": "Success", "data": {"message_id": "mid1"}})

    adapter = _adapter(handler)
    acct = SimpleNamespace(external_id="OA1", config={})
    res = await adapter.send(acct, {"access_token": "TOK"}, "U1", {"text": "hello"})
    assert res.ok and res.external_message_id == "mid1"
    assert captured["url"].endswith("/v3.0/oa/message/cs")
    assert captured["body"] == {"recipient": {"user_id": "U1"}, "message": {"text": "hello"}}
    assert captured["token"] == "TOK"


async def test_send_maps_token_error_to_auth():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": -216, "message": "access token invalid"})

    res = await _adapter(handler).send(SimpleNamespace(external_id="OA1", config={}), {}, "U1", {"text": "x"})
    assert not res.ok and res.error_code == "AUTH"


def test_classify_error_table():
    assert ZaloAdapter.classify_error(401, None) == "AUTH"
    assert ZaloAdapter.classify_error(200, -211) == "INVALID_RECIPIENT"
    assert ZaloAdapter.classify_error(200, -49) == "RATE_LIMITED"
    assert ZaloAdapter.classify_error(500, None) == "RETRYABLE"


# --------------------------------------------------------------------------
# 5. OAuth v4 refresh (rotating refresh token)
# --------------------------------------------------------------------------
async def test_refresh_rotates_tokens():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["secret_key"] = request.headers.get("secret_key")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "NEW", "refresh_token": "RT2", "expires_in": "3600"})

    adapter = _adapter(handler)
    creds = {"access_token": "OLD", "refresh_token": "RT1", "app_id": "APP", "oa_secret": "SEC"}
    updated = await adapter.refresh_credentials(None, creds)
    assert updated is not None
    assert updated["access_token"] == "NEW"
    assert updated["refresh_token"] == "RT2"  # rotated
    assert "token_expires_at" in updated
    assert captured["secret_key"] == "SEC"
    assert "grant_type=refresh_token" in captured["body"]
    assert "refresh_token=RT1" in captured["body"]


async def test_refresh_missing_fields_returns_none():
    assert await _adapter().refresh_credentials(None, {"refresh_token": "only"}) is None


# --------------------------------------------------------------------------
# 6. connect_validate
# --------------------------------------------------------------------------
async def test_connect_validate_ok_resolves_oa():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getoa")
        return httpx.Response(200, json={"error": 0, "data": {"oa_id": "OA9", "name": "Shop"}})

    cr = await _adapter(handler).connect_validate({"oa_id": "OA9"}, {"access_token": "T", "app_id": "APP"})
    assert cr.external_id == "OA9"
    assert cr.name == "Shop"
    assert cr.health.ok
    assert cr.needs_webhook_secret is True
    assert cr.config_patch["app_id"] == "APP"


async def test_connect_validate_missing_token():
    cr = await _adapter().connect_validate({}, {})
    assert not cr.health.ok
