"""AI subsystem ARQ tasks (plan 附錄 B.2).

Registered by importing this module from jobs.worker (append-only). Provides:
  - ingest_kb_document_task — chunk + embed a KB document into kb_chunks
  - resume_idle_ai_task     — cron: auto-resume paused_human conversations
  - drain_ai_events_task    — process one batch of the AI consumer group
"""
from __future__ import annotations

import uuid
from typing import Any

from arq import cron

from ..jobs.worker import register_cron, task
from . import agent_runtime, rag


@task
async def ingest_kb_document_task(ctx: dict[str, Any], document_id: str) -> int:
    """Chunk + embed one KB document (idempotent re-ingest). Returns chunks."""
    async with ctx["session_factory"]() as session:
        n = await rag.ingest_document(session, document_id=uuid.UUID(document_id))
        await session.commit()
        return n


@task
async def resume_idle_ai_task(ctx: dict[str, Any]) -> int:
    return await agent_runtime.resume_idle_ai(ctx["session_factory"], ctx["redis"])


@task
async def drain_ai_events_task(ctx: dict[str, Any]) -> int:
    return await agent_runtime.drain_ai_events_once(ctx["session_factory"], ctx["redis"])


# hourly safety-net resume sweep (a dedicated AI process may run more often)
register_cron(cron(resume_idle_ai_task, minute=7, run_at_startup=False))
