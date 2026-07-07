"""Slack adapter: request-signing vector + replay window, Events API parse
(message / bot echo ignore / file share), Block Kit render, connect_validate,
error classification — plus the url_verification challenge echo at the webhook.

All pure/faked: no Slack API is contacted (recorded payloads + httpx.MockTransport).
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from py_contracts.content import (
    CardButton,
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButton,
    QuickButtonsBlock,
    TextBlock,
)

from apps.api.app.channels.adapters.slack import (
    SlackAdapter,
    slack_signature,
    verify_slack_signature,
)
from apps.api.app.db import get_session
from apps.api.app.modules.hooks import slack as slack_hook

# Slack's own documented worked example (api.slack.com/authentication/verifying-requests).
_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
_TS = "1531420618"
_BODY = (
    b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow"
    b"&channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner"
    b"&command=%2Fwebhook-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2F"
    b"commands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
    b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
_EXPECTED = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
_NOW = 1531420618.0  # == timestamp → inside the window


# --------------------------------------------------------------------------
# 1. signature vector + replay window
# --------------------------------------------------------------------------
def test_signature_known_answer_vector():
    assert slack_signature(_SECRET, _TS, _BODY) == _EXPECTED


def test_verify_ok_inside_window():
    assert verify_slack_signature(
        _SECRET, _BODY, timestamp=_TS, signature=_EXPECTED, now=_NOW
    )


def test_verify_rejects_stale_timestamp():
    # 6 minutes later → outside the 5-minute replay window
    assert not verify_slack_signature(
        _SECRET, _BODY, timestamp=_TS, signature=_EXPECTED, now=_NOW + 361
    )


def test_verify_rejects_tampered_body():
    assert not verify_slack_signature(
        _SECRET, _BODY + b"x", timestamp=_TS, signature=_EXPECTED, now=_NOW
    )


def test_verify_rejects_missing_parts():
    assert not verify_slack_signature(_SECRET, _BODY, timestamp=None, signature=_EXPECTED)
    assert not verify_slack_signature(_SECRET, _BODY, timestamp=_TS, signature=None)
    assert not verify_slack_signature("", _BODY, timestamp=_TS, signature=_EXPECTED)


def test_adapter_verify_webhook_reads_headers():
    adapter = SlackAdapter()
    headers = {"x-slack-request-timestamp": _TS, "x-slack-signature": _EXPECTED}
    # verify_webhook uses wall-clock now(); the documented ts is long past, so
    # a real-time check fails — but a fresh signature over "now" passes.
    assert not adapter.verify_webhook(headers=headers, body=_BODY, secret=_SECRET)


# --------------------------------------------------------------------------
# 2. Events API parse
# --------------------------------------------------------------------------
def test_parse_message_event():
    adapter = SlackAdapter()
    payload = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev0001",
        "event": {
            "type": "message",
            "channel": "D100",
            "user": "U200",
            "text": "hi there",
            "ts": "1700000000.000100",
        },
    }
    events = adapter.parse_inbound(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.external_message_id == "Ev0001"
    assert ev.external_user_id == "D100"  # channel is the reply target
    assert isinstance(ev.content.blocks[0], TextBlock)
    assert ev.content.blocks[0].text == "hi there"
    assert ev.profile.meta["slack_user_id"] == "U200"


def test_parse_ignores_bot_echo():
    adapter = SlackAdapter()
    payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "subtype": "bot_message",
            "bot_id": "B1",
            "channel": "D100",
            "text": "our own reply",
        },
    }
    assert adapter.parse_inbound(payload) == []


def test_parse_ignores_edit_and_delete_subtypes():
    adapter = SlackAdapter()
    for subtype in ("message_changed", "message_deleted", "channel_join"):
        payload = {
            "type": "event_callback",
            "event": {"type": "message", "subtype": subtype, "channel": "D1", "text": "x"},
        }
        assert adapter.parse_inbound(payload) == []


def test_parse_file_share_media():
    adapter = SlackAdapter()
    payload = {
        "type": "event_callback",
        "event_id": "Ev9",
        "event": {
            "type": "message",
            "subtype": "file_share",
            "channel": "D100",
            "user": "U200",
            "ts": "1700000000.000200",
            "files": [
                {
                    "id": "F1",
                    "mimetype": "image/png",
                    "url_private": "https://files.slack.com/f1",
                    "name": "a.png",
                    "size": 123,
                    "title": "pic",
                }
            ],
        },
    }
    events = adapter.parse_inbound(payload)
    assert len(events) == 1
    ev = events[0]
    media = ev.content.blocks[0]
    assert isinstance(media, MediaBlock) and media.media_type == "image"
    assert ev.media_refs[0].ref["kind"] == "slack_file"
    assert ev.media_refs[0].ref["url"] == "https://files.slack.com/f1"


def test_parse_non_event_callback_ignored():
    adapter = SlackAdapter()
    assert adapter.parse_inbound({"type": "url_verification", "challenge": "c"}) == []


# --------------------------------------------------------------------------
# 3. Block Kit render
# --------------------------------------------------------------------------
def test_render_quick_buttons_to_actions_block():
    adapter = SlackAdapter()
    content = MessageContent(
        blocks=[
            QuickButtonsBlock(
                text="Pick one",
                buttons=[QuickButton(id="a", text="Alpha"), QuickButton(id="b", text="Beta")],
            )
        ]
    )
    out = adapter.render(content)
    assert len(out) == 1
    p = out[0]
    assert p["_method"] == "chat.postMessage"
    actions = [b for b in p["blocks"] if b["type"] == "actions"][0]
    assert [e["value"] for e in actions["elements"]] == ["a", "b"]
    assert actions["elements"][0]["action_id"] == "qb_a"


def test_render_product_card_with_link_button():
    adapter = SlackAdapter()
    content = MessageContent(
        blocks=[
            ProductCardBlock(
                title="Widget",
                price="9.99",
                currency="USD",
                image_url="https://img/x.png",
                url="https://shop/x",
                buttons=[CardButton(text="Buy", action="url", value="https://shop/x")],
            )
        ]
    )
    p = adapter.render(content)[0]
    section = p["blocks"][0]
    assert section["type"] == "section"
    assert section["accessory"]["image_url"] == "https://img/x.png"
    btn = p["blocks"][1]["elements"][0]
    assert btn["url"] == "https://shop/x"


def test_render_image_media_as_image_block():
    adapter = SlackAdapter()
    content = MessageContent(
        blocks=[MediaBlock(media_type="image", file_id="00000000-0000-0000-0000-000000000001")]
    )
    p = adapter.render(content)[0]
    assert p["blocks"][0]["type"] == "image"


# --------------------------------------------------------------------------
# 4. connect_validate (auth.test, mocked http)
# --------------------------------------------------------------------------
def _adapter(handler) -> SlackAdapter:
    return SlackAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_connect_validate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth.test")
        assert request.headers["authorization"] == "Bearer xoxb-abc"
        return httpx.Response(
            200, json={"ok": True, "team": "Acme", "team_id": "T123", "user_id": "U9"}
        )

    cr = await _adapter(handler).connect_validate({}, {"bot_token": "xoxb-abc"})
    assert cr.external_id == "T123"
    assert cr.name == "Acme"
    assert cr.health.ok is True
    assert cr.config_patch["bot_user_id"] == "U9"
    assert cr.needs_webhook_secret is False


async def test_connect_validate_rejects_non_bot_token():
    adapter = SlackAdapter()  # no http needed — prefix check short-circuits
    cr = await adapter.connect_validate({}, {"bot_token": "xoxp-user-token"})
    assert cr.health.ok is False
    assert "xoxb" in cr.health.detail["error"]


async def test_connect_validate_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    cr = await _adapter(handler).connect_validate({}, {"bot_token": "xoxb-bad"})
    assert cr.health.ok is False
    assert cr.health.status == "token_expired"
    assert cr.health.detail["error"] == "invalid_auth"


# --------------------------------------------------------------------------
# 5. error classification
# --------------------------------------------------------------------------
def test_classify_error():
    c = SlackAdapter.classify_error
    assert c(429, "ratelimited", {"Retry-After": "7"}) == ("RATE_LIMITED", 7.0)
    assert c(200, "invalid_auth")[0] == "AUTH"
    assert c(200, "channel_not_found")[0] == "INVALID_RECIPIENT"
    assert c(200, "restricted_action")[0] == "BLOCKED"
    assert c(200, "msg_too_long")[0] == "MESSAGE_TOO_LONG"
    assert c(503, "")[0] == "RETRYABLE"
    assert c(200, "something_else")[0] == "PERMANENT"


# --------------------------------------------------------------------------
# 6. webhook url_verification challenge echo (dev: signing secret unset)
# --------------------------------------------------------------------------
def _client(signing_secret: str = "") -> TestClient:
    app = FastAPI()
    app.include_router(slack_hook.router)

    async def _no_session():
        yield None

    app.dependency_overrides[get_session] = _no_session
    slack_hook.get_settings = lambda: SimpleNamespace(slack_signing_secret=signing_secret)  # type: ignore[attr-defined]
    return TestClient(app)


def test_webhook_echoes_challenge_dev_unsigned():
    client = _client(signing_secret="")
    r = client.post("/hooks/slack", json={"type": "url_verification", "challenge": "CH123"})
    assert r.status_code == 200
    assert r.text == "CH123"


def test_webhook_rejects_bad_signature_when_secret_set():
    client = _client(signing_secret=_SECRET)
    r = client.post(
        "/hooks/slack",
        content=b'{"type":"url_verification","challenge":"x"}',
        headers={"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=deadbeef"},
    )
    assert r.status_code == 403
