"""Nightly distinct-count day tables (plan 附錄 B.4).

Distinct counts (new / deduped / repeat customers; per-ad conversations) cannot
be composed from additive hourly buckets, so they get their own per-workspace-
local-day tables computed at 03:00 workspace-local:

- ``agg_customers_daily`` — 客戶分析: 新增 / 去重(不重複) / 重複
- ``agg_ads_daily``       — 廣告分析: conversations / messages / leads per (platform,
                            campaign, ad), from ``conversation_attribution``

Both recompute a trailing window each night (default 2 local days) so late data
and same-day merges settle. Everything is bounded by the workspace-local day's
UTC bounds (DST-safe via ``collectors.local_day_bounds_utc``).
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.contacts import Contact
from ..models.conversations import Conversation
from ..models.misc import EventRow
from ..models.reports import AggAdsDaily, AggCustomersDaily, ConversationAttribution
from ..models.tenancy import Workspace
from . import attribution as attr
from . import collectors

log = logging.getLogger("smartchat.analytics.daily")

_OPENED_TYPES = ("conversation.created", "conversation.reopened")
_first_seen = func.coalesce(Contact.first_seen_at, Contact.created_at)


# ==========================================================================
# customers (客戶分析)
# ==========================================================================
async def recompute_customers_day(
    session: AsyncSession, workspace_id: uuid.UUID, day_local: date, tz_name: str | None
) -> None:
    start, end = collectors.local_day_bounds_utc(day_local, tz_name)

    new_count = (
        await session.execute(
            select(func.count())
            .select_from(Contact)
            .where(Contact.workspace_id == workspace_id, _first_seen >= start, _first_seen < end)
        )
    ).scalar_one()

    new_deduped = (
        await session.execute(
            select(func.count())
            .select_from(Contact)
            .where(
                Contact.workspace_id == workspace_id,
                _first_seen >= start,
                _first_seen < end,
                Contact.merged_into_id.is_(None),
            )
        )
    ).scalar_one()

    merged_away = (
        await session.execute(
            select(func.count())
            .select_from(Contact)
            .where(
                Contact.workspace_id == workspace_id,
                Contact.merged_into_id.isnot(None),
                Contact.updated_at >= start,
                Contact.updated_at < end,
            )
        )
    ).scalar_one()

    # 重複: contacts first seen BEFORE this day who opened a service cycle today
    repeat_count = (
        await session.execute(
            select(func.count(func.distinct(EventRow.contact_id)))
            .select_from(EventRow)
            .join(Contact, Contact.id == EventRow.contact_id)
            .where(
                EventRow.workspace_id == workspace_id,
                EventRow.type.in_(_OPENED_TYPES),
                EventRow.occurred_at >= start,
                EventRow.occurred_at < end,
                EventRow.contact_id.isnot(None),
                _first_seen < start,
            )
        )
    ).scalar_one()

    stmt = pg_insert(AggCustomersDaily).values(
        workspace_id=workspace_id,
        day_local=day_local,
        new_count=int(new_count),
        new_deduped_count=int(new_deduped),
        repeat_count=int(repeat_count),
        merged_away=int(merged_away),
    ).on_conflict_do_update(
        index_elements=["workspace_id", "day_local"],
        set_={
            "new_count": int(new_count),
            "new_deduped_count": int(new_deduped),
            "repeat_count": int(repeat_count),
            "merged_away": int(merged_away),
        },
    )
    await session.execute(stmt)


# ==========================================================================
# ads (廣告分析)
# ==========================================================================
async def recompute_ads_day(
    session: AsyncSession, workspace_id: uuid.UUID, day_local: date, tz_name: str | None
) -> None:
    start, end = collectors.local_day_bounds_utc(day_local, tz_name)

    # conversations created today that carry attribution
    rows = (
        await session.execute(
            select(
                ConversationAttribution.source,
                ConversationAttribution.campaign_id,
                ConversationAttribution.ad_id,
                Conversation.channel_type,
                Conversation.id,
            )
            .join(Conversation, Conversation.id == ConversationAttribution.conversation_id)
            .where(
                ConversationAttribution.workspace_id == workspace_id,
                Conversation.created_at >= start,
                Conversation.created_at < end,
            )
        )
    ).all()

    # clear the day then re-insert (a platform may go to zero)
    await session.execute(
        AggAdsDaily.__table__.delete().where(
            AggAdsDaily.workspace_id == workspace_id, AggAdsDaily.day_local == day_local
        )
    )
    if not rows:
        return

    agg: dict[tuple[str, str, str], dict] = {}
    conv_ids: list[uuid.UUID] = []
    conv_key: dict[uuid.UUID, tuple[str, str, str]] = {}
    for source, campaign, ad, channel_type, conv_id in rows:
        platform = attr.Attribution(source=source).platform(channel_type)
        if platform is None:
            continue
        key = (platform, campaign or "", ad or "")
        bucket = agg.setdefault(key, {"conversations": 0, "messages": 0, "leads": 0})
        bucket["conversations"] += 1
        conv_ids.append(conv_id)
        conv_key[conv_id] = key

    if conv_ids:
        msg_rows = (
            await session.execute(
                select(EventRow.conversation_id, func.count())
                .where(
                    EventRow.workspace_id == workspace_id,
                    EventRow.type == "message.created",
                    EventRow.conversation_id.in_(conv_ids),
                    EventRow.occurred_at >= start,
                    EventRow.occurred_at < end,
                )
                .group_by(EventRow.conversation_id)
            )
        ).all()
        for conv_id, n in msg_rows:
            key = conv_key.get(conv_id)
            if key:
                agg[key]["messages"] += int(n)

        lead_rows = (
            await session.execute(
                select(EventRow.conversation_id, func.count())
                .where(
                    EventRow.workspace_id == workspace_id,
                    EventRow.type == "lead.submitted",
                    EventRow.conversation_id.in_(conv_ids),
                    EventRow.occurred_at >= start,
                    EventRow.occurred_at < end,
                )
                .group_by(EventRow.conversation_id)
            )
        ).all()
        for conv_id, n in lead_rows:
            key = conv_key.get(conv_id)
            if key:
                agg[key]["leads"] += int(n)

    values = [
        {
            "workspace_id": workspace_id,
            "day_local": day_local,
            "platform": platform,
            "campaign_id": campaign,
            "ad_id": ad,
            "conversations": v["conversations"],
            "messages": v["messages"],
            "leads": v["leads"],
            "spend_micros": None,
        }
        for (platform, campaign, ad), v in agg.items()
    ]
    if values:
        await session.execute(pg_insert(AggAdsDaily).values(values))


# ==========================================================================
# nightly orchestration
# ==========================================================================
def _ws_tz(ws: Workspace) -> str | None:
    return (ws.settings or {}).get("timezone") or "UTC"


async def run_daily(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    back_days: int = 2,
) -> int:
    """Recompute the trailing ``back_days`` local days (+ today) of the distinct
    tables for every active workspace. Returns workspace-days processed."""
    now = now or datetime.now(UTC)
    processed = 0
    async with session_factory() as session:
        workspaces = (
            await session.execute(select(Workspace).where(Workspace.status == "active"))
        ).scalars().all()
    for ws in workspaces:
        tz = _ws_tz(ws)
        today_local = collectors.ensure_utc(now).astimezone(collectors.zone(tz)).date()
        days = [today_local - timedelta(days=d) for d in range(back_days + 1)]
        for day_local in days:
            try:
                async with session_factory() as session:
                    async with session.begin():
                        await recompute_customers_day(session, ws.id, day_local, tz)
                        await recompute_ads_day(session, ws.id, day_local, tz)
                processed += 1
            except Exception:  # noqa: BLE001 — one bad day must not sink the sweep
                log.exception("daily rollup failed ws=%s day=%s", ws.id, day_local)
    return processed
