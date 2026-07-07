"""Admin channel-account + widget management API.

Frontend contract (apps/web/src/api/endpoints.ts):
  GET    /api/v1/channels/accounts
  POST   /api/v1/channels/{channel_type}/accounts
  DELETE /api/v1/channels/accounts/{id}
  GET    /api/v1/widgets            POST /api/v1/widgets
  GET    /api/v1/widgets/{id}       PATCH/DELETE /api/v1/widgets/{id}

Credentials are envelope-encrypted at rest (channels.creds); responses never
echo secrets back. Connect performs a live credential validation through the
adapter before persisting, and registers provider webhooks where the
platform requires per-account URLs (telegram).
"""
from __future__ import annotations

import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.creds import set_credentials
from ...channels.registry import get_adapter, registered_channel_types
from ...db import get_session
from ...deps import MemberContext, require_permission
from ...models.channels import ChannelAccount, Widget
from ...models.misc import AuditLog
from ...services.quotas import effective_limits
from ...services.redis_client import get_redis
from ...settings import get_settings

router = APIRouter(prefix="/api/v1", tags=["channels"])

_CONNECTABLE = {
    "widget",
    "telegram",
    "whatsapp_cloud",
    "messenger",
    "instagram",
    "line_oa",
    "email",
}

# per-type credential fields accepted at connect time (everything else → config)
_CRED_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": ("bot_token",),
    "whatsapp_cloud": ("access_token",),
    "messenger": ("page_access_token",),
    "instagram": ("page_access_token",),
    "line_oa": ("channel_secret", "channel_access_token"),
    "email": ("imap_password", "smtp_password"),
}


def _serialize_account(acct: ChannelAccount) -> dict[str, Any]:
    return {
        "id": str(acct.id),
        "channel_type": acct.channel_type,
        "name": acct.name,
        "external_id": acct.external_id,
        "status": acct.status,
        "health": acct.health or {},
        "enabled": acct.enabled,
        "config": {k: v for k, v in (acct.config or {}).items() if "password" not in k},
        "created_at": acct.created_at.isoformat() if acct.created_at else None,
    }


# ------------------------------------------------------------------ accounts
@router.get("/channels/accounts")
async def list_accounts(
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(ChannelAccount)
                .where(ChannelAccount.workspace_id == member.workspace.id)
                .order_by(ChannelAccount.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_serialize_account(a) for a in rows]


class ConnectBody(BaseModel):
    name: str = Field(default="", max_length=128)
    credentials: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    external_id: str | None = None


async def _check_channel_quota(session: AsyncSession, member: MemberContext) -> None:
    limits = await effective_limits(session, get_redis(), member.workspace.id)
    cap = limits.get("channel_accounts")
    if cap is None:
        return
    count = (
        await session.execute(
            select(func.count())
            .select_from(ChannelAccount)
            .where(ChannelAccount.workspace_id == member.workspace.id)
        )
    ).scalar_one()
    if count >= int(cap):
        raise HTTPException(402, "channel account quota reached — upgrade your plan")


@router.post("/channels/{channel_type}/accounts")
async def connect_account(
    channel_type: str,
    body: ConnectBody,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if channel_type not in _CONNECTABLE or channel_type not in registered_channel_types():
        raise HTTPException(422, f"unsupported channel type: {channel_type}")
    await _check_channel_quota(session, member)

    settings = get_settings()
    webhook_secret = secrets.token_urlsafe(24)
    credentials = {k: v for k, v in body.credentials.items() if v is not None}
    config = dict(body.config)
    external_id = (body.external_id or "").strip()
    name = body.name.strip()
    health_detail: dict[str, Any] = {}

    if channel_type == "widget":
        raise HTTPException(422, "create widgets via POST /api/v1/widgets")

    adapter = get_adapter(channel_type)

    if channel_type == "telegram":
        token = credentials.get("bot_token", "")
        if not token:
            raise HTTPException(422, "bot_token required")
        try:
            me = await adapter.validate_token(token)
        except ValueError as e:
            raise HTTPException(422, f"telegram: {e}") from e
        external_id = str(me["id"])
        name = name or (me.get("username") or f"bot {external_id}")
        health_detail = {"username": me.get("username")}
        hook_url = f"{settings.public_base_url}/hooks/telegram/{webhook_secret}"
        if not await adapter.set_webhook(token, hook_url, webhook_secret):
            raise HTTPException(502, "telegram setWebhook failed")
    elif channel_type == "whatsapp_cloud":
        if not credentials.get("access_token") or not config.get("phone_number_id"):
            raise HTTPException(422, "access_token and phone_number_id required")
        external_id = str(config["phone_number_id"])
    elif channel_type in ("messenger", "instagram"):
        if not credentials.get("page_access_token") or not (external_id or config.get("page_id")):
            raise HTTPException(422, "page_access_token and page_id required")
        external_id = external_id or str(config.get("page_id"))
    elif channel_type == "line_oa":
        if not credentials.get("channel_secret") or not credentials.get("channel_access_token"):
            raise HTTPException(422, "channel_secret and channel_access_token required")
        external_id = external_id or str(config.get("channel_id") or "")
        if not external_id:
            raise HTTPException(422, "channel_id required")
    elif channel_type == "email":
        for k in ("imap_host", "smtp_host", "address"):
            if not config.get(k):
                raise HTTPException(422, f"{k} required")
        external_id = str(config["address"]).lower()

    dup = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type == channel_type,
                ChannelAccount.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, "this account is already connected")

    acct = ChannelAccount(
        workspace_id=member.workspace.id,
        channel_type=channel_type,
        name=name or channel_type,
        external_id=external_id,
        config=config,
        webhook_secret=webhook_secret,
        status="active",
        health=health_detail,
    )
    session.add(acct)
    await session.flush()
    if credentials:
        await set_credentials(session, acct, credentials)

    # live health probe (best effort; failures surface as status)
    try:
        result = await adapter.check_health(acct, credentials)
        acct.status = result.status if not result.ok else "active"
        acct.health = {**health_detail, **result.detail}
    except Exception:  # noqa: BLE001 — probe must not block connect
        acct.health = {**health_detail, "probe": "failed"}

    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="channel.connect",
            target_type="channel_account",
            target_id=str(acct.id),
            detail={"channel_type": channel_type, "external_id": external_id},
        )
    )
    await session.commit()
    return _serialize_account(acct)


