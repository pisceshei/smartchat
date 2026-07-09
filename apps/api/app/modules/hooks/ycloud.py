"""YCloud BSP webhook (app-level fan-in, plan A.7/A.9 pattern).

Route:
  POST /hooks/ycloud   one URL per deployment. Registered automatically at
                       connect (POST /webhookEndpoints) or pasted manually
                       into the YCloud console (Developers → Webhook).

One YCloud account (one API key, one endpoint) fronts MANY numbers/channel
accounts, so there is no per-account path secret — each event is routed by the
business phone number it carries: inbound → ``whatsappInboundMessage.to``,
delivery status → ``whatsappMessage.from`` (we send with from=external_id, so
YCloud echoes it back); ``whatsapp.template.reviewed`` routes by wabaId.

Signature: header ``YCloud-Signature: t={unix_ts},s={hex_hmac}`` where
``s = hmac_sha256(endpoint_secret, f"{t}.{raw_body}")``. The endpoint secret is
stored per account in encrypted credentials["webhook_secret"] (every sibling
account sharing the API key stores the same secret). Fail-closed (403) when a
secret is stored; log-warn accept when none is (mirrors the meta_webhook
"META_APP_SECRET unset" precedent) so manual-console setups keep working until
the operator pastes the secret into the connect modal.

Unmatched/disabled accounts return 200 {ok:true} with a loud log — YCloud
retries 7× and suspends endpoints with high failure rates.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.adapters.whatsapp_bsp import _plus
from ...channels.creds import get_credentials
from ...db import get_session
from ...marketing import ycloud_templates
from ...models.channels import ChannelAccount
from .router import _account_by_external, _enqueue

log = logging.getLogger("smartchat.hooks.ycloud")

router = APIRouter(prefix="/hooks", tags=["hooks"])

_INGRESS_TYPES = ("whatsapp.inbound_message.received", "whatsapp.message.updated")
SIGNATURE_TOLERANCE_S = 300


def verify_ycloud_signature(
    secret: str, body: bytes, header: str | None, *, now: float | None = None
) -> bool:
    """YCloud-Signature: 't={unix_ts},s={hex_hmac}'; hmac_sha256(secret, f'{t}.{body}').
    Constant-time compare; stale timestamps (±SIGNATURE_TOLERANCE_S) rejected."""
    if not secret or not header:
        return False
    parts: dict[str, str] = {}
    for p in header.split(","):
        if "=" in p:
            k, v = p.strip().split("=", 1)
            parts[k] = v
    t, s = parts.get("t"), parts.get("s")
    if not t or not s:
        return False
    try:
        ts = int(t)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, s.strip())


async def _account_by_number(session: AsyncSession, num: Any) -> ChannelAccount | None:
    """external_id stores +E.164 but YCloud event numbers may come bare —
    try the normalized form first, then verbatim."""
    if not num:
        return None
    acct = await _account_by_external(session, "whatsapp_bsp", _plus(num) or "")
    return acct or await _account_by_external(session, "whatsapp_bsp", str(num))


async def _accounts_by_waba(session: AsyncSession, waba_id: Any) -> list[ChannelAccount]:
    """ALL enabled whatsapp_bsp accounts on this WABA. One WABA fronts many
    numbers (and may span workspaces on an agency's shared YCloud key), so a
    template-review event must reach every workspace using it — never an
    arbitrary single row."""
    if not waba_id:
        return []
    return list(
        (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.channel_type == "whatsapp_bsp",
                    ChannelAccount.enabled.is_(True),
                    ChannelAccount.config["waba_id"].astext == str(waba_id),
                )
            )
        ).scalars()
    )


def _sig_ok(secret: str, body: bytes, header: str | None) -> bool:
    return bool(secret) and verify_ycloud_signature(secret, body, header)


@router.post("/ycloud")
async def ycloud_webhook(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    body = await request.body()
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid json") from None
    if not isinstance(data, dict):
        raise HTTPException(400, "invalid payload")
    etype = str(data.get("type") or "")
    header = request.headers.get("YCloud-Signature")

    # --- conversation events: routed by the (unique) business phone number ---
    if etype in _INGRESS_TYPES:
        if etype.startswith("whatsapp.inbound_message"):
            num = (data.get("whatsappInboundMessage") or {}).get("to")
        else:
            num = (data.get("whatsappMessage") or {}).get("from")
        acct = await _account_by_number(session, num)
        if acct is None or not acct.enabled:
            log.warning("ycloud webhook DROPPED: no enabled account for %s (%s)", etype, num)
            return {"ok": True}
        secret = str((await get_credentials(session, acct)).get("webhook_secret") or "")
        if secret:
            if not _sig_ok(secret, body, header):
                # fail-closed but 200-drop (not 403): matches the platform
                # convention and denies an unauthenticated presence/signature
                # oracle; YCloud retries only on non-2xx.
                log.warning("ycloud webhook bad signature (dropped) account=%s", acct.id)
                return {"ok": True}
        else:
            log.warning(
                "ycloud webhook UNSIGNED-ACCEPT account=%s (no stored webhook_secret — "
                "re-connect with the endpoint secret to enforce signatures)",
                acct.id,
            )
        # raw envelope is exactly what parse_inbound consumes — no reshaping
        await _enqueue(acct, data)
        return {"ok": True}

    # --- template review: fan out to EVERY workspace on this WABA ---
    if etype == "whatsapp.template.reviewed":
        tpl = data.get("whatsappTemplate") or {}
        accts = await _accounts_by_waba(session, tpl.get("wabaId"))
        if not accts:
            log.warning("ycloud template.reviewed: no enabled account for wabaId=%s", tpl.get("wabaId"))
            return {"ok": True}
        # verify against ANY sibling that stores a secret; fail-closed if at
        # least one does (they share the YCloud endpoint secret)
        secrets = [
            s
            for a in accts
            if (s := str((await get_credentials(session, a)).get("webhook_secret") or ""))
        ]
        if secrets:
            if not any(_sig_ok(s, body, header) for s in secrets):
                log.warning("ycloud template.reviewed bad signature (dropped) wabaId=%s", tpl.get("wabaId"))
                return {"ok": True}
        else:
            log.warning("ycloud template.reviewed UNSIGNED-ACCEPT wabaId=%s", tpl.get("wabaId"))
        for ws_id in {a.workspace_id for a in accts}:
            await ycloud_templates.apply_template_review(session, workspace_id=ws_id, event=tpl)
        await session.commit()
        return {"ok": True}

    log.info("ycloud webhook unhandled type=%s", etype)
    return {"ok": True}
