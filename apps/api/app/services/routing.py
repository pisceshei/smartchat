"""Conversation lifecycle state machine + assignment engine (plan A.5).

Routing decision for a new inbound conversation:
    ① bot flow bound & enabled           → handler=bot        (託管)
    ② eligible AI member                 → handler=ai_agent   (receive on, under cap)
    ③ online + on-shift + under-cap human→ handler=member     (round_robin | least_busy,
                                            widget-pinned members restrict the pool)
    ④ nobody                             → handler=unassigned (待分配池)

Race protection (plan A.5): one routing job per conversation via a Redis
NX lock, SELECT ... FOR UPDATE on the conversation row, and Redis Lua
check-then-increment on the per-member concurrency cap.

Every transition writes a conversation_assignments audit row, emits
conversation.assigned/updated/resolved/reopened through the transactional
outbox, and returns the events for the caller to publish_realtime() after
commit.

Redis key conventions (shared with the realtime service):
    presence:member:{workspace_id}:{member_id}   SET EX 60 by ws heartbeat
    cap:{member_id}                              in-flight conversation count
    route:lock:{conversation_id}                 single-routing-job lock
    rr:{workspace_id}                            round-robin cursor
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from py_contracts.events import Actor, Event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.channels import Widget
from ..models.conversations import Conversation, ConversationAssignment, ConversationSession
from ..models.members import MemberShift, WorkspaceMember
from ..models.misc import AuditLog
from ..models.tenancy import Workspace
from . import event_bus, messaging, timers

log = logging.getLogger("smartchat.routing")

ROUTE_LOCK_TTL_S = 10
AUTO_CLOSE_TIMER_KIND = "conversation.auto_close"

# KEYS[1]=cap key, ARGV[1]=max (0/neg = unlimited).
# Returns new count on success, -1 when the member is at capacity.
CAP_INCR_LUA = """\
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
local max = tonumber(ARGV[1])
if max > 0 and cur >= max then return -1 end
return redis.call('INCR', KEYS[1])
"""

# Floor-0 decrement (close/transfer of an unassigned leg must never go negative).
CAP_DECR_LUA = """\
local cur = tonumber(redis.call('GET', KEYS[1]) or '0')
if cur <= 0 then redis.call('SET', KEYS[1], '0') return 0 end
return redis.call('DECR', KEYS[1])
"""


def cap_key(member_id: uuid.UUID | str) -> str:
    return f"cap:{member_id}"


def presence_key(workspace_id: uuid.UUID | str, member_id: uuid.UUID | str) -> str:
    """Same key the ws-gateway heartbeat refreshes (realtime.presence) —
    online status and routability are one source of truth (plan A.8)."""
    from ..realtime.presence import member_key

    return member_key(workspace_id, member_id)


def route_lock_key(conversation_id: uuid.UUID | str) -> str:
    return f"route:lock:{conversation_id}"


def rr_key(workspace_id: uuid.UUID | str) -> str:
    return f"rr:{workspace_id}"


# ==========================================================================
# pure decision layer (unit-tested without IO)
# ==========================================================================
@dataclass(frozen=True)
class AICandidate:
    member_id: uuid.UUID
    max_concurrent: int  # 0 = unlimited
    current_load: int
    receive_enabled: bool = True

    @property
    def under_cap(self) -> bool:
        return self.max_concurrent <= 0 or self.current_load < self.max_concurrent


@dataclass(frozen=True)
class HumanCandidate:
    member_id: uuid.UUID
    max_concurrent: int
    current_load: int
    online: bool
    on_shift: bool

    @property
    def under_cap(self) -> bool:
        return self.max_concurrent <= 0 or self.current_load < self.max_concurrent

    @property
    def eligible(self) -> bool:
        return self.online and self.on_shift and self.under_cap


@dataclass(frozen=True)
class RouteDecision:
    handler: str  # bot / ai_agent / member / unassigned
    member_id: uuid.UUID | None = None
    reason: str = "auto"


def is_on_shift(
    shifts: list[tuple[int, int, int]],
    now_utc: datetime,
    tz_name: str | None,
) -> bool:
    """shifts = [(weekday 0=Mon, start_min, end_min)] in the member's local
    timezone. No shifts defined = always on shift (plan A.2)."""
    if not shifts:
        return True
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:  # unknown tz string — fail open to UTC
        local = now_utc.astimezone(UTC)
    weekday = local.weekday()
    minute = local.hour * 60 + local.minute
    return any(wd == weekday and start <= minute < end for wd, start, end in shifts)


def pick_ai(candidates: list[AICandidate]) -> AICandidate | None:
    """First AI member with receiving enabled and headroom (stable order =
    creation order, mirroring the product's deterministic AI pick)."""
    for c in candidates:
        if c.receive_enabled and c.under_cap:
            return c
    return None


def pick_human(
    candidates: list[HumanCandidate],
    *,
    strategy: str = "round_robin",
    rr_counter: int = 0,
    pinned_member_ids: list[uuid.UUID] | None = None,
) -> HumanCandidate | None:
    """Pick among eligible humans. A non-empty pinned list (widget 指派成員)
    restricts the pool — if none of the pinned members are eligible the
    conversation falls to the unassigned pool rather than leaking to others."""
    pool = candidates
    if pinned_member_ids:
        pinned = set(pinned_member_ids)
        pool = [c for c in candidates if c.member_id in pinned]
    eligible = [c for c in pool if c.eligible]
    if not eligible:
        return None
    if strategy == "least_busy":
        return min(eligible, key=lambda c: (c.current_load, str(c.member_id)))
    return eligible[rr_counter % len(eligible)]  # round_robin (default)


def decide_route(
    *,
    bot_available: bool,
    ai_candidates: list[AICandidate],
    human_candidates: list[HumanCandidate],
    strategy: str = "round_robin",
    rr_counter: int = 0,
    pinned_member_ids: list[uuid.UUID] | None = None,
    prefer_bot: bool = True,
    prefer_ai_member: bool = True,
    auto_assign: bool = True,
) -> RouteDecision:
    """The plan-A.5 decision table, pure. Cap numbers are snapshots — the
    orchestrator re-checks atomically (Lua) and re-decides on a lost race."""
    if prefer_bot and bot_available:
        return RouteDecision(handler="bot")
    if prefer_ai_member:
        ai = pick_ai(ai_candidates)
        if ai is not None:
            return RouteDecision(handler="ai_agent", member_id=ai.member_id)
    if auto_assign:
        human = pick_human(
            human_candidates,
            strategy=strategy,
            rr_counter=rr_counter,
            pinned_member_ids=pinned_member_ids,
        )
        if human is not None:
            return RouteDecision(handler="member", member_id=human.member_id)
    return RouteDecision(handler="unassigned")


# ==========================================================================
# redis cap helpers
# ==========================================================================
async def cap_try_incr(redis: aioredis.Redis, member_id: uuid.UUID, max_concurrent: int) -> bool:
    """Atomic check-and-increment; False = member is at capacity."""
    res = int(await redis.eval(CAP_INCR_LUA, 1, cap_key(member_id), max_concurrent))
    return res != -1


async def cap_incr(redis: aioredis.Redis, member_id: uuid.UUID) -> int:
    """Unconditional increment (manual assign/transfer overrides the cap)."""
    return int(await redis.incr(cap_key(member_id)))


async def cap_decr(redis: aioredis.Redis, member_id: uuid.UUID) -> int:
    return int(await redis.eval(CAP_DECR_LUA, 1, cap_key(member_id)))


async def cap_load(redis: aioredis.Redis, member_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not member_ids:
        return {}
    vals = await redis.mget([cap_key(m) for m in member_ids])
    return {m: int(v or 0) for m, v in zip(member_ids, vals, strict=True)}


async def presence_check(
    redis: aioredis.Redis, workspace_id: uuid.UUID, member_ids: list[uuid.UUID]
) -> dict[uuid.UUID, bool]:
    if not member_ids:
        return {}
    pipe = redis.pipeline(transaction=False)
    for m in member_ids:
        pipe.exists(presence_key(workspace_id, m))
    flags = await pipe.execute()
    return {m: bool(f) for m, f in zip(member_ids, flags, strict=True)}


# ==========================================================================
# candidate gathering
# ==========================================================================
def _assignment_settings(workspace: Workspace) -> dict[str, Any]:
    return (workspace.settings or {}).get("assignment", {}) or {}


async def _bot_flow_available(session: AsyncSession, conversation: Conversation) -> bool:
    """P1: a bot handles the conversation when the widget has a bound
    automation (widgets.default_flow_id). Other channels gain flow triggers in
    P2 via the indexed flow_triggers table."""
    if conversation.channel_type != "widget":
        return False
    widget = (
        await session.execute(
            select(Widget).where(Widget.channel_account_id == conversation.channel_account_id)
        )
    ).scalars().first()
    return bool(widget is not None and widget.enabled and widget.default_flow_id)


async def _widget_routing(
    session: AsyncSession, conversation: Conversation
) -> tuple[list[uuid.UUID], str | None]:
    """widget.config.routing = {"member_ids": [...], "strategy": "..."} —
    指派成員 pins routing to those members."""
    if conversation.channel_type != "widget":
        return [], None
    widget = (
        await session.execute(
            select(Widget).where(Widget.channel_account_id == conversation.channel_account_id)
        )
    ).scalars().first()
    if widget is None:
        return [], None
    routing = (widget.config or {}).get("routing") or {}
    pinned: list[uuid.UUID] = []
    for raw in routing.get("member_ids") or []:
        try:
            pinned.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    return pinned, routing.get("strategy")


async def _gather_ai_candidates(
    session: AsyncSession, redis: aioredis.Redis, workspace_id: uuid.UUID
) -> list[AICandidate]:
    rows = (
        await session.execute(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.member_type == "ai_agent",
                WorkspaceMember.status == "active",
            )
            .order_by(WorkspaceMember.created_at)
        )
    ).scalars().all()
    loads = await cap_load(redis, [m.id for m in rows])
    return [
        AICandidate(
            member_id=m.id,
            max_concurrent=m.max_concurrent,
            current_load=loads.get(m.id, 0),
            receive_enabled=bool((m.ai_config or {}).get("receive_enabled", True)),
        )
        for m in rows
    ]


async def _gather_human_candidates(
    session: AsyncSession,
    redis: aioredis.Redis,
    workspace: Workspace,
    *,
    now: datetime,
) -> list[HumanCandidate]:
    rows = (
        await session.execute(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.member_type == "human",
                WorkspaceMember.status == "active",
            )
            .order_by(WorkspaceMember.created_at)
        )
    ).scalars().all()
    ids = [m.id for m in rows]
    loads = await cap_load(redis, ids)
    online = await presence_check(redis, workspace.id, ids)
    shift_rows = (
        await session.execute(
            select(MemberShift).where(
                MemberShift.workspace_id == workspace.id,
                MemberShift.member_id.in_(ids) if ids else False,
            )
        )
    ).scalars().all()
    shifts_by_member: dict[uuid.UUID, list[tuple[int, int, int]]] = {}
    tz_by_member: dict[uuid.UUID, str | None] = {}
    for s in shift_rows:
        shifts_by_member.setdefault(s.member_id, []).append((s.weekday, s.start_min, s.end_min))
        if s.timezone:
            tz_by_member[s.member_id] = s.timezone
    ws_tz = (workspace.settings or {}).get("timezone") or "UTC"
    return [
        HumanCandidate(
            member_id=m.id,
            max_concurrent=m.max_concurrent,
            current_load=loads.get(m.id, 0),
            online=online.get(m.id, False),
            on_shift=is_on_shift(
                shifts_by_member.get(m.id, []), now, tz_by_member.get(m.id) or ws_tz
            ),
        )
        for m in rows
    ]


