"""Contacts + ONE ID (plan 附錄 A.4).

Invariant: messages/conversations always hang off channel_identities, never
directly off contacts — contact_id on a conversation is a denormalized
pointer. Merges re-point identities and snapshot everything for exact undo.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_fk, uuid_pk, workspace_fk


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_ws_email", "workspace_id", "email"),
        Index("ix_contacts_ws_phone", "workspace_id", "phone"),
        Index("ix_contacts_ws_last_seen", "workspace_id", "last_seen_at"),
        Index("ix_contacts_custom_gin", "custom", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    remark_name: Mapped[str | None] = mapped_column(String(128))  # 備註名 set by agents
    avatar_url: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(CITEXT())
    phone: Mapped[str | None] = mapped_column(String(32))  # E.164
    language: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(64))
    timezone: Mapped[str | None] = mapped_column(String(48))
    last_ip: Mapped[str | None] = mapped_column(String(45))
    device: Mapped[str | None] = mapped_column(String(64))
    browser: Mapped[str | None] = mapped_column(String(64))
    os: Mapped[str | None] = mapped_column(String(64))
    custom: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # tombstone: set when this contact was merged into another (ONE ID)
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ChannelIdentity(Base):
    """One row per (channel account, external user). The inbound upsert key.
    contact_id is the ONLY mutable edge (merge = re-point it)."""

    __tablename__ = "channel_identities"
    __table_args__ = (
        UniqueConstraint("channel_account_id", "external_user_id", name="uq_channel_identities_acct_ext"),
        Index("ix_channel_identities_ws_contact", "workspace_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    channel_account_id: Mapped[uuid.UUID] = uuid_fk("channel_accounts.id")
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False)  # denorm for filters
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")
    display_name: Mapped[str | None] = mapped_column(String(128))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    # merchant-side user id from widget setLoginInfo (revocable auto-link source)
    logged_in_external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContactMerge(Base):
    """Merge audit + undo snapshot. Undo = exact replay of the snapshot; only
    the newest merge in a chain is undoable."""

    __tablename__ = "contact_merges"
    __table_args__ = (Index("ix_contact_merges_ws_target", "workspace_id", "target_contact_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    target_contact_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")
    source_contact_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")
    moved_identity_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    moved_conversation_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    field_overwrites: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    merged_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    merged_at: Mapped[datetime] = created_at_col()
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by_member_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))


class ContactMergeCandidate(Base):
    """重複聯絡人 suggestions: same phone/email/logged-in id/fuzzy name."""

    __tablename__ = "contact_merge_candidates"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "contact_a_id", "contact_b_id", "match_type",
            name="uq_merge_candidates_pair",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    contact_a_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")
    contact_b_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")
    match_type: Mapped[str] = mapped_column(String(24), nullable=False)  # phone/email/logged_in_id/name_fuzzy
    score: Mapped[float | None] = mapped_column(Float)
    # suggested/linked/dismissed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="suggested")
    created_at: Mapped[datetime] = created_at_col()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContactNote(Base):
    __tablename__ = "contact_notes"
    __table_args__ = (Index("ix_contact_notes_ws_contact", "workspace_id", "contact_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    contact_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")
    author_member_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class ContactOrder(Base):
    """E-commerce orders surfaced in the inbox side panel (Shopify/Fecify…)."""

    __tablename__ = "contact_orders"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "store_platform", "external_order_id", name="uq_contact_orders_ext"
        ),
        Index("ix_contact_orders_ws_contact", "workspace_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    contact_id: Mapped[uuid.UUID] = uuid_fk("contacts.id")
    store_platform: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    external_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str | None] = mapped_column(String(8))
    total: Mapped[str | None] = mapped_column(String(32))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class VisitorEvent(Base):
    """Widget browsing trail: page views / widget opens / lead submits."""

    __tablename__ = "visitor_events"
    __table_args__ = (
        Index("ix_visitor_events_ws_occurred", "workspace_id", "occurred_at"),
        Index("ix_visitor_events_ws_identity", "workspace_id", "channel_identity_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE")
    )
    channel_identity_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("channel_identities.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)  # page_view/widget_open/lead_submit
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    referrer: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = created_at_col()
