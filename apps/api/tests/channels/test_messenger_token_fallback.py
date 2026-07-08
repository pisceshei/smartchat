"""Messenger adapter token fallback: accounts connected through the modal store
page_access_token; the adapter must use it whenever access_token is absent."""
from __future__ import annotations

import json
import uuid

import httpx
from py_contracts.content import MessageContent, TextBlock

from apps.api.app.channels.adapters.messenger import MessengerAdapter
from apps.api.app.channels.base import AccountRef


def _account() -> AccountRef:
    return AccountRef(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        channel_type="messenger",
        external_id="page_1",
        name="Page",
        config={},
    )


def _capture():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["access_token"] = request.url.params.get("access_token")
        seen["body"] = json.loads(request.content) if request.content else {}
        return httpx.Response(200, json={"message_id": "mid.1", "id": "page_1", "name": "Page"})

    return seen, handler


def _adapter(handler) -> MessengerAdapter:
    return MessengerAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


_TEXT = MessageContent(blocks=[TextBlock(text="hi")])


async def test_send_falls_back_to_page_access_token():
    seen, handler = _capture()
    adapter = _adapter(handler)
    payload = adapter.render(_TEXT)[0]
    res = await adapter.send(_account(), {"page_access_token": "PT_ONLY"}, "PSID", payload)
    assert res.ok
    assert seen["access_token"] == "PT_ONLY"


async def test_send_prefers_access_token_when_both_present():
    seen, handler = _capture()
    adapter = _adapter(handler)
    payload = adapter.render(_TEXT)[0]
    await adapter.send(
        _account(), {"access_token": "AT", "page_access_token": "PT"}, "PSID", payload
    )
    assert seen["access_token"] == "AT"


async def test_check_health_and_mark_read_fall_back():
    seen, handler = _capture()
    adapter = _adapter(handler)
    result = await adapter.check_health(_account(), {"page_access_token": "PT_ONLY"})
    assert result.ok and seen["access_token"] == "PT_ONLY"
    await adapter.mark_read(_account(), {"page_access_token": "PT2"}, to="PSID")
    assert seen["access_token"] == "PT2"


async def test_fetch_profile_falls_back():
    seen, handler = _capture()
    adapter = _adapter(handler)
    await adapter.fetch_profile({"page_access_token": "PT3"}, "PSID")
    assert seen["access_token"] == "PT3"
