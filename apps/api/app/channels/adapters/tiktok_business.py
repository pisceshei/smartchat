"""TikTok Business adapter — channel_type "tiktok_business".

⚠️ HONESTY NOTE ON API LIMITS (docs/channel-integration.md §8):
TikTok's *direct messaging* (DM) API is an allow-listed, gated surface — it is
NOT generally available and its exact request/response schema is not publicly
documented, so it is left as a TODO (`send_dm`) below. The realistically
available inbound-engagement surface for a Business account is **video comment
management** (list + reply) via the Business API v1.3, so that is the working
path this adapter implements:

  * inbound  — comment webhook events (POST /hooks/tiktok/{webhook_secret}) when
               the app is subscribed to comment events; otherwise poll_comments
               (fallback) lists comments for the configured video_ids.
  * outbound — a text reply to a comment via
               POST {BASE}/business/comment/reply/create/.

Because a comment reply needs BOTH the video id and the comment id, the ingress
identity (external_user_id) is keyed on "<video_id>:<comment_id>" so the fixed
sender contract (`to = external_user_id`) carries everything send() needs. The
per-comment id is the dedup key (external_message_id). Capabilities are
text-only (see CAPABILITIES["tiktok_business"]).

Auth: header `Access-Token: <access_token>`. TikTok Business responses wrap a
`code` (0 == OK) + `message` + `data`. refresh_credentials uses the platform
app (settings.tiktok_client_key / tiktok_client_secret).

Credential fields (connect_validate): access_token, business_id (+ optional
refresh_token for refresh_credentials).
"""
from __future__ import annotations

import json
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

BASE = "https://business-api.tiktok.com/open_api/v1.3"
_TARGET_SEP = ":"


def _split_target(to: str) -> tuple[str, str]:
    """Reverse the "<video_id>:<comment_id>" identity key. TikTok ids are
    digit-strings, so a single ':' split is unambiguous."""
    video_id, _, comment_id = to.partition(_TARGET_SEP)
    return video_id, comment_id


class TikTokBusinessAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "tiktok_business"

    # -- inbound (pure) ----------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        # Accept either a single event dict or a wrapper carrying many
        # ({"events": [...]}). TikTok's comment webhook envelope is not fully
        # public, so extraction below is deliberately tolerant.
        raw = payload.get("events")
        raw_events = raw if isinstance(raw, list) else [payload]
        events: list[InboundEvent] = []
        for ev in raw_events:
            parsed = self._parse_comment_event(ev)
            if parsed is not None:
                events.append(parsed)
        return events

    def _parse_comment_event(self, ev: dict[str, Any]) -> MessageIn | None:
        body: Any = ev.get("content") or ev.get("data") or ev
        if isinstance(body, str):  # some deliveries stringify the content field
            try:
                body = json.loads(body)
            except ValueError:
                return None
        if not isinstance(body, dict):
            return None
        comment_id = body.get("comment_id") or body.get("id")
        video_id = body.get("video_id") or body.get("item_id") or ""
        text = body.get("text") or body.get("content")
        if not comment_id or not text:
            return None
        ts = ev.get("create_time") or body.get("create_time")
        occurred: datetime | None = None
        if ts:
            try:
                occurred = datetime.fromtimestamp(int(ts), UTC)
            except (ValueError, TypeError, OSError):
                occurred = None
        return MessageIn(
            external_message_id=str(comment_id),
            external_user_id=f"{video_id}{_TARGET_SEP}{comment_id}",  # reply target
            content=MessageContent(blocks=[TextBlock(text=str(text))]),
            external_timestamp=occurred,
            profile=ProfileHint(
                display_name=body.get("nickname") or body.get("unique_id") or body.get("username"),
                meta={
                    "unique_id": body.get("unique_id"),
                    "user_id": body.get("user_id") or body.get("open_id"),
                },
            ),
            meta={"video_id": str(video_id), "comment_id": str(comment_id)},
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        # Text-only channel: degrade_content flattens everything to text.
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
        """Post a text reply to a comment. `to` == "<video_id>:<comment_id>"."""
        video_id, comment_id = _split_target(to)
        if not (video_id and comment_id):
            return SendResult(
                ok=False,
                error_code="INVALID_RECIPIENT",
                error_message="missing video_id/comment_id in target",
            )
        business_id = getattr(account, "external_id", "") or credentials.get("business_id", "")
        body = {
            "business_id": business_id,
            "video_id": video_id,
            "comment_id": comment_id,
            "text": payload.get("text", ""),
        }
        try:
            r = await self.http.post(
                f"{BASE}/business/comment/reply/create/",
                json=body,
                headers={
                    "Access-Token": credentials.get("access_token", ""),
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        code = data.get("code")
        if r.status_code < 400 and code in (0, None):
            reply_id = (data.get("data") or {}).get("comment_id") or (data.get("data") or {}).get("id")
            return SendResult(ok=True, external_message_id=reply_id, raw=data)
        typed = self.classify_error(r.status_code, code)
        return SendResult(
            ok=False,
            error_code=typed,
            error_message=str(data.get("message") or r.text)[:500],
            raw=data,
        )

    @staticmethod
    def classify_error(status_code: int, code: Any) -> str:
        """TikTok Business returns HTTP 200 with a body `code`; map the common
        ones. The catalogue is large and partly product-specific — unknown
        non-zero codes are treated as permanent."""
        if status_code == 401 or code in (40100, 40101, 40102, 40105, 40110):
            return "AUTH"
        if code in (40001, 40002):  # invalid params
            return "PERMANENT"
        if status_code == 429 or code in (50002, 40133):
            return "RATE_LIMITED"
        if status_code >= 500 or code in (50000,):
            return "RETRYABLE"
        return "PERMANENT"

    async def send_dm(
        self, account: Any, credentials: dict[str, Any], to: str, text: str
    ) -> SendResult:  # pragma: no cover - gated API
        """TODO(gated): TikTok direct messaging is allow-listed and its schema
        is not publicly documented. When granted, POST the Business Messaging
        send endpoint here. Until then callers must use send() (comment reply).
        """
        return SendResult(
            ok=False,
            error_code="UNSUPPORTED_CONTENT",
            error_message="TikTok DM API is gated/allow-listed; not enabled",
        )

    # -- polling fallback (when comment webhooks are not subscribed) -------
    async def poll_comments(
        self, account: Any, credentials: dict[str, Any]
    ) -> dict[str, Any]:
        """Fallback receive path: list comments for the video ids in
        config["video_ids"] and return an {"events": [...]} payload shaped for
        parse_inbound. A full implementation would first enumerate the account's
        videos via /business/video/list/; that (and comment webhooks) require
        Business API allow-listing, so this stays scoped to explicitly-watched
        videos. Returns {"events": []} when nothing is configured/available."""
        cfg = getattr(account, "config", None) or {}
        business_id = getattr(account, "external_id", "") or credentials.get("business_id", "")
        video_ids = cfg.get("video_ids") or []
        token = credentials.get("access_token", "")
        out: list[dict[str, Any]] = []
        for video_id in video_ids:
            try:
                r = await self.http.get(
                    f"{BASE}/business/comment/list/",
                    params={"business_id": business_id, "video_id": video_id},
                    headers={"Access-Token": token},
                )
                data = r.json()
            except (httpx.HTTPError, ValueError):
                continue
            if r.status_code >= 400 or data.get("code") not in (0, None):
                continue
            for c in (data.get("data") or {}).get("comments") or []:
                out.append({**c, "video_id": c.get("video_id") or video_id})
        return {"events": out}

    # -- auth --------------------------------------------------------------
    async def refresh_credentials(
        self, account: Any, credentials: dict[str, Any]
    ) -> dict[str, Any] | None:
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            return None
        s = get_settings()
        if not (s.tiktok_client_key and s.tiktok_client_secret):
            return None
        try:
            r = await self.http.post(
                f"{BASE}/oauth2/refresh_token/",
                json={
                    "client_key": s.tiktok_client_key,
                    "client_secret": s.tiktok_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        payload = data.get("data") or data
        new_access = payload.get("access_token")
        if not new_access:
            return None
        updated = {**credentials, "access_token": new_access}
        if payload.get("refresh_token"):
            updated["refresh_token"] = payload["refresh_token"]
        exp = payload.get("expires_in")
        if exp:
            try:
                updated["token_expires_at"] = (
                    datetime.now(UTC) + timedelta(seconds=int(exp))
                ).isoformat()
            except (ValueError, TypeError):
                pass
        return updated

    async def _get_business(self, token: str, business_id: str) -> dict[str, Any] | None:
        try:
            r = await self.http.get(
                f"{BASE}/business/get/",
                params={
                    "business_id": business_id,
                    "fields": json.dumps(["username", "display_name", "profile_image"]),
                },
                headers={"Access-Token": token},
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if r.status_code >= 400 or data.get("code") not in (0, None):
            return None
        return data.get("data") or {}

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        business_id = getattr(account, "external_id", "") or credentials.get("business_id", "")
        info = await self._get_business(credentials.get("access_token", ""), business_id)
        if info is None:
            return HealthResult(
                ok=False,
                status="token_expired",
                detail={"error": "business/get failed (token invalid or not allow-listed)"},
            )
        return HealthResult(
            ok=True,
            status="active",
            detail={"username": info.get("username"), "display_name": info.get("display_name")},
        )

    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        merged = {**config, **credentials}
        access_token = merged.get("access_token")
        business_id = str(merged.get("business_id") or "")
        if not access_token or not business_id:
            return ConnectResult(
                external_id=business_id,
                health=HealthResult(
                    ok=False,
                    status="error",
                    detail={"error": "access_token and business_id required"},
                ),
            )
        info = await self._get_business(access_token, business_id)
        if info is None:
            return ConnectResult(
                external_id=business_id,
                health=HealthResult(
                    ok=False,
                    status="token_expired",
                    detail={"error": "could not fetch business account (invalid token / not gated)"},
                ),
            )
        patch: dict[str, Any] = {"business_id": business_id}
        if info.get("username"):
            patch["username"] = info["username"]
        return ConnectResult(
            external_id=business_id,
            name=str(info.get("display_name") or info.get("username") or ""),
            health=HealthResult(ok=True, status="active", detail=info),
            config_patch=patch,
            needs_webhook_secret=True,
        )
