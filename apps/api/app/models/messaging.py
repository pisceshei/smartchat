"""Messages (hottest table) + dedup + files + translation caches (附錄 A.6).

PARTITIONING NOTE: `messages` is declared normally here, but the initial
migration creates it as `PARTITION BY RANGE (created_at)` with a DEFAULT
partition via raw SQL (its PK there is (id, created_at) — partitioned PKs must
include the partition key). Monthly child partitions are managed by pg_partman
(or the beat loop's month-roller) in production. Same applies to `events` in
misc.py. `message_dedup` is intentionally NOT partitioned so the
(channel_account_id, external_message_id) uniqueness stays global; rows are
purged after 90 days.
"""
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, uuid_pk, workspace_fk

# tables created via raw SQL in the migration (range-partitioned)
PARTITIONED_TABLES = {"messages", "events"}


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_ws_conv_created", "workspace_id", "conversation_id", "created_at"),
        Index("ix_messages_conv_created", "conversation_id", "created_at"),
        Index("ix_messages_ws_client_msg", "workspace_id", "client_msg_id"),
        # trigram GIN on text_plain added in migration (needs pg_trgm)
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    channel_identity_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    direction: Mapped[str] = mapped_column(String(3), nullable=False)  # in/out
    # contact/member/ai_agent/automation/system
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)
    sender_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    msg_type: Mapped[str] = mapped_column(String(24), nullable=False, default="text")
    # py_contracts.content.MessageContent — canonical block union, stored as-is
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    text_plain: Mapped[str | None] = mapped_column(Text)  # trigram search (134 langs → no tsvector)
    is_note: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # internal note
    sent_via: Mapped[str | None] = mapped_column(String(16))  # automation/broadcast/api marker
    source_flow_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    external_message_id: Mapped[str | None] = mapped_column(String(255))
    client_msg_id: Mapped[str | None] = mapped_column(String(64))  # REST idempotency key
    # pending/sent/delivered/read/failed
    delivery_status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    delivery_error: Mapped[str | None] = mapped_column(Text)
    external_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class MessageDedup(Base):
    """Inbound idempotency. Separate un-partitioned table because a
    partitioned unique constraint would have to include created_at."""

    __tablename__ = "message_dedup"

    channel_account_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    external_message_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    message_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class File(Base):
    """MinIO-backed attachment; sha256 dedup per workspace. Channel media is
    copied in at ingest (WA media URLs expire in ~5 minutes)."""

    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("workspace_id", "sha256", name="uq_files_ws_sha256"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    original_name: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_by_type: Mapped[str | None] = mapped_column(String(16))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class MessageTranslation(Base):
    """Per-message translation (lazy: only for the open panel, only the
    agent↔customer language pair)."""

    __tablename__ = "message_translations"
    __table_args__ = (Index("ix_message_translations_ws", "workspace_id"),)

    message_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    target_lang: Mapped[str] = mapped_column(String(16), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    engine: Mapped[str] = mapped_column(String(24), nullable=False, default="llm")
    detected_source_lang: Mapped[str | None] = mapped_column(String(16))
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class TranslationCache(Base):
    """Content-hash cache shared across messages (quick replies repeat a lot).
    Key = sha256(src_text + src_lang + dst_lang + engine). Metered only on
    cache miss."""

    __tablename__ = "translation_cache"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    src_lang: Mapped[str | None] = mapped_column(String(16))
    dst_lang: Mapped[str] = mapped_column(String(16), nullable=False)
    engine: Mapped[str] = mapped_column(String(24), nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
