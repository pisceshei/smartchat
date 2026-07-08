"""Device-bridge provisioning lifecycle (WhatsApp App / LINE App).

Flow:
1. ``provision_device`` — create a ``ChannelAccount`` (status ``awaiting_qr``) +
   ``DeviceBridge`` row, wire ``config['bridge_url']`` to the per-device path so
   the outbound sender's BridgeAdapter reaches ``…/devices/{device_id}/send``,
   store the webhook secret as the bridge signing credential, then ask the
   whatsmeow bridge to create + start the device (begins QR login). If the bridge
   is offline/unconfigured the account is still created but degraded to status
   ``pending`` with a surfaced error (never a 500).
2. ``get_qr`` — proxy the bridge's current QR string for the client to render.
3. ``refresh_status`` — poll bridge health, mirror it onto ``channel_accounts``
   (``online→active`` / ``offline→disconnected``, same mapping the inbound
   account_status handler uses) + ``device_bridges``, and manage the send pause
   key so the account becomes sendable exactly when it is online.
4. ``logout`` / ``teardown`` — end the session (terminal; never auto re-pair).

``device_id`` == ``str(account.id)`` per the bridge contract.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.creds import set_credentials
from ...deps import MemberContext
from ...models.channels import ChannelAccount, DeviceBridge
from ...models.misc import AuditLog
from ...services.bridge_client import BridgeClient, BridgeError, get_bridge_client
from ...services.redis_client import get_redis
from ...settings import get_settings

# frontend channel name → device_bridges.bridge_type
BRIDGE_CHANNELS: dict[str, str] = {"whatsapp_app": "wa_app", "line_app": "line_app"}

# bridge health status → channel_accounts.status (mirrors ingress_pipeline
# _handle_account_status so the polled status matches the pushed one).
_ACCT_STATUS_MAP = {"online": "active", "offline": "disconnected"}
# bridge statuses that must pause the send queue (unsendable) / clear it.
_PAUSE_STATUSES = {"banned", "logged_out", "offline", "disconnected", "token_expired"}
_RESUME_STATUSES = {"online", "active"}


def _acct_status(bridge_status: str) -> str:
    return _ACCT_STATUS_MAP.get(bridge_status, bridge_status)[:16]


def serialize_device_account(acct: ChannelAccount, bridge: DeviceBridge | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(acct.id),
        "channel_type": acct.channel_type,
        "name": acct.name,
        "external_id": acct.external_id,
        "status": acct.status,
        "health": acct.health or {},
        "enabled": acct.enabled,
        "device_id": str(acct.id),
        "config": {k: v for k, v in (acct.config or {}).items() if "secret" not in k},
        "created_at": acct.created_at.isoformat() if acct.created_at else None,
    }
    if bridge is not None:
        out["bridge_status"] = bridge.status
        out["last_heartbeat_at"] = (
            bridge.last_heartbeat_at.isoformat() if bridge.last_heartbeat_at else None
        )
    return out


async def _bridge_for(session: AsyncSession, account_id: uuid.UUID) -> DeviceBridge | None:
    from sqlalchemy import select

    return (
        await session.execute(
            select(DeviceBridge).where(DeviceBridge.channel_account_id == account_id)
        )
    ).scalar_one_or_none()


async def load_bridge_account(
    session: AsyncSession, member: MemberContext, channel_type: str, account_id: uuid.UUID
) -> tuple[ChannelAccount, DeviceBridge | None]:
    """Fetch + authorize a device-bridge account for this workspace/channel."""
    if channel_type not in BRIDGE_CHANNELS:
        raise HTTPException(404, "not a device-bridge channel")
    acct = await session.get(ChannelAccount, account_id)
    if acct is None or acct.workspace_id != member.workspace.id or acct.channel_type != channel_type:
        raise HTTPException(404, "account not found")
    bridge = await _bridge_for(session, account_id)
    return acct, bridge


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------
async def provision_device(
    session: AsyncSession,
    member: MemberContext,
    channel_type: str,
    *,
    name: str,
    client: BridgeClient | None = None,
) -> dict[str, Any]:
    """Create the account + device_bridge and start QR login on the bridge.

    Caller (channels connect_account) has already enforced the channel quota.
    Commits the session. Degrades to status ``pending`` + surfaced error when the
    bridge is unreachable/unconfigured."""
    settings = get_settings()
    bridge_type = BRIDGE_CHANNELS[channel_type]
    webhook_secret = secrets.token_urlsafe(24)
    external_id = "dev_" + secrets.token_hex(8)

    acct = ChannelAccount(
        workspace_id=member.workspace.id,
        channel_type=channel_type,
        name=name or channel_type,
        external_id=external_id,
        config={},
        webhook_secret=webhook_secret,
        status="awaiting_qr",
        health={},
    )
    session.add(acct)
    await session.flush()
    device_id = str(acct.id)

    bridge_base = (settings.bridge_wa_url or "").rstrip("/")
    acct.config = {
        "bridge_type": bridge_type,
        # per-device path so BridgeAdapter.send hits …/devices/{device_id}/send
        "bridge_url": f"{bridge_base}/devices/{device_id}" if bridge_base else "",
        "webhook_secret": webhook_secret,
    }
    # webhook secret doubles as the outbound HMAC signing key (BridgeAdapter.send)
    await set_credentials(session, acct, {"bridge_token": webhook_secret})

    bridge = DeviceBridge(
        workspace_id=member.workspace.id,
        channel_account_id=acct.id,
        bridge_type=bridge_type,
        container_name=None,
        status="provisioning",
        config={"device_id": device_id},
    )
    session.add(bridge)

    client = client or get_bridge_client(settings)
    callback_url = f"{settings.public_base_url}/hooks/bridge/{webhook_secret}"
    error: str | None = None
    bridge_disabled = False
    try:
        res = await client.create_device(
            device_id, callback_url=callback_url, callback_secret=webhook_secret
        )
        bstatus = str(res.get("status") or "awaiting_qr")
        acct.status = _acct_status(bstatus) if bstatus != "awaiting_qr" else "awaiting_qr"
        bridge.status = bstatus[:16]
    except BridgeError as e:
        error = e.message
        bridge_disabled = e.disabled
        acct.status = "pending"
        bridge.status = "offline"

    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="device.provision",
            target_type="channel_account",
            target_id=str(acct.id),
            detail={"channel_type": channel_type, "bridge_error": error},
        )
    )
    await session.commit()

    out = serialize_device_account(acct, bridge)
    out["status"] = acct.status
    if error is not None:
        out["error"] = error
        out["bridge_disabled"] = bridge_disabled
    return out


# --------------------------------------------------------------------------
# QR + status
# --------------------------------------------------------------------------
async def get_qr(
    session: AsyncSession,
    member: MemberContext,
    channel_type: str,
    account_id: uuid.UUID,
    *,
    client: BridgeClient | None = None,
) -> dict[str, Any]:
    acct, bridge = await load_bridge_account(session, member, channel_type, account_id)
    client = client or get_bridge_client(get_settings())
    try:
        res = await client.get_qr(str(acct.id))
    except BridgeError as e:
        return {"qr": None, "status": acct.status, "error": e.message, "bridge_disabled": e.disabled}
    qr = res.get("qr")
    status = str(res.get("status") or acct.status)
    return {"qr": qr, "status": status}


async def refresh_status(
    session: AsyncSession,
    member: MemberContext,
    channel_type: str,
    account_id: uuid.UUID,
    *,
    client: BridgeClient | None = None,
) -> dict[str, Any]:
    """Poll bridge health, mirror onto the account + bridge rows, and manage the
    send pause key. Commits."""
    acct, bridge = await load_bridge_account(session, member, channel_type, account_id)
    client = client or get_bridge_client(get_settings())
    try:
        health = await client.get_health(str(acct.id))
    except BridgeError as e:
        out = serialize_device_account(acct, bridge)
        out["error"] = e.message
        out["bridge_disabled"] = e.disabled
        return out

    bstatus = str(health.get("status") or "offline")
    profile = {k: health[k] for k in ("jid", "phone", "pushname") if health.get(k)}
    acct.status = _acct_status(bstatus)
    acct.health = {**(acct.health or {}), "last_status": bstatus, **profile}
    if profile.get("pushname") and (not acct.name or acct.name == channel_type):
        acct.name = str(profile["pushname"])[:128]
    if bridge is not None:
        bridge.status = bstatus[:16]
        bridge.last_heartbeat_at = datetime.now(UTC)
    await session.commit()

    # keep the send queue's pause key consistent with the device state
    redis = get_redis()
    from ...channels.sender import pause_key

    if bstatus in _PAUSE_STATUSES:
        await redis.set(pause_key(account_id), bstatus, ex=1800)
    elif bstatus in _RESUME_STATUSES:
        await redis.delete(pause_key(account_id))

    out = serialize_device_account(acct, bridge)
    out["status"] = acct.status
    out["bridge_status"] = bstatus
    if profile:
        out["profile"] = profile
    return out


# --------------------------------------------------------------------------
# logout + teardown (terminal — never auto re-pair)
# --------------------------------------------------------------------------
async def logout(
    session: AsyncSession,
    member: MemberContext,
    channel_type: str,
    account_id: uuid.UUID,
    *,
    client: BridgeClient | None = None,
) -> dict[str, Any]:
    acct, bridge = await load_bridge_account(session, member, channel_type, account_id)
    client = client or get_bridge_client(get_settings())
    error: str | None = None
    try:
        await client.logout(str(acct.id))
    except BridgeError as e:
        error = e.message
    acct.status = "logged_out"
    if bridge is not None:
        bridge.status = "logged_out"
    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="device.logout",
            target_type="channel_account",
            target_id=str(acct.id),
            detail={"channel_type": channel_type},
        )
    )
    await session.commit()
    await get_redis().set(_pause_key(account_id), "logged_out", ex=1800)
    out = {"ok": True, "status": "logged_out"}
    if error is not None:
        out["error"] = error
    return out


def _pause_key(account_id: uuid.UUID) -> str:
    from ...channels.sender import pause_key

    return pause_key(account_id)


async def teardown_device(
    session: AsyncSession, acct: ChannelAccount, *, client: BridgeClient | None = None
) -> None:
    """Best-effort bridge delete when a device account is removed. Never raises —
    the account disable must succeed even if the bridge is down."""
    client = client or get_bridge_client(get_settings())
    try:
        await client.delete_device(str(acct.id))
    except BridgeError:
        pass
    bridge = await _bridge_for(session, acct.id)
    if bridge is not None:
        bridge.status = "offline"
