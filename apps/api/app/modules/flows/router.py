"""流程 (automation) management API (plan 附錄 B.1).

Flow list with 7-day trigger/participant/engagement/completion stats; CRUD;
publish (validate → freeze version → rebuild triggers); duplicate; test-run
(mode=test on the draft graph, sandbox conversation); 數據 drill-down (funnel
from flow_session_steps); categories CRUD; keyword-dict CRUD; template gallery
「使用」 = deep-copy into a new draft.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, current_member, require_permission
from ...flows.graph_schema import parse_graph, validate_graph
from ...models.flows import (
    Flow,
    FlowCategory,
    FlowSession,
    FlowTemplate,
    FlowVersion,
    KeywordDict,
    KeywordDictItem,
)
from ...services.messaging import publish_realtime
from ...services.redis_client import get_redis
from . import service

router = APIRouter(prefix="/api/v1", tags=["flows"])


# ==========================================================================
# schemas
# ==========================================================================
class FlowIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    channel_type: str = Field(default="widget", max_length=24)
    description: str | None = None
    category_id: uuid.UUID | None = None
    priority: int = 100
    draft_graph: dict[str, Any] | None = None
    template_slug: str | None = None


class FlowPatch(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    channel_type: str | None = Field(default=None, max_length=24)
    description: str | None = None
    category_id: uuid.UUID | None = None
    priority: int | None = None
    enabled: bool | None = None
    draft_graph: dict[str, Any] | None = None


class StatsOut(BaseModel):
    triggered_sessions: int
    triggered_users: int
    engaged_users: int
    completed_sessions: int
    engagement_rate: float
    completion_rate: float


class FlowOut(BaseModel):
    id: uuid.UUID
    channel_type: str
    name: str
    description: str | None
    category_id: uuid.UUID | None
    enabled: bool
    priority: int
    published_version_id: uuid.UUID | None
    has_draft_changes: bool
    updated_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class FlowListItem(BaseModel):
    flow: FlowOut
    stats: StatsOut


class FlowDetailOut(FlowOut):
    draft_graph: dict[str, Any]


# ==========================================================================
# helpers
# ==========================================================================
async def _get_flow(session: AsyncSession, workspace_id: uuid.UUID, flow_id: uuid.UUID) -> Flow:
    flow = await session.get(Flow, flow_id)
    if flow is None or flow.workspace_id != workspace_id:
        raise HTTPException(404, detail="flow not found")
    return flow


def _has_draft_changes(flow: Flow) -> bool:
    """Draft differs from the published version (or never published)."""
    return flow.published_version_id is None or bool(flow.draft_graph)


def _flow_out(flow: Flow) -> FlowOut:
    return FlowOut(
        id=flow.id,
        channel_type=flow.channel_type,
        name=flow.name,
        description=flow.description,
        category_id=flow.category_id,
        enabled=flow.enabled,
        priority=flow.priority,
        published_version_id=flow.published_version_id,
        has_draft_changes=flow.published_version_id is None,
        updated_at=flow.updated_at,
        created_at=flow.created_at,
    )


def _workspace_today(member: MemberContext):
    tz = (member.workspace.settings or {}).get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz)).date(), tz
    except Exception:  # noqa: BLE001
        return datetime.now(UTC).date(), "UTC"


# ==========================================================================
# flow CRUD + list
# ==========================================================================
@router.get("/flows", response_model=list[FlowListItem])
async def list_flows(
    category_id: uuid.UUID | None = None,
    channel_type: str | None = None,
    enabled: bool | None = None,
    q: str | None = None,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[FlowListItem]:
    query = select(Flow).where(Flow.workspace_id == member.workspace_id)
    if category_id is not None:
        query = query.where(Flow.category_id == category_id)
    if channel_type:
        query = query.where(Flow.channel_type == channel_type)
    if enabled is not None:
        query = query.where(Flow.enabled.is_(enabled))
    if q:
        query = query.where(Flow.name.ilike(f"%{q}%"))
    flows = (await session.execute(query.order_by(Flow.updated_at.desc()))).scalars().all()

    today, _tz = _workspace_today(member)
    stats = await service.stats_for_flows(
        session, workspace_id=member.workspace_id, flow_ids=[f.id for f in flows], today=today
    )
    out: list[FlowListItem] = []
    for f in flows:
        st = stats.get(f.id, service.FlowStats())
        out.append(
            FlowListItem(
                flow=_flow_out(f),
                stats=StatsOut(
                    triggered_sessions=st.triggered_sessions,
                    triggered_users=st.triggered_users,
                    engaged_users=st.engaged_users,
                    completed_sessions=st.completed_sessions,
                    engagement_rate=st.engagement_rate,
                    completion_rate=st.completion_rate,
                ),
            )
        )
    return out


@router.post("/flows", response_model=FlowDetailOut, status_code=201)
async def create_flow(
    body: FlowIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> FlowDetailOut:
    draft = body.draft_graph or {"schema_version": 1, "nodes": [], "edges": []}
    if body.template_slug:
        tpl = (
            await session.execute(
                select(FlowTemplate).where(
                    FlowTemplate.slug == body.template_slug, FlowTemplate.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if tpl is None:
            raise HTTPException(404, detail="template not found")
        draft = dict(tpl.graph or draft)
    flow = Flow(
        workspace_id=member.workspace_id,
        channel_type=body.channel_type,
        name=body.name,
        description=body.description,
        category_id=body.category_id,
        priority=body.priority,
        draft_graph=draft,
        enabled=False,
        updated_by_member_id=member.member_id,
    )
    session.add(flow)
    await session.commit()
    return FlowDetailOut(**_flow_out(flow).model_dump(), draft_graph=flow.draft_graph or {})


@router.get("/flows/{flow_id}", response_model=FlowDetailOut)
async def get_flow(
    flow_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> FlowDetailOut:
    flow = await _get_flow(session, member.workspace_id, flow_id)
    return FlowDetailOut(**_flow_out(flow).model_dump(), draft_graph=flow.draft_graph or {})


@router.patch("/flows/{flow_id}", response_model=FlowDetailOut)
async def update_flow(
    flow_id: uuid.UUID,
    body: FlowPatch,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> FlowDetailOut:
    flow = await _get_flow(session, member.workspace_id, flow_id)
    data = body.model_dump(exclude_unset=True)
    flags_changed = False
    for key in ("name", "channel_type", "description", "category_id", "priority", "draft_graph"):
        if key in data:
            setattr(flow, key, data[key])
            if key in ("channel_type", "priority"):
                flags_changed = True
    if "enabled" in data:
        flow.enabled = data["enabled"]
        flags_changed = True
    flow.updated_by_member_id = member.member_id
    if flags_changed:
        await service.sync_trigger_flags(session, flow)
    await session.commit()
    return FlowDetailOut(**_flow_out(flow).model_dump(), draft_graph=flow.draft_graph or {})


@router.delete("/flows/{flow_id}", status_code=204)
async def delete_flow(
    flow_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    flow = await _get_flow(session, member.workspace_id, flow_id)
    await session.delete(flow)
    await session.commit()


# ==========================================================================
# validate / publish / duplicate / test-run
# ==========================================================================
@router.post("/flows/{flow_id}/validate")
async def validate_flow(
    flow_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    flow = await _get_flow(session, member.workspace_id, flow_id)
    try:
        graph = parse_graph(flow.draft_graph or {})
    except Exception as e:  # noqa: BLE001
        return {"valid": False, "errors": [f"graph parse error: {e}"]}
    errors = validate_graph(graph, channel_type=flow.channel_type)
    return {"valid": not errors, "errors": errors}


@router.post("/flows/{flow_id}/publish")
async def publish_flow(
    flow_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    flow = await _get_flow(session, member.workspace_id, flow_id)
    try:
        result = await service.publish_flow(
            session, workspace_id=member.workspace_id, flow=flow, member_id=member.member_id
        )
    except service.PublishError as e:
        raise HTTPException(422, detail={"code": "invalid_graph", "errors": e.errors}) from e
    await session.commit()
    return {
        "version_id": str(result.version_id),
        "version_no": result.version_no,
        "trigger_count": result.trigger_count,
    }


@router.post("/flows/{flow_id}/duplicate", response_model=FlowDetailOut, status_code=201)
async def duplicate_flow(
    flow_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> FlowDetailOut:
    src = await _get_flow(session, member.workspace_id, flow_id)
    dup = await service.duplicate_flow(
        session, workspace_id=member.workspace_id, source=src, member_id=member.member_id
    )
    await session.commit()
    return FlowDetailOut(**_flow_out(dup).model_dump(), draft_graph=dup.draft_graph or {})


@router.post("/flows/{flow_id}/test-run")
async def test_run_flow(
    flow_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    flow = await _get_flow(session, member.workspace_id, flow_id)
    _, tz = _workspace_today(member)
    try:
        fs, events = await service.test_run(
            session, get_redis(), workspace_id=member.workspace_id, flow=flow, workspace_tz=tz
        )
    except service.PublishError as e:
        raise HTTPException(422, detail={"code": "invalid_graph", "errors": e.errors}) from e
    await session.commit()
    await publish_realtime(events)
    return {
        "session_id": str(fs.id),
        "status": fs.status,
        "conversation_id": str(fs.conversation_id) if fs.conversation_id else None,
        "step_count": fs.step_count,
    }


# ==========================================================================
# stats / funnel / versions / sessions drill-down
# ==========================================================================
@router.get("/flows/{flow_id}/stats")
async def flow_stats(
    flow_id: uuid.UUID,
    days: int = Query(default=7, ge=1, le=90),
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_flow(session, member.workspace_id, flow_id)
    today, _tz = _workspace_today(member)
    stats = await service.stats_for_flows(
        session, workspace_id=member.workspace_id, flow_ids=[flow_id], days=days, today=today
    )
    st = stats.get(flow_id, service.FlowStats())
    funnel = await service.flow_funnel(
        session, workspace_id=member.workspace_id, flow_id=flow_id, days=days
    )
    return {
        "stats": {
            "triggered_sessions": st.triggered_sessions,
            "triggered_users": st.triggered_users,
            "engaged_users": st.engaged_users,
            "completed_sessions": st.completed_sessions,
            "engagement_rate": st.engagement_rate,
            "completion_rate": st.completion_rate,
        },
        "funnel": funnel,
    }


@router.get("/flows/{flow_id}/versions")
async def list_versions(
    flow_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _get_flow(session, member.workspace_id, flow_id)
    rows = (
        await session.execute(
            select(FlowVersion)
            .where(FlowVersion.flow_id == flow_id, FlowVersion.version_no > 0)
            .order_by(FlowVersion.version_no.desc())
        )
    ).scalars().all()
    return [
        {"id": str(v.id), "version_no": v.version_no, "published_at": v.published_at.isoformat()}
        for v in rows
    ]


@router.get("/flows/{flow_id}/sessions")
async def list_flow_sessions(
    flow_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _get_flow(session, member.workspace_id, flow_id)
    rows = (
        await session.execute(
            select(FlowSession)
            .where(
                FlowSession.workspace_id == member.workspace_id,
                FlowSession.flow_id == flow_id,
                FlowSession.mode == "live",
            )
            .order_by(FlowSession.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": str(s.id),
            "conversation_id": str(s.conversation_id) if s.conversation_id else None,
            "status": s.status,
            "current_node_id": s.current_node_id,
            "step_count": s.step_count,
            "engaged": s.engaged,
            "created_at": s.created_at.isoformat(),
            "ended_at": s.ended_at.isoformat() if s.ended_at else None,
            "end_reason": s.end_reason,
        }
        for s in rows
    ]


# ==========================================================================
# categories CRUD
# ==========================================================================
class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=96)
    sort_order: int = 0


@router.get("/flow-categories")
async def list_categories(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(FlowCategory)
            .where(FlowCategory.workspace_id == member.workspace_id)
            .order_by(FlowCategory.sort_order, FlowCategory.name)
        )
    ).scalars().all()
    return [{"id": str(c.id), "name": c.name, "sort_order": c.sort_order} for c in rows]


@router.post("/flow-categories", status_code=201)
async def create_category(
    body: CategoryIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cat = FlowCategory(
        workspace_id=member.workspace_id, name=body.name, sort_order=body.sort_order
    )
    session.add(cat)
    try:
        await session.commit()
    except Exception as e:  # noqa: BLE001 — unique (ws, name)
        await session.rollback()
        raise HTTPException(409, detail="category name already exists") from e
    return {"id": str(cat.id), "name": cat.name, "sort_order": cat.sort_order}


@router.patch("/flow-categories/{category_id}")
async def update_category(
    category_id: uuid.UUID,
    body: CategoryIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cat = await session.get(FlowCategory, category_id)
    if cat is None or cat.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="category not found")
    cat.name = body.name
    cat.sort_order = body.sort_order
    await session.commit()
    return {"id": str(cat.id), "name": cat.name, "sort_order": cat.sort_order}


@router.delete("/flow-categories/{category_id}", status_code=204)
async def delete_category(
    category_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    cat = await session.get(FlowCategory, category_id)
    if cat is None or cat.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="category not found")
    await session.delete(cat)
    await session.commit()


# ==========================================================================
# keyword dicts CRUD (詞庫)
# ==========================================================================
class KeywordDictIn(BaseModel):
    name: str = Field(min_length=1, max_length=96)
    description: str | None = None


class KeywordItemsIn(BaseModel):
    keywords: list[str] = Field(default_factory=list)


@router.get("/flow-keyword-dicts")
async def list_keyword_dicts(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                KeywordDict, func.count(KeywordDictItem.id)
            )
            .outerjoin(KeywordDictItem, KeywordDictItem.dict_id == KeywordDict.id)
            .where(KeywordDict.workspace_id == member.workspace_id)
            .group_by(KeywordDict.id)
            .order_by(KeywordDict.name)
        )
    ).all()
    return [
        {"id": str(d.id), "name": d.name, "description": d.description, "item_count": int(cnt)}
        for d, cnt in rows
    ]


@router.post("/flow-keyword-dicts", status_code=201)
async def create_keyword_dict(
    body: KeywordDictIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    d = KeywordDict(
        workspace_id=member.workspace_id, name=body.name, description=body.description
    )
    session.add(d)
    try:
        await session.commit()
    except Exception as e:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(409, detail="dict name already exists") from e
    return {"id": str(d.id), "name": d.name, "description": d.description, "item_count": 0}


@router.get("/flow-keyword-dicts/{dict_id}/items")
async def get_keyword_items(
    dict_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    d = await session.get(KeywordDict, dict_id)
    if d is None or d.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="dict not found")
    rows = (
        await session.execute(
            select(KeywordDictItem.keyword).where(KeywordDictItem.dict_id == dict_id)
        )
    ).scalars().all()
    return {"id": str(dict_id), "keywords": list(rows)}


@router.put("/flow-keyword-dicts/{dict_id}/items")
async def set_keyword_items(
    dict_id: uuid.UUID,
    body: KeywordItemsIn,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    d = await session.get(KeywordDict, dict_id)
    if d is None or d.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="dict not found")
    await session.execute(delete(KeywordDictItem).where(KeywordDictItem.dict_id == dict_id))
    seen: set[str] = set()
    for kw in body.keywords:
        kw = (kw or "").strip()
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        session.add(
            KeywordDictItem(
                workspace_id=member.workspace_id, dict_id=dict_id, keyword=kw[:255]
            )
        )
    await session.commit()
    return {"id": str(dict_id), "keywords": sorted({k for k in seen})}


@router.delete("/flow-keyword-dicts/{dict_id}", status_code=204)
async def delete_keyword_dict(
    dict_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("flows.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    d = await session.get(KeywordDict, dict_id)
    if d is None or d.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="dict not found")
    await session.delete(d)
    await session.commit()


# ==========================================================================
# template gallery
# ==========================================================================
@router.get("/flow-templates")
async def list_templates(
    channel_type: str | None = None,
    category: str | None = None,
    _member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(FlowTemplate).where(FlowTemplate.is_active.is_(True))
    if channel_type:
        query = query.where(FlowTemplate.channel_type == channel_type)
    if category:
        query = query.where(FlowTemplate.category == category)
    rows = (await session.execute(query.order_by(FlowTemplate.sort_order))).scalars().all()
    return [
        {
            "id": t.slug,  # frontend identifies templates by slug
            "slug": t.slug,
            "channel_type": t.channel_type,
            "category": t.category,
            "name": t.name,
            "description": t.description,
            "preview": t.preview,
        }
        for t in rows
    ]
