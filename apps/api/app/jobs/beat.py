"""Beat process: long-running asyncio loops (plan B.0).

- outbox relay: events table → Redis Streams (the bus)
- timer poller: Redis ZSET hot window + PG truth, 1s tick, boot reseed
- usage-counter flush: Redis deltas → usage_counters every 30s
- monthly AI-point grants: hourly idempotent sweep; nightly (03:xx UTC)
  balance reconcile (ledger → Redis)

Run: `python -m apps.api.app.jobs.beat`
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime

from sqlalchemy import select

from ..db import session_factory
from ..models.tenancy import Workspace
from ..services import event_bus, points, quotas, timers
from ..services.redis_client import close_redis, get_redis

log = logging.getLogger("smartchat.beat")

USAGE_FLUSH_INTERVAL_S = 30
GRANTS_INTERVAL_S = 3600
RECONCILE_HOUR_UTC = 3


async def _usage_flush_loop(sf, redis, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            n = await quotas.flush_usage_counters(sf, redis)
            if n:
                log.debug("flushed %d usage metrics", n)
        except Exception:  # noqa: BLE001
            log.exception("usage flush pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=USAGE_FLUSH_INTERVAL_S)
        except TimeoutError:
            pass


async def _grants_loop(sf, redis, stop: asyncio.Event) -> None:
    last_reconcile_day: str | None = None
    while not stop.is_set():
        try:
            granted = await points.run_monthly_grants(sf, redis)
            if granted:
                log.info("monthly grants applied to %d workspaces", granted)
        except Exception:  # noqa: BLE001
            log.exception("monthly grants pass failed")
        now = datetime.now(UTC)
        day = now.strftime("%Y-%m-%d")
        if now.hour == RECONCILE_HOUR_UTC and day != last_reconcile_day:
            try:
                async with sf() as session:
                    ws_ids = (
                        (await session.execute(select(Workspace.id).where(Workspace.status == "active")))
                        .scalars()
                        .all()
                    )
                    for ws_id in ws_ids:
                        await points.reconcile_balance(session, redis, ws_id)
                last_reconcile_day = day
                log.info("nightly points reconcile done for %d workspaces", len(ws_ids))
            except Exception:  # noqa: BLE001
                log.exception("nightly reconcile failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=GRANTS_INTERVAL_S)
        except TimeoutError:
            pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sf = session_factory()
    redis = get_redis()
    stop = asyncio.Event()

    def _request_stop(*_: object) -> None:
        log.info("shutdown requested")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # Windows
            signal.signal(sig, _request_stop)

    log.info("beat up: relay + timer poller + usage flush + grants")
    await asyncio.gather(
        event_bus.relay(sf, redis, stop=stop),
        timers.poller(sf, redis, stop=stop),
        _usage_flush_loop(sf, redis, stop),
        _grants_loop(sf, redis, stop),
    )
    await close_redis()


if __name__ == "__main__":
    asyncio.run(main())
