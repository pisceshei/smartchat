"""WhatsApp BSP proxy adapter (YCloud) — pure parse/render + mocked transport.

No real network: httpx.MockTransport records/answers every request. Covers the
YCloud webhook→canonical mapping, render inheritance, the send envelope + error
classification, connect-time number discovery, and the documented BSP stubs.
"""
from __future__ import annotations

import json
import uuid

import httpx
from py_contracts.content import MessageContent, QuickButton, QuickButtonsBlock, TextBlock

from apps.api.app.channels.adapters.whatsapp_bsp import WhatsAppBspAdapter
from apps.api.app.channels.base import (
    AccountRef,
    DeliveryStatus,
    MessageIn,
    SendResult,
    capabilities_for,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _account(external_id: str = "+85251234567", **config) -> AccountRef:
    cfg = {"bsp": "ycloud", **config}
    return AccountRef(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        channel_type="whatsapp_bsp",
        external_id=external_id,
        name="Acme HK",
        config=cfg,
    )


def _adapter_with(handler) -> WhatsAppBspAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WhatsAppBspAdapter(http=client)


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


# --------------------------------------------------------------------------
# capabilities + render inheritance
# --------------------------------------------------------------------------
def test_capabilities_mirror_whatsapp_cloud():
    caps = WhatsAppBspAdapter().capabilities
    assert caps == capabilities_for("whatsapp_cloud")
    assert caps.templates and caps.session_window_hours == 24
    assert caps.buttons and "image" in caps.media_types


def test_render_is_inherited_from_cloud():
    adapter = WhatsAppBspAdapter()
    # text → WA text object with preview_url
    out = adapter.render(MessageContent(blocks=[TextBlock(text="hello")]))
    assert out == [{"type": "text", "text": {"body": "hello", "preview_url": True}}]
    # quick buttons (≤3) → interactive button object (same shape as Cloud API)
    qb = QuickButtonsBlock(
        text="Pick", buttons=[QuickButton(id="a", text="A"), QuickButton(id="b", text="B")]
    )
    out2 = adapter.render(MessageContent(blocks=[qb]))
    assert out2[0]["type"] == "interactive"
    assert out2[0]["interactive"]["type"] == "button"
    ids = [b["reply"]["id"] for b in out2[0]["interactive"]["action"]["buttons"]]
    assert ids == ["a", "b"]


# --------------------------------------------------------------------------
# parse_inbound — YCloud webhook envelope
# --------------------------------------------------------------------------
def _inbound_text_event() -> dict:
    return {
        "id": "evt_1",
        "type": "whatsapp.inbound_message.received",
        "apiVersion": "v2",
        "createTime": "2026-02-22T12:00:00.000Z",
        "whatsappInboundMessage": {
            "id": "63f872f6741c165b4342a751",
            "wamid": "wamid.HBgNODhIN",
            "wabaId": "WABA-1",
            "from": "85298765432",
            "to": "85251234567",
            "sendTime": "2026-02-22T12:00:00.000Z",
            "type": "text",
            "text": {"body": "hi there"},
            "customerProfile": {"name": "Joe", "username": "@joe"},
        },
    }


def test_parse_inbound_text():
    adapter = WhatsAppBspAdapter()
    events = adapter.parse_inbound(_inbound_text_event())
    assert len(events) == 1
    m = events[0]
    assert isinstance(m, MessageIn)
    assert m.external_message_id == "wamid.HBgNODhIN"
    assert m.external_user_id == "85298765432"
    assert m.content.blocks[0].text == "hi there"
    assert m.profile.display_name == "Joe"
    assert m.profile.phone == "+85298765432"
    assert m.external_timestamp is not None and m.external_timestamp.year == 2026


def test_parse_inbound_image_with_link_makes_url_media_ref():
    adapter = WhatsAppBspAdapter()
    ev = {
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": {
            "wamid": "wamid.IMG",
            "from": "85298765432",
            "type": "image",
            "image": {
                "id": "media_1",
                "link": "https://cdn.ycloud.com/media_1.jpg",
                "caption": "look",
                "mime_type": "image/jpeg",
            },
        },
    }
    m = adapter.parse_inbound(ev)[0]
    assert m.content.blocks[0].media_type == "image"
    assert m.content.blocks[0].caption == "look"
    assert len(m.media_refs) == 1
    ref = m.media_refs[0]
    assert ref.block_index == 0
    assert ref.ref == {
        "kind": "url",
        "url": "https://cdn.ycloud.com/media_1.jpg",
        "filename": None,
        "mime": "image/jpeg",
    }


