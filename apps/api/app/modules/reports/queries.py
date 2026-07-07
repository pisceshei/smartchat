"""Report query layer (plan 附錄 B.4 + live-captured Pro layouts).

Everything is aggregated in **UTC hours** in the agg_* tables; this module
folds those hourly rows into **workspace-local** day/week/month/hour buckets at
read time (DST-safe — no recompute on a tz change). Averages are derived here
as ``sum ÷ n`` so the stored buckets stay additively mergeable.

"today" (service-overview KPI + current trend bucket) is the merge of
agg-so-far with a **live** indexed count over the current events partition, so
the number is fresh even while the rollup is a few seconds behind.

``channel_account_id`` is not a column on the hourly aggs, so when that filter
is present the conversation/message metrics are recomputed live from the events
table via the same ``rollup.fold_event`` classifier (bounded by the window).
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...analytics import collectors
from ...analytics.rollup import Accumulator, fold_event
from ...models.channels import ChannelAccount
from ...models.contacts import Contact
from ...models.conversations import Conversation
from ...models.members import MemberGroup, MemberGroupMember, WorkspaceMember
from ...models.misc import EventRow
from ...models.reports import (
    AgentPresenceSession,
    AggAgentHourly,
    AggConversationsHourly,
    AggCustomersDaily,
    AggMessagesHourly,
)
from ...models.tenancy import Workspace
from ...services import event_bus

DEFAULT_WINDOW_DAYS = 7
_OPENED_TYPES = ("conversation.created", "conversation.reopened")
_PRESENCE_GRACE_S = 90


# ==========================================================================
# filters + window
# ==========================================================================
@dataclass(frozen=True)
class Filters:
    from_: datetime
    to: datetime
    interval: str = "day"
    channel_type: str | None = None
    channel_account_id: uuid.UUID | None = None
    member_id: uuid.UUID | None = None

    @property
    def start_hour(self) -> datetime:
        return collectors.floor_hour(self.from_)

    @property
    def end_hour(self) -> datetime:
        # exclusive upper bound covering the hour that ``to`` lands in
        return collectors.floor_hour(self.to) + timedelta(hours=1)


def parse_filters(
    *,
    from_: str | None,
    to: str | None,
    interval: str | None,
    channel_type: str | None,
    channel_account_id: str | None,
    member_id: str | None,
    now: datetime | None = None,
) -> Filters:
    now = now or datetime.now(UTC)
    to_dt = _parse_dt(to) or now
    from_dt = _parse_dt(from_) or (to_dt - timedelta(days=DEFAULT_WINDOW_DAYS))
    if from_dt > to_dt:
        from_dt, to_dt = to_dt, from_dt
    return Filters(
        from_=from_dt,
        to=to_dt,
        interval=interval if interval in ("hour", "day", "week", "month") else "day",
        channel_type=channel_type or None,
        channel_account_id=_parse_uuid(channel_account_id),
        member_id=_parse_uuid(member_id),
    )


def config_dict(f: Filters, *, dimension: str | None = None) -> dict[str, Any]:
    """Freeze a filter set into a JSON config (share/export snapshots)."""
    d: dict[str, Any] = {
        "from": f.from_.isoformat(),
        "to": f.to.isoformat(),
        "interval": f.interval,
        "channel_type": f.channel_type,
        "channel_account_id": str(f.channel_account_id) if f.channel_account_id else None,
        "member_id": str(f.member_id) if f.member_id else None,
    }
    if dimension is not None:
        d["dimension"] = dimension
    return d


def filters_from_config(cfg: dict[str, Any]) -> Filters:
    return parse_filters(
        from_=cfg.get("from"),
        to=cfg.get("to"),
        interval=cfg.get("interval"),
        channel_type=cfg.get("channel_type"),
        channel_account_id=cfg.get("channel_account_id"),
        member_id=cfg.get("member_id"),
    )


async def workspace_tz(session: AsyncSession, workspace_id: uuid.UUID) -> str:
    ws = await session.get(Workspace, workspace_id)
    return (ws.settings or {}).get("timezone", "UTC") if ws else "UTC"


# ==========================================================================
# hourly row loaders (aggs fast path / events scan when account-filtered)
# ==========================================================================
async def _scan_accumulator(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters
) -> Accumulator:
    """Fold events in the window into an in-memory accumulator honoring the
    channel_account_id / channel_type filters the aggs can't express."""
    q = select(EventRow).where(
        EventRow.workspace_id == workspace_id,
        EventRow.occurred_at >= f.start_hour,
        EventRow.occurred_at < f.end_hour,
    )
    if f.channel_account_id is not None:
        q = q.where(EventRow.channel_account_id == f.channel_account_id)
    if f.channel_type:
        q = q.where(EventRow.channel_type == f.channel_type)
    rows = (await session.execute(q.order_by(EventRow.occurred_at, EventRow.id))).scalars().all()
    events = [event_bus.row_to_event(r) for r in rows]
    starts = await _session_starts(session, events)
    acc = Accumulator()
    for ev in events:
        fold_event(acc, ev, session_started_at=starts)
    return acc


