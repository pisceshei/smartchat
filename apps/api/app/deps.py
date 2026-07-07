"""FastAPI dependencies: current_user (JWT), current_member (workspace scope
via X-Workspace-Id), require_permission (RBAC), and OpenAPI token auth.

Permission keys are dotted ("inbox.view_all"); a role's permissions list may
contain exact keys, module wildcards ("inbox.*"), or "*" (super admin).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .models.members import Role, User, WorkspaceMember
from .models.misc import ApiToken
from .models.tenancy import Workspace
from .services.security import TokenInvalid, hash_api_token, verify_token

_bearer = HTTPBearer(auto_error=False)

WORKSPACE_HEADER = "X-Workspace-Id"

# canonical permission catalog (roles may hold any subset, wildcards allowed)
PERMISSION_KEYS: tuple[str, ...] = (
    "inbox.view_mine",
    "inbox.view_all",
    "inbox.reply",
    "inbox.assign",
    "inbox.close",
    "contacts.view",
    "contacts.edit",
    "contacts.export",
    "contacts.merge",
    "channels.manage",
    "flows.manage",
    "broadcasts.manage",
    "reports.view",
    "members.manage",
    "roles.manage",
    "settings.manage",
    "billing.manage",
    "developer.manage",
    "workspace.manage",
)


def has_permission(granted: set[str] | list[str], key: str) -> bool:
    """Pure check: exact key, "module.*" wildcard, or global "*"."""
    granted_set = set(granted)
    if "*" in granted_set or key in granted_set:
        return True
    module, _, _ = key.partition(".")
    return f"{module}.*" in granted_set


@dataclass
class MemberContext:
    member: WorkspaceMember
    workspace: Workspace
    user: User
    permissions: set[str] = field(default_factory=set)

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspace.id

    @property
    def member_id(self) -> uuid.UUID:
        return self.member.id

    def can(self, key: str) -> bool:
        return has_permission(self.permissions, key)


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = verify_token(creds.credentials, expected_type="access")
    except TokenInvalid as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
    try:
        user_id = uuid.UUID(claims["sub"])
    except ValueError as e:
        raise HTTPException(status_code=401, detail="invalid token subject") from e
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or disabled")
    return user


def get_workspace_id(
    x_workspace_id: str | None = Header(default=None, alias=WORKSPACE_HEADER),
) -> uuid.UUID:
    if not x_workspace_id:
        raise HTTPException(status_code=400, detail=f"{WORKSPACE_HEADER} header required")
    try:
        return uuid.UUID(x_workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid {WORKSPACE_HEADER}") from e


async def current_member(
    workspace_id: uuid.UUID = Depends(get_workspace_id),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MemberContext:
    """Resolve the caller's membership in the requested workspace. Loads the
    role fresh each request (role edits take effect immediately)."""
    row = (
        await session.execute(
            select(WorkspaceMember, Workspace, Role)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .outerjoin(Role, Role.id == WorkspaceMember.role_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.status == "active",
                WorkspaceMember.member_type == "human",
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=403, detail="not a member of this workspace")
    member, workspace, role = row
    if workspace.status != "active":
        raise HTTPException(status_code=403, detail=f"workspace {workspace.status}")
    perms: set[str] = set(role.permissions) if role is not None else set()
    return MemberContext(member=member, workspace=workspace, user=user, permissions=perms)


def require_permission(*keys: str):
    """Dependency factory: member must hold ALL listed permission keys.
        @router.get(..., dependencies=[Depends(require_permission("inbox.view_all"))])
    or  member: MemberContext = Depends(require_permission("contacts.edit"))
    """

    async def dep(member: MemberContext = Depends(current_member)) -> MemberContext:
        for key in keys:
            if not member.can(key):
                raise HTTPException(
                    status_code=403,
                    detail={"code": "permission_denied", "permission": key},
                )
        return member

    return dep


# --------------------------------------------------------------------------
# OpenAPI (developer surface) token auth
# --------------------------------------------------------------------------
@dataclass
class ApiTokenContext:
    token: ApiToken

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.token.workspace_id


async def current_api_token(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiTokenContext:
    """Project token auth for /api/openapi/*: `X-Api-Token: sct_…` (or
    Authorization: Bearer). Only the sha256 hash is stored."""
    raw = request.headers.get("X-Api-Token")
    if not raw:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
    if not raw:
        raise HTTPException(status_code=401, detail="missing api token")
    row = (
        await session.execute(select(ApiToken).where(ApiToken.token_hash == hash_api_token(raw)))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None or row.revoked_at is not None or (row.expires_at and row.expires_at < now):
        raise HTTPException(status_code=401, detail="invalid api token")
    row.last_used_at = now
    return ApiTokenContext(token=row)
