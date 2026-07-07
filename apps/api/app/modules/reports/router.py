"""報告 (reports / analytics) API — plan 附錄 B.4 + live-captured Pro layouts.

Nav: 服務概覽 / 客戶分析 / 綜合報表 / 渠道分析 / 廣告分析 / 在線時長 / AI 分析,
each with a shared 時間範圍·間隔·社媒·帳號·成員 filter bar, async CSV export,
and a public frozen-config share link.

All endpoints are ``/api/v1/reports/*``, Bearer-JWT + X-Workspace-Id + the
``reports.view`` permission — except the public ``/api/v1/shared-report/{token}``
which re-runs a frozen config with no auth.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, require_permission
from . import queries, service

router = APIRouter(prefix="/api/v1", tags=["reports"])

_view = require_permission("reports.view")
_CUST_DIMS = ("member", "channel", "account", "day", "week", "month", "hour")


# ==========================================================================
# shared filter parsing
# ==========================================================================
def _filters(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    interval: str | None = Query(default=None),
    channel_type: str | None = Query(default=None),
    channel_account_id: str | None = Query(default=None),
    member_id: str | None = Query(default=None),
) -> queries.Filters:
    return queries.parse_filters(
        from_=from_,
        to=to,
        interval=interval,
        channel_type=channel_type,
        channel_account_id=channel_account_id,
        member_id=member_id,
    )


# ==========================================================================
# report endpoints
# ==========================================================================
@router.get("/reports/service-overview")
async def service_overview(
    f: queries.Filters = Depends(_filters),
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await queries.service_overview(session, member.workspace_id, f)


@router.get("/reports/customers")
async def customers(
    dimension: str = Query(default="day"),
    f: queries.Filters = Depends(_filters),
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    dim = dimension if dimension in _CUST_DIMS else "day"
    return await queries.customers(session, member.workspace_id, f, dim)


@router.get("/reports/online-time")
async def online_time(
    f: queries.Filters = Depends(_filters),
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await queries.online_time(session, member.workspace_id, f)


@router.get("/reports/summary")
async def summary(
    f: queries.Filters = Depends(_filters),
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await queries.summary(session, member.workspace_id, f)


@router.get("/reports/channels")
async def channels(
    f: queries.Filters = Depends(_filters),
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await queries.channels(session, member.workspace_id, f)


@router.get("/reports/ads/facebook")
async def ads_facebook(
    f: queries.Filters = Depends(_filters),
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await queries.ads(session, member.workspace_id, "facebook", f)


@router.get("/reports/ads/messenger")
async def ads_messenger(
    f: queries.Filters = Depends(_filters),
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await queries.ads(session, member.workspace_id, "messenger", f)


@router.get("/reports/ai-summary")
async def ai_summary(
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await service.latest_ai_summary(session, member.workspace_id)


# ==========================================================================
# export (async CSV → signed MinIO URL)
# ==========================================================================
@router.post("/reports/{report:path}/export")
async def create_export(
    report: str,
    body: dict[str, Any],
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    service.validate_report_key(report)
    f = queries.parse_filters(
        from_=body.get("from"),
        to=body.get("to"),
        interval=body.get("interval"),
        channel_type=body.get("channel_type"),
        channel_account_id=body.get("channel_account_id"),
        member_id=body.get("member_id"),
    )
    cfg = queries.config_dict(f, dimension=body.get("dimension"))
    export_id = await service.create_export(session, member.workspace_id, report, cfg)
    return {"job_id": str(export_id)}


@router.get("/reports/exports/{job_id}")
async def export_status(
    job_id: uuid.UUID,
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await service.get_export(session, member.workspace_id, job_id)


# ==========================================================================
# share (frozen config → public token)
# ==========================================================================
class ShareIn(BaseModel):
    config: dict[str, Any]
    password: str | None = None


@router.post("/reports/{report:path}/share")
async def create_share(
    report: str,
    body: ShareIn,
    member: MemberContext = Depends(_view),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    service.validate_report_key(report)
    cfg = dict(body.config or {})
    # normalise the client filter payload through the parser, then re-freeze
    f = queries.parse_filters(
        from_=cfg.get("from"),
        to=cfg.get("to"),
        interval=cfg.get("interval"),
        channel_type=cfg.get("channel_type"),
        channel_account_id=cfg.get("channel_account_id"),
        member_id=cfg.get("member_id"),
    )
    frozen = queries.config_dict(f, dimension=cfg.get("dimension"))
    return await service.create_share(
        session, member.workspace_id, member.member_id, report, frozen, password=body.password
    )


# ==========================================================================
# public shared report (NO auth — frozen config re-run)
# ==========================================================================
@router.get("/shared-report/{token}")
async def shared_report(
    token: str,
    password: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await service.run_shared_report(session, token, password=password)
