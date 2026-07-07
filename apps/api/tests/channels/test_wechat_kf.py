"""WeChat 微信客服 adapter: connect (mocked gettoken), sync_msg page parsing,
render + msgmenu, open_kfid enrichment, kf/send_msg, and the cursor sync loop
(mocked http + fake redis/session). No live qyapi.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
from py_contracts.content import (
    MediaBlock,
    MessageContent,
    QuickButton,
    QuickButtonsBlock,
    TextBlock,
)

from apps.api.app.channels.adapters.wechat_kf import WeChatKfAdapter, sync_kf_messages
from apps.api.app.channels.base import MessageIn

CORP = "wwcorp999"
SECRET = "kfsecret"
AES = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"


def _account():
    return SimpleNamespace(external_id=CORP, config={"corp_id": CORP}, name="KF")


def _adapter(handler) -> WeChatKfAdapter:
    return WeChatKfAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# --------------------------------------------------------------------------
# connect_validate
# --------------------------------------------------------------------------
async def test_connect_validate_success_names_from_kf_account():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK", "expires_in": 7200})
        assert request.url.path == "/cgi-bin/kf/account/list"
        return httpx.Response(
            200, json={"errcode": 0, "account_list": [{"open_kfid": "wk1", "name": "門市客服"}]}
        )

    cr = await _adapter(handler).connect_validate(
        {"corp_id": CORP}, {"secret": SECRET, "token": "t", "encoding_aes_key": AES}
    )
    assert cr.health.ok
    assert cr.external_id == CORP
    assert cr.name == "門市客服"
    assert cr.needs_webhook_secret is True


async def test_connect_validate_missing_fields():
    cr = await _adapter(lambda r: httpx.Response(200, json={})).connect_validate({"corp_id": CORP}, {})
    assert not cr.health.ok and "missing" in cr.health.detail["error"]


async def test_connect_validate_bad_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 40001, "errmsg": "invalid credential"})

    cr = await _adapter(handler).connect_validate(
        {"corp_id": CORP}, {"secret": "bad", "token": "t", "encoding_aes_key": AES}
    )
    assert not cr.health.ok and cr.health.detail["errcode"] == 40001


# --------------------------------------------------------------------------
# parse_sync_messages (inbound)
# --------------------------------------------------------------------------
def _msg(**kw):
    base = {"open_kfid": "wk1", "external_userid": "wmABC", "send_time": 1615478585, "origin": 3}
    base.update(kw)
    return base


def test_parse_text_captures_open_kfid_in_meta():
    page = [_msg(msgid="M1", msgtype="text", text={"content": "你好"})]
    events = WeChatKfAdapter().parse_inbound({"msg_list": page})
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, MessageIn)
    assert ev.external_user_id == "wmABC"
    assert ev.external_message_id == "M1"
    assert ev.content.blocks[0].text == "你好"
    assert ev.profile.meta["open_kfid"] == "wk1"
    assert ev.external_timestamp is not None


def test_parse_skips_non_customer_origin():
    page = [
        _msg(msgid="A", msgtype="text", text={"content": "servicer echo"}, origin=5),
        _msg(msgid="B", msgtype="event", origin=4),
        _msg(msgid="C", msgtype="text", text={"content": "real"}, origin=3),
    ]
    events = WeChatKfAdapter().parse_inbound({"msg_list": page})
    assert [e.external_message_id for e in events] == ["C"]


def test_parse_image_media_ref():
    page = [_msg(msgid="M2", msgtype="image", image={"media_id": "IMG9"})]
    ev = WeChatKfAdapter().parse_inbound({"msg_list": page})[0]
    assert ev.content.blocks[0].media_type == "image"
    assert ev.media_refs[0].ref == {"kind": "wechat_kf_media", "media_id": "IMG9"}


def test_parse_voice_and_file_media():
    page = [
        _msg(msgid="V", msgtype="voice", voice={"media_id": "VC"}),
        _msg(msgid="F", msgtype="file", file={"media_id": "FL"}),
    ]
    evs = WeChatKfAdapter().parse_inbound({"msg_list": page})
    assert {e.content.blocks[0].media_type for e in evs} == {"voice", "file"}


def test_parse_link_becomes_text():
    page = [_msg(msgid="L", msgtype="link", link={"title": "Deal", "url": "https://x"})]
    ev = WeChatKfAdapter().parse_inbound({"msg_list": page})[0]
    assert "Deal" in ev.content.blocks[0].text and "https://x" in ev.content.blocks[0].text


def test_parse_empty_list():
    assert WeChatKfAdapter().parse_inbound({"msg_list": []}) == []
    assert WeChatKfAdapter().parse_inbound({}) == []


# --------------------------------------------------------------------------
# render + msgmenu
# --------------------------------------------------------------------------
def test_render_text():
    out = WeChatKfAdapter().render(MessageContent(blocks=[TextBlock(text="hi")]))
    assert out == [{"msgtype": "text", "text": {"content": "hi"}}]


def test_render_media_marks_upload():
    content = MessageContent(
        blocks=[MediaBlock(media_type="voice", file_id="00000000-0000-0000-0000-000000000002")]
    )
    out = WeChatKfAdapter().render(content)
    assert out[0]["msgtype"] == "voice" and out[0]["_upload"]["media_type"] == "voice"


def test_render_quick_buttons_as_msgmenu():
    block = QuickButtonsBlock(
        text="Choose", buttons=[QuickButton(id="p1", text="Plan A"), QuickButton(id="p2", text="Plan B")]
    )
    out = WeChatKfAdapter().render(MessageContent(blocks=[block]))
    assert out[0]["msgtype"] == "msgmenu"
    menu = out[0]["msgmenu"]
    assert menu["head_content"] == "Choose"
    assert menu["list"][0] == {"type": "click", "click": {"id": "p1", "content": "Plan A"}}


# --------------------------------------------------------------------------
# enrich_outbound injects open_kfid
# --------------------------------------------------------------------------
async def test_enrich_outbound_injects_open_kfid():
    identity = SimpleNamespace(meta={"open_kfid": "wk1"})
    payloads = [{"msgtype": "text", "text": {"content": "hi"}}]
    out = await WeChatKfAdapter().enrich_outbound(
        None, account=_account(), credentials={}, conversation=None, identity=identity, payloads=payloads
    )
    assert out[0]["open_kfid"] == "wk1"


async def test_enrich_outbound_no_meta_is_noop():
    identity = SimpleNamespace(meta={})
    payloads = [{"msgtype": "text", "text": {"content": "hi"}}]
    out = await WeChatKfAdapter().enrich_outbound(
        None, account=_account(), credentials={}, conversation=None, identity=identity, payloads=payloads
    )
    assert "open_kfid" not in out[0]


# --------------------------------------------------------------------------
# send
# --------------------------------------------------------------------------
async def test_send_requires_open_kfid():
    res = await WeChatKfAdapter().send(_account(), {"secret": SECRET}, "wmABC", {"msgtype": "text"})
    assert not res.ok and res.error_code == "PERMANENT"


async def test_send_text_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK"})
        assert request.url.path == "/cgi-bin/kf/send_msg"
        body = json.loads(request.read())
        assert body["touser"] == "wmABC" and body["open_kfid"] == "wk1"
        assert body["msgtype"] == "text"
        return httpx.Response(200, json={"errcode": 0, "msgid": "KFMSG1"})

    payload = {"msgtype": "text", "text": {"content": "hi"}, "open_kfid": "wk1"}
    res = await _adapter(handler).send(_account(), {"secret": SECRET}, "wmABC", payload)
    assert res.ok and res.external_message_id == "KFMSG1"


async def test_send_media_uploads_then_sends():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK"})
        if "/api/v1/files/" in path:
            return httpx.Response(200, content=b"voicebytes")
        if path == "/cgi-bin/media/upload":
            assert request.url.params["type"] == "voice"
            return httpx.Response(200, json={"errcode": 0, "media_id": "MID_V"})
        if path == "/cgi-bin/kf/send_msg":
            body = json.loads(request.read())
            assert body["voice"] == {"media_id": "MID_V"}
            return httpx.Response(200, json={"errcode": 0, "msgid": "KM2"})
        return httpx.Response(404)

    payload = {
        "msgtype": "voice",
        "open_kfid": "wk1",
        "_upload": {
            "media_type": "voice",
            "url": "http://localhost:8000/api/v1/files/z",
            "filename": "v.amr",
            "mime": "audio/amr",
        },
    }
    res = await _adapter(handler).send(_account(), {"secret": SECRET}, "wmABC", payload)
    assert res.ok and res.external_message_id == "KM2"


# --------------------------------------------------------------------------
# sync_kf_messages cursor loop (fake redis + fake session)
# --------------------------------------------------------------------------
class _FakeRedis:
    def __init__(self):
        self.xadds: list[tuple[str, dict]] = []

    async def set(self, key, val, nx=False, ex=None):
        return True

    async def delete(self, key):
        return 1

    async def xadd(self, name, fields, maxlen=None, approximate=True):
        self.xadds.append((name, fields))
        return b"1-0"


class _FakeSession:
    def __init__(self, acct):
        self._acct = acct

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, model, pk):
        return self._acct

    def begin(self):
        return _FakeBegin()


class _FakeBegin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_sync_kf_messages_paginates_and_enqueues(monkeypatch):
    account_id = uuid.uuid4()
    acct = SimpleNamespace(
        id=account_id, enabled=True, channel_type="wechat_kf",
        config={"corp_id": CORP}, workspace_id=uuid.uuid4(), credentials_enc=None,
    )

    # stub credential decryption + ORM flag_modified (no real DB)
    async def _fake_creds(session, account):
        return {"secret": SECRET}

    monkeypatch.setattr("apps.api.app.channels.creds.get_credentials", _fake_creds)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a, **k: None)

    pages = [
        {"errcode": 0, "has_more": 1, "next_cursor": "C1",
         "msg_list": [_msg(msgid="M1", msgtype="text", text={"content": "one"})]},
        {"errcode": 0, "has_more": 0, "next_cursor": "C2",
         "msg_list": [_msg(msgid="M2", msgtype="text", text={"content": "two"})]},
    ]
    call = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cgi-bin/gettoken":
            return httpx.Response(200, json={"errcode": 0, "access_token": "TOK"})
        assert request.url.path == "/cgi-bin/kf/sync_msg"
        page = pages[call["i"]]
        call["i"] += 1
        return httpx.Response(200, json=page)

    adapter = _adapter(handler)
    redis = _FakeRedis()

    def factory():
        return _FakeSession(acct)

    pulled = await sync_kf_messages(factory, redis, account_id, token="synctok", adapter=adapter)
    assert pulled == 2
    # two ingress pages enqueued onto ingress:wechat_kf
    assert len(redis.xadds) == 2
    assert all(name == "ingress:wechat_kf" for name, _ in redis.xadds)
    first_payload = json.loads(redis.xadds[0][1]["payload"])
    assert first_payload["msg_list"][0]["msgid"] == "M1"
    # cursor advanced to the last page's next_cursor
    assert acct.config["kf_cursor"] == "C2"


async def test_sync_kf_messages_lock_busy_skips(monkeypatch):
    class _BusyRedis(_FakeRedis):
        async def set(self, key, val, nx=False, ex=None):
            return False  # lock held by a concurrent callback

    redis = _BusyRedis()

    def factory():
        raise AssertionError("must not touch the DB when the lock is busy")

    adapter = _adapter(lambda r: httpx.Response(200))
    pulled = await sync_kf_messages(factory, redis, uuid.uuid4(), adapter=adapter)
    assert pulled == 0
    assert redis.xadds == []
