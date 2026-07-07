"""Intent classification (plan 附錄 B.2 「意圖識別」).

One classification per inbound message, shared across every flow's ai_intent
trigger. Result is cached in Redis by (workspace, normalised text) for 24h so a
repeated phrase never re-hits the LLM (1 point per uncached call). The LLM is
asked to pick a number from a compact numbered list (name + description + 2
examples) or 0 for "none" — forced, parseable, relay-proof.

Expose classify_intent() for the flow-engine's ai_intent trigger to call
directly; a message.intent_classified event is also emitted for analytics.
"""
from __future__ import annotations

import hashlib
import re
import uuid

import redis.asyncio as aioredis
from py_contracts.events import Actor, Event
from py_contracts.llm import LLMMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.ai import Intent
from ..services.llm_client import LLMClientProtocol, get_default_llm
from . import points_enforce

CACHE_TTL_S = 24 * 3600
NONE_SENTINEL = "-"  # cached value meaning "classified, no intent matched"
MAX_TEXT_FOR_HASH = 512

_INTENT_SYSTEM = (
    "You are an intent classifier. Read the customer message and choose the "
    "SINGLE best-matching intent from the numbered list. Reply with ONLY the "
    "number. If none clearly matches, reply 0."
)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:MAX_TEXT_FOR_HASH]


def cache_key(workspace_id: uuid.UUID, text: str) -> str:
    digest = hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()[:20]
    return f"intent:{workspace_id}:{digest}"


def build_intent_prompt(intents: list[Intent], text: str) -> str:
    """Numbered list (1..N) of name + description + up to 2 examples, then the
    message to classify. Index 0 is reserved for 'none'."""
    lines: list[str] = []
    for i, intent in enumerate(intents, start=1):
        lines.append(f"{i}. {intent.name}")
        if intent.description:
            lines.append(f"   description: {intent.description}")
        examples = [str(e) for e in (intent.examples or [])][:2]
        for ex in examples:
            lines.append(f"   example: {ex}")
    listing = "\n".join(lines)
    return (
        f"Intents:\n{listing}\n\n"
        f"Customer message:\n{text}\n\n"
        f"Answer with the intent number (1-{len(intents)}) or 0 for none."
    )


def parse_choice(raw: str, n_intents: int) -> int:
    """Extract the leading integer choice; clamp out-of-range to 0 (none)."""
    m = re.search(r"-?\d+", raw or "")
    if not m:
        return 0
    val = int(m.group())
    return val if 1 <= val <= n_intents else 0


async def classify_intent(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    text: str,
    client: LLMClientProtocol | None = None,
    emit_event: bool = True,
) -> uuid.UUID | None:
    """Classify one message into a workspace intent (or None). Cached 24h;
    charges 1 `intent` point only on a cache miss. On a points hard-stop the
    trigger stays silent (returns None). Writes the ledger into the session —
    the caller commits."""
    norm = normalize_text(text)
    if not norm:
        return None

    intents = (
        await session.execute(
            select(Intent)
            .where(Intent.workspace_id == workspace_id, Intent.enabled.is_(True))
            .order_by(Intent.created_at)
        )
    ).scalars().all()
    if not intents:
        return None

    key = cache_key(workspace_id, text)
    cached = await redis.get(key)
    if cached is not None:
        if cached == NONE_SENTINEL:
            return None
        try:
            intent_id = uuid.UUID(cached)
        except ValueError:
            intent_id = None
        # guard against a since-deleted/disabled intent
        if intent_id is not None and any(i.id == intent_id for i in intents):
            return intent_id
        return None

    # cache miss → meter 1 point (skip trigger silently if exhausted)
    outcome = await points_enforce.spend(
        session, redis, workspace_id=workspace_id, feature_key="intent",
        ref_type="intent",
    )
    if outcome.blocked:
        return None

    client = client or get_default_llm()
    try:
        raw = await client.complete(
            tier="fast",
            system=_INTENT_SYSTEM,
            messages=[LLMMessage(role="user", content=build_intent_prompt(list(intents), text))],
            max_tokens=8,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — LLM failure = no classification (do not cache)
        return None

    choice = parse_choice(raw, len(intents))
    intent = intents[choice - 1] if choice >= 1 else None
    intent_id = intent.id if intent else None
    await redis.set(key, str(intent_id) if intent_id else NONE_SENTINEL, ex=CACHE_TTL_S)

    if emit_event and intent_id is not None:
        from ..services import event_bus

        await event_bus.emit(
            session,
            Event(
                workspace_id=workspace_id,
                type="message.intent_classified",
                actor=Actor(type="ai_agent"),
                payload={"intent_id": str(intent_id), "intent_name": intent.name if intent else None},
            ),
        )
    return intent_id
