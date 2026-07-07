"""TikTok Business webhook.

Route:
  POST /hooks/tiktok/{webhook_secret}   business-account event callback. The path
                                        secret maps to one ChannelAccount
                                        (channel_type="tiktok_business").

⚠️ Verification is best-effort. TikTok does not publish a stable HMAC scheme for
Business comment webhooks, so the primary protection is the unguessable
per-account path secret. When TikTok DOES send a signature header and the
platform app secret (settings.tiktok_client_secret) is configured, we
additionally verify HMAC-SHA256(client_secret, timestamp + rawBody) and reject
on mismatch; otherwise we accept (path-secret-gated) and enqueue. Comment/DM
delivery is allow-listed — see TikTokBusinessAdapter for the honest limits.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.ingress_pipeline import enqueue_inbound
from ...db import get_session
from ...models.channels import ChannelAccount
from ...services.redis_client import get_redis
from ...settings import get_settings

log = logging.getLogger("smartchat.hooks.tiktok")

router = APIRouter(prefix="/hooks", tags=["hooks"])


def _verify_optional_signature(body: bytes, header: str | None, timestamp: str, secret: str) -> bool:
    """Best-effort TikTok signature check (only enforced when both a signature
    header and the platform app secret are present)."""
    if not header or not secret:
        return True
    base = f"{timestamp}{body.decode('utf-8')}".encode()
    expected = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    candidate = header.strip()
    if candidate.startswith("sha256="):
        candidate = candidate[7:]
    return hmac.compare_digest(expected, candidate)


@router.post("/tiktok/{webhook_secret}")
async def tiktok_webhook(
    webhook_secret: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    body = await request.body()
    acct = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type == "tiktok_business",
                ChannelAccount.webhook_secret == webhook_secret,
            )
        )
    ).scalar_one_or_none()
    if acct is None or not acct.enabled:
        log.warning("unmatched tiktok webhook secret=%s…", webhook_secret[:6])
        return Response(status_code=200)
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        return Response(status_code=400, content="invalid json")

    header = request.headers.get("X-Tiktok-Signature") or request.headers.get("X-TT-Signature")
    timestamp = request.headers.get("X-Tiktok-Timestamp") or str(data.get("create_time") or "")
    if not _verify_optional_signature(body, header, timestamp, get_settings().tiktok_client_secret):
        raise HTTPException(status_code=403, detail="bad signature")

    await enqueue_inbound(
        get_redis(),
        account_id=acct.id,
        workspace_id=acct.workspace_id,
        channel_type=acct.channel_type,
        payload=data,
    )
    return Response(status_code=200)
