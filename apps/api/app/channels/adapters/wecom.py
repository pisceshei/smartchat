"""WeCom 企業微信 (self-built app) adapter — channel_type "wecom".

Connect: {corp_id, agent_id} in config + {secret, token, encoding_aes_key} in
credentials. ``gettoken`` (corpid + corpsecret) mints a ~7200s access_token that
is cached in-process.

Inbound: the AES callback (/hooks/wechat/{secret}) decrypts to an inner
``<xml>`` message which the webhook enqueues as {"xml": "..."}; parse_inbound is
a PURE XML→event transform (text / image / voice / video / location / link /
unsubscribe-event).

Outbound: POST /cgi-bin/message/send with agentid; text + markdown natively,
image/voice/video/file via /cgi-bin/media/upload (WeCom app messages take a
media_id, not a link), product cards as a single-article ``news``.

This module also hosts the shared ``WeComApiBase`` (token cache + media
upload/get) reused by the 微信客服 adapter (wechat_kf.py) — 微信客服 is a WeCom
sub-API on the same qyapi host.
"""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar
from xml.etree import ElementTree as ET

import httpx
from py_contracts.content import (
    ContentBlock,
    LocationBlock,
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    TextBlock,
)

from ..base import (
    BaseAdapter,
    ConnectResult,
    HealthResult,
    InboundEvent,
    MediaFetched,
    MediaRef,
    MessageIn,
    OptOut,
    ProfileHint,
    SendResult,
    degrade_content,
)
from ..media import file_public_url
from ..wechat_crypto import WeChatCryptError, WXBizMsgCrypt

QYAPI = "https://qyapi.weixin.qq.com"

# access_token errors → drop the cached token and retry (the token, not the
# secret, went stale).
_TOKEN_STALE = frozenset({40014, 42001, 41001})
# invalid corpsecret / permanently bad credential → pause the account.
_AUTH_ERR = frozenset({40001, 41002, 41004})
_RATE_ERR = frozenset({45009, 45011, 45033, -1, 45018})
_RECIPIENT_ERR = frozenset({40003, 82001, 40031, 60011, 46004})

# WeCom app message media type per canonical media_type
_OUT_MEDIA = {"image": "image", "voice": "voice", "video": "video", "file": "file"}
# inbound MsgType → (canonical media_type)
_IN_MEDIA = {"image": "image", "voice": "voice", "video": "video", "shortvideo": "video"}


