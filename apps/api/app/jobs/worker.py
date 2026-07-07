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


# safety-net crons (beat is the primary scheduler; these cover beat downtime)
register_cron(cron(monthly_grants_task, hour=0, minute=17, run_at_startup=False))


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
