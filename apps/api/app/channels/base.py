"""ChannelAdapter contract (plan A.7).

One canonical inbound event union + one static capability matrix drive every
channel. Adapters normalize once (parse_inbound is PURE — no I/O, so it is
trivially unit-testable); media bytes are fetched separately via fetch_media
using adapter-specific refs. Outbound rendering degrades content per the
capability matrix with FIXED rules: product card → image+text+link,
quick buttons → numbered text menu.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, Protocol, runtime_checkable

import httpx
from py_contracts.content import (
    ButtonReplyBlock,
    ContentBlock,
    EmailBlock,
    LocationBlock,
    MediaBlock,
    MessageContent,
    ProductCardBlock,
    QuickButtonsBlock,
    TemplateBlock,
    TextBlock,
)
from pydantic import BaseModel, Field, TypeAdapter

# --------------------------------------------------------------------------
# inbound event union
# --------------------------------------------------------------------------


class ProfileHint(BaseModel):
    """Best-effort contact profile extracted from an inbound payload; used to
    seed a new Contact and refresh channel_identity display data."""

    display_name: str | None = None
    avatar_url: str | None = None
    email: str | None = None
    phone: str | None = None
    language: str | None = None
    country: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class MediaRef(BaseModel):
    """Adapter-specific pointer to media bytes that still need downloading.
    block_index points at the MediaBlock (or EmailBlock) inside
    MessageIn.content whose file_id must be rewritten once stored."""

    block_index: int
    ref: dict[str, Any]


class MessageIn(BaseModel):
    kind: Literal["message_in"] = "message_in"
    external_message_id: str
    external_user_id: str
    content: MessageContent
    external_timestamp: datetime | None = None
    profile: ProfileHint = Field(default_factory=ProfileHint)
    media_refs: list[MediaRef] = Field(default_factory=list)
    reply_to_external_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class DeliveryStatus(BaseModel):
    """Channel report about one of OUR outbound messages."""

    kind: Literal["delivery_status"] = "delivery_status"
    external_message_id: str
    status: Literal["sent", "delivered", "read", "failed"]
    external_user_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    occurred_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ReadReceipt(BaseModel):
    """Watermark-style read receipt (Messenger/IG): everything we sent up to
    `watermark` has been read."""

    kind: Literal["read_receipt"] = "read_receipt"
    external_user_id: str
    watermark: datetime


class ContactUpdate(BaseModel):
    kind: Literal["contact_update"] = "contact_update"
    external_user_id: str
    profile: ProfileHint = Field(default_factory=ProfileHint)


class AccountStatus(BaseModel):
    """Channel-account level status change (token expiry, ban, bridge
    online/offline heartbeat…)."""

    kind: Literal["account_status"] = "account_status"
    # active/token_expired/disconnected/banned/logged_out/online/offline
    status: str
    detail: dict[str, Any] = Field(default_factory=dict)


class TypingIn(BaseModel):
    kind: Literal["typing_in"] = "typing_in"
    external_user_id: str
    is_typing: bool = True


class OptOut(BaseModel):
    kind: Literal["opt_out"] = "opt_out"
    external_user_id: str
    scope: str = "channel"  # channel (blocked/unfollowed) or marketing
    reason: str | None = None


InboundEvent = Annotated[
    MessageIn | DeliveryStatus | ReadReceipt | ContactUpdate | AccountStatus | TypingIn | OptOut,
    Field(discriminator="kind"),
]

INBOUND_EVENTS_ADAPTER: TypeAdapter[list[InboundEvent]] = TypeAdapter(list[InboundEvent])


def parse_normalized_events(payload: dict[str, Any]) -> list[InboundEvent]:
    """Shared parse for pre-normalized payloads ({"events": [...]}) used by
    widget, device bridges and the email poller."""
    return INBOUND_EVENTS_ADAPTER.validate_python(payload.get("events", []))


# --------------------------------------------------------------------------
# capabilities matrix
# --------------------------------------------------------------------------


class Capabilities(BaseModel):
    typing_indicator: bool = False
    read_receipts: bool = False
    buttons: bool = False
    max_buttons: int = 0
    button_text_max: int = 20
    product_cards: bool = False
    templates: bool = False
    template_required_outside_window: bool = False
    session_window_hours: int | None = None
    max_text_len: int = 4096
    media_types: set[str] = Field(default_factory=set)
    location: bool = False
    native_email: bool = False
    supports_edit: bool = False
    supports_recall: bool = False


_ALL_MEDIA = {"image", "video", "audio", "voice", "file", "sticker"}

# Static matrix per plan A.7 (widget/wa_cloud/wa_app/messenger/instagram/
# telegram/email/line_oa/line_app). Keys are the channel_accounts.channel_type
# canonical names; plan short names resolve via _ALIASES.
CAPABILITIES: dict[str, Capabilities] = {
    "widget": Capabilities(
        typing_indicator=True, read_receipts=True,
        buttons=True, max_buttons=10, button_text_max=40,
        product_cards=True, max_text_len=4000, media_types=set(_ALL_MEDIA), location=True,
    ),
    "whatsapp_cloud": Capabilities(
        read_receipts=True,
        buttons=True, max_buttons=10, button_text_max=20,
        templates=True, template_required_outside_window=True, session_window_hours=24,
        max_text_len=4096, media_types=set(_ALL_MEDIA), location=True,
    ),
    "whatsapp_app": Capabilities(
        typing_indicator=True, read_receipts=True,
        max_text_len=4096, media_types=set(_ALL_MEDIA), location=True,
    ),
    # WhatsApp via a BSP proxy (YCloud/ChatApp/…) — same WhatsApp semantics as
    # the direct Cloud API, so capabilities (24h window, templates) must match;
    # sender.py/ingress read capabilities_for(channel_type) directly.
    "whatsapp_bsp": Capabilities(
        read_receipts=True,
        buttons=True, max_buttons=10, button_text_max=20,
        templates=True, template_required_outside_window=True, session_window_hours=24,
        max_text_len=4096, media_types=set(_ALL_MEDIA), location=True,
    ),
    "messenger": Capabilities(
        typing_indicator=True, read_receipts=True,
        buttons=True, max_buttons=13, button_text_max=20,
        product_cards=True, session_window_hours=24,
        max_text_len=2000, media_types={"image", "video", "audio", "file"},
    ),
    "instagram": Capabilities(
        typing_indicator=True, read_receipts=True,
        buttons=True, max_buttons=13, button_text_max=20,
        product_cards=True, session_window_hours=24,
        max_text_len=1000, media_types={"image", "video", "audio"},
    ),
    "telegram_bot": Capabilities(
        typing_indicator=True,
        buttons=True, max_buttons=20, button_text_max=64,
        product_cards=True, max_text_len=4096, media_types=set(_ALL_MEDIA), location=True,
        supports_edit=True, supports_recall=True,
    ),
    "email": Capabilities(
        native_email=True, max_text_len=200_000,
        media_types={"image", "video", "audio", "voice", "file"},
    ),
    "line_oa": Capabilities(
        buttons=True, max_buttons=13, button_text_max=20,
        product_cards=True, max_text_len=5000,
        media_types={"image", "video", "audio", "sticker"}, location=True,
    ),
    "line_app": Capabilities(
        max_text_len=5000, media_types={"image", "video", "audio", "file"}, location=True,
    ),
    # -- Phase 4 channels (docs/channel-integration.md §8–14) ----------------
    # Slack: Block Kit interactive buttons; no customer-service time window.
    # section text field caps at 3000 chars; button text at 75.
    "slack": Capabilities(
        buttons=True, max_buttons=5, button_text_max=75,
        product_cards=True, max_text_len=3000,
        media_types={"image", "video", "audio", "file"},
    ),
    # VKontakte community: inline/bot keyboards (label ≤40, ≤10 buttons here);
    # messages.setActivity gives a typing indicator. Text ≤4096.
    "vk": Capabilities(
        typing_indicator=True,
        buttons=True, max_buttons=10, button_text_max=40,
        max_text_len=4096, media_types={"image", "video", "audio", "file"},
    ),
    # WeChat 微信客服 (Customer Service): text/image/voice/video/file + msgmenu
    # (menu → buttons); 48h active-messaging session window. Text ≤2048 bytes.
    "wechat_kf": Capabilities(
        buttons=True, max_buttons=10, button_text_max=30,
        session_window_hours=48,
        max_text_len=2048, media_types={"image", "voice", "video", "file"},
    ),
    # WeCom 企業微信: text + rich (news/textcard → product card); no interactive
    # reply buttons in external messaging. Text ≤2048.
    "wecom": Capabilities(
        product_cards=True,
        max_text_len=2048, media_types={"image", "voice", "video", "file"},
    ),
    # TikTok Business Messaging: text only, business-account gated (limited API).
    "tiktok_business": Capabilities(
        max_text_len=2000,
    ),
    # YouTube comments (Data API): text-only replies, no typing/receipts.
    # comment body caps at 10k chars.
    "youtube": Capabilities(
        max_text_len=10_000,
    ),
    # Zalo OA: text + template (list/media templates → product card + buttons).
    "zalo_app": Capabilities(
        buttons=True, max_buttons=5, button_text_max=40,
        product_cards=True, templates=True,
        max_text_len=2000, media_types={"image", "file"},
    ),
}

_ALIASES = {
    "wa_cloud": "whatsapp_cloud",
    "wa_app": "whatsapp_app",
    "telegram": "telegram_bot",
    "line": "line_oa",
}


def capabilities_for(channel_type: str) -> Capabilities:
    ct = _ALIASES.get(channel_type, channel_type)
    caps = CAPABILITIES.get(ct)
    if caps is None:
        # unknown/future channels: conservative text-only profile
        return Capabilities(max_text_len=2000)
    return caps


# --------------------------------------------------------------------------
# account ref / send result
# --------------------------------------------------------------------------


class AccountRef(BaseModel):
    """Lightweight, ORM-free view of a channel account passed to adapters."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    channel_type: str
    external_id: str
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Any) -> AccountRef:
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            channel_type=row.channel_type,
            external_id=row.external_id,
            name=row.name or "",
            config=dict(row.config or {}),
        )


