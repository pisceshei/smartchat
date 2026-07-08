"""Device-bridge QR endpoints (WhatsApp App / LINE App).

The *create* step shares the ``POST /api/v1/channels/{channel_type}/accounts``
path and is dispatched from channels/router.py's connect_account (which routes
whatsapp_app / line_app into ``devices.service.provision_device``). This router
adds the QR-lifecycle endpoints that hang off a created account:

  GET  /api/v1/channels/{channel_type}/{account_id}/qr      current QR string
  GET  /api/v1/channels/{channel_type}/{account_id}/status  poll + refresh health
  POST /api/v1/channels/{channel_type}/{account_id}/logout  end session (terminal)

``channel_type`` ∈ {whatsapp_app, line_app}; the extra path segment keeps these
distinct from the channels router's ``/{channel_type}/accounts`` route.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, require_permission
from . import service

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


@router.get("/{channel_type}/{account_id}/qr")
async def get_device_qr(
    channel_type: str,
    account_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await service.get_qr(session, member, channel_type, account_id)


@router.get("/{channel_type}/{account_id}/status")
async def get_device_status(
    channel_type: str,
    account_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await service.refresh_status(session, member, channel_type, account_id)


@router.post("/{channel_type}/{account_id}/logout")
async def logout_device(
    channel_type: str,
    account_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await service.logout(session, member, channel_type, account_id)
