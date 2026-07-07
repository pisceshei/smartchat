"""分流連結 CRUD + click stats + server-side QR (plan B.3 / route contract)."""
from __future__ import annotations

import base64
import uuid
from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, require_permission
from ...models.marketing import SplitLink
from ...services.redis_client import get_redis
from . import service as svc

router = APIRouter(prefix="/api/v1/split-links", tags=["split_links"])


class SplitLinkIn(BaseModel):
    name: str = Field(default="", max_length=64)
    channel_type: str = Field(max_length=24)
    strategy: Literal["random", "time_period", "sequential"] = "random"
    targets: list[dict[str, Any]] = Field(default_factory=list)
    prefill_text: str | None = Field(default=None, max_length=1000)


class SplitLinkPatch(BaseModel):
    name: str | None = None
    strategy: Literal["random", "time_period", "sequential"] | None = None
    targets: list[dict[str, Any]] | None = None
    prefill_text: str | None = None
    status: Literal["active", "paused"] | None = None


class SplitLinkItem(BaseModel):
    id: uuid.UUID
    name: str
    channel_type: str
    strategy: str
    status: str
    short_url: str
    qr_url: str
    target_count: int
    click_count: int
    created_at: datetime


class SplitLinkFull(SplitLinkItem):
    targets: list[dict[str, Any]]
    prefill_text: str | None
    qr_data_uri: str
    clicks: dict[str, Any]


def _item(link: SplitLink) -> SplitLinkItem:
    return SplitLinkItem(
        id=link.id, name=link.name, channel_type=link.channel_type, strategy=link.strategy,
        status=link.status, short_url=svc.short_url(link.slug), qr_url=svc.qr_url(link.id),
        target_count=len(link.targets or []), click_count=int(link.click_count), created_at=link.created_at,
    )


async def _get(session: AsyncSession, workspace_id: uuid.UUID, link_id: uuid.UUID) -> SplitLink:
    link = await session.get(SplitLink, link_id)
    if link is None or link.workspace_id != workspace_id:
        raise HTTPException(404, detail="split link not found")
    return link


@router.get("", response_model=list[SplitLinkItem])
async def list_links(
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[SplitLinkItem]:
    rows = (
        await session.execute(
            select(SplitLink).where(SplitLink.workspace_id == member.workspace_id)
            .order_by(SplitLink.created_at.desc())
        )
    ).scalars().all()
    return [_item(r) for r in rows]


@router.post("", response_model=SplitLinkFull, status_code=201)
async def create_link(
    body: SplitLinkIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> SplitLinkFull:
    try:
        targets = svc.validate_targets(body.targets, channel_type=body.channel_type)
    except svc.SplitLinkError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    slug = await svc.unique_slug(session)
    link = SplitLink(
        workspace_id=member.workspace_id, slug=slug, name=body.name,
        channel_type=body.channel_type, strategy=body.strategy, targets=targets,
        prefill_text=body.prefill_text,
    )
    session.add(link)
    await session.flush()
    link.qr_key = await svc.cache_qr(session, link)
    await session.commit()
    await svc.cache_config(get_redis(), link)
    return await _full(session, link)


@router.get("/{link_id}", response_model=SplitLinkFull)
async def get_link(
    link_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> SplitLinkFull:
    return await _full(session, await _get(session, member.workspace_id, link_id))


@router.patch("/{link_id}", response_model=SplitLinkFull)
async def update_link(
    link_id: uuid.UUID,
    body: SplitLinkPatch,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> SplitLinkFull:
    link = await _get(session, member.workspace_id, link_id)
    if body.name is not None:
        link.name = body.name
    if body.strategy is not None:
        link.strategy = body.strategy
    if body.status is not None:
        link.status = body.status
    if body.prefill_text is not None:
        link.prefill_text = body.prefill_text
    if body.targets is not None:
        try:
            link.targets = svc.validate_targets(body.targets, channel_type=link.channel_type)
        except svc.SplitLinkError as e:
            raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    await svc.cache_config(get_redis(), link)
    return await _full(session, link)


@router.delete("/{link_id}", status_code=204)
async def delete_link(
    link_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    link = await _get(session, member.workspace_id, link_id)
    slug = link.slug
    await session.delete(link)
    await session.commit()
    await svc.invalidate_config(get_redis(), slug)


@router.get("/{link_id}/clicks")
async def link_clicks(
    link_id: uuid.UUID,
    frm: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get(session, member.workspace_id, link_id)
    return await svc.click_series(session, link_id=link_id, frm=frm, to=to)


@router.get("/{link_id}/qr.png")
async def link_qr(
    link_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    link = await _get(session, member.workspace_id, link_id)
    return Response(content=svc.render_qr_png(link.slug), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


async def _full(session: AsyncSession, link: SplitLink) -> SplitLinkFull:
    base = _item(link)
    try:
        png_b64 = base64.b64encode(svc.render_qr_png(link.slug)).decode()
        qr_data_uri = f"data:image/png;base64,{png_b64}"
    except Exception:  # noqa: BLE001
        qr_data_uri = ""
    clicks = await svc.click_series(session, link_id=link.id)
    return SplitLinkFull(
        **base.model_dump(), targets=link.targets or [], prefill_text=link.prefill_text,
        qr_data_uri=qr_data_uri, clicks=clicks,
    )
