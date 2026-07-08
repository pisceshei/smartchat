"""ARQ worker (plan B.0: unified async worker framework).

Other modules register work by importing TASKS / CRON_JOBS (or the
@task decorator) and appending their coroutines BEFORE the worker starts —
WorkerSettings.functions references these lists, so registration is a plain
import-time append:

    from apps.api.app.jobs.worker import task

    @task
    async def send_broadcast_chunk(ctx, run_id: str, chunk: list[str]) -> None: ...

Run: `arq apps.api.app.jobs.worker.WorkerSettings`
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from ..db import session_factory
from ..services import points, quotas
from ..services.redis_client import close_redis, get_redis
from ..settings import get_settings

log = logging.getLogger("smartchat.worker")

TaskFn = Callable[..., Awaitable[Any]]

# registries other packages import and extend (append-only, before worker boot)
TASKS: list[TaskFn] = []
CRON_JOBS: list[Any] = []


def task(fn: TaskFn) -> TaskFn:
    """Decorator: register an ARQ task function."""
    if fn not in TASKS:
        TASKS.append(fn)
    return fn


def register_cron(job: Any) -> None:
    CRON_JOBS.append(job)


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
async def startup(ctx: dict[str, Any]) -> None:
    ctx["session_factory"] = session_factory()
    ctx["redis"] = get_redis()
    log.info("worker up: %d tasks, %d cron jobs", len(TASKS), len(CRON_JOBS))


async def shutdown(ctx: dict[str, Any]) -> None:
    await close_redis()


# --------------------------------------------------------------------------
# built-in tasks
# --------------------------------------------------------------------------
@task
async def flush_usage_counters_task(ctx: dict[str, Any]) -> int:
    return await quotas.flush_usage_counters(ctx["session_factory"], ctx["redis"])


@task
async def monthly_grants_task(ctx: dict[str, Any]) -> int:
    return await points.run_monthly_grants(ctx["session_factory"], ctx["redis"])


@task
async def reconcile_points_task(ctx: dict[str, Any], workspace_id: str) -> int:
    import uuid

    async with ctx["session_factory"]() as session:
        return await points.reconcile_balance(session, ctx["redis"], uuid.UUID(workspace_id))


@task
async def flush_flow_stats_task(ctx: dict[str, Any]) -> int:
    """Flush the flow-engine's Redis hash counters into flow_stats_daily +
    re-materialise distinct-user counts (plan B.1: every 60s)."""
    from apps.flow_engine import stats as flow_stats

    return await flow_stats.flush(ctx["session_factory"], ctx["redis"])


# safety-net crons (beat is the primary scheduler; these cover beat downtime)
register_cron(cron(monthly_grants_task, hour=0, minute=17, run_at_startup=False))
# roll flow analytics once a minute (plan B.1 統計每 60s 落庫)
register_cron(cron(flush_flow_stats_task, second={0}, run_at_startup=False))

# P2 AI subsystem tasks (KB ingest, idle AI resume, AI event drain) register
# themselves via the @task/register_cron decorators on import.
from ..ai import jobs as _ai_jobs  # noqa: E402,F401

# P3 reports/analytics: rollup + presence + nightly + export tasks register via
# @task/register_cron on import.
from ..analytics import jobs as _analytics_jobs  # noqa: E402,F401

# P3 billing: hourly subscription-expiry sweep registers via @task/register_cron.
from ..billing import jobs as _billing_jobs  # noqa: E402,F401

# P3 broadcast/marketing: fan-out + scheduler + delivery-status drain + recycle
# purge + WhatsApp reconcile; EDM launch/poll. Register via @task/register_cron.
from ..marketing import fanout as _mkt_fanout  # noqa: E402,F401
from ..modules.edm import service as _edm_service  # noqa: E402,F401

# Channel I/O crons — WITHOUT this import the worker never runs them and every
# real channel silently breaks: ingress_drain (inbound ingress:* → inbox),
# outbox_sender (outbound → channels), email poll, stuck-send requeue. sender.py
# self-registers via _register_crons() on import; it was previously only pulled
# in lazily inside marketing.fanout, so the worker process never loaded it.
from ..channels import sender as _channel_sender  # noqa: E402,F401


class WorkerSettings:
    functions = TASKS
    cron_jobs = CRON_JOBS
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 300
    keep_result = 120
    retry_jobs = True
    max_tries = 5
