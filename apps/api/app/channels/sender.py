"""Transactional-outbox sender (plan A.7 outbound path).

The inbox/message service persists an outbound Message with
delivery_status='pending' and enqueues the ARQ job `send_outbound_message`.
This worker claims the row (pending→sending, race-safe), renders through the
adapter (capability degradation), waits on the per-account/per-chat Redis
token bucket, sends, and finalizes:

- success   → sent + external_message_id + dedup row + parked-status replay
- retryable → backoff 2s/10s/60s/5m/30m, max 5 tries, then failed
- permanent → failed with a typed error code (incl. WINDOW_EXPIRED so the
              composer can offer the template path)
- auth      → channel_accounts.status='token_expired', account queue paused
              via Redis flag, job retried later
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import redis.asyncio as aioredis
from arq import Retry
from arq.connections import RedisSettings, create_pool
from py_contracts.content import MessageContent
from py_contracts.events import Actor, Event
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..jobs.worker import register_cron, task
from ..models.channels import ChannelAccount
from ..models.contacts import ChannelIdentity
from ..models.conversations import Conversation
from ..models.messaging import Message, MessageDedup
from ..services import event_bus
from ..settings import get_settings
from . import ratelimit
from .base import AccountRef, SendResult, capabilities_for, content_has_template
from .creds import get_credentials, set_credentials
from .ingress_pipeline import apply_delivery_status, pop_parked
from .registry import get_adapter

log = logging.getLogger("smartchat.channels.sender")

JOB_SEND = "send_outbound_message"
BACKOFF_SCHEDULE: tuple[int, ...] = (2, 10, 60, 300, 1800)
MAX_TRIES = 5
CLAIM_TTL_S = 600

Outcome = Literal["sent", "failed", "retry", "auth"]


def pause_key(account_id: uuid.UUID | str) -> str:
    return f"send:paused:{account_id}"


def claim_key(message_id: uuid.UUID | str) -> str:
    return f"send:claim:{message_id}"


def backoff_for_attempt(attempt: int) -> int:
    """attempt is 1-based; caps at the last step."""
    idx = max(0, min(attempt - 1, len(BACKOFF_SCHEDULE) - 1))
    return BACKOFF_SCHEDULE[idx]


def classify_result(result: SendResult) -> Outcome:
    if result.ok:
        return "sent"
    if result.auth_failed:
        return "auth"
    if result.retryable:
        return "retry"
    return "failed"


# --------------------------------------------------------------------------
# public API for other modules
# --------------------------------------------------------------------------
_arq_pool: Any = None


async def _get_arq_pool() -> Any:
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _arq_pool


async def enqueue_send(message_id: uuid.UUID | str, *, defer_by: float | None = None) -> None:
    """Enqueue the outbound job for a pending Message row (call after commit)."""
    pool = await _get_arq_pool()
    await pool.enqueue_job(JOB_SEND, str(message_id), _defer_by=defer_by)


# --------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------
@task
async def send_outbound_message(ctx: dict[str, Any], message_id: str) -> str:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    redis: aioredis.Redis = ctx["redis"]
    attempt = int(ctx.get("job_try") or 1)
    mid = uuid.UUID(message_id)

    # claim: Redis NX guard against concurrent duplicate jobs, then
    # pending/sending → sending in PG ('sending' reclaim covers a worker that
    # died after claiming — its Redis claim key has expired by then).
    if not await redis.set(claim_key(mid), "1", nx=True, ex=CLAIM_TTL_S):
        return "skipped"
    async with session_factory() as session:
        async with session.begin():
            res = await session.execute(
                update(Message)
                .where(
                    Message.id == mid,
                    Message.direction == "out",
                    Message.delivery_status.in_(["pending", "sending"]),
                )
                .values(delivery_status="sending")
                .returning(Message.id)
            )
            if res.first() is None:
                await redis.delete(claim_key(mid))
                return "skipped"

    try:
        outcome, detail, ext_id = await _send_flow(session_factory, redis, mid)
    except ratelimit.RateLimitTimeout as e:
        outcome, detail, ext_id = "retry", str(e), None
    except Exception as e:  # noqa: BLE001 — infra failure: retry with backoff
        log.exception("send flow crashed message=%s", message_id)
        outcome, detail, ext_id = "retry", f"internal:{e}", None

    if outcome == "sent":
        await _finalize_sent(session_factory, redis, mid, ext_id)
        await redis.delete(claim_key(mid))
        return "sent"

    if outcome == "failed":
        await _finalize_failed(session_factory, mid, detail)
        await redis.delete(claim_key(mid))
        return f"failed:{detail}"

    # auth / retry → put the row back to pending and re-run with backoff
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(Message)
                .where(Message.id == mid, Message.delivery_status == "sending")
                .values(delivery_status="pending")
            )
    await redis.delete(claim_key(mid))
    if attempt >= MAX_TRIES:
        await _finalize_failed(session_factory, mid, detail or "RETRY_EXHAUSTED")
        return f"failed:{detail or 'RETRY_EXHAUSTED'}"
    delay = 300 if outcome == "auth" else backoff_for_attempt(attempt)
    raise Retry(defer=delay)


async def _send_flow(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    mid: uuid.UUID,
) -> tuple[Outcome, str | None, str | None]:
    async with session_factory() as session:
        msg = (
            await session.execute(select(Message).where(Message.id == mid))
        ).scalar_one_or_none()
        if msg is None:
            return "failed", "MESSAGE_MISSING", None
        conv = await session.get(Conversation, msg.conversation_id)
        if conv is None:
            return "failed", "CONVERSATION_MISSING", None
        identity_id = msg.channel_identity_id or conv.channel_identity_id
        identity = await session.get(ChannelIdentity, identity_id)
        if identity is None:
            return "failed", "IDENTITY_MISSING", None
        acct = await session.get(ChannelAccount, conv.channel_account_id)
        if acct is None or not acct.enabled:
            return "failed", "ACCOUNT_DISABLED", None
        if await redis.get(pause_key(acct.id)):
            return "retry", "ACCOUNT_PAUSED", None

        caps = capabilities_for(acct.channel_type)
        content = MessageContent.model_validate(msg.content)
        now = datetime.now(UTC)
        window_open = True
        if caps.session_window_hours:
            window_open = (
                conv.customer_window_expires_at is not None
                and conv.customer_window_expires_at > now
            )
        if (
            caps.template_required_outside_window
            and not window_open
            and not content_has_template(content)
        ):
            return "failed", "WINDOW_EXPIRED", None

        credentials = await get_credentials(session, acct)
        adapter = get_adapter(acct.channel_type)
        account_ref = AccountRef.from_row(acct)
        to = _bridge_to(identity, acct.channel_type)
        payloads = adapter.render(content, window_open=window_open)
        if not payloads:
            return "failed", "UNSUPPORTED_CONTENT", None
        payloads = await adapter.enrich_outbound(
            session,
            account=account_ref,
            credentials=credentials,
            conversation=conv,
            identity=identity,
            payloads=payloads,
        )
        channel_type = acct.channel_type
        account_id = acct.id

    # network I/O outside any DB transaction
    ext_id: str | None = None
    for payload in payloads:
        await ratelimit.wait_for_slot(
            redis, channel_type=channel_type, account_id=account_id, chat_id=to
        )
        result = await adapter.send(account_ref, credentials, to, payload)
        if not result.ok:
            outcome = classify_result(result)
            if outcome == "auth":
                await _pause_account(session_factory, redis, account_id, result)
            code = result.error_code or "PERMANENT"
            log.warning(
                "send %s message=%s account=%s code=%s msg=%s",
                outcome, mid, account_id, code, result.error_message,
            )
            return outcome, code, ext_id
        ext_id = ext_id or result.external_message_id
    return "sent", None, ext_id


def _bridge_to(identity: ChannelIdentity, channel_type: str) -> str:
    """Outbound recipient for the channel adapter. whatsapp_app identities
    still keyed by an UNRESOLVED lid (external id == meta.wa_lid) are addressed
    at the lid server explicitly (``<digits>@lid``) so delivery never depends
    on the bridge's error-string retry heuristic; identities healed to a real
    phone send bare digits as before."""
    to: str = identity.external_user_id
    if channel_type == "whatsapp_app":
        wa_lid = (identity.meta or {}).get("wa_lid")
        if wa_lid and wa_lid == to:
            return f"{wa_lid}@lid"
    return to


async def _pause_account(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    account_id: uuid.UUID,
    result: SendResult,
) -> None:
    """Auth failure: mark the account token_expired and pause its queue."""
    await redis.set(pause_key(account_id), "token_expired", ex=1800)
    async with session_factory() as session:
        async with session.begin():
            acct = await session.get(ChannelAccount, account_id)
            if acct is None:
                return
            acct.status = "token_expired"
            acct.health = {
                **(acct.health or {}),
                "last_error": (result.error_message or "auth failed")[:300],
                "at": datetime.now(UTC).isoformat(),
            }
            await event_bus.emit(
                session,
                Event(
                    workspace_id=acct.workspace_id,
                    type="channel.status",
                    actor=Actor(type="system"),
                    channel_type=acct.channel_type,
                    channel_account_id=acct.id,
                    payload={"status": "token_expired", "error": result.error_message},
                ),
            )


async def _finalize_sent(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    mid: uuid.UUID,
    ext_id: str | None,
) -> None:
    from ..services import messaging  # local import to avoid a cycle

    account_id: uuid.UUID | None = None
    ev_out: Event | None = None
    async with session_factory() as session:
        async with session.begin():
            msg = (
                await session.execute(select(Message).where(Message.id == mid))
            ).scalar_one_or_none()
            if msg is None:
                return
            if msg.delivery_status in ("pending", "sending"):
                msg.delivery_status = "sent"
            msg.external_message_id = ext_id or msg.external_message_id
            conv = await session.get(Conversation, msg.conversation_id)
            if conv is not None:
                account_id = conv.channel_account_id
                now = datetime.now(UTC)
                conv.last_message_at = now
                if not msg.is_note:
                    conv.last_agent_message_at = now
                    conv.needs_reply = False
                conv.snippet = (msg.text_plain or f"[{msg.msg_type}]")[:140]
                if ext_id:
                    # echo/status dedup + status lookup key
                    await session.execute(
                        pg_insert(MessageDedup)
                        .values(
                            channel_account_id=conv.channel_account_id,
                            external_message_id=ext_id,
                            workspace_id=msg.workspace_id,
                            message_id=msg.id,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["channel_account_id", "external_message_id"]
                        )
                    )
            ev_out = messaging.delivery_status_event(
                msg,
                status=msg.delivery_status,
                external_message_id=msg.external_message_id,
                channel_account_id=account_id,
            )
            await event_bus.emit(session, ev_out)
    # replay delivery/read statuses that arrived before we knew the ext id
    replay_events: list[Event] = []
    if ext_id and account_id is not None:
        parked = await pop_parked(redis, account_id, ext_id)
        if parked:
            async with session_factory() as session:
                async with session.begin():
                    acct = await session.get(ChannelAccount, account_id)
                    if acct is not None:
                        for status_ev in parked:
                            e = await apply_delivery_status(session, acct, mid, status_ev)
                            if e is not None:
                                replay_events.append(e)
    # live tick advance (pending→sent→…) — best effort AFTER the commits; must
    # never raise: the send is already finalized and a raise would Retry a
    # completed send.
    to_publish = [e for e in [ev_out, *replay_events] if e is not None]
    if to_publish:
        try:
            await messaging.publish_realtime(to_publish)
        except Exception:  # noqa: BLE001
            log.warning("realtime publish failed for message %s", mid, exc_info=True)


async def _finalize_failed(
    session_factory: async_sessionmaker[AsyncSession],
    mid: uuid.UUID,
    error_code: str | None,
) -> None:
    from ..services import messaging  # local import to avoid a cycle

    ev_out: Event | None = None
    async with session_factory() as session:
        async with session.begin():
            msg = (
                await session.execute(select(Message).where(Message.id == mid))
            ).scalar_one_or_none()
            if msg is None:
                return
            msg.delivery_status = "failed"
            msg.delivery_error = error_code or "PERMANENT"
            ev_out = messaging.delivery_status_event(
                msg, status="failed", error_code=msg.delivery_error
            )
            await event_bus.emit(session, ev_out)
    if ev_out is not None:
        try:
            await messaging.publish_realtime([ev_out])
        except Exception:  # noqa: BLE001
            log.warning("realtime publish failed for message %s", mid, exc_info=True)


# --------------------------------------------------------------------------
# safety crons: stale 'sending' reaper + ingress drain + email poll are
# registered here so importing this single module wires the whole channel
# layer into the worker (jobs.worker TASKS / CRON_JOBS registries).
# --------------------------------------------------------------------------
@task
async def requeue_stuck_sending_task(ctx: dict[str, Any]) -> int:
    """A worker that died mid-send leaves delivery_status='sending' and its
    Redis claim expires; put those rows back to pending and re-enqueue."""
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    redis: aioredis.Redis = ctx["redis"]
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Message.id).where(
                    Message.direction == "out", Message.delivery_status == "sending"
                ).limit(200)
            )
        ).scalars().all()
    requeued = 0
    for mid in rows:
        if await redis.exists(claim_key(mid)):
            continue  # still actively claimed
        async with session_factory() as session:
            async with session.begin():
                res = await session.execute(
                    update(Message)
                    .where(Message.id == mid, Message.delivery_status == "sending")
                    .values(delivery_status="pending")
                    .returning(Message.id)
                )
                if res.first() is None:
                    continue
        await enqueue_send(mid)
        requeued += 1
    return requeued


@task
async def drain_pending_sends_task(ctx: dict[str, Any]) -> int:
    """At-least-once outbound safety net (the transactional-outbox drain the
    messaging docstring describes): enqueue every unclaimed 'pending' outbound
    message. This backs the low-latency hot-path enqueue
    (messaging.dispatch_channel_sends) so agent / AI / flow / broadcast replies
    ALWAYS reach their channel — even if a caller never enqueued or its enqueue
    failed. Idempotent: send_outbound_message claims each row via a Redis NX
    guard, so re-dispatching an in-flight row is a harmless no-op."""
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    redis: aioredis.Redis = ctx["redis"]
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Message.id).where(
                    Message.direction == "out",
                    Message.delivery_status == "pending",
                ).limit(500)
            )
        ).scalars().all()
    dispatched = 0
    for mid in rows:
        if await redis.exists(claim_key(mid)):
            continue  # a live send job already owns this row
        await enqueue_send(mid)
        dispatched += 1
    return dispatched


@task
async def ingress_drain_task(ctx: dict[str, Any], max_batches: int = 10) -> int:
    """Bounded ingress drain — safety net when the dedicated run_ingress_loop
    process is not (yet) deployed."""
    from . import ingress_pipeline

    session_factory = ctx["session_factory"]
    redis = ctx["redis"]
    await ingress_pipeline.ensure_groups(redis)
    total = 0
    for _ in range(max_batches):
        n = await ingress_pipeline.consume_once(session_factory, redis, block_ms=100)
        total += n
        if n == 0:
            break
    return total


@task
async def email_poll_task(ctx: dict[str, Any]) -> int:
    """Poll every enabled email account (IMAP); per-account Redis lock keeps
    concurrent beats/workers from double-polling."""
    from .adapters.email_imap import poll_email_account

    session_factory = ctx["session_factory"]
    redis = ctx["redis"]
    async with session_factory() as session:
        ids = (
            await session.execute(
                select(ChannelAccount.id).where(
                    ChannelAccount.channel_type == "email",
                    ChannelAccount.enabled.is_(True),
                )
            )
        ).scalars().all()
    total = 0
    for aid in ids:
        if not await redis.set(f"email:poll:{aid}", "1", nx=True, ex=55):
            continue
        try:
            total += await poll_email_account(session_factory, redis, aid)
        except Exception:  # noqa: BLE001
            log.exception("email poll failed account=%s", aid)
        finally:
            await redis.delete(f"email:poll:{aid}")
    return total


# OAuth channels whose short-lived access tokens must be refreshed proactively
# (adapters implement refresh_credentials). Permanent-token / password channels
# (messenger/whatsapp_cloud/telegram/line_oa) carry no expiry stamp → skipped.
_REFRESHABLE_CHANNELS = ("email", "youtube", "zalo_app")


def _token_expiry(credentials: dict[str, Any]) -> datetime | None:
    raw = credentials.get("token_expires_at") or credentials.get("oauth_token_expires_at")
    if not raw:
        return None
    try:
        exp = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    return exp.replace(tzinfo=UTC) if exp.tzinfo is None else exp


async def _refresh_if_expiring(
    session: AsyncSession, acct: ChannelAccount, credentials: dict[str, Any], *, skew_min: int = 5
) -> dict[str, Any]:
    """Refresh an account's OAuth token when it expires within skew_min minutes,
    persist it, and return the fresh credentials. No-op for tokens without an
    expiry stamp or when the adapter's refresh yields nothing (caller keeps the
    stored credentials). Central so both the poll beats and the refresh sweep
    stay consistent — connect only probes once, nothing refreshed before."""
    exp = _token_expiry(credentials)
    if exp is None or exp - datetime.now(UTC) > timedelta(minutes=skew_min):
        return credentials
    try:
        updated = await get_adapter(acct.channel_type).refresh_credentials(acct, credentials)
    except Exception:  # noqa: BLE001 — refresh must never crash a beat
        log.exception("token refresh failed account=%s", acct.id)
        return credentials
    if updated:
        await set_credentials(session, acct, updated)
        await session.commit()
        return updated
    return credentials


@task
async def youtube_poll_task(ctx: dict[str, Any]) -> int:
    """Poll every enabled YouTube account for new top-level comments (YouTube
    has NO webhook). Per-account Redis lock prevents double-polling; the poll
    cursor is persisted to config so comments are never re-ingested."""
    from sqlalchemy.orm.attributes import flag_modified

    from .ingress_pipeline import enqueue_inbound

    session_factory = ctx["session_factory"]
    redis = ctx["redis"]
    async with session_factory() as session:
        ids = (
            await session.execute(
                select(ChannelAccount.id).where(
                    ChannelAccount.channel_type == "youtube",
                    ChannelAccount.enabled.is_(True),
                )
            )
        ).scalars().all()
    total = 0
    for aid in ids:
        if not await redis.set(f"youtube:poll:{aid}", "1", nx=True, ex=110):
            continue
        try:
            async with session_factory() as session:
                acct = await session.get(ChannelAccount, aid)
                if acct is None or not acct.enabled:
                    continue
                creds = await get_credentials(session, acct)
                creds = await _refresh_if_expiring(session, acct, creds)
                res = await get_adapter("youtube").poll_comments(acct, creds)
                if res.count:
                    await enqueue_inbound(
                        redis,
                        account_id=acct.id,
                        workspace_id=acct.workspace_id,
                        channel_type="youtube",
                        payload=res.payload,
                    )
                    total += res.count
                if res.cursor and res.cursor != (acct.config or {}).get("youtube_poll_cursor"):
                    acct.config = {**(acct.config or {}), "youtube_poll_cursor": res.cursor}
                    flag_modified(acct, "config")
                    await session.commit()
        except Exception:  # noqa: BLE001
            log.exception("youtube poll failed account=%s", aid)
        finally:
            await redis.delete(f"youtube:poll:{aid}")
    return total


@task
async def refresh_tokens_task(ctx: dict[str, Any]) -> int:
    """Proactively refresh OAuth access tokens ~5 min before expiry so a channel
    never dies waiting for a reactive send failure. Accounts without an expiry
    stamp (permanent/password auth) are skipped by _refresh_if_expiring."""
    session_factory = ctx["session_factory"]
    redis = ctx["redis"]
    async with session_factory() as session:
        ids = (
            await session.execute(
                select(ChannelAccount.id).where(
                    ChannelAccount.channel_type.in_(_REFRESHABLE_CHANNELS),
                    ChannelAccount.enabled.is_(True),
                )
            )
        ).scalars().all()
    refreshed = 0
    for aid in ids:
        if not await redis.set(f"token:refresh:{aid}", "1", nx=True, ex=55):
            continue
        try:
            async with session_factory() as session:
                acct = await session.get(ChannelAccount, aid)
                if acct is None or not acct.enabled:
                    continue
                creds = await get_credentials(session, acct)
                before = creds.get("access_token") or creds.get("oauth_access_token")
                creds = await _refresh_if_expiring(session, acct, creds)
                after = creds.get("access_token") or creds.get("oauth_access_token")
                if before != after:
                    refreshed += 1
        except Exception:  # noqa: BLE001
            log.exception("token refresh sweep failed account=%s", aid)
        finally:
            await redis.delete(f"token:refresh:{aid}")
    return refreshed


@task
async def health_probe_task(ctx: dict[str, Any]) -> int:
    """Periodic health re-probe: connect only probes once, so a token that dies
    later (or a webhook removed in the provider console) is invisible until the
    next send. Refreshes channel_accounts.status/health for the admin UI. The
    per-account lock (long TTL) spreads probes so each account is hit ~once per
    beat window, not on every beat."""
    session_factory = ctx["session_factory"]
    redis = ctx["redis"]
    async with session_factory() as session:
        ids = (
            await session.execute(
                select(ChannelAccount.id).where(
                    ChannelAccount.enabled.is_(True),
                    ChannelAccount.channel_type.notin_(("widget",)),
                )
            )
        ).scalars().all()
    probed = 0
    for aid in ids:
        if not await redis.set(f"health:probe:{aid}", "1", nx=True, ex=590):
            continue
        try:
            async with session_factory() as session:
                acct = await session.get(ChannelAccount, aid)
                if acct is None or not acct.enabled:
                    continue
                creds = await get_credentials(session, acct)
                creds = await _refresh_if_expiring(session, acct, creds)
                result = await get_adapter(acct.channel_type).check_health(acct, creds)
                acct.status = "active" if result.ok else result.status
                acct.health = {**(acct.health or {}), **(result.detail or {})}
                await session.commit()
                probed += 1
        except Exception:  # noqa: BLE001
            log.exception("health probe failed account=%s", aid)
    return probed


def _register_crons() -> None:
    from arq import cron

    register_cron(cron(ingress_drain_task, second={0, 15, 30, 45}, run_at_startup=True))
    # outbound safety net: drain unclaimed 'pending' rows every 15s (offset from
    # the ingress drain to spread worker load); run_at_startup flushes a backlog
    # left by a version that never enqueued (e.g. agent/AI replies stuck pending).
    register_cron(cron(drain_pending_sends_task, second={5, 20, 35, 50}, run_at_startup=True))
    register_cron(cron(email_poll_task, minute=set(range(60)), run_at_startup=False))
    register_cron(cron(requeue_stuck_sending_task, minute=set(range(0, 60, 2))))
    # YouTube has no webhook — poll comments every 2 min (Data API quota-friendly)
    register_cron(cron(youtube_poll_task, minute=set(range(0, 60, 2))))
    # proactive OAuth refresh + periodic health so tokens/webhooks don't silently rot
    register_cron(cron(refresh_tokens_task, minute=set(range(0, 60, 5))))
    register_cron(cron(health_probe_task, minute=set(range(0, 60, 10))))


_register_crons()
