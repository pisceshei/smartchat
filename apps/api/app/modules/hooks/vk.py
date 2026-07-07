"""VKontakte Callback API webhook.

Route:
  POST /hooks/vk/{webhook_secret}   per-community Callback API endpoint. The path
                                    secret maps to one ChannelAccount (type "vk").

Flow (VK requires the response body to be exactly "ok" for every non-confirmation
event, or it keeps retrying / disables the callback):
  * type == "confirmation" → return the community's confirmation string as PLAIN
    TEXT (per-account ChannelAccount.config["confirmation_string"], falling back
    to settings.vk_confirmation_default before the account is fully set up).
  * otherwise → verify the body "secret" field against the community's stored
    callback secret (constant-time); on mismatch drop the event but still reply
    "ok". message_new / message_deny are enqueued to the ingress pipeline.

ALTERNATIVE (not used): VK Bots Long Poll — poll groups.getLongPollServer + a
blocking a_check loop instead of a callback URL; needs no public URL, confirmation
string or secret, and yields the same message_new objects the adapter parses.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.base import secrets_equal
from ...channels.creds import get_credentials
from ...db import get_session
from ...settings import get_settings
from .router import _account_by_secret, _enqueue

log = logging.getLogger("smartchat.hooks.vk")

router = APIRouter(prefix="/hooks", tags=["hooks"])

# events forwarded to the ingress pipeline (mirrors vk adapter _ENQUEUE_TYPES).
_ENQUEUE_TYPES = {"message_new", "message_deny"}


def _ok() -> Response:
    return Response(content="ok", media_type="text/plain")


@router.post("/vk/{webhook_secret}")
async def vk_webhook(
    webhook_secret: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        # VK expects "ok"; a malformed body won't be fixed by retrying.
        return _ok()

    acct = await _account_by_secret(session, ["vk"], webhook_secret)

    # confirmation handshake — echo the community confirmation string (plain text).
    if data.get("type") == "confirmation":
        conf = ""
        if acct is not None:
            conf = str((acct.config or {}).get("confirmation_string") or "")
        return Response(
            content=conf or get_settings().vk_confirmation_default, media_type="text/plain"
        )

    if acct is None or not acct.enabled:
        log.warning("unmatched vk webhook secret=%s…", webhook_secret[:6])
        return _ok()

    # verify the community callback secret (VK includes it in every event body).
    creds = await get_credentials(session, acct)
    expected = str(creds.get("secret") or (acct.config or {}).get("secret") or "")
    if expected and not secrets_equal(str(data.get("secret") or ""), expected):
        log.warning("vk webhook bad secret for account=%s", acct.id)
        return _ok()

    if data.get("type") in _ENQUEUE_TYPES:
        await _enqueue(acct, data)
    return _ok()
