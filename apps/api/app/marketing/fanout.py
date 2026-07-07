"""Broadcast fan-out worker + scheduler + delivery-status drain (plan B.3).

Pipeline for one run (``run_broadcast`` ARQ task):

  timer/scheduler fires → quota (Pro ``broadcast`` gate) → materialise audience
  in 10k batches → suppression pass (dedupe / invalid_identity / blacklist /
  unsubscribe / weekly freq cap) writing planned|skipped recipients → 500-recipient
  chunks → send-window check (defer to next window or continue) → each recipient
  sends via ``messaging.send_message(sender_type='campaign')`` (same outbound
  pipeline + per-account Redis token bucket as the inbox) → recipient
  planned→queued→sent, provider linkage recorded for the delivery bridge →
  counters recomputed into broadcast_runs / broadcasts.

Pause/cancel stop pulling new chunks; resume re-enqueues and the run resumes at
its remaining ``planned`` recipients. Delivery webhooks flow back through the
channel ingress as ``message.updated`` events; ``broadcast_status_drain_task``
advances recipients to delivered/read/failed.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from arq.connections import RedisSettings, create_pool
from py_contracts.content import MessageContent
from py_contracts.events import Actor, Event
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..jobs.worker import register_cron, task
from ..models.contacts import ChannelIdentity, Contact
from ..models.marketing import Broadcast, BroadcastRecipient, BroadcastRun, MsgTemplate, Segment
from ..modules.msg_templates import service as tpl_svc
from ..services import event_bus, messaging, quotas
from ..services.redis_client import get_redis
from ..settings import get_settings
from . import recipients as rcpt
from . import schedule as sched
from . import suppression as supp
from .schedule import within_send_window

log = logging.getLogger("smartchat.marketing.fanout")

JOB_RUN = "run_broadcast"
AUDIENCE_BATCH = 10_000
_arq_pool: Any = None


async def _pool() -> Any:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _arq_pool


async def enqueue_fanout(run_id: uuid.UUID | str, *, defer_by: float | None = None) -> None:
    """Enqueue (or re-enqueue) a run's fan-out. Best-effort — a broadcast that
    can't reach the queue is retried by the scheduler tick."""
    try:
        pool = await _pool()
        await pool.enqueue_job(JOB_RUN, str(run_id), _defer_by=defer_by)
    except Exception:  # noqa: BLE001
        log.exception("enqueue_fanout failed run=%s", run_id)


# --------------------------------------------------------------------------
# run creation (used by the service + the scheduler)
# --------------------------------------------------------------------------
async def create_run(
    session: AsyncSession, broadcast: Broadcast, *, scheduled_at: datetime | None, now: datetime
) -> BroadcastRun:
    run = BroadcastRun(
        workspace_id=broadcast.workspace_id,
        broadcast_id=broadcast.id,
        status="pending",
        scheduled_at=scheduled_at or now,
    )
    session.add(run)
    await session.flush()
    return run


# --------------------------------------------------------------------------
# content rendering
# --------------------------------------------------------------------------
class _NoContent(Exception):
    pass


async def _build_content(
    session: AsyncSession,
    *,
    broadcast: Broadcast,
    template: MsgTemplate | None,
    contact: Contact | None,
) -> MessageContent:
    if template is not None:
        return await tpl_svc.build_content(
            session, template=template, variable_mapping=broadcast.variable_mapping or {},
            contact=contact, channel_type=broadcast.channel_type,
        )
    vm = broadcast.variable_mapping or {}
    if vm.get("content"):
        return MessageContent.model_validate(vm["content"])
    if vm.get("text") is not None:
        text = tpl_svc.substitute(str(vm["text"]), vm.get("vars", {}), contact)
        return MessageContent.model_validate({"blocks": [{"kind": "text", "text": text}]})
    raise _NoContent("broadcast has neither a template nor inline content")


# --------------------------------------------------------------------------
# the fan-out task
# --------------------------------------------------------------------------
@task
async def run_broadcast(ctx: dict[str, Any], run_id: str) -> str:
    sf: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    redis: aioredis.Redis = ctx["redis"]
    return await execute_run(sf, redis, uuid.UUID(run_id))


