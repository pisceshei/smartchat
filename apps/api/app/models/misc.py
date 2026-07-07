"""Tags / audit / views / quick replies / materials / developer surface /
event outbox / timers (plan 附錄 A + B.0).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_fk, uuid_pk, workspace_fk


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("workspace_id", "kind", "name", name="uq_tags_ws_kind_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # contact/conversation
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = created_at_col()


class ContactTag(Base):
    __tablename__ = "contact_tags"

    contact_id: Mapped[uuid.UUID] = uuid_fk("contacts.id", primary_key=True)
    tag_id: Mapped[uuid.UUID] = uuid_fk("tags.id", primary_key=True)
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    created_at: Mapped[datetime] = created_at_col()


class ConversationTag(Base):
    __tablename__ = "conversation_tags"

    conversation_id: Mapped[uuid.UUID] = uuid_fk("conversations.id", primary_key=True)
    tag_id: Mapped[uuid.UUID] = uuid_fk("tags.id", primary_key=True)
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    created_at: Mapped[datetime] = created_at_col()


class AuditLog(Base):
    """操作紀錄 (member actions) + login/online monitor entries."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = created_at_col()


class SavedView(Base):
    """自訂檢視 for inbox / contacts (personal or public)."""

    __tablename__ = "saved_views"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    module: Mapped[str] = mapped_column(String(16), nullable=False, default="inbox")  # inbox/contacts
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility: Mapped[str] = mapped_column(String(8), nullable=False, default="private")  # private/public
    owner_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="CASCADE")
    )
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class QuickReplyFolder(Base):
    __tablename__ = "quick_reply_folders"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(8), nullable=False, default="public")  # personal/public
    owner_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="CASCADE")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()


class QuickReply(Base):
    """話術庫 entry. content = MessageContent blocks (rich, not just text)."""

    __tablename__ = "quick_replies"
    __table_args__ = (Index("ix_quick_replies_ws_scope", "workspace_id", "scope"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("quick_reply_folders.id", ondelete="SET NULL")
    )
    scope: Mapped[str] = mapped_column(String(8), nullable=False, default="public")  # personal/public
    owner_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    shortcut: Mapped[str | None] = mapped_column(String(32))  # "/hello"
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    text_plain: Mapped[str | None] = mapped_column(Text)
    starred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Material(Base):
    """素材庫 (image/video/file/richtext assets reusable in replies & flows)."""

    __tablename__ = "materials"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="image")
    title: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WebhookSubscription(Base):
    """Developer webhook push: url + signing token + subscribed event list."""

    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    url: Mapped[str] = mapped_column(Text, nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False)  # HMAC signing secret
    events: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    only_customer_messages: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WebhookDelivery(Base):
    """At-least-once delivery log with retry backoff schedule."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_ws_sub", "workspace_id", "subscription_id", "created_at"),
        Index("ix_webhook_deliveries_retry", "status", "next_retry_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    subscription_id: Mapped[uuid.UUID] = uuid_fk("webhook_subscriptions.id")
    event_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending/success/failed/dead
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class ApiToken(Base):
    """OpenAPI project token (≥Max plan). Only the sha256 hash is stored;
    prefix is kept for display ("sct_ab12…")."""

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False, default="")
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()


class CustomFieldDefinition(Base):
    """Schema for contacts.custom jsonb — no EAV."""

    __tablename__ = "custom_field_definitions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "entity", "key", name="uq_custom_fields_ws_entity_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    entity: Mapped[str] = mapped_column(String(16), nullable=False, default="contact")
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    # text/number/date/select/multiselect/bool
    field_type: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    options: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()


class EventRow(Base):
    """Transactional outbox AND reports raw store (plan B.0) — one write path
    feeds the Redis Streams bus and analytics. 13-month retention.

    PARTITIONING NOTE: created as PARTITION BY RANGE (occurred_at) with a
    DEFAULT partition in the migration (PK there = (id, occurred_at)); monthly
    children via pg_partman / beat month-roller. `published` drives the relay
    (SELECT … WHERE NOT published ORDER BY id FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_ws_occurred", "workspace_id", "occurred_at"),
        Index("ix_events_type_occurred", "type", "occurred_at"),
        Index("ix_events_unpublished", "published", postgresql_where=sa_text("NOT published")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = created_at_col()
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    channel_type: Mapped[str | None] = mapped_column(String(24))
    channel_account_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Timer(Base):
    """Generic timer service source of truth (plan B.0): PG rows + Redis ZSET
    hot window (24h) + 1s poller + boot reseed. Used by flow delays, timeout
    triggers, recurring broadcasts, auto-close."""

    __tablename__ = "timers"
    __table_args__ = (
        Index("ix_timers_status_fire", "status", "fire_at"),
        Index("ix_timers_ws_kind_ref", "workspace_id", "kind", "ref_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)  # doubles as bus event type when fired
    ref_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # pending/fired/cancelled
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
