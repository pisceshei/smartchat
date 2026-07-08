"""Outbound send pipeline + conversation message side effects (plan A.5–A.7).

send_message() writes the message row and its transactional-outbox event in
ONE transaction (the caller's session); the channel sender worker consumes the
`message.created` event off the conversation stream and does the actual
channel I/O. Internal notes and system-event chips never reach a channel
(`requires_channel_send=False` in the event payload).

needs_reply / agent_unread_count semantics (plan A.5):
- inbound customer message  → needs_reply=True,  agent_unread_count += 1
- outbound REPLY (not note) → needs_reply=False
- internal note             → no state change, no channel send
Read cursors (conversation_reads) feed the realtime unread counters.

Realtime: services collect py_contracts Events; callers publish them via
publish_realtime() AFTER commit (the outbox relay is the at-least-once
safety net; the direct publish is the low-latency path).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from py_contracts.content import MessageContent
from py_contracts.events import Actor, Event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.contacts import Contact
from ..models.conversations import Conversation, ConversationSession
from ..models.messaging import File, Message
from ..models.misc import QuickReply
from . import quotas

log = logging.getLogger("smartchat.messaging")

SNIPPET_LEN = 140

# Channels whose platform enforces a customer-service window: outbound free-form
# messages are rejected once the window is closed (WhatsApp allows approved
# templates outside the window).
WINDOW_CHANNELS: frozenset[str] = frozenset({"whatsapp_cloud", "messenger", "instagram"})
TEMPLATE_CHANNELS: frozenset[str] = frozenset({"whatsapp_cloud"})

# Hard capability rejections (soft degradation — card→image+link etc. — is the
# adapter's job per the capability matrix; only impossible sends fail here).
_BLOCK_CHANNEL_RESTRICTIONS: dict[str, frozenset[str]] = {
    "template": TEMPLATE_CHANNELS,
    "email": frozenset({"email"}),
}


# --------------------------------------------------------------------------
# typed errors (routers map .code to HTTP 422 detail)
# --------------------------------------------------------------------------
class SendError(Exception):
    code = "SEND_ERROR"

    def __init__(self, detail: str = ""):
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class WindowExpiredError(SendError):
    code = "WINDOW_EXPIRED"


class UnsupportedContentError(SendError):
    code = "UNSUPPORTED_CONTENT"


class InvalidContentError(SendError):
    code = "INVALID_CONTENT"


class FileNotFoundError_(SendError):
    code = "FILE_NOT_FOUND"


class QuickReplyNotFoundError(SendError):
    code = "QUICK_REPLY_NOT_FOUND"


class ConversationClosedError(SendError):
    code = "CONVERSATION_CLOSED"


# --------------------------------------------------------------------------
# pure helpers (unit-tested)
# --------------------------------------------------------------------------
def msg_type_for(content: MessageContent) -> str:
    """messages.msg_type = the leading block's kind (media uses its subtype)."""
    if not content.blocks:
        return "text"
    b = content.blocks[0]
    if b.kind == "media":
        return b.media_type  # image/video/audio/voice/file/sticker
    return b.kind


def make_snippet(content: MessageContent, *, limit: int = SNIPPET_LEN) -> str:
    text = content.plain_text().replace("\n", " ").strip()
    if not text:
        kinds = {b.kind for b in content.blocks}
        if "media" in kinds:
            text = "[附件]"
        elif "product_card" in kinds:
            text = "[商品卡片]"
        elif "location" in kinds:
            text = "[位置]"
        else:
            text = "[訊息]"
    return text[:limit]


