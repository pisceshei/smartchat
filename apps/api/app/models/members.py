"""Users / members / roles / groups / shifts (plan 附錄 A.2).

Humans and AI agents share workspace_members (member_type) so
conversations.assignee_member_id is a single FK either way.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import created_at_col, updated_at_col, uuid_fk, uuid_pk, workspace_fk


class User(Base):
    """Global login identity; can join multiple workspaces."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    avatar_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Role(Base):
    """permissions jsonb = list of permission keys; "*" = all. role_version
    bumps on edit to invalidate cached JWTs/permission caches."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_roles_ws_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str | None] = mapped_column(String(32))  # system role key: super_admin/admin/agent
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    role_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )  # NULL for ai_agent members
    member_type: Mapped[str] = mapped_column(String(16), nullable=False, default="human")  # human/ai_agent
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="SET NULL")
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    avatar_url: Mapped[str | None] = mapped_column(Text)
    # active/invited/disabled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 每日同時接待上限; 0 = unlimited. Live in-flight count is Redis cap:{member}.
    max_concurrent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # persona/kb/skills/quota for ai_agent
    invited_email: Mapped[str | None] = mapped_column(CITEXT())
    invite_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


# partial unique: one membership per (workspace, user) for human members
Index(
    "uq_members_ws_user_notnull",
    WorkspaceMember.workspace_id,
    WorkspaceMember.user_id,
    unique=True,
    postgresql_where=WorkspaceMember.user_id.isnot(None),
)


class MemberGroup(Base):
    __tablename__ = "member_groups"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_member_groups_ws_name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class MemberGroupMember(Base):
    __tablename__ = "member_group_members"

    workspace_id: Mapped[uuid.UUID] = workspace_fk()
    group_id: Mapped[uuid.UUID] = uuid_fk("member_groups.id", primary_key=True)
    member_id: Mapped[uuid.UUID] = uuid_fk("workspace_members.id", primary_key=True)
    created_at: Mapped[datetime] = created_at_col()


class MemberShift(Base):
    """Weekly recurring shift. Assignment engine: online = presence AND
    on-shift (no shifts defined = always on-shift)."""

    __tablename__ = "member_shifts"
    __table_args__ = (Index("ix_member_shifts_ws_member", "workspace_id", "member_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    member_id: Mapped[uuid.UUID] = uuid_fk("workspace_members.id")
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon .. 6=Sun
    start_min: Mapped[int] = mapped_column(Integer, nullable=False)  # minutes from local midnight
    end_min: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str | None] = mapped_column(String(48))  # fallback: workspace tz
    created_at: Mapped[datetime] = created_at_col()


class MemberDailyStats(Base):
    __tablename__ = "member_daily_stats"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    member_id: Mapped[uuid.UUID] = uuid_fk("workspace_members.id", primary_key=True)
    day: Mapped[datetime] = mapped_column(Date, primary_key=True)
    handled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_response_ms_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_response_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    online_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = updated_at_col()
