"""Instagram Messaging adapter — same Send API surface as Messenger with an
Instagram capability profile (1000-char text, image/video/audio media).
Webhook entries arrive under ``object=instagram`` and are routed by the IG
account id on ``/hooks/meta``.

Two official outbound paths (docs/channel-integration.md §4):

* **via-Page** (default) — ``graph.facebook.com/me/messages`` with a Page access
    token; the recipient is a Page-scoped id (**PSID**). Used when the IG
    professional account is linked to a Facebook Page (the standard model,
    inherited unchanged from :class:`MessengerAdapter`).
* **IG-Login** — ``graph.instagram.com/v21.0/{ig_id}/messages`` with an IG User
    access token; the recipient is an Instagram-scoped id (**IGSID**). Used for
    IG accounts connected *without* a linked Page. Selected per-account by
    ``config.ig_login == True``.

Both paths POST the identical message body (``recipient.id`` + ``message`` from
the inherited ``render()``); only the endpoint, the token and the id *namespace*
(PSID vs IGSID) differ. The ``to`` value is whatever the inbound webhook carried
as ``sender.id`` for that path, so no id translation is needed here.
"""
from __future__ import annotations

from typing import Any, ClassVar

import httpx

from ..base import SendResult
from .messenger import MessengerAdapter

# IG-Login send path (Instagram API with Instagram Login).
IG_GRAPH_BASE = "https://graph.instagram.com/v21.0"


class InstagramAdapter(MessengerAdapter):
    channel_type: ClassVar[str] = "instagram"

    @staticmethod
    def _ig_login(account: Any) -> bool:
        return bool((getattr(account, "config", None) or {}).get("ig_login"))

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        # Default: via-Page (graph.facebook.com) — unchanged Messenger send.
        if not self._ig_login(account):
            return await super().send(account, credentials, to, payload)
        return await self._ig_login_send(account, credentials, to, payload)

    async def _ig_login_send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        """IG-Login path: graph.instagram.com with the IG User token; ``to`` is
        an IGSID (not a Page-scoped PSID)."""
        token = (
            credentials.get("ig_access_token")
            or credentials.get("access_token")
            or credentials.get("page_access_token", "")
        )
        ig_id = str((account.config or {}).get("ig_id") or account.external_id)
        tag = payload.get("_tag")
        body: dict[str, Any] = {
            "recipient": {"id": to},  # IGSID
            "message": payload.get("message", {}),
        }
        if tag:
            body["messaging_type"] = "MESSAGE_TAG"
            body["tag"] = tag
        else:
            body["messaging_type"] = "RESPONSE"
        try:
            r = await self.http.post(
                f"{IG_GRAPH_BASE}/{ig_id}/messages",
                params={"access_token": token},
                json=body,
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
        return SendResult(
            ok=False,
            error_code=self.classify_error(r.status_code, err),
            error_message=str(err.get("message") or r.text)[:500],
            raw=data,
        )
