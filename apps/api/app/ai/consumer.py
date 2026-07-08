"""AI-agent consumer process — the `ai-agent` compose service entrypoint
(`python -m apps.api.app.ai.consumer`).

Dedicated long-running consumer (group 'ai-agent' on events:conversation)
driving agent_runtime.ai_agent_consumer: inbound contact messages on
handler=ai_agent conversations → handle_ai_inbound; human replies →
pause_ai_for_human. Independent of the flow-engine's consumer group —
handle_ai_inbound is idempotent, so both may run.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from ..db import session_factory as make_session_factory
from ..services.redis_client import close_redis, get_redis
from .agent_runtime import ai_agent_consumer

log = logging.getLogger("smartchat.ai.consumer")


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
    log.info("ai-agent consumer up (group=ai-agent, stream=events:conversation)")
    try:
        await ai_agent_consumer(sf, redis, stop=stop)
    finally:
        await close_redis()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
