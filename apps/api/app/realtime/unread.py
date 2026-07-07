"""Per-member unread counters (plan A.8).

Hot path: Redis hash `unread:{ws}:{member_id}` {conversation_id: n}. Truth
source: conversation_reads cursors + messages — rebuild-on-miss recomputes the
hash (UUIDv7 message ids are time-ordered, so `id > last_read_message_id` is
the after-cursor predicate).

The message pipeline (inbox module) calls incr_unread when an inbound customer
message lands; the gateway's read frame calls advance_read_cursor, which
upserts the cursor row, zeroes the hash field and emits the delta. Every
mutation emits a persisted `unread.changed` event targeted at that member only
(audience member:{id}) so all their devices converge, including via resume
replay.

Per-tab totals are a pure fold (tab_totals) over the hash given the caller's
conversation→tab classification — the inbox module owns tab semantics
(mine/unassigned/all/…), not this layer.
"""
from __future__ import annotations

import logging
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.conversations import Conversation, ConversationRead
from ..models.messaging import Message
from .protocol import member_audience
from .publisher import publish

log = logging.getLogger("smartchat.realtime.unread")

UNREAD_KEY_TTL = 3 * 86400  # idle hashes expire → next reader rebuilds
_SENTINEL = "_"  # marks a hash as "built" even when all counts are zero


def unread_key(workspace_id: UUID | str, member_id: UUID | str) -> str:
    return f"unread:{workspace_id}:{member_id}"


def tab_totals(
    unread_map: dict[str, int],
    conv_tabs: dict[str, str],
    *,
    default_tab: str = "other",
) -> dict[str, int]:
    """Pure per-tab fold. conv_tabs: conversation_id → tab label (caller-owned
    classification). Result includes the grand total under "_total"."""
    totals: dict[str, int] = {}
    for conv_id, n in unread_map.items():
        tab = conv_tabs.get(conv_id, default_tab)
        totals[tab] = totals.get(tab, 0) + n
    totals["_total"] = sum(unread_map.values())
    return totals


# --------------------------------------------------------------------------
# redis hot path
# --------------------------------------------------------------------------
async def get_unread_map(
    redis: aioredis.Redis, workspace_id: UUID, member_id: UUID
) -> dict[str, int]:
    raw = await redis.hgetall(unread_key(workspace_id, member_id))
    return {k: int(v) for k, v in raw.items() if k != _SENTINEL}


async def total_unread(redis: aioredis.Redis, workspace_id: UUID, member_id: UUID) -> int:
    return sum((await get_unread_map(redis, workspace_id, member_id)).values())


async def get_or_rebuild(
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
    *,
    session: AsyncSession | None = None,
) -> dict[str, int]:
    """Rebuild-on-miss: if the hash is gone (expired / redis flushed) and a DB
    session is available, recompute from conversation_reads."""
    key = unread_key(workspace_id, member_id)
    if not await redis.exists(key) and session is not None:
        return await rebuild(session, redis, workspace_id, member_id)
    return await get_unread_map(redis, workspace_id, member_id)


async def incr_unread(
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
    conversation_id: UUID,
    n: int = 1,
    *,
    session: AsyncSession | None = None,
    emit: bool = True,
) -> int:
    """Called by the message pipeline for each member who should see the
    conversation as unread. Returns the new per-conversation count."""
    key = unread_key(workspace_id, member_id)
    if session is not None and not await redis.exists(key):
        await rebuild(session, redis, workspace_id, member_id)
    pipe = redis.pipeline(transaction=False)
    pipe.hincrby(key, str(conversation_id), n)
    pipe.hsetnx(key, _SENTINEL, 0)
    pipe.expire(key, UNREAD_KEY_TTL)
    count = int((await pipe.execute())[0])
    if emit:
        await _emit_changed(redis, workspace_id, member_id, conversation_id, count)
    return count


async def clear_unread(
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
    conversation_id: UUID,
    *,
    emit: bool = True,
) -> None:
    key = unread_key(workspace_id, member_id)
    removed = await redis.hdel(key, str(conversation_id))
    if emit and removed:
        await _emit_changed(redis, workspace_id, member_id, conversation_id, 0)


async def _emit_changed(
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
    conversation_id: UUID,
    count: int,
) -> None:
    total = await total_unread(redis, workspace_id, member_id)
    await publish(
        workspace_id,
        "unread.changed",
        {
            "member_id": str(member_id),
            "conversation_id": str(conversation_id),
            "count": count,
            "total": total,
        },
        (member_audience(member_id),),
        conversation_id=conversation_id,
        redis=redis,
    )


# --------------------------------------------------------------------------
# truth source: conversation_reads + messages
# --------------------------------------------------------------------------
async def rebuild(
    session: AsyncSession,
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
) -> dict[str, int]:
    """Recompute unread counts for the member's open assigned conversations:
    inbound customer messages (never notes) after the member's read cursor
    (or all of them when no cursor exists yet)."""
    stmt = (
        select(Conversation.id, func.count(Message.id))
        .select_from(Conversation)
        .outerjoin(
            ConversationRead,
            and_(
                ConversationRead.conversation_id == Conversation.id,
                ConversationRead.member_id == member_id,
            ),
        )
        .join(
            Message,
            and_(
                Message.conversation_id == Conversation.id,
                Message.workspace_id == workspace_id,
                Message.direction == "in",
                Message.is_note.is_(False),
                or_(
                    ConversationRead.last_read_message_id.is_(None),
                    Message.id > ConversationRead.last_read_message_id,
                ),
            ),
        )
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.status == "open",
            Conversation.assignee_member_id == member_id,
        )
        .group_by(Conversation.id)
    )
    counts = {str(conv_id): int(n) for conv_id, n in (await session.execute(stmt)).all() if n}

    key = unread_key(workspace_id, member_id)
    pipe = redis.pipeline(transaction=True)
    pipe.delete(key)
    pipe.hset(key, mapping={_SENTINEL: 0, **counts})
    pipe.expire(key, UNREAD_KEY_TTL)
    await pipe.execute()
    return counts


async def advance_read_cursor(
    session: AsyncSession,
    redis: aioredis.Redis,
    workspace_id: UUID,
    member_id: UUID,
    conversation_id: UUID,
    message_id: UUID | None = None,
    *,
    emit: bool = True,
) -> None:
    """Gateway read frame → upsert conversation_reads + zero the hash field +
    emit unread.changed {count: 0}. Caller owns the transaction (commit)."""
    stmt = pg_insert(ConversationRead).values(
        conversation_id=conversation_id,
        member_id=member_id,
        workspace_id=workspace_id,
        last_read_message_id=message_id,
        last_read_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ConversationRead.conversation_id, ConversationRead.member_id],
        set_={
            # None message_id = "mark read" without moving the cursor backwards
            "last_read_message_id": func.coalesce(
                stmt.excluded.last_read_message_id, ConversationRead.last_read_message_id
            ),
            "last_read_at": func.now(),
        },
    )
    await session.execute(stmt)
    await clear_unread(redis, workspace_id, member_id, conversation_id, emit=emit)
