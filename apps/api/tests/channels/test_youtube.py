"""YouTube comments adapter — comment thread → MessageIn, text-only render
degradation, comments.insert reply shape + error mapping, poll cursor
filtering, Google OAuth refresh, connect. Pure/faked (httpx.MockTransport).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from py_contracts.content import MessageContent, QuickButton, QuickButtonsBlock

from apps.api.app.channels.adapters import youtube as ytmod
from apps.api.app.channels.adapters.youtube import YouTubeAdapter, YouTubePollResult


def _adapter(handler=None) -> YouTubeAdapter:
    if handler is None:
        return YouTubeAdapter()
    return YouTubeAdapter(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _thread(cid: str, text: str, published: str, video: str = "VID1") -> dict:
    return {
        "id": f"thread-{cid}",
        "snippet": {
            "videoId": video,
            "topLevelComment": {
                "id": cid,
                "snippet": {
                    "textOriginal": text,
                    "authorDisplayName": "Alice",
                    "authorProfileImageUrl": "https://a/pic.png",
                    "authorChannelId": {"value": "UCauthor"},
                    "publishedAt": published,
                },
            },
        },
    }


# --------------------------------------------------------------------------
# 1. inbound parse
# --------------------------------------------------------------------------
def test_parse_comment_thread_to_messagein():
    payload = {"items": [_thread("C1", "Great video", "2024-01-01T00:00:00Z")]}
    ev = _adapter().parse_inbound(payload)[0]
    assert ev.external_message_id == "C1"
    # thread top-level comment id doubles as the reply target (parentId)
    assert ev.external_user_id == "C1"
    assert ev.content.blocks[0].text == "Great video"
    assert ev.profile.display_name == "Alice"
    assert ev.profile.avatar_url == "https://a/pic.png"
    assert ev.meta["video_id"] == "VID1"
    assert ev.meta["author_channel_id"] == "UCauthor"
    assert ev.external_timestamp is not None


def test_parse_skips_items_without_top_comment():
    assert _adapter().parse_inbound({"items": [{"id": "t", "snippet": {}}]}) == []


# --------------------------------------------------------------------------
# 2. text-only render degradation
# --------------------------------------------------------------------------
def test_render_degrades_buttons_to_numbered_text():
    buttons = [QuickButton(id="a", text="A"), QuickButton(id="b", text="B")]
    content = MessageContent(blocks=[QuickButtonsBlock(text="Pick", buttons=buttons)])
    payloads = _adapter().render(content)
    assert all(set(p.keys()) == {"text"} for p in payloads)
    joined = "\n".join(p["text"] for p in payloads)
    assert "1. A" in joined and "2. B" in joined


# --------------------------------------------------------------------------
# 3. send (comments.insert reply)
# --------------------------------------------------------------------------
async def test_send_reply_shape():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "REPLY1", "snippet": {}})

    res = await _adapter(handler).send(
        SimpleNamespace(external_id="", config={}), {"oauth_access_token": "TOK"}, "C1", {"text": "thanks!"}
    )
    assert res.ok and res.external_message_id == "REPLY1"
    assert "part=snippet" in captured["url"]
    assert captured["body"] == {"snippet": {"parentId": "C1", "textOriginal": "thanks!"}}
    assert captured["auth"] == "Bearer TOK"


async def test_send_quota_maps_to_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}
        )

    res = await _adapter(handler).send(SimpleNamespace(external_id="", config={}), {}, "C1", {"text": "x"})
    assert not res.ok and res.error_code == "RATE_LIMITED"


def test_classify_error_table():
    assert YouTubeAdapter.classify_error(401, {})[0] == "AUTH"
    assert YouTubeAdapter.classify_error(404, {})[0] == "INVALID_RECIPIENT"
    assert YouTubeAdapter.classify_error(500, {})[0] == "RETRYABLE"


# --------------------------------------------------------------------------
# 4. poll_comments (cursor watermark)
# --------------------------------------------------------------------------
async def test_poll_filters_by_cursor_and_advances_watermark():
    items = [
        _thread("C2", "new", "2024-02-02T00:00:00Z"),   # newer than cursor → keep
        _thread("C1", "old", "2024-01-01T00:00:00Z"),   # older → stop
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "allThreadsRelatedToChannelId=UCchan" in str(request.url)
        return httpx.Response(200, json={"items": items})

    acct = SimpleNamespace(
        config={"channel_id": "UCchan", "youtube_poll_cursor": "2024-01-15T00:00:00Z"}
    )
    res = await _adapter(handler).poll_comments(acct, {"oauth_access_token": "T"})
    assert isinstance(res, YouTubePollResult)
    assert res.count == 1
    assert res.payload["items"][0]["id"] == "thread-C2"
    assert res.cursor == "2024-02-02T00:00:00Z"


async def test_poll_no_channel_returns_empty():
    res = await _adapter().poll_comments(SimpleNamespace(config={}), {})
    assert res.count == 0 and res.payload == {"items": []}


# --------------------------------------------------------------------------
# 5. OAuth refresh (Google)
# --------------------------------------------------------------------------
async def test_refresh_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "grant_type=refresh_token" in request.content.decode()
        return httpx.Response(200, json={"access_token": "NEWTOK", "expires_in": 3600})

    s = ytmod.get_settings()
    monkeypatch.setattr(s, "youtube_oauth_client_id", "cid")
    monkeypatch.setattr(s, "youtube_oauth_client_secret", "csec")
    updated = await _adapter(handler).refresh_credentials(None, {"oauth_refresh_token": "RT"})
    assert updated is not None
    assert updated["oauth_access_token"] == "NEWTOK"
    assert updated["oauth_refresh_token"] == "RT"  # preserved when not rotated
    assert "oauth_token_expires_at" in updated


async def test_refresh_without_client_creds_returns_none(monkeypatch):
    s = ytmod.get_settings()
    monkeypatch.setattr(s, "youtube_oauth_client_id", "")
    monkeypatch.setattr(s, "google_oauth_client_id", "")
    assert await _adapter().refresh_credentials(None, {"oauth_refresh_token": "RT"}) is None


# --------------------------------------------------------------------------
# 6. connect_validate (polled channel → no webhook secret)
# --------------------------------------------------------------------------
async def test_connect_validate_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/channels")
        return httpx.Response(200, json={"items": [{"id": "UCchan", "snippet": {"title": "My Channel"}}]})

    cr = await _adapter(handler).connect_validate({"channel_id": "UCchan"}, {"oauth_access_token": "T"})
    assert cr.external_id == "UCchan"
    assert cr.name == "My Channel"
    assert cr.health.ok
    assert cr.needs_webhook_secret is False  # YouTube is polled, no webhook URL
    assert cr.config_patch["channel_id"] == "UCchan"


async def test_connect_validate_missing_token():
    cr = await _adapter().connect_validate({"channel_id": "X"}, {})
    assert not cr.health.ok