async def _session_starts(session: AsyncSession, events: list) -> dict[uuid.UUID, datetime]:
    from ...models.conversations import ConversationSession

    ids: set[uuid.UUID] = set()
    for ev in events:
        r = collectors.resolved(ev)
        if r and r.session_id:
            ids.add(r.session_id)
        fr = collectors.first_responded(ev)
        if fr and fr.session_id:
            ids.add(fr.session_id)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ConversationSession.id, ConversationSession.started_at).where(
                ConversationSession.id.in_(ids)
            )
        )
    ).all()
    return {rid: started for rid, started in rows}


async def _conv_hourly(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters
) -> list[dict[str, Any]]:
    """Rows: {hour, channel_type, opened, resolved, reopened, frt_sum_s, frt_n,
    resolution_sum_s, resolution_n}."""
    if f.channel_account_id is not None:
        acc = await _scan_accumulator(session, workspace_id, f)
        return [
            {
                "hour": hour,
                "channel_type": ch,
                "opened": a.opened,
                "resolved": a.resolved,
                "reopened": a.reopened,
                "frt_sum_s": a.frt_sum_s,
                "frt_n": a.frt_n,
                "resolution_sum_s": a.resolution_sum_s,
                "resolution_n": a.resolution_n,
            }
            for (_ws, hour, ch), a in acc.convs.items()
        ]
    q = select(AggConversationsHourly).where(
        AggConversationsHourly.workspace_id == workspace_id,
        AggConversationsHourly.hour >= f.start_hour,
        AggConversationsHourly.hour < f.end_hour,
    )
    if f.channel_type:
        q = q.where(AggConversationsHourly.channel_type == f.channel_type)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "hour": r.hour,
            "channel_type": r.channel_type,
            "opened": r.opened,
            "resolved": r.resolved,
            "reopened": r.reopened,
            "frt_sum_s": r.frt_sum_s,
            "frt_n": r.frt_n,
            "resolution_sum_s": r.resolution_sum_s,
            "resolution_n": r.resolution_n,
        }
        for r in rows
    ]


async def _msg_hourly(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters
) -> list[dict[str, Any]]:
    """Rows: {hour, channel_type, agent_id, direction, ai_flag, count}."""
    if f.channel_account_id is not None:
        acc = await _scan_accumulator(session, workspace_id, f)
        return [
            {
                "hour": hour,
                "channel_type": ch,
                "agent_id": agent,
                "direction": direction,
                "ai_flag": ai,
                "count": n,
            }
            for (_ws, hour, ch, agent, direction, ai), n in acc.messages.items()
        ]
    q = select(AggMessagesHourly).where(
        AggMessagesHourly.workspace_id == workspace_id,
        AggMessagesHourly.hour >= f.start_hour,
        AggMessagesHourly.hour < f.end_hour,
    )
    if f.channel_type:
        q = q.where(AggMessagesHourly.channel_type == f.channel_type)
    if f.member_id is not None:
        q = q.where(AggMessagesHourly.agent_id == f.member_id)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "hour": r.hour,
            "channel_type": r.channel_type,
            "agent_id": r.agent_id,
            "direction": r.direction,
            "ai_flag": r.ai_flag,
            "count": r.count,
        }
        for r in rows
    ]


# ==========================================================================
# service overview
# ==========================================================================
async def _live_opened_count(
    session: AsyncSession, workspace_id: uuid.UUID, start: datetime, end: datetime, f: Filters
) -> int:
    q = select(func.count()).select_from(EventRow).where(
        EventRow.workspace_id == workspace_id,
        EventRow.type.in_(_OPENED_TYPES),
        EventRow.occurred_at >= start,
        EventRow.occurred_at < end,
    )
    if f.channel_type:
        q = q.where(EventRow.channel_type == f.channel_type)
    if f.channel_account_id is not None:
        q = q.where(EventRow.channel_account_id == f.channel_account_id)
    return int((await session.execute(q)).scalar_one())


