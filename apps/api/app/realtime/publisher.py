"""Realtime event publisher (plan A.8) — THE entry point for inbox / channels
/ flow modules to push events at connected clients.

    from apps.api.app.realtime.publisher import publish
    await publish(ws_id, "message.created", data, audiences=[AUDIENCE_AGENTS,
                  visitor_audience(identity_id)], conversation_id=...,
                  channel_identity_id=identity_id)

Persisted path (default): a Lua script atomically INCRs seq:{ws} and XADDs the
envelope to evt:{ws} (MAXLEN ~10k) so stream order always matches seq order;
then PUBLISH evtps:{ws} wakes gateways, plus evtps:vis:{identity} for every
visitor:* audience so widget sockets never subscribe the workspace channel.

Ephemeral path (persist=False — typing, presence): pub/sub only, seq=None.

This is fanout to LIVE clients; the durable domain bus is services.event_bus
(outbox → events table → Redis Streams). Domain writes go through BOTH.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis

from ..services.redis_client import get_redis
from .protocol import (
    AUDIENCE_AGENTS,
    STREAM_MAXLEN,
    RtEvent,
    pubsub_key,
    seq_key,
    stream_key,
    visitor_pubsub_key,
    visitor_targets,
)

log = logging.getLogger("smartchat.realtime.publisher")

# KEYS[1]=seq:{ws} KEYS[2]=evt:{ws}  ARGV[1]=maxlen ARGV[2]=envelope json
# Atomic INCR+XADD keeps stream insertion order == seq order (replay relies
# on it to page backwards and stop at resume_from).
_PUBLISH_LUA = """
local seq = redis.call('INCR', KEYS[1])
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[1], '*', 'seq', seq, 'data', ARGV[2])
return seq
"""

_scripts: dict[int, Any] = {}


def _script(redis: aioredis.Redis) -> Any:
    script = _scripts.get(id(redis))
    if script is None:
        script = redis.register_script(_PUBLISH_LUA)
        _scripts[id(redis)] = script
    return script


async def publish(
    workspace_id: UUID,
    event_type: str,
    data: dict[str, Any],
    audiences: tuple[str, ...] | list[str] = (AUDIENCE_AGENTS,),
    *,
    conversation_id: UUID | None = None,
    channel_identity_id: UUID | None = None,
    persist: bool = True,
    redis: aioredis.Redis | None = None,
) -> RtEvent:
    """Publish one realtime event. Returns the envelope (with seq assigned
    when persisted). At-least-once: clients dedup by event_id."""
    r = redis if redis is not None else get_redis()
    event = RtEvent(
        workspace_id=workspace_id,
        type=event_type,
        data=data,
        audiences=list(audiences),
        conversation_id=conversation_id,
        channel_identity_id=channel_identity_id,
    )
    if persist:
        seq = await _script(r)(
            keys=[seq_key(workspace_id), stream_key(workspace_id)],
            args=[str(STREAM_MAXLEN), event.model_dump_json()],
        )
        event.seq = int(seq)

    payload = event.model_dump_json()
    pipe = r.pipeline(transaction=False)
    pipe.publish(pubsub_key(workspace_id), payload)
    # widget-scoped wakeups — one per addressed visitor identity
    seen: set[str] = set()
    for ident in visitor_targets(event.audiences):
        if ident not in seen:
            seen.add(ident)
            pipe.publish(visitor_pubsub_key(ident), payload)
    await pipe.execute()
    return event


async def current_seq(redis: aioredis.Redis, workspace_id: UUID) -> int:
    v = await redis.get(seq_key(workspace_id))
    return int(v) if v else 0
