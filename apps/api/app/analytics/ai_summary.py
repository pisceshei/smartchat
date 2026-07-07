"""Nightly AI report digest (plan 附錄 B.4 AI 分析).

For each Pro+ workspace, at 03:xx workspace-local we compute day-over-day deltas
across the aggregates, ask the ``smart``-tier LLM for a short operator-facing
digest, and store it in ``report_ai_summaries`` (one row per workspace-day). The
job costs **20 AI points** metered through ``points.check_and_decr`` (hard-stop
at 0 balance), and is gated to Pro+ plans.

Degrades safely: no LLM configured, no points, or a model error → the day is
skipped, never crashes the sweep.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import redis.asyncio as aioredis
from py_contracts.llm import LLMMessage
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.reports import (
    AggAgentHourly,
    AggConversationsHourly,
    AggCustomersDaily,
    AggMessagesHourly,
    ReportAiSummary,
)
from ..models.tenancy import Workspace
from ..services import points, quotas
from ..services.llm_client import get_default_llm
from ..services.redis_client import get_redis
from . import collectors

log = logging.getLogger("smartchat.analytics.ai_summary")

AI_SUMMARY_COST = 20
_PRO_PLUS = frozenset({"pro", "max", "custom"})

_SYSTEM = (
    "你是一位客服營運分析師。根據提供的『今日 vs 昨日』客服指標，"
    "用繁體中文寫一段 4-6 句、可操作的每日摘要：先點出關鍵變化（會話量、"
    "新客、首次回應時間、滿意度、在線時長），再給 1-2 條具體建議。"
    "只輸出摘要本文，不要標題或客套話。"
)


def is_pro_plus(limits: dict) -> bool:
    if limits.get("ai_summary") or limits.get("reports_ai"):
        return True
    return limits.get("_plan_code") in _PRO_PLUS


@dataclass
class DayMetrics:
    conversations: int = 0
    resolved: int = 0
    messages_in: int = 0
    messages_out: int = 0
    frt_avg_s: float = 0.0
    resolution_avg_s: float = 0.0
    new_customers: int = 0
    repeat_customers: int = 0
    csat_avg: float = 0.0
    online_hours: float = 0.0


async def day_metrics(
    session: AsyncSession, workspace_id: uuid.UUID, day_local: date, tz: str
) -> DayMetrics:
    start, end = collectors.local_day_bounds_utc(day_local, tz)
    m = DayMetrics()

    conv = (
        await session.execute(
            select(
                func.coalesce(func.sum(AggConversationsHourly.opened), 0),
                func.coalesce(func.sum(AggConversationsHourly.reopened), 0),
                func.coalesce(func.sum(AggConversationsHourly.resolved), 0),
                func.coalesce(func.sum(AggConversationsHourly.frt_sum_s), 0),
                func.coalesce(func.sum(AggConversationsHourly.frt_n), 0),
                func.coalesce(func.sum(AggConversationsHourly.resolution_sum_s), 0),
                func.coalesce(func.sum(AggConversationsHourly.resolution_n), 0),
            ).where(
                AggConversationsHourly.workspace_id == workspace_id,
                AggConversationsHourly.hour >= start,
                AggConversationsHourly.hour < end,
            )
        )
    ).one()
    opened, reopened, resolved, frt_s, frt_n, res_s, res_n = (int(x) for x in conv)
    m.conversations = opened + reopened
    m.resolved = resolved
    m.frt_avg_s = (frt_s / frt_n) if frt_n else 0.0
    m.resolution_avg_s = (res_s / res_n) if res_n else 0.0

    msgs = (
        await session.execute(
            select(AggMessagesHourly.direction, func.sum(AggMessagesHourly.count))
            .where(
                AggMessagesHourly.workspace_id == workspace_id,
                AggMessagesHourly.hour >= start,
                AggMessagesHourly.hour < end,
            )
            .group_by(AggMessagesHourly.direction)
        )
    ).all()
    for direction, n in msgs:
        if direction == "in":
            m.messages_in = int(n or 0)
        else:
            m.messages_out = int(n or 0)

    cust = (
        await session.execute(
            select(AggCustomersDaily.new_count, AggCustomersDaily.repeat_count).where(
                AggCustomersDaily.workspace_id == workspace_id,
                AggCustomersDaily.day_local == day_local,
            )
        )
    ).first()
    if cust:
        m.new_customers = int(cust[0] or 0)
        m.repeat_customers = int(cust[1] or 0)

    agent = (
        await session.execute(
            select(
                func.coalesce(func.sum(AggAgentHourly.csat_sum), 0),
                func.coalesce(func.sum(AggAgentHourly.csat_n), 0),
                func.coalesce(func.sum(AggAgentHourly.online_seconds), 0),
            ).where(
                AggAgentHourly.workspace_id == workspace_id,
                AggAgentHourly.hour >= start,
                AggAgentHourly.hour < end,
            )
        )
    ).one()
    csat_s, csat_n, online_s = (int(x) for x in agent)
    m.csat_avg = (csat_s / csat_n / 5.0) if csat_n else 0.0
    m.online_hours = round(online_s / 3600, 1)
    return m


def _prompt(today: DayMetrics, prev: DayMetrics, day_local: date) -> str:
    def line(label: str, a: float, b: float, unit: str = "") -> str:
        return f"- {label}：今日 {a:g}{unit}，昨日 {b:g}{unit}"

    return "\n".join(
        [
            f"日期：{day_local.isoformat()}",
            line("新會話數", today.conversations, prev.conversations),
            line("已解決", today.resolved, prev.resolved),
            line("客戶訊息", today.messages_in, prev.messages_in),
            line("客服訊息", today.messages_out, prev.messages_out),
            line("首次回應(秒)", round(today.frt_avg_s), round(prev.frt_avg_s), "s"),
            line("解決時長(秒)", round(today.resolution_avg_s), round(prev.resolution_avg_s), "s"),
            line("新客戶", today.new_customers, prev.new_customers),
            line("回頭客", today.repeat_customers, prev.repeat_customers),
            line("滿意度", round(today.csat_avg * 100), round(prev.csat_avg * 100), "%"),
            line("在線時長(小時)", today.online_hours, prev.online_hours, "h"),
        ]
    )


def _has_signal(m: DayMetrics) -> bool:
    return bool(m.conversations or m.messages_in or m.messages_out or m.new_customers)


async def generate_for_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis,
    workspace_id: uuid.UUID,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> str | None:
    """Generate + store the digest for the workspace's most recently completed
    local day. Returns the text, or None if skipped (not Pro+, no signal, no
    points, already done, or LLM error)."""
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        ws = await session.get(Workspace, workspace_id)
        if ws is None:
            return None
        tz = (ws.settings or {}).get("timezone", "UTC")
        limits = await quotas.effective_limits(session, redis, workspace_id, use_cache=False)
        if not is_pro_plus(limits):
            return None
        target_day = collectors.ensure_utc(now).astimezone(collectors.zone(tz)).date() - timedelta(days=1)
        if not force:
            existing = await session.get(ReportAiSummary, (workspace_id, target_day))
            if existing is not None and existing.text:
                return existing.text
        today = await day_metrics(session, workspace_id, target_day, tz)
        prev = await day_metrics(session, workspace_id, target_day - timedelta(days=1), tz)
    if not _has_signal(today):
        return None

    # meter points before spending the model
    async with session_factory() as session:
        async with session.begin():
            spend = await points.check_and_decr(
                session,
                redis,
                workspace_id=workspace_id,
                cost=AI_SUMMARY_COST,
                reason="ai_summary",
                ref_type="report",
                ref_id=target_day.isoformat(),
            )
        if not spend.ok:
            log.info("ai summary skipped ws=%s: insufficient points", workspace_id)
            return None

    llm = get_default_llm()
    model_name = ""
    try:
        text = await llm.complete(
            tier="smart",
            system=_SYSTEM,
            messages=[LLMMessage(role="user", content=_prompt(today, prev, target_day))],
            max_tokens=600,
            temperature=0.4,
        )
    except Exception:  # noqa: BLE001 — refund the points, skip the day
        log.exception("ai summary LLM failed ws=%s", workspace_id)
        async with session_factory() as session:
            async with session.begin():
                await points.refund(
                    session,
                    redis,
                    workspace_id=workspace_id,
                    points=AI_SUMMARY_COST,
                    reason="ai_summary_refund",
                    ref_type="report",
                    ref_id=target_day.isoformat(),
                )
        return None

    text = (text or "").strip()
    async with session_factory() as session:
        async with session.begin():
            stmt = pg_insert(ReportAiSummary).values(
                workspace_id=workspace_id, day=target_day, text=text, model=model_name or None
            ).on_conflict_do_update(
                index_elements=["workspace_id", "day"], set_={"text": text, "model": model_name or None}
            )
            await session.execute(stmt)
    return text


async def run_nightly_ai(
    session_factory: async_sessionmaker[AsyncSession],
    redis: aioredis.Redis | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Generate digests for every active Pro+ workspace. Returns count written."""
    redis = redis or get_redis()
    written = 0
    async with session_factory() as session:
        ws_ids = (
            await session.execute(select(Workspace.id).where(Workspace.status == "active"))
        ).scalars().all()
    for ws_id in ws_ids:
        try:
            if await generate_for_workspace(session_factory, redis, ws_id, now=now):
                written += 1
        except Exception:  # noqa: BLE001
            log.exception("nightly ai summary failed ws=%s", ws_id)
    return written
