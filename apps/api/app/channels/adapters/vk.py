"""VKontakte community (Callback API + Bots API) adapter — channel_type "vk".

Inbound: VK Callback API POSTs each event to a per-community Request URL
`/hooks/vk/{webhook_secret}` (the path secret maps to one ChannelAccount).

  * First save: VK POSTs {"type": "confirmation", "group_id": …}; the server must
    answer with the community's confirmation string as a PLAIN-TEXT body.
  * Every subsequent event carries the community "secret" key (when configured);
    the hook verifies it before enqueuing and ALWAYS replies plain-text "ok"
    (VK retries the delivery until it sees exactly "ok").

  ALTERNATIVE (not used here): VK Bots Long Poll — instead of a callback URL the
  server polls groups.getLongPollServer + a blocking `/act=a_check` GET loop. It
  needs no public URL/confirmation/secret and yields the SAME `message_new`
  objects, so the same parse_inbound would drive a long-poll worker. Callback is
  preferred to match the rest of the webhook-based ingress pipeline.

Outbound: messages.send (access_token = community token, api version `v`, a fresh
`random_id` per call for idempotency). Quick buttons render as an inline keyboard;
product cards as text + open_link/text keyboard buttons.

NOTE (protocol deviation): posting media (photo/doc) inline requires VK's
multi-step upload flow (…getMessagesUploadServer → upload → save → attach). Our
media already lives at a public `{assets_base_url}` URL, so outbound media is
delivered as an unfurled link in the message body rather than an uploaded
attachment.
"""
from __future__ import annotations

import json
import os
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
    BaseAdapter,
    ConnectResult,
    HealthResult,
    InboundEvent,
    MediaRef,
    MessageIn,
    OptOut,
    ProfileHint,
    SendResult,
    degrade_content,
)
from ..media import file_public_url

API_BASE = "https://api.vk.com/method"
API_VERSION = "5.199"

# VK API error_code → our canonical ErrorCode.
_AUTH_CODES = frozenset({5, 27, 28, 15})  # user/group/app auth failed, access denied
_RATE_CODES = frozenset({6, 9, 29})  # too many per sec / flood / rate limit reached
_RECIPIENT_CODES = frozenset({7, 917})  # permission denied / no access to conversation
_BLOCKED_CODES = frozenset({901, 902})  # can't message (privacy) / blacklisted
# events we forward to the ingress pipeline (parse_inbound handles these).
_ENQUEUE_TYPES = frozenset({"message_new", "message_deny"})


class VKAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "vk"

    # -- inbound -----------------------------------------------------------
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        etype = payload.get("type")
        obj = payload.get("object") or {}
        if etype == "message_new":
            # v5.103+ wraps the message under object.message; older = object itself.
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
            ev = self._parse_message(payload, msg)
            return [ev] if ev is not None else []
        if etype == "message_deny":
            uid = obj.get("user_id")
            if uid is not None:
                return [OptOut(external_user_id=str(uid), reason="message_deny")]
        return []

    def _parse_message(self, payload: dict[str, Any], msg: dict[str, Any]) -> MessageIn | None:
        from_id = msg.get("from_id")
        peer_id = msg.get("peer_id")
        if from_id is None and peer_id is None:
            return None
        # reply target: the peer we send back to (for a user DM peer_id == from_id).
        target = peer_id if peer_id is not None else from_id
        blocks: list[ContentBlock] = []
        media_refs: list[MediaRef] = []
        text = msg.get("text")
        if text:
            blocks.append(TextBlock(text=text))
        for att in msg.get("attachments", []) or []:
            self._parse_attachment(att, blocks, media_refs)
        geo = msg.get("geo")
        if geo:
            coords = geo.get("coordinates") or {}
            place = geo.get("place") or {}
            blocks.append(
                LocationBlock(
                    latitude=float(coords.get("latitude") or 0.0),
                    longitude=float(coords.get("longitude") or 0.0),
                    name=place.get("title"),
                    address=place.get("address") or place.get("city"),
                )
            )
        if not blocks:
            return None
        ts = msg.get("date")
        occurred = datetime.fromtimestamp(ts, UTC) if ts else None
        # dedup key: envelope event_id is stable across VK retries; fall back to
        # the community-unique message id.
        external_id = str(payload.get("event_id") or msg.get("id") or f"{target}:{ts}")
        return MessageIn(
            external_message_id=external_id,
            external_user_id=str(target),
            content=MessageContent(blocks=blocks),
            external_timestamp=occurred,
            profile=ProfileHint(meta={"vk_user_id": from_id, "peer_id": peer_id}),
            media_refs=media_refs,
            meta={
                "from_id": from_id,
                "peer_id": peer_id,
                "conversation_message_id": msg.get("conversation_message_id"),
            },
        )

    def _parse_attachment(
        self, att: dict[str, Any], blocks: list[ContentBlock], media_refs: list[MediaRef]
    ) -> None:
        atype = att.get("type")
        if atype == "photo":
            photo = att.get("photo") or {}
            sizes = photo.get("sizes") or []
            url = (
                max(sizes, key=lambda s: s.get("width", 0) or 0).get("url")
                if sizes
                else photo.get("url")
            )
            if url:
                blocks.append(MediaBlock(media_type="image", file_id=uuid.uuid4()))
                media_refs.append(
                    MediaRef(
                        block_index=len(blocks) - 1,
                        ref={"kind": "url", "url": url, "filename": f"photo_{photo.get('id')}.jpg"},
                    )
                )
        elif atype == "doc":
            doc = att.get("doc") or {}
            url = doc.get("url")
            if url:
                blocks.append(
                    MediaBlock(media_type="file", file_id=uuid.uuid4(), caption=doc.get("title"))
                )
                media_refs.append(
                    MediaRef(
                        block_index=len(blocks) - 1,
                        ref={"kind": "url", "url": url, "filename": doc.get("title")},
                    )
                )
        elif atype == "audio_message":
            am = att.get("audio_message") or {}
            url = am.get("link_ogg") or am.get("link_mp3")
            if url:
                blocks.append(
                    MediaBlock(
                        media_type="voice",
                        file_id=uuid.uuid4(),
                        duration_ms=(am.get("duration") or 0) * 1000 or None,
                    )
                )
                media_refs.append(
                    MediaRef(
                        block_index=len(blocks) - 1,
                        ref={"kind": "url", "url": url, "filename": "voice.ogg"},
                    )
                )
        else:
            # video / audio / sticker / wall / link … no simple downloadable URL:
            # surface a short text placeholder so the message is never empty.
            inner = att.get(atype) if isinstance(att.get(atype), dict) else {}
            title = inner.get("title") if isinstance(inner, dict) else None
            blocks.append(TextBlock(text=f"[{atype}{': ' + title if title else ''}]"))

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities, media_url=file_public_url)
        payloads: list[dict[str, Any]] = []
        for block in degraded.blocks:
            if isinstance(block, TextBlock):
                payloads.append({"_method": "messages.send", "message": block.text})
            elif isinstance(block, MediaBlock):
                url = file_public_url(block.file_id)
                label = block.caption or f"[{block.media_type}]"
                payloads.append({"_method": "messages.send", "message": f"{label}\n{url}"})
            elif isinstance(block, QuickButtonsBlock):
                buttons = [
                    [
                        {
                            "action": {
                                "type": "text",
                                "label": b.text[: self.capabilities.button_text_max],
                                "payload": json.dumps({"button": b.id}, ensure_ascii=False),
                            }
                        }
                    ]
                    for b in block.buttons
                ]
                payloads.append(
                    {
                        "_method": "messages.send",
                        "message": block.text,
                        "keyboard": {"inline": True, "buttons": buttons},
                    }
                )
            # NOTE: VK has no native product-card element (CAPABILITIES["vk"]
            # product_cards=False), so ProductCardBlock is converted to text+image
            # by degrade_content before it reaches here — the card's link lands in
            # the message body; there is no ProductCardBlock branch.
        return payloads

    @staticmethod
    def _random_id() -> int:
        # non-zero 31-bit int; VK dedupes repeated random_id within ~1h.
        return int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF or 1

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        token = credentials.get("community_token") or credentials.get("access_token", "")
        method = payload.get("_method", "messages.send")
        params: dict[str, Any] = {
            "access_token": token,
            "v": API_VERSION,
            "peer_id": to,
            "random_id": self._random_id(),
        }
        if payload.get("message"):
            params["message"] = payload["message"]
        if payload.get("keyboard"):
            params["keyboard"] = json.dumps(payload["keyboard"], ensure_ascii=False)
        if payload.get("attachment"):
            params["attachment"] = payload["attachment"]
        try:
            r = await self.http.post(f"{API_BASE}/{method}", data=params)
        except httpx.HTTPError as e:
            return self.network_error(e)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if "response" in data:
            resp = data["response"]
            # messages.send → int message id; some methods → {message_id: …}
            mid = resp.get("message_id") if isinstance(resp, dict) else resp
            return SendResult(ok=True, external_message_id=str(mid), raw=data)
        err = data.get("error") or {}
        code, retry_after = self.classify_error(err)
        return SendResult(
            ok=False,
            error_code=code,
            error_message=str(err.get("error_msg") or err or r.text)[:500],
            retry_after_s=retry_after,
            raw=data,
        )

    @staticmethod
    def classify_error(err: dict[str, Any]) -> tuple[str, float | None]:
        code = err.get("error_code")
        if code in _AUTH_CODES:
            return "AUTH", None
        if code in _RATE_CODES:
            return "RATE_LIMITED", 1.0
        if code == 914:
            return "MESSAGE_TOO_LONG", None
        if code in _BLOCKED_CODES:
            return "BLOCKED", None
        if code in _RECIPIENT_CODES:
            return "INVALID_RECIPIENT", None
        if code == 10:  # internal server error
            return "RETRYABLE", None
        return "PERMANENT", None

    async def send_typing(
        self, account: Any, credentials: dict[str, Any], to: str, on: bool = True
    ) -> None:
        if not on:
            return
        token = credentials.get("community_token") or credentials.get("access_token", "")
        try:
            await self.http.post(
                f"{API_BASE}/messages.setActivity",
                data={"access_token": token, "v": API_VERSION, "peer_id": to, "type": "typing"},
            )
        except httpx.HTTPError:
            pass

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        token = credentials.get("community_token") or credentials.get("access_token", "")
        group_id = str(getattr(account, "external_id", "") or "")
        grp, err = await self._get_by_id(token, group_id)
        if grp is not None:
            return HealthResult(
                ok=True,
                status="active",
                detail={"name": grp.get("name"), "screen_name": grp.get("screen_name")},
            )
        status = "token_expired" if (err or {}).get("error_code") in _AUTH_CODES else "error"
        return HealthResult(
            ok=False, status=status, detail={"error": (err or {}).get("error_msg") or "groups.getById failed"}
        )

    async def _get_by_id(
        self, token: str, group_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        params: dict[str, Any] = {"access_token": token, "v": API_VERSION}
        if group_id:
            params["group_id"] = group_id
        try:
            r = await self.http.get(f"{API_BASE}/groups.getById", params=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return None, {"error_msg": str(e)[:200]}
        if "error" in data:
            return None, data["error"]
        resp = data.get("response")
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        if isinstance(groups, list) and groups:
            return groups[0], None
        return None, {"error_msg": "group not found"}

    # -- connect-time validation (dispatched from modules/channels) --------
    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        token = (
            credentials.get("community_token")
            or config.get("community_token")
            or credentials.get("access_token")
            or ""
        )
        group_id = str(config.get("group_id") or credentials.get("group_id") or "").strip()
        confirmation = str(config.get("confirmation_string") or config.get("confirmation") or "")
        if not token:
            return ConnectResult(
                external_id="",
                health=HealthResult(
                    ok=False, status="error", detail={"error": "community_token required"}
                ),
            )
        grp, err = await self._get_by_id(token, group_id)
        if grp is None:
            ec = (err or {}).get("error_code")
            status = "token_expired" if ec in _AUTH_CODES else "error"
            return ConnectResult(
                external_id="",
                health=HealthResult(
                    ok=False,
                    status=status,
                    detail={"error": (err or {}).get("error_msg") or "groups.getById failed"},
                ),
            )
        gid = str(grp.get("id") or group_id)
        return ConnectResult(
            external_id=gid,
            name=str(config.get("name") or grp.get("name") or f"vk {gid}"),
            health=HealthResult(
                ok=True,
                status="active",
                detail={"name": grp.get("name"), "screen_name": grp.get("screen_name")},
            ),
            # confirmation_string is surfaced to the hook via ChannelAccount.config;
            # the callback "secret" travels in (encrypted) credentials.
            config_patch={
                "group_id": gid,
                "confirmation_string": confirmation,
                "screen_name": grp.get("screen_name"),
            },
            needs_webhook_secret=True,
        )
