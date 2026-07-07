"""群發 CRUD + lifecycle + drill-down (plan B.3 / route contract).

Route order: the static ``/recycle-bin`` subpath is declared before the
``/{broadcast_id}`` matcher so it is not swallowed as an id.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, require_permission
from ...marketing import fanout
from ...marketing.recipients import success_rate as _sr
from ...models.contacts import Contact
from ...models.marketing import Broadcast, BroadcastRecipient, BroadcastRun
from . import service as svc

router = APIRouter(prefix="/api/v1/broadcasts", tags=["broadcasts"])
RECIPIENT_PAGE = 100


# ==========================================================================
# schemas
# ==========================================================================
class BroadcastCreateIn(BaseModel):
    name: str = Field(default="", max_length=120)
    type: Literal["one_time", "recurring"] = "one_time"
    channel_type: str = Field(max_length=24)
    channel_account_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    variable_mapping: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    send_rules: dict[str, Any] = Field(default_factory=dict)


class BroadcastPatchIn(BaseModel):
    name: str | None = None
    type: Literal["one_time", "recurring"] | None = None
    channel_type: str | None = None
    channel_account_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    variable_mapping: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    send_rules: dict[str, Any] | None = None


class BroadcastListItem(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    status: str
    channel_type: str
    channel_account_id: uuid.UUID | None
    send_rule_summary: str
    planned_count: int
    sent_count: int
    delivered_count: int
    success_rate: float
    created_at: datetime


class RunOut(BaseModel):
    id: uuid.UUID
    scheduled_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    status: str
    planned: int
    sent: int
    delivered: int
    read: int
    failed: int
    skipped: int

    model_config = {"from_attributes": True}


class BroadcastOut(BroadcastListItem):
    segment_id: uuid.UUID | None
    template_id: uuid.UUID | None
    variable_mapping: dict[str, Any]
    schedule: dict[str, Any]
    send_rules: dict[str, Any]
    read_count: int
    failed_count: int
    skipped_count: int
    runs: list[RunOut]


class RecipientOut(BaseModel):
    contact_id: uuid.UUID | None
    display_name: str | None
    state: str
    skip_reason: str | None
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    error: str | None


class RecipientPage(BaseModel):
    items: list[RecipientOut]
    next_cursor: str | None


def _item(bc: Broadcast) -> BroadcastListItem:
    return BroadcastListItem(
        id=bc.id, name=bc.name, type=bc.type, status=bc.status, channel_type=bc.channel_type,
        channel_account_id=bc.channel_account_id, send_rule_summary=svc.send_rule_summary(bc),
        planned_count=bc.planned_count, sent_count=bc.sent_count,
        delivered_count=bc.delivered_count, success_rate=_sr(bc.sent_count, bc.delivered_count),
        created_at=bc.created_at,
    )


async def _full(session: AsyncSession, bc: Broadcast) -> BroadcastOut:
    runs = (
        await session.execute(
            select(BroadcastRun).where(BroadcastRun.broadcast_id == bc.id)
            .order_by(BroadcastRun.scheduled_at.desc().nulls_last(), BroadcastRun.created_at.desc())
        )
    ).scalars().all()
    base = _item(bc)
    return BroadcastOut(
        **base.model_dump(),
        segment_id=bc.segment_id, template_id=bc.template_id,
        variable_mapping=bc.variable_mapping or {}, schedule=bc.schedule or {},
        send_rules=bc.send_rules or {}, read_count=bc.read_count,
        failed_count=bc.failed_count, skipped_count=bc.skipped_count,
        runs=[RunOut.model_validate(r) for r in runs],
    )


def _enc_cursor(rid: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(rid.bytes).decode().rstrip("=")


def _dec_cursor(cur: str) -> uuid.UUID:
    try:
        pad = cur + "=" * (-len(cur) % 4)
        return uuid.UUID(bytes=base64.urlsafe_b64decode(pad))
    except (ValueError, TypeError) as e:
        raise HTTPException(422, detail="bad cursor") from e


# ==========================================================================
# list / create
# ==========================================================================
@router.get("", response_model=list[BroadcastListItem])
async def list_broadcasts(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[BroadcastListItem]:
    query = select(Broadcast).where(
        Broadcast.workspace_id == member.workspace_id, Broadcast.deleted_at.is_(None)
    )
    if type:
        query = query.where(Broadcast.type == type)
    if status:
        query = query.where(Broadcast.status == status)
    if q:
        query = query.where(Broadcast.name.ilike(f"%{q}%"))
    rows = (await session.execute(query.order_by(Broadcast.created_at.desc()))).scalars().all()
    return [_item(b) for b in rows]


@router.post("", response_model=BroadcastOut, status_code=201)
async def create_broadcast(
    body: BroadcastCreateIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    try:
        bc, run_id = await svc.create(
            session, workspace_id=member.workspace_id,
            created_by_member_id=member.member_id, data=body.model_dump(),
        )
    except svc.BroadcastError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    if run_id is not None:
        await fanout.enqueue_fanout(run_id)
    return await _full(session, bc)


@router.get("/recycle-bin", response_model=list[BroadcastListItem])
async def recycle_bin(
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[BroadcastListItem]:
    rows = (
        await session.execute(
            select(Broadcast).where(
                Broadcast.workspace_id == member.workspace_id, Broadcast.deleted_at.is_not(None)
            ).order_by(Broadcast.deleted_at.desc())
        )
    ).scalars().all()
    return [_item(b) for b in rows]


# ==========================================================================
# detail / edit / lifecycle
# ==========================================================================
@router.get("/{broadcast_id}", response_model=BroadcastOut)
async def get_broadcast(
    broadcast_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    try:
        bc = await svc.get(session, member.workspace_id, broadcast_id)
    except svc.BroadcastError as e:
        raise HTTPException(404, detail=e.detail) from e
    return await _full(session, bc)


@router.patch("/{broadcast_id}", response_model=BroadcastOut)
async def update_broadcast(
    broadcast_id: uuid.UUID,
    body: BroadcastPatchIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    try:
        bc = await svc.get(session, member.workspace_id, broadcast_id)
        bc = await svc.update(session, bc, body.model_dump(exclude_unset=True))
    except svc.BroadcastError as e:
        code = 404 if e.code == "not_found" else 422
        raise HTTPException(code, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    return await _full(session, bc)


async def _lifecycle(session, member, broadcast_id) -> Broadcast:
    try:
        return await svc.get(session, member.workspace_id, broadcast_id)
    except svc.BroadcastError as e:
        raise HTTPException(404, detail=e.detail) from e


@router.post("/{broadcast_id}/pause", response_model=BroadcastOut)
async def pause_broadcast(
    broadcast_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    bc = await _lifecycle(session, member, broadcast_id)
    try:
        await svc.pause(session, bc)
    except svc.BroadcastError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    return await _full(session, bc)


@router.post("/{broadcast_id}/resume", response_model=BroadcastOut)
async def resume_broadcast(
    broadcast_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    bc = await _lifecycle(session, member, broadcast_id)
    try:
        run_ids = await svc.resume(session, bc)
    except svc.BroadcastError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    for rid in run_ids:
        await fanout.enqueue_fanout(rid)
    return await _full(session, bc)


@router.post("/{broadcast_id}/cancel", response_model=BroadcastOut)
async def cancel_broadcast(
    broadcast_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    bc = await _lifecycle(session, member, broadcast_id)
    try:
        await svc.cancel(session, bc)
    except svc.BroadcastError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    return await _full(session, bc)


@router.delete("/{broadcast_id}", status_code=204)
async def delete_broadcast(
    broadcast_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    bc = await _lifecycle(session, member, broadcast_id)
    await svc.soft_delete(session, bc)
    await session.commit()


@router.post("/{broadcast_id}/restore", response_model=BroadcastOut)
async def restore_broadcast(
    broadcast_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> BroadcastOut:
    try:
        bc = await svc.get(session, member.workspace_id, broadcast_id, include_deleted=True)
        await svc.restore(session, bc)
    except svc.BroadcastError as e:
        code = 404 if e.code == "not_found" else 422
        raise HTTPException(code, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    return await _full(session, bc)


# ==========================================================================
# runs + recipients drill-down
# ==========================================================================
@router.get("/{broadcast_id}/runs", response_model=list[RunOut])
async def list_runs(
    broadcast_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[RunOut]:
    await _lifecycle(session, member, broadcast_id)
    rows = (
        await session.execute(
            select(BroadcastRun).where(BroadcastRun.broadcast_id == broadcast_id)
            .order_by(BroadcastRun.scheduled_at.desc().nulls_last(), BroadcastRun.created_at.desc())
        )
    ).scalars().all()
    return [RunOut.model_validate(r) for r in rows]


@router.get("/{broadcast_id}/runs/{run_id}/recipients", response_model=RecipientPage)
async def list_recipients(
    broadcast_id: uuid.UUID,
    run_id: uuid.UUID,
    state: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> RecipientPage:
    await _lifecycle(session, member, broadcast_id)
    run = await session.get(BroadcastRun, run_id)
    if run is None or run.broadcast_id != broadcast_id:
        raise HTTPException(404, detail="run not found")
    q = (
        select(BroadcastRecipient, Contact.display_name)
        .join(Contact, Contact.id == BroadcastRecipient.contact_id, isouter=True)
        .where(BroadcastRecipient.run_id == run_id)
    )
    if state:
        q = q.where(BroadcastRecipient.state == state)
    if cursor:
        q = q.where(BroadcastRecipient.id > _dec_cursor(cursor))
    rows = (
        await session.execute(q.order_by(BroadcastRecipient.id).limit(RECIPIENT_PAGE + 1))
    ).all()
    has_more = len(rows) > RECIPIENT_PAGE
    rows = rows[:RECIPIENT_PAGE]
    items = [
        RecipientOut(
            contact_id=r.contact_id, display_name=name, state=r.state,
            skip_reason=r.skip_reason, sent_at=r.sent_at, delivered_at=r.delivered_at,
            read_at=r.read_at, error=r.last_error,
        )
        for r, name in rows
    ]
    next_cursor = _enc_cursor(rows[-1][0].id) if has_more and rows else None
    return RecipientPage(items=items, next_cursor=next_cursor)
