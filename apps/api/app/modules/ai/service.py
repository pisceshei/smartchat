"""AI management service (plan 附錄 B.2).

AI member creation pairs a workspace_members(member_type=ai_agent) row with an
ai_agents config row in one transaction so conversations.assignee_member_id can
point at the AI exactly like a human. KB document ingest is enqueued onto ARQ
(ingest_kb_document_task) after commit.
"""
from __future__ import annotations

import uuid
from typing import Any

from arq.connections import RedisSettings, create_pool
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.ai import AIAgent
from ...models.members import WorkspaceMember
from ...settings import get_settings

INGEST_JOB = "ingest_kb_document_task"

_arq_pool: Any = None


async def _get_arq_pool() -> Any:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _arq_pool


async def enqueue_ingest(document_id: uuid.UUID | str) -> bool:
    """Enqueue a KB ingest job (call after commit). Best-effort — returns False
    if the queue is unreachable (the document simply stays 'pending')."""
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job(INGEST_JOB, str(document_id))
        return True
    except Exception:  # noqa: BLE001 — enqueue failure must not fail the request
        return False


async def create_ai_member(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    name: str,
    persona: dict[str, Any],
    model_tier: str,
    kb_collection_ids: list[str],
    skills: list[str],
    monthly_msg_quota: int,
    mode: str,
    external: dict[str, Any],
    escalation_rules: dict[str, Any],
    max_concurrent: int,
    role_id: uuid.UUID | None,
) -> tuple[WorkspaceMember, AIAgent]:
    """Create the member + config pair (caller commits). ai_config mirrors the
    key persona/skills/quota so P1 routing (_gather_ai_candidates reads
    ai_config.receive_enabled) keeps working."""
    # 轉人工 keywords default only at create time — tenant edits (including an
    # intentionally emptied list via PATCH) are never overwritten afterwards
    if not escalation_rules.get("keywords"):
        escalation_rules = {**escalation_rules, "keywords": ["真人", "人工", "human"]}
    member = WorkspaceMember(
        workspace_id=workspace_id,
        member_type="ai_agent",
        role_id=role_id,
        display_name=name,
        status="active",
        max_concurrent=max_concurrent,
        ai_config={"receive_enabled": True, "skills": skills, "monthly_msg_quota": monthly_msg_quota},
    )
    session.add(member)
    await session.flush()  # assign member.id

    agent = AIAgent(
        workspace_id=workspace_id,
        member_id=member.id,
        name=name,
        persona=persona,
        model_tier=model_tier,
        kb_collection_ids=kb_collection_ids,
        skills=skills,
        monthly_msg_quota=monthly_msg_quota,
        mode=mode,
        external=external,
        escalation_rules=escalation_rules,
        enabled=True,
    )
    session.add(agent)
    await session.flush()
    return member, agent
