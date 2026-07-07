"""Slack (Events API + Web API) adapter — channel_type "slack".

Inbound: Events API callbacks delivered to the SINGLE app-level Request URL
`/hooks/slack` (one URL per Slack app; routed to the connected workspace by the
top-level `team_id`). Request signing (verified in the hook / verify_webhook):

    basestring = "v0:" + X-Slack-Request-Timestamp + ":" + raw_body
    X-Slack-Signature = "v0=" + hex(hmac_sha256(signing_secret, basestring))

with a 5-minute timestamp replay window. Our own bot messages come back as
`message` events (subtype == "bot_message" / a `bot_id` is present); those are
ignored so the bot never answers itself.

Outbound: chat.postMessage (Authorization: Bearer xoxb-…). Quick buttons render
as a Block Kit `actions` block; product cards as a `section` (title/price + image
accessory) followed by link/action buttons.

NOTE (protocol deviation from docs/channel-integration.md §"files.upload for
media"): the classic `files.upload` Web API method is deprecated (retired 2025 in
favour of the getUploadURLExternal/completeUploadExternal flow). Our inbound media
is already copied to a public `{assets_base_url}` URL, so outbound images render as
Block Kit `image` blocks (Slack fetches the public URL) and other files as an
unfurled link inside a single chat.postMessage call — no upload round-trip.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from py_contracts.content import (
    ButtonReplyBlock,
    ContentBlock,
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButtonsBlock,
    TextBlock,
)

from ..base import (
    BaseAdapter,
    ConnectResult,
    HealthResult,
    InboundEvent,
    MediaFetched,
    MediaRef,
    MessageIn,
    ProfileHint,
    SendResult,
    degrade_content,
)
from ..media import file_public_url

API_BASE = "https://slack.com/api"

# Slack Web API error strings → our canonical ErrorCode.
_AUTH_ERRORS = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "no_permission",
        "missing_scope",
        "ekm_access_denied",
    }
)
_RECIPIENT_ERRORS = frozenset(
    {
        "channel_not_found",
        "is_archived",
        "not_in_channel",
        "user_not_found",
        "cannot_dm_bot",
        "user_not_in_channel",
    }
)
_BLOCKED_ERRORS = frozenset({"restricted_action", "restricted_action_read_only_channel"})
_RATELIMIT_ERRORS = frozenset({"ratelimited", "rate_limited"})


# --------------------------------------------------------------------------
# signature helpers (pure — importable by hooks/slack.py + tests)
# --------------------------------------------------------------------------
def slack_basestring(timestamp: str, body: bytes) -> bytes:
    return b"v0:" + timestamp.encode() + b":" + body


def slack_signature(signing_secret: str, timestamp: str, body: bytes) -> str:
    """v0 request signature: 'v0=' + hex(hmac_sha256(signing_secret, basestring))."""
    digest = hmac.new(
        signing_secret.encode(), slack_basestring(timestamp, body), hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def verify_slack_signature(
    signing_secret: str,
    body: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    max_age_s: int = 300,
    now: float | None = None,
) -> bool:
    """Constant-time verify with a ±max_age_s replay window (default 5 min)."""
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_age_s:
        return False
    return hmac.compare_digest(slack_signature(signing_secret, timestamp, body), signature)


def _media_type_for(mimetype: str | None) -> str:
    top = (mimetype or "").split("/", 1)[0]
    if top in ("image", "video", "audio"):
        return top
    return "file"


class SlackAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "slack"

    # -- webhook -----------------------------------------------------------
    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> bool:
        ts = headers.get("x-slack-request-timestamp") or headers.get("X-Slack-Request-Timestamp")
        sig = headers.get("x-slack-signature") or headers.get("X-Slack-Signature")
        return verify_slack_signature(secret, body, timestamp=ts, signature=sig)

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        ptype = payload.get("type")
        if ptype == "event_callback":
            event = payload.get("event") or {}
            if event.get("type") == "message":
                ev = self._parse_message(payload, event)
                return [ev] if ev is not None else []
            return []
        if ptype == "block_actions":
            # Interactive button click. NOTE: block_actions payloads are delivered
            # form-encoded to the app's separate Interactivity Request URL, not the
            # Events API; this branch only fires if that URL is later wired to
            # enqueue the decoded JSON. parse_inbound stays pure either way.
            return self._parse_block_actions(payload)
        return []

    def _parse_message(
        self, payload: dict[str, Any], event: dict[str, Any]
    ) -> MessageIn | None:
        subtype = event.get("subtype")
        # ignore our own bot's echoes and non-user message subtypes (edits,
        # deletions, joins/leaves) to avoid answer loops / noise.
        if event.get("bot_id") or subtype == "bot_message":
            return None
        if subtype not in (None, "file_share", "me_message", "thread_broadcast"):
            return None
        channel = event.get("channel")
        if not channel:
            return None
        user = event.get("user")
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        text = event.get("text")
        if text:
            blocks.append(TextBlock(text=text))
        for f in event.get("files", []) or []:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            blocks.append(
                MediaBlock(
                    media_type=_media_type_for(f.get("mimetype")),  # type: ignore[arg-type]
                    file_id=uuid.uuid4(),
                    caption=f.get("title") or None,
                    mime=f.get("mimetype"),
                    size=f.get("size"),
                )
            )
            media_refs.append(
                MediaRef(
                    block_index=len(blocks) - 1,
                    ref={
                        "kind": "slack_file",
                        "url": url,
                        "filename": f.get("name"),
                        "mime": f.get("mimetype"),
                    },
                )
            )
        if not blocks:
            return None
        ts = event.get("ts")
        occurred = None
        if ts:
            try:
                occurred = datetime.fromtimestamp(float(ts), UTC)
            except (TypeError, ValueError):
                occurred = None
        # dedup key: envelope event_id is stable across Slack retries; fall back
        # to channel:ts (ts is unique per channel).
        external_id = str(payload.get("event_id") or f"{channel}:{ts}")
        return MessageIn(
            external_message_id=external_id,
            external_user_id=str(channel),
            content=MessageContent(blocks=blocks),
            external_timestamp=occurred,
            profile=ProfileHint(meta={"slack_user_id": user}),
            media_refs=media_refs,
            reply_to_external_id=(f"{channel}:{event['thread_ts']}" if event.get("thread_ts") else None),
            meta={"slack_user_id": user, "ts": ts, "thread_ts": event.get("thread_ts")},
        )

    def _parse_block_actions(self, payload: dict[str, Any]) -> list[InboundEvent]:
        actions = payload.get("actions") or []
        if not actions:
            return []
        action = actions[0]
        channel = (payload.get("channel") or {}).get("id")
        user = (payload.get("user") or {}).get("id")
        if not channel:
            return []
        value = action.get("value") or action.get("action_id") or ""
        return [
            MessageIn(
                external_message_id=f"ba:{payload.get('trigger_id') or action.get('action_ts')}",
                external_user_id=str(channel),
                content=MessageContent(blocks=[ButtonReplyBlock(payload=value, text=value)]),
                profile=ProfileHint(meta={"slack_user_id": user}),
            )
        ]

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"_method": "chat.postMessage", "text": block.text})
            elif isinstance(block, MediaBlock):
                payloads.append(self._media_payload(block))
            elif isinstance(block, QuickButtonsBlock):
                payloads.append(self._buttons_payload(block))
            elif isinstance(block, ProductCardBlock):
                payloads.append(self._card_payload(block))
        return payloads

    def _media_payload(self, block: MediaBlock) -> dict[str, Any]:
        url = file_public_url(block.file_id)
        caption = block.caption or ""
        if block.media_type == "image":
            return {
                "_method": "chat.postMessage",
                "text": caption or "image",
                "blocks": [
                    {"type": "image", "image_url": url, "alt_text": (caption or "image")[:2000]}
                ],
            }
        label = caption or f"[{block.media_type}]"
        return {"_method": "chat.postMessage", "text": f"{label}\n{url}"}

    def _buttons_payload(self, block: QuickButtonsBlock) -> dict[str, Any]:
        elements = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": b.text[: self.capabilities.button_text_max]},
                "action_id": f"qb_{b.id}"[:255],
                "value": b.id[:2000],
            }
            for b in block.buttons
        ]
        return {
            "_method": "chat.postMessage",
            "text": block.text,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": block.text[:3000]}},
                {"type": "actions", "elements": elements},
            ],
        }

    def _card_payload(self, block: ProductCardBlock) -> dict[str, Any]:
        lines = [f"*{block.title}*"]
        if block.subtitle:
            lines.append(block.subtitle)
        if block.price:
            lines.append(f"{block.price} {block.currency or ''}".strip())
        section: dict[str, Any] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)[:3000]},
        }
        image = file_public_url(block.image_file_id) if block.image_file_id else block.image_url
        if image:
            section["accessory"] = {
                "type": "image",
                "image_url": image,
                "alt_text": block.title[:2000],
            }
        blocks: list[dict[str, Any]] = [section]
        action_elements: list[dict[str, Any]] = []
        for b in block.buttons[:5]:
            el: dict[str, Any] = {
                "type": "button",
                "text": {"type": "plain_text", "text": b.text[: self.capabilities.button_text_max]},
                "action_id": f"card_{b.value}"[:255],
            }
            if b.action == "url":
                el["url"] = b.value
            else:
                el["value"] = b.value[:2000]
            action_elements.append(el)
        if not action_elements and block.url:
            action_elements.append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View"},
                    "url": block.url,
                    "action_id": "card_view",
                }
            )
        if action_elements:
            blocks.append({"type": "actions", "elements": action_elements})
        return {"_method": "chat.postMessage", "text": block.title, "blocks": blocks}

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        token = credentials.get("bot_token", "")
        method = payload.get("_method", "chat.postMessage")
        body = {k: v for k, v in payload.items() if not k.startswith("_")}
        body["channel"] = to
        try:
            r = await self.http.post(
                f"{API_BASE}/{method}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if data.get("ok"):
            ts = data.get("ts")
            ch = data.get("channel", to)
            return SendResult(
                ok=True,
                external_message_id=f"{ch}:{ts}" if ts else None,
                raw=data,
            )
        err = str(data.get("error") or "")
        code, retry_after = self.classify_error(r.status_code, err, r.headers)
        return SendResult(
            ok=False,
            error_code=code,
            error_message=err or r.text[:500],
            retry_after_s=retry_after,
            raw=data,
        )

    @staticmethod
    def classify_error(
        status_code: int, error: str, headers: Any = None
    ) -> tuple[str, float | None]:
        if status_code == 429 or error in _RATELIMIT_ERRORS:
            retry = 1.0
            if headers is not None:
                try:
                    retry = float(headers.get("Retry-After") or headers.get("retry-after") or 1.0)
                except (TypeError, ValueError):
                    retry = 1.0
            return "RATE_LIMITED", retry
        if error in _AUTH_ERRORS:
            return "AUTH", None
        if error in _BLOCKED_ERRORS:
            return "BLOCKED", None
        if error in _RECIPIENT_ERRORS:
            return "INVALID_RECIPIENT", None
        if error == "msg_too_long":
            return "MESSAGE_TOO_LONG", None
        if status_code >= 500 or error in ("service_unavailable", "internal_error", "fatal_error"):
            return "RETRYABLE", None
        return "PERMANENT", None

    async def fetch_media(
        self, account: Any, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None:
        if ref.get("kind") != "slack_file":
            return await super().fetch_media(account, credentials, ref)
        url = ref.get("url")
        if not url:
            return None
        try:
            r = await self.http.get(
                url, headers={"Authorization": f"Bearer {credentials.get('bot_token', '')}"}
            )
            r.raise_for_status()
        except httpx.HTTPError:
            return None
        return MediaFetched(
            data=r.content,
            mime=ref.get("mime") or r.headers.get("content-type"),
            filename=ref.get("filename"),
        )

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        info = await self._auth_test(credentials.get("bot_token", ""))
        if info.get("ok"):
            return HealthResult(
                ok=True,
                status="active",
                detail={
                    "team": info.get("team"),
                    "team_id": info.get("team_id"),
                    "bot_user_id": info.get("user_id"),
                },
            )
        err = str(info.get("error") or "")
        status = "token_expired" if err in _AUTH_ERRORS else "error"
        return HealthResult(ok=False, status=status, detail={"error": err or "auth.test failed"})

    async def _auth_test(self, bot_token: str) -> dict[str, Any]:
        try:
            r = await self.http.post(
                f"{API_BASE}/auth.test", headers={"Authorization": f"Bearer {bot_token}"}
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "error": str(e)[:200]}
        return data if isinstance(data, dict) else {"ok": False, "error": "bad response"}

    # -- connect-time validation (dispatched from modules/channels) --------
    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        bot_token = credentials.get("bot_token") or config.get("bot_token") or ""
        if not bot_token.startswith("xoxb-"):
            return ConnectResult(
                external_id="",
                health=HealthResult(
                    ok=False, status="error", detail={"error": "bot_token must start with xoxb-"}
                ),
            )
        info = await self._auth_test(bot_token)
        if not info.get("ok"):
            err = str(info.get("error") or "auth.test failed")
            status = "token_expired" if err in _AUTH_ERRORS else "error"
            return ConnectResult(
                external_id="", health=HealthResult(ok=False, status=status, detail={"error": err})
            )
        team_id = str(info.get("team_id") or "")
        return ConnectResult(
            external_id=team_id,
            name=str(config.get("name") or info.get("team") or team_id),
            health=HealthResult(
                ok=True,
                status="active",
                detail={
                    "team": info.get("team"),
                    "team_id": team_id,
                    "bot_user_id": info.get("user_id"),
                },
            ),
            config_patch={"team": info.get("team"), "bot_user_id": info.get("user_id")},
            # single app-level Events API URL (/hooks/slack); no per-account secret.
            needs_webhook_secret=False,
        )
