"""LINE Official Account (Messaging API) adapter.

Inbound: /hooks/line/{webhook_secret} with X-Line-Signature =
base64(HMAC-SHA256(channel_secret, body)). Media bytes come from the
api-data.line.me content endpoint.

Outbound: push messages (Bearer channel access token). Quick buttons render
as quickReply items; product cards as Flex bubbles per the plan.
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
    TextBlock,
)

from ..base import (
    BaseAdapter,
    ContactUpdate,
    HealthResult,
    InboundEvent,
    MediaFetched,
    MediaRef,
    MessageIn,
    OptOut,
    ProfileHint,
    SendResult,
    degrade_content,
    verify_line_signature,
)
from ..media import file_public_url

API_BASE = "https://api.line.me"
DATA_BASE = "https://api-data.line.me"

_LINE_MEDIA = {"image": "image", "video": "video", "audio": "audio", "file": "file"}


class LineAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "line_oa"

    # -- webhook -----------------------------------------------------------
    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> bool:
        header = headers.get("x-line-signature") or headers.get("X-Line-Signature")
        return verify_line_signature(secret, body, header)

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        for ev in payload.get("events", []) or []:
            etype = ev.get("type")
            user_id = (ev.get("source") or {}).get("userId")
            if not user_id:
                continue
            ts = ev.get("timestamp")
            occurred = datetime.fromtimestamp(ts / 1000, UTC) if ts else None
            if etype == "message":
                parsed = self._parse_message(user_id, ev, occurred)
                if parsed is not None:
                    events.append(parsed)
            elif etype == "postback":
                data = (ev.get("postback") or {}).get("data") or ""
                events.append(
                    MessageIn(
                        external_message_id=f"pb:{ev.get('webhookEventId') or ts}",
                        external_user_id=user_id,
                        content=MessageContent(
                            blocks=[ButtonReplyBlock(payload=data, text=data)]
                        ),
                        external_timestamp=occurred,
                    )
                )
            elif etype == "follow":
                events.append(ContactUpdate(external_user_id=user_id))
            elif etype == "unfollow":
                events.append(OptOut(external_user_id=user_id, reason="unfollow"))
        return events

    def _parse_message(
        self, user_id: str, ev: dict[str, Any], occurred: datetime | None
    ) -> MessageIn | None:
        msg = ev.get("message") or {}
        mid = msg.get("id")
        mtype = msg.get("type")
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        if mtype == "text":
            blocks.append(TextBlock(text=msg.get("text", "")))
        elif mtype in ("image", "video", "audio", "file"):
            blocks.append(
                MediaBlock(
                    media_type=_LINE_MEDIA[mtype],  # type: ignore[arg-type]
                    file_id=uuid.uuid4(),
                    duration_ms=msg.get("duration"),
                )
            )
            media_refs.append(
                MediaRef(
                    block_index=0,
                    ref={
                        "kind": "line_content",
                        "message_id": mid,
                        "filename": msg.get("fileName"),
                    },
                )
            )
        elif mtype == "location":
            blocks.append(
                LocationBlock(
                    latitude=msg.get("latitude", 0.0),
                    longitude=msg.get("longitude", 0.0),
                    name=msg.get("title"),
                    address=msg.get("address"),
                )
            )
        elif mtype == "sticker":
            blocks.append(TextBlock(text="[sticker]"))
        else:
            return None
        return MessageIn(
            external_message_id=str(mid or ev.get("webhookEventId")),
            external_user_id=user_id,
            content=MessageContent(blocks=blocks),
            external_timestamp=occurred,
            media_refs=media_refs,
            meta={"reply_token": ev.get("replyToken")},
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"type": "text", "text": block.text})
            elif isinstance(block, MediaBlock):
                url = file_public_url(block.file_id)
                if block.media_type == "image":
                    payloads.append(
                        {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
                    )
                elif block.media_type == "video":
                    payloads.append(
                        {"type": "video", "originalContentUrl": url, "previewImageUrl": url}
                    )
                elif block.media_type in ("audio", "voice"):
                    payloads.append(
                        {
                            "type": "audio",
                            "originalContentUrl": url,
                            "duration": block.duration_ms or 60000,
                        }
                    )
                else:
                    payloads.append({"type": "text", "text": f"{block.caption or 'File'}\n{url}"})
            elif isinstance(block, QuickButtonsBlock):
                payloads.append(
                    {
                        "type": "text",
                        "text": block.text,
                        "quickReply": {
                            "items": [
                                {
                                    "type": "action",
                                    "action": {
                                        "type": "postback",
                                        "label": b.text[: self.capabilities.button_text_max],
                                        "data": b.id[:300],
                                        "displayText": b.text[:300],
                                    },
                                }
                                for b in block.buttons
                            ]
                        },
                    }
                )
            elif isinstance(block, ProductCardBlock):
                payloads.append(self._flex_card(block))
            elif isinstance(block, LocationBlock):
                payloads.append(
                    {
                        "type": "location",
                        "title": (block.name or "Location")[:100],
                        "address": (block.address or "-")[:100],
                        "latitude": block.latitude,
                        "longitude": block.longitude,
                    }
                )
        return payloads

    def _flex_card(self, block: ProductCardBlock) -> dict[str, Any]:
        image = file_public_url(block.image_file_id) if block.image_file_id else block.image_url
        body_contents: list[dict[str, Any]] = [
            {"type": "text", "text": block.title[:200], "weight": "bold", "size": "md", "wrap": True}
        ]
        if block.subtitle:
            body_contents.append(
                {"type": "text", "text": block.subtitle[:200], "size": "sm", "color": "#888888", "wrap": True}
            )
        if block.price:
            body_contents.append(
                {
                    "type": "text",
                    "text": f"{block.price} {block.currency or ''}".strip(),
                    "size": "lg",
                    "weight": "bold",
                }
            )
        footer_buttons: list[dict[str, Any]] = []
        for b in block.buttons[:3]:
            if b.action == "url":
                footer_buttons.append(
                    {
                        "type": "button",
                        "style": "primary",
                        "action": {"type": "uri", "label": b.text[:20], "uri": b.value},
                    }
                )
            else:
                footer_buttons.append(
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "postback",
                            "label": b.text[:20],
                            "data": b.value[:300],
                        },
                    }
                )
        if not footer_buttons and block.url:
            footer_buttons.append(
                {
                    "type": "button",
                    "style": "primary",
                    "action": {"type": "uri", "label": "Buy Now", "uri": block.url},
                }
            )
        bubble: dict[str, Any] = {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents},
        }
        if image:
            bubble["hero"] = {
                "type": "image",
                "url": image,
                "size": "full",
                "aspectRatio": "20:13",
                "aspectMode": "cover",
            }
        if footer_buttons:
            bubble["footer"] = {"type": "box", "layout": "vertical", "contents": footer_buttons}
        return {"type": "flex", "altText": block.title[:400], "contents": bubble}

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        token = credentials.get("access_token", "")
        try:
            r = await self.http.post(
                f"{API_BASE}/v2/bot/message/push",
                json={"to": to, "messages": [payload]},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code < 400:
            sent = data.get("sentMessages") or []
            ext = sent[0].get("id") if sent else r.headers.get("x-line-request-id")
            return SendResult(ok=True, external_message_id=ext, raw=data)
        code = self.classify_error(r.status_code)
        retry_after = None
        if r.status_code == 429:
            try:
                retry_after = float(r.headers.get("retry-after") or 1.0)
            except ValueError:
                retry_after = 1.0
        return SendResult(
            ok=False,
            error_code=code,
            error_message=str(data.get("message") or r.text)[:500],
            retry_after_s=retry_after,
            raw=data,
        )

    @staticmethod
    def classify_error(status_code: int) -> str:
        if status_code == 401:
            return "AUTH"
        if status_code == 429:
            return "RATE_LIMITED"
        if status_code == 403:
            return "BLOCKED"
        if status_code >= 500:
            return "RETRYABLE"
        return "PERMANENT"

    async def fetch_media(
        self, account: Any, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None:
        if ref.get("kind") != "line_content":
            return await super().fetch_media(account, credentials, ref)
        try:
            r = await self.http.get(
                f"{DATA_BASE}/v2/bot/message/{ref.get('message_id')}/content",
                headers={"Authorization": f"Bearer {credentials.get('access_token', '')}"},
            )
            r.raise_for_status()
            return MediaFetched(
                data=r.content,
                mime=r.headers.get("content-type"),
                filename=ref.get("filename"),
            )
        except httpx.HTTPError:
            return None

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        try:
            r = await self.http.get(
                f"{API_BASE}/v2/bot/info",
                headers={"Authorization": f"Bearer {credentials.get('access_token', '')}"},
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return HealthResult(ok=False, status="error", detail={"error": str(e)[:300]})
        if r.status_code < 400:
            return HealthResult(
                ok=True,
                status="active",
                detail={"basic_id": data.get("basicId"), "display_name": data.get("displayName")},
            )
        status = "token_expired" if r.status_code == 401 else "error"
        return HealthResult(ok=False, status=status, detail={"error": data.get("message")})

    async def fetch_profile(
        self, credentials: dict[str, Any], user_id: str
    ) -> ProfileHint | None:
        try:
            r = await self.http.get(
                f"{API_BASE}/v2/bot/profile/{user_id}",
                headers={"Authorization": f"Bearer {credentials.get('access_token', '')}"},
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            return ProfileHint(
                display_name=data.get("displayName"),
                avatar_url=data.get("pictureUrl"),
                language=data.get("language"),
            )
        except (httpx.HTTPError, ValueError):
            return None

    async def set_webhook(self, access_token: str, url: str) -> bool:
        try:
            r = await self.http.put(
                f"{API_BASE}/v2/bot/channel/webhook/endpoint",
                json={"endpoint": url},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            return r.status_code < 400
        except httpx.HTTPError:
            return False