ErrorCode = Literal[
    "WINDOW_EXPIRED",       # 24h customer window closed and no template used
    "AUTH",                 # token invalid/expired → pause the account queue
    "RATE_LIMITED",         # throttled by the platform (retryable)
    "INVALID_RECIPIENT",    # user unreachable / blocked the account
    "MESSAGE_TOO_LONG",
    "UNSUPPORTED_CONTENT",
    "BLOCKED",              # platform policy block
    "BRIDGE_OFFLINE",       # hosted-device bridge not configured/reachable
    "PERMANENT",            # other non-retryable API error
    "RETRYABLE",            # other transient API error (5xx…)
    "NETWORK",              # connection error / timeout (retryable)
]

RETRYABLE_CODES: frozenset[str] = frozenset({"RATE_LIMITED", "RETRYABLE", "NETWORK"})


class SendResult(BaseModel):
    ok: bool
    external_message_id: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    retry_after_s: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        return (not self.ok) and self.error_code in RETRYABLE_CODES

    @property
    def auth_failed(self) -> bool:
        return (not self.ok) and self.error_code == "AUTH"


@dataclass
class MediaFetched:
    data: bytes
    mime: str | None = None
    filename: str | None = None


class HealthResult(BaseModel):
    ok: bool
    status: str = "active"  # suggested channel_accounts.status
    detail: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ConnectResult:
    """Adapter connect-time validation outcome (Phase 4 connect dispatch).

    The channels router persists external_id/name/health, merges config_patch
    into the account config, and — when needs_webhook_secret is set — echoes the
    generated webhook_secret + hook URL in the connect response so the operator
    can paste it into the provider console (VK/WeChat/Zalo/TikTok use a
    per-account path secret; Slack/YouTube do not)."""

    external_id: str
    name: str = ""
    health: HealthResult = field(default_factory=lambda: HealthResult(ok=True))
    config_patch: dict[str, Any] = field(default_factory=dict)
    needs_webhook_secret: bool = False


