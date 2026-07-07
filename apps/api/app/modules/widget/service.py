"""Visitor-facing widget service: session/identity lifecycle + bootstrap
assembly. The REST router stays thin; message ingest reuses the shared
channel ingress pipeline (handle_events) so widget messages take the exact
same routing/unread/event path as every other channel (plan A.7)."""
from __future__ import annotations

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
    return {
        "widget_key": widget.widget_key,
        "brand": cfg.get("brand") or {"name": widget.name or None},
        "appearance": appearance,
        "locale_default": cfg.get("locale_default"),
        "pre_chat": cfg.get("pre_chat"),
        "offline": offline,
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
