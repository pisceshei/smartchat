"""Broadcast / marketing data model (plan 附錄 B.3).

Segments (自訂受眾 = AND/OR predicate tree compiled to one safe SQL statement),
broadcasts (one_time | recurring+rrule) → broadcast_runs (one per recurring
fire) → broadcast_recipients (the send state machine, monthly-partitioned),
channel message templates (WhatsApp / Email / Messenger / SMS) with Meta
approval-sync fields + SMS signatures, split links (分流連結) with their
monthly-partitioned click stream, and third-party EDM campaigns.

PARTITIONING NOTE: ``broadcast_recipients`` and ``split_link_clicks`` are the
two high-volume append tables here. Like ``messages`` / ``events`` (see
models/messaging.py) they are created via raw SQL in the migration as
``PARTITION BY RANGE`` tables with a DEFAULT partition; their ORM PK therefore
includes the partition key. Monthly children are managed by pg_partman / the
beat month-roller in production. ``PARTITIONED_TABLES`` below lists them so a
future alembic env.py can union it into its autogenerate skip-set.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid7, uuid_pk, workspace_fk

# P3 tables created via raw SQL in migration 0003 (range-partitioned).
PARTITIONED_TABLES = {"broadcast_recipients", "split_link_clicks"}


class Segment(Base):
    """自訂受眾. ``mode`` = dynamic (predicate re-evaluated at send time) or
    static (``snapshot_ids`` frozen at creation). ``definition`` is the AND/OR
    predicate tree (same grammar as contacts/query) compiled by the segments
    module into a single parameterised, operator-whitelisted SQL statement."""

    __tablename__ = "segments"
    __table_args__ = (Index("ix_segments_ws", "workspace_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="dynamic")  # dynamic/static
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # static snapshot: frozen list of contact ids (nullable → dynamic segment)
    snapshot_ids: Mapped[list[Any] | None] = mapped_column(JSONB)
    count_cache: Mapped[int | None] = mapped_column(Integer)  # last estimate
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Broadcast(Base):
    """群發計劃. ``type`` one_time | recurring. ``schedule`` jsonb =
    {send_at} for one_time, {rrule, until} for recurring (a run is derived per
    fire by the timer service). ``send_rules`` jsonb =
    {allowed_hours, allowed_weekdays, tz, spillover, interval_seconds}. Counts
    are denormalised roll-ups across runs for the list view; success_rate is
    derived (delivered ÷ sent). ``deleted_at`` → recycle bin (30-day purge)."""

    __tablename__ = "broadcasts"
    __table_args__ = (
        Index("ix_broadcasts_ws_type_status", "workspace_id", "type", "status"),
        Index("ix_broadcasts_ws_deleted", "workspace_id", "deleted_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(12), nullable=False, default="one_time")  # one_time/recurring
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False)
    channel_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("channel_accounts.id", ondelete="SET NULL")
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("segments.id", ondelete="SET NULL")
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("msg_templates.id", ondelete="SET NULL")
    )
    variable_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    send_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # draft/scheduled/running/paused/completed/cancelled
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")
    planned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    read_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class BroadcastRun(Base):
    """One send execution. one_time broadcasts have exactly one run; recurring
    derive a run per rrule fire (timer service). Counters are the authoritative
    per-run tallies the recipient state machine writes back into."""

    __tablename__ = "broadcast_runs"
    __table_args__ = (
        Index("ix_broadcast_runs_ws_broadcast", "workspace_id", "broadcast_id"),
        Index("ix_broadcast_runs_broadcast_scheduled", "broadcast_id", "scheduled_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    broadcast_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("broadcasts.id", ondelete="CASCADE"), nullable=False
    )
    # pending/running/paused/completed/failed/cancelled
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    planned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()


class BroadcastRecipient(Base):
    """收件人 state machine (plan B.3): planned→queued→sent→delivered→read,
    terminal failed(reason) / skipped(dedupe|unsubscribed|blacklist|freq_cap|
    quota|invalid_identity|out_of_window). Idempotency for double-send is the
    (run_id, contact) pair guarded by the fan-out; success_rate = delivered÷sent.

    Range-partitioned by ``created_at`` (see module docstring); created via raw
    SQL in migration 0003 so the ORM PK includes the partition key."""

    __tablename__ = "broadcast_recipients"
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at", name="pk_broadcast_recipients"),
        Index("ix_broadcast_recipients_run_state", "run_id", "state"),
        Index("ix_broadcast_recipients_ws_contact", "workspace_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    broadcast_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    channel_identity_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # planned/queued/sent/delivered/read/failed/skipped
    state: Mapped[str] = mapped_column(String(12), nullable=False, default="planned")
    skip_reason: Mapped[str | None] = mapped_column(String(24))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class MsgTemplate(Base):
    """Channel message template. ``body`` is the channel-specific jsonb payload
    (WhatsApp header/body/footer/buttons + category/language; Email
    subject+mjml_source+variables; Messenger payload+message_tag; SMS
    text+signature_id). ``approval_status`` + ``meta_template_id`` +
    ``rejected_reason`` carry the Meta WhatsApp approval sync state."""

    __tablename__ = "msg_templates"
    __table_args__ = (
        Index("ix_msg_templates_ws_channel", "workspace_id", "channel"),
        Index("ix_msg_templates_meta", "workspace_id", "meta_template_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    channel: Mapped[str] = mapped_column(String(12), nullable=False)  # whatsapp/email/messenger/sms
    folder: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    language: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str | None] = mapped_column(String(24))  # marketing/utility/authentication (WA)
    waba_account_id: Mapped[str | None] = mapped_column(String(64))
    # WA Meta review: none/draft/pending/approved/rejected/paused/disabled
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    meta_template_id: Mapped[str | None] = mapped_column(String(128))
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class SmsSignature(Base):
    """SMS 簽名 (prepended/required by many carriers; referenced by SMS
    templates via body.signature_id)."""

    __tablename__ = "sms_signatures"
    __table_args__ = (Index("ix_sms_signatures_ws", "workspace_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class SplitLink(Base):
    """分流連結. ``strategy`` random | time_period | sequential; ``targets``
    jsonb = [{channel_account_id, weight?, enabled?, time_windows?, daily_cap?}].
    ``slug`` is a base62 token served by the edge app (302→wa.me). ``rr_cursor``
    is the lock-free INCR-mod-n cursor for sequential/round-robin. ``prefill_text``
    (0/300) may carry a {{code}} tracking token for the attribution loop."""

    __tablename__ = "split_links"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_split_links_slug"),
        Index("ix_split_links_ws_status", "workspace_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    slug: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False)
    strategy: Mapped[str] = mapped_column(String(12), nullable=False, default="random")
    targets: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    prefill_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="active")  # active/paused
    rr_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    click_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    qr_key: Mapped[str | None] = mapped_column(Text)  # MinIO storage key for cached QR
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class SplitLinkClick(Base):
    """Per-click record (plan B.3): IP hash / UA / device / GeoIP country /
    referrer + the resolved ``target_idx`` and ``tracking_code`` for the
    attribution loop. Range-partitioned by ``ts`` (created via raw SQL in
    migration 0003 → ORM PK includes the partition key)."""

    __tablename__ = "split_link_clicks"
    __table_args__ = (
        PrimaryKeyConstraint("id", "ts", name="pk_split_link_clicks"),
        Index("ix_split_link_clicks_link_ts", "link_id", "ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), default=uuid7)
    link_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ts: Mapped[datetime] = created_at_col()
    target_idx: Mapped[int | None] = mapped_column(Integer)
    tracking_code: Mapped[str | None] = mapped_column(String(32))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    ua: Mapped[str | None] = mapped_column(Text)
    device: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(2))
    referrer: Mapped[str | None] = mapped_column(Text)


class EdmCampaign(Base):
    """第三方代發 EDM (plan B.3): same UI/segment/template surface as a
    broadcast but delivered through an email-sender adapter (smtp/ses/sendgrid/
    edm_provider). Stats are polled back into the shared broadcast_runs table
    via ``run_id``."""

    __tablename__ = "edm_campaigns"
    __table_args__ = (Index("ix_edm_campaigns_ws_status", "workspace_id", "status"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="smtp")
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("segments.id", ondelete="SET NULL")
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("msg_templates.id", ondelete="SET NULL")
    )
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # draft/scheduled/running/paused/completed/cancelled
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="draft")
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("broadcast_runs.id", ondelete="SET NULL")
    )
    planned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