# --------------------------------------------------------------------------
# webhook signature helpers
# --------------------------------------------------------------------------


def verify_meta_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta X-Hub-Signature-256: 'sha256=' + hex(hmac_sha256(app_secret, body))."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:].strip())


def line_signature(channel_secret: str, body: bytes) -> str:
    """LINE X-Line-Signature: base64(hmac_sha256(channel_secret, body))."""
    return base64.b64encode(hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()).decode()


def verify_line_signature(channel_secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    return hmac.compare_digest(line_signature(channel_secret, body), header.strip())


def bridge_signature(secret: str, body: bytes) -> str:
    """Internal device-bridge webhook: hex(hmac_sha256(webhook_secret, body))."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_bridge_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    return hmac.compare_digest(bridge_signature(secret, body), header.strip())


def secrets_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# --------------------------------------------------------------------------
# capability degradation (fixed rules, shared by all adapters)
# --------------------------------------------------------------------------


def split_text(text: str, max_len: int) -> list[str]:
    """Split on whitespace boundaries where possible, hard-cut otherwise."""
    if max_len <= 0 or len(text) <= max_len:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > max_len:
        cut = rest.rfind("\n", max_len // 2, max_len)
        if cut == -1:
            cut = rest.rfind(" ", max_len // 2, max_len)
        if cut == -1:
            cut = max_len
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def quick_buttons_to_menu(block: QuickButtonsBlock) -> TextBlock:
    """Fixed degrade rule: buttons → numbered menu."""
    lines = [block.text, ""]
    lines += [f"{i}. {b.text}" for i, b in enumerate(block.buttons, start=1)]
    lines.append("")
    lines.append("Reply with a number to choose.")
    return TextBlock(text="\n".join(lines))


def card_to_blocks(block: ProductCardBlock) -> list[ContentBlock]:
    """Fixed degrade rule: product card → image + text + link."""
    out: list[ContentBlock] = []
    lines: list[str] = [block.title]
    if block.subtitle:
        lines.append(block.subtitle)
    if block.price:
        lines.append(f"{block.price} {block.currency or ''}".strip())
    if block.url:
        lines.append(block.url)
    for btn in block.buttons:
        if btn.action == "url" and btn.value != block.url:
            lines.append(f"{btn.text}: {btn.value}")
    if block.image_file_id is not None:
        out.append(
            MediaBlock(media_type="image", file_id=block.image_file_id, caption=block.title)
        )
    elif block.image_url:
        lines.insert(1, block.image_url)
    out.append(TextBlock(text="\n".join(lines)))
    return out


def location_to_text(block: LocationBlock) -> TextBlock:
    label = block.name or block.address or "Location"
    return TextBlock(
        text=f"{label}\nhttps://maps.google.com/?q={block.latitude},{block.longitude}"
    )


def email_to_text(block: EmailBlock) -> TextBlock:
    text = f"{block.subject}\n\n{block.text}" if block.subject else block.text
    return TextBlock(text=text)


def media_fallback_text(block: MediaBlock, url: str | None = None) -> TextBlock:
    label = block.caption or f"[{block.media_type}]"
    return TextBlock(text=f"{label}\n{url}" if url else label)


def degrade_content(
    content: MessageContent,
    caps: Capabilities,
    *,
    media_url: Any = None,
) -> MessageContent:
    """Apply the fixed degradation rules for a channel's capabilities.
    media_url: optional callable(file_id) -> str used when a media type is
    unsupported and must become a text link."""
    out: list[ContentBlock] = []
    for block in content.blocks:
        if isinstance(block, ProductCardBlock) and not caps.product_cards:
            for b in card_to_blocks(block):
                out.append(b)
        elif isinstance(block, QuickButtonsBlock) and (
            not caps.buttons or len(block.buttons) > caps.max_buttons
        ):
            out.append(quick_buttons_to_menu(block))
        elif isinstance(block, MediaBlock) and block.media_type not in caps.media_types:
            url = media_url(block.file_id) if callable(media_url) else None
            out.append(media_fallback_text(block, url))
        elif isinstance(block, LocationBlock) and not caps.location:
            out.append(location_to_text(block))
        elif isinstance(block, EmailBlock) and not caps.native_email:
            out.append(email_to_text(block))
        elif isinstance(block, TemplateBlock) and not caps.templates:
            out.append(TextBlock(text=f"[template:{block.template_name}]"))
        else:
            out.append(block)
    # text length pass (after conversions so degraded text also splits)
    final: list[ContentBlock] = []
    for block in out:
        if isinstance(block, TextBlock) and len(block.text) > caps.max_text_len:
            final.extend(TextBlock(text=p) for p in split_text(block.text, caps.max_text_len))
        else:
            final.append(block)
    return MessageContent(blocks=final)


def content_has_template(content: MessageContent) -> bool:
    return any(isinstance(b, TemplateBlock) for b in content.blocks)


def primary_msg_type(content: MessageContent) -> str:
    """messages.msg_type from the first meaningful block."""
    for b in content.blocks:
        if isinstance(b, MediaBlock):
            return b.media_type
        if isinstance(b, TextBlock):
            return "text"
        return b.kind
    return "text"


# --------------------------------------------------------------------------
# adapter protocol + base implementation
# --------------------------------------------------------------------------


@runtime_checkable
class ChannelAdapter(Protocol):
    channel_type: ClassVar[str]

    @property
    def capabilities(self) -> Capabilities: ...

    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> bool: ...

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]: ...

    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]: ...

    async def send(
        self, account: AccountRef, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult: ...

    async def send_typing(
        self, account: AccountRef, credentials: dict[str, Any], to: str, on: bool = True
    ) -> None: ...

    async def mark_read(
        self,
        account: AccountRef,
        credentials: dict[str, Any],
        *,
        external_message_id: str | None = None,
        to: str | None = None,
    ) -> None: ...

    async def fetch_media(
        self, account: AccountRef, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None: ...

    async def check_health(self, account: AccountRef, credentials: dict[str, Any]) -> HealthResult: ...

    async def refresh_credentials(
        self, account: AccountRef, credentials: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult: ...

    async def enrich_outbound(
        self,
        session: Any,
        *,
        account: AccountRef,
        credentials: dict[str, Any],
        conversation: Any,
        identity: Any,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]: ...


class BaseAdapter:
    """Default implementations; concrete adapters override what they support.
    The httpx client is injectable for tests (MockTransport)."""

    channel_type: ClassVar[str] = ""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        return self._http

    @property
    def capabilities(self) -> Capabilities:
        return capabilities_for(self.channel_type)

    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> bool:
        # default: path-secret channels (telegram/bridge) — the router already
        # matched the secret; an extra header, when present, must also match.
        return True

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        return parse_normalized_events(payload)

    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities)
        return [{"blocks": degraded.model_dump(mode="json")["blocks"]}]

    async def send(
        self, account: AccountRef, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:  # pragma: no cover - must be overridden for real channels
        return SendResult(ok=False, error_code="UNSUPPORTED_CONTENT", error_message="send not implemented")

    async def send_typing(
        self, account: AccountRef, credentials: dict[str, Any], to: str, on: bool = True
    ) -> None:
        return None

    async def mark_read(
        self,
        account: AccountRef,
        credentials: dict[str, Any],
        *,
        external_message_id: str | None = None,
        to: str | None = None,
    ) -> None:
        return None

    async def fetch_media(
        self, account: AccountRef, credentials: dict[str, Any], ref: dict[str, Any]
    ) -> MediaFetched | None:
        if ref.get("kind") == "url" and ref.get("url"):
            try:
                r = await self.http.get(ref["url"])
                r.raise_for_status()
                return MediaFetched(
                    data=r.content,
                    mime=r.headers.get("content-type"),
                    filename=ref.get("filename"),
                )
            except httpx.HTTPError:
                return None
        return None

    async def check_health(self, account: AccountRef, credentials: dict[str, Any]) -> HealthResult:
        return HealthResult(ok=True)

    async def refresh_credentials(
        self, account: AccountRef, credentials: dict[str, Any]
    ) -> dict[str, Any] | None:
        return None

    async def connect_validate(
        self, config: dict[str, Any], credentials: dict[str, Any]
    ) -> ConnectResult:
        """Connect-time validation + account-identity resolution.

        The default accepts the connection, taking external_id from an explicit
        config hint (or a generated uuid) and requesting a per-account webhook
        secret. Real Phase-4 adapters override this to authenticate the
        credentials, discover the provider account id, register the webhook and
        set health accordingly."""
        external_id = str(
            config.get("external_id")
            or config.get("app_id")
            or config.get("account_id")
            or uuid.uuid4()
        )
        return ConnectResult(
            external_id=external_id,
            name=str(config.get("name") or ""),
            health=HealthResult(ok=True, status="active"),
            needs_webhook_secret=True,
        )

    async def enrich_outbound(
        self,
        session: Any,
        *,
        account: AccountRef,
        credentials: dict[str, Any],
        conversation: Any,
        identity: Any,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return payloads

    # shared helper for graph-style / REST errors
    @staticmethod
    def network_error(exc: Exception) -> SendResult:
        return SendResult(ok=False, error_code="NETWORK", error_message=str(exc)[:500])


__all__ = [
    "AccountRef",
    "AccountStatus",
    "BaseAdapter",
    "ButtonReplyBlock",
    "CAPABILITIES",
    "Capabilities",
    "ChannelAdapter",
    "ConnectResult",
    "ContactUpdate",
    "DeliveryStatus",
    "ErrorCode",
    "HealthResult",
    "InboundEvent",
    "MediaFetched",
    "MediaRef",
    "MessageIn",
    "OptOut",
    "ProfileHint",
    "ReadReceipt",
    "RETRYABLE_CODES",
    "SendResult",
    "TypingIn",
    "bridge_signature",
    "capabilities_for",
    "card_to_blocks",
    "content_has_template",
    "degrade_content",
    "email_to_text",
    "line_signature",
    "location_to_text",
    "media_fallback_text",
    "parse_normalized_events",
    "primary_msg_type",
    "quick_buttons_to_menu",
    "secrets_equal",
    "split_text",
    "verify_bridge_signature",
    "verify_line_signature",
    "verify_meta_signature",
]
