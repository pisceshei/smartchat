"""BridgeClient contract tests — pure, mocked httpx transport (no network).

Verifies the client speaks the BRIDGE HTTP CONTRACT exactly (paths, method,
auth header, request/response bodies) and classifies failures into BridgeError
(disabled vs unreachable vs HTTP error) so provisioning can degrade gracefully.
"""
from __future__ import annotations

import json

import httpx
import pytest

from apps.api.app.services.bridge_client import (
    BridgeClient,
    BridgeError,
    get_bridge_client,
)

BASE = "http://bridge-wa:8100"
TOKEN = "shared-bridge-token"


def _client(handler) -> BridgeClient:
    transport = httpx.MockTransport(handler)
    return BridgeClient(BASE, TOKEN, http=httpx.AsyncClient(transport=transport))


async def test_create_device_sends_contract_body_and_auth() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["method"] = req.method
        seen["auth"] = req.headers.get("X-Bridge-Auth")
        seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"device_id": "d1", "status": "awaiting_qr"})

    client = _client(handler)
    res = await client.create_device(
        "d1", callback_url="https://api/hooks/bridge/sec", callback_secret="sec"
    )
    assert res == {"device_id": "d1", "status": "awaiting_qr"}
    assert seen["url"] == f"{BASE}/devices"
    assert seen["method"] == "POST"
    assert seen["auth"] == TOKEN
    assert seen["body"] == {
        "device_id": "d1",
        "callback_url": "https://api/hooks/bridge/sec",
        "callback_secret": "sec",
    }


async def test_get_qr_and_health() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/qr"):
            return httpx.Response(200, json={"qr": "2@abc", "status": "awaiting_qr"})
        return httpx.Response(
            200, json={"status": "online", "jid": "5511@s.whatsapp.net", "phone": "+5511", "pushname": "Joe"}
        )

    client = _client(handler)
    qr = await client.get_qr("d1")
    assert qr == {"qr": "2@abc", "status": "awaiting_qr"}
    health = await client.get_health("d1")
    assert health["status"] == "online"
    assert health["phone"] == "+5511" and health["pushname"] == "Joe"


async def test_send_posts_to_device_path_with_auth() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("X-Bridge-Auth")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "message_id": "m1"})

    client = _client(handler)
    payload = {"blocks": [{"kind": "text", "text": "hi"}]}
    res = await client.send("d1", to="+5511", payload=payload)
    assert res == {"ok": True, "message_id": "m1"}
    assert seen["url"] == f"{BASE}/devices/d1/send"
    assert seen["auth"] == TOKEN
    assert seen["body"] == {"to": "+5511", "payload": payload}


async def test_logout_and_delete() -> None:
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    assert (await client.logout("d1")) == {"ok": True}
    assert (await client.delete_device("d1")) == {"ok": True}
    assert seen == [("POST", "/devices/d1/logout"), ("DELETE", "/devices/d1")]


async def test_http_error_becomes_bridge_error_with_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client(handler)
    with pytest.raises(BridgeError) as ei:
        await client.get_health("d1")
    assert ei.value.status == 500
    assert ei.value.disabled is False
    assert "500" in ei.value.message


async def test_network_error_becomes_bridge_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _client(handler)
    with pytest.raises(BridgeError) as ei:
        await client.create_device("d1", callback_url="u", callback_secret="s")
    assert ei.value.disabled is False
    assert "unreachable" in ei.value.message


async def test_disabled_client_raises_without_calling() -> None:
    client = BridgeClient("", TOKEN)  # no URL configured
    assert client.enabled is False
    for coro in (
        client.create_device("d1", callback_url="u", callback_secret="s"),
        client.get_qr("d1"),
        client.get_health("d1"),
        client.send("d1", to="x", payload={}),
        client.logout("d1"),
        client.delete_device("d1"),
    ):
        with pytest.raises(BridgeError) as ei:
            await coro
        assert ei.value.disabled is True


def test_get_bridge_client_reads_settings(monkeypatch) -> None:
    from apps.api.app import settings as settings_mod

    s = settings_mod.get_settings()
    monkeypatch.setattr(s, "bridge_wa_url", "http://x:9000/", raising=False)
    monkeypatch.setattr(s, "bridge_api_token", "tok", raising=False)
    client = get_bridge_client(s)
    assert client.base_url == "http://x:9000"  # trailing slash trimmed
    assert client.token == "tok"
    assert client.enabled is True
