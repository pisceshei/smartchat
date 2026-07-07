"""Shared model helpers: UUIDv7 PKs, tenant scoping, timestamps.

Conventions (plan 附錄 A.0):
- every tenant table has workspace_id as the FIRST column of its composite
  indexes; PKs are UUIDv7 (time-ordered, generatable in any service)
- all timestamps are UTC timestamptz
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

try:  # uuid7 package exposes uuid7()
    from uuid_extensions import uuid7  # type: ignore
except ImportError:  # pragma: no cover
    from uuid6 import uuid7  # type: ignore

__all__ = ["uuid7", "uuid_pk", "uuid_fk", "workspace_fk", "created_at_col", "updated_at_col"]


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)


def uuid_fk(target: str, *, ondelete: str = "CASCADE", **kw: Any) -> Mapped[uuid.UUID]:
    return mapped_column(PG_UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), **kw)


def workspace_fk(*, index: bool = True, **kw: Any) -> Mapped[uuid.UUID]:
    """Tenant scope column. Indexed unless the table has a composite index
    that already leads with workspace_id (pass index=False there)."""
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=index,
        **kw,
    )


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
