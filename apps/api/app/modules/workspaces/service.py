"""Workspace bootstrap shared by auth.register and workspaces.create."""
from __future__ import annotations

import copy
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...models.members import Role, User, WorkspaceMember
from ...models.tenancy import Workspace
from ...seed import ensure_plan
from ...services import points
from ...services.redis_client import get_redis
from ...services.security import get_cipher

log = logging.getLogger("smartchat.workspaces")

DEFAULT_WORKSPACE_SETTINGS: dict[str, Any] = {
    "timezone": "UTC",
    "assignment": {
        "auto_assign": True,  # 會話自動分配
        "mode": "round_robin",  # round_robin / least_busy
        "prefer_ai_member": True,  # 先 AI 員工後人類
        "prefer_bot": True,  # 優先機器人接待
        "keep_managed": False,  # 保持託管
    },
    "auto_close": {"enabled": True, "days": 1, "hours": 0, "minutes": 0},
    "offline_reply_mode": "email",  # widget 離線訪客回覆方式: email / widget
    "notifications": {"sound": True, "desktop": True},
}

SYSTEM_ROLES: list[dict[str, Any]] = [
    {"key": "super_admin", "name": "超級管理員", "permissions": ["*"]},
    {
        "key": "admin",
        "name": "管理員",
        "permissions": [
            "inbox.*", "contacts.*", "channels.manage", "flows.manage",
            "broadcasts.manage", "reports.view", "members.manage", "roles.manage",
            "settings.manage", "developer.manage", "workspace.manage",
        ],
    },
    {
        "key": "agent",
        "name": "客服",
        "permissions": [
            "inbox.view_mine", "inbox.reply", "inbox.close",
            "contacts.view", "contacts.edit", "reports.view",
        ],
    },
]


def default_settings() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_WORKSPACE_SETTINGS)


async def bootstrap_workspace(
    session: AsyncSession,
    *,
    name: str,
    owner: User,
    plan_code: str = "free",
) -> tuple[Workspace, WorkspaceMember]:
    """Create workspace + envelope data key + system roles + owner membership
    (super_admin). Caller commits. Best-effort initial points grant (the beat
    grants sweep is the safety net)."""
    await ensure_plan(session, plan_code)
    ws = Workspace(
        name=name,
        plan_code=plan_code,
        status="active",
        settings=default_settings(),
        owner_user_id=owner.id,
        data_key_enc=get_cipher().new_wrapped_data_key(),
    )
    session.add(ws)
    await session.flush()

    roles: dict[str, Role] = {}
    for spec in SYSTEM_ROLES:
        role = Role(
            workspace_id=ws.id,
            name=spec["name"],
            key=spec["key"],
            is_system=True,
            permissions=spec["permissions"],
        )
        session.add(role)
        roles[spec["key"]] = role
    await session.flush()

    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=owner.id,
        member_type="human",
        role_id=roles["super_admin"].id,
        display_name=owner.display_name or owner.email,
        status="active",
    )
    session.add(member)
    await session.flush()

    try:
        from ...models.tenancy import Plan

        plan = await session.get(Plan, plan_code)
        pts = int((plan.limits or {}).get("ai_points_monthly") or 0) if plan else 0
        if pts > 0:
            await points.grant_monthly(
                session, get_redis(), workspace_id=ws.id, points=pts,
                period_month=points.current_period(),
            )
    except Exception:  # noqa: BLE001 — Redis down: beat's hourly sweep will grant
        log.warning("initial points grant deferred for %s", ws.id, exc_info=True)

    return ws, member