# ==========================================================================
# transition primitives
# ==========================================================================
async def _lock_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation | None:
    return (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.workspace_id == workspace_id,
            )
            .with_for_update()
        )
    ).scalars().first()


def _audit_assignment(
    session: AsyncSession,
    conversation: Conversation,
    *,
    from_handler: str | None,
    from_member_id: uuid.UUID | None,
    to_handler: str,
    to_member_id: uuid.UUID | None,
    reason: str,
    actor: Actor,
) -> None:
    session.add(
        ConversationAssignment(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            from_handler=from_handler,
            from_member_id=from_member_id,
            to_handler=to_handler,
            to_member_id=to_member_id,
            reason=reason,
            actor_type=actor.type,
            actor_id=actor.id,
        )
    )


async def ensure_open_session(
    session: AsyncSession,
    conversation: Conversation,
    *,
    opened_by: str = "contact",
) -> ConversationSession:
    existing = (
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
    if existing is not None:
        return existing
    sess = ConversationSession(
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        opened_by=opened_by,
    )
    session.add(sess)
    conversation.session_count = int(conversation.session_count or 0) + 1
    await session.flush()
    return sess


def _assigned_event(conversation: Conversation, actor: Actor, reason: str) -> Event:
    return Event(
        workspace_id=conversation.workspace_id,
        type="conversation.assigned",
        actor=actor,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        channel_type=conversation.channel_type,
        channel_account_id=conversation.channel_account_id,
        payload={
            "handler": conversation.handler,
            "assignee_member_id": str(conversation.assignee_member_id)
            if conversation.assignee_member_id
            else None,
            "reason": reason,
            "bot_managed": conversation.bot_managed,
            "ai_state": conversation.ai_state,
        },
    )


def _apply_decision(conversation: Conversation, decision: RouteDecision) -> None:
    conversation.handler = decision.handler
    conversation.assignee_member_id = decision.member_id
    if decision.handler == "bot":
        conversation.bot_managed = True
    elif decision.handler == "ai_agent":
        conversation.bot_managed = True
        conversation.ai_state = "managed"
    elif decision.handler == "member":
        # a human taking over ends 託管 (unless re-enabled via the toggle)
        conversation.bot_managed = False
        conversation.ai_state = "off"


@dataclass
class RouteResult:
    decision: RouteDecision
    events: list[Event] = field(default_factory=list)


# ==========================================================================
# ① route new inbound
# ==========================================================================
async def route_new_inbound(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    now: datetime | None = None,
) -> RouteResult | None:
    """Route a fresh (or reopened-unassigned) conversation per the state
    machine. Single routing job per conversation (Redis NX lock); no-op if the
    conversation is already handled. Caller commits, then publishes events."""
    now = now or datetime.now(UTC)
    lock = await redis.set(route_lock_key(conversation_id), "1", nx=True, ex=ROUTE_LOCK_TTL_S)
    if not lock:
        return None  # another routing job owns this conversation
    try:
        conversation = await _lock_conversation(session, workspace_id, conversation_id)
        if conversation is None or conversation.status != "open":
            return None
        if conversation.handler != "unassigned" or conversation.assignee_member_id is not None:
            return None  # already routed (idempotent re-delivery of the event)

        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            return None
        cfg = _assignment_settings(workspace)
        await ensure_open_session(session, conversation, opened_by="contact")

        bot_available = (
            await _bot_flow_available(session, conversation) if cfg.get("prefer_bot", True) else False
        )
        ai_candidates = await _gather_ai_candidates(session, redis, workspace_id)
        human_candidates = await _gather_human_candidates(session, redis, workspace, now=now)
        pinned, widget_strategy = await _widget_routing(session, conversation)
        strategy = widget_strategy or cfg.get("mode", "round_robin")

        decision = RouteDecision(handler="unassigned")
        remaining_ai = list(ai_candidates)
        remaining_humans = list(human_candidates)
        for _ in range(len(ai_candidates) + len(human_candidates) + 1):
            rr_counter = 0
            candidate = decide_route(
                bot_available=bot_available,
                ai_candidates=remaining_ai,
                human_candidates=remaining_humans,
                strategy=strategy,
                rr_counter=rr_counter,
                pinned_member_ids=pinned,
                prefer_bot=cfg.get("prefer_bot", True),
                prefer_ai_member=cfg.get("prefer_ai_member", True),
                auto_assign=cfg.get("auto_assign", True),
            )
            if candidate.handler == "member" and strategy != "least_busy":
                # burn one round-robin tick only when we actually pick a human
                rr_counter = int(await redis.incr(rr_key(workspace_id))) - 1
                candidate = decide_route(
                    bot_available=bot_available,
                    ai_candidates=remaining_ai,
                    human_candidates=remaining_humans,
                    strategy=strategy,
                    rr_counter=rr_counter,
                    pinned_member_ids=pinned,
                    prefer_bot=cfg.get("prefer_bot", True),
                    prefer_ai_member=cfg.get("prefer_ai_member", True),
                    auto_assign=cfg.get("auto_assign", True),
                )
            if candidate.handler in ("bot", "unassigned"):
                decision = candidate
                break
            # atomic cap seat for the chosen member; on a lost race drop the
            # candidate and re-decide
            assert candidate.member_id is not None
            pool = remaining_ai if candidate.handler == "ai_agent" else remaining_humans
            cand_obj = next((c for c in pool if c.member_id == candidate.member_id), None)
            max_cc = cand_obj.max_concurrent if cand_obj else 0
            if await cap_try_incr(redis, candidate.member_id, max_cc):
                decision = candidate
                break
            if candidate.handler == "ai_agent":
                remaining_ai = [c for c in remaining_ai if c.member_id != candidate.member_id]
            else:
                remaining_humans = [c for c in remaining_humans if c.member_id != candidate.member_id]

        from_handler, from_member = conversation.handler, conversation.assignee_member_id
        _apply_decision(conversation, decision)
        events: list[Event] = []
        if decision.handler != "unassigned":
            _audit_assignment(
                session,
                conversation,
                from_handler=from_handler,
                from_member_id=from_member,
                to_handler=decision.handler,
                to_member_id=decision.member_id,
                reason="auto",
                actor=Actor(type="system"),
            )
            ev = _assigned_event(conversation, Actor(type="system"), "auto")
            await event_bus.emit(session, ev)
            events.append(ev)
            if decision.member_id is not None:
                await messaging.add_system_event(
                    session,
                    conversation=conversation,
                    event="assigned",
                    meta={"member_id": str(decision.member_id), "handler": decision.handler,
                          "reason": "auto"},
                )
        else:
            ev = messaging._conversation_event(conversation, Actor(type="system"))
            await event_bus.emit(session, ev)
            events.append(ev)
        return RouteResult(decision=decision, events=events)
    finally:
        try:
            await redis.delete(route_lock_key(conversation_id))
        except Exception:  # noqa: BLE001 — lock has a TTL anyway
            pass


# ==========================================================================
# ② transfer / claim
# ==========================================================================
async def transfer(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    to_member_id: uuid.UUID | None,
    actor: Actor,
    reason: str = "transfer",
) -> RouteResult:
    """Manual assign/transfer (to_member_id=None → 轉未分配). Manual moves
    override the cap (unconditional INCR) — the cap only gates auto-routing."""
    conversation = await _lock_conversation(session, workspace_id, conversation_id)
    if conversation is None:
        raise LookupError("conversation not found")
    from_handler, from_member = conversation.handler, conversation.assignee_member_id

    to_handler = "unassigned"
    target: WorkspaceMember | None = None
    if to_member_id is not None:
        target = await session.get(WorkspaceMember, to_member_id)
        if target is None or target.workspace_id != workspace_id or target.status != "active":
            raise LookupError("target member not found or inactive")
        to_handler = "ai_agent" if target.member_type == "ai_agent" else "member"
    if from_member == to_member_id and from_handler == to_handler:
        return RouteResult(decision=RouteDecision(handler=to_handler, member_id=to_member_id,
                                                  reason=reason), events=[])

    if from_member is not None and from_handler in ("member", "ai_agent"):
        await cap_decr(redis, from_member)
    if to_member_id is not None:
        await cap_incr(redis, to_member_id)

    _apply_decision(
        conversation, RouteDecision(handler=to_handler, member_id=to_member_id, reason=reason)
    )
    _audit_assignment(
        session,
        conversation,
        from_handler=from_handler,
        from_member_id=from_member,
        to_handler=to_handler,
        to_member_id=to_member_id,
        reason=reason,
        actor=actor,
    )
    session.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_type=actor.type,
            actor_id=actor.id,
            action="inbox.assign",
            target_type="conversation",
            target_id=str(conversation_id),
            detail={"from_member_id": str(from_member) if from_member else None,
                    "to_member_id": str(to_member_id) if to_member_id else None,
                    "reason": reason},
        )
    )
    events: list[Event] = []
    ev = _assigned_event(conversation, actor, reason)
    await event_bus.emit(session, ev)
    events.append(ev)
    await messaging.add_system_event(
        session,
        conversation=conversation,
        event="assigned" if to_member_id else "unassigned",
        meta={"member_id": str(to_member_id) if to_member_id else None,
              "handler": to_handler, "reason": reason},
        actor=actor,
    )
    return RouteResult(
        decision=RouteDecision(handler=to_handler, member_id=to_member_id, reason=reason),
        events=events,
    )