def merge_today_series(
    series: dict[str, tuple[str, int]], current_bucket_key: str, current_ts: str, live_count: int
) -> dict[str, tuple[str, int]]:
    """Override the in-progress bucket with the live count (agg may lag). Pure —
    unit-tested. ``series`` maps bucket_key → (ts, value)."""
    out = dict(series)
    out[current_bucket_key] = (current_ts, live_count)
    return out


async def service_overview(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters, *, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    tz = await workspace_tz(session, workspace_id)
    conv_rows = await _conv_hourly(session, workspace_id, f)

    # trend: opened + reopened per interval bucket
    series: dict[str, tuple[str, int]] = {}
    for r in conv_rows:
        b = collectors.bucket_of_hour(r["hour"], tz, f.interval)
        ts, val = series.get(b.key, (b.ts, 0))
        series[b.key] = (ts, val + r["opened"] + r["reopened"])

    # live-merge the current in-progress bucket
    cur_hour = collectors.floor_hour(now)
    cur_bucket = collectors.bucket_of_hour(cur_hour, tz, f.interval)
    if f.start_hour <= cur_hour < f.end_hour:
        live = await _live_opened_count(session, workspace_id, cur_bucket_start(cur_bucket), now, f)
        series = merge_today_series(series, cur_bucket.key, cur_bucket.ts, live)

    trend = [
        {"ts": ts, "conversations": val}
        for _key, (ts, val) in sorted(series.items(), key=lambda kv: kv[1][0])
    ]

    # KPIs
    day_start, day_end = collectors.local_day_bounds_utc(
        collectors.local_day_of_hour(cur_hour, tz), tz
    )
    new_today = await _live_opened_count(session, workspace_id, day_start, now, f)
    in_progress = await _open_conversations(session, workspace_id, f)
    online = await _online_members_count(session, workspace_id, now)
    return {
        "kpis": {
            "new_conversations_today": new_today,
            "in_progress": in_progress,
            "online_members": online,
        },
        "trend": trend,
    }


def cur_bucket_start(bucket: collectors.Bucket) -> datetime:
    return collectors.ensure_utc(datetime.fromisoformat(bucket.ts))


async def _open_conversations(session: AsyncSession, workspace_id: uuid.UUID, f: Filters) -> int:
    q = select(func.count()).select_from(Conversation).where(
        Conversation.workspace_id == workspace_id, Conversation.status == "open"
    )
    if f.channel_type:
        q = q.where(Conversation.channel_type == f.channel_type)
    if f.channel_account_id is not None:
        q = q.where(Conversation.channel_account_id == f.channel_account_id)
    return int((await session.execute(q)).scalar_one())


async def _online_members_count(
    session: AsyncSession, workspace_id: uuid.UUID, now: datetime
) -> int:
    cutoff = now - timedelta(seconds=_PRESENCE_GRACE_S)
    q = (
        select(func.count(func.distinct(AgentPresenceSession.agent_id)))
        .where(
            AgentPresenceSession.workspace_id == workspace_id,
            AgentPresenceSession.ended_at.is_(None),
            or_(
                AgentPresenceSession.last_heartbeat_at.is_(None),
                AgentPresenceSession.last_heartbeat_at >= cutoff,
            ),
        )
    )
    return int((await session.execute(q)).scalar_one())


# ==========================================================================
# channels
# ==========================================================================
async def channels(session: AsyncSession, workspace_id: uuid.UUID, f: Filters) -> dict[str, Any]:
    conv_rows = await _conv_hourly(session, workspace_id, f)
    msg_rows = await _msg_hourly(session, workspace_id, f)
    by_channel: dict[str, dict[str, int]] = defaultdict(
        lambda: {"conversations": 0, "messages_in": 0, "messages_out": 0}
    )
    for r in conv_rows:
        by_channel[r["channel_type"]]["conversations"] += r["opened"] + r["reopened"]
    for r in msg_rows:
        key = "messages_in" if r["direction"] == "in" else "messages_out"
        by_channel[r["channel_type"]][key] += r["count"]
    rows = [
        {"channel_type": ch or "unknown", **vals}
        for ch, vals in sorted(by_channel.items(), key=lambda kv: -(kv[1]["conversations"]))
    ]
    return {"rows": rows}


# ==========================================================================
# summary (per-agent scorecard)
# ==========================================================================
async def summary(session: AsyncSession, workspace_id: uuid.UUID, f: Filters) -> dict[str, Any]:
    q = select(
        AggAgentHourly.agent_id,
        func.sum(AggAgentHourly.msgs),
        func.sum(AggAgentHourly.convs),
        func.sum(AggAgentHourly.frt_sum_s),
        func.sum(AggAgentHourly.frt_n),
        func.sum(AggAgentHourly.csat_sum),
        func.sum(AggAgentHourly.csat_n),
        func.sum(AggAgentHourly.online_seconds),
    ).where(
        AggAgentHourly.workspace_id == workspace_id,
        AggAgentHourly.hour >= f.start_hour,
        AggAgentHourly.hour < f.end_hour,
    ).group_by(AggAgentHourly.agent_id)
    if f.member_id is not None:
        q = q.where(AggAgentHourly.agent_id == f.member_id)
    rows = (await session.execute(q)).all()

    # resolution time is channel-level (agg_conversations); split per agent is not
    # tracked, so resolution_avg_ms is the workspace resolution mean (shown per
    # row for reference, matching the live layout's single-source column).
    conv_rows = await _conv_hourly(session, workspace_id, f)
    res_sum = sum(r["resolution_sum_s"] for r in conv_rows)
    res_n = sum(r["resolution_n"] for r in conv_rows)
    resolution_avg_ms = int((res_sum / res_n) * 1000) if res_n else 0

    names = await _member_names(session, workspace_id, [r[0] for r in rows])
    agents = []
    for agent_id, msgs, convs, frt_s, frt_n, csat_s, csat_n, online_s in rows:
        # func.sum returns Decimal — coerce before mixing with float
        frt_s, frt_n = int(frt_s or 0), int(frt_n or 0)
        csat_s, csat_n = int(csat_s or 0), int(csat_n or 0)
        agents.append(
            {
                "member_id": str(agent_id),
                "display_name": names.get(agent_id),
                "msgs": int(msgs or 0),
                "convs": int(convs or 0),
                "frt_avg_ms": int((frt_s / frt_n) * 1000) if frt_n else 0,
                "csat_avg": round((csat_s / csat_n) / 5.0, 4) if csat_n else 0.0,
                "resolution_avg_ms": resolution_avg_ms,
                "online_seconds": int(online_s or 0),
            }
        )
    agents.sort(key=lambda a: (-a["msgs"], a["member_id"]))
    return {"agents": agents}


# ==========================================================================
# online time
# ==========================================================================
async def online_time(session: AsyncSession, workspace_id: uuid.UUID, f: Filters) -> dict[str, Any]:
    q = select(
        AggAgentHourly.agent_id, func.sum(AggAgentHourly.online_seconds)
    ).where(
        AggAgentHourly.workspace_id == workspace_id,
        AggAgentHourly.hour >= f.start_hour,
        AggAgentHourly.hour < f.end_hour,
    ).group_by(AggAgentHourly.agent_id)
    if f.member_id is not None:
        q = q.where(AggAgentHourly.agent_id == f.member_id)
    rows = (await session.execute(q)).all()
    names = await _member_names(session, workspace_id, [r[0] for r in rows])
    out = [
        {
            "member_id": str(agent_id),
            "display_name": names.get(agent_id) or str(agent_id)[:8],
            "online_seconds": int(secs or 0),
        }
        for agent_id, secs in rows
    ]
    out.sort(key=lambda r: -r["online_seconds"])
    return {"rows": out}


# ==========================================================================
# customers (客戶分析)
# ==========================================================================
async def customers(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters, dimension: str
) -> dict[str, Any]:
    tz = await workspace_tz(session, workspace_id)
    day_rows = (
        await session.execute(
            select(AggCustomersDaily)
            .where(
                AggCustomersDaily.workspace_id == workspace_id,
                AggCustomersDaily.day_local >= _local_date(f.from_, tz),
                AggCustomersDaily.day_local <= _local_date(f.to, tz),
            )
            .order_by(AggCustomersDaily.day_local)
        )
    ).scalars().all()

    kpis = {
        "new": sum(r.new_count for r in day_rows),
        "new_deduped": sum(r.new_deduped_count for r in day_rows),
        "repeat": sum(r.repeat_count for r in day_rows),
    }
    trend = [
        {
            "date": r.day_local.isoformat(),
            "new": r.new_count,
            "new_deduped": r.new_deduped_count,
            "repeat": r.repeat_count,
        }
        for r in day_rows
    ]
    detail_rows = await _customers_detail(session, workspace_id, f, dimension, tz, day_rows)
    return {"kpis": kpis, "trend": trend, "detail": {"dimension": dimension, "rows": detail_rows}}


async def _customers_detail(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    f: Filters,
    dimension: str,
    tz: str,
    day_rows: list,
) -> list[dict[str, Any]]:
    if dimension in ("day", "week", "month"):
        buckets: dict[str, dict[str, Any]] = {}
        for r in day_rows:
            hour = collectors.local_day_bounds_utc(r.day_local, tz)[0]
            b = collectors.bucket_of_hour(hour, tz, dimension)
            cell = buckets.setdefault(
                b.key, {"期間": b.key, "新增客戶數": 0, "新增不重複客戶數": 0, "重複數": 0, "_sort": b.sort}
            )
            cell["新增客戶數"] += r.new_count
            cell["新增不重複客戶數"] += r.new_deduped_count
            cell["重複數"] += r.repeat_count
        return [
            {k: v for k, v in cell.items() if k != "_sort"}
            for cell in sorted(buckets.values(), key=lambda c: c["_sort"])
        ]
    if dimension == "hour":
        return await _customers_by_hour(session, workspace_id, f, tz)
    if dimension == "member":
        return await _customers_by_member(session, workspace_id, f)
    if dimension in ("channel", "account"):
        by = "account" if dimension == "account" else "channel"
        return await _customers_by_channel(session, workspace_id, f, by=by)
    return []


_first_seen = func.coalesce(Contact.first_seen_at, Contact.created_at)


async def _customers_by_hour(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters, tz: str
) -> list[dict[str, Any]]:
    new_rows = (
        await session.execute(
            select(_first_seen, Contact.merged_into_id).where(
                Contact.workspace_id == workspace_id,
                _first_seen >= f.start_hour,
                _first_seen < f.end_hour,
            )
        )
    ).all()
    buckets: dict[str, dict[str, Any]] = {}

    def cell(ts: datetime) -> dict[str, Any]:
        b = collectors.bucket_of_hour(collectors.floor_hour(ts), tz, "hour")
        return buckets.setdefault(
            b.key, {"期間": b.key, "新增客戶數": 0, "新增不重複客戶數": 0, "重複數": 0, "_sort": b.sort}
        )

    for first_seen, merged in new_rows:
        c = cell(first_seen)
        c["新增客戶數"] += 1
        if merged is None:
            c["新增不重複客戶數"] += 1
    repeat_rows = (
        await session.execute(
            select(EventRow.occurred_at)
            .select_from(EventRow)
            .join(Contact, Contact.id == EventRow.contact_id)
            .where(
                EventRow.workspace_id == workspace_id,
                EventRow.type.in_(_OPENED_TYPES),
                EventRow.occurred_at >= f.start_hour,
                EventRow.occurred_at < f.end_hour,
                EventRow.contact_id.isnot(None),
                _first_seen < EventRow.occurred_at,
            )
        )
    ).all()
    for (ts,) in repeat_rows:
        cell(ts)["重複數"] += 1
    return [
        {k: v for k, v in c.items() if k != "_sort"}
        for c in sorted(buckets.values(), key=lambda c: c["_sort"])
    ]


async def _customers_by_member(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                Conversation.assignee_member_id,
                func.count(func.distinct(Contact.id)),
                func.count(func.distinct(Contact.id)).filter(Contact.merged_into_id.is_(None)),
            )
            .select_from(Contact)
            .join(Conversation, Conversation.contact_id == Contact.id)
            .where(
                Contact.workspace_id == workspace_id,
                _first_seen >= f.start_hour,
                _first_seen < f.end_hour,
            )
            .group_by(Conversation.assignee_member_id)
        )
    ).all()
    member_ids = [r[0] for r in rows if r[0]]
    names = await _member_names(session, workspace_id, member_ids)
    groups = await _member_groups(session, workspace_id, member_ids)
    out = []
    for member_id, new_c, dedup_c in rows:
        out.append(
            {
                "接待成員": names.get(member_id) or ("未分配" if member_id is None else str(member_id)[:8]),
                "分組": groups.get(member_id, ""),
                "新增客戶數": int(new_c or 0),
                "新增不重複客戶數": int(dedup_c or 0),
                "重複數": 0,
            }
        )
    out.sort(key=lambda r: -r["新增客戶數"])
    return out