def test_parse_inbound_interactive_button_reply():
    adapter = WhatsAppBspAdapter()
    ev = {
        "type": "whatsapp.inbound_message.received",
        "whatsappInboundMessage": {
            "wamid": "wamid.BTN",
            "from": "85298765432",
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": "opt_1", "title": "Yes"}},
        },
    }
    m = adapter.parse_inbound(ev)[0]
    blk = m.content.blocks[0]
    assert blk.payload == "opt_1" and blk.text == "Yes"


def test_parse_inbound_status_delivered():
    adapter = WhatsAppBspAdapter()
    ev = {
        "type": "whatsapp.message.updated",
        "whatsappMessage": {
            "id": "yc_1",
            "wamid": "wamid.OUT1",
            "status": "delivered",
            "recipientUserId": "85298765432",
            "deliverTime": "2026-02-22T12:01:00.000Z",
        },
    }
    d = adapter.parse_inbound(ev)[0]
    assert isinstance(d, DeliveryStatus)
    assert d.status == "delivered"
    assert d.external_message_id == "wamid.OUT1"
    assert d.external_user_id == "85298765432"
    assert d.error_code is None


def test_parse_inbound_status_failed_carries_wa_error():
    adapter = WhatsAppBspAdapter()
    ev = {
        "type": "whatsapp.message.updated",
        "whatsappMessage": {
            "wamid": "wamid.OUT2",
            "status": "failed",
            "errorMessage": "Re-engagement message",
            "whatsappApiError": {"code": 131047, "message": "Re-engagement message"},
        },
    }
    d = adapter.parse_inbound(ev)[0]
    assert d.status == "failed"
    assert d.error_code == "131047"
    assert d.error_message == "Re-engagement message"


def test_parse_inbound_accepted_maps_to_sent():
    adapter = WhatsAppBspAdapter()
    ev = {"type": "whatsapp.message.updated", "whatsappMessage": {"wamid": "w.A", "status": "accepted"}}
    d = adapter.parse_inbound(ev)[0]
    assert d.status == "sent"


def test_parse_inbound_batch_and_unknown():
    adapter = WhatsAppBspAdapter()
    batch = {
        "events": [
            _inbound_text_event(),
            {"type": "whatsapp.message.updated", "whatsappMessage": {"wamid": "w.B", "status": "read"}},
        ]
    }
    evs = adapter.parse_inbound(batch)
    assert len(evs) == 2
    assert isinstance(evs[0], MessageIn) and isinstance(evs[1], DeliveryStatus)
    # unrecognised payload (e.g. a non-YCloud BSP) yields nothing
    assert adapter.parse_inbound({"foo": "bar"}) == []


# --------------------------------------------------------------------------
# send — YCloud envelope + error classification
# --------------------------------------------------------------------------
async def test_ycloud_send_shape_and_wamid():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["api_key"] = request.headers.get("X-API-Key")
        seen["body"] = json.loads(request.content)
        return _json_response({"id": "yc_1", "wamid": "wamid.SENT", "status": "accepted"})

    adapter = _adapter_with(handler)
    payload = {"type": "text", "text": {"body": "hi", "preview_url": True}}
    res = await adapter.send(_account(), {"api_key": "sk_live_1"}, "85298765432", payload)
    assert isinstance(res, SendResult) and res.ok
    assert res.external_message_id == "wamid.SENT"
    assert seen["url"] == "https://api.ycloud.com/v2/whatsapp/messages"
    assert seen["method"] == "POST"
    assert seen["api_key"] == "sk_live_1"
    assert seen["body"] == {
        "from": "+85251234567",
        "to": "85298765432",
        "type": "text",
        "text": {"body": "hi", "preview_url": True},
    }


async def test_ycloud_send_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"code": "40100", "message": "invalid api key"}, status=401)

    adapter = _adapter_with(handler)
    text = {"type": "text", "text": {"body": "x"}}
    res = await adapter.send(_account(), {"api_key": "bad"}, "85298765432", text)
    assert not res.ok and res.error_code == "AUTH"


