"""Reports orchestration: query dispatch, async export jobs, frozen-config
public shares, AI-summary read (plan 附錄 B.4)."""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from arq.connections import RedisSettings, create_pool
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...analytics import export as export_svc
from ...models.reports import ReportAiSummary, ReportExport, ReportShare
from ...services.security import hash_password, verify_password
from ...settings import get_settings
from . import queries

REPORT_KEYS: frozenset[str] = frozenset(
    {
        "service-overview",
        "customers",
        "online-time",
        "summary",
        "channels",
        "ads/facebook",
        "ads/messenger",
    }
)

EXPORT_JOB = "run_report_export_task"

_arq_pool: Any = None


async def _get_arq_pool() -> Any:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _arq_pool


def validate_report_key(report_key: str) -> str:
    if report_key not in REPORT_KEYS:
        raise HTTPException(404, detail="unknown report")
    return report_key


# ==========================================================================
# query dispatch (used by both the auth'd endpoints and public shares)
# ==========================================================================
async def run_report(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    report_key: str,
    f: queries.Filters,
    *,
    dimension: str = "day",
) -> dict[str, Any]:
    if report_key == "service-overview":
        return await queries.service_overview(session, workspace_id, f)
    if report_key == "customers":
        return await queries.customers(session, workspace_id, f, dimension)
    if report_key == "online-time":
        return await queries.online_time(session, workspace_id, f)
    if report_key == "summary":
        return await queries.summary(session, workspace_id, f)
    if report_key == "channels":
        return await queries.channels(session, workspace_id, f)
    if report_key.startswith("ads/"):
        platform = "facebook" if report_key.endswith("facebook") else "messenger"
        return await queries.ads(session, workspace_id, platform, f)
    raise HTTPException(404, detail="unknown report")


# ==========================================================================
# export
# ==========================================================================
async def create_export(
    session: AsyncSession, workspace_id: uuid.UUID, report_key: str, config: dict[str, Any]
) -> uuid.UUID:
    row = ReportExport(
        workspace_id=workspace_id,
        report_key=report_key,
        status="pending",
        config=config,
    )
    session.add(row)
    await session.commit()
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job(EXPORT_JOB, str(row.id))
    except Exception:  # noqa: BLE001 — enqueue failure leaves a pending row a cron can pick up
        pass
    return row.id


async def get_export(
    session: AsyncSession, workspace_id: uuid.UUID, export_id: uuid.UUID
) -> dict[str, Any]:
    row = await session.get(ReportExport, export_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(404, detail="export not found")
    url = export_svc.signed_url_of(row) if row.status == "ready" else None
    return {"status": row.status, "url": url}


# ==========================================================================
# share (frozen config → public link)
# ==========================================================================
async def create_share(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    member_id: uuid.UUID | None,
    report_key: str,
    config: dict[str, Any],
    *,
    password: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, str]:
    token = secrets.token_urlsafe(24)
    row = ReportShare(
        token=token,
        workspace_id=workspace_id,
        report_key=report_key,
        report_config=config,  # frozen snapshot — re-run verbatim on read
        expires_at=expires_at,
        password_hash=hash_password(password) if password else None,
        created_by_member_id=member_id,
    )
    session.add(row)
    await session.commit()
    base = get_settings().public_base_url.rstrip("/")
    return {"token": token, "url": f"{base}/shared-report/{token}"}


async def run_shared_report(
    session: AsyncSession, token: str, *, password: str | None = None
) -> dict[str, Any]:
    row = await session.get(ReportShare, token)
    if row is None:
        raise HTTPException(404, detail="share not found")
    if row.expires_at is not None and row.expires_at < datetime.now(UTC):
        raise HTTPException(410, detail="share expired")
    if row.password_hash is not None:
        if not password or not verify_password(password, row.password_hash):
            raise HTTPException(401, detail={"code": "password_required"})
    cfg = dict(row.report_config or {})
    f = queries.filters_from_config(cfg)
    data = await run_report(
        session, row.workspace_id, row.report_key, f, dimension=cfg.get("dimension", "day")
    )
    return {"report_key": row.report_key, "config": cfg, "data": data}


# ==========================================================================
# ai summary read
# ==========================================================================
async def latest_ai_summary(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            select(ReportAiSummary)
            .where(ReportAiSummary.workspace_id == workspace_id)
            .order_by(ReportAiSummary.day.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"day": datetime.now(UTC).date().isoformat(), "text": ""}
    return {"day": row.day.isoformat(), "text": row.text}