async def _customers_by_channel(
    session: AsyncSession, workspace_id: uuid.UUID, f: Filters, *, by: str
) -> list[dict[str, Any]]:
    group_col = Conversation.channel_account_id if by == "account" else Conversation.channel_type
    rows = (
        await session.execute(
            select(
                group_col,
                func.count(func.distinct(Contact.id)),
                func.count(func.distinct(Contact.id)).filter(Contact.merged_into_id.is_(None)),
            )
            .select_from(Contact)
            .join(Conversation, Conversation.contact_id == Contact.id)
            .where(
                Contact.workspace_id == workspace_id,
                _first_seen >= f.start_hour,
                _first_seen < f.end_hour,
            )
            .group_by(group_col)
        )
    ).all()
    label = "社群帳號" if by == "account" else "社群管道"
    acct_names: dict[Any, str] = {}
    if by == "account":
        ids = [r[0] for r in rows if r[0]]
        if ids:
            for aid, name in (
                await session.execute(
                    select(ChannelAccount.id, ChannelAccount.display_name).where(
                        ChannelAccount.id.in_(ids)
                    )
                )
            ).all():
                acct_names[aid] = name
    out = []
    for key, new_c, dedup_c in rows:
        display = acct_names.get(key, str(key)) if by == "account" else (key or "unknown")
        out.append(
            {
                label: display,
                "新增客戶數": int(new_c or 0),
                "新增不重複客戶數": int(dedup_c or 0),
                "重複數": 0,
            }
        )
    out.sort(key=lambda r: -r["新增客戶數"])
    return out


