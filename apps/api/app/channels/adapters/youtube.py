"""YouTube comments adapter — channel_type "youtube".

YouTube has NO webhook for comments, so this channel is POLLED (there is no
/hooks/youtube route). A scheduled worker beat calls `poll_comments(account,
credentials)`; it queries the Data API v3 commentThreads.list (part=snippet),
returns the top-level comments published since the last cursor, and hands back
the new cursor to persist. The returned payload feeds the normal ingress path
(enqueue_inbound → parse_inbound), so parse_inbound stays PURE.

Conversation model: a top-level comment starts a thread; we key the identity
(external_user_id) on the *top-level comment id* so it doubles as the reply
target (comments.insert parentId). The commenter's name/avatar/channel id ride
along in the ProfileHint. Replies are text-only (comments have no attachments).

Worker schedule (NOT wired here — the sender's cron file owns registration):
    every 1–2 min, for each enabled youtube account:
        creds = get_credentials(...)                       # + refresh if expired
        res = await adapter.poll_comments(account, creds)
        if res.count:
            await enqueue_inbound(redis, account_id=…, channel_type="youtube",
                                  payload=res.payload)
        if res.cursor:  # persist watermark so we don't re-ingest
            acct.config["youtube_poll_cursor"] = res.cursor

Credential fields (connect_validate): oauth_access_token, oauth_refresh_token,
channel_id. Token refresh uses the platform Google/YouTube OAuth app
(settings.youtube_oauth_* → google_oauth_* fallback).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from py_contracts.content import MediaBlock, MessageContent, TextBlock

from ...settings import get_settings
from ..base import (
    BaseAdapter,
    ConnectResult,
    HealthResult,
    InboundEvent,
    MessageIn,
    ProfileHint,
    SendResult,
    degrade_content,
)
from ..media import file_public_url

YT_API = "https://www.googleapis.com/youtube/v3"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


@dataclass
class YouTubePollResult:
    """Outcome of one poll pass: a payload ready for enqueue_inbound +
    parse_inbound, the new cursor watermark to persist, and the item count."""

    payload: dict[str, Any] = field(default_factory=lambda: {"items": []})
    cursor: str | None = None
    count: int = 0


def _access_token(credentials: dict[str, Any]) -> str:
    return credentials.get("oauth_access_token") or credentials.get("access_token", "")


class YouTubeAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "youtube"

    # -- inbound (pure) ----------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            top = snippet.get("topLevelComment") or {}
            top_id = top.get("id")
            if not top_id:
                continue
            ctx = {"video_id": snippet.get("videoId"), "thread_id": item.get("id")}
            ev = self._parse_comment(top, top_id, ctx)
            if ev is not None:
                events.append(ev)
            # part=snippet returns only top-level comments; if a caller also
            # requested part=replies, ingest those nested replies too.
            for rep in ((item.get("replies") or {}).get("comments") or []):
                rev = self._parse_comment(rep, top_id, ctx)
                if rev is not None:
                    events.append(rev)
        return events

    def _parse_comment(
        self, comment: dict[str, Any], thread_top_id: str, ctx: dict[str, Any]
    ) -> MessageIn | None:
        cid = comment.get("id")
        sn = comment.get("snippet") or {}
        text = sn.get("textOriginal") or sn.get("textDisplay")
        if not cid or text is None:
            return None
        published = sn.get("publishedAt")
        occurred: datetime | None = None
        if published:
            try:
                occurred = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                occurred = None
        author_channel = (sn.get("authorChannelId") or {}).get("value")
        return MessageIn(
            external_message_id=str(cid),
            external_user_id=str(thread_top_id),  # thread == reply target (parentId)
            content=MessageContent(blocks=[TextBlock(text=text)]),
            external_timestamp=occurred,
            profile=ProfileHint(
                display_name=sn.get("authorDisplayName"),
                avatar_url=sn.get("authorProfileImageUrl"),
                meta={"author_channel_id": author_channel},
            ),
            meta={
                "video_id": ctx.get("video_id"),
                "thread_id": ctx.get("thread_id"),
                "comment_id": str(cid),
                "author_channel_id": author_channel,
            },
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        # Text-only channel: degrade_content collapses buttons/cards/location to
        # text; media (e.g. a card image that survives) becomes a link.
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                text = block.text
            elif isinstance(block, MediaBlock):
                url = file_public_url(block.file_id)
                text = f"{block.caption or '[' + block.media_type + ']'}\n{url}"
            else:
                text = MessageContent(blocks=[block]).plain_text()
            if text:
                payloads.append({"text": text})
        return payloads

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        # comments.insert replies to the top-level comment `to` (parentId).
        body = {"snippet": {"parentId": to, "textOriginal": payload.get("text", "")}}
        try:
            r = await self.http.post(
                f"{YT_API}/comments",
                params={"part": "snippet"},
                json=body,
                headers={"Authorization": f"Bearer {_access_token(credentials)}"},
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code < 400:
            return SendResult(ok=True, external_message_id=data.get("id"), raw=data)
        code, retry_after = self.classify_error(r.status_code, data)
        return SendResult(
            ok=False,
            error_code=code,
            error_message=str((data.get("error") or {}).get("message") or r.text)[:500],
            retry_after_s=retry_after,
            raw=data,
        )

    @staticmethod
    def classify_error(status_code: int, data: dict[str, Any]) -> tuple[str, float | None]:
        err = data.get("error") or {}
        errors = err.get("errors") or []
        reason = errors[0].get("reason", "") if errors else ""
        if status_code == 401 or reason in ("authError", "invalidCredentials"):
            return "AUTH", None
        if status_code == 403:
            if reason in ("quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded"):
                return "RATE_LIMITED", 60.0
            if reason in ("commentsDisabled", "processingFailure"):
                return "PERMANENT", None
            return "BLOCKED", None
        if status_code == 404:  # comment/thread deleted
            return "INVALID_RECIPIENT", None
        if status_code == 429:
            return "RATE_LIMITED", 60.0
        if status_code >= 500:
            return "RETRYABLE", None
        return "PERMANENT", None

    # -- polling (called by the worker; no webhook for YouTube) ------------
    async def poll_comments(
        self, account: Any, credentials: dict[str, Any], *, max_results: int = 100
    ) -> YouTubePollResult:
        """Fetch top-level comments across the channel's videos published after
        the stored cursor. On cold start (no cursor) it ingests the most recent
        page and sets the watermark; a bounded single page (max_results) caps
        the backlog. Returns a YouTubePollResult (empty when nothing new)."""
        cfg = getattr(account, "config", None) or {}
        channel_id = cfg.get("channel_id") or credentials.get("channel_id")
        if not channel_id:
            return YouTubePollResult()
        cursor = cfg.get("youtube_poll_cursor")
        try:
            r = await self.http.get(
                f"{YT_API}/commentThreads",
                params={
                    "part": "snippet",
                    "allThreadsRelatedToChannelId": channel_id,
                    "order": "time",
                    "maxResults": max_results,
                    "textFormat": "plainText",
                },
                headers={"Authorization": f"Bearer {_access_token(credentials)}"},
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return YouTubePollResult(cursor=cursor)
        if r.status_code >= 400:
            return YouTubePollResult(cursor=cursor)

        items = data.get("items") or []
        new_items: list[dict[str, Any]] = []
        newest = cursor
        for item in items:  # order=time → newest first
            published = (
                ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
            ).get("publishedAt")
            if not published:
                continue
            if cursor is not None and published <= cursor:
                break  # reached already-seen comments
            new_items.append(item)
            if newest is None or published > newest:
                newest = published
        return YouTubePollResult(
            payload={"items": new_items},
            cursor=newest,
            count=len(new_items),
        )

    # -- auth --------------------------------------------------------------
    async def refresh_credentials(
        self, account: Any, credentials: dict[str, Any]
    ) -> dict[str, Any] | None:
        refresh_token = credentials.get("oauth_refresh_token") or credentials.get("refresh_token")
        if not refresh_token:
            return None
        s = get_settings()
        client_id = s.youtube_oauth_client_id or s.google_oauth_client_id
        client_secret = s.youtube_oauth_client_secret or s.google_oauth_client_secret
        if not (client_id and client_secret):
            return None
        try:
            r = await self.http.post(
                GOOGLE_TOKEN,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        new_access = data.get("access_token")
        if not new_access:
            return None
        updated = {**credentials, "oauth_access_token": new_access}
        if data.get("refresh_token"):  # Google usually keeps the same RT
            updated["oauth_refresh_token"] = data["refresh_token"]
        exp = data.get("expires_in")
        if exp:
            try:
                updated["oauth_token_expires_at"] = (
                    datetime.now(UTC) + timedelta(seconds=int(exp))
                ).isoformat()
            except (ValueError, TypeError):
                pass
        return updated

    async def _get_channel(self, token: str, channel_id: str) -> dict[str, Any] | None:
        params: dict[str, Any] = {"part": "snippet"}
        if channel_id:
            params["id"] = channel_id
        else:
            params["mine"] = "true"
        try:
            r = await self.http.get(
                f"{YT_API}/channels",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if r.status_code >= 400:
            return None
        items = data.get("items") or []
        if not items:
            return None
        first = items[0]
        return {"id": first.get("id"), "title": (first.get("snippet") or {}).get("title")}

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        cfg = getattr(account, "config", None) or {}
        channel_id = str(cfg.get("channel_id") or credentials.get("channel_id") or "")
        info = await self._get_channel(_access_token(credentials), channel_id)
        if info is None:
            return HealthResult(
                ok=False, status="token_expired", detail={"error": "channels.list failed"}
            )
        return HealthResult(
            ok=True, status="active", detail={"channel_id": info["id"], "title": info["title"]}
        )

    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        merged = {**config, **credentials}
        token = merged.get("oauth_access_token") or merged.get("access_token")
        channel_id = str(merged.get("channel_id") or "")
        if not token:
            return ConnectResult(
                external_id=channel_id,
                health=HealthResult(
                    ok=False, status="error", detail={"error": "oauth_access_token required"}
                ),
            )
        info = await self._get_channel(token, channel_id)
        if info is None:
            return ConnectResult(
                external_id=channel_id,
                health=HealthResult(
                    ok=False,
                    status="token_expired",
                    detail={"error": "could not resolve YouTube channel (invalid token?)"},
                ),
            )
        external_id = str(info["id"] or channel_id)
        return ConnectResult(
            external_id=external_id,
            name=str(info.get("title") or ""),
            health=HealthResult(ok=True, status="active", detail={"title": info.get("title")}),
            config_patch={"channel_id": external_id},
            needs_webhook_secret=False,  # polled channel — no webhook URL to register
        )
