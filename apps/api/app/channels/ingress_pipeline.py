"""Ingress normalizer (plan A.7): webhook handlers XADD raw payloads onto
ingress:{channel_type} Redis Streams; this consumer-group worker normalizes,
dedupes and persists them.

Per raw entry:
  adapter.parse_inbound → for each MessageIn:
    per-identity Redis lock (ordering) →
    message_dedup INSERT ON CONFLICT DO NOTHING (dup → drop) →
    media fetch → MinIO copy →
    upsert channel_identity (+Contact create w/ profile hint → contact.created) →
    get-or-create conversation (conversation.created / reopened) →
    persist message (content jsonb + text_plain, counters, snippet, 24h window) →
    emit message.created via the transactional outbox.
  DeliveryStatus/ReadReceipt about messages we don't know yet are parked in
  Redis for 10 minutes and replayed once the send records its external id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from py_contracts.content import MessageContent, TextBlock
from py_contracts.events import Actor, Event
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.channels import ChannelAccount
from ..models.contacts import ChannelIdentity, Contact
from ..models.conversations import Conversation
from ..models.messaging import Message, MessageDedup
from ..services import event_bus
from .base import (
    AccountStatus,
    ChannelAdapter,
    ContactUpdate,
    DeliveryStatus,
    InboundEvent,
    MessageIn,
    OptOut,
    ReadReceipt,
    TypingIn,
    capabilities_for,
    primary_msg_type,
)
from .creds import get_credentials
from .media import get_media_store
from .registry import get_adapter, registered_channel_types

try:  # uuid7 package exposes uuid7()
    from uuid_extensions import uuid7  # type: ignore
except ImportError:  # pragma: no cover
    from uuid6 import uuid7  # type: ignore

log = logging.getLogger("smartchat.channels.ingress")

GROUP = "ingress"
ORPHAN_TTL_S = 600  # status-before-message parking lot (plan: 10 minutes)
SNIPPET_LEN = 140

_STATUS_ORDER = {"pending": 0, "sending": 1, "sent": 2, "delivered": 3, "read": 4}


def ingress_stream(channel_type: str) -> str:
    return f"ingress:{channel_type}"


def all_ingress_streams() -> list[str]:
    return [ingress_stream(ct) for ct in registered_channel_types()]


def status_can_advance(current: str, new: str) -> bool:
    """Delivery statuses only move forward; failed always applies."""
    if new == "failed":
        return current != "failed"
    return _STATUS_ORDER.get(new, 0) > _STATUS_ORDER.get(current, 0)


# --------------------------------------------------------------------------
# enqueue (called by hooks router / widget module / bridges)
# --------------------------------------------------------------------------
async def enqueue_inbound(
    redis: aioredis.Redis,
    *,
    account_id: uuid.UUID | str,
    workspace_id: uuid.UUID | str,
    channel_type: str,
    payload: dict[str, Any],
) -> str:
    return await redis.xadd(
        ingress_stream(channel_type),
        {
            "account_id": str(account_id),
            "workspace_id": str(workspace_id),
            "channel_type": channel_type,
            "payload": json.dumps(payload, separators=(",", ":"), default=str),
        },
        maxlen=100_000,
        approximate=True,
    )


# --------------------------------------------------------------------------
# orphan parking lot (status events that beat their message)
# --------------------------------------------------------------------------
def park_key(account_id: uuid.UUID | str, external_message_id: str) -> str:
    return f"ingress:park:{account_id}:{external_message_id}"


async def park_status(
    redis: aioredis.Redis, account_id: uuid.UUID | str, ev: DeliveryStatus
) -> None:
    key = park_key(account_id, ev.external_message_id)
    await redis.rpush(key, ev.model_dump_json())
    await redis.expire(key, ORPHAN_TTL_S)


async def pop_parked(
    redis: aioredis.Redis, account_id: uuid.UUID | str, external_message_id: str
) -> list[DeliveryStatus]:
    key = park_key(account_id, external_message_id)
    raw = await redis.lrange(key, 0, -1)
    if raw:
        await redis.delete(key)
    out: list[DeliveryStatus] = []
    for item in raw or []:
        try:
            out.append(DeliveryStatus.model_validate_json(item))
        except Exception:  # noqa: BLE001
            log.warning("dropping unparseable parked status on %s", key)
    return out


# --------------------------------------------------------------------------
# identity ordering lock
# --------------------------------------------------------------------------
class _IdentityLock:
    def __init__(self, redis: aioredis.Redis, key: str, ttl: int = 30, max_wait: float = 10.0):
        self.redis, self.key, self.ttl, self.max_wait = redis, key, ttl, max_wait
        self.token = uuid.uuid4().hex

    async def __aenter__(self) -> None:
        waited = 0.0
        while not await self.redis.set(self.key, self.token, nx=True, ex=self.ttl):
            await asyncio.sleep(0.1)
            waited += 0.1
            if waited >= self.max_wait:  # proceed anyway — at-least-once beats deadlock
                log.warning("identity lock %s busy for %.1fs; proceeding", self.key, waited)
                return

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if await self.redis.get(self.key) == self.token:
                await self.redis.delete(self.key)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# event handlers
# --------------------------------------------------------------------------
async def handle_events(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    account_id: uuid.UUID,
    events: list[InboundEvent],
    *,
    adapter: ChannelAdapter | None = None,
) -> None:
    for ev in events:
        try:
            if isinstance(ev, MessageIn):
                await _handle_message_in(session_factory, redis, account_id, ev, adapter)
            elif isinstance(ev, DeliveryStatus):
                await _handle_delivery_status(session_factory, redis, account_id, ev)
            elif isinstance(ev, ReadReceipt):
                await _handle_read_receipt(session_factory, account_id, ev)
            elif isinstance(ev, ContactUpdate):
                await _handle_contact_update(session_factory, account_id, ev)
            elif isinstance(ev, AccountStatus):
                await _handle_account_status(session_factory, redis, account_id, ev)
            elif isinstance(ev, TypingIn):
                await _handle_typing(session_factory, redis, account_id, ev)
            elif isinstance(ev, OptOut):
                await _handle_opt_out(session_factory, account_id, ev)
        except Exception:  # noqa: BLE001 — one poison event must not sink the batch
            log.exception("ingress event failed account=%s kind=%s", account_id, ev.kind)


async def _handle_message_in(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    account_id: uuid.UUID,
    ev: MessageIn,
    adapter: ChannelAdapter | None,
) -> None:
    lock = _IdentityLock(redis, f"ingress:lock:{account_id}:{ev.external_user_id}")
    async with lock:
        # cheap duplicate pre-check (authoritative check = INSERT conflict below)
        async with session_factory() as session:
            if await session.get(MessageDedup, (account_id, ev.external_message_id)):
                return
            acct = await session.get(ChannelAccount, account_id)
            if acct is None:
                log.warning("ingress for unknown account %s", account_id)
                return
            adapter = adapter or get_adapter(acct.channel_type)
            credentials = await get_credentials(session, acct) if ev.media_refs else {}

        # media download outside any transaction (network)
        fetched: list[tuple[int, Any]] = []
        if ev.media_refs:
            from .base import AccountRef

            async with session_factory() as session:
                acct = await session.get(ChannelAccount, account_id)
                ref_acct = AccountRef.from_row(acct)
            for mref in ev.media_refs:
                got = await adapter.fetch_media(ref_acct, credentials, mref.ref)
                fetched.append((mref.block_index, got))

        async with session_factory() as session:
            async with session.begin():
                acct = await session.get(ChannelAccount, account_id)
                if acct is None:
                    return
                msg_id = uuid7()
                ins = (
                    pg_insert(MessageDedup)
                    .values(
                        channel_account_id=account_id,
                        external_message_id=ev.external_message_id,
                        workspace_id=acct.workspace_id,
                        message_id=msg_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["channel_account_id", "external_message_id"]
                    )
                )
                res = await session.execute(ins)
                if (res.rowcount or 0) == 0:
                    return  # duplicate delivery

                # store fetched media / replace failed blocks
                store = get_media_store()
                for block_index, got in fetched:
                    if block_index >= len(ev.content.blocks):
                        continue
                    block = ev.content.blocks[block_index]
                    if got is None:
                        ev.content.blocks[block_index] = TextBlock(
                            text=f"[{getattr(block, 'media_type', 'media')} unavailable]"
                        )
                        continue
                    f = await store.store_bytes(
                        session,
                        workspace_id=acct.workspace_id,
                        data=got.data,
                        mime=got.mime or getattr(block, "mime", None),
                        filename=got.filename,
                        created_by_type="contact",
                    )
                    block.file_id = f.id  # type: ignore[union-attr]
                    if getattr(block, "mime", None) is None:
                        block.mime = got.mime  # type: ignore[union-attr]
                    if getattr(block, "size", None) in (None, 0):
                        block.size = len(got.data)  # type: ignore[union-attr]

                identity, contact, contact_created = await _upsert_identity(session, acct, ev)
                conv, conv_created, conv_reopened = await _get_or_create_conversation(
                    session, acct, identity
                )
                now = datetime.now(UTC)
                content = MessageContent(blocks=ev.content.blocks)
                text_plain = content.plain_text() or None
                msg = Message(
                    id=msg_id,
                    workspace_id=acct.workspace_id,
                    conversation_id=conv.id,
                    channel_identity_id=identity.id,
                    direction="in",
                    sender_type="contact",
                    sender_id=contact.id,
                    msg_type=primary_msg_type(content),
                    content=content.model_dump(mode="json"),
                    text_plain=text_plain,
                    external_message_id=ev.external_message_id,
                    delivery_status="delivered",
                    external_timestamp=ev.external_timestamp,
                )
                session.add(msg)

                conv.needs_reply = True
                conv.agent_unread_count = (conv.agent_unread_count or 0) + 1
                conv.last_message_at = now
                conv.last_contact_message_at = now
                conv.snippet = (text_plain or f"[{msg.msg_type}]")[:SNIPPET_LEN]
                caps = capabilities_for(acct.channel_type)
                if caps.session_window_hours:
                    conv.customer_window_expires_at = now + timedelta(
                        hours=caps.session_window_hours
                    )

                base_kwargs: dict[str, Any] = {
                    "workspace_id": acct.workspace_id,
                    "conversation_id": conv.id,
                    "contact_id": contact.id,
                    "channel_type": acct.channel_type,
                    "channel_account_id": acct.id,
                }
                if contact_created:
                    await event_bus.emit(
                        session,
                        Event(
                            type="contact.created",
                            actor=Actor(type="contact", id=contact.id),
                            payload={
                                "contact_id": str(contact.id),
                                "display_name": contact.display_name,
                                "source_channel": acct.channel_type,
                            },
                            **{**base_kwargs, "conversation_id": None},
                        ),
                    )
                if conv_created:
                    await event_bus.emit(
                        session,
                        Event(
                            type="conversation.created",
                            actor=Actor(type="contact", id=contact.id),
                            payload={"channel_identity_id": str(identity.id)},
                            **base_kwargs,
                        ),
                    )
                elif conv_reopened:
                    await event_bus.emit(
                        session,
                        Event(
                            type="conversation.reopened",
                            actor=Actor(type="contact", id=contact.id),
                            payload={},
                            **base_kwargs,
                        ),
                    )
                await event_bus.emit(
                    session,
                    Event(
                        type="message.created",
                        actor=Actor(type="contact", id=contact.id),
                        payload={
                            "message_id": str(msg.id),
                            "direction": "in",
                            "msg_type": msg.msg_type,
                            "text_plain": (text_plain or "")[:500],
                            "external_message_id": ev.external_message_id,
                            "channel_identity_id": str(identity.id),
                        },
                        **base_kwargs,
                    ),
                )

    # replay any statuses that referenced this (rare, but harmless)
    parked = await pop_parked(redis, account_id, ev.external_message_id)
    for status_ev in parked:
        await _handle_delivery_status(session_factory, redis, account_id, status_ev)


async def _upsert_identity(
    session: AsyncSession, acct: ChannelAccount, ev: MessageIn
) -> tuple[ChannelIdentity, Contact, bool]:
    now = datetime.now(UTC)
    identity = (
        await session.execute(
            select(ChannelIdentity).where(
                ChannelIdentity.channel_account_id == acct.id,
                ChannelIdentity.external_user_id == ev.external_user_id,
            )
        )
    ).scalar_one_or_none()
    hint = ev.profile
    if identity is None:
        contact = Contact(
            workspace_id=acct.workspace_id,
            display_name=hint.display_name or ev.external_user_id,
            avatar_url=hint.avatar_url,
            email=hint.email,
            phone=hint.phone,
            language=hint.language,
            country=hint.country,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(contact)
        await session.flush()
        identity = ChannelIdentity(
            workspace_id=acct.workspace_id,
            channel_account_id=acct.id,
            channel_type=acct.channel_type,
            external_user_id=ev.external_user_id,
            contact_id=contact.id,
            display_name=hint.display_name,
            avatar_url=hint.avatar_url,
            meta=hint.meta or {},
            last_seen_at=now,
        )
        session.add(identity)
        await session.flush()
        return identity, contact, True
    identity.last_seen_at = now
    if hint.display_name and identity.display_name != hint.display_name:
        identity.display_name = hint.display_name
    if hint.avatar_url:
        identity.avatar_url = hint.avatar_url
    contact = await session.get(Contact, identity.contact_id)
    if contact is None:  # should not happen; heal
        contact = Contact(
            workspace_id=acct.workspace_id,
            display_name=hint.display_name or ev.external_user_id,
            first_seen_at=now,
        )
        session.add(contact)
        await session.flush()
        identity.contact_id = contact.id
    contact.last_seen_at = now
    if hint.display_name and not contact.display_name:
        contact.display_name = hint.display_name
    if hint.email and not contact.email:
        contact.email = hint.email
    if hint.phone and not contact.phone:
        contact.phone = hint.phone
    if hint.language and not contact.language:
        contact.language = hint.language
    return identity, contact, False


async def _get_or_create_conversation(
    session: AsyncSession, acct: ChannelAccount, identity: ChannelIdentity
) -> tuple[Conversation, bool, bool]:
    conv = (
        await session.execute(
            select(Conversation).where(Conversation.channel_identity_id == identity.id)
        )
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            workspace_id=acct.workspace_id,
            channel_identity_id=identity.id,
            channel_account_id=acct.id,
            channel_type=acct.channel_type,
            contact_id=identity.contact_id,
            status="open",
            handler="unassigned",
            session_count=1,
        )
        session.add(conv)
        await session.flush()
        return conv, True, False
    reopened = False
    if conv.status == "closed":
        conv.status = "open"
        conv.handler = "unassigned"
        conv.assignee_member_id = None
        conv.closed_at = None
        conv.session_count = (conv.session_count or 0) + 1
        reopened = True
    # keep denorm contact pointer fresh (merges re-point identities)
    if conv.contact_id != identity.contact_id:
        conv.contact_id = identity.contact_id
    return conv, False, reopened


async def _handle_delivery_status(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    account_id: uuid.UUID,
    ev: DeliveryStatus,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            acct = await session.get(ChannelAccount, account_id)
            if acct is None:
                return
            dedup = await session.get(MessageDedup, (account_id, ev.external_message_id))
            if dedup is None or dedup.message_id is None:
                await park_status(redis, account_id, ev)
                return
            await apply_delivery_status(session, acct, dedup.message_id, ev)


async def apply_delivery_status(
    session: AsyncSession,
    acct: ChannelAccount,
    message_id: uuid.UUID,
    ev: DeliveryStatus,
) -> bool:
    msg = (
        await session.execute(select(Message).where(Message.id == message_id))
    ).scalar_one_or_none()
    if msg is None:
        return False
    if not status_can_advance(msg.delivery_status, ev.status):
        return False
    msg.delivery_status = ev.status
    if ev.status == "failed":
        msg.delivery_error = ev.error_code or ev.error_message or "failed"
    await event_bus.emit(
        session,
        Event(
            workspace_id=acct.workspace_id,
            type="message.updated",
            actor=Actor(type="system"),
            conversation_id=msg.conversation_id,
            channel_type=acct.channel_type,
            channel_account_id=acct.id,
            payload={
                "message_id": str(msg.id),
                "delivery_status": ev.status,
                "error_code": ev.error_code,
            },
        ),
    )
    return True


async def _handle_read_receipt(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    ev: ReadReceipt,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            acct = await session.get(ChannelAccount, account_id)
            if acct is None:
                return
            identity = (
                await session.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.channel_account_id == account_id,
                        ChannelIdentity.external_user_id == ev.external_user_id,
                    )
                )
            ).scalar_one_or_none()
            if identity is None:
                return
            conv = (
                await session.execute(
                    select(Conversation).where(Conversation.channel_identity_id == identity.id)
                )
            ).scalar_one_or_none()
            if conv is None:
                return
            await session.execute(
                update(Message)
                .where(
                    Message.conversation_id == conv.id,
                    Message.direction == "out",
                    Message.delivery_status.in_(["sent", "delivered"]),
                    Message.created_at <= ev.watermark,
                )
                .values(delivery_status="read")
            )
            await event_bus.emit(
                session,
                Event(
                    workspace_id=acct.workspace_id,
                    type="message.updated",
                    actor=Actor(type="contact", id=identity.contact_id),
                    conversation_id=conv.id,
                    contact_id=identity.contact_id,
                    channel_type=acct.channel_type,
                    channel_account_id=acct.id,
                    payload={"read_watermark": ev.watermark.isoformat()},
                ),
            )


async def _handle_contact_update(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    ev: ContactUpdate,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            acct = await session.get(ChannelAccount, account_id)
            if acct is None:
                return
            identity = (
                await session.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.channel_account_id == account_id,
                        ChannelIdentity.external_user_id == ev.external_user_id,
                    )
                )
            ).scalar_one_or_none()
            if identity is None:
                return
            hint = ev.profile
            if hint.display_name:
                identity.display_name = hint.display_name
            if hint.avatar_url:
                identity.avatar_url = hint.avatar_url
            contact = await session.get(Contact, identity.contact_id)
            if contact is None:
                return
            changed = {}
            for field_name in ("display_name", "avatar_url", "email", "phone", "language", "country"):
                v = getattr(hint, field_name, None)
                if v and getattr(contact, field_name, None) != v:
                    setattr(contact, field_name, v)
                    changed[field_name] = v
            if changed:
                await event_bus.emit(
                    session,
                    Event(
                        workspace_id=acct.workspace_id,
                        type="contact.updated",
                        actor=Actor(type="system"),
                        contact_id=contact.id,
                        channel_type=acct.channel_type,
                        channel_account_id=acct.id,
                        payload={"contact_id": str(contact.id), "changed": changed},
                    ),
                )


async def _handle_account_status(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    account_id: uuid.UUID,
    ev: AccountStatus,
) -> None:
    from .sender import pause_key  # local import to avoid cycle

    async with session_factory() as session:
        async with session.begin():
            acct = await session.get(ChannelAccount, account_id)
            if acct is None:
                return
            mapped = {
                "online": "active",
                "offline": "disconnected",
            }.get(ev.status, ev.status)
            acct.status = mapped[:16]
            acct.health = {**(acct.health or {}), "last_status": ev.status, "detail": ev.detail}
            await event_bus.emit(
                session,
                Event(
                    workspace_id=acct.workspace_id,
                    type="channel.status",
                    actor=Actor(type="system"),
                    channel_type=acct.channel_type,
                    channel_account_id=acct.id,
                    payload={"status": acct.status, "detail": ev.detail},
                ),
            )
    if ev.status in ("token_expired", "banned", "logged_out", "disconnected", "offline"):
        await redis.set(pause_key(account_id), ev.status, ex=1800)
    elif ev.status in ("active", "online"):
        await redis.delete(pause_key(account_id))


async def _handle_typing(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    account_id: uuid.UUID,
    ev: TypingIn,
) -> None:
    """Typing is ephemeral (never persisted, plan A.8): published straight to
    the bus stream for the realtime gateway, bypassing the outbox."""
    async with session_factory() as session:
        acct = await session.get(ChannelAccount, account_id)
        if acct is None:
            return
        identity = (
            await session.execute(
                select(ChannelIdentity).where(
                    ChannelIdentity.channel_account_id == account_id,
                    ChannelIdentity.external_user_id == ev.external_user_id,
                )
            )
        ).scalar_one_or_none()
        conv = None
        if identity is not None:
            conv = (
                await session.execute(
                    select(Conversation).where(Conversation.channel_identity_id == identity.id)
                )
            ).scalar_one_or_none()
    event = Event(
        workspace_id=acct.workspace_id,
        type="typing.visitor",
        actor=Actor(type="contact", id=identity.contact_id if identity else None),
        conversation_id=conv.id if conv else None,
        channel_type=acct.channel_type,
        channel_account_id=acct.id,
        payload={"is_typing": ev.is_typing, "external_user_id": ev.external_user_id},
    )
    await redis.xadd(
        event_bus.stream_for("typing.visitor"),
        event_bus.encode_fields(event),
        maxlen=10_000,
        approximate=True,
    )


async def _handle_opt_out(
    session_factory: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    ev: OptOut,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            acct = await session.get(ChannelAccount, account_id)
            if acct is None:
                return
            identity = (
                await session.execute(
                    select(ChannelIdentity).where(
                        ChannelIdentity.channel_account_id == account_id,
                        ChannelIdentity.external_user_id == ev.external_user_id,
                    )
                )
            ).scalar_one_or_none()
            if identity is None:
                return
            contact = await session.get(Contact, identity.contact_id)
            if contact is None:
                return
            contact.custom = {
                **(contact.custom or {}),
                "opt_out": {"scope": ev.scope, "reason": ev.reason, "at": datetime.now(UTC).isoformat()},
            }
            await event_bus.emit(
                session,
                Event(
                    workspace_id=acct.workspace_id,
                    type="contact.updated",
                    actor=Actor(type="system"),
                    contact_id=contact.id,
                    channel_type=acct.channel_type,
                    channel_account_id=acct.id,
                    payload={"contact_id": str(contact.id), "opt_out": ev.scope},
                ),
            )


# --------------------------------------------------------------------------
# stream consumer
# --------------------------------------------------------------------------
async def process_entry(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    fields: dict[str, Any],
) -> None:
    account_id = uuid.UUID(str(fields["account_id"]))
    payload = json.loads(fields["payload"])
    async with session_factory() as session:
        acct = await session.get(ChannelAccount, account_id)
        if acct is None or not acct.enabled:
            log.info("dropping ingress entry for missing/disabled account %s", account_id)
            return
        adapter = get_adapter(acct.channel_type)
    try:
        events = adapter.parse_inbound(payload)
    except Exception:  # noqa: BLE001
        log.exception("parse_inbound failed account=%s", account_id)
        return
    await handle_events(session_factory, redis, account_id, events, adapter=adapter)


def _consumer_name() -> str:
    return f"{os.environ.get('HOSTNAME', 'local')}-{os.getpid()}"


async def ensure_groups(redis: aioredis.Redis) -> None:
    for stream in all_ingress_streams():
        await event_bus.ensure_group(redis, stream, GROUP)


async def consume_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    consumer: str | None = None,
    count: int = 32,
    block_ms: int = 2000,
    start_id: str = ">",
) -> int:
    """One consumer-group read across all ingress streams. Entries are acked
    after processing (at-least-once). Returns entries handled."""
    consumer = consumer or _consumer_name()
    streams = all_ingress_streams()
    resp = await redis.xreadgroup(
        GROUP, consumer, {s: start_id for s in streams}, count=count, block=block_ms
    )
    handled = 0
    for stream, entries in resp or []:
        for entry_id, fields in entries:
            if not fields:  # pending-scan tombstone
                await redis.xack(stream, GROUP, entry_id)
                continue
            try:
                await process_entry(session_factory, redis, fields)
            except Exception:  # noqa: BLE001
                log.exception("ingress entry %s failed on %s (acking to avoid poison loop)", entry_id, stream)
            await redis.xack(stream, GROUP, entry_id)
            handled += 1
    return handled


async def run_ingress_loop(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Dedicated long-running consumer (channel-ingress worker entrypoint).
    On boot it first drains this consumer's own pending entries (crash
    recovery), then tails new ones."""
    await ensure_groups(redis)
    consumer = _consumer_name()
    try:
        await consume_once(
            session_factory, redis, consumer=consumer, count=128, block_ms=1, start_id="0"
        )
    except Exception:  # noqa: BLE001
        log.exception("pending drain failed")
    while stop is None or not stop.is_set():
        try:
            await consume_once(session_factory, redis, consumer=consumer)
        except Exception:  # noqa: BLE001
            log.exception("ingress consume pass failed")
            await asyncio.sleep(1.0)
