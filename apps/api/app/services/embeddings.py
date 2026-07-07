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


async def embed_texts(
    texts: Sequence[str],
    *,
    client: LLMClientProtocol | None = None,
    batch_size: int = DEFAULT_BATCH,
) -> list[list[float]]:
    """Embed a list of texts, batching requests. Returns one 1024-dim vector per
    input, in order. Uses the injected client or the process default."""
    if not texts:
        return []
    if client is None:
        from .llm_client import get_default_llm

        client = get_default_llm()
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
