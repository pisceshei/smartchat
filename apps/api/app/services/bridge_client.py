"""HTTP client for the whatsmeow device bridge (bridge-wa Go sidecar).

The bridge is ONE Go process that manages many WhatsApp-App / LINE-App device
sessions, listening on ``settings.bridge_wa_url`` (compose-internal :8100),
authenticated with the shared ``X-Bridge-Auth: <bridge_api_token>`` header.

This client speaks the BRIDGE HTTP CONTRACT exactly:

    POST   /devices                       {device_id, callback_url, callback_secret}
    GET    /devices/{device_id}/qr        -> {qr, status}
    GET    /devices/{device_id}/health    -> {status, jid?, phone?, pushname?}
    POST   /devices/{device_id}/send      {to, payload} -> {ok, message_id}
    POST   /devices/{device_id}/logout    -> {ok}
    DELETE /devices/{device_id}           -> {ok}

It never raises ``httpx`` errors to callers: transport failures and non-2xx
responses become :class:`BridgeError` (``disabled=True`` when no URL is
configured) so the provisioning layer can degrade gracefully — a connect with
the bridge offline still creates the account (status ``pending``) and surfaces a
clear error instead of a 500.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..settings import Settings, get_settings

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class BridgeError(RuntimeError):
    """A device-bridge call failed. ``disabled`` marks the "no bridge configured"
    case (BRIDGE_WA_URL unset); ``status`` carries the HTTP status when the bridge
    answered with an error."""

    def __init__(self, message: str, *, status: int | None = None, disabled: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.disabled = disabled


class BridgeClient:
    """Thin async wrapper over the bridge HTTP API. The httpx client is injectable
    for tests (``httpx.MockTransport``)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self._http = http

    # -- infrastructure ----------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._http

    def _headers(self) -> dict[str, str]:
        return {"X-Bridge-Auth": self.token, "Content-Type": "application/json"}

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise BridgeError(
                "device bridge not configured (BRIDGE_WA_URL unset)", disabled=True
            )

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._require_enabled()
        url = f"{self.base_url}{path}"
        try:
            r = await self.http.request(method, url, json=json, headers=self._headers())
        except httpx.HTTPError as e:
            raise BridgeError(f"bridge unreachable: {e}", disabled=False) from e
        if r.status_code >= 400:
            body = r.text[:300]
            raise BridgeError(
                f"bridge returned {r.status_code}: {body}", status=r.status_code
            )
        try:
            data = r.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {"raw": data}

    # -- contract methods --------------------------------------------------
    async def create_device(
        self, device_id: str, *, callback_url: str, callback_secret: str
    ) -> dict[str, Any]:
        """Create + start a whatsmeow client for ``device_id`` and begin QR login
        (if no stored session). Returns ``{device_id, status}``."""
        return await self._request(
            "POST",
            "/devices",
            json={
                "device_id": device_id,
                "callback_url": callback_url,
                "callback_secret": callback_secret,
            },
        )

    async def get_qr(self, device_id: str) -> dict[str, Any]:
        """Current QR string to render (or ``qr: null`` once paired) + status."""
        return await self._request("GET", f"/devices/{device_id}/qr")

    async def get_health(self, device_id: str) -> dict[str, Any]:
        """Session health: ``{status, jid?, phone?, pushname?}``. status is one of
        awaiting_qr / connecting / online / logged_out / banned / offline."""
        return await self._request("GET", f"/devices/{device_id}/health")

    async def send(
        self, device_id: str, *, to: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a rendered ``{blocks:[...]}`` payload to ``to`` (E.164 or JID)."""
        return await self._request(
            "POST", f"/devices/{device_id}/send", json={"to": to, "payload": payload}
        )

    async def logout(self, device_id: str) -> dict[str, Any]:
        """End the session and clear the stored device (terminal — no re-pair)."""
        return await self._request("POST", f"/devices/{device_id}/logout")

    async def delete_device(self, device_id: str) -> dict[str, Any]:
        """Stop + remove the device from the bridge process."""
        return await self._request("DELETE", f"/devices/{device_id}")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()


def get_bridge_client(settings: Settings | None = None) -> BridgeClient:
    """Build a :class:`BridgeClient` from settings. Cheap to construct per use;
    a disabled client (no URL) is returned when the bridge isn't configured, and
    every call raises ``BridgeError(disabled=True)`` so callers degrade cleanly."""
    s = settings or get_settings()
    return BridgeClient(s.bridge_wa_url, s.bridge_api_token)
