"""Workspace CRUD + plan/quota info (/api/v1/workspaces/*)."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, current_member, current_user, require_permission
from ...models.members import User, WorkspaceMember
from ...models.tenancy import Plan, UsageCounter, Workspace
from ...services import quotas
from ...services.redis_client import get_redis
from .service import bootstrap_workspace

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    plan_code: str
    status: str
    settings: dict[str, Any]

    model_config = {"from_attributes": True}


class WorkspaceCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class WorkspaceUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)


class PlanInfoOut(BaseModel):
    plan_code: str
    plan_name: str
    price_usd_month: float | None
    limits: dict[str, Any]
    usage: dict[str, int]
    period_month: str


@router.get("", response_model=list[WorkspaceOut])
async def list_my_workspaces(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[WorkspaceOut]:
    rows = (
        await session.execute(
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.status == "active",
                Workspace.status == "active",
            )
            .order_by(Workspace.created_at)
        )
    ).scalars().all()
    return [WorkspaceOut.model_validate(w) for w in rows]


@router.post("", response_model=WorkspaceOut, status_code=201)
async def create_workspace(
    body: WorkspaceCreateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceOut:
    ws, _member = await bootstrap_workspace(session, name=body.name, owner=user)
    await session.commit()
    return WorkspaceOut.model_validate(ws)


@router.get("/current", response_model=WorkspaceOut)
async def get_current_workspace(member: MemberContext = Depends(current_member)) -> WorkspaceOut:
    return WorkspaceOut.model_validate(member.workspace)


@router.patch("/current", response_model=WorkspaceOut)
async def update_current_workspace(
    body: WorkspaceUpdateIn,
    member: MemberContext = Depends(require_permission("workspace.manage")),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceOut:
    ws = await session.get(Workspace, member.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if body.name is not None:
        ws.name = body.name
    await session.commit()
    return WorkspaceOut.model_validate(ws)


@router.get("/current/plan", response_model=PlanInfoOut)
async def get_plan_info(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> PlanInfoOut:
    """Plan + effective limits (plan ⊕ overrides) + this month's usage."""
    plan = await session.get(Plan, member.workspace.plan_code)
    if plan is None:
        raise HTTPException(status_code=500, detail="plan missing — run seed")
    limits = await quotas.effective_limits(session, get_redis(), member.workspace_id)
    limits.pop("_plan_code", None)
    period = quotas.current_period()
    usage_rows = (
        await session.execute(
            select(UsageCounter.metric, UsageCounter.value).where(
                UsageCounter.workspace_id == member.workspace_id,
                UsageCounter.period_month == period,
            )
        )
    ).all()
    return PlanInfoOut(
        plan_code=plan.code,
        plan_name=plan.name,
        price_usd_month=float(plan.price_usd_month) if plan.price_usd_month is not None else None,
        limits=limits,
        usage={m: int(v) for m, v in usage_rows},
        period_month=period,
    )
