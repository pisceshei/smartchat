"""P2 AI subsystem (plan 附錄 B.2).

Modules:
  points_enforce — per-feature points metering + hard-stop behaviors
  rag            — KB chunking + pgvector/pg_trgm retrieval (RRF merge)
  intent         — one-shot intent classification (cached, 1 point/uncached)
  translation    — engine adapters (llm/google/deepl) + cache + metering
  assist         — stateless composer ops (rewrite/expand/…), SSE-streamed
  agent_runtime  — AI member reply loop (guards → context → LLM → markers →
                   send), handoff, managed state machine, external webhook

The LLM boundary is always the injectable services.llm_client.LLMClientProtocol
(FakeLLM in tests — never a real endpoint).
"""
from __future__ import annotations
