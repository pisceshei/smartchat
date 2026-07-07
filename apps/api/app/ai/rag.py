"""KB chunking + pgvector/pg_trgm retrieval (plan 附錄 B.2 「KB+RAG」).

Chunking (source_type aware):
  - prose  : heading-aware recursive split, ~350–500 tokens, 15% overlap
  - faq    : one chunk per Q&A, never split
  - product: one structured chunk per SKU carrying meta.handle — this is the
             grounding source for [CARD:handle] product references

Retrieval:
  condense the question (tier fast) → embed → pgvector HNSW cosine top-8 +
  pg_trgm lexical top-8 → Reciprocal Rank Fusion merge → score floor. An empty
  result is a no-hit (the caller shows a fallback line and bumps its escalation
  counter). Guardrail: the agent prompt answers ONLY from the returned CONTEXT.

Token counts are approximate (chars/4) — good enough for chunk sizing; the
embed dimension check is authoritative (services.embeddings).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.ai import KBChunk, KBDocument
from ..services.embeddings import embed_query, embed_texts
from ..services.llm_client import LLMClientProtocol

# chunk sizing (approx tokens)
TARGET_TOKENS = 420
MAX_TOKENS = 500
MIN_TOKENS = 60
OVERLAP_RATIO = 0.15

# retrieval knobs
VECTOR_K = 8
LEXICAL_K = 8
RRF_K = 60
SCORE_FLOOR = 1.0 / (RRF_K + 60)  # below this = effectively no relevance
LEXICAL_SIM_FLOOR = 0.05


# --------------------------------------------------------------------------
# token estimate
# --------------------------------------------------------------------------
def approx_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token, min 1 for non-empty)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------
# chunk specs
# --------------------------------------------------------------------------
@dataclass
class ChunkSpec:
    text: str
    token_count: int
    meta: dict[str, Any] = field(default_factory=dict)


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+\S", re.MULTILINE)


def _split_paragraphs(text: str) -> list[str]:
    parts = re.split(r"\n\s*\n", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _split_sections(text: str) -> list[str]:
    """Split on markdown headings, keeping the heading with its body."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [text.strip()] if text.strip() else []
    sections: list[str] = []
    starts = [m.start() for m in matches]
    if starts[0] > 0:
        head = text[: starts[0]].strip()
        if head:
            sections.append(head)
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        seg = text[start:end].strip()
        if seg:
            sections.append(seg)
    return sections


def _overlap_tail(text: str, ratio: float) -> str:
    """Last `ratio` of a chunk (by words) to prepend to the next chunk."""
    words = text.split()
    if not words:
        return ""
    n = max(1, int(len(words) * ratio))
    return " ".join(words[-n:])


def chunk_prose(text: str, *, target_tokens: int = TARGET_TOKENS,
                max_tokens: int = MAX_TOKENS, overlap_ratio: float = OVERLAP_RATIO) -> list[str]:
    """Heading-aware recursive chunker with word overlap. A single oversized
    paragraph is hard-split on sentence boundaries so no chunk exceeds
    max_tokens by much."""
    text = (text or "").strip()
    if not text:
        return []
    units: list[str] = []
    for section in _split_sections(text):
        if approx_tokens(section) <= max_tokens:
            units.append(section)
            continue
        for para in _split_paragraphs(section):
            if approx_tokens(para) <= max_tokens:
                units.append(para)
            else:
                units.extend(_hard_split(para, max_tokens))

    chunks: list[str] = []
    cur = ""
    for unit in units:
        candidate = f"{cur}\n\n{unit}".strip() if cur else unit
        if cur and approx_tokens(candidate) > target_tokens:
            chunks.append(cur)
            tail = _overlap_tail(cur, overlap_ratio)
            cur = f"{tail}\n\n{unit}".strip() if tail else unit
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def _hard_split(text: str, max_tokens: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?。！？])\s+", text)
    out: list[str] = []
    cur = ""
    for s in sentences:
        candidate = f"{cur} {s}".strip()
        if cur and approx_tokens(candidate) > max_tokens:
            out.append(cur)
            cur = s
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def chunk_faq(items: list[dict[str, Any]]) -> list[ChunkSpec]:
    """One chunk per {question, answer} pair (never split)."""
    out: list[ChunkSpec] = []
    for item in items:
        q = str(item.get("question") or item.get("q") or "").strip()
        a = str(item.get("answer") or item.get("a") or "").strip()
        if not q and not a:
            continue
        text = f"Q: {q}\nA: {a}".strip()
        out.append(ChunkSpec(text=text, token_count=approx_tokens(text),
                             meta={"source_type": "faq", "question": q}))
    return out


