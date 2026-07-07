"""Async report CSV export → MinIO → signed URL (plan 附錄 B.4).

The router creates a ``report_exports`` row (status=pending) and enqueues an ARQ
task; this module streams the frozen report query to a CSV, uploads it to MinIO
under ``exports/{ws}/{id}.csv`` and stores a short-lived presigned GET URL the
browser can open directly (no auth header, unlike the files endpoint).

A missing MinIO / object-store dependency degrades the job to ``failed`` with a
readable error instead of crashing the worker.
"""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.reports import ReportExport
from ..modules.reports import queries
from ..settings import get_settings

log = logging.getLogger("smartchat.analytics.export")

SIGNED_URL_TTL = timedelta(hours=1)


async def build_report_table(
    session: AsyncSession, workspace_id: uuid.UUID, report_key: str, cfg: dict
) -> list[dict]:
    """Return the tabular rows for a report (the shape the UI table shows)."""
    f = queries.filters_from_config(cfg)
    if report_key == "service-overview":
        data = await queries.service_overview(session, workspace_id, f)
        return data["trend"]
    if report_key == "customers":
        data = await queries.customers(session, workspace_id, f, cfg.get("dimension", "day"))
        return data["detail"]["rows"]
    if report_key == "summary":
        return (await queries.summary(session, workspace_id, f))["agents"]
    if report_key == "channels":
        return (await queries.channels(session, workspace_id, f))["rows"]
    if report_key == "online-time":
        return (await queries.online_time(session, workspace_id, f))["rows"]
    if report_key in ("ads/facebook", "ads/messenger"):
        platform = "facebook" if report_key.endswith("facebook") else "messenger"
        return (await queries.ads(session, workspace_id, platform, f))["rows"]
    return []


def rows_to_csv(rows: list[dict]) -> bytes:
    """UTF-8-BOM CSV (Excel opens CJK correctly). Header = union of keys."""
    header: list[str] = []
    for r in rows:
        for k in r:
            if k not in header:
                header.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header or ["(empty)"], extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8-sig")


def _presign(key: str) -> str:
    from ..channels.media import get_media_store

    store = get_media_store()
    client = store._get_client()  # reuse the configured MinIO client
    store._ensure_bucket_sync()
    return client.presigned_get_object(
        get_settings().minio_bucket, key, expires=SIGNED_URL_TTL
    )


def _upload(key: str, data: bytes) -> None:
    from ..channels.media import get_media_store

    get_media_store()._put_sync(key, data, "text/csv")


async def run_export(
    session_factory: async_sessionmaker[AsyncSession], export_id: uuid.UUID
) -> str:
    """Execute one export job. Returns the final status."""
    import asyncio

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(ReportExport, export_id)
            if row is None:
                return "missing"
            row.status = "running"
            workspace_id = row.workspace_id
            report_key = row.report_key
            cfg = dict(row.config or {})

    try:
        async with session_factory() as session:
            rows = await build_report_table(session, workspace_id, report_key, cfg)
        data = rows_to_csv(rows)
        key = f"exports/{workspace_id}/{export_id}.csv"
        await asyncio.to_thread(_upload, key, data)
        url = await asyncio.to_thread(_presign, key)
        async with session_factory() as session:
            async with session.begin():
                row = await session.get(ReportExport, export_id)
                if row is not None:
                    row.status = "ready"
                    row.storage_key = key
                    row.row_count = len(rows)
                    row.config = {**cfg, "_signed_url": url, "_signed_at": datetime.now(UTC).isoformat()}
        return "ready"
    except Exception as e:  # noqa: BLE001 — surface as a failed job, never crash the worker
        log.exception("report export %s failed", export_id)
        async with session_factory() as session:
            async with session.begin():
                row = await session.get(ReportExport, export_id)
                if row is not None:
                    row.status = "failed"
                    row.error = str(e)[:500]
        return "failed"


def signed_url_of(row: ReportExport) -> str | None:
    """Re-presign on read if the stored URL is stale (or presign lazily)."""
    if row.status != "ready" or not row.storage_key:
        return None
    stored = (row.config or {}).get("_signed_url")
    signed_at = (row.config or {}).get("_signed_at")
    if stored and signed_at:
        try:
            age = datetime.now(UTC) - datetime.fromisoformat(signed_at)
            if age < SIGNED_URL_TTL - timedelta(minutes=5):
                return stored
        except (ValueError, TypeError):
            pass
    try:
        return _presign(row.storage_key)
    except Exception:  # noqa: BLE001
        return stored
