"""Composer AI assist (plan 附錄 B.2 「Composer AI 輔助」).

Stateless one-shot ops on the agent's draft — rewrite / expand / shorten / tone
/ fix_grammar / translate_draft. Tier fast, 2 points per call, SSE-streamed, no
persistence. On a points hard-stop the op errors (the UI shows an upgrade CTA).

The injected LLMClient only exposes a non-streaming complete(); stream_assist()
charges up front then yields the result in word deltas so the composer renders
progressively. run_assist() is the awaitable form used by tests.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from py_contracts.llm import LLMMessage
from sqlalchemy.ext.asyncio import AsyncSession

from ..services.llm_client import LLMClientProtocol, get_default_llm
from . import points_enforce

ASSIST_OPS: frozenset[str] = frozenset(
    {"rewrite", "expand", "shorten", "tone", "fix_grammar", "translate_draft"}
)

_OP_INSTRUCTIONS: dict[str, str] = {
    "rewrite": "Rewrite the agent's draft reply to be clearer and more professional.",
    "expand": "Expand the agent's draft reply with helpful detail while staying on topic.",
    "shorten": "Make the agent's draft reply more concise without losing key information.",
    "tone": "Rewrite the agent's draft reply in a {tone} tone.",
    "fix_grammar": "Fix spelling, grammar and punctuation in the agent's draft. "
    "Keep the wording and meaning otherwise unchanged.",
    "translate_draft": "Translate the agent's draft reply into {target_lang}.",
}

_SYSTEM = (
    "You help a customer-service agent polish a draft reply. Apply the requested "
    "operation and reply with ONLY the resulting message text — no quotes, no "
    "preamble, no explanation."
)


class AssistError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def build_assist_messages(
    op: str, text: str, params: dict[str, Any] | None = None
) -> tuple[str, list[LLMMessage]]:
    """(system, messages) for an op. Raises AssistError on an unknown op or
    missing required params."""
    if op not in ASSIST_OPS:
        raise AssistError("unknown_op", f"unknown assist op '{op}'")
    params = params or {}
    instruction = _OP_INSTRUCTIONS[op]
    if op == "tone":
        instruction = instruction.format(tone=str(params.get("tone") or "friendly"))
    elif op == "translate_draft":
        target = params.get("target_lang")
        if not target:
            raise AssistError("missing_param", "translate_draft requires target_lang")
        instruction = instruction.format(target_lang=str(target))
    user = f"{instruction}\n\nDraft:\n{text}"
    return _SYSTEM, [LLMMessage(role="user", content=user)]


@dataclass
class AssistResult:
    text: str
    ok: bool
    balance_after: int


async def run_assist(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    op: str,
    text: str,
    params: dict[str, Any] | None = None,
    client: LLMClientProtocol | None = None,
) -> AssistResult:
    """Charge 2 points then run the op (tier fast). Raises AssistError with
    code 'insufficient_points' on a hard-stop, or 'llm_error' on failure
    (points are refunded on LLM failure). Writes the ledger into the session —
    the caller commits."""
    if not (text or "").strip():
        raise AssistError("empty_draft", "draft text is required")
    _system, messages = build_assist_messages(op, text, params)

    outcome = await points_enforce.spend(
        session, redis, workspace_id=workspace_id, feature_key="composer", ref_type="composer",
    )
    if outcome.blocked:
        raise AssistError("insufficient_points", "not enough AI points for composer assist")

    client = client or get_default_llm()
    try:
        result = await client.complete(
            tier="fast", system=_system, messages=messages, max_tokens=1024, temperature=0.4
        )
    except Exception as e:  # noqa: BLE001 — refund on failure so the agent isn't charged
        from ..services import points

        await points.refund(
            session, redis, workspace_id=workspace_id, points=outcome.points_charged,
            reason="composer:refund", ref_type="composer",
        )
        raise AssistError("llm_error", str(e)) from e
    return AssistResult(text=(result or "").strip(), ok=True, balance_after=outcome.balance_after)


def sse_chunks(text: str) -> list[str]:
    """Split a completed result into progressive deltas for SSE streaming."""
    if not text:
        return []
    words = text.split(" ")
    out: list[str] = []
    for i, w in enumerate(words):
        out.append(w if i == 0 else " " + w)
    return out


async def stream_assist(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    op: str,
    text: str,
    params: dict[str, Any] | None = None,
    client: LLMClientProtocol | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator of SSE payloads: {type: delta, text} … then
    {type: done, text, balance_after}, or {type: error, code, detail}. The
    caller commits the session after the generator is exhausted."""
    try:
        result = await run_assist(
            session, redis, workspace_id=workspace_id, op=op, text=text,
            params=params, client=client,
        )
    except AssistError as e:
        yield {"type": "error", "code": e.code, "detail": e.detail}
        return
    for delta in sse_chunks(result.text):
        yield {"type": "delta", "text": delta}
    yield {"type": "done", "text": result.text, "balance_after": result.balance_after}
