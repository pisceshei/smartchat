"""AI member reply loop (plan 附錄 B.2 「AI 成員」+ 託管狀態機 + 標記協議).

handle_ai_inbound() is the single entry the flow-engine / routing calls when a
conversation is handler=ai_agent & ai_state=managed and a customer message
arrives. It:
  1. guards  — applicability, per-message idempotency + per-conversation lock,
     agent monthly quota (ai_agent_usage), workspace AI points (ai_reply)
  2. context — last ~20 messages + contact profile + RAG retrieval (condensed
     query) over the agent's KB collections
  3. generate — builtin LLM (persona system prompt, tier, temp 0.3) OR external
     webhook (HMAC; timeout/5xx → handoff)
  4. markers — parse_markers: [CARD:handles] validated against the product
     catalog (hallucinated handles dropped), [HANDOFF:reason] → escalate,
     [LEAD:field=value] → update contact
  5. send    — via messaging.send_message(sender_type='ai_agent')

Managed state machine: off | managed | paused_human. A human interjecting flips
managed→paused_human (pause_ai_for_human, driven by the standalone consumer);
resume_idle_ai auto-resumes after the configured idle window. Handoff sets
ai_state=off, moves the conversation to the unassigned pool, and leaves an
LLM-written internal summary note.

The standalone ai_agent_consumer() (consumer group 'ai-agent' on
events:conversation) is provided for a dedicated process; it is INDEPENDENT of
the flow-engine's own consumer group. handle_ai_inbound is idempotent so being
driven from both is safe.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import redis.asyncio as aioredis
from py_contracts.content import MessageContent
from py_contracts.events import STREAMS, Actor, Event
from py_contracts.llm import LLMMessage, parse_markers
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.ai import AIAgent, AIAgentUsage
from ..models.contacts import Contact
from ..models.conversations import Conversation
from ..models.messaging import Message
from ..models.tenancy import Workspace
from ..services import event_bus, messaging, points, routing
from ..services.llm_client import LLMClientProtocol, get_default_llm
from . import points_enforce, rag

log = logging.getLogger("smartchat.ai.agent_runtime")

AI_CONSUMER_GROUP = "ai-agent"
HISTORY_LIMIT = 20
LOCK_TTL_S = 90
MSG_MARKER_TTL_S = 900
DEFAULT_MAX_KB_MISS = 3
MISS_WINDOW_S = 3600

# contact scalar columns a [LEAD:field=value] may write directly; anything else
# lands under contact.custom.*
LEAD_SCALAR_FIELDS: frozenset[str] = frozenset(
    {"email", "phone", "display_name", "language", "country", "city"}
)


# ==========================================================================
# pure helpers (unit-tested)
# ==========================================================================
def keyword_hit(text: str, keywords: list[str] | None) -> bool:
    if not keywords:
        return False
    low = (text or "").lower()
    return any(str(k).lower() in low for k in keywords if str(k).strip())


def validate_cards(handles: list[str], catalog: dict[str, dict[str, Any]]) -> list[str]:
    """Keep only handles that exist in the grounded catalog (drop hallucinated),
    de-duplicated, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for h in handles:
        if h in catalog and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def build_card_blocks(handles: list[str], catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for h in handles:
        meta = catalog.get(h) or {}
        blocks.append(
            {
                "kind": "product_card",
                "title": str(meta.get("title") or meta.get("name") or h),
                "subtitle": meta.get("subtitle"),
                "image_url": meta.get("image_url"),
                "price": str(meta["price"]) if meta.get("price") not in (None, "") else None,
                "currency": meta.get("currency"),
                "url": meta.get("url"),
                "buttons": [],
            }
        )
    return blocks


def split_lead_fields(lead_fields: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(scalar column updates, custom.* updates) from parsed [LEAD:] markers."""
    scalars: dict[str, str] = {}
    custom: dict[str, str] = {}
    for raw_key, value in lead_fields.items():
        key = raw_key.strip()
        val = (value or "").strip()
        if not key or not val:
            continue
        if key.startswith("custom."):
            custom[key.split(".", 1)[1]] = val
        elif key in LEAD_SCALAR_FIELDS:
            scalars[key] = val
        else:
            custom[key] = val
    return scalars, custom


def persona_system_prompt(
    persona: dict[str, Any],
    retrieved: rag.Retrieved,
    catalog_handles: list[str],
    *,
    allow_cards: bool,
) -> str:
    """Compose the agent system prompt: persona + RAG guardrail + marker
    protocol (+ grounded card handles)."""
    persona = persona or {}
    role = (
        persona.get("system")
        or persona.get("prompt")
        or persona.get("role")
        or "You are a helpful customer-service assistant."
    )
    parts: list[str] = [str(role)]

    tone = persona.get("tone")
    if tone:
        parts.append(f"Tone: {tone}.")
    languages = persona.get("languages")
    if languages:
        langs = ", ".join(str(x) for x in languages) if isinstance(languages, list) else str(languages)
        parts.append(f"Reply in the customer's language (supported: {langs}).")
    else:
        parts.append("Always reply in the customer's language.")
    refusal = persona.get("refusal_topics") or persona.get("refuse")
    if refusal:
        topics = ", ".join(str(x) for x in refusal) if isinstance(refusal, list) else str(refusal)
        parts.append(f"Politely decline to discuss: {topics}.")

    if retrieved and retrieved.hit:
        parts.append(
            "Answer the customer using ONLY the CONTEXT below. If the answer is "
            "not contained in the CONTEXT, say you are not certain and include "
            "[HANDOFF:no_context] to bring in a human.\n\nCONTEXT:\n"
            + retrieved.context_text()
        )
    else:
        parts.append(
            "You have no knowledge-base context for this question. If you cannot "
            "answer confidently from the conversation alone, include "
            "[HANDOFF:no_context] to bring in a human."
        )

    marker_lines = [
        "Marker protocol — the customer never sees these markers, keep them on "
        "their own and write natural text around them:",
        "- To hand off to a human agent, include [HANDOFF:reason].",
        "- To save a customer detail, include [LEAD:field=value] "
        "(fields: email, phone, display_name, language, country, city, or custom.<name>).",
    ]
    if allow_cards and catalog_handles:
        sample = ", ".join(sorted(catalog_handles)[:40])
        marker_lines.append(
            f"- To show product cards, include [CARD:handle1,handle2] using ONLY "
            f"these handles: {sample}."
        )
    parts.append("\n".join(marker_lines))
    return "\n\n".join(p for p in parts if p)


def history_to_messages(rows: list[Message]) -> list[LLMMessage]:
    """Map stored messages (oldest→newest) to LLM turns: contact→user,
    member/ai_agent→assistant; skip notes/system/empty."""
    out: list[LLMMessage] = []
    for m in rows:
        if m.is_note or m.msg_type == "system_event":
            continue
        text = (m.text_plain or "").strip()
        if not text:
            continue
        role = "user" if m.sender_type == "contact" else "assistant"
        out.append(LLMMessage(role=role, content=text))
    return out


# ==========================================================================
# external agent webhook
# ==========================================================================
async def call_external_agent(agent: AIAgent, *, payload: dict[str, Any]) -> str:
    """POST the conversation context to the tenant's own AI webhook, signed with
    HMAC-SHA256. Raises on missing config / timeout / 5xx (→ handoff)."""
    cfg = agent.external or {}
    url = cfg.get("webhook_url") or cfg.get("url")
    if not url:
        raise ValueError("external agent has no webhook_url")
    secret = str(cfg.get("hmac_secret") or cfg.get("secret") or "")
    timeout = float(cfg.get("timeout_s", 10))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": f"sha256={signature}"},
        )
    if resp.status_code >= 500:
        raise RuntimeError(f"external agent 5xx: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("reply") or data.get("text") or "")


# ==========================================================================
# outcome
# ==========================================================================
@dataclass
class AIOutcome:
    action: str  # replied / handoff / skipped
    events: list[Event] = field(default_factory=list)
    reply_text: str | None = None
    handoff_reason: str | None = None
    card_handles: list[str] = field(default_factory=list)


# ==========================================================================
# main entry
# ==========================================================================
async def handle_ai_inbound(
    session: AsyncSession,
    redis: aioredis.Redis,
    conversation: Conversation,
    message: Message,
    *,
    client: LLMClientProtocol | None = None,
    now: datetime | None = None,
) -> AIOutcome | None:
    """Generate + send an AI reply for an inbound customer message (or hand off).
    Returns None when not applicable (not AI-managed / duplicate / lock held).
    Emits events into `session`; the caller commits and publish_realtime()s."""
    if conversation.handler != "ai_agent" or conversation.ai_state != "managed":
        return None
    if conversation.assignee_member_id is None:
        return None
    if message.direction != "in" or message.sender_type != "contact" or message.is_note:
        return None

    marker = f"ai:done:{message.id}"
    if not await redis.set(marker, "1", nx=True, ex=MSG_MARKER_TTL_S):
        return None  # already processed / in flight
    lock = f"ai:lock:{conversation.id}"
    if not await redis.set(lock, "1", nx=True, ex=LOCK_TTL_S):
        await redis.delete(marker)  # let it be retried once the lock frees
        return None
    try:
        return await _run(session, redis, conversation, message, client=client, now=now)
    except Exception:
        await redis.delete(marker)  # allow reprocessing after a hard failure
        raise
    finally:
        await redis.delete(lock)


async def _load_agent(session: AsyncSession, conversation: Conversation) -> AIAgent | None:
    return (
        await session.execute(
            select(AIAgent).where(
                AIAgent.member_id == conversation.assignee_member_id,
                AIAgent.workspace_id == conversation.workspace_id,
            )
        )
    ).scalars().first()


async def _recent_messages(session: AsyncSession, conversation: Conversation) -> list[Message]:
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars().all()
    return list(reversed(rows))


async def _run(
    session: AsyncSession,
    redis: aioredis.Redis,
    conversation: Conversation,
    message: Message,
    *,
    client: LLMClientProtocol | None,
    now: datetime | None,
) -> AIOutcome:
    now = now or datetime.now(UTC)
    client = client or get_default_llm()
    agent = await _load_agent(session, conversation)
    if agent is None or not agent.enabled:
        return AIOutcome(action="skipped")

    month = f"{now.year:04d}-{now.month:02d}"
    rules = agent.escalation_rules or {}
    skills = agent.skills or []
    customer_text = (message.text_plain or "").strip()
    history_rows = await _recent_messages(session, conversation)

    # ---- monthly quota guard ----
    if agent.monthly_msg_quota > 0:
        usage = await _get_usage(session, agent, month)
        if usage.replies >= agent.monthly_msg_quota:
            return await _handoff(session, redis, conversation, agent, reason="quota_exceeded",
                                  history_rows=history_rows, client=client, now=now)

    # ---- escalation pre-checks ----
    if keyword_hit(customer_text, rules.get("keywords")):
        return await _handoff(session, redis, conversation, agent, reason="keyword",
                              history_rows=history_rows, client=client, now=now)

    # ---- context (RAG) ----
    collection_ids = _collection_ids(agent)
    history_texts = [m.text_plain for m in history_rows if m.text_plain and not m.is_note]
    condensed = await rag.condense_query(client, history=history_texts[:-1], query=customer_text)
    retrieved = await rag.retrieve(
        session, workspace_id=conversation.workspace_id, collection_ids=collection_ids,
        query=condensed or customer_text, client=client,
    )
    allow_cards = "product_card" in skills
    catalog = (
        await rag.product_catalog(session, workspace_id=conversation.workspace_id,
                                  collection_ids=collection_ids)
        if allow_cards else {}
    )

    # ---- consecutive KB-miss escalation ----
    miss_key = f"ai:miss:{conversation.id}"
    max_miss = int(rules.get("max_kb_miss", DEFAULT_MAX_KB_MISS) or 0)
    if collection_ids and not retrieved.hit:
        misses = int(await redis.incr(miss_key))
        await redis.expire(miss_key, MISS_WINDOW_S)
        if max_miss > 0 and misses >= max_miss:
            await redis.delete(miss_key)
            return await _handoff(session, redis, conversation, agent, reason="kb_miss",
                                  history_rows=history_rows, client=client, now=now)
    else:
        await redis.delete(miss_key)

    # ---- generate ----
    charged = 0
    if agent.mode == "external":
        try:
            payload = _external_payload(conversation, history_rows, customer_text)
            raw = await call_external_agent(agent, payload=payload)
        except Exception:  # noqa: BLE001 — external failure → handoff
            log.warning("external agent failed for conversation %s", conversation.id, exc_info=True)
            return await _handoff(session, redis, conversation, agent, reason="external_error",
                                  history_rows=history_rows, client=client, now=now)
    else:
        spend = await points_enforce.spend(
            session, redis, workspace_id=conversation.workspace_id, feature_key="ai_reply",
            ref_type="ai_reply", ref_id=str(conversation.id),
        )
        if spend.blocked:
            return await _handoff(session, redis, conversation, agent, reason="points_exhausted",
                                  history_rows=history_rows, client=client, now=now)
        charged = spend.points_charged
        system = persona_system_prompt(agent.persona or {}, retrieved, list(catalog.keys()),
                                       allow_cards=allow_cards)
        try:
            raw = await client.complete(
                tier=agent.model_tier or "smart", system=system,
                messages=history_to_messages(history_rows), max_tokens=1024, temperature=0.3,
            )
        except Exception:  # noqa: BLE001 — refund the reserved points, then handoff
            if charged:
                await points.refund(session, redis, workspace_id=conversation.workspace_id,
                                    points=charged, reason="ai_reply:refund", ref_type="ai_reply")
            log.warning("LLM completion failed for conversation %s", conversation.id, exc_info=True)
            return await _handoff(session, redis, conversation, agent, reason="llm_error",
                                  history_rows=history_rows, client=client, now=now)

    # ---- markers ----
    parsed = parse_markers(raw or "")
    kept_handles = validate_cards(parsed.card_handles, catalog) if allow_cards else []
    events: list[Event] = []
    lead_enabled = ("lead_capture" in skills) or not skills  # default on if no skills configured
    if parsed.lead_fields and lead_enabled:
        await _apply_lead(session, conversation, parsed.lead_fields)

    if parsed.handoff_reason:
        # send the apology text (if any) as the AI, then escalate
        if parsed.text:
            sent = await messaging.send_message(
                session, conversation=conversation, sender_type="ai_agent",
                sender_id=agent.member_id,
                content=MessageContent(blocks=[{"kind": "text", "text": parsed.text}]),
                redis=redis, now=now,
            )
            events.extend(sent.events)
        ho = await _handoff(session, redis, conversation, agent, reason=parsed.handoff_reason,
                            history_rows=history_rows, client=client, now=now)
        events.extend(ho.events)
        return AIOutcome(action="handoff", events=events, reply_text=parsed.text or None,
                         handoff_reason=parsed.handoff_reason)

    # ---- normal reply ----
    blocks: list[dict[str, Any]] = []
    if parsed.text:
        blocks.append({"kind": "text", "text": parsed.text})
    blocks.extend(build_card_blocks(kept_handles, catalog))
    if not blocks:
        # model emitted nothing usable — refund the reply charge and skip
        if charged:
            await points.refund(session, redis, workspace_id=conversation.workspace_id,
                                points=charged, reason="ai_reply:refund", ref_type="ai_reply")
        return AIOutcome(action="skipped", events=events)

    sent = await messaging.send_message(
        session, conversation=conversation, sender_type="ai_agent", sender_id=agent.member_id,
        content=MessageContent(blocks=blocks), redis=redis, now=now,
    )
    events.extend(sent.events)
    await _bump_usage(session, agent, month)
    ai_ev = Event(
        workspace_id=conversation.workspace_id, type="ai.reply",
        actor=Actor(type="ai_agent", id=agent.member_id),
        conversation_id=conversation.id, contact_id=conversation.contact_id,
        channel_type=conversation.channel_type, channel_account_id=conversation.channel_account_id,
        payload={"agent_id": str(agent.id), "message_id": str(sent.message.id),
                 "cards": kept_handles, "kb_hit": retrieved.hit},
    )
    await event_bus.emit(session, ai_ev)
    events.append(ai_ev)
    return AIOutcome(action="replied", events=events, reply_text=parsed.text, card_handles=kept_handles)


def _collection_ids(agent: AIAgent) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for raw in agent.kb_collection_ids or []:
        try:
            out.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    return out


def _external_payload(conversation: Conversation, history_rows: list[Message], text: str) -> dict[str, Any]:
    return {
        "conversation_id": str(conversation.id),
        "workspace_id": str(conversation.workspace_id),
        "channel_type": conversation.channel_type,
        "message": text,
        "history": [
            {"role": "customer" if m.sender_type == "contact" else "agent", "text": m.text_plain}
            for m in history_rows if m.text_plain and not m.is_note
        ],
    }


async def _apply_lead(session: AsyncSession, conversation: Conversation, lead_fields: dict[str, str]) -> None:
    contact = await session.get(Contact, conversation.contact_id)
    if contact is None:
        return
    scalars, custom = split_lead_fields(lead_fields)
    for key, val in scalars.items():
        setattr(contact, key, val)
    if custom:
        contact.custom = {**(contact.custom or {}), **custom}


# ==========================================================================
# handoff / summary
# ==========================================================================
_SUMMARY_SYSTEM = (
    "Summarise this customer-service conversation in 2-4 sentences for the human "
    "agent who is taking over: what the customer wants, key details, and what is "
    "still unresolved. Reply with only the summary."
)


async def _summarize(
    session: AsyncSession,
    redis: aioredis.Redis,
    conversation: Conversation,
    client: LLMClientProtocol,
    history_rows: list[Message],
) -> str | None:
    """Best-effort LLM conversation summary (charges 5 `summary` points; skipped
    on a points hard-stop)."""
    spend = await points_enforce.spend(
        session, redis, workspace_id=conversation.workspace_id, feature_key="summary",
        ref_type="summary", ref_id=str(conversation.id),
    )
    if spend.blocked:
        return None
    transcript = "\n".join(
        f"{'Customer' if m.sender_type == 'contact' else 'Agent'}: {m.text_plain}"
        for m in history_rows if m.text_plain and not m.is_note
    )
    if not transcript.strip():
        return None
    try:
        return (
            await client.complete(
                tier="fast", system=_SUMMARY_SYSTEM,
                messages=[LLMMessage(role="user", content=transcript)],
                max_tokens=256, temperature=0.2,
            )
        ).strip() or None
    except Exception:  # noqa: BLE001
        await points.refund(session, redis, workspace_id=conversation.workspace_id,
                            points=spend.points_charged, reason="summary:refund", ref_type="summary")
        return None


async def _handoff(
    session: AsyncSession,
    redis: aioredis.Redis,
    conversation: Conversation,
    agent: AIAgent,
    *,
    reason: str,
    history_rows: list[Message],
    client: LLMClientProtocol,
    now: datetime,
) -> AIOutcome:
    """Escalate to a human: LLM summary as an internal note, ai_state off,
    move to the unassigned pool (frees the AI's cap seat), emit ai.handoff."""
    events: list[Event] = []
    summary = await _summarize(session, redis, conversation, client, history_rows)
    note = f"AI handoff ({reason})."
    if summary:
        note = f"AI handoff ({reason}):\n{summary}"
    note_res = await messaging.send_message(
        session, conversation=conversation, sender_type="ai_agent", sender_id=agent.member_id,
        content=MessageContent(blocks=[{"kind": "text", "text": note}]), is_note=True,
        redis=redis, now=now,
    )
    events.extend(note_res.events)

    conversation.ai_state = "off"
    conversation.bot_managed = False
    target_group = (agent.escalation_rules or {}).get("handoff_member_id")
    to_member_id = _maybe_uuid(target_group)
    tr = await routing.transfer(
        session, redis, workspace_id=conversation.workspace_id, conversation_id=conversation.id,
        to_member_id=to_member_id, actor=Actor(type="ai_agent", id=agent.member_id), reason="handoff",
    )
    events.extend(tr.events)

    ho_ev = Event(
        workspace_id=conversation.workspace_id, type="ai.handoff",
        actor=Actor(type="ai_agent", id=agent.member_id),
        conversation_id=conversation.id, contact_id=conversation.contact_id,
        channel_type=conversation.channel_type, channel_account_id=conversation.channel_account_id,
        payload={"agent_id": str(agent.id), "reason": reason, "summary": summary},
    )
    await event_bus.emit(session, ho_ev)
    events.append(ho_ev)
    return AIOutcome(action="handoff", events=events, handoff_reason=reason)


def _maybe_uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except (ValueError, TypeError):
        return None


# ==========================================================================
# usage counter
# ==========================================================================
async def _get_usage(session: AsyncSession, agent: AIAgent, month: str) -> AIAgentUsage:
    row = await session.get(AIAgentUsage, (agent.id, month))
    if row is None:
        row = AIAgentUsage(agent_id=agent.id, month=month, workspace_id=agent.workspace_id, replies=0)
        session.add(row)
        await session.flush()
    return row


async def _bump_usage(session: AsyncSession, agent: AIAgent, month: str) -> None:
    stmt = (
        pg_insert(AIAgentUsage)
        .values(agent_id=agent.id, month=month, workspace_id=agent.workspace_id, replies=1)
        .on_conflict_do_update(
            index_elements=["agent_id", "month"],
            set_={"replies": AIAgentUsage.replies + 1},
        )
    )
    await session.execute(stmt)


# ==========================================================================
# managed state machine — pause on human, idle auto-resume
# ==========================================================================
async def pause_ai_for_human(
    session: AsyncSession, redis: aioredis.Redis, conversation: Conversation
) -> list[Event]:
    """A human replied in an AI-managed conversation → pause (managed →
    paused_human). Emits conversation.updated. No-op if not AI-managed."""
    if conversation.handler != "ai_agent" or conversation.ai_state != "managed":
        return []
    conversation.ai_state = "paused_human"
    ev = messaging._conversation_event(conversation, Actor(type="member"))
    await event_bus.emit(session, ev)
    return [ev]


async def resume_idle_ai(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    now: datetime | None = None,
    limit: int = 500,
) -> int:
    """Auto-resume paused_human conversations idle past the workspace's
    settings.ai.resume_idle_hours (0/unset = disabled). Returns count resumed."""
    now = now or datetime.now(UTC)
    resumed = 0
    async with session_factory() as session:
        convs = (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.ai_state == "paused_human",
                    Conversation.handler == "ai_agent",
                    Conversation.status == "open",
                )
                .limit(limit)
            )
        ).scalars().all()
        hours_by_ws: dict[uuid.UUID, float] = {}
        events: list[Event] = []
        for conv in convs:
            hours = hours_by_ws.get(conv.workspace_id)
            if hours is None:
                ws = await session.get(Workspace, conv.workspace_id)
                ai_cfg = ((ws.settings or {}).get("ai") or {}) if ws else {}
                hours = float(ai_cfg.get("resume_idle_hours", 0) or 0)
                hours_by_ws[conv.workspace_id] = hours
            if hours <= 0:
                continue
            last = conv.last_agent_message_at or conv.last_message_at
            if last is None:
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last).total_seconds() < hours * 3600:
                continue
            conv.ai_state = "managed"
            ev = messaging._conversation_event(conv, Actor(type="system"))
            await event_bus.emit(session, ev)
            events.append(ev)
            resumed += 1
        if events:
            await session.commit()
            await messaging.publish_realtime(events)
    return resumed


