"""Auth: register / login / refresh / me (/api/v1/auth/*)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import current_user
from ...models.members import User, WorkspaceMember
from ...models.misc import AuditLog
from ...models.tenancy import Workspace
from ...services.security import (
    TokenInvalid,
    hash_password,
    issue_token_pair,
    verify_password,
    verify_token,
)
from ..workspaces.service import bootstrap_workspace

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# pragmatic email shape check (avoids the email-validator dependency)
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]


class RegisterIn(BaseModel):
    email: Email
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    workspace_name: str | None = Field(default=None, max_length=128)


class LoginIn(BaseModel):
    email: Email
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class WorkspaceBrief(BaseModel):
    id: uuid.UUID
    name: str
    plan_code: str
    member_id: uuid.UUID


class AuthOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID
    email: str
    display_name: str
    workspaces: list[WorkspaceBrief]


async def _memberships(session: AsyncSession, user_id: uuid.UUID) -> list[WorkspaceBrief]:
    rows = (
        await session.execute(
            select(Workspace.id, Workspace.name, Workspace.plan_code, WorkspaceMember.id)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.status == "active",
                Workspace.status == "active",
            )
            .order_by(Workspace.created_at)
        )
    ).all()
    return [
        WorkspaceBrief(id=wid, name=name, plan_code=plan, member_id=mid)
        for wid, name, plan, mid in rows
    ]


def _auth_out(user: User, workspaces: list[WorkspaceBrief]) -> AuthOut:
    pair = issue_token_pair(user.id)
    return AuthOut(
        access_token=pair["access_token"],
        refresh_token=pair["refresh_token"],
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        workspaces=workspaces,
    )


@router.post("/register", response_model=AuthOut, status_code=201)
async def register(
    body: RegisterIn, request: Request, session: AsyncSession = Depends(get_session)
) -> AuthOut:
    """Create user + first workspace (free plan) + super_admin membership."""
    existing = (
        await session.execute(select(User.id).where(User.email == body.email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.name,
        last_login_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    ws, member = await bootstrap_workspace(
        session, name=body.workspace_name or f"{body.name} 的工作區", owner=user
    )
    session.add(
        AuditLog(
            workspace_id=ws.id,
            actor_type="member",
            actor_id=member.id,
            action="auth.register",
            ip=request.client.host if request.client else None,
        )
    )
    await session.commit()
    return _auth_out(
        user, [WorkspaceBrief(id=ws.id, name=ws.name, plan_code=ws.plan_code, member_id=member.id)]
    )


@router.post("/login", response_model=AuthOut)
async def login(
    body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)
) -> AuthOut:
    user = (
        await session.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account disabled")
    user.last_login_at = datetime.now(UTC)
    workspaces = await _memberships(session, user.id)
    for ws in workspaces:
        session.add(
            AuditLog(
                workspace_id=ws.id,
                actor_type="member",
                actor_id=ws.member_id,
                action="auth.login",
                ip=request.client.host if request.client else None,
            )
        )
    await session.commit()
    return _auth_out(user, workspaces)


@router.post("/refresh", response_model=AuthOut)
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)) -> AuthOut:
    """Rotate the pair: a valid refresh token yields new access+refresh."""
    try:
        claims = verify_token(body.refresh_token, expected_type="refresh")
    except TokenInvalid as e:
        raise HTTPException(status_code=401, detail=f"invalid refresh token: {e}") from e
    user = await session.get(User, uuid.UUID(claims["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or disabled")
    return _auth_out(user, await _memberships(session, user.id))


class MeOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    avatar_url: str | None
    workspaces: list[WorkspaceBrief]


@router.get("/me", response_model=MeOut)
async def me(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> MeOut:
    return MeOut(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        workspaces=await _memberships(session, user.id),
    )
