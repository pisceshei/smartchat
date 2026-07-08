"""Flow engine runtime — the `flow-engine` compose service entrypoint
(`python -m apps.flow_engine.runtime`), plan 附錄 B.1.

An asyncio Redis-Streams consumer (group ``flow``) over events:conversation +
events:visitor (+ events:misc, where the timer service's flow.resume lands via
the fallback stream). Per event:

  message.created (inbound)      → feed a waiting session OR trigger-match
                                    visitor_message; (re)arm the agent timeout
  message.created (outbound)     → human reply cancels an active flow (takeover);
                                    any agent/ai/flow reply cancels the agent
                                    timeout and arms the visitor timeout
  widget.opened / page_view /
    lead.submitted / identified  → trigger-match the corresponding trigger type
  conversation.timeout.agent/    → re-check the condition still holds, then
    .visitor                        trigger-match the timeout trigger
  flow.resume (timer)            → resume a delayed / timed-out session

Delivery: at-least-once with XACK; an entry that fails 3 times is moved to the
dead-letter stream and acked. Graceful shutdown drains in-flight work.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from py_contracts.events import Event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.db import session_factory as make_session_factory
from apps.api.app.flows.graph_schema import parse_graph
from apps.api.app.models.contacts import Contact
from apps.api.app.models.conversations import Conversation
from apps.api.app.models.flows import Flow, FlowSession, FlowTrigger, FlowVersion
from apps.api.app.models.messaging import Message
from apps.api.app.models.tenancy import Workspace
from apps.api.app.services import event_bus, timers
from apps.api.app.services.messaging import dispatch_channel_sends, publish_realtime
from apps.api.app.services.redis_client import close_redis, get_redis

from . import interpreter, triggers
from .context import WAITING_STATUSES

log = logging.getLogger("smartchat.flow.runtime")

GROUP = "flow"
DEADLETTER_STREAM = "events:flow:deadletter"
MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT_S = 300
AGENT_TIMEOUT_KIND = "conversation.timeout.agent"
VISITOR_TIMEOUT_KIND = "conversation.timeout.visitor"

_TZ_CACHE: dict[uuid.UUID, str] = {}


def flow_streams() -> list[str]:
    return [
        event_bus.STREAMS["conversation"],
        event_bus.STREAMS["visitor"],
        event_bus.FALLBACK_STREAM,  # flow.resume timers land here
    ]


async def _ws_tz(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    if workspace_id in _TZ_CACHE:
        return _TZ_CACHE[workspace_id]
    ws = await session.get(Workspace, workspace_id)
    tz = ((ws.settings or {}).get("timezone") if ws else None) or "UTC"
    _TZ_CACHE[workspace_id] = tz
    return tz


# ==========================================================================
# conversation resolution
# ==========================================================================
async def _conversation_of_event(session: AsyncSession, event: Event) -> Conversation | None:
    if event.conversation_id is not None:
        return await session.get(Conversation, event.conversation_id)
    ident_raw = (event.payload or {}).get("channel_identity_id")
    if ident_raw:
        try:
            ident_id = uuid.UUID(str(ident_raw))
        except ValueError:
            return None
        return (
            await session.execute(
                select(Conversation).where(Conversation.channel_identity_id == ident_id)
            )
        ).scalars().first()
    return None


# ==========================================================================
# trigger firing
# ==========================================================================
async def _start_matched(
    session: AsyncSession,
    redis: aioredis.Redis,
    conversation: Conversation,
    contact: Contact | None,
    trigger_types: tuple[str, ...],
    *,
    text: str = "",
    url: str | None = None,
    kind: str | None = None,
    event_type: str,
    now: datetime,
    workspace_tz: str,
) -> list[Event]:
    """Match → select winner → start a session. Assumes the caller already
    confirmed there is no active session (single-active-session invariant)."""
    matches = await triggers.matching_triggers(
        session,
        workspace_id=conversation.workspace_id,
        channel_type=conversation.channel_type,
        trigger_types=trigger_types,
        text=text,
        url=url,
        kind=kind,
        now=now,
        workspace_tz=workspace_tz,
    )
    if not matches:
        return []
    winner = await triggers.select_winner(
        session, redis, matches,
        workspace_id=conversation.workspace_id,
        contact_id=conversation.contact_id,
        conversation_id=conversation.id,
    )
    if winner is None:
        return []
    flow = winner.flow
    if flow.published_version_id is None:
        return []
    version = await session.get(FlowVersion, flow.published_version_id)
    if version is None:
        return []
    try:
        graph = parse_graph(version.graph or {})
    except Exception:  # noqa: BLE001
        log.exception("bad published graph flow=%s", flow.id)
        return []
    await triggers.freq_cap_consume(
        redis, winner.trigger,
        workspace_id=conversation.workspace_id,
        contact_id=conversation.contact_id,
        conversation_id=conversation.id,
    )
    trigger_vars = {
        "message": text,
        "url": url,
        "kind": kind,
        "event_type": event_type,
        "channel_type": conversation.channel_type,
    }
    fs, events = await interpreter.start_session(
        session, redis,
        flow=flow,
        flow_version_id=version.id,
        graph=graph,
        conversation=conversation,
        contact=contact,
        trigger_vars=trigger_vars,
        mode="live",
        workspace_tz=workspace_tz,
        now=now,
    )
    triggers.log_outcome(
        session, workspace_id=conversation.workspace_id, flow_id=flow.id,
        trigger_id=winner.trigger.id, contact_id=conversation.contact_id,
        conversation_id=conversation.id, session_id=fs.id, outcome="triggered",
    )
    return events


# ==========================================================================
# timeout timer arming
# ==========================================================================
async def _enabled_timeout_seconds(
    session: AsyncSession, workspace_id: uuid.UUID, channel_type: str, trigger_type: str
) -> int | None:
    """Smallest timeout among enabled triggers of this type, or None if none
    exist (nothing to arm)."""
    rows = (
        await session.execute(
            select(FlowTrigger.config)
            .join(Flow, Flow.id == FlowTrigger.flow_id)
            .where(
                FlowTrigger.workspace_id == workspace_id,
                FlowTrigger.channel_type == channel_type,
                FlowTrigger.trigger_type == trigger_type,
                FlowTrigger.enabled.is_(True),
                Flow.enabled.is_(True),
            )
        )
    ).scalars().all()
    if not rows:
        return None
    seconds = [
        int((cfg or {}).get("timeout_s") or (cfg or {}).get("seconds") or DEFAULT_TIMEOUT_S)
        for cfg in rows
    ]
    return min(s for s in seconds if s > 0) if seconds else DEFAULT_TIMEOUT_S


async def _arm_timeout(
    session: AsyncSession,
    redis: aioredis.Redis,
    conversation: Conversation,
    trigger_type: str,
    kind: str,
    now: datetime,
) -> None:
    seconds = await _enabled_timeout_seconds(
        session, conversation.workspace_id, conversation.channel_type, trigger_type
    )
    if seconds is None:
        return
    # re-arm: cancel the previous timeout timer for this conversation first
    await timers.cancel(
        session, workspace_id=conversation.workspace_id, kind=kind,
        ref_id=conversation.id, redis=redis,
    )
    await timers.schedule(
        session,
        workspace_id=conversation.workspace_id,
        kind=kind,
        fire_at=now + timedelta(seconds=seconds),
        ref_id=conversation.id,
        payload={"conversation_id": str(conversation.id)},
        redis=redis,
    )


async def _cancel_timeout(
    session: AsyncSession, redis: aioredis.Redis, conversation: Conversation, kind: str
) -> None:
    await timers.cancel(
        session, workspace_id=conversation.workspace_id, kind=kind,
        ref_id=conversation.id, redis=redis,
    )


# ==========================================================================
# per-event handlers (each owns its transaction)
# ==========================================================================
async def _handle_message(
    session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis, event: Event
) -> list[Event]:
    payload = event.payload or {}
    direction = payload.get("direction")
    if event.conversation_id is None:
        return []
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            conv = await session.get(Conversation, event.conversation_id)
            if conv is None:
                return []
            ws_tz = await _ws_tz(session, conv.workspace_id)
            contact = await session.get(Contact, conv.contact_id) if conv.contact_id else None

            if direction == "in":
                active = await interpreter.active_session_for_conversation(
                    session, conv.workspace_id, conv.id
                )
                events: list[Event] = []
                if active is not None and active.status in WAITING_STATUSES:
                    msg = None
                    if payload.get("message_id"):
                        with contextlib.suppress(ValueError):
                            msg = await session.get(Message, uuid.UUID(str(payload["message_id"])))
                    events = await interpreter.feed_message(
                        session, redis, fs=active, message=msg, workspace_tz=ws_tz, now=now
                    )
                elif active is None:
                    events = await _start_matched(
                        session, redis, conv, contact, ("visitor_message",),
                        text=payload.get("text_plain", "") or "",
                        event_type=event.type, now=now, workspace_tz=ws_tz,
                    )
                # customer replied → cancel visitor timeout, (re)arm agent timeout
                await _cancel_timeout(session, redis, conv, VISITOR_TIMEOUT_KIND)
                await _arm_timeout(session, redis, conv, "agent_timeout", AGENT_TIMEOUT_KIND, now)
                return events

            # outbound
            sender_type = payload.get("sender_type")
            events = []
            if sender_type == "member":
                active = await interpreter.active_session_for_conversation(
                    session, conv.workspace_id, conv.id
                )
                if active is not None:
                    await interpreter.cancel_session(
                        session, redis, fs=active, reason="human_takeover"
                    )
            if sender_type in ("member", "ai_agent", "automation"):
                await _cancel_timeout(session, redis, conv, AGENT_TIMEOUT_KIND)
                await _arm_timeout(session, redis, conv, "visitor_timeout", VISITOR_TIMEOUT_KIND, now)
            return events


async def _handle_visitor(
    session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis, event: Event
) -> list[Event]:
    trigger_types = triggers.EVENT_TO_TRIGGER_TYPES.get(event.type)
    if not trigger_types:
        return []
    payload = event.payload or {}
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            conv = await _conversation_of_event(session, event)
            if conv is None:
                return []
            active = await interpreter.active_session_for_conversation(
                session, conv.workspace_id, conv.id
            )
            if active is not None:
                return []  # single active session — suppress
            ws_tz = await _ws_tz(session, conv.workspace_id)
            contact = await session.get(Contact, conv.contact_id) if conv.contact_id else None
            return await _start_matched(
                session, redis, conv, contact, trigger_types,
                url=payload.get("url"), kind=payload.get("kind"),
                event_type=event.type, now=now, workspace_tz=ws_tz,
            )


async def _handle_timeout(
    session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis, event: Event
) -> list[Event]:
    trigger_types = triggers.EVENT_TO_TRIGGER_TYPES.get(event.type)
    if not trigger_types:
        return []
    payload = event.payload or {}
    conv_id = payload.get("conversation_id") or event.conversation_id
    if conv_id is None:
        return []
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            try:
                conv = await session.get(Conversation, uuid.UUID(str(conv_id)))
            except ValueError:
                return []
            if conv is None or conv.status != "open":
                return []
            # re-check the condition still holds (plan: re-verify on fire)
            if event.type == AGENT_TIMEOUT_KIND and not conv.needs_reply:
                return []  # an agent already replied
            if event.type == VISITOR_TIMEOUT_KIND:
                la = conv.last_agent_message_at
                lc = conv.last_contact_message_at
                if la is None or (lc is not None and lc >= la):
                    return []  # visitor already replied
            active = await interpreter.active_session_for_conversation(
                session, conv.workspace_id, conv.id
            )
            if active is not None:
                return []
            ws_tz = await _ws_tz(session, conv.workspace_id)
            contact = await session.get(Contact, conv.contact_id) if conv.contact_id else None
            return await _start_matched(
                session, redis, conv, contact, trigger_types,
                event_type=event.type, now=now, workspace_tz=ws_tz,
            )


async def _handle_resume(
    session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis, event: Event
) -> list[Event]:
    payload = event.payload or {}
    sid_raw = payload.get("session_id")
    if not sid_raw:
        return []
    try:
        session_id = uuid.UUID(str(sid_raw))
    except ValueError:
        return []
    expected_seq = int(payload.get("expected_seq") or 0)
    async with session_factory() as session:
        async with session.begin():
            fs = await session.get(FlowSession, session_id)
            if fs is None:
                return []
            ws_tz = await _ws_tz(session, fs.workspace_id)
            return await interpreter.resume_from_timer(
                session, redis, session_id=session_id, expected_seq=expected_seq,
                workspace_tz=ws_tz,
            )


# ==========================================================================
# top-level dispatch
# ==========================================================================
async def handle_event(
    session_factory: async_sessionmaker[AsyncSession], redis: aioredis.Redis, event: Event
) -> list[Event]:
    """Route one bus event to its handler and return outbox events to publish
    realtime. Directly callable (tests publish a synthetic event)."""
    etype = event.type
    if etype == interpreter.RESUME_KIND:
        return await _handle_resume(session_factory, redis, event)
    if etype == "message.created":
        return await _handle_message(session_factory, redis, event)
    if etype in ("widget.opened", "visitor.page_view", "lead.submitted", "visitor.identified"):
        return await _handle_visitor(session_factory, redis, event)
    if etype in (AGENT_TIMEOUT_KIND, VISITOR_TIMEOUT_KIND):
        return await _handle_timeout(session_factory, redis, event)
    return []


# ==========================================================================
# consumer loop
# ==========================================================================
def _consumer_name() -> str:
    return f"{os.environ.get('HOSTNAME', 'flow')}-{os.getpid()}"


async def _read(
    redis: aioredis.Redis, streams: list[str], consumer: str, last_id: str, *, block_ms: int
) -> list[tuple[str, str, Event]]:
    resp = await redis.xreadgroup(
        GROUP, consumer, {s: last_id for s in streams}, count=64, block=block_ms
    )
    out: list[tuple[str, str, Event]] = []
    for stream, entries in resp or []:
        for entry_id, fields in entries:
            if not fields:
                await redis.xack(stream, GROUP, entry_id)
                continue
            try:
                out.append((stream, entry_id, event_bus.decode_event(fields)))
            except Exception:  # noqa: BLE001
                log.exception("poison entry %s on %s", entry_id, stream)
                await redis.xack(stream, GROUP, entry_id)
    return out


async def _dispatch_entry(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    stream: str,
    entry_id: str,
    event: Event,
    attempts: dict[str, int],
) -> None:
    key = f"{stream}:{entry_id}"
    attempts[key] = attempts.get(key, 0) + 1
    try:
        events = await handle_event(session_factory, redis, event)
        if events:
            await publish_realtime(events)
            await dispatch_channel_sends(events)
        await redis.xack(stream, GROUP, entry_id)
        attempts.pop(key, None)
    except Exception:  # noqa: BLE001
        n = attempts[key]
        log.exception("flow event failed (attempt %d) %s on %s", n, entry_id, stream)
        if n >= MAX_ATTEMPTS:
            with contextlib.suppress(Exception):
                await redis.xadd(
                    DEADLETTER_STREAM,
                    {"stream": stream, "entry_id": entry_id, "type": event.type,
                     "data": event.model_dump_json()},
                    maxlen=10_000, approximate=True,
                )
            await redis.xack(stream, GROUP, entry_id)  # drop after dead-lettering
            attempts.pop(key, None)
        # else: leave unacked → retried on the next pending drain


async def run(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    stop: asyncio.Event | None = None,
    block_ms: int = 2000,
) -> None:
    streams = flow_streams()
    for s in streams:
        await event_bus.ensure_group(redis, s, GROUP)
    consumer = _consumer_name()
    attempts: dict[str, int] = {}
    log.info("flow-engine consuming %s as %s", streams, consumer)
    while stop is None or not stop.is_set():
        try:
            # retry this consumer's un-acked (failed) pending entries first
            for stream, entry_id, event in await _read(redis, streams, consumer, "0", block_ms=1):
                await _dispatch_entry(session_factory, redis, stream, entry_id, event, attempts)
            # then new entries
            for stream, entry_id, event in await _read(redis, streams, consumer, ">", block_ms=block_ms):
                await _dispatch_entry(session_factory, redis, stream, entry_id, event, attempts)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("flow consume pass failed")
            await asyncio.sleep(0.5)


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    redis = get_redis()
    sf = make_session_factory()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run(sf, redis, stop=stop)
    finally:
        await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
