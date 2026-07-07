"""WhatsApp Cloud API (Meta Graph v21) adapter.

Inbound: webhook change.value slices routed by metadata.phone_number_id
(app-level fan-in on /hooks/meta). Media ids are exchanged for a short-lived
URL (expires ~5min) and the bytes are copied to MinIO by the ingress pipeline
via fetch_media.

Outbound: POST /{phone_number_id}/messages. Quick buttons render as
interactive buttons (≤3) or an interactive list (4–10); >10 degrade to a
numbered menu. Product cards degrade (image + text + link) per the matrix.
Template sends use the `template` type; the 24h-window rule is enforced by
the sender (WINDOW_EXPIRED) and double-checked here via error mapping
(Graph error 131047 → WINDOW_EXPIRED).
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
    QuickButtonsBlock,
    TemplateBlock,
    TextBlock,
)

from ..base import (
    BaseAdapter,
    DeliveryStatus,
    HealthResult,
    InboundEvent,
    MediaFetched,
    MediaRef,
    MessageIn,
    ProfileHint,
    SendResult,
    degrade_content,
    quick_buttons_to_menu,
)
from ..media import file_public_url

GRAPH_BASE = "https://graph.facebook.com/v21.0"

_WA_MEDIA_TYPES = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "voice": "voice",  # inbound audio with voice=true
    "document": "file",
    "sticker": "sticker",
}

_OUT_MEDIA = {"image": "image", "video": "video", "audio": "audio", "voice": "audio",
              "file": "document", "sticker": "sticker"}


class WhatsAppCloudAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "whatsapp_cloud"

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        value = payload.get("value") or payload
        events: list[InboundEvent] = []
        profiles: dict[str, str] = {}
        for c in value.get("contacts", []) or []:
            wa_id = c.get("wa_id")
            name = (c.get("profile") or {}).get("name")
            if wa_id and name:
                profiles[wa_id] = name
        for m in value.get("messages", []) or []:
            ev = self._parse_message(m, profiles)
            if ev is not None:
                events.append(ev)
        for s in value.get("statuses", []) or []:
            status = s.get("status")
            if status not in ("sent", "delivered", "read", "failed"):
                continue
            errs = s.get("errors") or []
            err = errs[0] if errs else {}
            ts = s.get("timestamp")
            events.append(
                DeliveryStatus(
                    external_message_id=s.get("id", ""),
                    status=status,
                    external_user_id=s.get("recipient_id"),
                    error_code=str(err.get("code")) if err else None,
                    error_message=err.get("title") or err.get("message"),
                    occurred_at=datetime.fromtimestamp(int(ts), UTC) if ts else None,
                    meta={"pricing": s.get("pricing"), "conversation": s.get("conversation")},
                )
            )
        return events

    def _parse_message(
        self, m: dict[str, Any], profiles: dict[str, str]
    ) -> MessageIn | None:
        wa_id = m.get("from")
        ext_id = m.get("id")
        if not wa_id or not ext_id:
            return None
        mtype = m.get("type")
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        if mtype == "text":
            blocks.append(TextBlock(text=(m.get("text") or {}).get("body", "")))
        elif mtype in ("image", "video", "audio", "document", "sticker"):
            obj = m.get(mtype) or {}
            media_type = _WA_MEDIA_TYPES[mtype]
            if mtype == "audio" and obj.get("voice"):
                media_type = "voice"
            blocks.append(
                MediaBlock(
                    media_type=media_type,  # type: ignore[arg-type]
                    file_id=uuid.uuid4(),
                    caption=obj.get("caption"),
                    mime=obj.get("mime_type"),
                )
            )
            media_refs.append(
                MediaRef(
                    block_index=0,
                    ref={
                        "kind": "wa_media",
                        "media_id": obj.get("id"),
                        "mime": obj.get("mime_type"),
                        "filename": obj.get("filename"),
                    },
                )
            )
        elif mtype == "button":  # template quick-reply button
            b = m.get("button") or {}
            blocks.append(ButtonReplyBlock(payload=b.get("payload") or "", text=b.get("text") or ""))
        elif mtype == "interactive":
            i = m.get("interactive") or {}
            reply = i.get("button_reply") or i.get("list_reply") or {}
            blocks.append(
                ButtonReplyBlock(payload=reply.get("id") or "", text=reply.get("title") or "")
            )
        elif mtype == "location":
            loc = m.get("location") or {}
            blocks.append(
                LocationBlock(
                    latitude=loc.get("latitude"),
                    longitude=loc.get("longitude"),
                    name=loc.get("name"),
                    address=loc.get("address"),
                )
            )
        elif mtype == "reaction":
            emoji = (m.get("reaction") or {}).get("emoji")
            if not emoji:
                return None
            blocks.append(TextBlock(text=emoji))
        elif mtype == "contacts":
            names = [
                (c.get("name") or {}).get("formatted_name", "") for c in m.get("contacts") or []
            ]
            blocks.append(TextBlock(text="Contact: " + ", ".join(n for n in names if n)))
        else:
            blocks.append(TextBlock(text=f"[unsupported:{mtype}]"))
        ts = m.get("timestamp")
        ctx = m.get("context") or {}
        return MessageIn(
            external_message_id=ext_id,
            external_user_id=wa_id,
            content=MessageContent(blocks=blocks),
            external_timestamp=datetime.fromtimestamp(int(ts), UTC) if ts else None,
            profile=ProfileHint(display_name=profiles.get(wa_id), phone=f"+{wa_id}"),
            media_refs=media_refs,
            reply_to_external_id=ctx.get("id"),
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"type": "text", "text": {"body": block.text, "preview_url": True}})
            elif isinstance(block, MediaBlock):
                wa_type = _OUT_MEDIA.get(block.media_type, "document")
                obj: dict[str, Any] = {"link": file_public_url(block.file_id)}
                if block.caption and wa_type in ("image", "video", "document"):
                    obj["caption"] = block.caption[:1024]
                payloads.append({"type": wa_type, wa_type: obj})
            elif isinstance(block, QuickButtonsBlock):
                payloads.append(self._render_buttons(block))
            elif isinstance(block, TemplateBlock):
                tpl: dict[str, Any] = {
                    "name": block.template_name,
                    "language": {"code": block.language},
                }
                components = block.components.get("components") if isinstance(
                    block.components.get("components"), list
                ) else block.components.get("components_list")
                if components:
                    tpl["components"] = components
                elif block.components:
                    # raw dict of {component_type: params} → pass through as-is list
                    tpl["components"] = block.components.get("list", []) or [
                        v for v in block.components.values() if isinstance(v, dict)
                    ]
                payloads.append({"type": "template", "template": tpl})
            elif isinstance(block, LocationBlock):
                payloads.append(
                    {
                        "type": "location",
                        "location": {
                            "latitude": block.latitude,
                            "longitude": block.longitude,
                            "name": block.name or "",
                            "address": block.address or "",
                        },
                    }
                )
        return payloads

    def _render_buttons(self, block: QuickButtonsBlock) -> dict[str, Any]:
        n = len(block.buttons)
        if n <= 3:
            return {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": block.text[:1024]},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": b.id[:256], "title": b.text[:20]}}
                            for b in block.buttons
                        ]
                    },
                },
            }
        if n <= 10:
            return {
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": block.text[:1024]},
                    "action": {
                        "button": "Choose",
                        "sections": [
                            {
                                "title": "Options",
                                "rows": [
                                    {"id": b.id[:200], "title": b.text[:24]} for b in block.buttons
                                ],
                            }
                        ],
                    },
                },
            }
        menu = quick_buttons_to_menu(block)
        return {"type": "text", "text": {"body": menu.text}}

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        token = credentials.get("access_token", "")
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **payload}
        try:
            r = await self.http.post(
                f"{GRAPH_BASE}/{account.external_id}/messages",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code < 400:
            msgs = data.get("messages") or []
            return SendResult(
                ok=True,
                external_message_id=msgs[0].get("id") if msgs else None,
                raw=data,
            )
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
        """Graph error → typed code (plan A.7: 24h window → WINDOW_EXPIRED)."""
        code = err.get("code")
        if code in (131047, 131037):  # re-engagement required
            return "WINDOW_EXPIRED"
        if code in (131026, 131030):  # undeliverable / recipient not in allowed list
            return "INVALID_RECIPIENT"
        if code in (190, 0, 102, 104):  # token expired/invalid
            return "AUTH"
        if code in (4, 613, 80007, 130429, 131048, 131056):  # throughput / pair rate limits
            return "RATE_LIMITED"
        if code in (131052, 131053):  # media upload/download error
            return "PERMANENT"
        if code == 368:
            return "BLOCKED"
        if code == 100:
            return "PERMANENT"
        if status_code >= 500 or code in (1, 2):  # unknown/service error
            return "RETRYABLE"
        return "PERMANENT"

    async def mark_read(
        self,
        account: Any,
        credentials: dict[str, Any],
        *,
        external_message_id: str | None = None,
        to: str | None = None,
    ) -> None:
        if not external_message_id:
            return
        try:
            await self.http.post(
                f"{GRAPH_BASE}/{account.external_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": external_message_id,
                },
                headers={"Authorization": f"Bearer {credentials.get('access_token', '')}"},
            )
        except httpx.HTTPError:
            pass

    async def fetch_media(
        self, account: Any, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None:
        if ref.get("kind") != "wa_media":
            return await super().fetch_media(account, credentials, ref)
        token = credentials.get("access_token", "")
        headers = {"Authorization": f"Bearer {token}"}
        try:
            meta = await self.http.get(f"{GRAPH_BASE}/{ref.get('media_id')}", headers=headers)
            meta.raise_for_status()
            info = meta.json()
            url = info.get("url")
            if not url:
                return None
            blob = await self.http.get(url, headers=headers)
            blob.raise_for_status()
            return MediaFetched(
                data=blob.content,
                mime=info.get("mime_type") or ref.get("mime"),
                filename=ref.get("filename"),
            )
        except (httpx.HTTPError, ValueError):
            return None

    async def upload_media(
        self, account: Any, credentials: dict[str, Any], data: bytes, mime: str
    ) -> str | None:
        """Upload bytes to WA (alternative to link sends for hosts without a
        public assets URL). Returns the media id."""
        try:
            r = await self.http.post(
                f"{GRAPH_BASE}/{account.external_id}/media",
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": ("upload", data, mime)},
                headers={"Authorization": f"Bearer {credentials.get('access_token', '')}"},
            )
            r.raise_for_status()
            return r.json().get("id")
        except (httpx.HTTPError, ValueError):
            return None

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        try:
            r = await self.http.get(
                f"{GRAPH_BASE}/{account.external_id}",
                params={"fields": "display_phone_number,verified_name,quality_rating"},
                headers={"Authorization": f"Bearer {credentials.get('access_token', '')}"},
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return HealthResult(ok=False, status="error", detail={"error": str(e)[:300]})
        if r.status_code < 400:
            return HealthResult(ok=True, status="active", detail=data)
        err = (data.get("error") or {})
        status = "token_expired" if err.get("code") in (190, 102, 104) else "error"
        return HealthResult(ok=False, status=status, detail={"error": err.get("message")})
