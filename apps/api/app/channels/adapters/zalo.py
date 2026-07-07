"""Zalo Official Account (OA) adapter — channel_type "zalo_app".

Inbound: OA event callbacks delivered to /hooks/zalo/{webhook_secret}. Zalo
signs every callback with X-ZEvent-Signature:

    X-ZEvent-Signature = "mac=" + sha256(appId + rawBody + timestamp + OASecretKey)

where `rawBody` is the exact request body string and `timestamp` is the event's
own timestamp field (verify_zalo_signature reproduces this, constant-time). The
common user events are user_send_text / user_send_image / user_send_file /
follow / unfollow; oa_send_* echoes of our own outbound messages are ignored.

Outbound: POST https://openapi.zalo.me/v3.0/oa/message/cs (customer-service
message replying to a user who messaged the OA). Text → {"text": …}; quick
buttons and product cards → a `list` attachment template with Zalo button
objects (oa.open.url / oa.query.show); an image → a `media` attachment
template. The Access-Token header carries the short-lived OAuth-v4 access token
(~1h); refresh_credentials exchanges the (rotating) refresh token for a fresh
access token at https://oauth.zaloapp.com/v4/oa/access_token.

Credential fields (see connect_validate): oa_id, app_id, oa_secret,
access_token, refresh_token. app_id + oa_secret are also used by
refresh_credentials, so the frontend stores them in `credentials`; oa_id is the
resolved external_id and is echoed into config.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from py_contracts.content import (
    CardButton,
    ContentBlock,
    LocationBlock,
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButton,
    QuickButtonsBlock,
    TemplateBlock,
    TextBlock,
)

from ..base import (
    BaseAdapter,
    ConnectResult,
    ContactUpdate,
    HealthResult,
    InboundEvent,
    MediaRef,
    MessageIn,
    OptOut,
    SendResult,
    degrade_content,
)
from ..media import file_public_url

OA_API = "https://openapi.zalo.me/v2.0/oa"          # getoa + media upload
MSG_API = "https://openapi.zalo.me/v3.0/oa/message"  # send (append /cs)
OAUTH_API = "https://oauth.zaloapp.com/v4/oa"        # OAuth v4 token endpoint


def zalo_mac(app_id: str, raw_body: bytes | str, timestamp: str, oa_secret: str) -> str:
    """Reproduce the X-ZEvent-Signature value: 'mac=' + sha256(appId + body +
    timestamp + OASecretKey)."""
    data = raw_body.decode("utf-8") if isinstance(raw_body, (bytes, bytearray)) else raw_body
    digest = hashlib.sha256(f"{app_id}{data}{timestamp}{oa_secret}".encode()).hexdigest()
    return f"mac={digest}"


def verify_zalo_signature(
    app_id: str, raw_body: bytes | str, timestamp: str, oa_secret: str, header: str | None
) -> bool:
    """Constant-time compare of X-ZEvent-Signature. Accepts the header with or
    without the leading 'mac=' prefix."""
    if not header:
        return False
    expected = zalo_mac(app_id, raw_body, timestamp, oa_secret)
    header = header.strip()
    return hmac.compare_digest(expected, header) or hmac.compare_digest(expected[4:], header)


class ZaloAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "zalo_app"

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        event_name = str(payload.get("event_name") or "")
        ts = payload.get("timestamp")
        occurred: datetime | None = None
        if ts:
            try:  # Zalo timestamps are epoch milliseconds (string)
                occurred = datetime.fromtimestamp(int(ts) / 1000, UTC)
            except (ValueError, TypeError):
                occurred = None

        if event_name == "follow":
            uid = (payload.get("follower") or {}).get("id")
            if uid:
                events.append(ContactUpdate(external_user_id=str(uid)))
            return events
        if event_name == "unfollow":
            uid = (payload.get("follower") or {}).get("id")
            if uid:
                events.append(OptOut(external_user_id=str(uid), reason="unfollow"))
            return events
        if not event_name.startswith("user_send"):
            # oa_send_*, user_received_message, user_seen_message … — not inbound
            return events

        sender = (payload.get("sender") or {}).get("id")
        msg = payload.get("message") or {}
        mid = msg.get("msg_id") or msg.get("message_id")
        if not sender or not mid:
            return events
        blocks, media_refs = self._parse_message_body(msg)
        if not blocks:
            return events
        events.append(
            MessageIn(
                external_message_id=str(mid),
                external_user_id=str(sender),
                content=MessageContent(blocks=blocks),
                external_timestamp=occurred,
                media_refs=media_refs,
            )
        )
        return events

    def _parse_message_body(
        self, msg: dict[str, Any]
    ) -> tuple[list[ContentBlock], list[MediaRef]]:
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        if msg.get("text"):
            blocks.append(TextBlock(text=msg["text"]))
        for att in msg.get("attachments") or []:
            atype = att.get("type")
            pay = att.get("payload") or {}
            url = pay.get("url") or pay.get("thumbnail")
            if atype in ("image", "gif"):
                blocks.append(MediaBlock(media_type="image", file_id=uuid.uuid4()))
            elif atype == "file":
                blocks.append(
                    MediaBlock(media_type="file", file_id=uuid.uuid4(), caption=pay.get("name"))
                )
            elif atype == "video":
                blocks.append(MediaBlock(media_type="video", file_id=uuid.uuid4()))
            elif atype == "audio":
                blocks.append(MediaBlock(media_type="audio", file_id=uuid.uuid4()))
            elif atype == "sticker":
                blocks.append(TextBlock(text="[sticker]"))
                continue
            elif atype == "location":
                coords = pay.get("coordinates") or {}
                lat, lon = coords.get("latitude"), coords.get("longitude")
                if lat is not None and lon is not None:
                    blocks.append(LocationBlock(latitude=float(lat), longitude=float(lon)))
                continue
            else:
                continue
            if url:
                media_refs.append(
                    MediaRef(block_index=len(blocks) - 1, ref={"kind": "url", "url": url})
                )
        return blocks, media_refs

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"text": block.text})
            elif isinstance(block, MediaBlock):
                url = file_public_url(block.file_id)
                if block.media_type == "image":
                    payloads.append(
                        {
                            "attachment": {
                                "type": "template",
                                "payload": {
                                    "template_type": "media",
                                    "elements": [{"media_type": "image", "url": url}],
                                },
                            }
                        }
                    )
                else:  # file (and any residual media) → link text
                    payloads.append({"text": f"{block.caption or 'File'}\n{url}"})
            elif isinstance(block, QuickButtonsBlock):
                payloads.append(self._buttons_template(block))
            elif isinstance(block, ProductCardBlock):
                payloads.append(self._card_template(block))
            elif isinstance(block, LocationBlock):  # safety net (location degrades to text)
                payloads.append(
                    {
                        "text": f"{block.name or 'Location'}\n"
                        f"https://maps.google.com/?q={block.latitude},{block.longitude}"
                    }
                )
            elif isinstance(block, TemplateBlock):
                payloads.append({"text": f"[template:{block.template_name}]"})
        return payloads

    def _zalo_button(self, b: QuickButton) -> dict[str, Any]:
        # oa.query.show sends `payload` back as the user's next message; use the
        # button id so the flow engine's id-based matching still resolves.
        return {
            "title": b.text[: self.capabilities.button_text_max],
            "type": "oa.query.show",
            "payload": (b.id or b.text)[:100],
        }

    def _card_button(self, b: CardButton) -> dict[str, Any]:
        if b.action == "url":
            return {
                "title": b.text[: self.capabilities.button_text_max],
                "type": "oa.open.url",
                "payload": {"url": b.value},
            }
        return {
            "title": b.text[: self.capabilities.button_text_max],
            "type": "oa.query.show",
            "payload": b.value[:100],
        }

    def _buttons_template(self, block: QuickButtonsBlock) -> dict[str, Any]:
        # Zalo has no Messenger-style quick replies; the closest is a `list`
        # attachment template whose element carries the prompt and the choices
        # render as buttons. Template validation is strict server-side — verify
        # against your OA if Zalo rejects a bare element.
        return {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "list",
                    "elements": [{"title": (block.text or "Menu")[:100]}],
                    "buttons": [self._zalo_button(b) for b in block.buttons[:5]],
                },
            }
        }

    def _card_template(self, block: ProductCardBlock) -> dict[str, Any]:
        image = file_public_url(block.image_file_id) if block.image_file_id else block.image_url
        element: dict[str, Any] = {"title": block.title[:100]}
        if block.subtitle:
            element["subtitle"] = block.subtitle[:100]
        if image:
            element["image_url"] = image
        if block.url:
            element["default_action"] = {"type": "oa.open.url", "url": block.url}
        buttons = [self._card_button(b) for b in block.buttons[:5]]
        if not buttons and block.url:
            buttons = [{"title": "Xem", "type": "oa.open.url", "payload": {"url": block.url}}]
        payload: dict[str, Any] = {"template_type": "list", "elements": [element]}
        if buttons:
            payload["buttons"] = buttons
        return {"attachment": {"type": "template", "payload": payload}}

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        token = credentials.get("access_token", "")
        body = {"recipient": {"user_id": to}, "message": payload}
        try:
            r = await self.http.post(
                f"{MSG_API}/cs",
                json=body,
                headers={"access_token": token, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        err = data.get("error")
        if r.status_code < 400 and err in (0, None):
            mid = (data.get("data") or {}).get("message_id")
            return SendResult(ok=True, external_message_id=mid, raw=data)
        code = self.classify_error(r.status_code, err)
        return SendResult(
            ok=False,
            error_code=code,
            error_message=str(data.get("message") or r.text)[:500],
            raw=data,
        )

    @staticmethod
    def classify_error(status_code: int, err: Any) -> str:
        """Map Zalo's (large) error catalogue to typed codes. The common ones:
        -216/-124 invalid/expired access token, -211 user is not reachable
        (has not messaged the OA / outside the CS window), -32/-49/-240 rate
        limits."""
        if status_code == 401 or err in (-216, -124, -215):
            return "AUTH"
        if err in (-211, -230):
            return "INVALID_RECIPIENT"
        if err in (-32, -49, -240):
            return "RATE_LIMITED"
        if err in (-201, -202, -214):
            return "PERMANENT"
        if status_code == 429:
            return "RATE_LIMITED"
        if status_code >= 500:
            return "RETRYABLE"
        return "PERMANENT"

    async def upload_media(
        self, account: Any, credentials: dict[str, Any], data: bytes, mime: str
    ) -> str | None:
        """Upload image bytes to the OA and return the attachment_id (an
        alternative to sending images by public URL). Zalo requires uploads for
        non-image files; wire this in if url-based image sends are rejected."""
        try:
            r = await self.http.post(
                f"{OA_API}/upload/image",
                files={"file": ("upload", data, mime)},
                headers={"access_token": credentials.get("access_token", "")},
            )
            body = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if r.status_code < 400 and body.get("error") in (0, None):
            return (body.get("data") or {}).get("attachment_id")
        return None

    async def _get_oa_info(self, access_token: str) -> dict[str, Any] | None:
        try:
            r = await self.http.get(f"{OA_API}/getoa", headers={"access_token": access_token})
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if r.status_code < 400 and data.get("error") in (0, None):
            return data.get("data") or {}
        return None

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        info = await self._get_oa_info(credentials.get("access_token", ""))
        if info is None:
            return HealthResult(ok=False, status="token_expired", detail={"error": "getoa failed"})
        return HealthResult(
            ok=True,
            status="active",
            detail={"oa_id": info.get("oa_id"), "name": info.get("name")},
        )

    async def refresh_credentials(
        self, account: Any, credentials: dict[str, Any]
    ) -> dict[str, Any] | None:
        """OAuth v4 refresh: exchange the (rotating) refresh_token for a fresh
        short-lived access_token. Returns the updated credential dict or None."""
        refresh_token = credentials.get("refresh_token")
        app_id = credentials.get("app_id")
        if not app_id and account is not None:
            app_id = (getattr(account, "config", None) or {}).get("app_id")
        secret_key = credentials.get("oa_secret")
        if not (refresh_token and app_id and secret_key):
            return None
        try:
            r = await self.http.post(
                f"{OAUTH_API}/access_token",
                data={
                    "refresh_token": refresh_token,
                    "app_id": str(app_id),
                    "grant_type": "refresh_token",
                },
                headers={
                    "secret_key": secret_key,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        new_access = data.get("access_token")
        if not new_access:
            return None
        updated = {**credentials, "access_token": new_access}
        if data.get("refresh_token"):  # Zalo rotates the refresh token
            updated["refresh_token"] = data["refresh_token"]
        exp = data.get("expires_in")
        if exp:
            try:
                updated["token_expires_at"] = (
                    datetime.now(UTC) + timedelta(seconds=int(exp))
                ).isoformat()
            except (ValueError, TypeError):
                pass
        return updated

    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        merged = {**config, **credentials}
        access_token = merged.get("access_token")
        oa_id = str(merged.get("oa_id") or "")
        app_id = str(merged.get("app_id") or "")
        if not access_token:
            return ConnectResult(
                external_id=oa_id,
                health=HealthResult(ok=False, status="error", detail={"error": "access_token required"}),
            )
        info = await self._get_oa_info(access_token)
        if info is None:
            return ConnectResult(
                external_id=oa_id,
                health=HealthResult(
                    ok=False,
                    status="token_expired",
                    detail={"error": "could not fetch OA info (invalid access_token?)"},
                ),
            )
        resolved_oa = str(info.get("oa_id") or oa_id)
        patch: dict[str, Any] = {}
        if app_id:
            patch["app_id"] = app_id
        if resolved_oa:
            patch["oa_id"] = resolved_oa
        return ConnectResult(
            external_id=resolved_oa,
            name=str(info.get("name") or ""),
            health=HealthResult(ok=True, status="active", detail={"name": info.get("name")}),
            config_patch=patch,
            needs_webhook_secret=True,
        )
