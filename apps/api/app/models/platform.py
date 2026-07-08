"""Platform-level (super-admin, tenant-agnostic) settings.

Unlike every other table in the schema, ``platform_settings`` is NOT tenant
scoped — it holds instance-wide configuration a platform operator sets once
(currently the Stripe billing keys, so the payment processor can be swapped
from the admin backend without a redeploy).

Singleton row: ``id`` is pinned to ``1`` by a CHECK constraint, so upserts and
reads target a fixed primary key. Secrets are envelope-encrypted at rest with a
per-row platform data key (``data_key_enc``, itself wrapped by
CREDENTIALS_MASTER_KEY) — the same scheme workspaces use, so no plaintext
secret ever lands in a column. Non-secret values (publishable key, currency)
are stored plainly.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.dialects.postgresql import BYTEA
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col


class PlatformSettings(Base):
    """Instance-wide operator settings (singleton, id == 1)."""

    __tablename__ = "platform_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_platform_settings_singleton"),
    )

    #: pinned singleton primary key (always 1)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    #: wrapped platform data key (Fernet), used to encrypt the *_enc columns
    data_key_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    #: Stripe secret key (sk_…), envelope-encrypted
    stripe_secret_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    #: Stripe publishable key (pk_…) — public, stored plainly for the frontend
    stripe_publishable: Mapped[str | None] = mapped_column(String(255))
    #: Stripe webhook signing secret (whsec_…), envelope-encrypted
    stripe_webhook_secret_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    #: default charge currency override (falls back to Settings.stripe_currency)
    stripe_currency: Mapped[str | None] = mapped_column(String(3))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


PLATFORM_SETTINGS_ID = 1
