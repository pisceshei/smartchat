"""Recipient state machine + identity resolution + delivery-status bridge +
run/broadcast counter roll-up (plan B.3).

State machine (linear, plus two terminals)::

    planned → queued → sent → delivered → read
                 └────────────→ failed(reason)
    planned ──────────────────→ skipped(reason)

Delivery webhooks flow through the channel ingress as ``message.updated``
events carrying ``delivery_status``; the fan-out records a
``message_id → (run, recipient)`` map in Redis when it sends, and the
broadcast-status drain calls :func:`handle_delivery_status` to advance the
recipient. Counters are recomputed authoritatively from the recipient rows
(a race-free GROUP BY) and rolled into ``broadcast_runs`` and the denormalised
``broadcasts`` totals.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.contacts import ChannelIdentity
from ..models.conversations import Conversation
from ..models.marketing import Broadcast, BroadcastRecipient, BroadcastRun

# linear progression rank
_ORDER = {"planned": 0, "queued": 1, "sent": 2, "delivered": 3, "read": 4}
TERMINAL = frozenset({"read", "failed", "skipped"})
COUNTER_STATES = ("planned", "queued", "sent", "delivered", "read", "failed", "skipped")

MSGMAP_TTL_S = 14 * 24 * 3600  # keep the message→recipient map long enough for read receipts


def can_advance(current: str, new: str) -> bool:
    """Recipient states only move forward. ``skipped`` is reachable solely from
    ``planned``; ``failed`` from any non-terminal state that already tried."""
    if new == current:
        return False
    if new == "skipped":
        return current == "planned"
    if new == "failed":
        return current not in TERMINAL
    return _ORDER.get(new, -1) > _ORDER.get(current, -1)


# --------------------------------------------------------------------------
# message → recipient map (Redis) for the delivery-status bridge
# --------------------------------------------------------------------------
def msgmap_key(message_id: uuid.UUID | str) -> str:
    return f"bcast:msgmap:{message_id}"


async def record_send(
    redis: aioredis.Redis,
    *,
    message_id: uuid.UUID,
    run_id: uuid.UUID,
    recipient_id: uuid.UUID,
    recipient_created_at: datetime,
    workspace_id: uuid.UUID,
) -> None:
    await redis.set(
        msgmap_key(message_id),
        f"{run_id}|{recipient_id}|{recipient_created_at.isoformat()}|{workspace_id}",
        ex=MSGMAP_TTL_S,
    )


def _parse_msgmap(raw: str) -> tuple[uuid.UUID, uuid.UUID, datetime, uuid.UUID] | None:
    try:
        run_s, rid_s, created_s, ws_s = raw.split("|", 3)
        created = datetime.fromisoformat(created_s)
        return uuid.UUID(run_s), uuid.UUID(rid_s), created, uuid.UUID(ws_s)
    except (ValueError, AttributeError):
        return None


_STATUS_TO_STATE = {"sent": "sent", "delivered": "delivered", "read": "read", "failed": "failed"}
_STATE_TS_FIELD = {"sent": "sent_at", "delivered": "delivered_at", "read": "read_at"}


async def handle_delivery_status(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    message_id: uuid.UUID | str,
    status: str,
    error: str | None = None,
    provider_message_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Advance the recipient tied to ``message_id`` per a channel delivery
    status. Returns True if a recipient was advanced (so the drain can flush
    the run counters). Unknown messages / non-forward transitions are no-ops."""
    now = now or datetime.now(UTC)
    raw = await redis.get(msgmap_key(message_id))
    if not raw:
        return False
    parsed = _parse_msgmap(raw.decode() if isinstance(raw, bytes) else raw)
    if parsed is None:
        return False
    run_id, recipient_id, created_at, _ws = parsed
    new_state = _STATUS_TO_STATE.get(status)
    if new_state is None:
        return False
    rec = await session.get(BroadcastRecipient, (recipient_id, created_at))
    if rec is None or rec.run_id != run_id:
        return False
    if not can_advance(rec.state, new_state):
        return False
    rec.state = new_state
    if provider_message_id:
        rec.provider_message_id = provider_message_id
    ts_field = _STATE_TS_FIELD.get(new_state)
    if ts_field and getattr(rec, ts_field) is None:
        setattr(rec, ts_field, now)
    if new_state == "failed":
        rec.last_error = (error or "delivery_failed")[:2000]
    return True


