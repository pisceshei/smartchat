"""Conversations + state machine (plan 附錄 A.5).

One persistent thread per channel_identity (UNIQUE); each service cycle is a
conversation_sessions row for reporting.
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_fk, uuid_pk, workspace_fk


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_ws_status_last", "workspace_id", "status", "last_message_at"),
        Index("ix_conversations_ws_assignee", "workspace_id", "assignee_member_id"),
        Index("ix_conversations_ws_needs_reply", "workspace_id", "needs_reply"),
        Index("ix_conversations_ws_contact", "workspace_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    channel_identity_id: Mapped[uuid.UUID] = uuid_fk("channel_identities.id", unique=True)
    # denormalized routing/filter columns
    channel_account_id: Mapped[uuid.UUID] = uuid_fk("channel_accounts.id")
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False)
    contact_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")

    status: Mapped[str] = mapped_column(String(8), nullable=False, default="open")  # open/closed
    # bot/ai_agent/member/unassigned
    handler: Mapped[str] = mapped_column(String(16), nullable=False, default="unassigned")
    assignee_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    bot_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 託管
    # off/managed/paused_human
    ai_state: Mapped[str] = mapped_column(String(16), nullable=False, default="off")
    needs_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 待回覆
    agent_unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # WhatsApp/Messenger 24h customer window; send API hard-validates this
    customer_window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snippet: Mapped[str | None] = mapped_column(Text)  # list preview of last message
    # conversation-level translation config {enabled, agent_lang, customer_lang}
    translation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_contact_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_agent_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ConversationSession(Base):
    """One service cycle (open→close) — feeds reports (first response, counts,
    CSAT). Reopening a closed conversation starts a new session."""

    __tablename__ = "conversation_sessions"
    __table_args__ = (
        Index("ix_conversation_sessions_ws_conv", "workspace_id", "conversation_id"),
        Index("ix_conversation_sessions_ws_started", "workspace_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    conversation_id: Mapped[uuid.UUID] = uuid_fk("conversations.id")
    started_at: Mapped[datetime] = created_at_col()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # contact/member/flow/api
    opened_by: Mapped[str] = mapped_column(String(16), nullable=False, default="contact")
    closed_by_type: Mapped[str | None] = mapped_column(String(16))  # member/ai_agent/flow/system/auto
    closed_by_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    handler_at_close: Mapped[str | None] = mapped_column(String(16))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contact_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agent_message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    csat_score: Mapped[int | None] = mapped_column(Integer)


class ConversationAssignment(Base):
    """Audit of every routing/assignment/handoff transition."""

    __tablename__ = "conversation_assignments"
    __table_args__ = (Index("ix_conversation_assignments_ws_conv", "workspace_id", "conversation_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    conversation_id: Mapped[uuid.UUID] = uuid_fk("conversations.id")
    from_handler: Mapped[str | None] = mapped_column(String(16))
    from_member_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    to_handler: Mapped[str] = mapped_column(String(16), nullable=False)
    to_member_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # auto/manual/transfer/handoff/timeout/api
    reason: Mapped[str] = mapped_column(String(24), nullable=False, default="auto")
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = created_at_col()


class ConversationRead(Base):
    """Per-member read cursor (unread counts); hot path mirrored in Redis."""

    __tablename__ = "conversation_reads"
    __table_args__ = (Index("ix_conversation_reads_ws_member", "workspace_id", "member_id"),)

    conversation_id: Mapped[uuid.UUID] = uuid_fk("conversations.id", primary_key=True)
    member_id: Mapped[uuid.UUID] = uuid_fk("workspace_members.id", primary_key=True)
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    last_read_message_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_read_at: Mapped[datetime] = updated_at_col()
