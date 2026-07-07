"""Workspace settings (/api/v1/settings): inbox & reception config incl.
auto-assign toggles, auto-close, offline reply mode, timezone."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ...db import get_session
from ...deps import MemberContext, current_member, require_permission
from ...models.tenancy import Workspace
from ..workspaces.service import default_settings

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class AssignmentSettings(BaseModel):
    auto_assign: bool = True
    mode: str = Field(default="round_robin", pattern="^(round_robin|least_busy)$")
    prefer_ai_member: bool = True
    prefer_bot: bool = True
    keep_managed: bool = False


class AutoCloseSettings(BaseModel):
    enabled: bool = True
    days: int = Field(default=1, ge=0, le=365)
    hours: int = Field(default=0, ge=0, le=23)
    minutes: int = Field(default=0, ge=0, le=59)


class NotificationSettings(BaseModel):
    sound: bool = True
    desktop: bool = True


class WorkspaceSettingsModel(BaseModel):
    timezone: str = "UTC"
    assignment: AssignmentSettings = Field(default_factory=AssignmentSettings)
    auto_close: AutoCloseSettings = Field(default_factory=AutoCloseSettings)
    offline_reply_mode: str = Field(default="email", pattern="^(email|widget)$")
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)


def merged_settings(stored: dict[str, Any] | None) -> WorkspaceSettingsModel:
    """Defaults ⊕ stored — tolerates settings written before new keys existed."""
    base = default_settings()
    for k, v in (stored or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return WorkspaceSettingsModel.model_validate(base)


@router.get("", response_model=WorkspaceSettingsModel)
async def get_settings_endpoint(
    member: MemberContext = Depends(current_member),
) -> WorkspaceSettingsModel:
    return merged_settings(member.workspace.settings)


@router.put("", response_model=WorkspaceSettingsModel)
async def put_settings(
    body: WorkspaceSettingsModel,
    member: MemberContext = Depends(require_permission("settings.manage")),
    session: AsyncSession = Depends(get_session),
) -> WorkspaceSettingsModel:
    ws = await session.get(Workspace, member.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    ws.settings = body.model_dump()
    flag_modified(ws, "settings")
    await session.commit()
    return merged_settings(ws.settings)
