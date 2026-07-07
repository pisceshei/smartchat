"""Connection hub for the ws-gateway (plan A.8).

One Redis pub/sub reader per process; channels are subscribed with local
reference counting — a gateway instance only subscribes evtps:{ws} while it
actually holds connections for that workspace (and evtps:vis:{identity} per
visitor connection). No sticky sessions: N gateway replicas each subscribe
independently, so scaling out is just more containers.

Delivery: reader parses the envelope, applies the per-audience filter from
protocol.py per connection, and enqueues the client frame on the connection's
bounded queue. A slow consumer that overflows its queue is flagged; the pump
sends resync_required so the client REST-refetches instead of silently losing
events.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import redis.asyncio as aioredis

from .protocol import (
    AgentScope,
    ResumeAction,
    RtEvent,
    VisitorScope,
    filter_for_agent,
    filter_for_visitor,
    resume_decision,
    stream_key,
)
from .publisher import current_seq

try:  # uuid7 package exposes uuid7()
    from uuid_extensions import uuid7  # type: ignore
except ImportError:  # pragma: no cover
    from uuid6 import uuid7  # type: ignore

log = logging.getLogger("smartchat.realtime.hub")

SEND_QUEUE_MAX = 512
# permanent dummy subscription so the pub/sub reader always has ≥1 channel
_ANCHOR_CHANNEL = "evtps:__hub__"


@dataclass(eq=False)  # identity semantics — connections live in sets
class Connection:
    kind: Literal["agent", "visitor"]
    workspace_id: UUID
    scope: AgentScope | VisitorScope
    channels: tuple[str, ...]
    id: str = field(default_factory=lambda: uuid7().hex)
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=SEND_QUEUE_MAX)
    )
    overflowed: bool = False

    def frame_for(self, event: RtEvent) -> dict[str, Any] | None:
        if event.workspace_id != self.workspace_id:
            return None
        if self.kind == "agent":
            assert isinstance(self.scope, AgentScope)
            return filter_for_agent(event, self.scope)
        assert isinstance(self.scope, VisitorScope)
        return filter_for_visitor(event, self.scope)

    def enqueue(self, frame: dict[str, Any]) -> bool:
        """Best-effort enqueue; marks the connection for resync on overflow."""
        try:
            self.queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            self.overflowed = True
            return False


class Hub:
    """Per-process registry of live connections + refcounted pub/sub reader."""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
        self._by_channel: dict[str, set[Connection]] = {}
        self._lock = asyncio.Lock()
        self._pubsub: Any = None
        self._reader: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(_ANCHOR_CHANNEL)
        self._stopping.clear()
        self._reader = asyncio.create_task(self._read_loop(), name="hub-pubsub-reader")

    async def stop(self) -> None:
        self._stopping.set()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None

    # -- connection registry -------------------------------------------------
    async def attach(self, conn: Connection) -> None:
        async with self._lock:
            new_channels = []
            for ch in conn.channels:
                conns = self._by_channel.setdefault(ch, set())
                if not conns:
                    new_channels.append(ch)
                conns.add(conn)
            if new_channels and self._pubsub is not None:
                await self._pubsub.subscribe(*new_channels)

    async def detach(self, conn: Connection) -> None:
        async with self._lock:
            dead_channels = []
            for ch in conn.channels:
                conns = self._by_channel.get(ch)
                if conns is None:
                    continue
                conns.discard(conn)
                if not conns:
                    del self._by_channel[ch]
                    dead_channels.append(ch)
            if dead_channels and self._pubsub is not None:
                with contextlib.suppress(Exception):
                    await self._pubsub.unsubscribe(*dead_channels)

    def connection_count(self, workspace_id: UUID | None = None) -> int:
        seen: set[str] = set()
        n = 0
        for conns in self._by_channel.values():
            for c in conns:
                if c.id in seen:
                    continue
                seen.add(c.id)
                if workspace_id is None or c.workspace_id == workspace_id:
                    n += 1
        return n

    # -- fanout ---------------------------------------------------------------
    def route(self, channel: str, event: RtEvent) -> None:
        conns = self._by_channel.get(channel)
        if not conns:
            return
        for conn in tuple(conns):
            try:
                frame = conn.frame_for(event)
            except Exception:  # noqa: BLE001 — one bad filter must not kill fanout
                log.exception("audience filter failed for conn %s", conn.id)
                continue
            if frame is not None:
                conn.enqueue(frame)

    async def _read_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — reader must survive redis hiccups
                log.exception("hub pub/sub read failed; retrying")
                await asyncio.sleep(1.0)
                continue
            if msg is None or msg.get("type") not in ("message", "pmessage"):
                continue
            channel = msg["channel"]
            if isinstance(channel, bytes):
                channel = channel.decode()
            if channel == _ANCHOR_CHANNEL:
                continue
            try:
                event = RtEvent.model_validate_json(msg["data"])
            except Exception:  # noqa: BLE001
                log.exception("malformed realtime envelope on %s", channel)
                continue
            self.route(channel, event)

    # -- long-poll support ------------------------------------------------------
    async def wait_frames(self, conn: Connection, hold_seconds: float) -> list[dict[str, Any]]:
        """Block until the connection receives ≥1 frame or the hold elapses;
        drains whatever is queued at that moment (small batches per poll)."""
        try:
            async with asyncio.timeout(hold_seconds):
                first = await conn.queue.get()
        except TimeoutError:
            return []
        frames = [first]
        while True:
            try:
                frames.append(conn.queue.get_nowait())
            except asyncio.QueueEmpty:
                return frames


# --------------------------------------------------------------------------
# stream replay (resume protocol)
# --------------------------------------------------------------------------
async def collect_replay(
    redis: aioredis.Redis,
    workspace_id: UUID,
    resume_from: int,
    *,
    page: int = 256,
) -> tuple[ResumeAction, list[RtEvent], int]:
    """XRANGE-based replay: returns (decision, events with seq > resume_from
    ascending, safe cursor). Pages the stream backwards (newest first) and
    stops at the first entry ≤ resume_from — bounded by the gap size, not the
    stream depth. Stream insertion order == seq order (publisher Lua)."""
    stream = stream_key(workspace_id)
    current = await current_seq(redis, workspace_id)
    if resume_from >= current:
        return ResumeAction.NOOP, [], current

    oldest: int | None = None
    first = await redis.xrange(stream, count=1)
    if first:
        _, fields = first[0]
        raw = fields.get("seq") or fields.get(b"seq")
        oldest = int(raw) if raw is not None else None

    action = resume_decision(resume_from, current, oldest)
    if action is not ResumeAction.REPLAY:
        return action, [], current

    collected: list[RtEvent] = []
    max_id = "+"
    done = False
    while not done:
        entries = await redis.xrevrange(stream, max=max_id, min="-", count=page)
        if not entries:
            break
        for entry_id, fields in entries:
            try:
                ev = RtEvent.from_stream_fields(fields)
            except Exception:  # noqa: BLE001 — skip poison entries
                log.exception("poison stream entry %s on %s", entry_id, stream)
                continue
            if ev.seq is not None and ev.seq <= resume_from:
                done = True
                break
            collected.append(ev)
        else:
            last_id = entries[-1][0]
            if isinstance(last_id, bytes):
                last_id = last_id.decode()
            max_id = f"({last_id}"  # exclusive — continue past this page
            continue

    collected.sort(key=lambda e: e.seq or 0)
    cursor = max(current, collected[-1].seq or 0) if collected else current
    return ResumeAction.REPLAY, collected, cursor
