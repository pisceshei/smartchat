"""Zalo OA webhook.

Route:
  POST /hooks/zalo/{webhook_secret}   Official Account event callback. The path
                                      secret maps to one ChannelAccount
                                      (channel_type="zalo_app").

Verification: Zalo signs each callback with X-ZEvent-Signature:
    "mac=" + sha256(appId + rawBody + timestamp + OASecretKey)
verify_zalo_signature reproduces this (constant-time). app_id comes from the
event body (fallback: account config / creds); OASecretKey from the account's
encrypted credentials (oa_secret). Handlers verify + enqueue, then 200 fast;
normalization happens in the ingress pipeline via ZaloAdapter.parse_inbound.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.adapters.zalo import verify_zalo_signature
from ...channels.creds import get_credentials
from ...channels.ingress_pipeline import enqueue_inbound
from ...db import get_session
from ...models.channels import ChannelAccount
from ...services.redis_client import get_redis

log = logging.getLogger("smartchat.hooks.zalo")

router = APIRouter(prefix="/hooks", tags=["hooks"])


@router.post("/zalo/{webhook_secret}")
async def zalo_webhook(
    webhook_secret: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    body = await request.body()
    acct = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type == "zalo_app",
                ChannelAccount.webhook_secret == webhook_secret,
            )
        )
    ).scalar_one_or_none()
    if acct is None or not acct.enabled:
        log.warning("unmatched zalo webhook secret=%s…", webhook_secret[:6])
        return Response(status_code=200)
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return Response(status_code=400, content="invalid json")

    credentials = await get_credentials(session, acct)
    # oa_secret is canonical; app_secret is a fallback for accounts connected
    # before the frontend field name was corrected.
    oa_secret = credentials.get("oa_secret") or credentials.get("app_secret") or ""
    app_id = str(
        data.get("app_id") or (acct.config or {}).get("app_id") or credentials.get("app_id") or ""
    )
    timestamp = str(data.get("timestamp") or "")
    header = request.headers.get("X-ZEvent-Signature")
    if oa_secret and not verify_zalo_signature(app_id, body, timestamp, oa_secret, header):
        raise HTTPException(status_code=403, detail="bad signature")

    await enqueue_inbound(
        get_redis(),
        account_id=acct.id,
        workspace_id=acct.workspace_id,
        channel_type=acct.channel_type,
        payload=data,
    )
    return Response(status_code=200)
