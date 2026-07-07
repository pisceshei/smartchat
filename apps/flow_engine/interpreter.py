"""Flow session state machine + node execution (plan 附錄 B.1).

State machine:
    running ⇄ delayed / waiting_reply / waiting_button
            → completed / ended / failed / expired / cancelled

Execution: from ``current_node_id`` run a node → append a flow_session_steps
row → follow its output port to the next node → repeat, until a node suspends
(delay/ask/quick_buttons), reaches a terminal (close_conversation), or hits a
dead-end port (session ends cleanly). Budgets: ≤20 nodes per resume (yields to a
fresh immediate resume so one session can't starve the loop) and a lifetime cap
of MAX_STEPS (100) nodes (cycles are allowed in the graph, capped here).

Idempotency: every suspend bumps ``seq`` and schedules a flow.resume timer
carrying (session_id, expected_seq); a resume whose expected_seq != the live seq
is a stale double-fire and is ignored. Feeding a reply/button also bumps seq and
cancels the pending timer, so a race between a late timeout and a real reply
resolves to exactly one continuation.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from py_contracts.content import MessageContent
from py_contracts.events import Actor, Event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.flows.graph_schema import (
    CONDITION_TYPES,
    MAX_STEPS,
    Graph,
    parse_graph,
)
from apps.api.app.models.contacts import Contact
from apps.api.app.models.conversations import Conversation
from apps.api.app.models.flows import Flow, FlowSession, FlowSessionStep, FlowVersion
from apps.api.app.models.messaging import Message
from apps.api.app.services import event_bus, messaging, timers

from . import conditions, stats
from .actions import ACTION_DISPATCH
from .context import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DELAYED,
    STATUS_ENDED,
    STATUS_EXPIRED,
    WAITING_STATUSES,
    ExecutionContext,
    GraphNav,
    NodeResult,
)

log = logging.getLogger("smartchat.flow.interpreter")

RESUME_KIND = "flow.resume"
MAX_RESUME_NODES = 20
MAX_LIFETIME_STEPS = MAX_STEPS
DEFAULT_SESSION_TTL_S = 24 * 3600


# ==========================================================================
# graph loading
# ==========================================================================
async def load_graph_for_session(session: AsyncSession, fs: FlowSession) -> Graph | None:
    version = await session.get(FlowVersion, fs.flow_version_id)
    if version is None:
        return None
    try:
        return parse_graph(version.graph or {})
    except Exception:  # noqa: BLE001
        log.exception("failed to parse graph for flow_version %s", fs.flow_version_id)
        return None


# ==========================================================================
# step recording + finalization
# ==========================================================================
async def _record_step(
    ctx: ExecutionContext, node_id: str, node_type: str, result: NodeResult, latency_ms: int
) -> None:
    if ctx.test_mode:
        # test sessions still record steps (drill-down of a test run) but never
        # touch aggregate stats.
        pass
    ctx.session.add(
        FlowSessionStep(
            workspace_id=ctx.workspace_id,
            session_id=ctx.flow_session.id,
            flow_id=ctx.flow_session.flow_id,
            seq=ctx.flow_session.step_count,
            node_id=node_id,
            node_type=node_type,
            status=result.step_status,
            error=result.error,
            latency_ms=latency_ms,
        )
    )
    ctx.flow_session.step_count += 1


async def _finalize(ctx: ExecutionContext, status: str, reason: str, *, workspace_tz: str) -> None:
    fs = ctx.flow_session
    fs.status = status
    fs.end_reason = reason
    fs.ended_at = ctx.now
    fs.waiting = None
    fs.wakeup_at = None
    if not ctx.test_mode and status == STATUS_COMPLETED:
        await stats.record_completed(ctx.session, ctx.redis, fs, workspace_tz, now=ctx.now)


async def _suspend(
    ctx: ExecutionContext, node_id: str, result: NodeResult, *, workspace_tz: str
) -> None:
    """Persist a waiting descriptor + schedule the resume/timeout timer."""
    fs = ctx.flow_session
    fs.current_node_id = node_id
    fs.status = result.status or STATUS_DELAYED
    fs.waiting = result.waiting
    fs.seq += 1
    wakeup = result.wakeup_at
    # an un-timed reply/button wait still gets a hard 24h expiry timer so a
    # visitor who never answers can't pin the conversation's single active
    # session forever (blocking every future trigger).
    if wakeup is None and fs.status in WAITING_STATUSES:
        wakeup = fs.expires_at
    fs.wakeup_at = wakeup
    if wakeup is not None and ctx.redis is not None:
        await timers.schedule(
            ctx.session,
            workspace_id=ctx.workspace_id,
            kind=RESUME_KIND,
            fire_at=wakeup,
            ref_id=fs.id,
            payload={"session_id": str(fs.id), "expected_seq": fs.seq},
            redis=ctx.redis,
        )


# ==========================================================================
# core run loop
# ==========================================================================
async def run(
    ctx: ExecutionContext, nav: GraphNav, *, workspace_tz: str, start_node_id: str | None
) -> None:
    """Execute nodes starting at ``start_node_id`` until suspend/terminal/dead
    end / budget. Assumes ctx.flow_session is 'running'."""
    fs = ctx.flow_session
    # lifetime expiry guard
    if fs.expires_at is not None and _aware(fs.expires_at) <= ctx.now:
        await _finalize(ctx, STATUS_EXPIRED, "expired", workspace_tz=workspace_tz)
        return

    node_id = start_node_id
    executed_this_resume = 0

    while node_id is not None:
        if fs.step_count >= MAX_LIFETIME_STEPS:
            await _finalize(ctx, STATUS_ENDED, "max_steps", workspace_tz=workspace_tz)
            return
        if executed_this_resume >= MAX_RESUME_NODES:
            # yield: park at this node and continue in a fresh immediate resume
            fs.current_node_id = node_id
            fs.status = STATUS_DELAYED
            fs.waiting = {"type": "continue", "node_id": node_id}
            fs.seq += 1
            if ctx.redis is not None:
                await timers.schedule(
                    ctx.session,
                    workspace_id=ctx.workspace_id,
                    kind=RESUME_KIND,
                    fire_at=ctx.now,
                    ref_id=fs.id,
                    payload={"session_id": str(fs.id), "expected_seq": fs.seq},
                    redis=ctx.redis,
                )
            return

        node = nav.get(node_id)
        if node is None:
            await _finalize(ctx, STATUS_ENDED, "dead_end", workspace_tz=workspace_tz)
            return

        fs.current_node_id = node_id
        started = time.monotonic()
        try:
            result = await _execute_node(ctx, node, workspace_tz=workspace_tz)
        except Exception as e:  # noqa: BLE001 — node crash → on_error policy
            log.exception("node %s (%s) crashed", node.id, node.type)
            result = _on_error(node, e)
        latency_ms = int((time.monotonic() - started) * 1000)
        await _record_step(ctx, node.id, node.type, result, latency_ms)
        executed_this_resume += 1

        if result.kind == "wait":
            await _suspend(ctx, node.id, result, workspace_tz=workspace_tz)
            return
        if result.kind == "end":
            reason = result.end_reason or "ended"
            status = STATUS_COMPLETED if reason == "completed" else STATUS_ENDED
            await _finalize(ctx, status, reason, workspace_tz=workspace_tz)
            return
        # kind == next
        nxt = nav.next_id(node.id, result.port or "out")
        if nxt is None:
            # reached a leaf (terminal node or a dead-end port) — the flow ran
            # to its natural end = completed (plan 完成度). Abnormal ends
            # (max_steps / broken graph) stay 'ended'; drop-offs stay 'waiting'.
            await _finalize(ctx, STATUS_COMPLETED, "completed", workspace_tz=workspace_tz)
            return
        node_id = nxt


def _on_error(node: Any, exc: Exception) -> NodeResult:
    """node failure semantics: on_error = skip (default) | abort."""
    policy = (node.data or {}).get("on_error", "skip")
    if policy == "abort":
        return NodeResult.end("failed", step_status="error", error=str(exc))
    # skip → continue via the default port (external_request defaults to failed)
    port = "failed" if node.type == "external_request" else "out"
    return NodeResult.next(port, step_status="error")


async def _execute_node(ctx: ExecutionContext, node: Any, *, workspace_tz: str) -> NodeResult:
    if node.type in CONDITION_TYPES:
        port = conditions.evaluate(node, ctx, workspace_tz=workspace_tz)
        return NodeResult.next(port)
    fn = ACTION_DISPATCH.get(node.type)
    if fn is None:
        return NodeResult.next("out", step_status="skipped")
    return await fn(ctx, node)


# ==========================================================================
# session start
# ==========================================================================
async def start_session(
    session: AsyncSession,
    redis: aioredis.Redis | None,
    *,
    flow: Flow,
    flow_version_id: uuid.UUID,
    graph: Graph,
    conversation: Conversation | None,
    contact: Contact | None,
    trigger_vars: dict[str, Any] | None = None,
    mode: str = "live",
    workspace_tz: str = "UTC",
    now: datetime | None = None,
) -> tuple[FlowSession, list[Event]]:
    """Create a FlowSession and run it to its first suspend/terminal. Returns
    (session_row, events-to-publish). The caller commits."""
    now = now or datetime.now(UTC)
    nav = GraphNav.build(graph)
    start_id = nav.start_node_id()

    fs = FlowSession(
        workspace_id=flow.workspace_id,
        conversation_id=conversation.id if conversation else None,
        contact_id=contact.id if contact else None,
        flow_id=flow.id,
        flow_version_id=flow_version_id,
        mode=mode,
        status="running",
        current_node_id=start_id,
        variables={"vars": {}, "trigger": trigger_vars or {}, "ext": {}},
        expires_at=now + timedelta(seconds=DEFAULT_SESSION_TTL_S),
    )
    session.add(fs)
    await session.flush()

    events: list[Event] = []
    if conversation is not None and mode != "test":
        await _open_for_flow(session, conversation, flow, now, events)

    ctx = ExecutionContext(
        session=session, redis=redis, flow_session=fs, conversation=conversation,
        contact=contact, now=now, test_mode=(mode == "test"), events=events,
    )
    if mode != "test":
        await stats.record_triggered(session, redis, fs, workspace_tz, now=now)

    if start_id is None:
        await _finalize(ctx, STATUS_ENDED, "empty_graph", workspace_tz=workspace_tz)
        return fs, ctx.events
    await run(ctx, nav, workspace_tz=workspace_tz, start_node_id=start_id)
    return fs, ctx.events


async def _open_for_flow(
    session: AsyncSession, conversation: Conversation, flow: Flow, now: datetime, events: list[Event]
) -> None:
    """A proactive flow (widget opened / page view / lead) may start on a
    conversation that is still 'closed' (widgets open closed until the first
    message). Open it and let the bot own it so the send actions surface it."""
    from apps.api.app.services import routing

    if conversation.status == "closed":
        conversation.status = "open"
        conversation.closed_at = None
        await routing.ensure_open_session(session, conversation, opened_by="flow")
        if conversation.handler == "unassigned":
            conversation.handler = "bot"
            conversation.bot_managed = True
        ev = messaging._conversation_event(
            conversation, Actor(type="flow", id=flow.id), etype="conversation.updated"
        )
        await event_bus.emit(session, ev)
        events.append(ev)


# ==========================================================================
# resume (timer) + feed (event)
# ==========================================================================
def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _load_context(
    session: AsyncSession, fs: FlowSession
) -> tuple[ExecutionContext, Conversation | None, Contact | None]:
    conversation = (
        await session.get(Conversation, fs.conversation_id) if fs.conversation_id else None
    )
    contact = await session.get(Contact, fs.contact_id) if fs.contact_id else None
    ctx = ExecutionContext(
        session=session, redis=None, flow_session=fs, conversation=conversation,
        contact=contact, now=datetime.now(UTC), test_mode=(fs.mode == "test"),
    )
    return ctx, conversation, contact


async def resume_from_timer(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    session_id: uuid.UUID,
    expected_seq: int,
    workspace_tz: str,
    now: datetime | None = None,
) -> list[Event]:
    """flow.resume timer fired. Continue a delayed session, or follow the
    'timeout' port of a waiting ask/quick_buttons. Stale (seq mismatch) or
    already-terminal fires are ignored (idempotency)."""
    fs = await session.get(FlowSession, session_id)
    if fs is None or fs.status not in ACTIVE_STATUSES:
        return []
    if fs.seq != expected_seq:
        return []  # stale double-fire
    graph = await load_graph_for_session(session, fs)
    if graph is None:
        return []
    nav = GraphNav.build(graph)
    ctx, _, _ = await _load_context(session, fs)
    ctx.redis = redis
    ctx.now = now or ctx.now

    waiting = fs.waiting or {}
    waiting_type = waiting.get("type")
    from_node = fs.current_node_id
    if fs.status == STATUS_DELAYED:
        port = "out"
    elif fs.status in WAITING_STATUSES:
        if not waiting.get("timeout_s"):
            # the timer that fired is the hard 24h expiry of an un-timed wait
            fs.seq += 1
            await _finalize(ctx, STATUS_EXPIRED, "expired", workspace_tz=workspace_tz)
            return ctx.events
        port = "timeout"
    else:
        return []
    # entering the run loop as 'running'
    fs.status = "running"
    fs.waiting = None
    fs.seq += 1
    if waiting_type == "continue":
        # budget-yield continuation resumes AT the parked node, not past it
        await run(ctx, nav, workspace_tz=workspace_tz, start_node_id=from_node)
        return ctx.events
    nxt = nav.next_id(from_node, port) if from_node else None
    if nxt is None:
        await _finalize(ctx, STATUS_COMPLETED, "completed", workspace_tz=workspace_tz)
        return ctx.events
    await run(ctx, nav, workspace_tz=workspace_tz, start_node_id=nxt)
    return ctx.events


async def feed_message(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    fs: FlowSession,
    message: Message | None,
    workspace_tz: str,
    now: datetime | None = None,
) -> list[Event]:
    """Feed an inbound visitor message to a waiting session: capture the reply
    (ask) or resolve a button/typed-reply port (quick_buttons)."""
    if fs.status not in WAITING_STATUSES:
        return []
    graph = await load_graph_for_session(session, fs)
    if graph is None:
        return []
    nav = GraphNav.build(graph)
    ctx, conversation, contact = await _load_context(session, fs)
    ctx.redis = redis
    ctx.now = now or ctx.now

    # cancel the pending timeout timer + invalidate its seq
    await timers.cancel(session, workspace_id=fs.workspace_id, kind=RESUME_KIND, ref_id=fs.id, redis=redis)
    fs.seq += 1
    waiting = fs.waiting or {}
    from_node = fs.current_node_id
    fs.status = "running"
    fs.waiting = None

    text, button_payload = _extract_message(message)

    if fs.mode != "test":
        await stats.record_engaged(session, fs)

    if waiting.get("type") == "ask":
        variable = waiting.get("variable") or "answer"
        ctx.set_var(variable, text)
        valid = _validate_answer(text, waiting.get("validation"))
        if valid and waiting.get("save_to_contact") and contact is not None:
            field = waiting["save_to_contact"]
            if field.startswith("custom."):
                custom = dict(contact.custom or {})
                custom[field.split(".", 1)[1]] = text
                contact.custom = custom
            elif hasattr(contact, field):
                setattr(contact, field, text)
        port = "answered" if valid else "invalid"
    elif waiting.get("type") == "buttons":
        if button_payload is not None and button_payload in set(waiting.get("button_ids") or []):
            port = f"button:{button_payload}"
        else:
            port = "typed_reply"
    else:
        return ctx.events

    nxt = nav.next_id(from_node, port) if from_node else None
    if nxt is None:
        await _finalize(ctx, STATUS_COMPLETED, "completed", workspace_tz=workspace_tz)
        return ctx.events
    await run(ctx, nav, workspace_tz=workspace_tz, start_node_id=nxt)
    return ctx.events


def _extract_message(message: Message | None) -> tuple[str, str | None]:
    """Return (plain_text, button_payload|None) from a stored inbound message."""
    if message is None:
        return "", None
    button_payload: str | None = None
    text = message.text_plain or ""
    try:
        content = MessageContent.model_validate(message.content or {"blocks": []})
        for b in content.blocks:
            if b.kind == "button_reply":
                button_payload = b.payload
            elif b.kind == "text" and not text:
                text = b.text
    except Exception:  # noqa: BLE001
        pass
    return text, button_payload


def _validate_answer(text: str, validation: str | None) -> bool:
    if not validation:
        return True
    text = (text or "").strip()
    if not text:
        return False
    if validation == "email":
        import re

        return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", text))
    if validation == "phone":
        import re

        return bool(re.match(r"^\+?[0-9][0-9\s\-()]{5,}$", text))
    if validation == "number":
        try:
            float(text)
            return True
        except ValueError:
            return False
    return True


async def cancel_session(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    fs: FlowSession,
    reason: str = "cancelled",
) -> None:
    """Terminate an active session (e.g. human takeover). Cancels its timer."""
    if fs.status not in ACTIVE_STATUSES:
        return
    await timers.cancel(session, workspace_id=fs.workspace_id, kind=RESUME_KIND, ref_id=fs.id, redis=redis)
    fs.status = STATUS_CANCELLED
    fs.end_reason = reason
    fs.ended_at = datetime.now(UTC)
    fs.waiting = None
    fs.wakeup_at = None
    fs.seq += 1


# ==========================================================================
# active-session lookup
# ==========================================================================
async def active_session_for_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> FlowSession | None:
    return (
        await session.execute(
            select(FlowSession)
            .where(
                FlowSession.workspace_id == workspace_id,
                FlowSession.conversation_id == conversation_id,
                FlowSession.mode == "live",
                FlowSession.status.in_(list(ACTIVE_STATUSES)),
            )
            .order_by(FlowSession.created_at.desc())
            .with_for_update()
            .limit(1)
        )
    ).scalars().first()
