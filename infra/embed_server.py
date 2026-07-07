"""bge-m3 embedding sidecar (P3 RAG embed tier).

A tiny FastAPI server wrapping ``BAAI/bge-m3`` via sentence-transformers. It
serves the SmartChat API's embed tier (the sub2api LLM relay has no embeddings
endpoint). Output is 1024-dim dense vectors, L2-normalised (cosine-ready to
match ``kb_chunks.embedding vector(1024)`` + its HNSW cosine index).

Runs CPU-only out of the box (bge-m3 on CPU is ample for KB ingest + query
volumes); set ``EMBED_DEVICE=cuda`` and use a CUDA base image for GPU.

Endpoints
---------
- ``GET  /health`` → {"status": "ok", "model": ..., "dim": 1024}
- ``POST /embed``  → body {"texts": ["...", ...]} → {"embeddings": [[...], ...]}

Env
---
- ``EMBED_MODEL``       default ``BAAI/bge-m3``
- ``EMBED_DEVICE``      default ``cpu``
- ``EMBED_MAX_BATCH``   default ``64``
- ``EMBED_MAX_SEQ_LEN`` default ``512`` (bge-m3 supports up to 8192; 512 keeps
  CPU latency low for chat-KB chunks)
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
DEVICE = os.environ.get("EMBED_DEVICE", "cpu")
MAX_BATCH = int(os.environ.get("EMBED_MAX_BATCH", "64"))
MAX_SEQ_LEN = int(os.environ.get("EMBED_MAX_SEQ_LEN", "512"))
EMBED_DIM = 1024

app = FastAPI(title="smartchat-embed", version="1.0.0")


@lru_cache(maxsize=1)
def _model() -> Any:
    # Imported lazily so the module is importable (for tests/tooling) without
    # torch + sentence-transformers installed.
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    model.max_seq_length = MAX_SEQ_LEN
    return model


class EmbedRequest(BaseModel):
    texts: list[str] = Field(default_factory=list)


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "model": MODEL_NAME, "dim": EMBED_DIM, "device": DEVICE}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest) -> EmbedResponse:
    if not req.texts:
        return EmbedResponse(embeddings=[])
    try:
        vectors = _model().encode(
            req.texts,
            batch_size=MAX_BATCH,
            normalize_embeddings=True,  # cosine-ready
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    except Exception as e:  # noqa: BLE001 — surface as 500 with a clear message
        raise HTTPException(status_code=500, detail=f"embedding failed: {e}") from e
    out = [[float(x) for x in row] for row in vectors]
    if out and len(out[0]) != EMBED_DIM:
        raise HTTPException(
            status_code=500,
            detail=f"model produced dim {len(out[0])} != expected {EMBED_DIM}",
        )
    return EmbedResponse(embeddings=out)
