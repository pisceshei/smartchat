"""Channel accounts / hosted-device bridges / widgets (plan 附錄 A.3)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_fk, uuid_pk, workspace_fk

CHANNEL_TYPES = (
    "widget",
    "telegram_bot",
    "telegram_app",
    "email",
    "messenger",
    "instagram",
    "whatsapp_cloud",
    "whatsapp_app",
    "whatsapp_bsp",
    "line_oa",
    "line_app",
    "wechat",
    "wecom",
    "wechat_kf",
    "tiktok",
    "tiktok_business",
    "youtube",
    "zalo_app",
    "slack",
    "vk",
    "sms",
)


class ChannelAccount(Base):
    """One connected channel account. UNIQUE(channel_type, external_id) is the
    webhook fan-in routing key AND prevents cross-tenant double-binding.
    external_id: WA phone_number_id / FB page_id / IG account id / TG bot id /
    email address / LINE channel id / widget key."""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint("channel_type", "external_id", name="uq_channel_accounts_type_ext"),
        CheckConstraint(
            "channel_type IN ({})".format(",".join(f"'{t}'" for t in CHANNEL_TYPES)),
            name="ck_channel_accounts_type",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    channel_type: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # envelope-encrypted credentials blob (never plaintext in JSONB)
    credentials_enc: Mapped[bytes | None] = mapped_column(BYTEA)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    webhook_secret: Mapped[str | None] = mapped_column(String(64))
    # pending/active/disconnected/error
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class DeviceBridge(Base):
    """Hosted personal-account bridge (WhatsApp App / LINE App). One container
    per device; session blob (encrypted) lives in MinIO; 15s heartbeat, 60s
    offline, 5m restart. logged_out/banned are terminal — never auto re-pair."""

    __tablename__ = "device_bridges"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    channel_account_id: Mapped[uuid.UUID] = uuid_fk("channel_accounts.id", unique=True)
    bridge_type: Mapped[str] = mapped_column(String(16), nullable=False)  # wa_app/line_app
    container_name: Mapped[str | None] = mapped_column(String(128))
    # pairing/online/offline/logged_out/banned
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pairing")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_blob_key: Mapped[str | None] = mapped_column(Text)  # MinIO object key
    proxy_url_enc: Mapped[bytes | None] = mapped_column(BYTEA)  # per-device proxy, encrypted
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    restart_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Widget(Base):
    """Chat-widget configuration. widget_key is the public embed token used in
    /js/project_{key}.js; the paired ChannelAccount(channel_type=widget) has
    external_id = widget_key."""

    __tablename__ = "widgets"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    channel_account_id: Mapped[uuid.UUID | None] = uuid_fk(
        "channel_accounts.id", ondelete="SET NULL", nullable=True
    )
    widget_key: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # brand/colors/position/languages/lead form/home mode/help center/auto-open…
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    allowed_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    brand_removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # gated ≥Pro
    default_flow_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))  # bound automation (P2)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
