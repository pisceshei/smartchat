"""EDM 第三方代發 CRUD + launch (plan B.3 / route contract)."""
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
from ...models.marketing import EdmCampaign, MsgTemplate, Segment
from . import service as svc

router = APIRouter(prefix="/api/v1/edm", tags=["edm"])


class EdmIn(BaseModel):
    name: str = Field(default="", max_length=128)
    provider: Literal["smtp", "ses", "sendgrid", "edm_provider"] = "smtp"
    segment_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    schedule: dict[str, Any] = Field(default_factory=dict)


class EdmPatch(BaseModel):
    name: str | None = None
    provider: Literal["smtp", "ses", "sendgrid", "edm_provider"] | None = None
    segment_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    schedule: dict[str, Any] | None = None


class EdmOut(BaseModel):
    id: uuid.UUID
    name: str
    provider: str
    segment_id: uuid.UUID | None
    template_id: uuid.UUID | None
    schedule: dict[str, Any]
    status: str
    planned_count: int
    sent_count: int
    delivered_count: int
    opened_count: int
    clicked_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


async def _validate_refs(session: AsyncSession, workspace_id: uuid.UUID, data: dict[str, Any]) -> None:
    if data.get("segment_id"):
        seg = await session.get(Segment, data["segment_id"])
        if seg is None or seg.workspace_id != workspace_id:
            raise HTTPException(422, detail="segment not found")
    if data.get("template_id"):
        tpl = await session.get(MsgTemplate, data["template_id"])
        if tpl is None or tpl.workspace_id != workspace_id:
            raise HTTPException(422, detail="template not found")
        if tpl.channel != "email":
            raise HTTPException(422, detail="EDM requires an email template")


async def _get(session: AsyncSession, workspace_id: uuid.UUID, cid: uuid.UUID) -> EdmCampaign:
    row = await session.get(EdmCampaign, cid)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(404, detail="campaign not found")
    return row


@router.get("", response_model=list[EdmOut])
async def list_campaigns(
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[EdmOut]:
    rows = (
        await session.execute(
            select(EdmCampaign).where(EdmCampaign.workspace_id == member.workspace_id)
            .order_by(EdmCampaign.created_at.desc())
        )
    ).scalars().all()
    return [EdmOut.model_validate(r) for r in rows]


@router.post("", response_model=EdmOut, status_code=201)
async def create_campaign(
    body: EdmIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> EdmOut:
    try:
        svc.validate(body.model_dump())
    except svc.EdmError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await _validate_refs(session, member.workspace_id, body.model_dump())
    row = EdmCampaign(workspace_id=member.workspace_id, **body.model_dump())
    session.add(row)
    await session.commit()
    return EdmOut.model_validate(row)


@router.get("/{campaign_id}", response_model=EdmOut)
async def get_campaign(
    campaign_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> EdmOut:
    return EdmOut.model_validate(await _get(session, member.workspace_id, campaign_id))


@router.patch("/{campaign_id}", response_model=EdmOut)
async def update_campaign(
    campaign_id: uuid.UUID,
    body: EdmPatch,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> EdmOut:
    row = await _get(session, member.workspace_id, campaign_id)
    if row.status in ("running", "completed"):
        raise HTTPException(422, detail="cannot edit a launched campaign")
    data = body.model_dump(exclude_unset=True)
    if "provider" in data:
        svc.validate(data)
    await _validate_refs(session, member.workspace_id, {**{"segment_id": row.segment_id,
                         "template_id": row.template_id}, **data})
    for k, v in data.items():
        setattr(row, k, v)
    await session.commit()
    return EdmOut.model_validate(row)


@router.delete("/{campaign_id}", status_code=204)
async def delete_campaign(
    campaign_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await _get(session, member.workspace_id, campaign_id)
    await session.delete(row)
    await session.commit()


@router.post("/{campaign_id}/send", response_model=EdmOut)
async def send_campaign(
    campaign_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> EdmOut:
    row = await _get(session, member.workspace_id, campaign_id)
    if row.status in ("running", "completed"):
        raise HTTPException(422, detail=f"campaign already {row.status}")
    if row.status == "draft":
        row.status = "scheduled"
        await session.commit()
    await svc.enqueue_launch(campaign_id)
    return EdmOut.model_validate(row)