# --------------------------------------------------------------------------
# identity + conversation resolution
# --------------------------------------------------------------------------
async def resolve_identity(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    channel_account_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> ChannelIdentity | None:
    """The contact's reachable identity on the broadcast's send channel (most
    recently seen wins when a contact has several)."""
    return (
        await session.execute(
            select(ChannelIdentity)
            .where(
                ChannelIdentity.workspace_id == workspace_id,
                ChannelIdentity.channel_account_id == channel_account_id,
                ChannelIdentity.contact_id == contact_id,
            )
            .order_by(ChannelIdentity.last_seen_at.desc().nulls_last(), ChannelIdentity.created_at.desc())
            .limit(1)
        )
    ).scalars().first()


async def get_or_create_conversation(
    session: AsyncSession,
    *,
    identity: ChannelIdentity,
    channel_account_id: uuid.UUID,
    channel_type: str,
) -> Conversation:
    """One persistent thread per channel identity (same invariant as the
    inbox); a broadcast reuses the existing conversation when there is one."""
    conv = (
        await session.execute(
            select(Conversation).where(Conversation.channel_identity_id == identity.id)
        )
    ).scalar_one_or_none()
    if conv is not None:
        return conv
    conv = Conversation(
        workspace_id=identity.workspace_id,
        channel_identity_id=identity.id,
        channel_account_id=channel_account_id,
        channel_type=channel_type,
        contact_id=identity.contact_id,
        status="open",
        handler="unassigned",
        session_count=1,
    )
    session.add(conv)
    await session.flush()
    return conv


# --------------------------------------------------------------------------
# counter roll-up (authoritative recompute)
# --------------------------------------------------------------------------
async def run_state_counts(session: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await session.execute(
            select(BroadcastRecipient.state, func.count())
            .where(BroadcastRecipient.run_id == run_id)
            .group_by(BroadcastRecipient.state)
        )
    ).all()
    counts = {s: 0 for s in COUNTER_STATES}
    for state, n in rows:
        counts[state] = int(n)
    return counts


async def flush_run_counters(
    session: AsyncSession, run_id: uuid.UUID, *, now: datetime | None = None
) -> dict[str, int]:
    """Recompute a run's tallies from its recipient rows and persist them.
    A run whose fan-out has drained (no planned/queued left) is marked
    ``completed``. Returns the counts."""
    now = now or datetime.now(UTC)
    counts = await run_state_counts(session, run_id)
    run = await session.get(BroadcastRun, run_id)
    if run is None:
        return counts
    run.planned = sum(counts[s] for s in COUNTER_STATES)  # total materialised (all states)
    run.sent = counts["sent"] + counts["delivered"] + counts["read"]
    run.delivered = counts["delivered"] + counts["read"]
    run.read = counts["read"]
    run.failed = counts["failed"]
    run.skipped = counts["skipped"]
    inflight = counts["planned"] + counts["queued"]
    if run.status == "running" and inflight == 0 and run.planned > 0:
        run.status = "completed"
        run.finished_at = now
    await rollup_broadcast(session, run.broadcast_id, now=now)
    return counts


async def rollup_broadcast(
    session: AsyncSession, broadcast_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """Sum a broadcast's run counters into its denormalised list-view totals
    and settle its lifecycle status when every run has finished."""
    agg = (
        await session.execute(
            select(
                func.coalesce(func.sum(BroadcastRun.planned), 0),
                func.coalesce(func.sum(BroadcastRun.sent), 0),
                func.coalesce(func.sum(BroadcastRun.delivered), 0),
                func.coalesce(func.sum(BroadcastRun.read), 0),
                func.coalesce(func.sum(BroadcastRun.failed), 0),
                func.coalesce(func.sum(BroadcastRun.skipped), 0),
                func.count(),
                func.count().filter(BroadcastRun.status.in_(("completed", "failed", "cancelled"))),
            ).where(BroadcastRun.broadcast_id == broadcast_id)
        )
    ).one()
    planned, sent, delivered, read, failed, skipped, total_runs, done_runs = agg
    bc = await session.get(Broadcast, broadcast_id)
    if bc is None:
        return
    bc.planned_count = int(planned)
    bc.sent_count = int(sent)
    bc.delivered_count = int(delivered)
    bc.read_count = int(read)
    bc.failed_count = int(failed)
    bc.skipped_count = int(skipped)
    # one_time settles to completed when its single run is done; recurring stays
    # 'running' until the scheduler exhausts the series (it sets completed).
    if bc.type == "one_time" and total_runs > 0 and done_runs == total_runs and bc.status == "running":
        bc.status = "completed"


def success_rate(sent: int, delivered: int) -> float:
    """delivered ÷ sent (plan B.3). 0 when nothing was sent."""
    return round(delivered / sent, 4) if sent > 0 else 0.0


async def bulk_update_state(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    recipient_ids: list[uuid.UUID],
    new_state: str,
) -> None:
    """Move a batch of recipients (by id) to ``new_state`` — used to flip a
    surviving chunk planned→queued before sending."""
    if not recipient_ids:
        return
    await session.execute(
        update(BroadcastRecipient)
        .where(
            BroadcastRecipient.run_id == run_id,
            BroadcastRecipient.id.in_(recipient_ids),
        )
        .values(state=new_state)
    )
