"""P2 — flow engine + AI subsystem.

Adds the automation flow tables (flows / versions / denormalised triggers /
keyword dicts / runtime sessions + steps / freq-cap log / daily+user stats /
global template gallery) and the AI tables (agents + usage / intents / points
price list + balance cache / translation usage / pgvector knowledge base).

- CREATE EXTENSION vector (pgvector) for kb_chunks.embedding vector(1024) +
  an HNSW cosine index.
- The flows ⇄ flow_versions circular FK (flows.published_version_id) is added
  after both tables exist via a named use_alter constraint.
- Seeds the ai_point_prices config rows and two global flow_templates.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------------ flows
    op.create_table(
        "flow_categories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=96), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_flow_categories_ws_name"),
    )

    op.create_table(
        "flows",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("channel_type", sa.String(length=24), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("published_version_id", sa.UUID(), nullable=True),
        sa.Column("draft_graph", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by_member_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["category_id"], ["flow_categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_member_id"], ["workspace_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_flows_ws_channel_enabled", "flows", ["workspace_id", "channel_type", "enabled"])
    op.create_index("ix_flows_ws_category", "flows", ["workspace_id", "category_id"])

    op.create_table(
        "flow_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("graph", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("published_by_member_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["flow_id"], ["flows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by_member_id"], ["workspace_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flow_id", "version_no", name="uq_flow_versions_flow_no"),
    )
    op.create_index("ix_flow_versions_ws_flow", "flow_versions", ["workspace_id", "flow_id"])
    # circular FK now that flow_versions exists
    op.create_foreign_key(
        "fk_flows_published_version", "flows", "flow_versions",
        ["published_version_id"], ["id"], ondelete="SET NULL",
    )

    op.create_table(
        "flow_triggers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("channel_type", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("freq_cap", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["flow_id"], ["flows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["flow_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_flow_triggers_route", "flow_triggers",
        ["workspace_id", "channel_type", "trigger_type", "enabled"],
    )
    op.create_index("ix_flow_triggers_flow", "flow_triggers", ["workspace_id", "flow_id"])

    op.create_table(
        "keyword_dicts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=96), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_keyword_dicts_ws_name"),
    )

    op.create_table(
        "keyword_dict_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("dict_id", sa.UUID(), nullable=False),
        sa.Column("keyword", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["dict_id"], ["keyword_dicts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_keyword_dict_items_dict", "keyword_dict_items", ["workspace_id", "dict_id"])

    op.create_table(
        "flow_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("flow_id", sa.UUID(), nullable=False),
        sa.Column("flow_version_id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_node_id", sa.String(length=64), nullable=True),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("waiting", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("engaged", sa.Boolean(), nullable=False),
        sa.Column("wakeup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["flow_id"], ["flows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["flow_version_id"], ["flow_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_flow_sessions_ws_conv", "flow_sessions", ["workspace_id", "conversation_id"])
    op.create_index(
        "ix_flow_sessions_ws_flow_created", "flow_sessions", ["workspace_id", "flow_id", "created_at"]
    )
    op.create_index("ix_flow_sessions_wakeup", "flow_sessions", ["status", "wakeup_at"])

    op.create_table(
        "flow_session_steps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["flow_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_flow_session_steps_session_seq", "flow_session_steps", ["session_id", "seq"])

    op.create_table(
        "flow_trigger_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=False),
        sa.Column("trigger_id", sa.UUID(), nullable=True),
        sa.Column("contact_id", sa.UUID(), nullable=True),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_flow_trigger_log_cap", "flow_trigger_log",
        ["workspace_id", "flow_id", "contact_id", "created_at"],
    )
    op.create_index(
        "ix_flow_trigger_log_trigger", "flow_trigger_log", ["workspace_id", "trigger_id", "created_at"]
    )

    op.create_table(
        "flow_stats_daily",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("triggered_sessions", sa.Integer(), nullable=False),
        sa.Column("triggered_users", sa.Integer(), nullable=False),
        sa.Column("engaged_users", sa.Integer(), nullable=False),
        sa.Column("completed_sessions", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "flow_id", "day"),
    )

    op.create_table(
        "flow_stats_users",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("flow_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("contact_id", sa.UUID(), nullable=False),
        sa.Column("engaged", sa.Boolean(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "flow_id", "day", "contact_id"),
    )

    op.create_table(
        "flow_templates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("channel_type", sa.String(length=24), nullable=False),
        sa.Column("category", sa.String(length=48), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("graph", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preview", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_flow_templates_slug"),
    )
    op.create_index(
        "ix_flow_templates_channel_category", "flow_templates", ["channel_type", "category"]
    )

    # ------------------------------------------------------------------- ai
    op.create_table(
        "ai_agents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("member_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("persona", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_tier", sa.String(length=8), nullable=False),
        sa.Column("kb_collection_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("skills", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("monthly_msg_quota", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=12), nullable=False),
        sa.Column("external", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("escalation_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["workspace_members.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", name="uq_ai_agents_member"),
    )
    op.create_index("ix_ai_agents_ws", "ai_agents", ["workspace_id"])

    op.create_table(
        "ai_agent_usage",
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("replies", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["ai_agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id", "month"),
    )

    op.create_table(
        "intents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=96), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("examples", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_intents_ws_name"),
    )

    op.create_table(
        "ai_point_prices",
        sa.Column("feature_key", sa.String(length=48), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("feature_key"),
    )

    op.create_table(
        "ai_point_balances",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("grant_remaining", sa.BigInteger(), nullable=False),
        sa.Column("topup_remaining", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )

    op.create_table(
        "translation_usage",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("engine", sa.String(length=24), nullable=False),
        sa.Column("chars", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "month", "engine"),
    )

    # ------------------------------------------------------------ knowledge base
    op.create_table(
        "kb_collections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kb_collections_ws", "kb_collections", ["workspace_id"])

    op.create_table(
        "kb_documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("collection_id", sa.UUID(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["collection_id"], ["kb_collections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kb_documents_ws_collection", "kb_documents", ["workspace_id", "collection_id"])

    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["kb_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_kb_chunks_ws_document", "kb_chunks", ["workspace_id", "document_id"])
    op.execute(
        "CREATE INDEX ix_kb_chunks_embedding_hnsw ON kb_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    _seed(op)


def _seed(op) -> None:  # noqa: ANN001
    prices = sa.table(
        "ai_point_prices",
        sa.column("feature_key", sa.String),
        sa.column("points", sa.Integer),
        sa.column("description", sa.Text),
    )
    op.bulk_insert(
        prices,
        [
            {"feature_key": "ai_reply", "points": 10, "description": "AI member reply"},
            {"feature_key": "intent", "points": 1, "description": "Intent classification (per message)"},
            {"feature_key": "translate_llm_per500", "points": 1,
             "description": "LLM translation per 500 characters"},
            {"feature_key": "composer", "points": 2, "description": "Composer AI assist"},
            {"feature_key": "embed_per10k", "points": 1, "description": "Embedding per 10k tokens"},
            {"feature_key": "summary", "points": 5, "description": "Conversation summary"},
            {"feature_key": "report_summary", "points": 20, "description": "Report AI summary"},
        ],
    )

    templates = sa.table(
        "flow_templates",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("channel_type", sa.String),
        sa.column("category", sa.String),
        sa.column("slug", sa.String),
        sa.column("name", postgresql.JSONB),
        sa.column("description", postgresql.JSONB),
        sa.column("graph", postgresql.JSONB),
        sa.column("preview", postgresql.JSONB),
        sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(templates, [_welcome_template(), _product_inquiry_template()])


def _welcome_template() -> dict:
    graph = {
        "schema_version": 1,
        "nodes": [
            {"id": "trigger", "type": "trigger", "position": {"x": 0, "y": 0},
             "data": {"triggers": [{"type": "new_visitor", "config": {}, "freq_cap": {}}]}},
            {"id": "welcome", "type": "send_message", "position": {"x": 0, "y": 160},
             "data": {"blocks": [{"kind": "text",
                                  "text": "Hi {{ contact.display_name }}, welcome! How can we help?"}]}},
        ],
        "edges": [{"id": "e1", "source": "trigger", "source_port": "out", "target": "welcome"}],
    }
    return {
        "id": uuid.uuid4(),
        "channel_type": "widget",
        "category": "greeting",
        "slug": "welcome-new-visitor",
        "name": {"en": "Welcome new visitor", "zh-hant": "歡迎新訪客", "zh-hans": "欢迎新访客"},
        "description": {"en": "Greet first-time visitors automatically.",
                        "zh-hant": "自動歡迎首次到訪的訪客。"},
        "graph": graph,
        "preview": {"trigger": "new_visitor", "nodes": 2},
        "sort_order": 10,
        "is_active": True,
    }


def _product_inquiry_template() -> dict:
    graph = {
        "schema_version": 1,
        "nodes": [
            {"id": "trigger", "type": "trigger", "position": {"x": 0, "y": 0},
             "data": {"triggers": [{"type": "visitor_message",
                                    "config": {"match": "keyword", "mode": "fuzzy",
                                               "keywords": ["price", "buy", "order", "價格", "購買"]},
                                    "freq_cap": {"scope": "contact", "count": 1, "window_s": 3600}}]}},
            {"id": "ask", "type": "quick_buttons", "position": {"x": 0, "y": 160},
             "data": {"text": "What would you like to know?",
                      "buttons": [{"id": "pricing", "text": "Pricing"},
                                  {"id": "human", "text": "Talk to a human"}]}},
            {"id": "reply", "type": "send_message", "position": {"x": -200, "y": 320},
             "data": {"blocks": [{"kind": "text", "text": "Here is our pricing information."}]}},
            {"id": "handoff", "type": "transfer_unassigned", "position": {"x": 200, "y": 320},
             "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "trigger", "source_port": "out", "target": "ask"},
            {"id": "e2", "source": "ask", "source_port": "button:pricing", "target": "reply"},
            {"id": "e3", "source": "ask", "source_port": "button:human", "target": "handoff"},
            {"id": "e4", "source": "ask", "source_port": "typed_reply", "target": "handoff"},
        ],
    }
    return {
        "id": uuid.uuid4(),
        "channel_type": "widget",
        "category": "sales",
        "slug": "product-inquiry",
        "name": {"en": "Product inquiry", "zh-hant": "產品諮詢", "zh-hans": "产品咨询"},
        "description": {"en": "Answer pricing questions and offer a human handoff.",
                        "zh-hant": "回答價格問題並提供轉真人服務。"},
        "graph": graph,
        "preview": {"trigger": "visitor_message", "nodes": 4},
        "sort_order": 20,
        "is_active": True,
    }


def downgrade() -> None:
    op.drop_constraint("fk_flows_published_version", "flows", type_="foreignkey")

    op.execute("DROP INDEX IF EXISTS ix_kb_chunks_embedding_hnsw")
    op.drop_index("ix_kb_chunks_ws_document", table_name="kb_chunks")
    op.drop_table("kb_chunks")
    op.drop_index("ix_kb_documents_ws_collection", table_name="kb_documents")
    op.drop_table("kb_documents")
    op.drop_index("ix_kb_collections_ws", table_name="kb_collections")
    op.drop_table("kb_collections")

    op.drop_table("translation_usage")
    op.drop_table("ai_point_balances")
    op.drop_table("ai_point_prices")
    op.drop_table("intents")
    op.drop_table("ai_agent_usage")
    op.drop_index("ix_ai_agents_ws", table_name="ai_agents")
    op.drop_table("ai_agents")

    op.drop_index("ix_flow_templates_channel_category", table_name="flow_templates")
    op.drop_table("flow_templates")
    op.drop_table("flow_stats_users")
    op.drop_table("flow_stats_daily")
    op.drop_index("ix_flow_trigger_log_trigger", table_name="flow_trigger_log")
    op.drop_index("ix_flow_trigger_log_cap", table_name="flow_trigger_log")
    op.drop_table("flow_trigger_log")
    op.drop_index("ix_flow_session_steps_session_seq", table_name="flow_session_steps")
    op.drop_table("flow_session_steps")
    op.drop_index("ix_flow_sessions_wakeup", table_name="flow_sessions")
    op.drop_index("ix_flow_sessions_ws_flow_created", table_name="flow_sessions")
    op.drop_index("ix_flow_sessions_ws_conv", table_name="flow_sessions")
    op.drop_table("flow_sessions")
    op.drop_index("ix_keyword_dict_items_dict", table_name="keyword_dict_items")
    op.drop_table("keyword_dict_items")
    op.drop_table("keyword_dicts")
    op.drop_index("ix_flow_triggers_flow", table_name="flow_triggers")
    op.drop_index("ix_flow_triggers_route", table_name="flow_triggers")
    op.drop_table("flow_triggers")
    op.drop_index("ix_flow_versions_ws_flow", table_name="flow_versions")
    op.drop_table("flow_versions")
    op.drop_index("ix_flows_ws_category", table_name="flows")
    op.drop_index("ix_flows_ws_channel_enabled", table_name="flows")
    op.drop_table("flows")
    op.drop_table("flow_categories")
