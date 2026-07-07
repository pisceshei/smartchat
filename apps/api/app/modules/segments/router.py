"""自訂受眾 CRUD + estimate (plan B.3 / route contract)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, require_permission
from ...models.marketing import Segment
from . import service as svc

router = APIRouter(prefix="/api/v1/segments", tags=["segments"])


class SegmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    mode: Literal["dynamic", "static"] = "dynamic"
    definition: dict[str, Any] = Field(default_factory=dict)


class SegmentPatch(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    definition: dict[str, Any] | None = None


class SegmentOut(BaseModel):
    id: uuid.UUID
    name: str
    mode: str
    definition: dict[str, Any]
    count: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EstimateIn(BaseModel):
    definition: dict[str, Any] = Field(default_factory=dict)


def _out(row: Segment) -> SegmentOut:
    return SegmentOut(
        id=row.id, name=row.name, mode=row.mode, definition=row.definition or {},
        count=row.count_cache, created_at=row.created_at,
    )


async def _get(session: AsyncSession, workspace_id: uuid.UUID, segment_id: uuid.UUID) -> Segment:
    row = await session.get(Segment, segment_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(404, detail="segment not found")
    return row


@router.get("", response_model=list[SegmentOut])
async def list_segments(
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[SegmentOut]:
    rows = (
        await session.execute(
            select(Segment).where(Segment.workspace_id == member.workspace_id)
            .order_by(Segment.created_at.desc())
        )
    ).scalars().all()
    return [_out(r) for r in rows]


@router.post("/estimate")
async def estimate(
    body: EstimateIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    try:
        count = await svc.estimate_count(session, member.workspace_id, body.definition)
    except svc.SegmentDefinitionError as e:
        raise HTTPException(422, detail={"code": "invalid_definition", "error": str(e)}) from e
    except svc.EstimateTimeout as e:
        raise HTTPException(422, detail={"code": "estimate_timeout",
                                        "error": "audience too complex to estimate in 5s"}) from e
    return {"count": count}


@router.post("", response_model=SegmentOut, status_code=201)
async def create_segment(
    body: SegmentIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> SegmentOut:
    # validate + compute the initial count / static snapshot
    try:
        svc.compile_definition(member.workspace_id, body.definition)
    except svc.SegmentDefinitionError as e:
        raise HTTPException(422, detail={"code": "invalid_definition", "error": str(e)}) from e
    snap: list[str] | None = None
    count: int | None = None
    try:
        if body.mode == "static":
            snap = await svc.snapshot_ids(session, member.workspace_id, body.definition)
            count = len(snap)
        else:
            count = await svc.estimate_count(session, member.workspace_id, body.definition)
    except svc.EstimateTimeout:
        count = None
    row = Segment(
        workspace_id=member.workspace_id, name=body.name, mode=body.mode,
        definition=body.definition, snapshot_ids=snap, count_cache=count,
    )
    session.add(row)
    await session.commit()
    return _out(row)


@router.get("/{segment_id}", response_model=SegmentOut)
async def get_segment(
    segment_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> SegmentOut:
    return _out(await _get(session, member.workspace_id, segment_id))


@router.patch("/{segment_id}", response_model=SegmentOut)
async def update_segment(
    segment_id: uuid.UUID,
    body: SegmentPatch,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> SegmentOut:
    row = await _get(session, member.workspace_id, segment_id)
    if body.name is not None:
        row.name = body.name
    if body.definition is not None:
        try:
            svc.compile_definition(member.workspace_id, body.definition)
        except svc.SegmentDefinitionError as e:
            raise HTTPException(422, detail={"code": "invalid_definition", "error": str(e)}) from e
        row.definition = body.definition
        try:
            if row.mode == "static":
                snap = await svc.snapshot_ids(session, member.workspace_id, body.definition)
                row.snapshot_ids = snap
                row.count_cache = len(snap)
            else:
                row.count_cache = await svc.estimate_count(session, member.workspace_id, body.definition)
        except svc.EstimateTimeout:
            row.count_cache = None
    await session.commit()
    return _out(row)


@router.delete("/{segment_id}", status_code=204)
async def delete_segment(
    segment_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await _get(session, member.workspace_id, segment_id)
    await session.delete(row)
    await session.commit()