# ==========================================================================
# standalone consumer (dedicated process; group 'ai-agent')
# ==========================================================================
async def process_event(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    event: Event,
    *,
    client: LLMClientProtocol | None = None,
) -> bool:
    """Dispatch one events:conversation entry. Returns True if it produced any
    AI side effect (reply / pause). Commits + publishes on effect."""
    if event.type != "message.created":
        return False
    payload = event.payload or {}
    if payload.get("is_note") or event.conversation_id is None:
        return False
    # messaging-service events carry sender_type in the payload; ingress-pipeline
    # events only identify the sender via the envelope actor — accept both.
    actor_type = event.actor.type if event.actor is not None else None
    sender_type = payload.get("sender_type") or actor_type
    direction = payload.get("direction")
    async with session_factory() as session:
        conversation = await session.get(Conversation, event.conversation_id)
        if conversation is None:
            return False
        events: list[Event] = []
        if sender_type == "contact" and direction == "in":
            message_id = _maybe_uuid(payload.get("message_id"))
            if message_id is None:
                return False
            message = await session.get(Message, message_id)
            if message is None:
                return False
            outcome = await handle_ai_inbound(session, redis, conversation, message, client=client)
            if outcome is not None:
                events = outcome.events
        elif sender_type == "member" and direction == "out":
            events = await pause_ai_for_human(session, redis, conversation)
        if events:
            await session.commit()
            await messaging.publish_realtime(events)
            await messaging.dispatch_channel_sends(events)
            return True
        await session.rollback()
    return False