# ==========================================================================
# ads (廣告分析) — reads agg_ads_daily
# ==========================================================================
async def ads(
    session: AsyncSession, workspace_id: uuid.UUID, platform: str, f: Filters
) -> dict[str, Any]:
    from ...models.reports import AggAdsDaily

    tz = await workspace_tz(session, workspace_id)
    rows = (
        await session.execute(
            select(
                AggAdsDaily.campaign_id,
                AggAdsDaily.ad_id,
                func.sum(AggAdsDaily.conversations),
                func.sum(AggAdsDaily.messages),
                func.sum(AggAdsDaily.leads),
            )
            .where(
                AggAdsDaily.workspace_id == workspace_id,
                AggAdsDaily.platform == platform,
                AggAdsDaily.day_local >= _local_date(f.from_, tz),
                AggAdsDaily.day_local <= _local_date(f.to, tz),
            )
            .group_by(AggAdsDaily.campaign_id, AggAdsDaily.ad_id)
        )
    ).all()
    out = [
        {
            "廣告系列": campaign or "—",
            "廣告": ad or "—",
            "會話數": int(convs or 0),
            "訊息數": int(msgs or 0),
            "留資數": int(leads or 0),
        }
        for campaign, ad, convs, msgs, leads in rows
    ]
    out.sort(key=lambda r: -r["會話數"])
    return {"rows": out}