def _product_text(item: dict[str, Any]) -> str:
    lines: list[str] = []
    title = str(item.get("title") or item.get("name") or "").strip()
    if title:
        lines.append(f"Product: {title}")
    for key, label in (
        ("handle", "Handle"), ("sku", "SKU"), ("price", "Price"),
        ("currency", "Currency"), ("category", "Category"), ("brand", "Brand"),
        ("availability", "Availability"),
    ):
        val = item.get(key)
        if val not in (None, ""):
            lines.append(f"{label}: {val}")
    desc = str(item.get("description") or item.get("body") or "").strip()
    if desc:
        lines.append(f"Description: {desc}")
    return "\n".join(lines)


def chunk_products(items: list[dict[str, Any]]) -> list[ChunkSpec]:
    """One structured chunk per product; meta.handle grounds [CARD:] cards."""
    out: list[ChunkSpec] = []
    for item in items:
        text = _product_text(item)
        if not text:
            continue
        handle = str(item.get("handle") or item.get("sku") or "").strip()
        meta: dict[str, Any] = {"source_type": "product"}
        if handle:
            meta["handle"] = handle
        for k in ("title", "name", "price", "currency", "url", "image_url", "sku"):
            if item.get(k) not in (None, ""):
                meta[k] = item[k]
        out.append(ChunkSpec(text=text, token_count=approx_tokens(text), meta=meta))
    return out


def build_chunks(
    *, source_type: str, content: Any, base_meta: dict[str, Any] | None = None
) -> list[ChunkSpec]:
    """Dispatch chunking by document source_type. `content` is a str for prose/
    upload, or a list[dict] for faq/product."""
    base = dict(base_meta or {})
    if source_type == "faq" and isinstance(content, list):
        specs = chunk_faq(content)
    elif source_type == "product" and isinstance(content, list):
        specs = chunk_products(content)
    else:
        text = content if isinstance(content, str) else str(content or "")
        specs = [
            ChunkSpec(text=t, token_count=approx_tokens(t),
                      meta={"source_type": source_type or "upload"})
            for t in chunk_prose(text)
        ]
    if base:
        for s in specs:
            s.meta = {**base, **s.meta}
    return specs


# --------------------------------------------------------------------------
# ingest (called from the ARQ task)
# --------------------------------------------------------------------------
def _document_content(document: KBDocument) -> tuple[Any, dict[str, Any]]:
    """Extract the raw content + base meta from a document. Uploads/urls keep
    text in meta['text'] or meta['content']; faq/product keep meta['items']."""
    meta = document.meta or {}
    base = {k: v for k, v in meta.items() if k not in ("text", "content", "items")}
    if document.source_type in ("faq", "product"):
        return meta.get("items") or [], base
    return meta.get("text") or meta.get("content") or "", base


async def ingest_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    client: LLMClientProtocol | None = None,
) -> int:
    """Chunk + embed a document into kb_chunks (idempotent re-ingest: replaces
    prior chunks). Sets document.status ready/error. Returns chunk count.
    Runs inside the caller's session; the ARQ task commits."""
    document = await session.get(KBDocument, document_id)
    if document is None:
        return 0
    document.status = "processing"
    document.error = None
    await session.flush()

    # clear any prior chunks for a clean re-ingest
    from sqlalchemy import delete

    await session.execute(delete(KBChunk).where(KBChunk.document_id == document_id))

    content, base_meta = _document_content(document)
    specs = build_chunks(source_type=document.source_type, content=content, base_meta=base_meta)
    if not specs:
        document.status = "ready"
        await session.flush()
        return 0

    try:
        vectors = await embed_texts([s.text for s in specs], client=client)
    except Exception as e:  # noqa: BLE001 — surface embedding failures on the doc
        document.status = "error"
        document.error = f"embedding failed: {e}"[:500]
        await session.flush()
        raise

    for seq, (spec, vec) in enumerate(zip(specs, vectors, strict=True)):
        session.add(
            KBChunk(
                workspace_id=document.workspace_id,
                document_id=document.id,
                seq=seq,
                text=spec.text,
                token_count=spec.token_count,
                embedding=vec,
                meta=spec.meta,
            )
        )
    document.status = "ready"
    await session.flush()
    return len(specs)


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------
_CONDENSE_SYSTEM = (
    "You rewrite a customer's latest message into a single standalone search "
    "query for a knowledge base. Reply with ONLY the query text, no quotes, no "
    "explanation. Keep the customer's language."
)


async def condense_query(
    client: LLMClientProtocol,
    *,
    history: list[str],
    query: str,
) -> str:
    """Fold recent context into a self-contained search query (tier fast).
    With no history the query passes through unchanged."""
    query = (query or "").strip()
    if not query or not history:
        return query
    from py_contracts.llm import LLMMessage

    convo = "\n".join(history[-6:])
    prompt = f"Conversation so far:\n{convo}\n\nLatest message: {query}\n\nStandalone search query:"
    try:
        out = await client.complete(
            tier="fast",
            system=_CONDENSE_SYSTEM,
            messages=[LLMMessage(role="user", content=prompt)],
            max_tokens=128,
            temperature=0.0,
        )
    except Exception:  # noqa: BLE001 — condense is best-effort
        return query
    out = (out or "").strip().splitlines()[0] if out else ""
    return out.strip() or query


