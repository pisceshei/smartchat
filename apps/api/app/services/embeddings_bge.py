"""bge-m3 embedding client (P3 RAG production swap).

The sub2api LLM relay has no embeddings endpoint, so the ``embed`` tier is
served by a self-hosted **bge-m3** HTTP sidecar (1024-dim, matches
``kb_chunks.embedding vector(1024)``; see infra/embed_server.py). This module
speaks to it and exposes the SAME surface as services/embeddings so RAG can
swap transparently::

    embed_texts(texts, *, client=None, batch_size=...) -> list[list[float]]

``client`` is accepted for signature-compatibility and ignored (the sidecar is
addressed via ``EMBED_BASE_URL``). When ``EMBED_BASE_URL`` is unset a clear
"embeddings unavailable" error is raised.

Wire protocol (simple, non-OpenAI): ``POST {base}/embed {"texts": [...]}`` →
``{"embeddings": [[...1024 floats...], ...]}``.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from ..settings import get_settings

EMBED_DIM = 1024
DEFAULT_BATCH = 64


class EmbeddingsUnavailableError(RuntimeError):
    """Raised when no embedding sidecar is configured/reachable."""


def _batched(seq: Sequence[str], n: int) -> list[list[str]]:
    if n <= 0:
        raise ValueError("batch size must be positive")
    return [list(seq[i : i + n]) for i in range(0, len(seq), n)]


def _check_dim(vectors: list[list[float]]) -> None:
    for v in vectors:
        if len(v) != EMBED_DIM:
            raise ValueError(
                f"embedding dimension {len(v)} != expected {EMBED_DIM}; "
                "the bge-m3 sidecar must output 1024 dims"
            )


async def _post_embed(
    base_url: str, texts: list[str], *, timeout_s: float
) -> list[list[float]]:
    url = base_url.rstrip("/") + "/embed"
    async with httpx.AsyncClient(timeout=timeout_s) as http:
        try:
            resp = await http.post(url, json={"texts": texts})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbeddingsUnavailableError(f"bge-m3 sidecar request failed: {e}") from e
        data: dict[str, Any] = resp.json()
    vectors = data.get("embeddings")
    if not isinstance(vectors, list):
        raise EmbeddingsUnavailableError("bge-m3 sidecar returned no 'embeddings' array")
    return [[float(x) for x in vec] for vec in vectors]


async def embed_texts(
    texts: Sequence[str],
    *,
    client: Any | None = None,  # accepted for signature-compat; ignored
    batch_size: int = DEFAULT_BATCH,
) -> list[list[float]]:
    """Embed texts via the bge-m3 sidecar. Returns one 1024-dim vector per input,
    in order. Raises ``EmbeddingsUnavailableError`` when EMBED_BASE_URL is unset."""
    if not texts:
        return []
    settings = get_settings()
    base_url = settings.embed_base_url
    if not base_url:
        raise EmbeddingsUnavailableError(
            "embeddings unavailable: EMBED_BASE_URL not configured"
        )
    out: list[list[float]] = []
    for chunk in _batched(texts, batch_size):
        vectors = await _post_embed(base_url, chunk, timeout_s=settings.embed_timeout_s)
        if len(vectors) != len(chunk):
            raise ValueError(
                f"sidecar returned {len(vectors)} vectors for {len(chunk)} inputs"
            )
        _check_dim(vectors)
        out.extend(vectors)
    return out


async def embed_query(text: str, *, client: Any | None = None) -> list[float]:
    """Embed a single query string → one 1024-dim vector."""
    result = await embed_texts([text])
    return result[0]
