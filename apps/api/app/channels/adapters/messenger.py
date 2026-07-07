"""Facebook Messenger (Send API, Graph v21) adapter.

Inbound: page entry slices ({"messaging": [...]}) routed by page id on
/hooks/meta. Echo messages (is_echo) are skipped — our own sends are deduped
via message_dedup anyway. delivery/read arrive as watermark events.

Outbound: POST /me/messages with the page access token. Quick buttons →
quick_replies (≤13); product cards → generic template; sends outside the 24h
window get a message tag attached (render(window_open=False)).
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
    DeliveryStatus,
    HealthResult,
    InboundEvent,
    MediaRef,
    MessageIn,
    OptOut,
    ProfileHint,
    ReadReceipt,
    SendResult,
    degrade_content,
)
from ..media import file_public_url

GRAPH_BASE = "https://graph.facebook.com/v21.0"

_ATTACH_TYPES = {"image": "image", "video": "video", "audio": "audio", "file": "file"}
_IN_ATTACH = {"image": "image", "video": "video", "audio": "audio", "file": "file"}

# tag applied outside the 24h window (requires app approval on Meta side)
DEFAULT_MESSAGE_TAG = "HUMAN_AGENT"


class MessengerAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "messenger"

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        for ev in payload.get("messaging", []) or []:
            sender = (ev.get("sender") or {}).get("id")
            if not sender:
                continue
            if "message" in ev:
                msg = ev["message"]
                if msg.get("is_echo"):
                    continue
                parsed = self._parse_message(sender, ev, msg)
                if parsed is not None:
                    events.append(parsed)
            elif "postback" in ev:
                pb = ev["postback"]
                ts = ev.get("timestamp")
                events.append(
                    MessageIn(
                        external_message_id=pb.get("mid") or f"pb:{sender}:{ts}",
                        external_user_id=sender,
                        content=MessageContent(
                            blocks=[
                                ButtonReplyBlock(
                                    payload=pb.get("payload") or "", text=pb.get("title") or ""
                                )
                            ]
                        ),
                        external_timestamp=(
                            datetime.fromtimestamp(ts / 1000, UTC) if ts else None
                        ),
                    )
                )
            elif "delivery" in ev:
                for mid in ev["delivery"].get("mids", []) or []:
                    events.append(
                        DeliveryStatus(
                            external_message_id=mid, status="delivered", external_user_id=sender
                        )
                    )
            elif "read" in ev:
                watermark = ev["read"].get("watermark")
                if watermark:
                    events.append(
                        ReadReceipt(
                            external_user_id=sender,
                            watermark=datetime.fromtimestamp(watermark / 1000, UTC),
                        )
                    )
            elif "optin" in ev:
                pass  # marketing opt-in; P3 broadcasts
            elif "account_linking" in ev:
                pass
        return events

    def _parse_message(
        self, sender: str, ev: dict[str, Any], msg: dict[str, Any]
    ) -> MessageIn | None:
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        qr = msg.get("quick_reply")
        if qr:
            blocks.append(
                ButtonReplyBlock(payload=qr.get("payload") or "", text=msg.get("text") or "")
            )
        elif msg.get("text"):
            blocks.append(TextBlock(text=msg["text"]))
        for att in msg.get("attachments", []) or []:
            atype = att.get("type")
            payload = att.get("payload") or {}
            if atype == "location":
                coords = payload.get("coordinates") or {}
                blocks.append(
                    LocationBlock(
                        latitude=coords.get("lat", 0.0), longitude=coords.get("long", 0.0)
                    )
                )
            elif atype in _IN_ATTACH and payload.get("url"):
                blocks.append(
                    MediaBlock(media_type=_IN_ATTACH[atype], file_id=uuid.uuid4())  # type: ignore[arg-type]
                )
                media_refs.append(
                    MediaRef(block_index=len(blocks) - 1, ref={"kind": "url", "url": payload["url"]})
                )
            elif atype == "fallback" and payload.get("url"):
                blocks.append(TextBlock(text=payload.get("title") or payload["url"]))
        if not blocks:
            return None
        ts = ev.get("timestamp")
        return MessageIn(
            external_message_id=msg.get("mid") or f"m:{sender}:{ts}",
            external_user_id=sender,
            content=MessageContent(blocks=blocks),
            external_timestamp=datetime.fromtimestamp(ts / 1000, UTC) if ts else None,
            profile=ProfileHint(),
            media_refs=media_refs,
            reply_to_external_id=(msg.get("reply_to") or {}).get("mid"),
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"message": {"text": block.text}})
            elif isinstance(block, MediaBlock):
                atype = _ATTACH_TYPES.get(block.media_type, "file")
                payloads.append(
                    {
                        "message": {
                            "attachment": {
                                "type": atype,
                                "payload": {"url": file_public_url(block.file_id), "is_reusable": False},
                            }
                        }
                    }
                )
            elif isinstance(block, QuickButtonsBlock):
                payloads.append(
                    {
                        "message": {
                            "text": block.text,
                            "quick_replies": [
                                {
                                    "content_type": "text",
                                    "title": b.text[: self.capabilities.button_text_max],
                                    "payload": b.id[:1000],
                                }
                                for b in block.buttons
                            ],
                        }
                    }
                )
            elif isinstance(block, ProductCardBlock):
                payloads.append({"message": {"attachment": self._generic_template(block)}})
            elif isinstance(block, LocationBlock):
                payloads.append(
                    {
                        "message": {
                            "text": f"{block.name or 'Location'}\n"
                            f"https://maps.google.com/?q={block.latitude},{block.longitude}"
                        }
                    }
                )
        if not window_open:
            for p in payloads:
                p["_tag"] = DEFAULT_MESSAGE_TAG
        return payloads

    def _generic_template(self, block: ProductCardBlock) -> dict[str, Any]:
        buttons: list[dict[str, Any]] = []
        for b in block.buttons[:3]:
            if b.action == "url":
                buttons.append({"type": "web_url", "url": b.value, "title": b.text[:20]})
            else:
                buttons.append({"type": "postback", "payload": b.value[:1000], "title": b.text[:20]})
        if not buttons and block.url:
            buttons = [{"type": "web_url", "url": block.url, "title": "Buy Now"}]
        element: dict[str, Any] = {"title": block.title[:80]}
        subtitle_parts = [p for p in [block.subtitle, block.price] if p]
        if subtitle_parts:
            element["subtitle"] = " · ".join(subtitle_parts)[:80]
        image = file_public_url(block.image_file_id) if block.image_file_id else block.image_url
        if image:
            element["image_url"] = image
        if block.url:
            element["default_action"] = {"type": "web_url", "url": block.url}
        if buttons:
            element["buttons"] = buttons
        return {
            "type": "template",
            "payload": {"template_type": "generic", "elements": [element]},
        }

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        token = credentials.get("access_token", "")
        tag = payload.get("_tag")
        body: dict[str, Any] = {
            "recipient": {"id": to},
            "message": payload.get("message", {}),
        }
        if tag:
            body["messaging_type"] = "MESSAGE_TAG"
            body["tag"] = tag
        else:
            body["messaging_type"] = "RESPONSE"
        try:
            r = await self.http.post(
                f"{GRAPH_BASE}/me/messages", params={"access_token": token}, json=body
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code < 400:
            return SendResult(ok=True, external_message_id=data.get("message_id"), raw=data)
        err = data.get("error") or {}
        code = self.classify_error(r.status_code, err)
        return SendResult(
            ok=False,
            error_code=code,
            error_message=str(err.get("message") or r.text)[:500],
            raw=data,
        )

    @staticmethod
    def classify_error(status_code: int, err: dict[str, Any]) -> str:
        code = err.get("code")
        subcode = err.get("error_subcode")
        if code == 10 or subcode in (2018278, 2018108):  # outside allowed window
            return "WINDOW_EXPIRED"
        if code == 551 or subcode == 1545041:  # person unavailable / blocked
            return "INVALID_RECIPIENT"
        if code in (190, 102, 104):
            return "AUTH"
        if code in (4, 613, 32, 80006, 80001):
            return "RATE_LIMITED"
        if status_code >= 500 or code in (1, 2):
            return "RETRYABLE"
        return "PERMANENT"

    async def send_typing(
        self, account: Any, credentials: dict[str, Any], to: str, on: bool = True
    ) -> None:
        try:
            await self.http.post(
                f"{GRAPH_BASE}/me/messages",
                params={"access_token": credentials.get("access_token", "")},
                json={
                    "recipient": {"id": to},
                    "sender_action": "typing_on" if on else "typing_off",
                },
            )
        except httpx.HTTPError:
            pass

    async def mark_read(
        self,
        account: Any,
        credentials: dict[str, Any],
        *,
        external_message_id: str | None = None,
        to: str | None = None,
    ) -> None:
        if not to:
            return
        try:
            await self.http.post(
                f"{GRAPH_BASE}/me/messages",
                params={"access_token": credentials.get("access_token", "")},
                json={"recipient": {"id": to}, "sender_action": "mark_seen"},
            )
        except httpx.HTTPError:
            pass

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        try:
            r = await self.http.get(
                f"{GRAPH_BASE}/{account.external_id}",
                params={
                    "fields": "id,name",
                    "access_token": credentials.get("access_token", ""),
                },
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return HealthResult(ok=False, status="error", detail={"error": str(e)[:300]})
        if r.status_code < 400:
            return HealthResult(ok=True, status="active", detail=data)
        err = data.get("error") or {}
        status = "token_expired" if err.get("code") in (190, 102, 104) else "error"
        return HealthResult(ok=False, status=status, detail={"error": err.get("message")})

    async def fetch_profile(
        self, credentials: dict[str, Any], user_id: str
    ) -> ProfileHint | None:
        """PSID → profile (name/avatar); used opportunistically by ingest."""
        try:
            r = await self.http.get(
                f"{GRAPH_BASE}/{user_id}",
                params={
                    "fields": "first_name,last_name,profile_pic",
                    "access_token": credentials.get("access_token", ""),
                },
            )
            if r.status_code >= 400:
                return None
            data = r.json()
            name = " ".join(
                p for p in [data.get("first_name"), data.get("last_name")] if p
            )
            return ProfileHint(display_name=name or None, avatar_url=data.get("profile_pic"))
        except (httpx.HTTPError, ValueError):
            return None

    @staticmethod
    def opt_out_event(user_id: str, reason: str) -> OptOut:
        return OptOut(external_user_id=user_id, reason=reason)
