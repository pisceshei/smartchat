"""VK adapter: Callback API parse (message_new / attachments / message_deny),
keyboard render, connect_validate (groups.getById), error classification — plus
the webhook confirmation echo and callback-secret gate.

All pure/faked: no VK API is contacted (recorded payloads + httpx.MockTransport +
monkeypatched account lookup).
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from py_contracts.content import (
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButton,
    QuickButtonsBlock,
    TextBlock,
)

from apps.api.app.channels.adapters.vk import VKAdapter
from apps.api.app.channels.base import OptOut
from apps.api.app.db import get_session
from apps.api.app.modules.hooks import vk as vk_hook


# --------------------------------------------------------------------------
# 1. Callback API parse
# --------------------------------------------------------------------------
def test_parse_message_new():
    adapter = VKAdapter()
    payload = {
        "type": "message_new",
        "event_id": "evt1",
        "v": "5.199",
        "group_id": 222,
        "secret": "S1",
        "object": {
            "message": {
                "from_id": 555,
                "peer_id": 555,
                "date": 1700000000,
                "text": "привет",
                "id": 42,
                "conversation_message_id": 7,
                "attachments": [],
            },
            "client_info": {},
        },
    }
    events = adapter.parse_inbound(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.external_message_id == "evt1"
    assert ev.external_user_id == "555"
    assert isinstance(ev.content.blocks[0], TextBlock)
    assert ev.content.blocks[0].text == "привет"
    assert ev.meta["from_id"] == 555


def test_parse_message_new_legacy_unwrapped_object():
    # pre-5.103 delivered the message directly as `object`
    adapter = VKAdapter()
    payload = {
        "type": "message_new",
        "object": {"from_id": 7, "peer_id": 7, "text": "hey", "id": 1, "date": 1700000000},
    }
    events = adapter.parse_inbound(payload)
    assert len(events) == 1
    assert events[0].external_user_id == "7"


def test_parse_photo_attachment_picks_largest():
    adapter = VKAdapter()
    payload = {
        "type": "message_new",
        "event_id": "e2",
        "object": {
            "message": {
                "from_id": 9,
                "peer_id": 9,
                "date": 1700000000,
                "text": "",
                "attachments": [
                    {
                        "type": "photo",
                        "photo": {
                            "id": 1,
                            "sizes": [
                                {"type": "m", "url": "http://p/m.jpg", "width": 130},
                                {"type": "x", "url": "http://p/x.jpg", "width": 604},
                            ],
                        },
                    }
                ],
            }
        },
    }
    events = adapter.parse_inbound(payload)
    ev = events[0]
    assert isinstance(ev.content.blocks[0], MediaBlock)
    assert ev.media_refs[0].ref["url"] == "http://p/x.jpg"  # largest width wins


def test_parse_audio_message_is_voice():
    adapter = VKAdapter()
    payload = {
        "type": "message_new",
        "event_id": "e3",
        "object": {
            "message": {
                "from_id": 9,
                "peer_id": 9,
                "date": 1700000000,
                "attachments": [
                    {
                        "type": "audio_message",
                        "audio_message": {"duration": 5, "link_ogg": "http://a/v.ogg"},
                    }
                ],
            }
        },
    }
    ev = adapter.parse_inbound(payload)[0]
    assert ev.content.blocks[0].media_type == "voice"
    assert ev.media_refs[0].ref["url"] == "http://a/v.ogg"


def test_parse_message_deny_is_opt_out():
    adapter = VKAdapter()
    events = adapter.parse_inbound({"type": "message_deny", "object": {"user_id": 321}})
    assert len(events) == 1
    assert isinstance(events[0], OptOut)
    assert events[0].external_user_id == "321"


def test_parse_unknown_type_ignored():
    adapter = VKAdapter()
    assert adapter.parse_inbound({"type": "group_join", "object": {}}) == []


# --------------------------------------------------------------------------
# 2. render (keyboards)
# --------------------------------------------------------------------------
def test_render_quick_buttons_inline_keyboard():
    adapter = VKAdapter()
    content = MessageContent(
        blocks=[
            QuickButtonsBlock(
                text="Choose",
                buttons=[QuickButton(id="a", text="A"), QuickButton(id="b", text="B")],
            )
        ]
    )
    p = adapter.render(content)[0]
    assert p["_method"] == "messages.send"
    kb = p["keyboard"]
    assert kb["inline"] is True
    labels = [row[0]["action"]["label"] for row in kb["buttons"]]
    assert labels == ["A", "B"]
    assert kb["buttons"][0][0]["action"]["type"] == "text"


def test_render_card_degrades_to_text_with_link():
    # VK has no native product card (CAPABILITIES["vk"].product_cards is False),
    # so degrade_content converts it to a text message carrying the link — there
    # is no VK keyboard for a card.
    adapter = VKAdapter()
    content = MessageContent(
        blocks=[ProductCardBlock(title="Item", price="5", url="https://shop/i")]
    )
    out = adapter.render(content)
    joined = " ".join(p.get("message", "") for p in out)
    assert "https://shop/i" in joined
    assert all("keyboard" not in p for p in out)


# --------------------------------------------------------------------------
# 3. connect_validate (groups.getById, mocked http)
# --------------------------------------------------------------------------
def _adapter(handler) -> VKAdapter:
    return VKAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_connect_validate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/groups.getById")
        assert request.url.params["access_token"] == "tok"
        return httpx.Response(
            200,
            json={"response": {"groups": [{"id": 222, "name": "My Community", "screen_name": "mycom"}]}},
        )

    cr = await _adapter(handler).connect_validate(
        {"group_id": "222", "confirmation_string": "CONF"},
        {"community_token": "tok", "secret": "S1"},
    )
    assert cr.external_id == "222"
    assert cr.name == "My Community"
    assert cr.health.ok is True
    assert cr.config_patch["confirmation_string"] == "CONF"
    assert cr.config_patch["group_id"] == "222"
    assert cr.needs_webhook_secret is True


async def test_connect_validate_requires_token():
    cr = await VKAdapter().connect_validate({"group_id": "1"}, {})
    assert cr.health.ok is False
    assert "community_token" in cr.health.detail["error"]


async def test_connect_validate_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"error_code": 5, "error_msg": "auth failed"}})

    cr = await _adapter(handler).connect_validate({"group_id": "1"}, {"community_token": "bad"})
    assert cr.health.ok is False
    assert cr.health.status == "token_expired"


# --------------------------------------------------------------------------
# 4. error classification
# --------------------------------------------------------------------------
def test_classify_error():
    c = VKAdapter.classify_error
    assert c({"error_code": 5})[0] == "AUTH"
    assert c({"error_code": 6})[0] == "RATE_LIMITED"
    assert c({"error_code": 914})[0] == "MESSAGE_TOO_LONG"
    assert c({"error_code": 901})[0] == "BLOCKED"
    assert c({"error_code": 7})[0] == "INVALID_RECIPIENT"
    assert c({"error_code": 100})[0] == "PERMANENT"


# --------------------------------------------------------------------------
# 5. webhook confirmation + secret gate
# --------------------------------------------------------------------------
def _client(
    monkeypatch,
    *,
    acct=None,
    creds: dict | None = None,
    default_conf: str = "DEFCONF",
) -> tuple[TestClient, list]:
    enqueued: list = []

    async def _fake_account_by_secret(session, types, secret):
        return acct

    async def _fake_get_credentials(session, account):
        return creds or {}

    async def _fake_enqueue(account, payload):
        enqueued.append((account, payload))

    async def _no_session():
        yield None

    monkeypatch.setattr(vk_hook, "_account_by_secret", _fake_account_by_secret)
    monkeypatch.setattr(vk_hook, "get_credentials", _fake_get_credentials)
    monkeypatch.setattr(vk_hook, "_enqueue", _fake_enqueue)
    monkeypatch.setattr(
        vk_hook, "get_settings", lambda: SimpleNamespace(vk_confirmation_default=default_conf)
    )

    app = FastAPI()
    app.include_router(vk_hook.router)
    app.dependency_overrides[get_session] = _no_session
    return TestClient(app), enqueued


def _fake_acct():
    return SimpleNamespace(
        id="acc-1",
        workspace_id="ws-1",
        channel_type="vk",
        enabled=True,
        config={"confirmation_string": "abc123"},
    )


def test_confirmation_returns_account_string(monkeypatch):
    client, _ = _client(monkeypatch, acct=_fake_acct())
    r = client.post("/hooks/vk/s3cr3t", json={"type": "confirmation", "group_id": 222})
    assert r.status_code == 200
    assert r.text == "abc123"


def test_confirmation_falls_back_to_default(monkeypatch):
    client, _ = _client(monkeypatch, acct=None, default_conf="PLATFORM_CONF")
    r = client.post("/hooks/vk/unknown", json={"type": "confirmation", "group_id": 1})
    assert r.text == "PLATFORM_CONF"


def _message_new(secret: str) -> dict:
    return {
        "type": "message_new",
        "secret": secret,
        "object": {"message": {"from_id": 1, "peer_id": 1, "text": "hi", "date": 1}},
    }


def test_message_new_good_secret_enqueues(monkeypatch):
    client, enqueued = _client(monkeypatch, acct=_fake_acct(), creds={"secret": "S1"})
    r = client.post("/hooks/vk/s3cr3t", json=_message_new("S1"))
    assert r.status_code == 200
    assert r.text == "ok"
    assert len(enqueued) == 1


def test_message_new_bad_secret_dropped(monkeypatch):
    client, enqueued = _client(monkeypatch, acct=_fake_acct(), creds={"secret": "S1"})
    r = client.post("/hooks/vk/s3cr3t", json=_message_new("WRONG"))
    assert r.status_code == 200
    assert r.text == "ok"  # VK always gets "ok"
    assert enqueued == []  # but the event is dropped


def test_unmatched_account_returns_ok(monkeypatch):
    client, enqueued = _client(monkeypatch, acct=None)
    r = client.post("/hooks/vk/nope", json={"type": "message_new", "object": {}})
    assert r.text == "ok"
    assert enqueued == []
