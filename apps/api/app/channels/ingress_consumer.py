"""Channel-ingress consumer process — the `channel-ingress` compose service
entrypoint (`python -m apps.api.app.channels.ingress_consumer`).

Dedicated long-running consumer tailing every ingress:{channel_type} Redis
stream with a blocking read (run_ingress_loop), so webhook inbound
(WhatsApp/Telegram/Meta/...) lands in the inbox within milliseconds. Without
this process the only consumer is the worker's ingress_drain_task cron —
every 15s — which shows up to the agent as a 10-15s inbox delay.

The cron stays registered as the at-least-once safety net: both share the
same consumer GROUP (each entry is delivered to exactly one consumer) and
MessageDedup makes redelivery harmless.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from ..db import session_factory as make_session_factory
from ..services.redis_client import close_redis, get_redis
from .ingress_pipeline import run_ingress_loop

log = logging.getLogger("smartchat.channels.ingress_consumer")


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sf = make_session_factory()
    redis = get_redis()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows dev
            loop.add_signal_handler(sig, stop.set)
    log.info("channel-ingress consumer up (blocking tail on ingress:* streams)")
    try:
        await run_ingress_loop(sf, redis, stop=stop)
    finally:
        await close_redis()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
