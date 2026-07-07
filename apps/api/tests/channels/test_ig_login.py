"""Instagram adapter — via-Page (default) vs graph.instagram.com IG-Login send.

No real network: httpx.MockTransport records the outbound request so we can
assert the endpoint host, token and recipient namespace (PSID vs IGSID) selected
by ``account.config.ig_login``. render() is inherited from Messenger and shared
by both paths.
"""
from __future__ import annotations

import json
import uuid

import httpx
from py_contracts.content import MessageContent, TextBlock

from apps.api.app.channels.adapters.instagram import InstagramAdapter
from apps.api.app.channels.base import AccountRef, SendResult


def _account(*, ig_login: bool, external_id: str = "17841400000000001", **config) -> AccountRef:
    cfg = dict(config)
    if ig_login:
        cfg["ig_login"] = True
    return AccountRef(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        channel_type="instagram",
        external_id=external_id,
        name="shop.ig",
        config=cfg,
    )


def _adapter_with(handler) -> InstagramAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return InstagramAdapter(http=client)


def _capture():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        seen["access_token"] = request.url.params.get("access_token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "mid.OUT", "recipient_id": "IGSID_1"})

    return seen, handler


_TEXT = MessageContent(blocks=[TextBlock(text="hi")])


async def test_default_path_is_via_page():
    """No ig_login → inherited Messenger send to graph.facebook.com/me/messages
    with a PSID recipient (unchanged behaviour)."""
    seen, handler = _capture()
    adapter = _adapter_with(handler)
    payload = adapter.render(_TEXT)[0]  # {"message": {"text": "hi"}}
    res = await adapter.send(_account(ig_login=False), {"access_token": "PAGE_TOK"}, "PSID_1", payload)
    assert isinstance(res, SendResult) and res.ok
    assert seen["host"] == "graph.facebook.com"
    assert seen["path"] == "/v21.0/me/messages"
    assert seen["access_token"] == "PAGE_TOK"
    assert seen["body"]["recipient"] == {"id": "PSID_1"}
    assert seen["body"]["message"] == {"text": "hi"}


async def test_ig_login_path_targets_graph_instagram():
    """config.ig_login → graph.instagram.com/v21.0/{ig_id}/messages with the IG
    User token and an IGSID recipient."""
    seen, handler = _capture()
    adapter = _adapter_with(handler)
    payload = adapter.render(_TEXT)[0]
    acct = _account(ig_login=True, external_id="17841400000000001")
    res = await adapter.send(acct, {"ig_access_token": "IG_USER_TOK"}, "IGSID_1", payload)
    assert res.ok and res.external_message_id == "mid.OUT"
    assert seen["host"] == "graph.instagram.com"
    assert seen["path"] == "/v21.0/17841400000000001/messages"
    assert seen["access_token"] == "IG_USER_TOK"
    assert seen["body"]["recipient"] == {"id": "IGSID_1"}
    assert seen["body"]["messaging_type"] == "RESPONSE"


async def test_ig_login_uses_config_ig_id_override():
    seen, handler = _capture()
    adapter = _adapter_with(handler)
    payload = adapter.render(_TEXT)[0]
    acct = _account(ig_login=True, external_id="page_scoped_id", ig_id="17841499999999999")
    await adapter.send(acct, {"access_token": "TOK"}, "IGSID_2", payload)
    assert seen["path"] == "/v21.0/17841499999999999/messages"


async def test_ig_login_message_tag_outside_window():
    seen, handler = _capture()
    adapter = _adapter_with(handler)
    # render with a closed window attaches _tag; the IG-Login path must forward it
    payload = adapter.render(_TEXT, window_open=False)[0]
    assert payload.get("_tag")  # sanity: Messenger render tagged it
    await adapter.send(_account(ig_login=True), {"access_token": "TOK"}, "IGSID_3", payload)
    assert seen["body"]["messaging_type"] == "MESSAGE_TAG"
    assert seen["body"]["tag"] == "HUMAN_AGENT"
    # the transport marker must not leak into the wire body
    assert "_tag" not in seen["body"]


async def test_ig_login_token_precedence_prefers_ig_access_token():
    seen, handler = _capture()
    adapter = _adapter_with(handler)
    payload = adapter.render(_TEXT)[0]
    await adapter.send(
        _account(ig_login=True),
        {"ig_access_token": "IG_TOK", "access_token": "FALLBACK"},
        "IGSID_4",
        payload,
    )
    assert seen["access_token"] == "IG_TOK"


async def test_ig_login_send_error_is_classified():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": 190, "message": "expired token"}})

    adapter = _adapter_with(handler)
    payload = adapter.render(_TEXT)[0]
    res = await adapter.send(_account(ig_login=True), {"access_token": "bad"}, "IGSID_5", payload)
    assert not res.ok and res.error_code == "AUTH"
