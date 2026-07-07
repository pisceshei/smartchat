"""Embedding helper (plan B.2 KB+RAG).

Wraps the active LLM client's ``embed()`` with batching and a hard dimension
check — kb_chunks.embedding is ``vector(1024)`` so a model returning a different
width must fail loudly (silent pad/truncate would corrupt cosine similarity).
The embed model + endpoint come from the LLMProfile (Settings), so the sub2api
relay is the single point of configuration.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING

EMBED_DIM = 1024
DEFAULT_BATCH = 64

if TYPE_CHECKING:
    from .llm_client import LLMClientProtocol


def batched(seq: Sequence[str], n: int) -> Iterator[list[str]]:
    """Yield successive n-sized batches."""
    if n <= 0:
        raise ValueError("batch size must be positive")
    for i in range(0, len(seq), n):
        yield list(seq[i : i + n])


def _check_dim(vectors: list[list[float]]) -> None:
    for v in vectors:
        if len(v) != EMBED_DIM:
            raise ValueError(
                f"embedding dimension {len(v)} != expected {EMBED_DIM}; "
                "configure the embed model to output 1024 dims"
            )


def _resolve_default_embed_client() -> LLMClientProtocol | None:
    """Pick the embedding backend when no explicit client is passed.

    Priority: an explicitly injected default (tests / DI — a client that is NOT
    the production ``LLMClient``, e.g. a FakeLLM) wins; otherwise, when
    ``EMBED_BASE_URL`` is set the caller should route to the bge-m3 sidecar
    (signalled by returning ``None``); otherwise fall back to the Settings-built
    default client. The sub2api relay has no embeddings endpoint, so in
    production the bge sidecar path is the real one."""
    from py_contracts.llm import LLMClient

    from . import llm_client as _lc

    injected = _lc._default
    if injected is not None and not isinstance(injected, LLMClient):
        return injected  # test / dependency-injected fake wins
    from ..settings import get_settings

    if get_settings().embed_base_url:
        return None  # → delegate to the bge-m3 sidecar
    return _lc.get_default_llm()


async def embed_texts(
    texts: Sequence[str],
    *,
    client: LLMClientProtocol | None = None,
    batch_size: int = DEFAULT_BATCH,
) -> list[list[float]]:
    """Embed a list of texts, batching requests. Returns one 1024-dim vector per
    input, in order. Uses the injected client, else an injected default, else the
    bge-m3 sidecar (EMBED_BASE_URL), else the Settings default LLM client."""
    if not texts:
        return []
    if client is None:
        client = _resolve_default_embed_client()
        if client is None:  # production RAG swap: embed tier → bge-m3 sidecar
            from . import embeddings_bge

            return await embeddings_bge.embed_texts(texts, batch_size=batch_size)
    out: list[list[float]] = []
    for chunk in batched(texts, batch_size):
        vectors = await client.embed(chunk)
        if len(vectors) != len(chunk):
            raise ValueError(
                f"embed() returned {len(vectors)} vectors for {len(chunk)} inputs"
            )
        _check_dim(vectors)
        out.extend(vectors)
    return out


async def embed_query(
    text: str,
    *,
    client: LLMClientProtocol | None = None,
) -> list[float]:
    """Embed a single query string → one 1024-dim vector."""
    result = await embed_texts([text], client=client)
    return result[0]