async def execute_run(
    sf: async_sessionmaker[AsyncSession], redis: aioredis.Redis, run_id: uuid.UUID
) -> str:
    now = datetime.now(UTC)
    # 1) load + guard + quota gate + start
    async with sf() as session:
        async with session.begin():
            run = await session.get(BroadcastRun, run_id)
            if run is None:
                return "missing"
            bc = await session.get(Broadcast, run.broadcast_id)
            if bc is None or bc.deleted_at is not None:
                run.status = "cancelled"
                return "broadcast_gone"
            if run.status in ("completed", "cancelled", "failed"):
                return f"noop:{run.status}"
            if bc.status in ("paused", "cancelled"):
                return f"broadcast_{bc.status}"
            limits = await quotas.effective_limits(session, redis, bc.workspace_id)
            if not quotas.limit_allows(limits, "broadcast"):
                run.status = "failed"
                run.finished_at = now
                bc.status = "failed" if bc.type == "one_time" else bc.status
                await event_bus.emit(session, _run_event(bc, run, "quota_denied"))
                return "quota_denied"
            run.status = "running"
            run.started_at = run.started_at or now
            if bc.status in ("draft", "scheduled"):
                bc.status = "running"
            workspace_id = bc.workspace_id
            channel_account_id = bc.channel_account_id
            channel_type = bc.channel_type
            segment_id = bc.segment_id
            template_id = bc.template_id

    if channel_account_id is None:
        await _finalize(sf, redis, run_id, reason="no_channel_account")
        return "no_channel_account"

    # 2) materialise audience + suppression (skip if already materialised)
    await _materialize(
        sf, redis, run_id=run_id, workspace_id=workspace_id,
        channel_account_id=channel_account_id, segment_id=segment_id, now=now,
    )

    # 3) send phase (chunked). Recover any interrupted 'queued' rows first.
    async with sf() as session:
        async with session.begin():
            await session.execute(
                update(BroadcastRecipient)
                .where(BroadcastRecipient.run_id == run_id, BroadcastRecipient.state == "queued")
                .values(state="planned")
            )

    settings = get_settings()
    chunk_size = settings.broadcast_chunk_size
    template: MsgTemplate | None = None

    while True:
        # pause / cancel check
        async with sf() as session:
            bc = await _load_broadcast_for_run(session, run_id)
            if bc is None:
                break
            if bc.status in ("paused", "cancelled"):
                log.info("broadcast %s %s — stop pulling chunks", bc.id, bc.status)
                await _flush(sf, run_id)
                return f"stopped:{bc.status}"
            if bc.deleted_at is not None:
                break
            send_rules = bc.send_rules or {}
            if template is None and template_id is not None:
                template = await session.get(MsgTemplate, template_id)
            # send-window: defer whole run to the next window
            if send_rules and not within_send_window(send_rules, datetime.now(UTC)):
                nxt = sched.next_window_start(send_rules, datetime.now(UTC))
                defer = max(1.0, (nxt - datetime.now(UTC)).total_seconds())
                await _flush(sf, run_id)
                await enqueue_fanout(run_id, defer_by=defer)
                log.info("broadcast %s deferred to %s", bc.id, nxt.isoformat())
                return "deferred"
            broadcast_snapshot = bc

        chunk = await _next_chunk(sf, run_id, chunk_size)
        if not chunk:
            break
        # planned → queued for this chunk
        async with sf() as session:
            async with session.begin():
                await rcpt.bulk_update_state(
                    session, run_id=run_id,
                    recipient_ids=[c[0] for c in chunk], new_state="queued",
                )
        for rid, created_at, contact_id, identity_id in chunk:
            await _send_one(
                sf, redis, run_id=run_id, broadcast=broadcast_snapshot, template=template,
                recipient_id=rid, recipient_created_at=created_at,
                contact_id=contact_id, identity_id=identity_id,
                channel_account_id=channel_account_id, channel_type=channel_type,
            )
        await _flush(sf, run_id)

    await _finalize(sf, redis, run_id)
    return "completed"


