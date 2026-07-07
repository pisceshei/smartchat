"""Two-way translation (plan 附錄 B.2 「翻譯」).

Engine adapters (ordered fallback chain per workspace):
  - llm    : the injectable LLMClient (tier fast), batched; bills points
             (translate_llm_per500) and is always available
  - google : Google Cloud Translation v2 REST (API key optional → unavailable)
  - deepl  : DeepL REST (API key optional → unavailable)

Content-hash cache (translation_cache) is checked before any engine call; only
misses are metered. Character volume is metered per engine per month
(translation_usage). Per-conversation state {enabled, agent_lang, customer_lang}
lives on conversations.translation; customer_lang is detected with lingua and
may be overridden by an agent.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import redis.asyncio as aioredis
from py_contracts.llm import LLMMessage
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.ai import TranslationUsage
from ..models.messaging import TranslationCache
from ..services.llm_client import LLMClientProtocol, get_default_llm
from . import points_enforce

CHARS_PER_POINT = 500  # translate_llm_per500

# --------------------------------------------------------------------------
# language detection (lingua singleton — first build loads the models once)
# --------------------------------------------------------------------------
_detector: Any = None


def _get_detector() -> Any:
    global _detector
    if _detector is None:
        from lingua import LanguageDetectorBuilder

        _detector = LanguageDetectorBuilder.from_all_languages().build()
    return _detector


def detect_language(text: str) -> str | None:
    """Best-effort ISO-639-1 code (lowercase) for a piece of text, or None."""
    text = (text or "").strip()
    if len(text) < 2:
        return None
    try:
        lang = _get_detector().detect_language_of(text)
    except Exception:  # noqa: BLE001
        return None
    if lang is None:
        return None
    return lang.iso_code_639_1.name.lower()


# --------------------------------------------------------------------------
# engine adapters
# --------------------------------------------------------------------------
class TranslationEngine(Protocol):
    name: str
    available: bool
    bills_points: bool

    async def translate(self, texts: list[str], *, src_lang: str | None, dst_lang: str) -> list[str]: ...


_LLM_SYSTEM = (
    "You are a professional translator. Translate each numbered line into "
    "{dst}. Preserve meaning, tone, emoji and formatting. Reply with ONLY the "
    "translations, one per line, each prefixed with its original number and a "
    "'. ' — no commentary."
)


class LLMEngine:
    name = "llm"
    available = True
    bills_points = True

    def __init__(self, client: LLMClientProtocol | None = None):
        self._client = client

    @property
    def client(self) -> LLMClientProtocol:
        return self._client or get_default_llm()

    async def translate(self, texts: list[str], *, src_lang: str | None, dst_lang: str) -> list[str]:
        if not texts:
            return []
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        raw = await self.client.complete(
            tier="fast",
            system=_LLM_SYSTEM.format(dst=dst_lang),
            messages=[LLMMessage(role="user", content=numbered)],
            max_tokens=min(4096, 64 + sum(len(t) for t in texts)),
            temperature=0.2,
        )
        return _parse_numbered(raw, len(texts), texts)


class GoogleEngine:
    name = "google"
    bills_points = False

    def __init__(self, api_key: str | None = None, *, endpoint: str | None = None):
        self.api_key = api_key or os.getenv("GOOGLE_TRANSLATE_API_KEY", "")
        self.endpoint = endpoint or "https://translation.googleapis.com/language/translate/v2"

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def translate(self, texts: list[str], *, src_lang: str | None, dst_lang: str) -> list[str]:
        if not texts:
            return []
        params: dict[str, Any] = {"target": dst_lang, "format": "text", "key": self.api_key}
        if src_lang:
            params["source"] = src_lang
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(self.endpoint, params=params, json={"q": texts})
            resp.raise_for_status()
            data = resp.json()
        return [t["translatedText"] for t in data["data"]["translations"]]


class DeepLEngine:
    name = "deepl"
    bills_points = False

    def __init__(self, api_key: str | None = None, *, endpoint: str | None = None):
        self.api_key = api_key or os.getenv("DEEPL_API_KEY", "")
        self.endpoint = endpoint or "https://api-free.deepl.com/v2/translate"

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def translate(self, texts: list[str], *, src_lang: str | None, dst_lang: str) -> list[str]:
        if not texts:
            return []
        data: list[tuple[str, str]] = [("target_lang", dst_lang.upper())]
        if src_lang:
            data.append(("source_lang", src_lang.upper()))
        data.extend(("text", t) for t in texts)
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.post(
                self.endpoint, data=data,
                headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        return [t["text"] for t in payload["translations"]]


def _parse_numbered(raw: str, n: int, fallback: list[str]) -> list[str]:
    """Parse the LLM's numbered translation lines back into a list; if parsing
    fails for a slot, fall back to the source text for that slot."""
    out: list[str | None] = [None] * n
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, rest = line.partition(".")
        if sep and head.strip().isdigit():
            idx = int(head.strip()) - 1
            if 0 <= idx < n:
                out[idx] = rest.strip()
    if n == 1 and out[0] is None:
        out[0] = (raw or "").strip() or fallback[0]
    return [out[i] if out[i] is not None else fallback[i] for i in range(n)]


def build_engine(name: str, client: LLMClientProtocol | None = None) -> TranslationEngine | None:
    if name == "llm":
        return LLMEngine(client)
    if name == "google":
        return GoogleEngine()
    if name == "deepl":
        return DeepLEngine()
    return None


def build_chain(
    engine_names: list[str] | None, client: LLMClientProtocol | None = None
) -> list[TranslationEngine]:
    """Build the ordered engine chain, dropping unavailable ones. llm is always
    available so the chain is never empty (llm appended as a safety net)."""
    names = engine_names or ["llm"]
    chain: list[TranslationEngine] = []
    for n in names:
        eng = build_engine(n, client)
        if eng is not None and eng.available:
            chain.append(eng)
    if not any(e.name == "llm" for e in chain):
        chain.append(LLMEngine(client))
    return chain


# --------------------------------------------------------------------------
# cache + metering
# --------------------------------------------------------------------------
def content_hash(text: str, src_lang: str | None, dst_lang: str, engine: str) -> str:
    raw = f"{src_lang or ''}\x1f{dst_lang}\x1f{engine}\x1f{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _cache_get(session: AsyncSession, h: str) -> str | None:
    row = await session.get(TranslationCache, h)
    if row is None:
        return None
    from datetime import UTC, datetime

    row.hit_count = int(row.hit_count or 0) + 1
    row.last_used_at = datetime.now(UTC)
    return row.translated_text


async def _cache_put(
    session: AsyncSession, h: str, *, src_lang: str | None, dst_lang: str, engine: str, text: str
) -> None:
    stmt = (
        pg_insert(TranslationCache)
        .values(
            content_hash=h, src_lang=src_lang, dst_lang=dst_lang, engine=engine,
            translated_text=text, hit_count=0,
        )
        .on_conflict_do_nothing(index_elements=["content_hash"])
    )
    await session.execute(stmt)


async def meter_chars(
    session: AsyncSession, *, workspace_id: uuid.UUID, engine: str, chars: int, month: str | None = None
) -> None:
    if chars <= 0:
        return
    from ..services.quotas import current_period

    period = month or current_period()
    stmt = (
        pg_insert(TranslationUsage)
        .values(workspace_id=workspace_id, month=period, engine=engine, chars=chars)
        .on_conflict_do_update(
            index_elements=["workspace_id", "month", "engine"],
            set_={"chars": TranslationUsage.chars + chars},
        )
    )
    await session.execute(stmt)


# --------------------------------------------------------------------------
# high-level translate
# --------------------------------------------------------------------------
@dataclass
class TransResult:
    text: str
    engine: str
    cached: bool
    ok: bool
    detected_src: str | None = None


async def translate_text(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    text: str,
    dst_lang: str,
    src_lang: str | None = None,
    chain: list[TranslationEngine] | None = None,
) -> TransResult:
    """Translate one string through the fallback chain. Cache is checked per
    engine before any call; the llm engine bills points (translate_llm_per500)
    and falls through to the next engine on a hard-stop. Returns the original
    text (ok=False) if every engine fails."""
    text = text or ""
    detected = src_lang or detect_language(text)
    if not text.strip() or (detected and detected == dst_lang):
        return TransResult(text=text, engine="none", cached=True, ok=True, detected_src=detected)

    chain = chain or build_chain(["llm"])
    for engine in chain:
        if not engine.available:
            continue
        h = content_hash(text, detected, dst_lang, engine.name)
        hit = await _cache_get(session, h)
        if hit is not None:
            return TransResult(text=hit, engine=engine.name, cached=True, ok=True, detected_src=detected)
        if engine.bills_points:
            outcome = await points_enforce.spend(
                session, redis, workspace_id=workspace_id,
                feature_key="translate_llm_per500", amount=len(text), ref_type="translation",
            )
            if outcome.blocked:
                continue  # FALLBACK → next engine
        try:
            translated = (await engine.translate([text], src_lang=detected, dst_lang=dst_lang))[0]
        except Exception:  # noqa: BLE001 — engine failure → next in chain
            continue
        await _cache_put(session, h, src_lang=detected, dst_lang=dst_lang,
                         engine=engine.name, text=translated)
        await meter_chars(session, workspace_id=workspace_id, engine=engine.name, chars=len(text))
        return TransResult(text=translated, engine=engine.name, cached=False, ok=True, detected_src=detected)

    return TransResult(text=text, engine="none", cached=False, ok=False, detected_src=detected)


def conversation_translation_state(conversation: Any) -> dict[str, Any]:
    state = dict(getattr(conversation, "translation", None) or {})
    return {
        "enabled": bool(state.get("enabled")),
        "agent_lang": state.get("agent_lang"),
        "customer_lang": state.get("customer_lang"),
    }


async def translate_inbound(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    message_id: uuid.UUID,
    text: str,
    agent_lang: str,
    src_lang: str | None = None,
    chain: list[TranslationEngine] | None = None,
) -> tuple[str, str | None]:
    """Translate an inbound customer message into the agent's language and
    persist message_translations. Returns (translated_text, detected_src)."""
    result = await translate_text(
        session, redis, workspace_id=workspace_id, text=text,
        dst_lang=agent_lang, src_lang=src_lang, chain=chain,
    )
    if result.ok and result.engine != "none":
        from ..models.messaging import MessageTranslation

        stmt = (
            pg_insert(MessageTranslation)
            .values(
                message_id=message_id, target_lang=agent_lang, workspace_id=workspace_id,
                engine=result.engine, detected_source_lang=result.detected_src,
                translated_text=result.text,
            )
            .on_conflict_do_update(
                index_elements=["message_id", "target_lang"],
                set_={"translated_text": result.text, "engine": result.engine},
            )
        )
        await session.execute(stmt)
    return result.text, result.detected_src


async def translate_outbound(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    text: str,
    customer_lang: str,
    agent_lang: str | None = None,
    chain: list[TranslationEngine] | None = None,
) -> TransResult:
    """Translate an outbound agent message back into the customer's language."""
    return await translate_text(
        session, redis, workspace_id=workspace_id, text=text,
        dst_lang=customer_lang, src_lang=agent_lang, chain=chain,
    )
