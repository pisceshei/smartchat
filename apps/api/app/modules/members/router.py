"""Team: members (human + AI) / roles / groups / shifts (/api/v1/members/*).

Route order matters: static subpaths (roles/groups/invite) are declared
before the /{member_id} matchers.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import (
    PERMISSION_KEYS,
    MemberContext,
    current_member,
    current_user,
    require_permission,
)
from ...models.members import (
    MemberGroup,
    MemberGroupMember,
    MemberShift,
    Role,
    User,
    WorkspaceMember,
)
from ...models.misc import AuditLog

router = APIRouter(prefix="/api/v1/members", tags=["members"])

INVITE_TTL_DAYS = 7


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------
class MemberOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID | None
    member_type: str
    role_id: uuid.UUID | None
    display_name: str
    avatar_url: str | None
    status: str
    max_concurrent: int
    ai_config: dict[str, Any] | None
    invited_email: str | None

    model_config = {"from_attributes": True}


class AIMemberCreateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    ai_config: dict[str, Any] = Field(default_factory=dict)
    max_concurrent: int = Field(default=0, ge=0)
    role_id: uuid.UUID | None = None


class MemberUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role_id: uuid.UUID | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    max_concurrent: int | None = Field(default=None, ge=0)
    ai_config: dict[str, Any] | None = None


class InviteIn(BaseModel):
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role_id: uuid.UUID | None = None
    display_name: str | None = None


class InviteOut(BaseModel):
    member_id: uuid.UUID
    invite_token: str
    invite_url: str
    expires_at: datetime


class InviteAcceptIn(BaseModel):
    token: str


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    permissions: list[str] = Field(default_factory=list)


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    key: str | None
    is_system: bool
    permissions: list[str]
    role_version: int

    model_config = {"from_attributes": True}


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    member_ids: list[uuid.UUID] = Field(default_factory=list)


class GroupMembersIn(BaseModel):
    member_ids: list[uuid.UUID]


class ShiftIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    start_min: int = Field(ge=0, le=1439)
    end_min: int = Field(ge=1, le=1440)
    timezone: str | None = None


class ShiftOut(ShiftIn):
    id: uuid.UUID

    model_config = {"from_attributes": True}


def _validate_permissions(perms: list[str]) -> None:
    valid = set(PERMISSION_KEYS) | {"*"} | {f"{k.split('.')[0]}.*" for k in PERMISSION_KEYS}
    unknown = [p for p in perms if p not in valid]
    if unknown:
        raise HTTPException(status_code=422, detail={"code": "unknown_permissions", "keys": unknown})


# --------------------------------------------------------------------------
# roles (before /{member_id})
# --------------------------------------------------------------------------
@router.get("/roles", response_model=list[RoleOut])
async def list_roles(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[RoleOut]:
    rows = (
        await session.execute(
            select(Role).where(Role.workspace_id == member.workspace_id).order_by(Role.created_at)
        )
    ).scalars().all()
    return [RoleOut.model_validate(r) for r in rows]


@router.post("/roles", response_model=RoleOut, status_code=201)
async def create_role(
    body: RoleIn,
    member: MemberContext = Depends(require_permission("roles.manage")),
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    _validate_permissions(body.permissions)
    role = Role(workspace_id=member.workspace_id, name=body.name, permissions=body.permissions)
    session.add(role)
    await session.commit()
    return RoleOut.model_validate(role)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: uuid.UUID,
    body: RoleIn,
    member: MemberContext = Depends(require_permission("roles.manage")),
    session: AsyncSession = Depends(get_session),
) -> RoleOut:
    role = await session.get(Role, role_id)
    if role is None or role.workspace_id != member.workspace_id:
        raise HTTPException(status_code=404, detail="role not found")
    if role.is_system and role.key == "super_admin":
        raise HTTPException(status_code=403, detail="super_admin role is immutable")
    _validate_permissions(body.permissions)
    role.name = body.name
    role.permissions = body.permissions
    role.role_version += 1
    await session.commit()
    return RoleOut.model_validate(role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("roles.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    role = await session.get(Role, role_id)
    if role is None or role.workspace_id != member.workspace_id:
        raise HTTPException(status_code=404, detail="role not found")
    if role.is_system:
        raise HTTPException(status_code=403, detail="system roles cannot be deleted")
    in_use = (
        await session.execute(
            select(WorkspaceMember.id).where(WorkspaceMember.role_id == role_id).limit(1)
        )
    ).scalar_one_or_none()
    if in_use:
        raise HTTPException(status_code=409, detail="role in use by members")
    await session.delete(role)
    await session.commit()


# --------------------------------------------------------------------------
# groups
# --------------------------------------------------------------------------
async def _group_out(session: AsyncSession, group: MemberGroup) -> GroupOut:
    ids = (
        await session.execute(
            select(MemberGroupMember.member_id).where(MemberGroupMember.group_id == group.id)
        )
    ).scalars().all()
    return GroupOut(id=group.id, name=group.name, description=group.description, member_ids=list(ids))


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[GroupOut]:
    rows = (
        await session.execute(
            select(MemberGroup)
            .where(MemberGroup.workspace_id == member.workspace_id)
            .order_by(MemberGroup.created_at)
        )
    ).scalars().all()
    return [await _group_out(session, g) for g in rows]


@router.post("/groups", response_model=GroupOut, status_code=201)
async def create_group(
    body: GroupIn,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    group = MemberGroup(
        workspace_id=member.workspace_id, name=body.name, description=body.description
    )
    session.add(group)
    await session.commit()
    return await _group_out(session, group)


@router.put("/groups/{group_id}/members", response_model=GroupOut)
async def set_group_members(
    group_id: uuid.UUID,
    body: GroupMembersIn,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    group = await session.get(MemberGroup, group_id)
    if group is None or group.workspace_id != member.workspace_id:
        raise HTTPException(status_code=404, detail="group not found")
    valid_ids = set(
        (
            await session.execute(
                select(WorkspaceMember.id).where(
                    WorkspaceMember.workspace_id == member.workspace_id,
                    WorkspaceMember.id.in_(body.member_ids),
                )
            )
        ).scalars()
    )
    unknown = set(body.member_ids) - valid_ids
    if unknown:
        raise HTTPException(
            status_code=422, detail={"code": "unknown_members", "ids": [str(u) for u in unknown]}
        )
    await session.execute(delete(MemberGroupMember).where(MemberGroupMember.group_id == group_id))
    for mid in valid_ids:
        session.add(
            MemberGroupMember(workspace_id=member.workspace_id, group_id=group_id, member_id=mid)
        )
    await session.commit()
    return await _group_out(session, group)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    group = await session.get(MemberGroup, group_id)
    if group is None or group.workspace_id != member.workspace_id:
        raise HTTPException(status_code=404, detail="group not found")
    await session.delete(group)
    await session.commit()


# --------------------------------------------------------------------------
# invite flow (stub: token issued; email delivery lands with the notifier)
# --------------------------------------------------------------------------
@router.post("/invite", response_model=InviteOut, status_code=201)
async def invite_member(
    body: InviteIn,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> InviteOut:
    from ...settings import get_settings

    dup = (
        await session.execute(
            select(WorkspaceMember.id)
            .join(User, User.id == WorkspaceMember.user_id, isouter=True)
            .where(
                WorkspaceMember.workspace_id == member.workspace_id,
                (WorkspaceMember.invited_email == body.email) | (User.email == body.email),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail="already a member or invited")
    token = secrets.token_urlsafe(24)
    expires = datetime.now(UTC) + timedelta(days=INVITE_TTL_DAYS)
    invited = WorkspaceMember(
        workspace_id=member.workspace_id,
        member_type="human",
        role_id=body.role_id,
        display_name=body.display_name or body.email.split("@")[0],
        status="invited",
        invited_email=body.email,
        invite_token=token,
        invite_expires_at=expires,
    )
    session.add(invited)
    session.add(
        AuditLog(
            workspace_id=member.workspace_id,
            actor_type="member",
            actor_id=member.member_id,
            action="members.invite",
            target_type="member_email",
            target_id=body.email,
        )
    )
    await session.commit()
    return InviteOut(
        member_id=invited.id,
        invite_token=token,
        invite_url=f"{get_settings().public_base_url}/invite/{token}",
        expires_at=expires,
    )


@router.post("/invite/accept", response_model=MemberOut)
async def accept_invite(
    body: InviteAcceptIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    """Logged-in user redeems an invite token → membership activates."""
    invited = (
        await session.execute(
            select(WorkspaceMember).where(WorkspaceMember.invite_token == body.token)
        )
    ).scalar_one_or_none()
    if invited is None or invited.status != "invited":
        raise HTTPException(status_code=404, detail="invite not found")
    if invited.invite_expires_at and invited.invite_expires_at < datetime.now(UTC):
        raise HTTPException(status_code=410, detail="invite expired")
    existing = (
        await session.execute(
            select(WorkspaceMember.id).where(
                WorkspaceMember.workspace_id == invited.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="already a member of this workspace")
    invited.user_id = user.id
    invited.status = "active"
    invited.invite_token = None
    if not invited.display_name:
        invited.display_name = user.display_name or user.email
    await session.commit()
    return MemberOut.model_validate(invited)


# --------------------------------------------------------------------------
# members
# --------------------------------------------------------------------------
@router.get("", response_model=list[MemberOut])
async def list_members(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    rows = (
        await session.execute(
            select(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == member.workspace_id)
            .order_by(WorkspaceMember.created_at)
        )
    ).scalars().all()
    return [MemberOut.model_validate(m) for m in rows]


@router.post("", response_model=MemberOut, status_code=201)
async def create_ai_member(
    body: AIMemberCreateIn,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    """AI 成員 — participates in auto-assignment like a human (member_type)."""
    ai = WorkspaceMember(
        workspace_id=member.workspace_id,
        member_type="ai_agent",
        role_id=body.role_id,
        display_name=body.display_name,
        status="active",
        max_concurrent=body.max_concurrent,
        ai_config=body.ai_config,
    )
    session.add(ai)
    await session.commit()
    return MemberOut.model_validate(ai)


@router.patch("/{member_id}", response_model=MemberOut)
async def update_member(
    member_id: uuid.UUID,
    body: MemberUpdateIn,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    target = await session.get(WorkspaceMember, member_id)
    if target is None or target.workspace_id != member.workspace_id:
        raise HTTPException(status_code=404, detail="member not found")
    if body.role_id is not None:
        role = await session.get(Role, body.role_id)
        if role is None or role.workspace_id != member.workspace_id:
            raise HTTPException(status_code=422, detail="role not found in workspace")
        target.role_id = body.role_id
    if body.display_name is not None:
        target.display_name = body.display_name
    if body.status is not None:
        target.status = body.status
    if body.max_concurrent is not None:
        target.max_concurrent = body.max_concurrent
    if body.ai_config is not None:
        if target.member_type != "ai_agent":
            raise HTTPException(status_code=422, detail="ai_config only valid for ai_agent members")
        target.ai_config = body.ai_config
    await session.commit()
    return MemberOut.model_validate(target)


@router.delete("/{member_id}", status_code=204)
async def remove_member(
    member_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    target = await session.get(WorkspaceMember, member_id)
    if target is None or target.workspace_id != member.workspace_id:
        raise HTTPException(status_code=404, detail="member not found")
    if target.user_id == member.workspace.owner_user_id:
        raise HTTPException(status_code=403, detail="cannot remove the workspace owner")
    await session.delete(target)
    await session.commit()


# --------------------------------------------------------------------------
# shifts
# --------------------------------------------------------------------------
@router.get("/{member_id}/shifts", response_model=list[ShiftOut])
async def list_shifts(
    member_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[ShiftOut]:
    rows = (
        await session.execute(
            select(MemberShift)
            .where(
                MemberShift.workspace_id == member.workspace_id,
                MemberShift.member_id == member_id,
            )
            .order_by(MemberShift.weekday, MemberShift.start_min)
        )
    ).scalars().all()
    return [ShiftOut.model_validate(s) for s in rows]


@router.put("/{member_id}/shifts", response_model=list[ShiftOut])
async def replace_shifts(
    member_id: uuid.UUID,
    body: list[ShiftIn],
    member: MemberContext = Depends(require_permission("members.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[ShiftOut]:
    target = await session.get(WorkspaceMember, member_id)
    if target is None or target.workspace_id != member.workspace_id:
        raise HTTPException(status_code=404, detail="member not found")
    for s in body:
        if s.end_min <= s.start_min:
            raise HTTPException(status_code=422, detail="shift end must be after start")
    await session.execute(
        delete(MemberShift).where(
            MemberShift.workspace_id == member.workspace_id,
            MemberShift.member_id == member_id,
        )
    )
    out: list[MemberShift] = []
    for s in body:
        shift = MemberShift(
            workspace_id=member.workspace_id,
            member_id=member_id,
            weekday=s.weekday,
            start_min=s.start_min,
            end_min=s.end_min,
            timezone=s.timezone,
        )
        session.add(shift)
        out.append(shift)
    await session.commit()
    return [ShiftOut.model_validate(s) for s in out]