@dataclass
class RetrievedChunk:
    id: uuid.UUID
    document_id: uuid.UUID
    text: str
    meta: dict[str, Any]
    score: float

    @property
    def handle(self) -> str | None:
        h = self.meta.get("handle") if self.meta else None
        return str(h) if h else None


@dataclass
class Retrieved:
    chunks: list[RetrievedChunk] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return bool(self.chunks)

    def context_text(self, *, max_chunks: int = 6) -> str:
        blocks = []
        for i, c in enumerate(self.chunks[:max_chunks], start=1):
            blocks.append(f"[{i}] {c.text}")
        return "\n\n".join(blocks)


def rrf_merge(ranked_lists: list[list[uuid.UUID]], *, k: int = RRF_K) -> list[tuple[uuid.UUID, float]]:
    """Reciprocal Rank Fusion: score(id) = Σ 1/(k + rank) over every list the id
    appears in (rank 0-based). Returns ids sorted by descending score. Pure."""
    scores: dict[uuid.UUID, float] = {}
    for ranked in ranked_lists:
        for rank, cid in enumerate(ranked):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))


async def _vector_search(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    collection_ids: list[uuid.UUID],
    qvec: list[float],
    limit: int,
) -> list[uuid.UUID]:
    dist = KBChunk.embedding.cosine_distance(qvec)
    rows = (
        await session.execute(
            select(KBChunk.id)
            .join(KBDocument, KBDocument.id == KBChunk.document_id)
            .where(
                KBChunk.workspace_id == workspace_id,
                KBDocument.collection_id.in_(collection_ids),
                KBChunk.embedding.is_not(None),
            )
            .order_by(dist)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def _lexical_search(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    collection_ids: list[uuid.UUID],
    query: str,
    limit: int,
) -> list[uuid.UUID]:
    if not query.strip():
        return []
    sim = func.similarity(KBChunk.text, query)
    rows = (
        await session.execute(
            select(KBChunk.id)
            .join(KBDocument, KBDocument.id == KBChunk.document_id)
            .where(
                KBChunk.workspace_id == workspace_id,
                KBDocument.collection_id.in_(collection_ids),
                sim > LEXICAL_SIM_FLOOR,
            )
            .order_by(sim.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def retrieve(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    collection_ids: list[uuid.UUID],
    query: str,
    client: LLMClientProtocol | None = None,
    top_k: int = VECTOR_K,
    score_floor: float = SCORE_FLOOR,
) -> Retrieved:
    """Hybrid retrieve: vector top-k + lexical top-k → RRF → floor → hydrate.
    Vector search degrades to lexical-only if embedding the query fails."""
    if not collection_ids:
        return Retrieved()

    vec_ids: list[uuid.UUID] = []
    try:
        qvec = await embed_query(query, client=client)
        vec_ids = await _vector_search(
            session, workspace_id=workspace_id, collection_ids=collection_ids,
            qvec=qvec, limit=top_k,
        )
    except Exception:  # noqa: BLE001 — fall back to lexical if embeddings are down
        vec_ids = []
    lex_ids = await _lexical_search(
        session, workspace_id=workspace_id, collection_ids=collection_ids,
        query=query, limit=LEXICAL_K,
    )

    merged = rrf_merge([lst for lst in (vec_ids, lex_ids) if lst], k=RRF_K)
    kept = [(cid, s) for cid, s in merged if s >= score_floor][:top_k]
    if not kept:
        return Retrieved()

    score_by_id = dict(kept)
    rows = (
        await session.execute(
            select(KBChunk).where(KBChunk.id.in_([cid for cid, _ in kept]))
        )
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    chunks = [
        RetrievedChunk(
            id=cid, document_id=by_id[cid].document_id, text=by_id[cid].text,
            meta=by_id[cid].meta or {}, score=score_by_id[cid],
        )
        for cid, _ in kept
        if cid in by_id
    ]
    return Retrieved(chunks=chunks)


async def product_catalog(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    collection_ids: list[uuid.UUID],
) -> dict[str, dict[str, Any]]:
    """{handle → product meta} for grounding [CARD:handle] against real SKUs
    in the agent's collections (hallucinated handles are dropped)."""
    if not collection_ids:
        return {}
    rows = (
        await session.execute(
            select(KBChunk.meta)
            .join(KBDocument, KBDocument.id == KBChunk.document_id)
            .where(
                KBChunk.workspace_id == workspace_id,
                KBDocument.collection_id.in_(collection_ids),
                KBChunk.meta["source_type"].astext == "product",
            )
        )
    ).scalars().all()
    catalog: dict[str, dict[str, Any]] = {}
    for meta in rows:
        if not meta:
            continue
        handle = meta.get("handle")
        if handle:
            catalog[str(handle)] = meta
    return catalog
