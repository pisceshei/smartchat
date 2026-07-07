"""Trigger routing algorithm (plan 附錄 B.1).

Routing for an inbound event (the runtime orchestrates; this module is the
matching + selection core):

  1. if the conversation already has an active waiting session → the runtime
     feeds the event to it and never trigger-matches (handled in runtime.py).
  2. else query the denormalised ``flow_triggers`` table by
     (workspace, channel_type, trigger_type, enabled) — a plain indexed SQL
     lookup, never a JSON scan — and evaluate each row's match config
     (keyword groups OR-within-group, contains/exact NFKC-casefold, dict refs;
     page-URL rules; new/returning visitor).
  3. frequency-cap check (Redis fixed-window front cache + durable
     flow_trigger_log): peek so a losing flow never consumes a visitor's cap.
  4. ONE active flow session per conversation (runtime enforces; suppressed hits
     are logged).
  5. multiple matches → lowest ``priority`` wins, tie broken by newest
     ``updated_at``; losers are logged for explainability.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.models.flows import (
    Flow,
    FlowTrigger,
    FlowTriggerLog,
    KeywordDictItem,
)

from .conditions import in_schedule, norm

log = logging.getLogger("smartchat.flow.triggers")

# event type → trigger types it can fire
EVENT_TO_TRIGGER_TYPES: dict[str, tuple[str, ...]] = {
    "message.created": ("visitor_message",),
    "widget.opened": ("widget_opened",),
    "visitor.page_view": ("page_visited",),
    "lead.submitted": ("lead_submitted",),
    "visitor.identified": ("new_visitor", "returning_visitor"),
    "conversation.timeout.agent": ("agent_timeout",),
    "conversation.timeout.visitor": ("visitor_timeout",),
}


# ==========================================================================
# pure matchers
# ==========================================================================
def match_message(text: str, config: dict[str, Any], dict_keywords: list[str]) -> bool:
    """訪客發消息 matcher. match_type=any → any message; else the message must
    match a keyword (keyword_groups flattened + imported dicts) under
    match_mode (contains = fuzzy, exact = whole-message equality)."""
    if (config.get("match_type") or "keyword") == "any":
        return True
    mode = config.get("match_mode") or "contains"
    ntext = norm(text)
    if not ntext:
        return False
    keywords: list[str] = []
    for group in config.get("keyword_groups") or []:
        if isinstance(group, list):
            keywords.extend(str(k) for k in group)
        elif isinstance(group, str):
            keywords.append(group)
    keywords.extend(dict_keywords)
    for kw in keywords:
        nkw = norm(kw)
        if not nkw:
            continue
        if mode == "exact":
            if ntext == nkw:
                return True
        else:  # contains (fuzzy)
            if nkw in ntext:
                return True
    return False


def match_page(url: str | None, config: dict[str, Any]) -> bool:
    """訪問特定頁面 matcher (also constrains widget_opened when rules given).
    Empty config = matches any page."""
    rules = config.get("rules")
    if rules is None:
        single = config.get("url")
        if not single:
            return True  # no constraint
        rules = [{"op": config.get("op", "contains"), "value": single}]
    if not rules:
        return True
    if not url:
        return False
    nurl = norm(url)
    for rule in rules:
        op = (rule.get("op") or "contains").lower()
        value = norm(rule.get("value") or "")
        if not value:
            continue
        if op == "exact" and nurl == value:
            return True
        if op == "prefix" and nurl.startswith(value):
            return True
        if op == "contains" and value in nurl:
            return True
        if op == "regex":
            try:
                if re.search(rule.get("value") or "", url):
                    return True
            except re.error:
                continue
    return False


def match_visitor_kind(trigger_type: str, kind: str | None) -> bool:
    if trigger_type == "new_visitor":
        return kind == "new"
    if trigger_type == "returning_visitor":
        return kind == "returning"
    return False


def _timeout_still_valid(config: dict[str, Any], now: datetime, tz_name: str | None) -> bool:
    """Timeout triggers may restrict to a schedule window; empty = always."""
    windows = config.get("windows")
    if not windows:
        return True
    return in_schedule(now, windows, config.get("timezone") or tz_name)


# ==========================================================================
# candidate query + content evaluation
# ==========================================================================
@dataclass
class TriggerMatch:
    trigger: FlowTrigger
    flow: Flow


async def _dict_keywords(
    session: AsyncSession, workspace_id: uuid.UUID, dict_ids: list[Any]
) -> list[str]:
    ids: list[uuid.UUID] = []
    for raw in dict_ids or []:
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    if not ids:
        return []
    rows = (
        await session.execute(
            select(KeywordDictItem.keyword).where(
                KeywordDictItem.workspace_id == workspace_id,
                KeywordDictItem.dict_id.in_(ids),
            )
        )
    ).scalars().all()
    return list(rows)


async def matching_triggers(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    channel_type: str,
    trigger_types: tuple[str, ...],
    text: str = "",
    url: str | None = None,
    kind: str | None = None,
    now: datetime,
    workspace_tz: str | None = None,
) -> list[TriggerMatch]:
    """Indexed lookup + per-row content evaluation. Returns content-matching
    triggers sorted by (flow.priority asc, flow.updated_at desc)."""
    rows = (
        await session.execute(
            select(FlowTrigger, Flow)
            .join(Flow, Flow.id == FlowTrigger.flow_id)
            .where(
                FlowTrigger.workspace_id == workspace_id,
                FlowTrigger.channel_type == channel_type,
                FlowTrigger.trigger_type.in_(list(trigger_types)),
                FlowTrigger.enabled.is_(True),
                Flow.enabled.is_(True),
            )
        )
    ).all()

    matches: list[TriggerMatch] = []
    for trigger, flow in rows:
        cfg = trigger.config or {}
        tt = trigger.trigger_type
        ok = False
        if tt == "visitor_message":
            dict_kw = await _dict_keywords(session, workspace_id, cfg.get("dict_ids") or [])
            ok = match_message(text, cfg, dict_kw)
        elif tt in ("page_visited", "widget_opened"):
            ok = match_page(url, cfg)
        elif tt == "lead_submitted":
            ok = True
        elif tt in ("new_visitor", "returning_visitor"):
            ok = match_visitor_kind(tt, kind)
        elif tt in ("agent_timeout", "visitor_timeout"):
            ok = _timeout_still_valid(cfg, now, workspace_tz)
        elif tt == "visitor_intent":
            ok = False  # AI intent classification lives in the AI subsystem
        if ok:
            matches.append(TriggerMatch(trigger=trigger, flow=flow))

    matches.sort(key=lambda m: (m.flow.priority, _neg_updated(m.flow)))
    return matches


def _neg_updated(flow: Flow) -> float:
    """Sort key helper: newest updated_at first among equal priority."""
    ts = flow.updated_at
    return -(ts.timestamp() if ts else 0.0)


# ==========================================================================
# frequency capping (Redis fixed-window front cache + durable log)
# ==========================================================================
def _scope_id(
    scope: str,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> str:
    if scope == "contact":
        return str(contact_id or "anon")
    if scope == "conversation":
        return str(conversation_id or "none")
    return str(workspace_id)


def cap_key(trigger_id: uuid.UUID, scope: str, scope_id: str) -> str:
    return f"flowcap:{trigger_id}:{scope}:{scope_id}"


def _cap_params(trigger: FlowTrigger) -> tuple[str, int, int]:
    fc = trigger.freq_cap or {}
    scope = fc.get("scope") or "contact"
    count = int(fc.get("count") or 0)
    window_s = int(fc.get("window_s") or fc.get("window") or 0)
    return scope, count, window_s


async def freq_cap_allows(
    redis: aioredis.Redis | None,
    trigger: FlowTrigger,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> bool:
    """Peek: would firing this trigger stay within its cap? (No mutation — so a
    flow that loses priority never burns the visitor's allowance.)"""
    scope, count, window_s = _cap_params(trigger)
    if count <= 0 or window_s <= 0 or redis is None:
        return True
    key = cap_key(trigger.id, scope, _scope_id(scope, workspace_id, contact_id, conversation_id))
    cur = int(await redis.get(key) or 0)
    return cur < count


async def freq_cap_consume(
    redis: aioredis.Redis | None,
    trigger: FlowTrigger,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> None:
    """Consume one unit of the winner's cap (INCR + set the window TTL once)."""
    scope, count, window_s = _cap_params(trigger)
    if count <= 0 or window_s <= 0 or redis is None:
        return
    key = cap_key(trigger.id, scope, _scope_id(scope, workspace_id, contact_id, conversation_id))
    new = int(await redis.incr(key))
    if new == 1:
        await redis.expire(key, window_s)


# ==========================================================================
# selection + logging
# ==========================================================================
def log_outcome(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    flow_id: uuid.UUID,
    trigger_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    outcome: str,
) -> None:
    session.add(
        FlowTriggerLog(
            workspace_id=workspace_id,
            flow_id=flow_id,
            trigger_id=trigger_id,
            contact_id=contact_id,
            conversation_id=conversation_id,
            session_id=session_id,
            outcome=outcome,
        )
    )


async def select_winner(
    session: AsyncSession,
    redis: aioredis.Redis | None,
    matches: list[TriggerMatch],
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> TriggerMatch | None:
    """Pick the highest-precedence match whose freq-cap allows firing. Logs
    every considered flow (winner triggered, others suppressed)."""
    winner: TriggerMatch | None = None
    for m in matches:
        if winner is None and await freq_cap_allows(
            redis, m.trigger, workspace_id=workspace_id,
            contact_id=contact_id, conversation_id=conversation_id,
        ):
            winner = m
        else:
            log_outcome(
                session, workspace_id=workspace_id, flow_id=m.flow.id, trigger_id=m.trigger.id,
                contact_id=contact_id, conversation_id=conversation_id, session_id=None,
                outcome="suppressed",
            )
    return winner
