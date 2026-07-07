"""Public webhook ingress (plan A.7 入口).

Handlers do exactly three things: verify the signature, XADD the raw payload
to the ingress:{channel_type} Redis Stream, and return 200 immediately (Meta
requires <5s). Normalization/persistence happens in the ingress pipeline
consumer. Unmatched accounts are logged and still get a 200 so the platform
doesn't disable the webhook (plan A.9: Meta app-level fan-in).

Routes:
  GET  /hooks/meta                       hub.challenge verification
  POST /hooks/meta                       WA Cloud + Messenger + IG (app-level,
                                         X-Hub-Signature-256, routed by
                                         phone_number_id / page id / IG id)
  POST /hooks/telegram/{webhook_secret}  Telegram Bot API updates
  POST /hooks/line/{webhook_secret}      LINE Messaging API (X-Line-Signature)
  POST /hooks/bridge/{webhook_secret}    internal device bridges (HMAC)
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# importing the sender wires the channel tasks/crons into the jobs registry
from ...channels import sender as _sender  # noqa: F401
from ...channels.base import (
    verify_bridge_signature,
    verify_line_signature,
    verify_meta_signature,
)
from ...channels.creds import get_credentials
from ...channels.ingress_pipeline import enqueue_inbound
from ...db import get_session
from ...models.channels import ChannelAccount
from ...services.redis_client import get_redis
from ...settings import get_settings

log = logging.getLogger("smartchat.hooks")

router = APIRouter(prefix="/hooks", tags=["hooks"])

_META_OBJECT_CHANNEL = {
    "whatsapp_business_account": "whatsapp_cloud",
    "page": "messenger",
    "instagram": "instagram",
}


async def _account_by_external(
    session: AsyncSession, channel_type: str, external_id: str
) -> ChannelAccount | None:
    return (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type == channel_type,
                ChannelAccount.external_id == external_id,
            )
        )
    ).scalar_one_or_none()


async def _account_by_secret(
    session: AsyncSession, channel_types: list[str], secret: str
) -> ChannelAccount | None:
    if not secret:
        return None
    return (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type.in_(channel_types),
                ChannelAccount.webhook_secret == secret,
            )
        )
    ).scalar_one_or_none()


async def _enqueue(acct: ChannelAccount, payload: dict) -> None:
    await enqueue_inbound(
        get_redis(),
        account_id=acct.id,
        workspace_id=acct.workspace_id,
        channel_type=acct.channel_type,
        payload=payload,
    )


# --------------------------------------------------------------------------
# Meta (WhatsApp Cloud + Messenger + Instagram)
# --------------------------------------------------------------------------
@router.get("/meta")
async def meta_verify(request: Request) -> Response:
    """Webhook subscription handshake: echo hub.challenge if the verify token
    matches META_VERIFY_TOKEN."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == get_settings().meta_verify_token
        and params.get("hub.challenge") is not None
    ):
        return PlainTextResponse(params["hub.challenge"])
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/meta")
async def meta_webhook(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    body = await request.body()
    secret = get_settings().meta_app_secret
    if secret:
        header = request.headers.get("X-Hub-Signature-256")
        if not verify_meta_signature(secret, body, header):
            raise HTTPException(status_code=403, detail="bad signature")
    else:  # dev without META_APP_SECRET
        log.warning("META_APP_SECRET unset — accepting unsigned webhook (dev only)")
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json") from None

    channel_type = _META_OBJECT_CHANNEL.get(data.get("object", ""))
    if channel_type is None:
        log.info("meta webhook with unhandled object=%s", data.get("object"))
        return {"ok": True}

    for entry in data.get("entry", []) or []:
        if channel_type == "whatsapp_cloud":
            for change in entry.get("changes", []) or []:
                value = change.get("value") or {}
                pnid = (value.get("metadata") or {}).get("phone_number_id")
                if not pnid:
                    continue
                acct = await _account_by_external(session, "whatsapp_cloud", str(pnid))
                if acct is None or not acct.enabled:
                    log.warning("unmatched whatsapp_cloud webhook phone_number_id=%s", pnid)
                    continue
                await _enqueue(acct, {"field": change.get("field"), "value": value})
        else:  # messenger / instagram: entry.id = page id / IG account id
            ext_id = str(entry.get("id", ""))
            messaging = entry.get("messaging") or entry.get("standby") or []
            if not ext_id or not messaging:
                continue
            acct = await _account_by_external(session, channel_type, ext_id)
            if acct is None or not acct.enabled:
                log.warning("unmatched %s webhook id=%s", channel_type, ext_id)
                continue
            await _enqueue(acct, {"messaging": messaging})
    return {"ok": True}


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
@router.post("/telegram/{webhook_secret}")
async def telegram_webhook(
    webhook_secret: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    acct = await _account_by_secret(session, ["telegram_bot"], webhook_secret)
    if acct is None or not acct.enabled:
        log.warning("unmatched telegram webhook secret=%s…", webhook_secret[:6])
        return {"ok": True}
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header is not None and header != webhook_secret:
        raise HTTPException(status_code=403, detail="bad secret token")
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    await _enqueue(acct, payload)
    return {"ok": True}


# --------------------------------------------------------------------------
# LINE
# --------------------------------------------------------------------------
@router.post("/line/{webhook_secret}")
async def line_webhook(
    webhook_secret: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    body = await request.body()
    acct = await _account_by_secret(session, ["line_oa"], webhook_secret)
    if acct is None or not acct.enabled:
        log.warning("unmatched line webhook secret=%s…", webhook_secret[:6])
        return {"ok": True}
    credentials = await get_credentials(session, acct)
    channel_secret = credentials.get("channel_secret", "")
    if not verify_line_signature(channel_secret, body, request.headers.get("X-Line-Signature")):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    await _enqueue(acct, payload)
    return {"ok": True}


# --------------------------------------------------------------------------
# device bridges (internal: WhatsApp App / LINE App containers)
# --------------------------------------------------------------------------
@router.post("/bridge/{webhook_secret}")
async def bridge_webhook(
    webhook_secret: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    body = await request.body()
    acct = await _account_by_secret(session, ["whatsapp_app", "line_app"], webhook_secret)
    if acct is None or not acct.enabled:
        log.warning("unmatched bridge webhook secret=%s…", webhook_secret[:6])
        return {"ok": True}
    if not verify_bridge_signature(
        webhook_secret, body, request.headers.get("X-Bridge-Signature")
    ):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    await _enqueue(acct, payload)
    return {"ok": True}
