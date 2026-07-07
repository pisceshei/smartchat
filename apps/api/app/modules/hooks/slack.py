"""Slack Events API webhook.

Route:
  POST /hooks/slack   app-level Events API callback (single URL for the Slack
                      app; routed to the connected account by team_id).

Flow:
  1. Verify the request signature BEFORE trusting the body:
       basestring = "v0:" + X-Slack-Request-Timestamp + ":" + raw_body
       expected   = "v0=" + hmac_sha256(settings.slack_signing_secret, basestring)
     constant-time compare vs X-Slack-Signature, rejecting stale timestamps (>5m).
     One signing secret per Slack app (settings.slack_signing_secret) signs every
     workspace's events on this shared URL.
  2. url_verification handshake → echo the challenge verbatim (plain text).
  3. event_callback → resolve the ChannelAccount by team_id and enqueue the raw
     payload; return 200 within 3s. Slack retries (X-Slack-Retry-Num) resend the
     same event_id, so the ingress dedup makes redelivery idempotent.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.adapters.slack import verify_slack_signature
from ...db import get_session
from ...settings import get_settings
from .router import _account_by_external, _enqueue

log = logging.getLogger("smartchat.hooks.slack")

router = APIRouter(prefix="/hooks", tags=["hooks"])


@router.post("/slack")
async def slack_webhook(
    request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    body = await request.body()
    secret = get_settings().slack_signing_secret
    if secret:
        if not verify_slack_signature(
            secret,
            body,
            timestamp=request.headers.get("X-Slack-Request-Timestamp"),
            signature=request.headers.get("X-Slack-Signature"),
        ):
            raise HTTPException(status_code=403, detail="bad signature")
    else:  # dev without SLACK_SIGNING_SECRET
        log.warning("SLACK_SIGNING_SECRET unset — accepting unsigned webhook (dev only)")

    try:
        data = json.loads(body) if body else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json") from None

    # URL verification handshake — echo the challenge so the app can be saved.
    if data.get("type") == "url_verification":
        return Response(content=data.get("challenge", ""), media_type="text/plain")

    if data.get("type") == "event_callback":
        team_id = str(data.get("team_id") or "")
        acct = await _account_by_external(session, "slack", team_id) if team_id else None
        if acct is None or not acct.enabled:
            log.warning("unmatched slack webhook team_id=%s", team_id)
            return Response(status_code=200)
        await _enqueue(acct, data)
    return Response(status_code=200)
