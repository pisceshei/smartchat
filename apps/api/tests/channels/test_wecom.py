"""WeCom 企業微信 adapter: connect (mocked gettoken), inbound XML parse,
outbound render + degradation, send + media upload, error classification.
All mocked/pure — no live qyapi.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
from py_contracts.content import (
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButton,
    QuickButtonsBlock,
    TextBlock,
)

from apps.api.app.channels.adapters.wecom import WeComAdapter, xml_to_dict
from apps.api.app.channels.base import DeliveryStatus, MessageIn, OptOut

CORP = "wwcorp123"
SECRET = "sekret"
AES = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"


def _account():
    return SimpleNamespace(
        external_id=f"{CORP}:1000002",
        config={"corp_id": CORP, "agent_id": "1000002"},
        name="WeCom",
    )


def _adapter(handler) -> WeComAdapter:
    return WeComAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --------------------------------------------------------------------------
# connect_validate
# --------------------------------------------------------------------------
async def test_connect_validate_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/gettoken"
        assert request.url.params["corpid"] == CORP
        return httpx.Response(200, json={"errcode": 0, "access_token": "TOK", "expires_in": 7200})

    adapter = _adapter(handler)
    cr = await adapter.connect_validate(
        {"corp_id": CORP, "agent_id": "1000002", "name": "Support"},
        {"secret": SECRET, "token": "tok", "encoding_aes_key": AES},
    )
    assert cr.health.ok
    assert cr.external_id == f"{CORP}:1000002"
    assert cr.name == "Support"
    assert cr.needs_webhook_secret is True


async def test_connect_validate_missing_fields():
    adapter = _adapter(lambda r: httpx.Response(200, json={}))
    cr = await adapter.connect_validate({"corp_id": CORP}, {})
    assert not cr.health.ok
    assert "missing" in cr.health.detail["error"]


async def test_connect_validate_bad_secret_surfaces_errcode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 40001, "errmsg": "invalid credential"})

    adapter = _adapter(handler)
    cr = await adapter.connect_validate(
        {"corp_id": CORP, "agent_id": "1"},
        {"secret": "bad", "token": "t", "encoding_aes_key": AES},
    )
    assert not cr.health.ok
    assert cr.health.detail["errcode"] == 40001


async def test_connect_validate_bad_aes_key():
    adapter = _adapter(lambda r: httpx.Response(200, json={"errcode": 0, "access_token": "T"}))
    cr = await adapter.connect_validate(
        {"corp_id": CORP, "agent_id": "1"},
        {"secret": SECRET, "token": "t", "encoding_aes_key": "short"},
    )
    assert not cr.health.ok


# --------------------------------------------------------------------------
# inbound XML parsing
# --------------------------------------------------------------------------
def test_xml_to_dict_unwraps_cdata():
    d = xml_to_dict(
        "<xml><ToUserName><![CDATA[corp]]></ToUserName><Content><![CDATA[hi]]></Content></xml>"
    )
    assert d["ToUserName"] == "corp"
    assert d["Content"] == "hi"


def test_parse_text_message():
    xml = (
        "<xml><ToUserName><![CDATA[wwcorp123]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1348831860</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[hello wecom]]></Content>"
        "<MsgId>1234567890123456</MsgId><AgentID>1</AgentID></xml>"
    )
    events = WeComAdapter().parse_inbound({"xml": xml})
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, MessageIn)
    assert ev.external_user_id == "zhangsan"
    assert ev.external_message_id == "1234567890123456"
    assert ev.content.blocks[0].text == "hello wecom"
    assert ev.external_timestamp is not None


def test_parse_image_message_yields_media_ref():
    xml = (
        "<xml><FromUserName><![CDATA[u1]]></FromUserName>"
        "<CreateTime>1348831860</CreateTime><MsgType><![CDATA[image]]></MsgType>"
        "<PicUrl><![CDATA[http://p]]></PicUrl><MediaId><![CDATA[MEDIA_9]]></MediaId>"
        "<MsgId>99</MsgId></xml>"
    )
    ev = WeComAdapter().parse_inbound({"xml": xml})[0]
    assert isinstance(ev, MessageIn)
    assert ev.content.blocks[0].media_type == "image"
    assert ev.media_refs[0].ref == {"kind": "wecom_media", "media_id": "MEDIA_9"}


def test_parse_unsubscribe_event_is_optout():
    xml = (
        "<xml><FromUserName><![CDATA[u2]]></FromUserName>"
        "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[unsubscribe]]></Event></xml>"
    )
    ev = WeComAdapter().parse_inbound({"xml": xml})[0]
    assert isinstance(ev, OptOut)
    assert ev.external_user_id == "u2"


def test_parse_other_event_ignored():
    xml = (
        "<xml><FromUserName><![CDATA[u3]]></FromUserName>"
        "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[subscribe]]></Event></xml>"
    )
    assert WeComAdapter().parse_inbound({"xml": xml}) == []


def test_parse_empty_payload():
    assert WeComAdapter().parse_inbound({}) == []


def test_no_deliverystatus_events_from_wecom():
    # sanity: wecom parse never emits DeliveryStatus (no such callback)
    ev = WeComAdapter().parse_inbound(
        {"xml": "<xml><FromUserName><![CDATA[u]]></FromUserName><MsgType><![CDATA[text]]></MsgType>"
                "<Content><![CDATA[x]]></Content><MsgId>1</MsgId></xml>"}
    )
    assert not any(isinstance(e, DeliveryStatus) for e in ev)


# --------------------------------------------------------------------------
# outbound render + degradation
# --------------------------------------------------------------------------
def test_render_text():
    out = WeComAdapter().render(MessageContent(blocks=[TextBlock(text="hi")]))
    assert out == [{"msgtype": "text", "text": {"content": "hi"}}]


def test_render_media_marks_upload():
    content = MessageContent(
        blocks=[MediaBlock(media_type="image", file_id="00000000-0000-0000-0000-000000000001")]
    )
    out = WeComAdapter().render(content)
    assert out[0]["msgtype"] == "image"
    assert "_upload" in out[0] and out[0]["_upload"]["media_type"] == "image"


def test_render_product_card_as_news():
    card = ProductCardBlock(
        title="Widget", subtitle="Nice", price="99", currency="HKD", url="https://shop/x"
    )
    out = WeComAdapter().render(MessageContent(blocks=[card]))
    assert out[0]["msgtype"] == "news"
    article = out[0]["news"]["articles"][0]
    assert article["title"] == "Widget" and article["url"] == "https://shop/x"


def test_render_quick_buttons_degrade_to_menu_text():
    # wecom has no interactive buttons → degrade_content converts to a numbered menu
    block = QuickButtonsBlock(
        text="Pick one", buttons=[QuickButton(id="a", text="Apple"), QuickButton(id="b", text="Pear")]
    )
    out = WeComAdapter().render(MessageContent(blocks=[block]))
    assert out[0]["msgtype"] == "text"
    assert "1. Apple" in out[0]["text"]["content"]


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------
async def test_send_text_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK", "expires_in": 7200})
        assert request.url.path == "/cgi-bin/message/send"
        body = request.read().decode()
        assert '"touser":"zhangsan"' in body and '"agentid":1000002' in body
        return httpx.Response(200, json={"errcode": 0, "msgid": "MSG42"})

    adapter = _adapter(handler)
    res = await adapter.send(
        _account(), {"secret": SECRET}, "zhangsan", {"msgtype": "text", "text": {"content": "hi"}}
    )
    assert res.ok and res.external_message_id == "MSG42"


async def test_send_token_stale_is_retryable_and_clears_cache():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            calls["n"] += 1
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 42001, "errmsg": "access_token expired"})

    adapter = _adapter(handler)
    text = {"msgtype": "text", "text": {"content": "x"}}
    res = await adapter.send(_account(), {"secret": SECRET}, "u", text)
    assert not res.ok and res.error_code == "RETRYABLE"
    # a subsequent send re-fetches the token (cache was invalidated)
    await adapter.send(_account(), {"secret": SECRET}, "u", text)
    assert calls["n"] == 2


async def test_send_invalid_recipient():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK"})
        return httpx.Response(200, json={"errcode": 40003, "errmsg": "invalid userid"})

    res = await _adapter(handler).send(
        _account(), {"secret": SECRET}, "ghost", {"msgtype": "text", "text": {"content": "x"}}
    )
    assert res.error_code == "INVALID_RECIPIENT"


async def test_send_media_uploads_then_sends():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK"})
        if "/api/v1/files/" in path:  # the render-time public URL for the bytes
            return httpx.Response(200, content=b"\xff\xd8imagebytes")
        if path == "/cgi-bin/media/upload":
            assert request.url.params["type"] == "image"
            return httpx.Response(200, json={"errcode": 0, "type": "image", "media_id": "MID77"})
        if path == "/cgi-bin/message/send":
            body = request.read().decode()
            assert '"media_id":"MID77"' in body
            return httpx.Response(200, json={"errcode": 0, "msgid": "M1"})
        return httpx.Response(404)

    adapter = _adapter(handler)
    payload = {
        "msgtype": "image",
        "_upload": {
            "media_type": "image",
            "url": "http://localhost:8000/api/v1/files/abc",
            "filename": "pic.jpg",
            "mime": "image/jpeg",
        },
    }
    res = await adapter.send(_account(), {"secret": SECRET}, "u", payload)
    assert res.ok and res.external_message_id == "M1"
    assert "/cgi-bin/media/upload" in seen and "/cgi-bin/message/send" in seen


# --------------------------------------------------------------------------
# fetch_media
# --------------------------------------------------------------------------
async def test_fetch_media_downloads_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK"})
        assert request.url.path == "/cgi-bin/media/get"
        return httpx.Response(200, content=b"voicebytes", headers={"content-type": "audio/amr"})

    got = await _adapter(handler).fetch_media(
        _account(), {"secret": SECRET}, {"kind": "wecom_media", "media_id": "M1"}
    )
    assert got is not None and got.data == b"voicebytes" and got.mime == "audio/amr"


async def test_fetch_media_json_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK"})
        return httpx.Response(
            200, json={"errcode": 40007, "errmsg": "invalid media_id"},
            headers={"content-type": "application/json"},
        )

    got = await _adapter(handler).fetch_media(
        _account(), {"secret": SECRET}, {"kind": "wecom_media", "media_id": "bad"}
    )
    assert got is None
