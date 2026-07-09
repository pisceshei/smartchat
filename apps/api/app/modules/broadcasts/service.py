"""Broadcast lifecycle service (plan B.3).

Owns validation (channel account / segment / template ownership + channel
match), status derivation from the schedule, the launch → BroadcastRun → fan-out
enqueue handoff, and the pause/resume/cancel/soft-delete/restore transitions.
The fan-out itself lives in ``app.marketing.fanout``.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from ...marketing import schedule as sched
from ...models.channels import ChannelAccount
from ...models.marketing import Broadcast, BroadcastRun, MsgTemplate, Segment
from ...services import timers

# msg_templates.channel → the channel_types that can send it
TEMPLATE_CHANNEL_MAP = {
    "whatsapp": {"whatsapp_cloud", "whatsapp_bsp"},
    "email": {"email"},
    "messenger": {"messenger"},
    "sms": {"sms"},
}
TIMER_KIND = "broadcast.run.fire"


class BroadcastError(ValueError):
    def __init__(self, detail: str, code: str = "invalid"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


async def _validate_refs(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    channel_type: str,
    channel_account_id: uuid.UUID | None,
    segment_id: uuid.UUID | None,
    template_id: uuid.UUID | None,
) -> None:
    if channel_account_id is not None:
        acct = await session.get(ChannelAccount, channel_account_id)
        if acct is None or acct.workspace_id != workspace_id:
            raise BroadcastError("channel account not found", "channel_account_not_found")
        if acct.channel_type != channel_type:
            raise BroadcastError("channel account type mismatch", "channel_mismatch")
    if segment_id is not None:
        seg = await session.get(Segment, segment_id)
        if seg is None or seg.workspace_id != workspace_id:
            raise BroadcastError("segment not found", "segment_not_found")
    if template_id is not None:
        tpl = await session.get(MsgTemplate, template_id)
        if tpl is None or tpl.workspace_id != workspace_id:
            raise BroadcastError("template not found", "template_not_found")
        allowed = TEMPLATE_CHANNEL_MAP.get(tpl.channel, set())
        if channel_type not in allowed:
            raise BroadcastError(
                f"template channel {tpl.channel} cannot send on {channel_type}", "template_channel"
            )
        if tpl.channel == "whatsapp":
            if tpl.approval_status not in ("approved",):
                raise BroadcastError("whatsapp template is not approved", "template_unapproved")
            # WhatsApp templates are approved PER WABA — a template bound to
            # one account/WABA does not exist on another. Require the selected
            # account to be the template's own account (or share its WABA).
            if tpl.waba_account_id and channel_account_id is not None:
                sel = await session.get(ChannelAccount, channel_account_id)
                sel_ids = {str(channel_account_id)}
                if sel is not None:
                    wid = (sel.config or {}).get("waba_id")
                    if wid:
                        sel_ids.add(str(wid))
                if str(tpl.waba_account_id) not in sel_ids:
                    raise BroadcastError(
                        "this template belongs to a different WhatsApp account",
                        "template_wrong_account",
                    )


def derive_status(bc_type: str, schedule: dict[str, Any], *, now: datetime) -> str:
    """Initial status from the schedule: immediate one_time ⇒ running (a run is
    launched now); future one_time ⇒ scheduled; recurring w/ rrule ⇒ scheduled;
    otherwise draft."""
    if bc_type == "recurring":
        return "scheduled" if (schedule or {}).get("rrule") else "draft"
    # one_time
    if sched.is_one_time_due(schedule or {}, now=now):
        return "running"
    return "scheduled"


async def get(session: AsyncSession, workspace_id: uuid.UUID, broadcast_id: uuid.UUID,
              *, include_deleted: bool = False) -> Broadcast:
    bc = await session.get(Broadcast, broadcast_id)
    if bc is None or bc.workspace_id != workspace_id:
        raise BroadcastError("broadcast not found", "not_found")
    if bc.deleted_at is not None and not include_deleted:
        raise BroadcastError("broadcast not found", "not_found")
    return bc


async def create(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    created_by_member_id: uuid.UUID | None,
    data: dict[str, Any],
    now: datetime | None = None,
) -> tuple[Broadcast, uuid.UUID | None]:
    """Create a broadcast and, when it is due immediately, its first run.
    Returns (broadcast, run_id_to_enqueue). The caller commits then enqueues."""
    now = now or datetime.now(UTC)
    bc_type = data.get("type", "one_time")
    if bc_type not in ("one_time", "recurring"):
        raise BroadcastError("type must be one_time or recurring", "bad_type")
    channel_type = data["channel_type"]
    schedule = data.get("schedule") or {}
    await _validate_refs(
        session, workspace_id=workspace_id, channel_type=channel_type,
        channel_account_id=data.get("channel_account_id"), segment_id=data.get("segment_id"),
        template_id=data.get("template_id"),
    )
    status = derive_status(bc_type, schedule, now=now)
    bc = Broadcast(
        workspace_id=workspace_id,
        name=data.get("name") or "",
        type=bc_type,
        channel_type=channel_type,
        channel_account_id=data.get("channel_account_id"),
        segment_id=data.get("segment_id"),
        template_id=data.get("template_id"),
        variable_mapping=data.get("variable_mapping") or {},
        schedule=schedule,
        send_rules=data.get("send_rules") or {},
        status=status,
        created_by_member_id=created_by_member_id,
    )
    session.add(bc)
    await session.flush()

    run_id: uuid.UUID | None = None
    if status == "running" and bc_type == "one_time":
        from ...marketing import fanout

        run = await fanout.create_run(session, bc, scheduled_at=now, now=now)
        run_id = run.id
    elif status == "scheduled":
        # arm a precise timer so short delays fire within 1s; the scheduler tick
        # is the durable safety net that actually spawns the run.
        fire_at = (
            sched.one_time_due_at(schedule, now=now)
            if bc_type == "one_time"
            else (sched.next_occurrence(schedule, after=now) or now)
        )
        await timers.schedule(
            session, workspace_id=workspace_id, kind=TIMER_KIND, ref_id=bc.id, fire_at=fire_at,
            payload={"broadcast_id": str(bc.id)},
        )
    return bc, run_id


async def update(
    session: AsyncSession, bc: Broadcast, data: dict[str, Any], *, now: datetime | None = None
) -> Broadcast:
    if bc.status not in ("draft", "scheduled", "paused"):
        raise BroadcastError("only draft/scheduled broadcasts can be edited", "immutable")
    now = now or datetime.now(UTC)
    channel_type = data.get("channel_type", bc.channel_type)
    await _validate_refs(
        session, workspace_id=bc.workspace_id, channel_type=channel_type,
        channel_account_id=data.get("channel_account_id", bc.channel_account_id),
        segment_id=data.get("segment_id", bc.segment_id),
        template_id=data.get("template_id", bc.template_id),
    )
    for field in ("name", "channel_type", "channel_account_id", "segment_id", "template_id",
                  "variable_mapping", "schedule", "send_rules", "type"):
        if field in data and data[field] is not None:
            setattr(bc, field, data[field])
    if bc.status in ("draft", "scheduled"):
        bc.status = derive_status(bc.type, bc.schedule or {}, now=now)
    return bc


async def pause(session: AsyncSession, bc: Broadcast) -> Broadcast:
    if bc.status not in ("running", "scheduled"):
        raise BroadcastError(f"cannot pause a {bc.status} broadcast", "bad_state")
    bc.status = "paused"
    await session.execute(
        sql_update(BroadcastRun)
        .where(BroadcastRun.broadcast_id == bc.id, BroadcastRun.status.in_(("pending", "running")))
        .values(status="paused")
    )
    return bc


async def resume(session: AsyncSession, bc: Broadcast, *, now: datetime | None = None) -> list[uuid.UUID]:
    if bc.status != "paused":
        raise BroadcastError("broadcast is not paused", "bad_state")
    now = now or datetime.now(UTC)
    bc.status = "running"
    rows = (
        await session.execute(
            select(BroadcastRun.id).where(
                BroadcastRun.broadcast_id == bc.id, BroadcastRun.status == "paused"
            )
        )
    ).scalars().all()
    for rid in rows:
        await session.execute(
            sql_update(BroadcastRun).where(BroadcastRun.id == rid).values(status="running")
        )
    if not rows:
        # nothing in flight → back to a schedulable state
        bc.status = "scheduled" if bc.schedule else "draft"
    return list(rows)


async def cancel(session: AsyncSession, bc: Broadcast) -> Broadcast:
    if bc.status in ("completed", "cancelled"):
        raise BroadcastError(f"broadcast already {bc.status}", "bad_state")
    bc.status = "cancelled"
    await session.execute(
        sql_update(BroadcastRun)
        .where(
            BroadcastRun.broadcast_id == bc.id,
            BroadcastRun.status.in_(("pending", "running", "paused")),
        )
        .values(status="cancelled")
    )
    await timers.cancel(session, workspace_id=bc.workspace_id, kind=TIMER_KIND, ref_id=bc.id)
    return bc


async def soft_delete(session: AsyncSession, bc: Broadcast, *, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    bc.deleted_at = now
    if bc.status in ("running", "scheduled", "paused"):
        bc.status = "cancelled"
        await session.execute(
            sql_update(BroadcastRun)
            .where(
                BroadcastRun.broadcast_id == bc.id,
                BroadcastRun.status.in_(("pending", "running", "paused")),
            )
            .values(status="cancelled")
        )
    await timers.cancel(session, workspace_id=bc.workspace_id, kind=TIMER_KIND, ref_id=bc.id)


async def restore(session: AsyncSession, bc: Broadcast, *, now: datetime | None = None) -> Broadcast:
    if bc.deleted_at is None:
        raise BroadcastError("broadcast is not deleted", "bad_state")
    now = now or datetime.now(UTC)
    bc.deleted_at = None
    bc.status = derive_status(bc.type, bc.schedule or {}, now=now)
    if bc.status == "running":  # never auto-relaunch a restored one_time
        bc.status = "scheduled"
    return bc


# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------
def send_rule_summary(bc: Broadcast) -> str:
    if bc.type == "recurring":
        rrule = (bc.schedule or {}).get("rrule") or {}
        freq = rrule.get("freq", "daily")
        interval = rrule.get("interval", 1)
        base = f"每{interval}{ {'hourly':'小時','daily':'日','weekly':'週','monthly':'月'}.get(freq, freq)}"
        byhour = rrule.get("byhour")
        if byhour:
            base += " " + ",".join(f"{int(h):02d}:00" for h in byhour)
        return base
    send_at = (bc.schedule or {}).get("send_at")
    return f"定時 {send_at}" if send_at else "立即發送"


def success_rate(bc: Broadcast) -> float:
    from ...marketing.recipients import success_rate as sr

    return sr(bc.sent_count, bc.delivered_count)
