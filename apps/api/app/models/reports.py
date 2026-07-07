"""Reports / analytics aggregates (plan 附錄 B.4).

The raw store is the P1 ``events`` table itself (no second write path). A rollup
consumer folds events into these hourly/daily aggregate tables; the watermark
row makes it resumable. Golden rules:

- **Aggregate in UTC hours**, localise to the workspace timezone at query time
  (DST / timezone changes never force a recompute).
- Store **sum + count, never averages** — averages are derived at read time so
  time buckets stay additively mergeable.
- Distinct-count metrics (new / deduped / repeat customers) cannot be composed
  from hourly buckets, so they get a nightly per-workspace-day table
  (``agg_customers_daily``) computed at 03:00 workspace-local.
- Agent online time is not event-derived; it is measured from
  ``agent_presence_sessions`` (WS heartbeat, 60s grace close-out).

Composite PKs use fixed sentinels (``NIL_UUID`` / empty string) for the
"no agent" / "no channel" slots so the rollup can upsert without NULL-in-PK.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_pk, workspace_fk

NIL_UUID = "00000000-0000-0000-0000-000000000000"
_NIL_UUID_SQL = text(f"'{NIL_UUID}'::uuid")
_EMPTY_SQL = text("''")


class AggMessagesHourly(Base):
    """Message volume by (workspace, UTC hour, channel, agent, direction,
    ai_flag). ``agent_id`` = NIL_UUID for inbound / non-agent messages."""

    __tablename__ = "agg_messages_hourly"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    channel_type: Mapped[str] = mapped_column(
        String(24), primary_key=True, server_default=_EMPTY_SQL
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=_NIL_UUID_SQL
    )
    direction: Mapped[str] = mapped_column(String(3), primary_key=True)  # in/out
    ai_flag: Mapped[bool] = mapped_column(
        primary_key=True, server_default=text("false")
    )
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class AggConversationsHourly(Base):
    """Conversation lifecycle counts + first-response / resolution time sums by
    (workspace, UTC hour, channel). Averages derived as sum ÷ n at query time."""

    __tablename__ = "agg_conversations_hourly"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    channel_type: Mapped[str] = mapped_column(
        String(24), primary_key=True, server_default=_EMPTY_SQL
    )
    opened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reopened: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frt_sum_s: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    frt_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolution_sum_s: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    resolution_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggAgentHourly(Base):
    """Per-agent hourly productivity (綜合報表). CSAT + FRT stored as sum+count;
    online_seconds folded from agent_presence_sessions in the same rollup."""

    __tablename__ = "agg_agent_hourly"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    msgs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    convs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    frt_sum_s: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    frt_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    csat_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    csat_n: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    online_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggCustomersDaily(Base):
    """Distinct-count customer metrics per workspace-local day (客戶分析:
    新增 / 去重 / 重複). Computed nightly at 03:00 workspace-local — distinct
    counts cannot be composed from hourly buckets."""

    __tablename__ = "agg_customers_daily"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    day_local: Mapped[date] = mapped_column(Date, primary_key=True)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 新增
    new_deduped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 去重
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 重複
    merged_away: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AggAdsDaily(Base):
    """Ad attribution per day (廣告分析: Facebook 廣告 + 訊息廣告 CTWA).
    ``platform`` = facebook / messenger; campaign_id / ad_id use '' sentinel."""

    __tablename__ = "agg_ads_daily"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    day_local: Mapped[date] = mapped_column(Date, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(128), primary_key=True, server_default=_EMPTY_SQL)
    ad_id: Mapped[str] = mapped_column(String(128), primary_key=True, server_default=_EMPTY_SQL)
    conversations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spend_micros: Mapped[int | None] = mapped_column(BigInteger)


class AgentPresenceSession(Base):
    """Agent online interval (在線時長). Opened on WS connect, kept alive by
    heartbeat, closed with a 60s grace window. Online time = Σ(ended−started)."""

    __tablename__ = "agent_presence_sessions"
    __table_args__ = (
        Index("ix_agent_presence_ws_agent_started", "workspace_id", "agent_id", "started_at"),
        Index("ix_agent_presence_open", "workspace_id", "ended_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = created_at_col()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str | None] = mapped_column(String(16))  # web/mobile


class ConversationAttribution(Base):
    """Ad / referral attribution captured at conversation creation (CTWA ref
    payload, split-link tracking code, UTM). Feeds the ad reports."""

    __tablename__ = "conversation_attribution"
    __table_args__ = (
        Index("ix_conv_attribution_ws_campaign", "workspace_id", "campaign_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="direct")
    ad_id: Mapped[str | None] = mapped_column(String(128))
    campaign_id: Mapped[str | None] = mapped_column(String(128))
    ref_code: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()


class ReportShare(Base):
    """Public share of a frozen report config (plan B.4). ``report_config`` is
    the snapshotted query; optional password + expiry gate the /shared-report
    endpoint."""

    __tablename__ = "report_shares"
    __table_args__ = (Index("ix_report_shares_ws", "workspace_id"),)

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    report_key: Mapped[str] = mapped_column(String(48), nullable=False)
    report_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()


class ReportExport(Base):
    """Async CSV export job → MinIO signed URL (plan B.4)."""

    __tablename__ = "report_exports"
    __table_args__ = (Index("ix_report_exports_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    report_key: Mapped[str] = mapped_column(String(48), nullable=False)
    # pending/running/ready/failed
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    storage_key: Mapped[str | None] = mapped_column(Text)
    row_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ReportAiSummary(Base):
    """Nightly LLM day-over-day digest (AI 分析), 20 points, Pro+ gated."""

    __tablename__ = "report_ai_summaries"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = created_at_col()


class RollupWatermark(Base):
    """Resumable rollup cursor per aggregate (global, single consumer). The
    rollup reads events ordered by (occurred_at, id) and advances the watermark;
    a nightly 48h look-back re-folds late arrivals."""

    __tablename__ = "rollup_watermark"

    aggregate: Mapped[str] = mapped_column(String(48), primary_key=True)
    last_event_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_at_col()