async def _materialize(
    sf: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    channel_account_id: uuid.UUID,
    segment_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Resolve the audience and write planned/skipped recipient rows. Idempotent
    per run: if the run already has recipients we assume materialisation is
    done (a resume)."""
    from ..modules.segments import service as seg_svc

    async with sf() as session:
        existing = (
            await session.execute(
                select(func.count()).select_from(BroadcastRecipient)
                .where(BroadcastRecipient.run_id == run_id)
            )
        ).scalar_one()
    if existing:
        return

    settings = get_settings()
    freq_cap = int(settings.broadcast_freq_cap_per_week or 0)
    seen_identities: set[uuid.UUID] = set()

    definition = None
    static_ids = None
    if segment_id is not None:
        async with sf() as session:
            seg = await session.get(Segment, segment_id)
            if seg is not None:
                definition = seg.definition
                static_ids = seg.snapshot_ids if seg.mode == "static" else None

    async with sf() as session:
        async for contact_batch in seg_svc.iter_audience(
            session, workspace_id=workspace_id, definition=definition,
            static_ids=static_ids, batch=AUDIENCE_BATCH,
        ):
            await _materialize_batch(
                sf, run_id=run_id, workspace_id=workspace_id,
                channel_account_id=channel_account_id, contact_ids=contact_batch,
                seen_identities=seen_identities, freq_cap=freq_cap, now=now,
            )
    await _flush(sf, run_id)


async def _materialize_batch(
    sf: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    channel_account_id: uuid.UUID,
    contact_ids: list[uuid.UUID],
    seen_identities: set[uuid.UUID],
    freq_cap: int,
    now: datetime,
) -> None:
    async with sf() as session:
        async with session.begin():
            contacts = (
                await session.execute(select(Contact).where(Contact.id.in_(contact_ids)))
            ).scalars().all()
            for contact in contacts:
                state, reason, identity_id = "planned", None, None
                creason = supp.contact_suppression_reason(contact)
                identity = await rcpt.resolve_identity(
                    session, workspace_id=workspace_id,
                    channel_account_id=channel_account_id, contact_id=contact.id,
                )
                if creason is not None:
                    state, reason = "skipped", creason
                elif identity is None:
                    state, reason = "skipped", supp.SKIP_INVALID_IDENTITY
                elif identity.id in seen_identities:
                    state, reason = "skipped", supp.SKIP_DEDUPE
                else:
                    identity_id = identity.id
                    if freq_cap > 0:
                        n = await supp.weekly_send_count(
                            session, workspace_id=workspace_id, contact_id=contact.id, now=now
                        )
                        if n >= freq_cap:
                            state, reason = "skipped", supp.SKIP_FREQ_CAP
                    if state == "planned":
                        seen_identities.add(identity.id)
                session.add(
                    BroadcastRecipient(
                        id=rcpt_uuid(), run_id=run_id, broadcast_id=None,
                        workspace_id=workspace_id, contact_id=contact.id,
                        channel_identity_id=identity_id, state=state, skip_reason=reason,
                        created_at=now,
                    )
                )


def rcpt_uuid() -> uuid.UUID:
    from ..models.base import uuid7

    return uuid7()


async def _next_chunk(
    sf: async_sessionmaker[AsyncSession], run_id: uuid.UUID, chunk_size: int
) -> list[tuple[uuid.UUID, datetime, uuid.UUID | None, uuid.UUID | None]]:
    async with sf() as session:
        rows = (
            await session.execute(
                select(
                    BroadcastRecipient.id, BroadcastRecipient.created_at,
                    BroadcastRecipient.contact_id, BroadcastRecipient.channel_identity_id,
                )
                .where(BroadcastRecipient.run_id == run_id, BroadcastRecipient.state == "planned")
                .order_by(BroadcastRecipient.id)
                .limit(chunk_size)
            )
        ).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


async def _send_one(
    sf: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    *,
    run_id: uuid.UUID,
    broadcast: Broadcast,
    template: MsgTemplate | None,
    recipient_id: uuid.UUID,
    recipient_created_at: datetime,
    contact_id: uuid.UUID | None,
    identity_id: uuid.UUID | None,
    channel_account_id: uuid.UUID,
    channel_type: str,
) -> None:
    """Send to one recipient in its own transaction (failure isolation +
    per-recipient idempotency). Records the message→recipient map so the
    delivery bridge can advance delivered/read later."""
    now = datetime.now(UTC)
    message_id: uuid.UUID | None = None
    events: list[Event] = []
    async with sf() as session:
        async with session.begin():
            rec = await session.get(BroadcastRecipient, (recipient_id, recipient_created_at))
            if rec is None or rec.state not in ("planned", "queued"):
                return  # idempotent: already terminal
            rec.attempts = (rec.attempts or 0) + 1
            if identity_id is None:
                rec.state, rec.skip_reason = "skipped", supp.SKIP_INVALID_IDENTITY
                return
            identity = await session.get(ChannelIdentity, identity_id)
            if identity is None:
                rec.state, rec.skip_reason = "skipped", supp.SKIP_INVALID_IDENTITY
                return
            contact = await session.get(Contact, contact_id) if contact_id else None
            conv = await rcpt.get_or_create_conversation(
                session, identity=identity, channel_account_id=channel_account_id,
                channel_type=channel_type,
            )
            try:
                content = await _build_content(
                    session, broadcast=broadcast, template=template, contact=contact
                )
                result = await messaging.send_message(
                    session, conversation=conv, sender_type="campaign", sender_id=None,
                    content=content, now=now,
                )
            except messaging.SendError as e:
                rec.state, rec.last_error = "failed", e.code
                return
            except _NoContent as e:
                rec.state, rec.last_error = "failed", str(e)[:120]
                return
            message_id = result.message.id
            events = result.events
            rec.state = "sent"
            rec.sent_at = now
            rec.provider_message_id = None  # external id set later by the delivery bridge
    if message_id is None:
        return
    # after commit: link message→recipient, dispatch to the channel, meter usage
    await rcpt.record_send(
        redis, message_id=message_id, run_id=run_id, recipient_id=recipient_id,
        recipient_created_at=recipient_created_at, workspace_id=broadcast.workspace_id,
    )
    try:
        await quotas.incr_usage(redis, broadcast.workspace_id, "broadcast_sent")
    except Exception:  # noqa: BLE001 — metering must never block a send
        pass
    try:
        from ..channels.sender import enqueue_send  # lazy: avoids sender↔worker import cycle

        await enqueue_send(message_id)
    except Exception:  # noqa: BLE001 — dispatch is best-effort; reaper re-picks pending rows
        log.debug("enqueue_send failed for broadcast message %s", message_id)
    try:
        await messaging.publish_realtime(events)
    except Exception:  # noqa: BLE001
        pass


async def _load_broadcast_for_run(session: AsyncSession, run_id: uuid.UUID) -> Broadcast | None:
    run = await session.get(BroadcastRun, run_id)
    if run is None:
        return None
    return await session.get(Broadcast, run.broadcast_id)


async def _flush(sf: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> None:
    async with sf() as session:
        async with session.begin():
            await rcpt.flush_run_counters(session, run_id)


async def _finalize(
    sf: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    run_id: uuid.UUID,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    async with sf() as session:
        async with session.begin():
            counts = await rcpt.flush_run_counters(session, run_id, now=now)
            run = await session.get(BroadcastRun, run_id)
            if run is None:
                return
            inflight = counts["planned"] + counts["queued"]
            if run.status == "running" and inflight == 0:
                run.status = "completed"
                run.finished_at = now
            bc = await session.get(Broadcast, run.broadcast_id)
            if bc is not None:
                await event_bus.emit(session, _run_event(bc, run, reason or "run_finished"))


def _run_event(bc: Broadcast, run: BroadcastRun, reason: str) -> Event:
    return Event(
        workspace_id=bc.workspace_id,
        type="broadcast.recipient_state",
        actor=Actor(type="system"),
        channel_type=bc.channel_type,
        channel_account_id=bc.channel_account_id,
        payload={
            "broadcast_id": str(bc.id), "run_id": str(run.id), "reason": reason,
            "planned": run.planned, "sent": run.sent, "delivered": run.delivered,
            "read": run.read, "failed": run.failed, "skipped": run.skipped,
            "status": run.status,
        },
    )


# --------------------------------------------------------------------------
# scheduler tick (drives one_time send_at + recurring rrule occurrences)
# --------------------------------------------------------------------------
@task
async def broadcast_scheduler_task(ctx: dict[str, Any]) -> int:
    sf: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    return await run_scheduler(sf)


async def run_scheduler(sf: async_sessionmaker[AsyncSession], *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    spawned = 0
    async with sf() as session:
        rows = (
            await session.execute(
                select(Broadcast).where(
                    Broadcast.deleted_at.is_(None),
                    Broadcast.status.in_(("scheduled", "running")),
                )
            )
        ).scalars().all()
        broadcasts = list(rows)
    for bc in broadcasts:
        try:
            spawned += await _schedule_one(sf, bc.id, now=now)
        except Exception:  # noqa: BLE001
            log.exception("scheduler failed for broadcast %s", bc.id)
    return spawned


async def _schedule_one(
    sf: async_sessionmaker[AsyncSession], broadcast_id: uuid.UUID, *, now: datetime
) -> int:
    spawned: list[uuid.UUID] = []
    async with sf() as session:
        async with session.begin():
            bc = await session.get(Broadcast, broadcast_id)
            if bc is None or bc.deleted_at is not None or bc.status not in ("scheduled", "running"):
                return 0
            last_at, run_count = (
                await session.execute(
                    select(func.max(BroadcastRun.scheduled_at), func.count())
                    .where(BroadcastRun.broadcast_id == bc.id)
                )
            ).one()
            due: list[datetime | None] = []
            if bc.type == "one_time":
                if run_count == 0 and sched.is_one_time_due(bc.schedule or {}, now=now):
                    due.append(sched.one_time_due_at(bc.schedule or {}, now=now))
            else:
                occ = sched.due_occurrences(bc.schedule or {}, now=now, after=last_at)
                due.extend(occ)
                if not occ and sched.next_occurrence(bc.schedule or {}, after=last_at or now) is None:
                    # series exhausted → settle when nothing is still running
                    if run_count > 0:
                        bc.status = "completed"
            for scheduled_at in due:
                run = await create_run(session, bc, scheduled_at=scheduled_at, now=now)
                spawned.append(run.id)
            if due and bc.type == "one_time":
                bc.status = "running"
    for run_id in spawned:
        await enqueue_fanout(run_id)
    return len(spawned)


# --------------------------------------------------------------------------
# delivery-status drain (events:conversation → recipient state)
# --------------------------------------------------------------------------
STATUS_GROUP = "broadcast-status"


@task
async def broadcast_status_drain_task(ctx: dict[str, Any], max_batches: int = 20) -> int:
    sf: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    redis: aioredis.Redis = ctx["redis"]
    stream = event_bus.STREAMS["conversation"]
    await event_bus.ensure_group(redis, stream, STATUS_GROUP)
    total = 0
    for _ in range(max_batches):
        n = await drain_delivery_once(sf, redis)
        total += n
        if n == 0:
            break
    return total


async def drain_delivery_once(
    sf: async_sessionmaker[AsyncSession], redis: aioredis.Redis, *, block_ms: int = 50
) -> int:
    stream = event_bus.STREAMS["conversation"]
    batch = await event_bus.read_batch(
        redis, [stream], STATUS_GROUP, "worker", count=128, block_ms=block_ms
    )
    if not batch:
        return 0
    touched_runs: set[uuid.UUID] = set()
    processed = 0
    async with sf() as session:
        async with session.begin():
            for _stream, _entry_id, event in batch:
                if event.type != "message.updated":
                    continue
                p = event.payload or {}
                status = p.get("delivery_status")
                mid = p.get("message_id")
                if status not in ("sent", "delivered", "read", "failed") or not mid:
                    continue
                err = p.get("error")
                err_code = err.get("code") if isinstance(err, dict) else p.get("error_code")
                advanced = await rcpt.handle_delivery_status(
                    session, redis, message_id=mid, status=status, error=err_code,
                    provider_message_id=p.get("external_message_id"),
                )
                if advanced:
                    processed += 1
                    raw = await redis.get(rcpt.msgmap_key(mid))
                    raw_s = raw.decode() if isinstance(raw, bytes) else raw
                    parsed = rcpt._parse_msgmap(raw_s) if raw_s else None
                    if parsed:
                        touched_runs.add(parsed[0])
            for run_id in touched_runs:
                await rcpt.flush_run_counters(session, run_id)
    await event_bus.ack(redis, stream, STATUS_GROUP, *[e[1] for e in batch])
    return processed


# --------------------------------------------------------------------------
# recycle-bin purge + WhatsApp approval reconcile (crons)
# --------------------------------------------------------------------------
@task
async def broadcast_recycle_purge_task(ctx: dict[str, Any]) -> int:
    from datetime import timedelta

    sf: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    days = get_settings().broadcast_recycle_days
    cutoff = datetime.now(UTC) - timedelta(days=days)
    async with sf() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    select(Broadcast.id).where(
                        Broadcast.deleted_at.is_not(None), Broadcast.deleted_at < cutoff
                    ).limit(500)
                )
            ).scalars().all()
            for bid in rows:
                bc = await session.get(Broadcast, bid)
                if bc is not None:
                    await session.delete(bc)
    return len(rows)


@task
async def wa_template_reconcile_task(ctx: dict[str, Any]) -> int:
    from . import wa_template_sync

    sf: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    return await wa_template_sync.reconcile_all(sf)


def _register_crons() -> None:
    from arq import cron

    register_cron(cron(broadcast_scheduler_task, second=set(range(0, 60, 20)), run_at_startup=False))
    register_cron(cron(broadcast_status_drain_task, second={5, 20, 35, 50}, run_at_startup=False))
    register_cron(cron(broadcast_recycle_purge_task, hour={4}, minute={23}, run_at_startup=False))
    register_cron(cron(wa_template_reconcile_task, hour={0, 6, 12, 18}, minute={11}, run_at_startup=False))


_register_crons()


# convenience for a dedicated beat process / smoke
def get_default_redis() -> aioredis.Redis:
    return get_redis()