class WeComApiError(Exception):
    def __init__(self, errcode: int, errmsg: str):
        super().__init__(f"wecom api error {errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


def xml_to_dict(xml_text: str) -> dict[str, str]:
    """Flatten a WeChat ``<xml>`` callback body into {tag: text}. CDATA is
    unwrapped by the parser transparently."""
    root = ET.fromstring(xml_text)  # noqa: S314 — trusted, AES-decrypted, no DTD
    out: dict[str, str] = {}
    for child in root:
        out[child.tag] = (child.text or "").strip()
    return out


class WeComApiBase(BaseAdapter):
    """Shared qyapi plumbing: in-process access_token cache + temp-media
    upload/download. Instance-scoped cache (fresh per adapter singleton)."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        super().__init__(http)
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    async def _access_token(self, corp_id: str, secret: str, *, force: bool = False) -> str:
        key = (corp_id, secret)
        if not force:
            cached = self._tokens.get(key)
            if cached and cached[1] > time.monotonic() + 30:
                return cached[0]
        r = await self.http.get(
            f"{QYAPI}/cgi-bin/gettoken", params={"corpid": corp_id, "corpsecret": secret}
        )
        data = r.json()
        if data.get("errcode"):
            raise WeComApiError(int(data["errcode"]), str(data.get("errmsg")))
        token = data["access_token"]
        ttl = int(data.get("expires_in") or 7200)
        self._tokens[key] = (token, time.monotonic() + ttl)
        return token

    def _invalidate_token(self, corp_id: str, secret: str) -> None:
        self._tokens.pop((corp_id, secret), None)

    async def _upload_temp_media(
        self, token: str, media_type: str, data: bytes, filename: str, mime: str | None
    ) -> str | None:
        try:
            r = await self.http.post(
                f"{QYAPI}/cgi-bin/media/upload",
                params={"access_token": token, "type": media_type},
                files={"media": (filename or "upload", data, mime or "application/octet-stream")},
            )
            body = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        if body.get("errcode"):
            return None
        return body.get("media_id")

    async def _get_temp_media(
        self, token: str, media_id: str, filename: str | None
    ) -> MediaFetched | None:
        try:
            r = await self.http.get(
                f"{QYAPI}/cgi-bin/media/get",
                params={"access_token": token, "media_id": media_id},
            )
        except httpx.HTTPError:
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code >= 400 or "application/json" in ctype:
            return None  # error body, not bytes
        return MediaFetched(data=r.content, mime=ctype or None, filename=filename)


class WeComAdapter(WeComApiBase):
    channel_type: ClassVar[str] = "wecom"

    # -- connect -----------------------------------------------------------
    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        corp_id = str(config.get("corp_id") or "").strip()
        agent_id = str(config.get("agent_id") or "").strip()
        secret = str(credentials.get("secret") or "").strip()
        token = str(credentials.get("token") or "").strip()
        aes = str(credentials.get("encoding_aes_key") or "").strip()
        missing = [
            name
            for name, val in (
                ("corp_id", corp_id),
                ("agent_id", agent_id),
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
            WXBizMsgCrypt(token, aes, corp_id)  # validates the AES key length
        except WeChatCryptError as e:
            return ConnectResult(
                external_id="", health=HealthResult(ok=False, status="error", detail={"error": str(e)})
            )
        try:
            await self._access_token(corp_id, secret, force=True)
        except (WeComApiError, httpx.HTTPError, ValueError, KeyError) as e:
            detail = {"error": getattr(e, "errmsg", str(e))}
            if isinstance(e, WeComApiError):
                detail["errcode"] = e.errcode
            return ConnectResult(
                external_id="", health=HealthResult(ok=False, status="error", detail=detail)
            )
        return ConnectResult(
            external_id=f"{corp_id}:{agent_id}",
            name=str(config.get("name") or "").strip() or f"WeCom {corp_id}",
            health=HealthResult(
                ok=True, status="active", detail={"corp_id": corp_id, "agent_id": agent_id}
            ),
            needs_webhook_secret=True,
        )

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        xml_text = payload.get("xml")
        if not xml_text:
            return []
        try:
            msg = xml_to_dict(xml_text)
        except ET.ParseError:
            return []
        ev = self._parse_message(msg)
        return [ev] if ev is not None else []

    def _parse_message(self, msg: dict[str, str]) -> InboundEvent | None:
        from_user = msg.get("FromUserName")
        if not from_user:
            return None
        msg_type = msg.get("MsgType")
        if msg_type == "event":
            event = (msg.get("Event") or "").lower()
            if event == "unsubscribe":
                return OptOut(external_user_id=from_user, reason="unsubscribe")
            return None  # subscribe / enter_agent / click … not ingested as messages
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        if msg_type == "text":
            blocks.append(TextBlock(text=msg.get("Content", "")))
        elif msg_type in _IN_MEDIA:
            media_id = msg.get("MediaId")
            blocks.append(
                MediaBlock(
                    media_type=_IN_MEDIA[msg_type],  # type: ignore[arg-type]
                    file_id=uuid.uuid4(),
                    mime=None,
                )
            )
            media_refs.append(
                MediaRef(block_index=0, ref={"kind": "wecom_media", "media_id": media_id})
            )
        elif msg_type == "location":
            blocks.append(
                LocationBlock(
                    latitude=float(msg.get("Location_X") or 0.0),
                    longitude=float(msg.get("Location_Y") or 0.0),
                    name=msg.get("Label"),
                )
            )
        elif msg_type == "link":
            title = msg.get("Title") or ""
            url = msg.get("Url") or ""
            blocks.append(TextBlock(text=f"{title}\n{url}".strip()))
        else:
            return None
        if not blocks:
            return None
        ts = msg.get("CreateTime")
        occurred = (
            datetime.fromtimestamp(int(ts), UTC) if (ts and ts.isdigit()) else None
        )
        return MessageIn(
            external_message_id=msg.get("MsgId") or f"{from_user}:{ts}",
            external_user_id=from_user,
            content=MessageContent(blocks=blocks),
            external_timestamp=occurred,
            profile=ProfileHint(meta={"userid": from_user, "agent_id": msg.get("AgentID")}),
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
                wtype = _OUT_MEDIA.get(block.media_type, "file")
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
            elif isinstance(block, ProductCardBlock):
                payloads.append(self._news_card(block))
        return payloads

    def _news_card(self, block: ProductCardBlock) -> dict[str, Any]:
        image = file_public_url(block.image_file_id) if block.image_file_id else block.image_url
        url = block.url or image
        if not url:  # news requires a url — fall back to a text message
            lines = [block.title]
            if block.subtitle:
                lines.append(block.subtitle)
            if block.price:
                lines.append(f"{block.price} {block.currency or ''}".strip())
            return {"msgtype": "text", "text": {"content": "\n".join(lines)}}
        desc = [p for p in (block.subtitle, f"{block.price} {block.currency or ''}".strip()) if p]
        article: dict[str, Any] = {
            "title": block.title[:128],
            "description": "\n".join(desc)[:512],
            "url": url,
        }
        if image:
            article["picurl"] = image
        return {"msgtype": "news", "news": {"articles": [article]}}

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        corp_id = str((account.config or {}).get("corp_id") or "")
        secret = str(credentials.get("secret") or "")
        agent_id = (account.config or {}).get("agent_id")
        try:
            token = await self._access_token(corp_id, secret)
        except WeComApiError as e:
            return self._api_error_result(corp_id, secret, e.errcode, e.errmsg)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return self.network_error(e)

        body: dict[str, Any] = {"touser": to, "agentid": _as_int(agent_id), "safe": 0}
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
            body.update({k: v for k, v in payload.items() if not k.startswith("_")})

        try:
            r = await self.http.post(
                f"{QYAPI}/cgi-bin/message/send", params={"access_token": token}, json=body
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
            return SendResult(
                ok=False, error_code="RETRYABLE", error_message=errmsg, raw=raw or {}
            )
        code = _classify(errcode)
        return SendResult(ok=False, error_code=code, error_message=errmsg, raw=raw or {})

    async def fetch_media(
        self, account: Any, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None:
        if ref.get("kind") != "wecom_media":
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
            status = "token_expired" if e.errcode in _AUTH_ERR else "error"
            return HealthResult(ok=False, status=status, detail={"error": e.errmsg, "errcode": e.errcode})
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return HealthResult(ok=False, status="error", detail={"error": str(e)[:300]})
        return HealthResult(ok=True, status="active", detail={"corp_id": corp_id})


def _as_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _classify(errcode: int) -> str:
    if errcode in _AUTH_ERR:
        return "AUTH"
    if errcode in _RATE_ERR:
        return "RATE_LIMITED"
    if errcode in _RECIPIENT_ERR:
        return "INVALID_RECIPIENT"
    if errcode == 48002:
        return "BLOCKED"
    return "PERMANENT"
