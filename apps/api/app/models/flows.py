"""Flow engine data model (plan 附錄 B.1).

The visual automation engine. A `flow` owns a mutable `draft_graph` and, once
published, a chain of frozen `flow_versions` (a running session pins itself to
its version and is NEVER hot-swapped). Publishing also denormalises the graph's
trigger node into indexed `flow_triggers` rows so the router matches with plain
SQL instead of scanning JSON. Runtime state lives in `flow_sessions` (+ per-node
`flow_session_steps` for funnels); frequency capping is backed by
`flow_trigger_log`; analytics roll up into `flow_stats_daily` /
`flow_stats_users` (the latter for exact distinct-user counts).

`flow_templates` is GLOBAL (no workspace_id) — the shared template gallery;
"use" deep-copies its graph into a new draft.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_pk, workspace_fk


class FlowCategory(Base):
    """流程分類資料夾 (folder in the flow list)."""

    __tablename__ = "flow_categories"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_flow_categories_ws_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()


class Flow(Base):
    """A single automation. `draft_graph` is the editable canvas; publishing
    freezes it into a flow_versions row and points published_version_id at it.
    `priority` orders multi-flow trigger hits — smaller wins (plan B.1)."""

    __tablename__ = "flows"
    __table_args__ = (
        Index("ix_flows_ws_channel_enabled", "workspace_id", "channel_type", "enabled"),
        Index("ix_flows_ws_category", "workspace_id", "category_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False, default="widget")
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("flow_categories.id", ondelete="SET NULL")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # smaller = higher precedence when several flows match the same event
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    published_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("flow_versions.id", ondelete="SET NULL", use_alter=True,
                   name="fk_flows_published_version"),
    )
    # editable working copy — graph_schema.Graph serialised
    draft_graph: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class FlowVersion(Base):
    """A frozen published graph. Running sessions pin to their version_id so an
    edit-and-republish never mutates in-flight executions."""

    __tablename__ = "flow_versions"
    __table_args__ = (
        UniqueConstraint("flow_id", "version_no", name="uq_flow_versions_flow_no"),
        Index("ix_flow_versions_ws_flow", "workspace_id", "flow_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    flow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("flows.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    graph: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    published_at: Mapped[datetime] = created_at_col()
    published_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )


class FlowTrigger(Base):
    """Denormalised, indexed trigger table (plan B.1): published flows write one
    row per trigger definition so the flow-engine router hits it with a SQL
    lookup keyed by (workspace, channel_type, trigger_type, enabled) rather than
    scanning graph JSON. `channel_type` and `priority` are copied from the flow
    for filter+sort in the same query. `freq_cap` = {scope, count, window_s}."""

    __tablename__ = "flow_triggers"
    __table_args__ = (
        Index("ix_flow_triggers_route", "workspace_id", "channel_type", "trigger_type", "enabled"),
        Index("ix_flow_triggers_flow", "workspace_id", "flow_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    flow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("flows.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("flow_versions.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)  # trigger node in the graph
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False, default="widget")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    freq_cap: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()


class KeywordDict(Base):
    """詞庫 — reusable keyword set referenced by visitor_message triggers."""

    __tablename__ = "keyword_dicts"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_keyword_dicts_ws_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class KeywordDictItem(Base):
    __tablename__ = "keyword_dict_items"
    __table_args__ = (Index("ix_keyword_dict_items_dict", "workspace_id", "dict_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    dict_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("keyword_dicts.id", ondelete="CASCADE"), nullable=False
    )
    keyword: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class FlowSession(Base):
    """Per-conversation runtime state (plan B.1). One active session per
    conversation; `waiting` holds the suspend descriptor (reply/button/timer),
    `variables` the namespaced scratch space, `current_node_id` the cursor.
    `mode` = live | test (sandbox). `seq` bumps on every wakeup so a resumed
    timer job is idempotent (expected_seq guard)."""

    __tablename__ = "flow_sessions"
    __table_args__ = (
        Index("ix_flow_sessions_ws_conv", "workspace_id", "conversation_id"),
        Index("ix_flow_sessions_ws_flow_created", "workspace_id", "flow_id", "created_at"),
        Index("ix_flow_sessions_wakeup", "status", "wakeup_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    flow_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("flows.id", ondelete="CASCADE"), nullable=False
    )
    flow_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("flow_versions.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="live")  # live/test
    # running/delayed/waiting_reply/waiting_button/completed/ended/failed/expired/cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    current_node_id: Mapped[str | None] = mapped_column(String(64))
    variables: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    waiting: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engaged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wakeup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class FlowSessionStep(Base):
    """Per-node execution record — the drill-down / funnel source."""

    __tablename__ = "flow_session_steps"
    __table_args__ = (Index("ix_flow_session_steps_session_seq", "session_id", "seq"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("flow_sessions.id", ondelete="CASCADE"), nullable=False
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="ok")  # ok/error/skipped
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ts: Mapped[datetime] = created_at_col()


class FlowTriggerLog(Base):
    """Durable backing for frequency capping (Redis is the hot pre-check). One
    row per fired trigger per contact/conversation, queried in a time window."""

    __tablename__ = "flow_trigger_log"
    __table_args__ = (
        Index("ix_flow_trigger_log_cap", "workspace_id", "flow_id", "contact_id", "created_at"),
        Index("ix_flow_trigger_log_trigger", "workspace_id", "trigger_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    flow_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # triggered/suppressed
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="triggered")
    created_at: Mapped[datetime] = created_at_col()


class FlowStatsDaily(Base):
    """Rolled daily counters (plan B.1 統計口徑). Distinct-user figures are
    materialised from flow_stats_users; session/step counts accumulate here."""

    __tablename__ = "flow_stats_daily"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    flow_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)  # workspace-tz day boundary
    triggered_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggered_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engaged_users: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = updated_at_col()


class FlowStatsUser(Base):
    """Exact per-day distinct-user backing (never COUNT(DISTINCT) at query
    time). `engaged` = did ≥1 interactive step; `completed` = reached a terminal
    node."""

    __tablename__ = "flow_stats_users"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    flow_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    engaged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = updated_at_col()


class FlowTemplate(Base):
    """GLOBAL template gallery (no workspace_id). `name`/`description` are i18n
    jsonb ({lang: text}); `graph` is a graph_schema.Graph; `preview` holds
    thumbnail / summary meta. "Use" deep-copies graph into a workspace draft."""

    __tablename__ = "flow_templates"
    __table_args__ = (Index("ix_flow_templates_channel_category", "channel_type", "category"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False, default="widget")
    category: Mapped[str] = mapped_column(String(48), nullable=False, default="general")
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # i18n
    description: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # i18n
    graph: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
