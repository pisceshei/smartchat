"""AI management API (/api/v1/ai).

AI members (CRUD, paired member+config), KB collections + documents (+ ingest),
intents (CRUD), point prices + balance. Config surfaces the P2 AI subsystem;
runtime lives in app.ai.*.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, current_member, require_permission
from ...models.ai import (
    AIAgent,
    AIPointPrice,
    Intent,
    KBChunk,
    KBCollection,
    KBDocument,
)
from ...models.members import WorkspaceMember
from ...services import points
from . import service

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])

Tier = Literal["fast", "smart"]
Mode = Literal["builtin", "external"]
SourceType = Literal["upload", "faq", "product", "url"]


# ==========================================================================
# schemas
# ==========================================================================
class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    persona: dict[str, Any] = Field(default_factory=dict)
    model_tier: Tier = "smart"
    kb_collection_ids: list[uuid.UUID] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    monthly_msg_quota: int = Field(default=0, ge=0)
    mode: Mode = "builtin"
    external: dict[str, Any] = Field(default_factory=dict)
    escalation_rules: dict[str, Any] = Field(default_factory=dict)
    max_concurrent: int = Field(default=0, ge=0)
    role_id: uuid.UUID | None = None


class AgentUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    persona: dict[str, Any] | None = None
    model_tier: Tier | None = None
    kb_collection_ids: list[uuid.UUID] | None = None
    skills: list[str] | None = None
    monthly_msg_quota: int | None = Field(default=None, ge=0)
    mode: Mode | None = None
    external: dict[str, Any] | None = None
    escalation_rules: dict[str, Any] | None = None
    max_concurrent: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


class AgentOut(BaseModel):
    id: uuid.UUID
    member_id: uuid.UUID
    name: str
    persona: dict[str, Any]
    model_tier: str
    kb_collection_ids: list[Any]
    skills: list[Any]
    monthly_msg_quota: int
    mode: str
    external: dict[str, Any]
    escalation_rules: dict[str, Any]
    enabled: bool

    model_config = {"from_attributes": True}


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    document_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    source_type: SourceType = "upload"
    source_ref: str | None = None
    # prose/upload/url → text; faq/product → items[]
    text: str | None = None
    items: list[dict[str, Any]] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class DocumentOut(BaseModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    title: str
    source_type: str
    source_ref: str | None
    status: str
    error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChunkOut(BaseModel):
    id: uuid.UUID
    seq: int
    text: str
    token_count: int
    meta: dict[str, Any]

    model_config = {"from_attributes": True}


class IntentIn(BaseModel):
    name: str = Field(min_length=1, max_length=96)
    description: str | None = None
    examples: list[str] = Field(default_factory=list)
    enabled: bool = True


class IntentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    examples: list[Any]
    enabled: bool

    model_config = {"from_attributes": True}


class PointPriceOut(BaseModel):
    feature_key: str
    points: int
    description: str | None

    model_config = {"from_attributes": True}


# ==========================================================================
# AI members
# ==========================================================================
async def _get_agent(session: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID) -> AIAgent:
    agent = await session.get(AIAgent, agent_id)
    if agent is None or agent.workspace_id != workspace_id:
        raise HTTPException(404, detail="ai agent not found")
    return agent


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[AgentOut]:
    rows = (
        await session.execute(
            select(AIAgent).where(AIAgent.workspace_id == member.workspace_id).order_by(AIAgent.created_at)
        )
    ).scalars().all()
    return [AgentOut.model_validate(a) for a in rows]


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    body: AgentIn,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    await _validate_collections(session, member.workspace_id, body.kb_collection_ids)
    _, agent = await service.create_ai_member(
        session,
        workspace_id=member.workspace_id,
        name=body.name,
        persona=body.persona,
        model_tier=body.model_tier,
        kb_collection_ids=[str(c) for c in body.kb_collection_ids],
        skills=body.skills,
        monthly_msg_quota=body.monthly_msg_quota,
        mode=body.mode,
        external=body.external,
        escalation_rules=body.escalation_rules,
        max_concurrent=body.max_concurrent,
        role_id=body.role_id,
    )
    await session.commit()
    return AgentOut.model_validate(agent)


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    return AgentOut.model_validate(await _get_agent(session, member.workspace_id, agent_id))


@router.patch("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentUpdateIn,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    agent = await _get_agent(session, member.workspace_id, agent_id)
    if body.kb_collection_ids is not None:
        await _validate_collections(session, member.workspace_id, body.kb_collection_ids)
        agent.kb_collection_ids = [str(c) for c in body.kb_collection_ids]
    for field_name in ("name", "persona", "model_tier", "skills", "monthly_msg_quota",
                       "mode", "external", "escalation_rules", "enabled"):
        val = getattr(body, field_name)
        if val is not None:
            setattr(agent, field_name, val)
    # keep the paired member row in sync
    ai_member = await session.get(WorkspaceMember, agent.member_id)
    if ai_member is not None:
        if body.name is not None:
            ai_member.display_name = body.name
        if body.max_concurrent is not None:
            ai_member.max_concurrent = body.max_concurrent
        if body.enabled is not None:
            ai_member.status = "active" if body.enabled else "disabled"
        cfg = dict(ai_member.ai_config or {})
        if body.skills is not None:
            cfg["skills"] = body.skills
        if body.monthly_msg_quota is not None:
            cfg["monthly_msg_quota"] = body.monthly_msg_quota
        ai_member.ai_config = cfg
    await session.commit()
    return AgentOut.model_validate(agent)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _get_agent(session, member.workspace_id, agent_id)
    member_id = agent.member_id
    await session.delete(agent)
    ai_member = await session.get(WorkspaceMember, member_id)
    if ai_member is not None:
        await session.delete(ai_member)  # cascades open assignments via SET NULL
    await session.commit()


# ==========================================================================
# KB collections
# ==========================================================================
async def _validate_collections(
    session: AsyncSession, workspace_id: uuid.UUID, ids: list[uuid.UUID]
) -> None:
    if not ids:
        return
    found = set(
        (
            await session.execute(
                select(KBCollection.id).where(
                    KBCollection.workspace_id == workspace_id, KBCollection.id.in_(ids)
                )
            )
        ).scalars()
    )
    missing = set(ids) - found
    if missing:
        raise HTTPException(422, detail={"code": "unknown_collections",
                                         "ids": [str(m) for m in missing]})


async def _get_collection(session: AsyncSession, workspace_id: uuid.UUID, cid: uuid.UUID) -> KBCollection:
    col = await session.get(KBCollection, cid)
    if col is None or col.workspace_id != workspace_id:
        raise HTTPException(404, detail="collection not found")
    return col


@router.get("/kb/collections", response_model=list[CollectionOut])
async def list_collections(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[CollectionOut]:
    rows = (
        await session.execute(
            select(KBCollection)
            .where(KBCollection.workspace_id == member.workspace_id)
            .order_by(KBCollection.created_at)
        )
    ).scalars().all()
    counts = dict(
        (
            await session.execute(
                select(KBDocument.collection_id, func.count())
                .where(KBDocument.workspace_id == member.workspace_id)
                .group_by(KBDocument.collection_id)
            )
        ).all()
    )
    out: list[CollectionOut] = []
    for c in rows:
        item = CollectionOut.model_validate(c)
        item.document_count = int(counts.get(c.id, 0))
        out.append(item)
    return out


@router.post("/kb/collections", response_model=CollectionOut, status_code=201)
async def create_collection(
    body: CollectionIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    col = KBCollection(workspace_id=member.workspace_id, name=body.name, description=body.description)
    session.add(col)
    await session.commit()
    return CollectionOut.model_validate(col)


@router.patch("/kb/collections/{collection_id}", response_model=CollectionOut)
async def update_collection(
    collection_id: uuid.UUID,
    body: CollectionIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> CollectionOut:
    col = await _get_collection(session, member.workspace_id, collection_id)
    col.name = body.name
    col.description = body.description
    await session.commit()
    return CollectionOut.model_validate(col)


@router.delete("/kb/collections/{collection_id}", status_code=204)
async def delete_collection(
    collection_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    col = await _get_collection(session, member.workspace_id, collection_id)
    await session.delete(col)  # documents + chunks cascade
    await session.commit()


# ==========================================================================
# KB documents + ingest
# ==========================================================================
def _document_meta(body: DocumentIn) -> dict[str, Any]:
    meta = dict(body.meta or {})
    if body.source_type in ("faq", "product"):
        meta["items"] = body.items or []
    else:
        meta["text"] = body.text or ""
    return meta


@router.get("/kb/collections/{collection_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    collection_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[DocumentOut]:
    await _get_collection(session, member.workspace_id, collection_id)
    rows = (
        await session.execute(
            select(KBDocument)
            .where(
                KBDocument.workspace_id == member.workspace_id,
                KBDocument.collection_id == collection_id,
            )
            .order_by(KBDocument.created_at.desc())
        )
    ).scalars().all()
    return [DocumentOut.model_validate(d) for d in rows]


@router.post("/kb/collections/{collection_id}/documents", response_model=DocumentOut, status_code=201)
async def create_document(
    collection_id: uuid.UUID,
    body: DocumentIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    """Create a KB document and enqueue chunk+embed ingest (status pending →
    processing → ready/error). faq/product carry items[]; others carry text."""
    await _get_collection(session, member.workspace_id, collection_id)
    doc = KBDocument(
        workspace_id=member.workspace_id,
        collection_id=collection_id,
        source_type=body.source_type,
        title=body.title,
        source_ref=body.source_ref,
        status="pending",
        meta=_document_meta(body),
    )
    session.add(doc)
    await session.commit()
    await service.enqueue_ingest(doc.id)
    return DocumentOut.model_validate(doc)


@router.post("/kb/documents/{document_id}/reingest", response_model=DocumentOut)
async def reingest_document(
    document_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> DocumentOut:
    doc = await session.get(KBDocument, document_id)
    if doc is None or doc.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="document not found")
    doc.status = "pending"
    doc.error = None
    await session.commit()
    await service.enqueue_ingest(doc.id)
    return DocumentOut.model_validate(doc)


@router.delete("/kb/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    doc = await session.get(KBDocument, document_id)
    if doc is None or doc.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="document not found")
    await session.delete(doc)  # chunks cascade
    await session.commit()


@router.get("/kb/documents/{document_id}/chunks", response_model=list[ChunkOut])
async def list_chunks(
    document_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[ChunkOut]:
    doc = await session.get(KBDocument, document_id)
    if doc is None or doc.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="document not found")
    rows = (
        await session.execute(
            select(KBChunk).where(KBChunk.document_id == document_id).order_by(KBChunk.seq)
        )
    ).scalars().all()
    return [ChunkOut.model_validate(c) for c in rows]


# ==========================================================================
# intents
# ==========================================================================
async def _get_intent(session: AsyncSession, workspace_id: uuid.UUID, intent_id: uuid.UUID) -> Intent:
    row = await session.get(Intent, intent_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(404, detail="intent not found")
    return row


@router.get("/intents", response_model=list[IntentOut])
async def list_intents(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[IntentOut]:
    rows = (
        await session.execute(
            select(Intent).where(Intent.workspace_id == member.workspace_id).order_by(Intent.created_at)
        )
    ).scalars().all()
    return [IntentOut.model_validate(i) for i in rows]


@router.post("/intents", response_model=IntentOut, status_code=201)
async def create_intent(
    body: IntentIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> IntentOut:
    dup = (
        await session.execute(
            select(Intent.id).where(
                Intent.workspace_id == member.workspace_id, Intent.name == body.name
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, detail="intent name already exists")
    row = Intent(
        workspace_id=member.workspace_id,
        name=body.name,
        description=body.description,
        examples=body.examples,
        enabled=body.enabled,
    )
    session.add(row)
    await session.commit()
    return IntentOut.model_validate(row)


@router.patch("/intents/{intent_id}", response_model=IntentOut)
async def update_intent(
    intent_id: uuid.UUID,
    body: IntentIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> IntentOut:
    row = await _get_intent(session, member.workspace_id, intent_id)
    row.name = body.name
    row.description = body.description
    row.examples = body.examples
    row.enabled = body.enabled
    await session.commit()
    return IntentOut.model_validate(row)


@router.delete("/intents/{intent_id}", status_code=204)
async def delete_intent(
    intent_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await _get_intent(session, member.workspace_id, intent_id)
    await session.delete(row)
    await session.commit()


# ==========================================================================
# point prices + balance
# ==========================================================================
@router.get("/point-prices", response_model=list[PointPriceOut])
async def list_point_prices(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[PointPriceOut]:
    rows = (
        await session.execute(select(AIPointPrice).order_by(AIPointPrice.feature_key))
    ).scalars().all()
    return [PointPriceOut.model_validate(p) for p in rows]


@router.get("/points/balance")
async def points_balance(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    bal = await points.load_balance(session, member.workspace_id)
    return {"balance": int(bal)}