async def claim(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    member_id: uuid.UUID,
) -> RouteResult:
    """Agent pulls a conversation from the 待分配 pool (or takes over)."""
    return await transfer(
        session,
        redis,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        to_member_id=member_id,
        actor=Actor(type="member", id=member_id),
        reason="manual",
    )


# ==========================================================================
# ③ close (結束會話) / ④ reopen
# ==========================================================================
async def close_conversation(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor: Actor,
    closed_by_type: str | None = None,
    now: datetime | None = None,
) -> RouteResult | None:
    """結束會話: close the open conversation_session, free the assignee's cap
    seat, emit conversation.resolved. Idempotent (closing a closed
    conversation is a no-op)."""
    now = now or datetime.now(UTC)
    conversation = await _lock_conversation(session, workspace_id, conversation_id)
    if conversation is None:
        raise LookupError("conversation not found")
    if conversation.status == "closed":
        return None
    conversation.status = "closed"
    conversation.closed_at = now
    conversation.needs_reply = False

    open_sess = (
        await session.execute(
            select(ConversationSession)
            .where(
                ConversationSession.conversation_id == conversation.id,
                ConversationSession.ended_at.is_(None),
            )
            .order_by(ConversationSession.started_at.desc())
        )
    ).scalars().first()
    if open_sess is not None:
        open_sess.ended_at = now
        open_sess.closed_by_type = closed_by_type or actor.type
        open_sess.closed_by_id = actor.id
        open_sess.handler_at_close = conversation.handler

    if conversation.assignee_member_id is not None and conversation.handler in ("member", "ai_agent"):
        await cap_decr(redis, conversation.assignee_member_id)

    try:  # a pending auto-close timer for this conversation is now moot
        await timers.cancel(
            session,
            workspace_id=workspace_id,
            kind=AUTO_CLOSE_TIMER_KIND,
            ref_id=conversation.id,
            redis=redis,
        )
    except Exception:  # noqa: BLE001 — timer hygiene must not block closing
        log.warning("auto-close timer cancel failed for %s", conversation_id, exc_info=True)

    session.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_type=actor.type,
            actor_id=actor.id,
            action="inbox.close",
            target_type="conversation",
            target_id=str(conversation_id),
            detail={"handler_at_close": conversation.handler},
        )
    )
    ev = Event(
        workspace_id=workspace_id,
        type="conversation.resolved",
        actor=actor,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        channel_type=conversation.channel_type,
        channel_account_id=conversation.channel_account_id,
        payload={
            "closed_at": now.isoformat(),
            "session_id": str(open_sess.id) if open_sess else None,
            "handler_at_close": conversation.handler,
        },
    )
    await event_bus.emit(session, ev)
    await messaging.add_system_event(
        session, conversation=conversation, event="closed", meta={}, actor=actor, now=now
    )
    return RouteResult(
        decision=RouteDecision(handler=conversation.handler,
                               member_id=conversation.assignee_member_id, reason="close"),
        events=[ev],
    )


