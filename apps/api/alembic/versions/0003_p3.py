"""P3 — broadcast/marketing + reports/analytics + Stripe billing.

Adds:
- marketing: segments / broadcasts / broadcast_runs / msg_templates /
  sms_signatures / split_links / edm_campaigns, plus the two monthly
  RANGE-partitioned append tables broadcast_recipients + split_link_clicks
  (raw SQL with a DEFAULT partition, same pattern as messages/events).
- reports: agg_messages_hourly / agg_conversations_hourly / agg_agent_hourly /
  agg_customers_daily / agg_ads_daily / agent_presence_sessions /
  conversation_attribution / report_shares / report_exports /
  report_ai_summaries / rollup_watermark.
- billing: workspace_balance / balance_ledger / billing_orders / stripe_events /
  invoices (extends the P1 tenancy plans/subscriptions/usage/points tables).

Seeds nothing — plans (Free/Pro/Max/Custom) are seeded idempotently by
apps.api.app.seed. Reversible.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")
_NIL_UUID = sa.text("'00000000-0000-0000-0000-000000000000'::uuid")
_EMPTY = sa.text("''")


def upgrade() -> None:
    # ================================================================ marketing
    op.create_table(
        "segments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("count_cache", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_segments_ws", "segments", ["workspace_id"])

    op.create_table(
        "msg_templates",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=12), nullable=False),
        sa.Column("folder", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("category", sa.String(length=24), nullable=True),
        sa.Column("waba_account_id", sa.String(length=64), nullable=True),
        sa.Column("approval_status", sa.String(length=16), nullable=False),
        sa.Column("meta_template_id", sa.String(length=128), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_msg_templates_ws_channel", "msg_templates", ["workspace_id", "channel"])
    op.create_index("ix_msg_templates_meta", "msg_templates", ["workspace_id", "meta_template_id"])

    op.create_table(
        "broadcasts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("type", sa.String(length=12), nullable=False),
        sa.Column("channel_type", sa.String(length=24), nullable=False),
        sa.Column("channel_account_id", sa.UUID(), nullable=True),
        sa.Column("segment_id", sa.UUID(), nullable=True),
        sa.Column("template_id", sa.UUID(), nullable=True),
        sa.Column("variable_mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("send_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("planned_count", sa.Integer(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("delivered_count", sa.Integer(), nullable=False),
        sa.Column("read_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("created_by_member_id", sa.UUID(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["channel_account_id"], ["channel_accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["template_id"], ["msg_templates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["workspace_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_broadcasts_ws_type_status", "broadcasts", ["workspace_id", "type", "status"])
    op.create_index("ix_broadcasts_ws_deleted", "broadcasts", ["workspace_id", "deleted_at"])

    op.create_table(
        "broadcast_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("broadcast_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned", sa.Integer(), nullable=False),
        sa.Column("sent", sa.Integer(), nullable=False),
        sa.Column("delivered", sa.Integer(), nullable=False),
        sa.Column("read", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["broadcast_id"], ["broadcasts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_broadcast_runs_ws_broadcast", "broadcast_runs", ["workspace_id", "broadcast_id"]
    )
    op.create_index(
        "ix_broadcast_runs_broadcast_scheduled", "broadcast_runs", ["broadcast_id", "scheduled_at"]
    )

    op.create_table(
        "sms_signatures",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("text", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sms_signatures_ws", "sms_signatures", ["workspace_id"])

    op.create_table(
        "split_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("channel_type", sa.String(length=24), nullable=False),
        sa.Column("strategy", sa.String(length=12), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("prefill_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("rr_cursor", sa.BigInteger(), nullable=False),
        sa.Column("click_count", sa.BigInteger(), nullable=False),
        sa.Column("qr_key", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_split_links_slug"),
    )
    op.create_index("ix_split_links_ws_status", "split_links", ["workspace_id", "status"])

    op.create_table(
        "edm_campaigns",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=24), nullable=False),
        sa.Column("segment_id", sa.UUID(), nullable=True),
        sa.Column("template_id", sa.UUID(), nullable=True),
        sa.Column("schedule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("planned_count", sa.Integer(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("delivered_count", sa.Integer(), nullable=False),
        sa.Column("opened_count", sa.Integer(), nullable=False),
        sa.Column("clicked_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["template_id"], ["msg_templates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["broadcast_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_edm_campaigns_ws_status", "edm_campaigns", ["workspace_id", "status"])

    # -------------------------------- partitioned append tables (raw SQL) -----
    op.execute(
        """
        CREATE TABLE broadcast_recipients (
            id UUID NOT NULL,
            run_id UUID NOT NULL REFERENCES broadcast_runs(id) ON DELETE CASCADE,
            broadcast_id UUID,
            workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            contact_id UUID,
            channel_identity_id UUID,
            state VARCHAR(12) NOT NULL DEFAULT 'planned',
            skip_reason VARCHAR(24),
            provider_message_id VARCHAR(255),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            sent_at TIMESTAMPTZ,
            delivered_at TIMESTAMPTZ,
            read_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_broadcast_recipients PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    op.execute(
        "CREATE TABLE broadcast_recipients_default PARTITION OF broadcast_recipients DEFAULT"
    )
    op.execute(
        "CREATE INDEX ix_broadcast_recipients_run_state "
        "ON broadcast_recipients (run_id, state)"
    )
    op.execute(
        "CREATE INDEX ix_broadcast_recipients_ws_contact "
        "ON broadcast_recipients (workspace_id, contact_id)"
    )

    op.execute(
        """
        CREATE TABLE split_link_clicks (
            id UUID NOT NULL,
            link_id UUID NOT NULL REFERENCES split_links(id) ON DELETE CASCADE,
            workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            target_idx INTEGER,
            tracking_code VARCHAR(32),
            ip_hash VARCHAR(64),
            ua TEXT,
            device VARCHAR(32),
            country VARCHAR(2),
            referrer TEXT,
            CONSTRAINT pk_split_link_clicks PRIMARY KEY (id, ts)
        ) PARTITION BY RANGE (ts)
        """
    )
    op.execute("CREATE TABLE split_link_clicks_default PARTITION OF split_link_clicks DEFAULT")
    op.execute("CREATE INDEX ix_split_link_clicks_link_ts ON split_link_clicks (link_id, ts)")

    # ================================================================== reports
    op.create_table(
        "agg_messages_hourly",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel_type", sa.String(length=24), server_default=_EMPTY, nullable=False),
        sa.Column("agent_id", sa.UUID(), server_default=_NIL_UUID, nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("ai_flag", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "workspace_id", "hour", "channel_type", "agent_id", "direction", "ai_flag"
        ),
    )

    op.create_table(
        "agg_conversations_hourly",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel_type", sa.String(length=24), server_default=_EMPTY, nullable=False),
        sa.Column("opened", sa.Integer(), nullable=False),
        sa.Column("resolved", sa.Integer(), nullable=False),
        sa.Column("reopened", sa.Integer(), nullable=False),
        sa.Column("frt_sum_s", sa.BigInteger(), nullable=False),
        sa.Column("frt_n", sa.Integer(), nullable=False),
        sa.Column("resolution_sum_s", sa.BigInteger(), nullable=False),
        sa.Column("resolution_n", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "hour", "channel_type"),
    )

    op.create_table(
        "agg_agent_hourly",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("msgs", sa.Integer(), nullable=False),
        sa.Column("convs", sa.Integer(), nullable=False),
        sa.Column("frt_sum_s", sa.BigInteger(), nullable=False),
        sa.Column("frt_n", sa.Integer(), nullable=False),
        sa.Column("csat_sum", sa.BigInteger(), nullable=False),
        sa.Column("csat_n", sa.Integer(), nullable=False),
        sa.Column("online_seconds", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "hour", "agent_id"),
    )

    op.create_table(
        "agg_customers_daily",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("day_local", sa.Date(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("new_deduped_count", sa.Integer(), nullable=False),
        sa.Column("repeat_count", sa.Integer(), nullable=False),
        sa.Column("merged_away", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "day_local"),
    )

    op.create_table(
        "agg_ads_daily",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("day_local", sa.Date(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("campaign_id", sa.String(length=128), server_default=_EMPTY, nullable=False),
        sa.Column("ad_id", sa.String(length=128), server_default=_EMPTY, nullable=False),
        sa.Column("conversations", sa.Integer(), nullable=False),
        sa.Column("messages", sa.Integer(), nullable=False),
        sa.Column("leads", sa.Integer(), nullable=False),
        sa.Column("spend_micros", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "day_local", "platform", "campaign_id", "ad_id"),
    )

    op.create_table(
        "agent_presence_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["workspace_members.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_presence_ws_agent_started",
        "agent_presence_sessions",
        ["workspace_id", "agent_id", "started_at"],
    )
    op.create_index(
        "ix_agent_presence_open", "agent_presence_sessions", ["workspace_id", "ended_at"]
    )

    op.create_table(
        "conversation_attribution",
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("ad_id", sa.String(length=128), nullable=True),
        sa.Column("campaign_id", sa.String(length=128), nullable=True),
        sa.Column("ref_code", sa.String(length=128), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index(
        "ix_conv_attribution_ws_campaign", "conversation_attribution", ["workspace_id", "campaign_id"]
    )

    op.create_table(
        "report_shares",
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("report_key", sa.String(length=48), nullable=False),
        sa.Column("report_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("created_by_member_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["workspace_members.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index("ix_report_shares_ws", "report_shares", ["workspace_id"])

    op.create_table(
        "report_exports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("report_key", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_exports_ws_created", "report_exports", ["workspace_id", "created_at"])

    op.create_table(
        "report_ai_summaries",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "day"),
    )

    op.create_table(
        "rollup_watermark",
        sa.Column("aggregate", sa.String(length=48), nullable=False),
        sa.Column("last_event_id", sa.UUID(), nullable=True),
        sa.Column("last_occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("aggregate"),
    )

    # ================================================================== billing
    op.create_table(
        "workspace_balance",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("balance_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )

    op.create_table(
        "balance_ledger",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("delta_cents", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=48), nullable=False),
        sa.Column("ref", sa.String(length=128), nullable=True),
        sa.Column("balance_after_cents", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_balance_ledger_ws_created", "balance_ledger", ["workspace_id", "created_at"])

    op.create_table(
        "billing_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("addons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("points", sa.BigInteger(), nullable=True),
        sa.Column("base_cents", sa.BigInteger(), nullable=False),
        sa.Column("addons_cents", sa.BigInteger(), nullable=False),
        sa.Column("discount_cents", sa.BigInteger(), nullable=False),
        sa.Column("handling_fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("balance_applied_cents", sa.BigInteger(), nullable=False),
        sa.Column("amount_due_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("stripe_ref", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_billing_orders_ws_created", "billing_orders", ["workspace_id", "created_at"])

    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("number", sa.String(length=48), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("stripe_invoice_id", sa.String(length=64), nullable=True),
        sa.Column("hosted_invoice_url", sa.Text(), nullable=True),
        sa.Column("pdf_url", sa.Text(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["billing_orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invoices_ws_created", "invoices", ["workspace_id", "created_at"])


def downgrade() -> None:
    # billing
    op.drop_index("ix_invoices_ws_created", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("stripe_events")
    op.drop_index("ix_billing_orders_ws_created", table_name="billing_orders")
    op.drop_table("billing_orders")
    op.drop_index("ix_balance_ledger_ws_created", table_name="balance_ledger")
    op.drop_table("balance_ledger")
    op.drop_table("workspace_balance")

    # reports
    op.drop_table("rollup_watermark")
    op.drop_table("report_ai_summaries")
    op.drop_index("ix_report_exports_ws_created", table_name="report_exports")
    op.drop_table("report_exports")
    op.drop_index("ix_report_shares_ws", table_name="report_shares")
    op.drop_table("report_shares")
    op.drop_index("ix_conv_attribution_ws_campaign", table_name="conversation_attribution")
    op.drop_table("conversation_attribution")
    op.drop_index("ix_agent_presence_open", table_name="agent_presence_sessions")
    op.drop_index("ix_agent_presence_ws_agent_started", table_name="agent_presence_sessions")
    op.drop_table("agent_presence_sessions")
    op.drop_table("agg_ads_daily")
    op.drop_table("agg_customers_daily")
    op.drop_table("agg_agent_hourly")
    op.drop_table("agg_conversations_hourly")
    op.drop_table("agg_messages_hourly")

    # marketing partitioned (raw)
    op.execute("DROP TABLE IF EXISTS split_link_clicks CASCADE")
    op.execute("DROP TABLE IF EXISTS broadcast_recipients CASCADE")

    # marketing
    op.drop_index("ix_edm_campaigns_ws_status", table_name="edm_campaigns")
    op.drop_table("edm_campaigns")
    op.drop_index("ix_split_links_ws_status", table_name="split_links")
    op.drop_table("split_links")
    op.drop_index("ix_sms_signatures_ws", table_name="sms_signatures")
    op.drop_table("sms_signatures")
    op.drop_index("ix_broadcast_runs_broadcast_scheduled", table_name="broadcast_runs")
    op.drop_index("ix_broadcast_runs_ws_broadcast", table_name="broadcast_runs")
    op.drop_table("broadcast_runs")
    op.drop_index("ix_broadcasts_ws_deleted", table_name="broadcasts")
    op.drop_index("ix_broadcasts_ws_type_status", table_name="broadcasts")
    op.drop_table("broadcasts")
    op.drop_index("ix_msg_templates_meta", table_name="msg_templates")
    op.drop_index("ix_msg_templates_ws_channel", table_name="msg_templates")
    op.drop_table("msg_templates")
    op.drop_index("ix_segments_ws", table_name="segments")
    op.drop_table("segments")