def window_is_open(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at > now


def ensure_sendable(
    *,
    channel_type: str,
    content: MessageContent,
    customer_window_expires_at: datetime | None,
    is_note: bool,
    now: datetime | None = None,
) -> None:
    """Window + hard-capability validation. Notes bypass everything (they
    never touch a channel). Raises typed SendError subclasses."""
    if is_note:
        return
    for block in content.blocks:
        allowed = _BLOCK_CHANNEL_RESTRICTIONS.get(block.kind)
        if allowed is not None and channel_type not in allowed:
            raise UnsupportedContentError(
                f"block '{block.kind}' is not supported on channel '{channel_type}'"
            )
    if channel_type in WINDOW_CHANNELS:
        now = now or datetime.now(UTC)
        has_template = any(b.kind == "template" for b in content.blocks)
        if not window_is_open(customer_window_expires_at, now) and not (
            has_template and channel_type in TEMPLATE_CHANNELS
        ):
            raise WindowExpiredError(
                "customer window expired — use a template message"
                if channel_type in TEMPLATE_CHANNELS
                else "customer window expired"
            )


def apply_inbound_transition(conversation: Any) -> None:
    """Inbound customer message → 待回覆 + unread bump (plan A.5)."""
    conversation.needs_reply = True
    conversation.agent_unread_count = int(conversation.agent_unread_count or 0) + 1


def apply_outbound_transition(conversation: Any, *, is_note: bool) -> None:
    """Outbound REPLY clears 待回覆; internal notes change nothing."""
    if not is_note:
        conversation.needs_reply = False


def parse_content(raw: dict[str, Any] | MessageContent) -> MessageContent:
    if isinstance(raw, MessageContent):
        return raw
    try:
        return MessageContent.model_validate(raw)
    except Exception as e:  # pydantic ValidationError
        raise InvalidContentError(str(e)) from e


# --------------------------------------------------------------------------
# realtime publish (module provided by the realtime service at integration)
# --------------------------------------------------------------------------
async def publish_realtime(events: Sequence[Event]) -> None:
    """Fan out domain events to live clients via the ws-gateway publisher.
    Call AFTER commit (the outbox relay is the durable path; this is the
    low-latency one). Customer-visible messages additionally address the
    widget visitor audience; internal notes and system chips stay agent-only."""
    if not events:
        return
    from ..realtime import publisher
    from ..realtime.protocol import AUDIENCE_AGENTS, visitor_audience

    for e in events:
        payload = e.payload or {}
        audiences: list[str] = [AUDIENCE_AGENTS]
        identity_id: uuid.UUID | None = None
        raw_ident = payload.get("channel_identity_id")
        if raw_ident:
            try:
                identity_id = uuid.UUID(str(raw_ident))
            except ValueError:
                identity_id = None
        if (
            identity_id is not None
            and e.type == "message.created"
            and not payload.get("is_note")
            and payload.get("msg_type") != "system_event"
        ):
            audiences.append(visitor_audience(identity_id))
        await publisher.publish(
            e.workspace_id,
            e.type,
            {
                "domain_event_id": str(e.id),
                "contact_id": str(e.contact_id) if e.contact_id else None,
                "channel_type": e.channel_type,
                **payload,
            },
            audiences,
            conversation_id=e.conversation_id,
            channel_identity_id=identity_id,
        )


async def dispatch_channel_sends(events: Sequence[Event]) -> None:
    """Enqueue the outbound channel job for every freshly-created message that
    needs real channel I/O (``requires_channel_send``). Call AFTER commit, next
    to publish_realtime — this is the low-latency path the module docstring
    refers to; the ``drain_pending_sends_task`` cron is the at-least-once safety
    net. Idempotent: ``send_outbound_message`` claims each message via a Redis
    NX guard, so a double dispatch (hot path + drain) is a harmless no-op. The
    widget adapter's ``send`` is itself a no-op, so widget rows resolve to
    ``sent`` without any real network call."""
    if not events:
        return
    ids: list[str] = []
    for e in events:
        if e.type != "message.created":
            continue
        payload = e.payload or {}
        if not payload.get("requires_channel_send"):
            continue
        mid = payload.get("message_id")
        if mid:
            ids.append(str(mid))
    if not ids:
        return
    from ..channels.sender import enqueue_send  # lazy: avoid service↔sender cycle

    for mid in ids:
        try:
            await enqueue_send(mid)
        except Exception:  # noqa: BLE001 — the pending-drain cron re-picks the row
            log.debug("hot-path enqueue_send failed for message %s", mid)


# --------------------------------------------------------------------------
# send pipeline
# --------------------------------------------------------------------------
@dataclass
class SendResult:
    message: Message
    events: list[Event] = field(default_factory=list)
    created: bool = True  # False = client_msg_id idempotent replay


async def expand_quick_reply(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    member_id: uuid.UUID | None,
    quick_reply_id: uuid.UUID,
) -> MessageContent:
    """話術庫 expansion: personal entries are only visible to their owner;
    bumps usage_count."""
    qr = await session.get(QuickReply, quick_reply_id)
    if qr is None or qr.workspace_id != workspace_id:
        raise QuickReplyNotFoundError("quick reply not found")
    if qr.scope == "personal" and qr.owner_member_id != member_id:
        raise QuickReplyNotFoundError("quick reply not found")
    qr.usage_count = int(qr.usage_count or 0) + 1
    return parse_content(qr.content or {})


async def _validate_file_refs(
    session: AsyncSession, workspace_id: uuid.UUID, content: MessageContent
) -> None:
    file_ids = {b.file_id for b in content.blocks if b.kind == "media" and b.file_id}
    for b in content.blocks:
        if b.kind == "product_card" and b.image_file_id:
            file_ids.add(b.image_file_id)
        if b.kind == "email" and b.html_body_file_id:
            file_ids.add(b.html_body_file_id)
    if not file_ids:
        return
    rows = set(
        (
            await session.execute(
                select(File.id).where(File.workspace_id == workspace_id, File.id.in_(file_ids))
            )
        ).scalars()
    )
    missing = file_ids - rows
    if missing:
        raise FileNotFoundError_(f"unknown file ids: {sorted(str(m) for m in missing)}")


async def _open_session(
    session: AsyncSession, conversation: Conversation
) -> ConversationSession | None:
    return (
        await session.execute(
            select(ConversationSession)
            .where(
                ConversationSession.conversation_id == conversation.id,
                ConversationSession.ended_at.is_(None),
            )
            .order_by(ConversationSession.started_at.desc())
            .limit(1)
        )
    ).scalars().first()


def _message_event(message: Message, conversation: Conversation, actor: Actor,
                   *, requires_channel_send: bool) -> Event:
    return Event(
        workspace_id=message.workspace_id,
        type="message.created",
        actor=actor,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        channel_type=conversation.channel_type,
        channel_account_id=conversation.channel_account_id,
        payload={
            "message_id": str(message.id),
            "channel_identity_id": str(message.channel_identity_id)
            if message.channel_identity_id
            else None,
            "direction": message.direction,
            "sender_type": message.sender_type,
            "sender_id": str(message.sender_id) if message.sender_id else None,
            "msg_type": message.msg_type,
            "content": message.content,
            "text_plain": message.text_plain,
            "is_note": message.is_note,
            "sent_via": message.sent_via,
            "client_msg_id": message.client_msg_id,
            "delivery_status": message.delivery_status,
            "requires_channel_send": requires_channel_send,
        },
    )


def _conversation_event(conversation: Conversation, actor: Actor, *, etype: str = "conversation.updated",
                        extra: dict[str, Any] | None = None) -> Event:
    return Event(
        workspace_id=conversation.workspace_id,
        type=etype,
        actor=actor,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        channel_type=conversation.channel_type,
        channel_account_id=conversation.channel_account_id,
        payload={
            "channel_identity_id": str(conversation.channel_identity_id)
            if conversation.channel_identity_id
            else None,
            "status": conversation.status,
            "handler": conversation.handler,
            "assignee_member_id": str(conversation.assignee_member_id)
            if conversation.assignee_member_id
            else None,
            "needs_reply": conversation.needs_reply,
            "agent_unread_count": conversation.agent_unread_count,
            "snippet": conversation.snippet,
            "bot_managed": conversation.bot_managed,
            "ai_state": conversation.ai_state,
            "translation": conversation.translation,
            **(extra or {}),
        },
    )


async def send_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    sender_type: str,  # member/ai_agent/automation/system
    sender_id: uuid.UUID | None,
    content: dict[str, Any] | MessageContent | None = None,
    quick_reply_id: uuid.UUID | None = None,
    is_note: bool = False,
    client_msg_id: str | None = None,
    sent_via: str | None = None,
    source_flow_id: uuid.UUID | None = None,
    redis: aioredis.Redis | None = None,
    now: datetime | None = None,
) -> SendResult:
    """Outbound send pipeline: idempotency → quick-reply expansion → content
    validation → window/capability check → message row + outbox event in the
    caller's transaction → conversation/session side effects.

    The caller commits, then publishes result.events via publish_realtime().
    """
    from . import event_bus  # local import keeps module import cheap

    now = now or datetime.now(UTC)

    # 1) client_msg_id idempotency (REST retries / reconnect double-sends)
    if client_msg_id:
        existing = (
            await session.execute(
                select(Message)
                .where(
                    Message.workspace_id == conversation.workspace_id,
                    Message.conversation_id == conversation.id,
                    Message.client_msg_id == client_msg_id,
                    Message.direction == "out",
                )
                .limit(1)
            )
        ).scalars().first()
        if existing is not None:
            return SendResult(message=existing, events=[], created=False)

    if conversation.status == "closed" and not is_note:
        raise ConversationClosedError("conversation is closed — reopen before replying")

    # 2) quick-reply expansion (explicit content wins if both are given)
    if content is None and quick_reply_id is not None:
        parsed = await expand_quick_reply(
            session,
            workspace_id=conversation.workspace_id,
            member_id=sender_id if sender_type == "member" else None,
            quick_reply_id=quick_reply_id,
        )
    elif content is not None:
        parsed = parse_content(content)
        if quick_reply_id is not None:  # attribution-only usage bump
            qr = await session.get(QuickReply, quick_reply_id)
            if qr is not None and qr.workspace_id == conversation.workspace_id:
                qr.usage_count = int(qr.usage_count or 0) + 1
    else:
        raise InvalidContentError("content or quick_reply_id required")
    if not parsed.blocks:
        raise InvalidContentError("message has no blocks")

    # 3) window + capability + file refs
    ensure_sendable(
        channel_type=conversation.channel_type,
        content=parsed,
        customer_window_expires_at=conversation.customer_window_expires_at,
        is_note=is_note,
        now=now,
    )
    await _validate_file_refs(session, conversation.workspace_id, parsed)

    # 4) message row (+ outbox event) — one transaction with the caller
    requires_send = not is_note
    message = Message(
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        channel_identity_id=conversation.channel_identity_id,
        direction="out",
        sender_type=sender_type,
        sender_id=sender_id,
        msg_type=msg_type_for(parsed),
        content=parsed.model_dump(mode="json"),
        text_plain=parsed.plain_text() or None,
        is_note=is_note,
        sent_via=sent_via,
        source_flow_id=source_flow_id,
        client_msg_id=client_msg_id,
        delivery_status="pending" if requires_send else "sent",
        created_at=now,
    )
    session.add(message)
    await session.flush()

    events: list[Event] = []
    actor_type = {"automation": "flow"}.get(sender_type, sender_type)
    if actor_type not in ("member", "ai_agent", "flow", "system", "api"):
        actor_type = "system"
    actor = Actor(type=actor_type, id=sender_id)

    # 5) conversation + session side effects (notes leave list state alone)
    if not is_note:
        apply_outbound_transition(conversation, is_note=False)
        conversation.snippet = make_snippet(parsed)
        conversation.last_message_at = now
        conversation.last_agent_message_at = now
        open_sess = await _open_session(session, conversation)
        if open_sess is not None:
            open_sess.message_count += 1
            open_sess.agent_message_count += 1
            if (
                open_sess.first_response_at is None
                and sender_type in ("member", "ai_agent")
                and open_sess.contact_message_count > 0
            ):
                open_sess.first_response_at = now
                events.append(
                    _conversation_event(
                        conversation, actor, etype="conversation.first_responded",
                        extra={"first_response_at": now.isoformat(),
                               "session_id": str(open_sess.id)},
                    )
                )
        if redis is not None:
            try:
                await quotas.incr_usage(redis, conversation.workspace_id, "monthly_replies")
            except Exception:  # noqa: BLE001 — metering must never block a send
                pass

    msg_event = _message_event(message, conversation, actor, requires_channel_send=requires_send)
    events.insert(0, msg_event)
    if not is_note:
        events.append(_conversation_event(conversation, actor))
    for e in events:
        await event_bus.emit(session, e)
    return SendResult(message=message, events=events, created=True)


