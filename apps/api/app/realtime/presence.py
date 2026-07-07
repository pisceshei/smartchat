"""Presence (plan A.8): Redis SET EX 60 + 25s client heartbeats + keyspace
expiry watcher (debounced 5s) emitting presence.member/visitor offline events.

Keys embed the workspace — `presence:m:{ws}:{member_id}` — because expiry
notifications carry ONLY the key, and the offline event must target a
workspace stream. The assignment engine reads these same keys (online state
and routability are one source of truth). Values are "online" or "away"
(manual toggle); away agents stay connected but are not routable.

Multi-instance safe: gateways never DEL presence on disconnect (the member may
hold sockets on other replicas) — they just stop refreshing and the TTL takes
care of it; quick reconnects within 60s never flap offline at all, and the 5s
debounce re-checks EXISTS before emitting.

Presence events are ephemeral (persist=False): they are state snapshots, and
churning them through the replay stream would evict real message history.
Clients re-fetch presence via REST on resync.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Literal
from uuid import UUID

import redis.asyncio as aioredis

from .protocol import AUDIENCE_AGENTS
from .publisher import publish

log = logging.getLogger("smartchat.realtime.presence")

PRESENCE_TTL = 60  # seconds
HEARTBEAT_INTERVAL = 25  # clients ping every 25s; each ping refreshes the TTL
OFFLINE_DEBOUNCE = 5.0  # seconds between key expiry and the offline event

STATE_ONLINE = "online"
STATE_AWAY = "away"

_EXPIRED_PATTERN = "__keyevent@*__:expired"  # requires notify-keyspace-events Ex

PresenceKind = Literal["member", "visitor"]


def member_key(workspace_id: UUID | str, member_id: UUID | str) -> str:
    return f"presence:m:{workspace_id}:{member_id}"


def visitor_key(workspace_id: UUID | str, channel_identity_id: UUID | str) -> str:
    return f"presence:v:{workspace_id}:{channel_identity_id}"


def parse_presence_key(key: str) -> tuple[PresenceKind, str, str] | None:
    """presence:m:{ws}:{id} → ("member", ws, id); None for foreign keys."""
    parts = key.split(":")
    if len(parts) != 4 or parts[0] != "presence" or parts[1] not in ("m", "v"):
        return None
    kind: PresenceKind = "member" if parts[1] == "m" else "visitor"
    return kind, parts[2], parts[3]


# --------------------------------------------------------------------------
# state transitions (called by the gateway)
# --------------------------------------------------------------------------
async def _set_state(
    redis: aioredis.Redis, key: str, state: str, *, ttl: int = PRESENCE_TTL
) -> str | None:
    """SET ... EX ttl GET — returns the previous value (None = was offline)."""
    return await redis.set(key, state, ex=ttl, get=True)


async def mark_member_online(
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
    *,
    display_name: str = "",
) -> bool:
    """Refresh presence; emits presence.member online only on the actual
    offline→online transition. Returns True if a transition happened."""
    prev = await _set_state(redis, member_key(workspace_id, member_id), STATE_ONLINE)
    if prev is None:
        await _emit_member(redis, workspace_id, member_id, STATE_ONLINE, display_name=display_name)
        return True
    if prev == STATE_AWAY:
        # explicit re-activity while away keeps "away" (manual toggle wins)
        await redis.set(member_key(workspace_id, member_id), STATE_AWAY, ex=PRESENCE_TTL)
    return False


async def heartbeat_member(
    redis: aioredis.Redis, workspace_id: UUID, member_id: UUID, *, display_name: str = ""
) -> None:
    """25s ping path: refresh TTL preserving state; resurrect as online if the
    key somehow expired between pings."""
    if not await redis.expire(member_key(workspace_id, member_id), PRESENCE_TTL):
        await mark_member_online(redis, workspace_id, member_id, display_name=display_name)


async def set_member_away(
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
    away: bool,
    *,
    display_name: str = "",
) -> None:
    """Manual away toggle."""
    state = STATE_AWAY if away else STATE_ONLINE
    prev = await _set_state(redis, member_key(workspace_id, member_id), state)
    if prev != state:
        await _emit_member(redis, workspace_id, member_id, state, display_name=display_name)


async def member_state(redis: aioredis.Redis, workspace_id: UUID, member_id: UUID) -> str | None:
    return await redis.get(member_key(workspace_id, member_id))


async def mark_visitor_online(
    redis: aioredis.Redis, workspace_id: UUID, channel_identity_id: UUID
) -> bool:
    prev = await _set_state(redis, visitor_key(workspace_id, channel_identity_id), STATE_ONLINE)
    if prev is None:
        await _emit_visitor(redis, workspace_id, channel_identity_id, STATE_ONLINE)
        return True
    return False


async def heartbeat_visitor(
    redis: aioredis.Redis, workspace_id: UUID, channel_identity_id: UUID
) -> None:
    if not await redis.expire(visitor_key(workspace_id, channel_identity_id), PRESENCE_TTL):
        await mark_visitor_online(redis, workspace_id, channel_identity_id)


async def visitor_state(
    redis: aioredis.Redis, workspace_id: UUID, channel_identity_id: UUID
) -> str | None:
    return await redis.get(visitor_key(workspace_id, channel_identity_id))


async def _emit_member(
    redis: aioredis.Redis, workspace_id: UUID, member_id: UUID, state: str, *, display_name: str = ""
) -> None:
    data: dict[str, Any] = {"member_id": str(member_id), "state": state}
    if display_name:
        data["display_name"] = display_name
    await publish(workspace_id, "presence.member", data, (AUDIENCE_AGENTS,), persist=False, redis=redis)


async def _emit_visitor(
    redis: aioredis.Redis, workspace_id: UUID, channel_identity_id: UUID, state: str
) -> None:
    await publish(
        workspace_id,
        "presence.visitor",
        {"channel_identity_id": str(channel_identity_id), "state": state},
        (AUDIENCE_AGENTS,),
        channel_identity_id=channel_identity_id,
        persist=False,
        redis=redis,
    )


# --------------------------------------------------------------------------
# expiry watcher (one per gateway process; emits are idempotent for clients)
# --------------------------------------------------------------------------
class PresenceWatcher:
    """psubscribe __keyevent@*__:expired → debounce 5s → re-check EXISTS →
    emit presence.* offline. Requires redis `notify-keyspace-events Ex`
    (already set in infra/docker-compose.yml)."""

    def __init__(self, redis: aioredis.Redis, *, debounce: float = OFFLINE_DEBOUNCE):
        self._redis = redis
        self._debounce = debounce
        self._pubsub: Any = None
        self._reader: asyncio.Task | None = None
        self._pending: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(_EXPIRED_PATTERN)
        self._stopping.clear()
        self._reader = asyncio.create_task(self._read_loop(), name="presence-expiry-watcher")

    async def stop(self) -> None:
        self._stopping.set()
        for t in (self._reader, *self._tasks):
            if t is not None:
                t.cancel()
        if self._reader is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        self._tasks.clear()
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None

    async def _read_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("presence watcher read failed; retrying")
                await asyncio.sleep(1.0)
                continue
            if msg is None or msg.get("type") != "pmessage":
                continue
            key = msg["data"]
            if isinstance(key, bytes):
                key = key.decode()
            if parse_presence_key(key) is None or key in self._pending:
                continue
            self._pending.add(key)
            task = asyncio.create_task(self._debounced_offline(key))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _debounced_offline(self, key: str) -> None:
        try:
            await asyncio.sleep(self._debounce)
        finally:
            self._pending.discard(key)
        try:
            if await self._redis.exists(key):
                return  # came back within the debounce window — no flap
            parsed = parse_presence_key(key)
            if parsed is None:  # pragma: no cover — filtered upstream
                return
            kind, ws_raw, id_raw = parsed
            workspace_id, entity_id = UUID(ws_raw), UUID(id_raw)
            if kind == "member":
                await _emit_member(self._redis, workspace_id, entity_id, "offline")
            else:
                await _emit_visitor(self._redis, workspace_id, entity_id, "offline")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("presence offline emit failed for %s", key)