async def reopen_conversation(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor: Actor,
    opened_by: str = "contact",
    now: datetime | None = None,
) -> RouteResult | None:
    """Auto-reopen on new inbound (or manual reopen): new conversation_session
    + assignee stickiness (settings.assignment.sticky_assignee, default True).
    If the sticky assignee is gone/disabled the conversation drops to the
    unassigned pool — the caller should then run route_new_inbound()."""
    now = now or datetime.now(UTC)
    conversation = await _lock_conversation(session, workspace_id, conversation_id)
    if conversation is None:
        raise LookupError("conversation not found")
    if conversation.status == "open":
        return None
    workspace = await session.get(Workspace, workspace_id)
    cfg = _assignment_settings(workspace) if workspace else {}
    sticky = bool(cfg.get("sticky_assignee", True))

    conversation.status = "open"
    conversation.closed_at = None
    sess = ConversationSession(
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        opened_by=opened_by,
    )
    session.add(sess)
    conversation.session_count = int(conversation.session_count or 0) + 1

    prev_member = conversation.assignee_member_id
    kept = False
    if sticky and prev_member is not None:
        member = await session.get(WorkspaceMember, prev_member)
        if member is not None and member.status == "active":
            kept = True
            await cap_incr(redis, prev_member)  # re-take the seat
    if not kept:
        from_handler, from_member = conversation.handler, conversation.assignee_member_id
        conversation.handler = "unassigned"
        conversation.assignee_member_id = None
        if from_member is not None:
            _audit_assignment(
                session,
                conversation,
                from_handler=from_handler,
                from_member_id=from_member,
                to_handler="unassigned",
                to_member_id=None,
                reason="auto",
                actor=Actor(type="system"),
            )

    ev = Event(
        workspace_id=workspace_id,
        type="conversation.reopened",
        actor=actor,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        channel_type=conversation.channel_type,
        channel_account_id=conversation.channel_account_id,
        payload={
            "session_id": str(sess.id),
            "sticky_kept_assignee": kept,
            "handler": conversation.handler,
            "assignee_member_id": str(conversation.assignee_member_id)
            if conversation.assignee_member_id
            else None,
        },
    )
    await event_bus.emit(session, ev)
    await messaging.add_system_event(
        session, conversation=conversation, event="reopened",
        meta={"opened_by": opened_by}, actor=actor, now=now,
    )
    return RouteResult(
        decision=RouteDecision(
            handler=conversation.handler, member_id=conversation.assignee_member_id, reason="reopen"
        ),
        events=[ev],
    )


# ==========================================================================
# 託管 toggle (bot/AI takeover on the conversation)
# ==========================================================================
async def set_managed(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    managed: bool,
    actor: Actor,
) -> RouteResult:
    conversation = await _lock_conversation(session, workspace_id, conversation_id)
    if conversation is None:
        raise LookupError("conversation not found")
    conversation.bot_managed = managed
    if conversation.handler == "ai_agent":
        conversation.ai_state = "managed" if managed else "paused_human"
    elif not managed:
        conversation.ai_state = "off"
    ev = messaging._conversation_event(conversation, actor)
    await event_bus.emit(session, ev)
    return RouteResult(
        decision=RouteDecision(handler=conversation.handler,
                               member_id=conversation.assignee_member_id, reason="managed_toggle"),
        events=[ev],
    )
