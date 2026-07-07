"""Analytics ARQ tasks + crons (plan 附錄 B.4).

Registered by importing this module from jobs.worker (append-only). Provides:
  - rollup_incremental_task — fold new events into hourly aggs (+ hot presence)
  - rollup_consume_task      — drain the 'rollup' stream group as a wake signal
  - presence_reconcile_task  — maintain agent_presence_sessions from Redis keys
  - rollup_nightly_task      — 48h re-aggregation + distinct-count day tables
  - ai_summary_nightly_task  — day-over-day LLM digest (Pro+, 20 points)
  - run_report_export_task   — async CSV export → MinIO signed URL

Crons run in UTC; per-workspace localisation happens inside the jobs (day
tables use each workspace's tz). The rollup + nightly share a Redis lock so
they never race.
"""
from __future__ import annotations

import uuid
from typing import Any

from arq import cron

from ..jobs.worker import register_cron, task
from . import ai_summary, daily, export, presence_tracker, rollup


@task
async def rollup_incremental_task(ctx: dict[str, Any]) -> int:
    return await rollup.run_incremental(ctx["session_factory"], ctx["redis"])


@task
async def rollup_consume_task(ctx: dict[str, Any]) -> int:
    return await rollup.run_rollup_consumer(ctx["session_factory"], ctx["redis"])


@task
async def presence_reconcile_task(ctx: dict[str, Any]) -> dict[str, int]:
    return await presence_tracker.reconcile(ctx["session_factory"], ctx["redis"])


@task
async def rollup_nightly_task(ctx: dict[str, Any]) -> int:
    folded = await rollup.run_nightly(ctx["session_factory"], ctx["redis"])
    await daily.run_daily(ctx["session_factory"])
    return folded


@task
async def ai_summary_nightly_task(ctx: dict[str, Any]) -> int:
    return await ai_summary.run_nightly_ai(ctx["session_factory"], ctx["redis"])


@task
async def run_report_export_task(ctx: dict[str, Any], export_id: str) -> str:
    return await export.run_export(ctx["session_factory"], uuid.UUID(export_id))


# every-minute rollup + presence maintenance (a dedicated consumer process may
# run rollup_consume_task in a tight loop for lower latency)
register_cron(cron(rollup_incremental_task, second={0}, run_at_startup=False))
register_cron(cron(presence_reconcile_task, second={30}, run_at_startup=False))
# nightly 03:xx UTC: late-event re-aggregation + distinct day tables + AI digest
register_cron(cron(rollup_nightly_task, hour={3}, minute={5}, run_at_startup=False))
register_cron(cron(ai_summary_nightly_task, hour={3}, minute={20}, run_at_startup=False))