async def add_system_event(
    session: AsyncSession,
    *,
    conversation: Conversation,
    event: str,
    meta: dict[str, Any] | None = None,
    actor: Actor | None = None,
    now: datetime | None = None,
) -> Message:
    """Grey timeline chip (assigned/closed/…): message row only, no channel
    send, no needs_reply change. Emits message.created for realtime."""
    from . import event_bus

    now = now or datetime.now(UTC)
    actor = actor or Actor(type="system")
    content = MessageContent.model_validate(
        {"blocks": [{"kind": "system_event", "event": event, "meta": meta or {}}]}
    )
    message = Message(
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        channel_identity_id=conversation.channel_identity_id,
        direction="out",
        sender_type="system",
        sender_id=actor.id,
        msg_type="system_event",
        content=content.model_dump(mode="json"),
        text_plain=None,
        is_note=False,
        delivery_status="sent",
        created_at=now,
    )
    session.add(message)
    await session.flush()
    await event_bus.emit(
        session, _message_event(message, conversation, actor, requires_channel_send=False)
    )
    return message


# --------------------------------------------------------------------------
# inbound side effects (called by channel ingest after it persists a message)
# --------------------------------------------------------------------------
async def register_inbound_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    message: Message,
    redis: aioredis.Redis | None = None,
    now: datetime | None = None,
) -> list[Event]:
    """needs_reply / unread / snippet / session counters for an inbound
    customer message. Bumps the assignee's realtime unread hash when a redis
    handle is given. Returns the conversation.updated event to publish."""
    now = now or message.created_at or datetime.now(UTC)
    apply_inbound_transition(conversation)
    content = parse_content(message.content or {"blocks": []})
    if content.blocks:
        conversation.snippet = make_snippet(content)
    conversation.last_message_at = now
    conversation.last_contact_message_at = now
    open_sess = await _open_session(session, conversation)
    if open_sess is not None:
        open_sess.message_count += 1
        open_sess.contact_message_count += 1
    if redis is not None and conversation.assignee_member_id is not None:
        from ..realtime import unread as rt_unread

        await rt_unread.incr_unread(
            redis,
            conversation.workspace_id,
            conversation.assignee_member_id,
            conversation.id,
            session=session,
        )
    actor = Actor(type="contact", id=None)
    ev = _conversation_event(conversation, actor)
    from . import event_bus

    await event_bus.emit(session, ev)
    return [ev]


