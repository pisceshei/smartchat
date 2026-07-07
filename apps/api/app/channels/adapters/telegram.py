"""Telegram Bot API adapter.

Inbound: Update objects delivered to /hooks/telegram/{webhook_secret}
(secret_token is also set on setWebhook and echoed back in the
X-Telegram-Bot-Api-Secret-Token header). Telegram message_id is only unique
per chat, so the dedup id is "{chat_id}:{message_id}".

Outbound: sendMessage / sendPhoto / sendDocument / sendVoice / sendVideo /
sendAudio / sendLocation; quick_buttons render as inline keyboards
(callback_data = button id); product cards as photo + caption + URL button.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from py_contracts.content import (
    ButtonReplyBlock,
    ContentBlock,
    LocationBlock,
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButtonsBlock,
    TemplateBlock,
    TextBlock,
)

from ..base import (
    AccountStatus,
    BaseAdapter,
    HealthResult,
    InboundEvent,
    MediaFetched,
    MediaRef,
    MessageIn,
    OptOut,
    ProfileHint,
    SendResult,
    degrade_content,
    secrets_equal,
)
from ..media import file_public_url

API_BASE = "https://api.telegram.org"

_MEDIA_FIELDS: list[tuple[str, str]] = [
    ("photo", "image"),
    ("video", "video"),
    ("video_note", "video"),
    ("voice", "voice"),
    ("audio", "audio"),
    ("document", "file"),
    ("sticker", "sticker"),
    ("animation", "video"),
]

_SEND_METHOD = {
    "image": "sendPhoto",
    "video": "sendVideo",
    "audio": "sendAudio",
    "voice": "sendVoice",
    "file": "sendDocument",
    "sticker": "sendDocument",
}

_MEDIA_PARAM = {
    "sendPhoto": "photo",
    "sendVideo": "video",
    "sendAudio": "audio",
    "sendVoice": "voice",
    "sendDocument": "document",
}


def _profile_from_user(user: dict[str, Any] | None) -> ProfileHint:
    if not user:
        return ProfileHint()
    name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p)
    return ProfileHint(
        display_name=name or user.get("username"),
        language=user.get("language_code"),
        meta={"username": user.get("username"), "telegram_user_id": user.get("id")},
    )


class TelegramAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "telegram_bot"

    # -- webhook -----------------------------------------------------------
    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> bool:
        header = headers.get("x-telegram-bot-api-secret-token") or headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )
        if header is None:
            return True  # path secret already matched by the router
        return secrets_equal(header, secret)

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        cbq = payload.get("callback_query")
        if cbq:
            chat = (cbq.get("message") or {}).get("chat") or {}
            chat_id = chat.get("id") or (cbq.get("from") or {}).get("id")
            events.append(
                MessageIn(
                    external_message_id=f"cbq:{cbq.get('id')}",
                    external_user_id=str(chat_id),
                    content=MessageContent(
                        blocks=[
                            ButtonReplyBlock(payload=cbq.get("data") or "", text=cbq.get("data") or "")
                        ]
                    ),
                    profile=_profile_from_user(cbq.get("from")),
                    reply_to_external_id=(
                        f"{chat_id}:{(cbq.get('message') or {}).get('message_id')}"
                        if cbq.get("message")
                        else None
                    ),
                )
            )
        mcm = payload.get("my_chat_member")
        if mcm:
            new_status = ((mcm.get("new_chat_member") or {}).get("status")) or ""
            user_id = str((mcm.get("chat") or {}).get("id", ""))
            if new_status in ("kicked", "left") and user_id:
                events.append(OptOut(external_user_id=user_id, reason=f"chat_member:{new_status}"))
        msg = payload.get("message") or payload.get("edited_message")
        if msg:
            ev = self._parse_message(msg)
            if ev is not None:
                events.append(ev)
        return events

    def _parse_message(self, msg: dict[str, Any]) -> MessageIn | None:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        caption = msg.get("caption")
        if msg.get("text"):
            blocks.append(TextBlock(text=msg["text"]))
        for field, media_type in _MEDIA_FIELDS:
            if field not in msg:
                continue
            obj = msg[field]
            if field == "photo":  # list of sizes → take the largest
                obj = sorted(obj, key=lambda p: p.get("file_size") or 0)[-1] if obj else None
            if not obj:
                continue
            provisional = uuid.uuid4()
            blocks.append(
                MediaBlock(
                    media_type=media_type,  # type: ignore[arg-type]
                    file_id=provisional,
                    caption=caption,
                    mime=obj.get("mime_type"),
                    size=obj.get("file_size"),
                    duration_ms=(obj.get("duration") or 0) * 1000 or None,
                    width=obj.get("width"),
                    height=obj.get("height"),
                )
            )
            media_refs.append(
                MediaRef(
                    block_index=len(blocks) - 1,
                    ref={
                        "kind": "tg_file",
                        "file_id": obj.get("file_id"),
                        "filename": obj.get("file_name"),
                    },
                )
            )
            break  # a Telegram message carries at most one media payload
        if "location" in msg:
            loc = msg["location"]
            blocks.append(LocationBlock(latitude=loc.get("latitude"), longitude=loc.get("longitude")))
        if "contact" in msg:
            c = msg["contact"]
            blocks.append(
                TextBlock(
                    text=f"Contact: {c.get('first_name', '')} {c.get('last_name', '') or ''} "
                    f"{c.get('phone_number', '')}".strip()
                )
            )
        if not blocks:
            return None
        ts = msg.get("date")
        return MessageIn(
            external_message_id=f"{chat_id}:{msg.get('message_id')}",
            external_user_id=str(chat_id),
            content=MessageContent(blocks=blocks),
            external_timestamp=datetime.fromtimestamp(ts, UTC) if ts else None,
            profile=_profile_from_user(msg.get("from")),
            media_refs=media_refs,
            reply_to_external_id=(
                f"{chat_id}:{msg['reply_to_message']['message_id']}"
                if msg.get("reply_to_message")
                else None
            ),
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"_method": "sendMessage", "text": block.text})
            elif isinstance(block, MediaBlock):
                method = _SEND_METHOD.get(block.media_type, "sendDocument")
                body: dict[str, Any] = {
                    "_method": method,
                    _MEDIA_PARAM[method]: file_public_url(block.file_id),
                }
                if block.caption:
                    body["caption"] = block.caption[:1024]
                payloads.append(body)
            elif isinstance(block, QuickButtonsBlock):
                keyboard = [
                    [{"text": b.text[: self.capabilities.button_text_max], "callback_data": b.id[:64]}]
                    for b in block.buttons
                ]
                payloads.append(
                    {
                        "_method": "sendMessage",
                        "text": block.text,
                        "reply_markup": {"inline_keyboard": keyboard},
                    }
                )
            elif isinstance(block, ProductCardBlock):
                caption_lines = [block.title]
                if block.subtitle:
                    caption_lines.append(block.subtitle)
                if block.price:
                    caption_lines.append(f"{block.price} {block.currency or ''}".strip())
                buttons = [
                    [{"text": b.text[:64], "url": b.value}]
                    for b in block.buttons
                    if b.action == "url"
                ]
                if block.url and not buttons:
                    buttons = [[{"text": "View", "url": block.url}]]
                image = (
                    file_public_url(block.image_file_id)
                    if block.image_file_id
                    else block.image_url
                )
                body = {
                    "_method": "sendPhoto" if image else "sendMessage",
                    ("caption" if image else "text"): "\n".join(caption_lines)[:1024],
                }
                if image:
                    body["photo"] = image
                elif block.url:
                    body["text"] = body["text"] + f"\n{block.url}"
                if buttons:
                    body["reply_markup"] = {"inline_keyboard": buttons}
                payloads.append(body)
            elif isinstance(block, LocationBlock):
                payloads.append(
                    {
                        "_method": "sendLocation",
                        "latitude": block.latitude,
                        "longitude": block.longitude,
                    }
                )
            elif isinstance(block, TemplateBlock):
                payloads.append({"_method": "sendMessage", "text": f"[template:{block.template_name}]"})
        return payloads

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        token = credentials.get("bot_token", "")
        body = {k: v for k, v in payload.items() if not k.startswith("_")}
        body["chat_id"] = to
        method = payload.get("_method", "sendMessage")
        try:
            r = await self.http.post(f"{API_BASE}/bot{token}/{method}", json=body)
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if data.get("ok"):
            result = data.get("result") or {}
            mid = result.get("message_id")
            return SendResult(
                ok=True,
                external_message_id=f"{to}:{mid}" if mid is not None else None,
                raw=data,
            )
        desc = str(data.get("description") or r.text)[:500]
        code, retry_after = self.classify_error(r.status_code, data)
        return SendResult(
            ok=False, error_code=code, error_message=desc, retry_after_s=retry_after, raw=data
        )

    @staticmethod
    def classify_error(status_code: int, data: dict[str, Any]) -> tuple[str, float | None]:
        desc = str(data.get("description") or "").lower()
        params = data.get("parameters") or {}
        if status_code == 401:
            return "AUTH", None
        if status_code == 429:
            return "RATE_LIMITED", float(params.get("retry_after") or 1.0)
        if status_code == 403:
            # "bot was blocked by the user" / "user is deactivated"
            return "INVALID_RECIPIENT", None
        if status_code == 400:
            if "message is too long" in desc:
                return "MESSAGE_TOO_LONG", None
            if "chat not found" in desc:
                return "INVALID_RECIPIENT", None
            return "PERMANENT", None
        if status_code >= 500:
            return "RETRYABLE", None
        return "PERMANENT", None

    async def send_typing(
        self, account: Any, credentials: dict[str, Any], to: str, on: bool = True
    ) -> None:
        if not on:
            return
        token = credentials.get("bot_token", "")
        try:
            await self.http.post(
                f"{API_BASE}/bot{token}/sendChatAction", json={"chat_id": to, "action": "typing"}
            )
        except httpx.HTTPError:
            pass

    async def fetch_media(
        self, account: Any, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None:
        if ref.get("kind") != "tg_file":
            return await super().fetch_media(account, credentials, ref)
        token = credentials.get("bot_token", "")
        try:
            r = await self.http.post(
                f"{API_BASE}/bot{token}/getFile", json={"file_id": ref.get("file_id")}
            )
            data = r.json()
            path = (data.get("result") or {}).get("file_path")
            if not path:
                return None
            f = await self.http.get(f"{API_BASE}/file/bot{token}/{path}")
            f.raise_for_status()
            return MediaFetched(
                data=f.content,
                mime=f.headers.get("content-type"),
                filename=ref.get("filename") or path.rsplit("/", 1)[-1],
            )
        except (httpx.HTTPError, ValueError):
            return None

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        token = credentials.get("bot_token", "")
        try:
            r = await self.http.get(f"{API_BASE}/bot{token}/getMe")
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return HealthResult(ok=False, status="error", detail={"error": str(e)[:300]})
        if data.get("ok"):
            result = data.get("result") or {}
            return HealthResult(
                ok=True,
                status="active",
                detail={"bot_id": result.get("id"), "username": result.get("username")},
            )
        status = "token_expired" if r.status_code == 401 else "error"
        return HealthResult(ok=False, status=status, detail={"error": data.get("description")})

    # -- connect-time helpers (used by modules/channels) --------------------
    async def validate_token(self, bot_token: str) -> dict[str, Any]:
        """getMe; raises ValueError on an invalid token."""
        r = await self.http.get(f"{API_BASE}/bot{bot_token}/getMe")
        data = r.json()
        if not data.get("ok"):
            raise ValueError(str(data.get("description") or "invalid bot token"))
        return data["result"]

    async def set_webhook(self, bot_token: str, url: str, secret: str) -> bool:
        try:
            r = await self.http.post(
                f"{API_BASE}/bot{bot_token}/setWebhook",
                json={
                    "url": url,
                    "secret_token": secret,
                    "allowed_updates": [
                        "message",
                        "edited_message",
                        "callback_query",
                        "my_chat_member",
                    ],
                },
            )
            return bool(r.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    def account_status_event(self, status: str, detail: dict[str, Any]) -> AccountStatus:
        return AccountStatus(status=status, detail=detail)
