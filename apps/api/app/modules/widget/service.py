"""Visitor-facing widget service: session/identity lifecycle + bootstrap
assembly. The REST router stays thin; message ingest reuses the shared
channel ingress pipeline (handle_events) so widget messages take the exact
same routing/unread/event path as every other channel (plan A.7)."""
from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from py_contracts.events import Actor, Event
from redis import asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.channels import ChannelAccount, Widget
from ...models.contacts import ChannelIdentity, Contact, VisitorEvent
from ...models.conversations import Conversation
from ...models.members import WorkspaceMember
from ...realtime import presence
from ...services import event_bus
from ...services.quotas import effective_limits

VISITOR_PREFIX = "v_"


def new_visitor_external_id() -> str:
    return VISITOR_PREFIX + secrets.token_hex(12)


async def get_widget_by_key(session: AsyncSession, widget_key: str) -> Widget | None:
    return (
        await session.execute(
            select(Widget).where(Widget.widget_key == widget_key, Widget.enabled.is_(True))
        )
    ).scalar_one_or_none()


async def widget_channel_account(session: AsyncSession, widget: Widget) -> ChannelAccount | None:
    if widget.channel_account_id is not None:
        acct = await session.get(ChannelAccount, widget.channel_account_id)
        if acct is not None:
            return acct
    return (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type == "widget",
                ChannelAccount.external_id == widget.widget_key,
            )
        )
    ).scalar_one_or_none()


async def any_agent_online(session: AsyncSession, redis: aioredis.Redis, workspace_id) -> bool:
    member_ids = (
        (
            await session.execute(
                select(WorkspaceMember.id).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.status == "active",
                    WorkspaceMember.member_type == "human",
                )
            )
        )
        .scalars()
        .all()
    )
    if not member_ids:
        return False
    keys = [presence.member_key(workspace_id, mid) for mid in member_ids]
    states = await redis.mget(keys)
    return any(s in (b"online", "online") for s in states if s)


# icon_key + default label per channel family that can surface a visitor
# "contact us" entry. Channels absent here (slack/youtube/tiktok/…) have no
# visitor-facing deep link and are never offered.
_SOCIAL_META: dict[str, tuple[str, str]] = {
    "whatsapp_bsp": ("whatsapp", "WhatsApp"),
    "whatsapp_cloud": ("whatsapp", "WhatsApp"),
    "whatsapp_app": ("whatsapp", "WhatsApp"),
    "telegram_bot": ("telegram", "Telegram"),
    "messenger": ("messenger", "Messenger"),
    "instagram": ("instagram", "Instagram"),
    "line_oa": ("line", "LINE"),
    "email": ("email", "Email"),
    "vk": ("vk", "VK"),
    "zalo_app": ("zalo", "Zalo"),
    "wechat_kf": ("wechat", "WeChat"),
}

# personal-number / internal channels never auto-shown to the public web (would
# leak a private phone or an internal workspace); the admin can opt them in.
_SOCIAL_DEFAULT_OFF = frozenset({"whatsapp_app"})


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def channel_contact_entry(acct: ChannelAccount) -> dict[str, Any] | None:
    """Derive a SAFE visitor contact entry from a connected account. Returns
    {channel_type, label, kind, url|value, icon_key} or None when no public
    handle is available (probe failed, no link). NEVER exposes external_id or
    health beyond the single derived link — the bootstrap endpoint is public.
    kind="link" opens a deep link; kind="copy" surfaces an id to copy (channels
    with no URL scheme, e.g. WeChat)."""
    meta = _SOCIAL_META.get(acct.channel_type)
    if meta is None:
        return None
    icon_key, label = meta
    health = acct.health or {}
    ext = acct.external_id or ""
    ct = acct.channel_type
    url: str | None = None
    if ct == "whatsapp_bsp":
        d = _digits(ext)
        url = f"https://wa.me/{d}" if d else None
    elif ct == "whatsapp_cloud":
        d = _digits(health.get("display_phone_number"))
        url = f"https://wa.me/{d}" if d else None
    elif ct == "whatsapp_app":
        d = _digits(health.get("phone"))
        url = f"https://wa.me/{d}" if d else None
    elif ct == "telegram_bot":
        u = str(health.get("username") or "").lstrip("@")
        url = f"https://t.me/{u}" if u else None
    elif ct == "messenger":
        url = f"https://m.me/{ext}" if ext else None
    elif ct == "line_oa":
        bid = str(health.get("basic_id") or "").lstrip("@")
        url = f"https://line.me/R/ti/p/@{bid}" if bid else None
    elif ct == "email":
        addr = ext or health.get("address")
        url = f"mailto:{addr}" if addr else None
    elif ct == "vk":
        name = health.get("screen_name")
        url = f"https://vk.me/{name}" if name else None
    elif ct == "zalo_app":
        url = f"https://zalo.me/{ext}" if ext else None
    if url:
        return {"channel_type": ct, "label": label, "kind": "link",
                "url": url, "icon_key": icon_key}
    # Non-linkable channels: only surface a copyable value that is a REAL public
    # handle. WeChat 客服 has no URL scheme; its external_id is the internal WeCom
    # corp_id, which must never be exposed on the public bootstrap and is useless
    # to a visitor anyway — so only emit a copy entry if a genuine display handle
    # was captured (never falls back to external_id). instagram likewise has no
    # stored public @username → no entry. Both return None until a usable public
    # handle exists.
    if ct == "wechat_kf":
        handle = health.get("contact") or health.get("kf_handle")
        if handle:
            return {"channel_type": ct, "label": label, "kind": "copy",
                    "value": str(handle), "icon_key": icon_key}
    return None


