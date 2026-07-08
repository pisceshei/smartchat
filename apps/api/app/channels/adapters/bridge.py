"""Hosted-device bridge adapter (WhatsApp App / LINE App personal accounts).

Bridges (whatsmeow etc., one container per device — plan A.3) POST
pre-normalized InboundEvent lists to /hooks/bridge/{webhook_secret} signed
with X-Bridge-Signature = hex(hmac_sha256(webhook_secret, body)). Outbound is
relayed to the bridge container's local API (account.config["bridge_url"]).

Rendering degrades to the generic block payload — personal accounts have no
buttons/cards, so the capability matrix (whatsapp_app/line_app) collapses
rich content to text+media before the bridge sees it. Rates are humanized
(6–10/min jitter) by the sender's token bucket.
"""
from __future__ import annotations

from typing import Any, ClassVar

import httpx
from py_contracts.content import MessageContent

from ..base import (
    BaseAdapter,
    HealthResult,
    SendResult,
    bridge_signature,
    degrade_content,
    verify_bridge_signature,
)


class BridgeAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "whatsapp_app"

    def __init__(self, channel_type: str, http: httpx.AsyncClient | None = None):
        super().__init__(http)
        # instance attribute shadows the ClassVar on purpose (two instances
        # cover whatsapp_app and line_app)
        self.channel_type = channel_type  # type: ignore[misc]

    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> bool:
        header = headers.get("x-bridge-signature") or headers.get("X-Bridge-Signature")
        return verify_bridge_signature(secret, body, header)

    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities)
        return [{"blocks": degraded.model_dump(mode="json")["blocks"]}]

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        bridge_url = (account.config or {}).get("bridge_url", "")
        if not bridge_url:
            return SendResult(
                ok=False,
                error_code="BRIDGE_OFFLINE",
                error_message="bridge container not configured (config.bridge_url missing)",
            )
        secret = credentials.get("bridge_token") or account.config.get("webhook_secret") or ""
        import json as _json

        from ...settings import get_settings

        body = _json.dumps({"to": to, "payload": payload}, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            # per-message HMAC (device-scoped secret) …
            "X-Bridge-Signature": bridge_signature(secret, body),
        }
        # … plus the shared bridge API token the Go /send endpoint authenticates on
        api_token = get_settings().bridge_api_token
        if api_token:
            headers["X-Bridge-Auth"] = api_token
        try:
            r = await self.http.post(
                f"{bridge_url.rstrip('/')}/send",
                content=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code < 400 and data.get("ok", True):
            return SendResult(ok=True, external_message_id=data.get("message_id"), raw=data)
        if r.status_code in (401, 403):
            return SendResult(ok=False, error_code="AUTH", error_message=r.text[:300], raw=data)
        if r.status_code == 409 and data.get("status") in ("logged_out", "banned"):
            return SendResult(
                ok=False, error_code="AUTH", error_message=str(data.get("status")), raw=data
            )
        if r.status_code >= 500:
            return SendResult(ok=False, error_code="RETRYABLE", error_message=r.text[:300], raw=data)
        return SendResult(ok=False, error_code="PERMANENT", error_message=r.text[:300], raw=data)

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        bridge_url = (account.config or {}).get("bridge_url", "")
        if not bridge_url:
            return HealthResult(ok=False, status="disconnected", detail={"error": "no bridge_url"})
        try:
            r = await self.http.get(f"{bridge_url.rstrip('/')}/health")
            data = r.json() if r.status_code < 500 else {}
            status = data.get("status", "online" if r.status_code < 400 else "error")
            return HealthResult(ok=r.status_code < 400, status=status, detail=data)
        except (httpx.HTTPError, ValueError) as e:
            return HealthResult(ok=False, status="disconnected", detail={"error": str(e)[:300]})
