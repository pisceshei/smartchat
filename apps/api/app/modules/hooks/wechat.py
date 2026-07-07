"""WeChat 微信客服 / WeCom 企業微信 callback webhook.

One route pair serves both channels; the per-account ``webhook_secret`` in the
path identifies the ChannelAccount (channel_type ∈ {"wecom", "wechat_kf"}) and
therefore which adapter/flow to run — no need to route by the decrypted
ToUserName. WeChat verification is AES + signature based (msg_signature =
sha1(sorted(token, timestamp, nonce, encrypt))), not a bearer token.

  GET  /hooks/wechat/{webhook_secret}   server-URL handshake: verify + decrypt
                                        the (encrypted) echostr, echo plaintext.
  POST /hooks/wechat/{webhook_secret}   AES callback:
      * wecom     → decrypt inner <xml> message, enqueue for parse_inbound.
      * wechat_kf → decrypt the event (carries a sync Token), kick off the
        sync_msg cursor loop in the background; return "success" immediately.

WeChat treats any response other than a 200 body of "success" (or the echoed
echostr) as failure and retries — which is harmless (dedup + cursor make it
idempotent).
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.adapters.wechat_kf import sync_kf_messages
from ...channels.adapters.wecom import xml_to_dict
from ...channels.creds import get_credentials
from ...channels.ingress_pipeline import enqueue_inbound
from ...channels.wechat_crypto import WeChatCryptError, WXBizMsgCrypt
from ...db import get_session, session_factory
from ...models.channels import ChannelAccount
from ...services.redis_client import get_redis

log = logging.getLogger("smartchat.hooks.wechat")

router = APIRouter(prefix="/hooks", tags=["hooks"])

_WECHAT_TYPES = ["wecom", "wechat_kf"]


async def _account_by_secret(session: AsyncSession, secret: str) -> ChannelAccount | None:
    if not secret:
        return None
    return (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type.in_(_WECHAT_TYPES),
                ChannelAccount.webhook_secret == secret,
            )
        )
    ).scalar_one_or_none()


async def _crypto_for(session: AsyncSession, acct: ChannelAccount) -> WXBizMsgCrypt:
    creds = await get_credentials(session, acct)
    corp_id = str((acct.config or {}).get("corp_id") or "")
    return WXBizMsgCrypt(
        token=str(creds.get("token") or ""),
        encoding_aes_key=str(creds.get("encoding_aes_key") or ""),
        receive_id=corp_id,
    )


@router.get("/wechat/{webhook_secret}")
async def wechat_verify(
    webhook_secret: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    acct = await _account_by_secret(session, webhook_secret)
    if acct is None or not acct.enabled:
        log.warning("wechat GET verify for unknown secret=%s…", webhook_secret[:6])
        return PlainTextResponse("", status_code=200)
    params = request.query_params
    try:
        crypto = await _crypto_for(session, acct)
        plain = crypto.verify_url(
            params.get("msg_signature", ""),
            params.get("timestamp", ""),
            params.get("nonce", ""),
            params.get("echostr", ""),
        )
    except WeChatCryptError as e:
        log.warning("wechat GET verify failed: %s", e)
        return PlainTextResponse("", status_code=403)
    return PlainTextResponse(plain)


@router.post("/wechat/{webhook_secret}")
async def wechat_webhook(
    webhook_secret: str,
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> Response:
    body = await request.body()
    acct = await _account_by_secret(session, webhook_secret)
    if acct is None or not acct.enabled:
        log.warning("wechat POST for unknown secret=%s…", webhook_secret[:6])
        return PlainTextResponse("success")
    try:
        envelope = xml_to_dict(body.decode())
    except (ET.ParseError, UnicodeDecodeError):
        return PlainTextResponse("", status_code=400)
    encrypt = envelope.get("Encrypt")
    if not encrypt:
        return PlainTextResponse("", status_code=400)
    params = request.query_params
    try:
        crypto = await _crypto_for(session, acct)
        decrypted = crypto.decrypt_message(
            params.get("msg_signature", ""),
            params.get("timestamp", ""),
            params.get("nonce", ""),
            encrypt,
        )
    except WeChatCryptError as e:
        log.warning("wechat POST decrypt failed: %s", e)
        return PlainTextResponse("", status_code=403)

    if acct.channel_type == "wecom":
        await enqueue_inbound(
            get_redis(),
            account_id=acct.id,
            workspace_id=acct.workspace_id,
            channel_type="wecom",
            payload={"xml": decrypted},
        )
    else:  # wechat_kf: the event only signals new messages → pull via sync_msg
        sync_token = _extract_kf_token(decrypted)
        background.add_task(
            sync_kf_messages, session_factory(), get_redis(), acct.id, token=sync_token
        )
    return PlainTextResponse("success")


def _extract_kf_token(decrypted_xml: str) -> str | None:
    """The 客服 event XML carries a one-shot <Token> that improves sync
    reliability; sync also works from the stored cursor alone if absent."""
    try:
        return xml_to_dict(decrypted_xml).get("Token")
    except ET.ParseError:
        return None