async def _connected_social_accounts(
    session: AsyncSession, workspace_id
) -> list[ChannelAccount]:
    return list(
        (
            await session.execute(
                select(ChannelAccount)
                .where(
                    ChannelAccount.workspace_id == workspace_id,
                    ChannelAccount.enabled.is_(True),
                    ChannelAccount.status == "active",
                    ChannelAccount.channel_type != "widget",
                )
                .order_by(ChannelAccount.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _assemble_social(
    session: AsyncSession, workspace_id, social_cfg: dict[str, Any]
) -> dict[str, Any]:
    """Build the public social-entry block from all connected channels, filtered
    by the widget's per-channel visibility config. Auto by default: connect a
    channel → its entry appears (except personal/internal, opt-in)."""
    if not social_cfg.get("enabled", True):
        return {"enabled": False, "channels": []}
    hidden = set(social_cfg.get("hidden") or [])
    shown = set(social_cfg.get("shown") or [])
    labels = social_cfg.get("labels") or {}
    order = list(social_cfg.get("order") or [])
    entries: list[dict[str, Any]] = []
    for acct in await _connected_social_accounts(session, workspace_id):
        ct = acct.channel_type
        if ct in hidden:
            continue
        if ct in _SOCIAL_DEFAULT_OFF and ct not in shown:
            continue
        entry = channel_contact_entry(acct)
        if entry is None:
            continue
        if ct in labels and labels[ct]:
            entry["label"] = str(labels[ct])
        entries.append(entry)
    if order:
        rank = {ct: i for i, ct in enumerate(order)}
        entries.sort(key=lambda e: rank.get(e["channel_type"], len(order)))
    return {"enabled": True, "channels": entries}


async def assemble_bootstrap(
    session: AsyncSession, redis: aioredis.Redis, widget: Widget
) -> dict[str, Any]:
    """WidgetBootstrap shape (apps/widget/src/shared/config.ts)."""
    cfg: dict[str, Any] = widget.config or {}
    limits = await effective_limits(session, redis, widget.workspace_id)
    brand_removal_allowed = bool(limits.get("brand_removal", False))
    online = await any_agent_online(session, redis, widget.workspace_id)
    appearance = dict(cfg.get("appearance") or {})
    appearance["show_branding"] = not (widget.brand_removed and brand_removal_allowed)
    offline = dict(cfg.get("offline") or {})
    offline["is_online"] = online
    social = await _assemble_social(session, widget.workspace_id, dict(cfg.get("social") or {}))
    return {
        "widget_key": widget.widget_key,
        "brand": cfg.get("brand") or {"name": widget.name or None},
        "appearance": appearance,
        "locale_default": cfg.get("locale_default"),
        "home": cfg.get("home"),
        "pre_chat": cfg.get("pre_chat"),
        "offline": offline,
        "social": social,
        "features": cfg.get("features") or {"file_upload": True, "emoji": True},
    }


async def get_identity(
    session: AsyncSession, channel_identity_id: uuid.UUID
) -> ChannelIdentity | None:
    return await session.get(ChannelIdentity, channel_identity_id)


async def create_visitor(
    session: AsyncSession,
    acct: ChannelAccount,
    *,
    login_info: dict[str, Any] | None = None,
    lang: str | None = None,
    page: dict[str, Any] | None = None,
) -> tuple[ChannelIdentity, Contact, Conversation]:
    """New anonymous visitor: contact + channel_identity + persistent
    conversation thread (closed until the first message so empty widget opens
    never pollute 待分配)."""
    now = datetime.now(UTC)
    li = login_info or {}
    contact = Contact(
        workspace_id=acct.workspace_id,
        display_name=li.get("user_name") or _next_guest_name(),
        email=li.get("email"),
        phone=li.get("phone"),
        language=lang,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(contact)
    await session.flush()
    identity = ChannelIdentity(
        workspace_id=acct.workspace_id,
        channel_account_id=acct.id,
        channel_type="widget",
        external_user_id=new_visitor_external_id(),
        contact_id=contact.id,
        display_name=contact.display_name,
        logged_in_external_id=str(li["user_id"]) if li.get("user_id") else None,
        meta={"first_page": (page or {}).get("url")} if page else {},
        last_seen_at=now,
    )
    session.add(identity)
    await session.flush()
    conversation = Conversation(
        workspace_id=acct.workspace_id,
        channel_identity_id=identity.id,
        channel_account_id=acct.id,
        channel_type="widget",
        contact_id=contact.id,
        status="closed",  # opens on first message via the ingress pipeline
        handler="unassigned",
        session_count=0,
    )
    session.add(conversation)
    await session.flush()
    await event_bus.emit(
        session,
        Event(
            workspace_id=acct.workspace_id,
            type="visitor.identified",
            actor=Actor(type="contact", id=contact.id),
            contact_id=contact.id,
            channel_type="widget",
            channel_account_id=acct.id,
            payload={"kind": "new", "channel_identity_id": str(identity.id)},
        ),
    )
    return identity, contact, conversation


_GUEST_COUNTER_FALLBACK = 0


def _next_guest_name() -> str:
    global _GUEST_COUNTER_FALLBACK
    _GUEST_COUNTER_FALLBACK += 1
    return f"Guest_{secrets.randbelow(90000) + 10000}"


async def touch_returning_visitor(
    session: AsyncSession, identity: ChannelIdentity
) -> Conversation | None:
    now = datetime.now(UTC)
    identity.last_seen_at = now
    contact = await session.get(Contact, identity.contact_id)
    if contact is not None:
        contact.last_seen_at = now
    conv = (
        await session.execute(
            select(Conversation).where(Conversation.channel_identity_id == identity.id)
        )
    ).scalar_one_or_none()
    await event_bus.emit(
        session,
        Event(
            workspace_id=identity.workspace_id,
            type="visitor.identified",
            actor=Actor(type="contact", id=identity.contact_id),
            contact_id=identity.contact_id,
            channel_type="widget",
            channel_account_id=identity.channel_account_id,
            payload={"kind": "returning", "channel_identity_id": str(identity.id)},
        ),
    )
    return conv


async def apply_login_info(
    session: AsyncSession, identity: ChannelIdentity, login_info: dict[str, Any]
) -> None:
    """ssq setLoginInfo → stamp merchant-side id + fill contact fields.
    Merge-candidate generation (reversible auto-link, plan A.9) is delegated
    to the contacts service hook."""
    if login_info.get("user_id"):
        identity.logged_in_external_id = str(login_info["user_id"])
    contact = await session.get(Contact, identity.contact_id)
    if contact is None:
        return
    if login_info.get("user_name"):
        contact.display_name = str(login_info["user_name"])
        identity.display_name = contact.display_name
    if login_info.get("email") and not contact.email:
        contact.email = str(login_info["email"])
    if login_info.get("phone") and not contact.phone:
        contact.phone = str(login_info["phone"])
    from ..contacts.service import generate_merge_candidates

    await generate_merge_candidates(
        session, workspace_id=identity.workspace_id, contact_id=identity.contact_id
    )
    await event_bus.emit(
        session,
        Event(
            workspace_id=identity.workspace_id,
            type="contact.updated",
            actor=Actor(type="contact", id=identity.contact_id),
            contact_id=identity.contact_id,
            payload={"source": "widget_identify"},
        ),
    )


async def record_visitor_event(
    session: AsyncSession,
    identity: ChannelIdentity,
    *,
    event: str,
    url: str | None = None,
    title: str | None = None,
    referrer: str | None = None,
    props: dict[str, Any] | None = None,
) -> None:
    session.add(
        VisitorEvent(
            workspace_id=identity.workspace_id,
            contact_id=identity.contact_id,
            channel_identity_id=identity.id,
            event_type=event,
            url=url,
            title=title,
            referrer=referrer,
            meta=props or {},
        )
    )
    bus_type = {
        "page_view": "visitor.page_view",
        "widget_open": "widget.opened",
        "lead_submit": "lead.submitted",
    }.get(event)
    if bus_type:
        await event_bus.emit(
            session,
            Event(
                workspace_id=identity.workspace_id,
                type=bus_type,
                actor=Actor(type="contact", id=identity.contact_id),
                contact_id=identity.contact_id,
                channel_type="widget",
                channel_account_id=identity.channel_account_id,
                payload={
                    "channel_identity_id": str(identity.id),
                    "url": url,
                    "title": title,
                    "referrer": referrer,
                    **({"props": props} if props else {}),
                },
            ),
        )