# --------------------------------------------------------------------------
# read cursor (feeds unread counters — realtime.unread owns the Redis hash)
# --------------------------------------------------------------------------
async def advance_read_cursor(
    session: AsyncSession,
    *,
    conversation: Conversation,
    member_id: uuid.UUID,
    last_read_message_id: uuid.UUID | None = None,
    redis: aioredis.Redis | None = None,
) -> list[Event]:
    """Delegate the conversation_reads upsert + unread-hash zeroing +
    member-scoped unread.changed to realtime.unread; additionally the assignee
    reading zeroes the conversation's agent_unread_count (list badge).
    Returned events are realtime-only (NOT outbox — unread is ephemeral)."""
    from ..realtime import unread as rt_unread
    from .redis_client import get_redis

    await rt_unread.advance_read_cursor(
        session,
        redis if redis is not None else get_redis(),
        conversation.workspace_id,
        member_id,
        conversation.id,
        last_read_message_id,
    )
    events: list[Event] = []
    # ANY member opening the conversation clears the list badge — not just the
    # assignee. Most conversations are AI-assigned, so the old assignee-only
    # rule meant a human reading could NEVER zero the count: the server kept
    # pushing the stale number back on every event (the badge "flicker") and
    # it grew without bound.
    if conversation.agent_unread_count:
        conversation.agent_unread_count = 0
        events.append(_conversation_event(conversation, Actor(type="member", id=member_id)))
    return events


# --------------------------------------------------------------------------
# misc helpers shared by routers
# --------------------------------------------------------------------------
async def get_conversation_contact(
    session: AsyncSession, conversation: Conversation
) -> Contact | None:
    return await session.get(Contact, conversation.contact_id)