async def test_ycloud_send_window_expired_from_wa_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"error": {"whatsappApiError": {"code": 131047, "message": "re-engagement"}}},
            status=400,
        )

    adapter = _adapter_with(handler)
    text = {"type": "text", "text": {"body": "x"}}
    res = await adapter.send(_account(), {"api_key": "k"}, "85298765432", text)
    assert not res.ok and res.error_code == "WINDOW_EXPIRED"


# --------------------------------------------------------------------------
# connect_validate — number discovery
# --------------------------------------------------------------------------
async def test_connect_validate_ycloud_lists_numbers():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("X-API-Key")
        return _json_response(
            {
                "items": [
                    {
                        "id": "pn_1",
                        "phoneNumber": "+85251234567",
                        "verifiedName": "Acme HK",
                        "wabaId": "waba_1",
                        "qualityRating": "GREEN",
                    }
                ]
            }
        )

    adapter = _adapter_with(handler)
    cr = await adapter.connect_validate({"bsp": "ycloud"}, {"api_key": "sk_live_1"})
    assert cr.health.ok and cr.health.status == "active"
    assert cr.external_id == "+85251234567"
    assert cr.name == "Acme HK"
    assert cr.config_patch == {"bsp": "ycloud", "waba_id": "waba_1"}
    assert cr.needs_webhook_secret is False
    assert seen["url"].startswith("https://api.ycloud.com/v2/whatsapp/phoneNumbers")
    assert seen["api_key"] == "sk_live_1"


async def test_connect_validate_ycloud_picks_requested_number():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "items": [
                    {"id": "pn_1", "phoneNumber": "+85200000001", "verifiedName": "One"},
                    {"id": "pn_2", "phoneNumber": "+85200000002", "verifiedName": "Two"},
                ]
            }
        )

    adapter = _adapter_with(handler)
    cr = await adapter.connect_validate(
        {"bsp": "ycloud", "phone_number": "+85200000002"}, {"api_key": "k"}
    )
    assert cr.external_id == "+85200000002" and cr.name == "Two"


async def test_connect_validate_missing_api_key():
    adapter = WhatsAppBspAdapter()
    cr = await adapter.connect_validate({"bsp": "ycloud"}, {})
    assert not cr.health.ok
    assert "api_key" in cr.health.detail["error"]


async def test_connect_validate_unknown_bsp():
    adapter = WhatsAppBspAdapter()
    cr = await adapter.connect_validate({"bsp": "twilio"}, {"api_key": "k"})
    assert not cr.health.ok
    assert "unknown BSP" in cr.health.detail["error"]


async def test_connect_validate_no_numbers():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"items": []})

    adapter = _adapter_with(handler)
    cr = await adapter.connect_validate({"bsp": "ycloud"}, {"api_key": "k"})
    assert not cr.health.ok and "no WhatsApp phone numbers" in cr.health.detail["error"]


# --------------------------------------------------------------------------
# documented stubs (chatapp/nxcloud/itnio)
# --------------------------------------------------------------------------
async def test_stub_bsp_send_is_clear_permanent_error():
    adapter = WhatsAppBspAdapter()  # no http needed — must not make a call
    res = await adapter.send(
        _account(bsp="chatapp"), {"api_key": "k"}, "85298765432", {"type": "text", "text": {"body": "x"}}
    )
    assert not res.ok and res.error_code == "PERMANENT"
    assert "chatapp" in res.error_message and "not implemented" in res.error_message


async def test_stub_bsp_connect_reports_not_implemented():
    adapter = WhatsAppBspAdapter()
    cr = await adapter.connect_validate({"bsp": "nxcloud"}, {"api_key": "k"})
    assert not cr.health.ok and "not implemented" in cr.health.detail["error"]


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------
async def test_check_health_ycloud_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"items": [{"id": "pn_1", "phoneNumber": "+85251234567"}]})

    adapter = _adapter_with(handler)
    hr = await adapter.check_health(_account(), {"api_key": "k"})
    assert hr.ok and hr.status == "active" and hr.detail["bsp"] == "ycloud"


async def test_check_health_ycloud_bad_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "unauthorized"}, status=401)

    adapter = _adapter_with(handler)
    hr = await adapter.check_health(_account(), {"api_key": "bad"})
    assert not hr.ok and hr.status == "token_expired"


async def test_registered_under_whatsapp_bsp():
    from apps.api.app.channels.registry import get_adapter

    adapter = get_adapter("whatsapp_bsp")
    assert adapter.channel_type == "whatsapp_bsp"