async def drain_ai_events_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    consumer: str = "ai-1",
    client: LLMClientProtocol | None = None,
    block_ms: int = 1000,
    count: int = 64,
) -> int:
    """Read + process one batch from events:conversation for the 'ai-agent'
    group, acking each. Returns the number processed (for tests / drains)."""
    stream = STREAMS["conversation"]
    await event_bus.ensure_group(redis, stream, AI_CONSUMER_GROUP)
    batch = await event_bus.read_batch(
        redis, [stream], AI_CONSUMER_GROUP, consumer, count=count, block_ms=block_ms
    )
    for s, entry_id, event in batch:
        try:
            await process_event(session_factory, redis, event, client=client)
        except Exception:  # noqa: BLE001 — one bad event must not stall the group
            log.exception("ai consumer failed on event %s", getattr(event, "id", "?"))
        await event_bus.ack(redis, s, AI_CONSUMER_GROUP, entry_id)
    return len(batch)


async def ai_agent_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    consumer: str = "ai-1",
    client: LLMClientProtocol | None = None,
    block_ms: int = 5000,
    stop: Any = None,
) -> None:
    """Long-running consumer loop for a dedicated AI process. Independent of the
    flow-engine's consumer group, so both can drive handle_ai_inbound safely
    (idempotent). Run: a process that awaits ai_agent_consumer(...)."""
    stream = STREAMS["conversation"]
    await event_bus.ensure_group(redis, stream, AI_CONSUMER_GROUP)
    while stop is None or not stop.is_set():
        try:
            batch = await event_bus.read_batch(
                redis, [stream], AI_CONSUMER_GROUP, consumer, block_ms=block_ms
            )
        except Exception:  # noqa: BLE001
            log.exception("ai consumer read failed")
            continue
        for s, entry_id, event in batch:
            try:
                await process_event(session_factory, redis, event, client=client)
            except Exception:  # noqa: BLE001
                log.exception("ai consumer failed on event %s", getattr(event, "id", "?"))
            await event_bus.ack(redis, s, AI_CONSUMER_GROUP, entry_id)
