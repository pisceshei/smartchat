"""P4 (device bridge + billing): platform_settings singleton table.

Instance-wide operator settings (currently the runtime-configurable Stripe
keys). Secrets are envelope-encrypted (``*_enc`` bytea columns) with a per-row
platform data key; the publishable key + currency are stored plainly. The row
is pinned to id = 1 by a CHECK constraint (singleton).

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BYTEA

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("data_key_enc", BYTEA(), nullable=True),
        sa.Column("stripe_secret_enc", BYTEA(), nullable=True),
        sa.Column("stripe_publishable", sa.String(length=255), nullable=True),
        sa.Column("stripe_webhook_secret_enc", BYTEA(), nullable=True),
        sa.Column("stripe_currency", sa.String(length=3), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_platform_settings_singleton"),
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
