"""Billing ARQ tasks (plan P3): subscription expiry sweep.

Registered by importing this module from ``jobs.worker`` (append-only, same
pattern as ``ai.jobs``). The beat process is the primary scheduler; this cron is
the safety-net that downgrades lapsed subscriptions to Free hourly.
"""
from __future__ import annotations

from typing import Any

from arq import cron

from ..jobs.worker import register_cron, task
from . import subscription as sub_svc


@task
async def expire_subscriptions_task(ctx: dict[str, Any]) -> int:
    """Downgrade every subscription past its current_period_end to Free."""
    return await sub_svc.expire_due_subscriptions(ctx["session_factory"], ctx["redis"])


# hourly expiry sweep (minute offset avoids colliding with the grants cron @ :17)
register_cron(cron(expire_subscriptions_task, minute=41, run_at_startup=False))
