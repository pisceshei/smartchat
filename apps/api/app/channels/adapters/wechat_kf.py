"""WeChat 微信客服 adapter — channel_type "wechat_kf".

微信客服 is a WeCom sub-API (same qyapi host, same ``gettoken`` + AES callback),
so this adapter reuses ``WeComApiBase`` for the access_token cache and temp-media
upload/download.

SYNC delivery model (the key difference from wecom):
  1. The AES callback only *signals* that new messages exist (an ``event`` XML
     carrying a one-shot sync ``Token`` + ``OpenKfId``).
  2. ``sync_kf_messages`` runs a cursor loop against ``/cgi-bin/kf/sync_msg``,
     pulling ``msg_list`` pages and enqueueing each page (``{"msg_list": [...]}``)
     onto the standard ingress stream. ``parse_inbound`` then transforms a page
     into MessageIn events (PURE) and the ingress pipeline dedupes/persists +
     downloads media — reusing all existing machinery. The per-account cursor
     lives in ``channel_accounts.config["kf_cursor"]``.

Outbound: POST /cgi-bin/kf/send_msg with ``open_kfid`` (which 客服 account) +
``touser`` (the external_userid). ``open_kfid`` is captured on inbound into the
channel_identity meta and re-injected by ``enrich_outbound``. text natively;
image/voice/video/file via media upload; quick buttons as a ``msgmenu``.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from py_contracts.content import (
    ContentBlock,
    LocationBlock,
    MediaBlock,
    MessageContent,
    QuickButtonsBlock,
    TextBlock,
)

from ..base import (
    ConnectResult,
    HealthResult,
    InboundEvent,
    MediaFetched,
    MediaRef,
    MessageIn,
    ProfileHint,
    SendResult,
    degrade_content,
)
from ..media import file_public_url
from ..wechat_crypto import WeChatCryptError, WXBizMsgCrypt
from .wecom import _TOKEN_STALE, QYAPI, WeComApiBase, WeComApiError, _classify

# origin: 3 = customer, 4 = system event, 5 = 客服/servicer. Only ingest 3.
_CUSTOMER_ORIGIN = 3
_KF_MEDIA = {"image": "image", "voice": "voice", "video": "video", "file": "file"}
_MAX_SYNC_PAGES = 20


class WeChatKfAdapter(WeComApiBase):
    channel_type: ClassVar[str] = "wechat_kf"

    # -- connect -----------------------------------------------------------
    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        corp_id = str(config.get("corp_id") or "").strip()
        secret = str(credentials.get("secret") or "").strip()
        token = str(credentials.get("token") or "").strip()
        aes = str(credentials.get("encoding_aes_key") or "").strip()
        missing = [
            name
            for name, val in (
                ("corp_id", corp_id),
                ("secret", secret),
                ("token", token),
                ("encoding_aes_key", aes),
            )
            if not val
        ]
        if missing:
            return ConnectResult(
                external_id="",
                health=HealthResult(
                    ok=False, status="error", detail={"error": f"missing: {', '.join(missing)}"}
                ),
            )
        try:
            WXBizMsgCrypt(token, aes, corp_id)  # validates AES key length
        except WeChatCryptError as e:
            return ConnectResult(
                external_id="", health=HealthResult(ok=False, status="error", detail={"error": str(e)})
            )
        try:
            access = await self._access_token(corp_id, secret, force=True)
        except (WeComApiError, httpx.HTTPError, ValueError, KeyError) as e:
            detail = {"error": getattr(e, "errmsg", str(e))}
            if isinstance(e, WeComApiError):
                detail["errcode"] = e.errcode
            return ConnectResult(
                external_id="", health=HealthResult(ok=False, status="error", detail=detail)
            )
        # best-effort: name the connection after the first 客服 account
        kf_name = await self._first_kf_name(access)
        return ConnectResult(
            external_id=corp_id,
            name=str(config.get("name") or "").strip() or kf_name or f"WeChat 客服 {corp_id}",
            health=HealthResult(ok=True, status="active", detail={"corp_id": corp_id}),
            needs_webhook_secret=True,
        )

    async def _first_kf_name(self, access: str) -> str | None:
        try:
            r = await self.http.get(
                f"{QYAPI}/cgi-bin/kf/account/list", params={"access_token": access}
            )
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        accounts = data.get("account_list") or []
        return accounts[0].get("name") if accounts else None

    # -- inbound (PURE: a sync_msg page → events) --------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        return self.parse_sync_messages(payload.get("msg_list") or [])

    def parse_sync_messages(self, msg_list: list[dict[str, Any]]) -> list[InboundEvent]:
        events: list[InboundEvent] = []
        for msg in msg_list:
            if msg.get("origin") != _CUSTOMER_ORIGIN:
                continue  # skip 客服-sent echoes (5) and system events (4)
            ev = self._parse_one(msg)
            if ev is not None:
                events.append(ev)
        return events

    def _parse_one(self, msg: dict[str, Any]) -> MessageIn | None:
        external_userid = msg.get("external_userid")
        if not external_userid:
            return None
        open_kfid = msg.get("open_kfid")
        msgtype = msg.get("msgtype")
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        if msgtype == "text":
            blocks.append(TextBlock(text=(msg.get("text") or {}).get("content", "")))
        elif msgtype in _KF_MEDIA:
            media_id = (msg.get(msgtype) or {}).get("media_id")
            blocks.append(
                MediaBlock(media_type=_KF_MEDIA[msgtype], file_id=uuid.uuid4())  # type: ignore[arg-type]
            )
            media_refs.append(
                MediaRef(block_index=0, ref={"kind": "wechat_kf_media", "media_id": media_id})
            )
        elif msgtype == "location":
            loc = msg.get("location") or {}
            blocks.append(
                LocationBlock(
                    latitude=float(loc.get("latitude") or 0.0),
                    longitude=float(loc.get("longitude") or 0.0),
                    name=loc.get("name"),
                    address=loc.get("address"),
                )
            )
        elif msgtype == "link":
            link = msg.get("link") or {}
            blocks.append(
                TextBlock(text=f"{link.get('title', '')}\n{link.get('url', '')}".strip())
            )
        else:
            return None  # event / channels / merged_msg … not ingested
        if not blocks:
            return None
        ts = msg.get("send_time")
        return MessageIn(
            external_message_id=str(msg.get("msgid") or f"{external_userid}:{ts}"),
            external_user_id=external_userid,
            content=MessageContent(blocks=blocks),
            external_timestamp=datetime.fromtimestamp(int(ts), UTC) if ts else None,
            profile=ProfileHint(meta={"open_kfid": open_kfid, "external_userid": external_userid}),
            media_refs=media_refs,
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"msgtype": "text", "text": {"content": block.text}})
            elif isinstance(block, MediaBlock):
                wtype = _KF_MEDIA.get(block.media_type, "file")
                payloads.append(
                    {
                        "msgtype": wtype,
                        "_upload": {
                            "media_type": wtype,
                            "url": file_public_url(block.file_id),
                            "filename": block.caption or f"{block.media_type}",
                            "mime": block.mime,
                        },
                    }
                )
            elif isinstance(block, QuickButtonsBlock):
                items = [
                    {
                        "type": "click",
                        "click": {
                            "id": b.id[:128],
                            "content": b.text[: self.capabilities.button_text_max],
                        },
                    }
                    for b in block.buttons[: self.capabilities.max_buttons]
                ]
                payloads.append(
                    {"msgtype": "msgmenu", "msgmenu": {"head_content": block.text, "list": items}}
                )
        return payloads

    async def enrich_outbound(
        self,
        session: Any,
        *,
        account: Any,
        credentials: dict[str, Any],
        conversation: Any,
        identity: Any,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Inject the open_kfid (captured on inbound into the identity meta) so
        send() can route the reply to the right 客服 account."""
        open_kfid = (getattr(identity, "meta", None) or {}).get("open_kfid")
        if open_kfid:
            for p in payloads:
                p.setdefault("open_kfid", open_kfid)
        return payloads

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        open_kfid = payload.get("open_kfid")
        if not open_kfid:
            return SendResult(
                ok=False, error_code="PERMANENT", error_message="missing open_kfid (no inbound context)"
            )
        corp_id = str((account.config or {}).get("corp_id") or "")
        secret = str(credentials.get("secret") or "")
        try:
            token = await self._access_token(corp_id, secret)
        except WeComApiError as e:
            return self._api_error_result(corp_id, secret, e.errcode, e.errmsg)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return self.network_error(e)

        body: dict[str, Any] = {"touser": to, "open_kfid": open_kfid}
        upload = payload.get("_upload")
        if upload:
            media_id = await self._resolve_media_id(token, upload)
            if not media_id:
                return SendResult(
                    ok=False, error_code="PERMANENT", error_message="media upload failed"
                )
            wtype = upload["media_type"]
            body["msgtype"] = wtype
            body[wtype] = {"media_id": media_id}
        else:
            body.update(
                {k: v for k, v in payload.items() if k not in ("_upload", "open_kfid")}
            )

        try:
            r = await self.http.post(
                f"{QYAPI}/cgi-bin/kf/send_msg", params={"access_token": token}, json=body
            )
            data = r.json()
        except httpx.HTTPError as e:
            return self.network_error(e)
        except ValueError:
            data = {}
        errcode = int(data.get("errcode") or 0)
        if errcode == 0:
            return SendResult(ok=True, external_message_id=data.get("msgid"), raw=data)
        return self._api_error_result(corp_id, secret, errcode, str(data.get("errmsg")), raw=data)

    async def _resolve_media_id(self, token: str, upload: dict[str, Any]) -> str | None:
        try:
            resp = await self.http.get(upload["url"])
            resp.raise_for_status()
            data = resp.content
        except httpx.HTTPError:
            return None
        return await self._upload_temp_media(
            token, upload["media_type"], data, upload.get("filename") or "upload", upload.get("mime")
        )

    def _api_error_result(
        self, corp_id: str, secret: str, errcode: int, errmsg: str, *, raw: dict | None = None
    ) -> SendResult:
        if errcode in _TOKEN_STALE:
            self._invalidate_token(corp_id, secret)
            return SendResult(ok=False, error_code="RETRYABLE", error_message=errmsg, raw=raw or {})
        # TODO(wechat-kf): map the 48h active-messaging-limit errcode to
        # WINDOW_EXPIRED once confirmed against a live 客服 account (docs are
        # ambiguous; the generic classifier covers auth/rate/recipient today).
        return SendResult(
            ok=False, error_code=_classify(errcode), error_message=errmsg, raw=raw or {}
        )

    async def fetch_media(
        self, account: Any, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None:
        if ref.get("kind") != "wechat_kf_media":
            return await super().fetch_media(account, credentials, ref)
        corp_id = str((account.config or {}).get("corp_id") or "")
        secret = str(credentials.get("secret") or "")
        try:
            token = await self._access_token(corp_id, secret)
        except (WeComApiError, httpx.HTTPError, ValueError, KeyError):
            return None
        return await self._get_temp_media(token, ref.get("media_id", ""), ref.get("filename"))

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        corp_id = str((account.config or {}).get("corp_id") or "")
        secret = str(credentials.get("secret") or "")
        try:
            await self._access_token(corp_id, secret, force=True)
        except WeComApiError as e:
            return HealthResult(
                ok=False, status="error", detail={"error": e.errmsg, "errcode": e.errcode}
            )
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return HealthResult(ok=False, status="error", detail={"error": str(e)[:300]})
        return HealthResult(ok=True, status="active", detail={"corp_id": corp_id})


# --------------------------------------------------------------------------
# sync cursor loop (triggered by the callback; also safe to run from a cron)
# --------------------------------------------------------------------------
async def sync_kf_messages(
    session_factory: Any,
    redis: Any,
    account_id: uuid.UUID,
    *,
    token: str | None = None,
    adapter: WeChatKfAdapter | None = None,
    max_pages: int = _MAX_SYNC_PAGES,
) -> int:
    """Pull new 客服 messages via the cursor and feed them to the ingress
    pipeline. A per-account Redis lock coalesces concurrent callbacks. Returns
    the number of messages enqueued. Imports are local to avoid the
    registry↔adapter import cycle (mirrors email_imap.poll_email_account)."""
    from sqlalchemy.orm.attributes import flag_modified

    from ...models.channels import ChannelAccount
    from .. import creds as creds_mod
    from ..ingress_pipeline import enqueue_inbound
    from ..registry import get_adapter

    lock_key = f"wechat_kf:sync:{account_id}"
    if not await redis.set(lock_key, "1", nx=True, ex=30):
        return 0
    try:
        async with session_factory() as session:
            acct = await session.get(ChannelAccount, account_id)
            if acct is None or not acct.enabled or acct.channel_type != "wechat_kf":
                return 0
            credentials = await creds_mod.get_credentials(session, acct)
            corp_id = str((acct.config or {}).get("corp_id") or "")
            cursor = str((acct.config or {}).get("kf_cursor") or "")
            workspace_id = acct.workspace_id
        secret = str(credentials.get("secret") or "")
        adapter = adapter or get_adapter("wechat_kf")  # type: ignore[assignment]
        try:
            access = await adapter._access_token(corp_id, secret)
        except (WeComApiError, httpx.HTTPError, ValueError, KeyError):
            return 0
        pulled = 0
        for _ in range(max_pages):
            req: dict[str, Any] = {"cursor": cursor, "limit": 1000}
            if token:
                req["token"] = token
            try:
                r = await adapter.http.post(
                    f"{QYAPI}/cgi-bin/kf/sync_msg", params={"access_token": access}, json=req
                )
                data = r.json()
            except (httpx.HTTPError, ValueError):
                break
            errcode = int(data.get("errcode") or 0)
            if errcode in _TOKEN_STALE:
                adapter._invalidate_token(corp_id, secret)
                try:
                    access = await adapter._access_token(corp_id, secret, force=True)
                except (WeComApiError, httpx.HTTPError, ValueError, KeyError):
                    break
                continue
            if errcode != 0:
                break
            msgs = data.get("msg_list") or []
            if msgs:
                await enqueue_inbound(
                    redis,
                    account_id=account_id,
                    workspace_id=workspace_id,
                    channel_type="wechat_kf",
                    payload={"msg_list": msgs},
                )
                pulled += len(msgs)
            cursor = data.get("next_cursor") or cursor
            async with session_factory() as session:
                async with session.begin():
                    a = await session.get(ChannelAccount, account_id)
                    if a is not None:
                        a.config = {**(a.config or {}), "kf_cursor": cursor}
                        flag_modified(a, "config")
            token = None  # the one-shot sync token only applies to the first page
            if not data.get("has_more"):
                break
        return pulled
    finally:
        await redis.delete(lock_key)
