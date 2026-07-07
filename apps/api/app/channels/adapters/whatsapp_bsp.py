"""WhatsApp Business Solution Provider (BSP) proxy adapter.

An **alternative** to the direct Meta Cloud API path (``whatsapp_cloud.py``,
which stays primary and is left untouched) for customers who do not run their
own Meta app. The business connects through a BSP that fronts the WhatsApp
Business API and hands out an **API key** instead of a Meta System-User token.

``channel_type = "whatsapp_bsp"``; capabilities mirror ``whatsapp_cloud``
(24h customer window, templates, full media, interactive buttons/list). The
WhatsApp *message object* is identical across BSPs and the Cloud API (snake_case
``text``/``image``/``interactive``/``template``/``location``), so outbound
**rendering is inherited verbatim** from :class:`WhatsAppCloudAdapter`; only the
transport envelope (endpoint, auth header, ``from``/``to`` wrapper), the webhook
payload shape and connect-time number discovery differ per BSP.

Supported BSPs (``account.config["bsp"]``):

* ``ycloud`` — **fully implemented** (best-documented).
    - send:         ``POST https://api.ycloud.com/v2/whatsapp/messages`` (``X-API-Key``)
    - list numbers: ``GET  https://api.ycloud.com/v2/whatsapp/phoneNumbers``
    - webhook:      ``whatsapp.inbound_message.received`` / ``whatsapp.message.updated``
* ``chatapp`` / ``nxcloud`` / ``itnio`` — documented **stubs** (TODO). connect
    and send return a clear typed error so the operator sees exactly what is
    missing; ``parse_inbound`` returns ``[]`` for their (unrecognised) payloads.

``external_id`` is the business phone number in E.164 (YCloud routes both send
``from`` and inbound ``to`` on it). YCloud webhooks are registered once at the
BSP-app level in the YCloud console (endpoint + event subscriptions), so
``connect_validate`` does not request a per-account path secret.
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
    TextBlock,
)

from ..base import (
    Capabilities,
    ConnectResult,
    DeliveryStatus,
    HealthResult,
    InboundEvent,
    MediaRef,
    MessageIn,
    ProfileHint,
    SendResult,
    capabilities_for,
)
from .whatsapp_cloud import _WA_MEDIA_TYPES, WhatsAppCloudAdapter

YCLOUD_BASE = "https://api.ycloud.com/v2"

# BSP ids accepted at connect time. Only ``ycloud`` is wired; the rest are
# documented stubs whose transport still has to be implemented.
SUPPORTED_BSPS: frozenset[str] = frozenset({"ycloud", "chatapp", "nxcloud", "itnio"})
_STUB_BSPS: frozenset[str] = frozenset({"chatapp", "nxcloud", "itnio"})

# YCloud message status → canonical DeliveryStatus.status. "accepted" is the
# BSP-internal ack before WhatsApp reports "sent"; we surface it as "sent" so the
# operator gets immediate feedback (idempotent — a later real "sent" is a no-op).
_YC_STATUS = {
    "accepted": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


def _plus(phone: Any) -> str | None:
    """Normalise a YCloud phone value to +E.164 for the contact profile."""
    s = str(phone or "").strip()
    if not s:
        return None
    return s if s.startswith("+") else f"+{s}"


def _parse_iso(ts: Any) -> datetime | None:
    """YCloud timestamps are ISO-8601 (…Z). Return an aware UTC datetime."""
    if not ts:
        return None
    if isinstance(ts, int | float):
        return datetime.fromtimestamp(ts, UTC)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _err_message(data: dict[str, Any], fallback: str = "") -> str:
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or fallback)
    return str(data.get("message") or err or fallback)


class WhatsAppBspAdapter(WhatsAppCloudAdapter):
    channel_type: ClassVar[str] = "whatsapp_bsp"

    # BSPs front the same WhatsApp Business API — reuse the Cloud capability
    # profile so render()/degrade behave exactly like the direct path (the
    # "whatsapp_bsp" key intentionally has no separate CAPABILITIES entry).
    @property
    def capabilities(self) -> Capabilities:
        return capabilities_for("whatsapp_cloud")

    @staticmethod
    def _bsp(account: Any) -> str:
        return str((getattr(account, "config", None) or {}).get("bsp") or "ycloud").lower()

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        """Map a YCloud webhook envelope to canonical events.

        Accepts a single YCloud event (``{type, whatsappInboundMessage|
        whatsappMessage}``) or a batch under ``events``/``items``. Unrecognised
        shapes (e.g. a not-yet-implemented BSP) yield ``[]``.
        """
        if isinstance(payload.get("events"), list):
            batch = payload["events"]
        elif isinstance(payload.get("items"), list):
            batch = payload["items"]
        else:
            batch = [payload]
        out: list[InboundEvent] = []
        for ev in batch:
            if not isinstance(ev, dict):
                continue
            etype = str(ev.get("type") or "")
            if etype.startswith("whatsapp.inbound_message") or "whatsappInboundMessage" in ev:
                m = self._yc_inbound(ev.get("whatsappInboundMessage") or ev)
                if m is not None:
                    out.append(m)
            elif etype.startswith("whatsapp.message") or "whatsappMessage" in ev:
                d = self._yc_status(ev.get("whatsappMessage") or ev)
                if d is not None:
                    out.append(d)
        return out

    def _yc_inbound(self, m: dict[str, Any]) -> MessageIn | None:
        frm = m.get("from")
        ext_id = m.get("wamid") or m.get("id")
        if not frm or not ext_id:
            return None
        mtype = m.get("type")
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        if mtype == "text":
            blocks.append(TextBlock(text=(m.get("text") or {}).get("body", "")))
        elif mtype in ("image", "video", "audio", "document", "sticker"):
            obj = m.get(mtype) or {}
            media_type = _WA_MEDIA_TYPES.get(mtype, "file")
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
            link = obj.get("link")
            ref: dict[str, Any] = (
                {"kind": "url", "url": link, "filename": obj.get("filename"),
                 "mime": obj.get("mime_type")}
                if link
                else {"kind": "ycloud_media", "media_id": obj.get("id"),
                      "mime": obj.get("mime_type"), "filename": obj.get("filename")}
            )
            media_refs.append(MediaRef(block_index=len(blocks) - 1, ref=ref))
        elif mtype == "interactive":
            i = m.get("interactive") or {}
            reply = i.get("button_reply") or i.get("list_reply") or {}
            blocks.append(
                ButtonReplyBlock(payload=reply.get("id") or "", text=reply.get("title") or "")
            )
        elif mtype == "button":  # template quick-reply button
            b = m.get("button") or {}
            blocks.append(ButtonReplyBlock(payload=b.get("payload") or "", text=b.get("text") or ""))
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
        else:
            blocks.append(TextBlock(text=f"[unsupported:{mtype}]"))
        profile = m.get("customerProfile") or {}
        ctx = m.get("context") or {}
        return MessageIn(
            external_message_id=str(ext_id),
            external_user_id=str(frm),
            content=MessageContent(blocks=blocks),
            external_timestamp=_parse_iso(m.get("sendTime")),
            profile=ProfileHint(display_name=profile.get("name"), phone=_plus(frm)),
            media_refs=media_refs,
            reply_to_external_id=ctx.get("id"),
        )

    def _yc_status(self, m: dict[str, Any]) -> DeliveryStatus | None:
        status = _YC_STATUS.get(str(m.get("status") or ""))
        ext_id = m.get("wamid") or m.get("id")
        if status is None or not ext_id:
            return None
        wa_err = m.get("whatsappApiError") or {}
        err_code = m.get("errorCode")
        if err_code is None and wa_err:
            err_code = wa_err.get("code")
        ts = m.get("readTime") or m.get("deliverTime") or m.get("sendTime")
        return DeliveryStatus(
            external_message_id=str(ext_id),
            status=status,  # type: ignore[arg-type]
            external_user_id=str(m.get("recipientUserId") or m.get("to") or "") or None,
            error_code=str(err_code) if err_code is not None else None,
            error_message=m.get("errorMessage") or wa_err.get("message"),
            occurred_at=_parse_iso(ts),
            meta={"bsp": "ycloud"},
        )

    # -- outbound ----------------------------------------------------------
    # render() is inherited from WhatsAppCloudAdapter — the WhatsApp message
    # object (type + text/image/interactive/template/location) is BSP-agnostic.
    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        bsp = self._bsp(account)
        if bsp == "ycloud":
            return await self._ycloud_send(account, credentials, to, payload)
        return self._stub_send(bsp)

    async def _ycloud_send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        api_key = credentials.get("api_key", "")
        body = {"from": account.external_id, "to": to, **payload}
        try:
            r = await self.http.post(
                f"{YCLOUD_BASE}/whatsapp/messages",
                json=body,
                headers={"X-API-Key": api_key},
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code < 400:
            # prefer the WhatsApp message id (wamid) so it lines up with the
            # id carried by whatsapp.message.updated delivery webhooks.
            mid = data.get("wamid") or data.get("id")
            return SendResult(ok=True, external_message_id=str(mid) if mid else None, raw=data)
        return SendResult(
            ok=False,
            error_code=self._ycloud_error_code(r.status_code, data),
            error_message=_err_message(data, r.text)[:500],
            raw=data,
        )

    def _ycloud_error_code(self, status_code: int, data: dict[str, Any]) -> str:
        """Map a YCloud send error to a typed code.

        When YCloud forwarded the request to the WhatsApp Business API and got a
        Graph error back it echoes it under ``error.whatsappApiError`` — reuse
        the Cloud-API classifier (24h window → WINDOW_EXPIRED). Otherwise fall
        back to the HTTP status of the YCloud call itself.
        """
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        wa_err = err.get("whatsappApiError") or data.get("whatsappApiError")
        if isinstance(wa_err, dict) and wa_err.get("code") is not None:
            return self.classify_error(status_code, wa_err)
        if status_code in (401, 403):
            return "AUTH"
        if status_code == 429:
            return "RATE_LIMITED"
        if status_code == 404:
            return "INVALID_RECIPIENT"
        if status_code >= 500:
            return "RETRYABLE"
        return "PERMANENT"

    def _stub_send(self, bsp: str) -> SendResult:
        # TODO: implement the {bsp} transport (endpoint, auth header, from/to
        # envelope). Until then fail non-retryably with a clear message.
        return SendResult(
            ok=False,
            error_code="PERMANENT",
            error_message=(
                f"BSP '{bsp}' send not implemented — only 'ycloud' is wired "
                f"(chatapp/nxcloud/itnio are documented stubs)"
            ),
        )

    async def mark_read(
        self,
        account: Any,
        credentials: dict[str, Any],
        *,
        external_message_id: str | None = None,
        to: str | None = None,
    ) -> None:
        # No separate read-receipt endpoint on the BSP path; override the
        # inherited Cloud implementation so we never hit graph.facebook.com.
        return None

    # -- connect / health --------------------------------------------------
    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        bsp = str(config.get("bsp") or "ycloud").lower()
        if bsp not in SUPPORTED_BSPS:
            return self._connect_error(f"unknown BSP '{bsp}' — supported: {sorted(SUPPORTED_BSPS)}")
        api_key = credentials.get("api_key", "")
        if not api_key:
            return self._connect_error("api_key required")
        if bsp == "ycloud":
            return await self._ycloud_connect(config, api_key)
        return self._stub_connect(bsp)

    async def _ycloud_connect(self, config: dict[str, Any], api_key: str) -> ConnectResult:
        try:
            r = await self.http.get(
                f"{YCLOUD_BASE}/whatsapp/phoneNumbers",
                params={"limit": 100},
                headers={"X-API-Key": api_key},
            )
        except httpx.HTTPError as e:
            return self._connect_error(str(e)[:300])
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            status = "token_expired" if r.status_code in (401, 403) else "error"
            return self._connect_error(_err_message(data, r.text)[:300], status=status)
        items = data.get("items") or []
        if not items:
            return self._connect_error("no WhatsApp phone numbers on this BSP account")
        want = str(config.get("phone_number") or config.get("external_id") or "").strip()
        chosen = None
        if want:
            for it in items:
                if str(it.get("phoneNumber")) == want or str(it.get("id")) == want:
                    chosen = it
                    break
        chosen = chosen or items[0]
        external_id = str(chosen.get("phoneNumber") or chosen.get("id") or "").strip()
        if not external_id:
            return self._connect_error("could not resolve a phone number from the BSP account")
        name = str(chosen.get("verifiedName") or chosen.get("displayName") or "")
        return ConnectResult(
            external_id=external_id,
            name=name,
            health=HealthResult(
                ok=True,
                status="active",
                detail={
                    "bsp": "ycloud",
                    "waba_id": chosen.get("wabaId"),
                    "quality": chosen.get("qualityRating"),
                    "numbers": len(items),
                },
            ),
            config_patch={"bsp": "ycloud", "waba_id": chosen.get("wabaId")},
            needs_webhook_secret=False,  # YCloud webhook is BSP-app level, no path secret
        )

    def _stub_connect(self, bsp: str) -> ConnectResult:
        return self._connect_error(
            f"BSP '{bsp}' not implemented — supported: ycloud "
            f"(chatapp/nxcloud/itnio are documented stubs)"
        )

    @staticmethod
    def _connect_error(msg: str, *, status: str = "error") -> ConnectResult:
        return ConnectResult(
            external_id="",
            health=HealthResult(ok=False, status=status, detail={"error": msg}),
        )

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        bsp = self._bsp(account)
        if bsp != "ycloud":
            return HealthResult(ok=False, status="error", detail={"error": f"BSP '{bsp}' not implemented"})
        api_key = credentials.get("api_key", "")
        try:
            r = await self.http.get(
                f"{YCLOUD_BASE}/whatsapp/phoneNumbers",
                params={"limit": 1},
                headers={"X-API-Key": api_key},
            )
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return HealthResult(ok=False, status="error", detail={"error": str(e)[:300]})
        if r.status_code < 400:
            return HealthResult(ok=True, status="active", detail={"bsp": "ycloud"})
        status = "token_expired" if r.status_code in (401, 403) else "error"
        return HealthResult(ok=False, status=status, detail={"error": _err_message(data, r.text)[:200]})
