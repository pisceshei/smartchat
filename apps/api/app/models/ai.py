"""AI subsystem data model (plan 附錄 B.2).

AI members (`ai_agents`, 1:1 with a workspace_members row of member_type=
ai_agent so they share the single assignee surface), intents, the AI points
price list + per-workspace balance cache, translation metering, and the pgvector
knowledge base (collections → documents → chunks with a 1024-dim embedding and
an HNSW cosine index).

NOTE: `message_translations` and `translation_cache` already exist in
models/messaging.py (P1) — they are NOT redefined here. Only the additive
`translation_usage` monthly meter is new.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
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

EMBED_DIM = 1024


class AIAgent(Base):
    """AI member config, 1:1 with a workspace_members(member_type=ai_agent) row
    so conversations.assignee_member_id can point at an AI exactly like a human.
    `persona` = role prompt/tone/languages/refusal topics/greeting; `skills` =
    enabled capabilities (kb_answer/product_card/lead_capture/handoff);
    `escalation_rules` = keywords/intents/N-miss/out-of-hours → handoff;
    `mode` = builtin (our LLM) | external (webhook + HMAC)."""

    __tablename__ = "ai_agents"
    __table_args__ = (
        UniqueConstraint("member_id", name="uq_ai_agents_member"),
        Index("ix_ai_agents_ws", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    member_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("workspace_members.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    persona: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    model_tier: Mapped[str] = mapped_column(String(8), nullable=False, default="fast")  # fast/smart
    kb_collection_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    skills: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    monthly_msg_quota: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 = unlimited
    mode: Mapped[str] = mapped_column(String(12), nullable=False, default="builtin")  # builtin/external
    # webhook/hmac/timeout
    external: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    escalation_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AIAgentUsage(Base):
    """Monthly reply counter per AI agent (enforces monthly_msg_quota)."""

    __tablename__ = "ai_agent_usage"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ai_agents.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # 'YYYY-MM'
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = updated_at_col()


class Intent(Base):
    """Tenant intent for the visitor_intent trigger. `examples` = 3–10 phrases
    (later usable for pgvector pre-filtering)."""

    __tablename__ = "intents"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_intents_ws_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    examples: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AIPointPrice(Base):
    """Config price list (plan B.2 — price is a config table, not code). Seeded:
    ai_reply=10, intent=1, translate_llm_per500=1, composer=2, embed_per10k=1,
    summary=5, report_summary=20."""

    __tablename__ = "ai_point_prices"

    feature_key: Mapped[str] = mapped_column(String(48), primary_key=True)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = updated_at_col()


class AIPointBalance(Base):
    """Per-workspace balance cache row (plan B.2): spend the expiring monthly
    grant first, then the non-expiring top-up. `period` is the month the current
    grant belongs to; the authoritative flow stays in ai_points_ledger."""

    __tablename__ = "ai_point_balances"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, default="")  # 'YYYY-MM'
    grant_remaining: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    topup_remaining: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = updated_at_col()


class TranslationUsage(Base):
    """Per-engine monthly character meter (Google/DeepL plan quotas; LLM engine
    is metered in points instead but still counted here for reporting)."""

    __tablename__ = "translation_usage"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # 'YYYY-MM'
    engine: Mapped[str] = mapped_column(String(24), primary_key=True)
    chars: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = updated_at_col()


class KBCollection(Base):
    """Knowledge base collection (referenced by ai_agents.kb_collection_ids)."""

    __tablename__ = "kb_collections"
    __table_args__ = (Index("ix_kb_collections_ws", "workspace_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class KBDocument(Base):
    """A source ingested into a collection. `source_type` = upload/faq/product/
    url; `status` = pending/processing/ready/error tracks the chunk+embed job."""

    __tablename__ = "kb_documents"
    __table_args__ = (Index("ix_kb_documents_ws_collection", "workspace_id", "collection_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("kb_collections.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_ref: Mapped[str | None] = mapped_column(Text)  # url / file_id / product handle
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class KBChunk(Base):
    """A retrievable chunk with its 1024-dim embedding (HNSW cosine index built
    in the migration). `meta.handle` grounds [CARD:] product references. FAQ =
    one Q&A per chunk; product = one structured chunk per SKU."""

    __tablename__ = "kb_chunks"
    __table_args__ = (Index("ix_kb_chunks_ws_document", "workspace_id", "document_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = created_at_col()