# ==========================================================================
# member helpers
# ==========================================================================
async def _member_names(
    session: AsyncSession, workspace_id: uuid.UUID, member_ids: list
) -> dict[uuid.UUID, str]:
    ids = [m for m in member_ids if m]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(WorkspaceMember.id, WorkspaceMember.display_name).where(
                WorkspaceMember.id.in_(set(ids))
            )
        )
    ).all()
    return {mid: name for mid, name in rows}


async def _member_groups(
    session: AsyncSession, workspace_id: uuid.UUID, member_ids: list
) -> dict[uuid.UUID, str]:
    ids = [m for m in member_ids if m]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(MemberGroupMember.member_id, MemberGroup.name)
            .join(MemberGroup, MemberGroup.id == MemberGroupMember.group_id)
            .where(MemberGroupMember.member_id.in_(set(ids)))
        )
    ).all()
    out: dict[uuid.UUID, str] = {}
    for member_id, gname in rows:
        out[member_id] = f"{out[member_id]}, {gname}" if member_id in out else gname
    return out


# ==========================================================================
# parse helpers
# ==========================================================================
def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return collectors.ensure_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def _parse_uuid(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _local_date(dt: datetime, tz: str):
    return collectors.ensure_utc(dt).astimezone(collectors.zone(tz)).date()