@router.delete("/channels/accounts/{account_id}", status_code=204)
async def remove_account(
    account_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    acct = await session.get(ChannelAccount, account_id)
    if acct is None or acct.workspace_id != member.workspace.id:
        raise HTTPException(404, "account not found")
    acct.enabled = False
    acct.status = "disconnected"
    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="channel.disconnect",
            target_type="channel_account",
            target_id=str(acct.id),
            detail={"channel_type": acct.channel_type},
        )
    )
    await session.commit()


# ------------------------------------------------------------------- widgets
def _serialize_widget(w: Widget) -> dict[str, Any]:
    return {
        "id": str(w.id),
        "widget_key": w.widget_key,
        "name": w.name,
        "config": w.config or {},
        "allowed_domains": w.allowed_domains or [],
        "brand_removed": w.brand_removed,
        "enabled": w.enabled,
        "channel_account_id": str(w.channel_account_id) if w.channel_account_id else None,
        "embed_script_url": f"{get_settings().assets_base_url}/js/project_{w.widget_key}.js",
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


@router.get("/widgets")
async def list_widgets(
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(Widget)
                .where(Widget.workspace_id == member.workspace.id)
                .order_by(Widget.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_serialize_widget(w) for w in rows]


class WidgetCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    domain: str | None = None


@router.post("/widgets")
async def create_widget(
    body: WidgetCreateBody,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    limits = await effective_limits(session, get_redis(), member.workspace.id)
    cap = limits.get("widgets")
    if cap is not None:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Widget)
                .where(Widget.workspace_id == member.workspace.id)
            )
        ).scalar_one()
        if count >= int(cap):
            raise HTTPException(402, "widget quota reached — upgrade your plan")

    widget_key = secrets.token_hex(8)
    acct = ChannelAccount(
        workspace_id=member.workspace.id,
        channel_type="widget",
        name=body.name,
        external_id=widget_key,
        webhook_secret=secrets.token_urlsafe(24),
        status="active",
    )
    session.add(acct)
    await session.flush()
    widget = Widget(
        workspace_id=member.workspace.id,
        channel_account_id=acct.id,
        widget_key=widget_key,
        name=body.name,
        config={"brand": {"name": body.name}},
        allowed_domains=[body.domain] if body.domain else [],
    )
    session.add(widget)
    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="widget.create",
            target_type="widget",
            target_id=widget_key,
            detail={"name": body.name},
        )
    )
    await session.commit()
    return _serialize_widget(widget)


@router.get("/widgets/{widget_id}")
async def get_widget(
    widget_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    w = await session.get(Widget, widget_id)
    if w is None or w.workspace_id != member.workspace.id:
        raise HTTPException(404, "widget not found")
    return _serialize_widget(w)


class WidgetPatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    config: dict[str, Any] | None = None
    allowed_domains: list[str] | None = None
    brand_removed: bool | None = None
    enabled: bool | None = None


@router.patch("/widgets/{widget_id}")
async def update_widget(
    widget_id: uuid.UUID,
    body: WidgetPatchBody,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    w = await session.get(Widget, widget_id)
    if w is None or w.workspace_id != member.workspace.id:
        raise HTTPException(404, "widget not found")
    if body.brand_removed is not None and body.brand_removed:
        limits = await effective_limits(session, get_redis(), member.workspace.id)
        if not limits.get("brand_removal", False):
            raise HTTPException(402, "brand removal requires Pro plan or above")
        w.brand_removed = True
    elif body.brand_removed is not None:
        w.brand_removed = False
    if body.name is not None:
        w.name = body.name
    if body.config is not None:
        w.config = body.config
    if body.allowed_domains is not None:
        w.allowed_domains = body.allowed_domains
    if body.enabled is not None:
        w.enabled = body.enabled
    await session.commit()
    return _serialize_widget(w)


@router.delete("/widgets/{widget_id}", status_code=204)
async def delete_widget(
    widget_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    w = await session.get(Widget, widget_id)
    if w is None or w.workspace_id != member.workspace.id:
        raise HTTPException(404, "widget not found")
    w.enabled = False
    if w.channel_account_id:
        acct = await session.get(ChannelAccount, w.channel_account_id)
        if acct is not None:
            acct.enabled = False
    await session.commit()
