"""Tenancy / plans / metering (plan 附錄 A.1)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_pk, workspace_fk


class Plan(Base):
    """free / pro / max / custom. `limits` is the single source of quota truth:
    seats, official_channels, hosted_devices, widgets, mac_monthly,
    monthly_replies, ai_points_monthly, history_days, broadcast, brand_removal,
    openapi, webhook, translation_chars_monthly. -1 = unlimited."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    price_usd_month: Mapped[float | None] = mapped_column(Numeric(10, 2))
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()


class Workspace(Base):
    """Tenant (=project). `settings` jsonb: timezone, auto_close_{days,hours,minutes},
    assignment (mode/prefer_bot/keep_managed/auto_assign), offline_reply_mode."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("plans.code"), nullable=False, default="free"
    )
    # active/suspended/deleted
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL", use_alter=True)
    )
    # per-workspace data key, wrapped by CREDENTIALS_MASTER_KEY (envelope encryption)
    data_key_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    plan_code: Mapped[str] = mapped_column(String(32), ForeignKey("plans.code"), nullable=False)
    # trialing/active/past_due/canceled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # Custom plan support: effective limits = plan.limits ⊕ plan_overrides
    plan_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64))
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class UsageCounter(Base):
    """Monthly usage. Hot increments live in Redis (usage:{ws}:{YYYY-MM}); the
    beat loop flushes every 30s with an additive upsert."""

    __tablename__ = "usage_counters"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    metric: Mapped[str] = mapped_column(String(48), primary_key=True)
    period_month: Mapped[str] = mapped_column(String(7), primary_key=True)  # 'YYYY-MM'
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = updated_at_col()


class AIPointsLedger(Base):
    """Append-only points flow. Balance is a Redis counter (Lua atomic decr,
    hard-stop at 0); rows are written in the same transaction as the outbox
    event. Monthly grants carry expires_at; purchases don't."""

    __tablename__ = "ai_points_ledger"
    __table_args__ = (Index("ix_ai_points_ledger_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # monthly_grant/purchase/ai_reply/intent/translation/assist/expire/adjust
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class MacActivity(Base):
    """Monthly-active-contact exact dedup: INSERT ON CONFLICT DO NOTHING; a
    successful insert is the only thing that increments the MAC usage counter.
    Never COUNT(DISTINCT)."""

    __tablename__ = "mac_activity"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    period_month: Mapped[str] = mapped_column(String(7), primary_key=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    first_seen_at: Mapped[datetime] = created_at_col()


class LLMProfileRow(Base):
    """LLM endpoint profile (plan B.0). workspace_id NULL = global default;
    a workspace row overrides it. api_key_enc is envelope-encrypted."""

    __tablename__ = "llm_profiles"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # anthropic/openai_compat
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="anthropic")
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    api_key_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    # {fast,smart,embed}
    model_map: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    timeout_s: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
