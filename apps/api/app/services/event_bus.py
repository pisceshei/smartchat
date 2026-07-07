"""Transactional outbox → events table → Redis Streams relay (plan B.0).

Write path: emit(session, event) inserts an events row inside the caller's
transaction — if the business write rolls back, the event never happened.
The relay tails unpublished rows in id (=UUIDv7 time) order and XADDs each to
its topic-family stream; `published` + FOR UPDATE SKIP LOCKED makes the tail
race-free across relay replicas (a pure id-watermark could skip events from
transactions that commit late). The events table doubles as the reports raw
store, so rows stay after publishing (13-month retention via partition drops).

Consumers: Redis consumer groups (flow-engine / rollup / notifier / webhook)
via ensure_group / read_batch / ack — at-least-once, restart-safe.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import redis.asyncio as aioredis
from py_contracts.events import EVENT_TOPICS, STREAMS, Actor, Event
from redis import exceptions as redis_ex
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.misc import EventRow

log = logging.getLogger("smartchat.event_bus")

FALLBACK_STREAM = "events:misc"
STREAM_MAXLEN = 100_000
RELAY_WATERMARK_KEY = "events:relay:last_id"  # observability only


def stream_for(event_type: str) -> str:
    """Topic-family stream for a type; unregistered types (e.g. custom timer
    kinds) go to the fallback stream instead of crashing the relay."""
    family = EVENT_TOPICS.get(event_type)
    return STREAMS.get(family, FALLBACK_STREAM) if family else FALLBACK_STREAM


def event_to_row(event: Event) -> EventRow:
    return EventRow(
        id=event.id,
        workspace_id=event.workspace_id,
        type=event.type,
        occurred_at=event.occurred_at,
        actor_type=event.actor.type,
        actor_id=event.actor.id,
        conversation_id=event.conversation_id,
        contact_id=event.contact_id,
        channel_type=event.channel_type,
        channel_account_id=event.channel_account_id,
        payload=event.payload,
        published=False,
    )


def row_to_event(row: EventRow) -> Event:
    return Event(
        id=row.id,
        workspace_id=row.workspace_id,
        type=row.type,
        occurred_at=row.occurred_at,
        actor=Actor(type=row.actor_type, id=row.actor_id),  # type: ignore[arg-type]
        conversation_id=row.conversation_id,
        contact_id=row.contact_id,
        channel_type=row.channel_type,
        channel_account_id=row.channel_account_id,
        payload=row.payload or {},
    )


def encode_fields(event: Event) -> dict[str, str]:
    """Flat string fields for XADD; `data` carries the full envelope."""
    return {
        "id": str(event.id),
        "ws": str(event.workspace_id),
        "type": event.type,
        "data": event.model_dump_json(),
    }


def decode_event(fields: dict[str, Any]) -> Event:
    data = fields.get("data") or fields.get(b"data")
    if data is None:
        raise ValueError("stream entry missing data field")
    return Event.model_validate_json(data)


# --------------------------------------------------------------------------
# producer side
# --------------------------------------------------------------------------
async def emit(session: AsyncSession, event: Event) -> EventRow:
    """Write the outbox row in the caller's transaction. No Redis I/O here —
    the relay publishes after commit."""
    row = event_to_row(event)
    session.add(row)
    return row


async def emit_many(session: AsyncSession, events: list[Event]) -> list[EventRow]:
    rows = [event_to_row(e) for e in events]
    session.add_all(rows)
    return rows


# --------------------------------------------------------------------------
# relay (runs in beat)
# --------------------------------------------------------------------------
async def relay_once(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    batch: int = 256,
) -> int:
    """Publish one batch of unpublished events. Returns rows published."""
    async with session_factory() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(EventRow)
                    .where(EventRow.published.is_(False))
                    .order_by(EventRow.id)
                    .limit(batch)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            if not rows:
                return 0
            pipe = redis.pipeline(transaction=False)
            for row in rows:
                event = row_to_event(row)
                pipe.xadd(
                    stream_for(row.type),
                    encode_fields(event),
                    maxlen=STREAM_MAXLEN,
                    approximate=True,
                )
            await pipe.execute()
            await session.execute(
                update(EventRow)
                .where(EventRow.id.in_([r.id for r in rows]))
                .values(published=True)
            )
            await redis.set(RELAY_WATERMARK_KEY, str(rows[-1].id))
            return len(rows)


async def relay(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    batch: int = 256,
    poll_interval: float = 0.25,
    stop: asyncio.Event | None = None,
) -> None:
    """Tail loop. Crash of a replica is safe: rows stay unpublished and the
    next pass (or replica) picks them up — at-least-once into the streams."""
    while stop is None or not stop.is_set():
        try:
            n = await relay_once(session_factory, redis, batch=batch)
        except Exception:  # noqa: BLE001 — relay must survive anything
            log.exception("event relay pass failed")
            n = 0
        if n < batch:
            try:
                if stop is not None:
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                else:
                    await asyncio.sleep(poll_interval)
            except TimeoutError:
                pass


# --------------------------------------------------------------------------
# consumer-group helpers
# --------------------------------------------------------------------------
async def ensure_group(redis: aioredis.Redis, stream: str, group: str) -> None:
    """Create the consumer group (and stream) if missing; idempotent."""
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except redis_ex.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def read_batch(
    redis: aioredis.Redis,
    streams: list[str],
    group: str,
    consumer: str,
    *,
    count: int = 64,
    block_ms: int = 5000,
) -> list[tuple[str, str, Event]]:
    """Read new entries for this consumer. Returns (stream, entry_id, event)
    triples; poison entries are logged, acked and skipped."""
    resp = await redis.xreadgroup(
        group, consumer, {s: ">" for s in streams}, count=count, block=block_ms
    )
    out: list[tuple[str, str, Event]] = []
    for stream, entries in resp or []:
        for entry_id, fields in entries:
            try:
                out.append((stream, entry_id, decode_event(fields)))
            except Exception:  # noqa: BLE001
                log.exception("poison stream entry %s on %s", entry_id, stream)
                await ack(redis, stream, group, entry_id)
    return out


async def ack(redis: aioredis.Redis, stream: str, group: str, *entry_ids: str) -> None:
    if entry_ids:
        await redis.xack(stream, group, *entry_ids)


async def ensure_all_groups(redis: aioredis.Redis, group: str) -> None:
    """Convenience: register a group on every topic-family stream + fallback."""
    for stream in [*STREAMS.values(), FALLBACK_STREAM]:
        await ensure_group(redis, stream, group)
