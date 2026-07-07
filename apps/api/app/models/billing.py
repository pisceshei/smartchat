"""Billing / commercialisation model (plan 2.3 + P3 計費模型實測).

Extends the P1 tenancy tables (``plans`` / ``subscriptions`` / ``usage_counters``
/ ``ai_points_ledger`` live in models/tenancy.py and are NOT redefined here)
with the pay-by-duration order + balance + Stripe-webhook surface:

- ``workspace_balance`` + ``balance_ledger`` — the 餘額系統 (prepaid balance the
  order math applies as ``balance_applied``), append-only ledger.
- ``billing_orders`` — one row per checkout (subscription | points_topup) with
  the full order breakdown (base/discount/handling_fee/balance_applied/amount_due
  in integer cents) and the Stripe reference.
- ``stripe_events`` — processed-webhook dedupe (idempotent event handling).
- ``invoices`` — issued invoice records (mirrors Stripe invoices when present).

All money is stored as integer **cents** to avoid float drift.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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
from .base import created_at_col, updated_at_col, uuid_pk, workspace_fk


class WorkspaceBalance(Base):
    """Prepaid account balance (餘額), one row per workspace. Authoritative
    total; every change is mirrored into ``balance_ledger``."""

    __tablename__ = "workspace_balance"

    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False, primary_key=True)
    balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd")
    updated_at: Mapped[datetime] = updated_at_col()


class BalanceLedger(Base):
    """Append-only balance flow: topup (+) / order_apply (−) / refund (+) /
    adjust (±). ``ref`` points at the order or Stripe object."""

    __tablename__ = "balance_ledger"
    __table_args__ = (Index("ix_balance_ledger_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    delta_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # topup/order_apply/refund/adjust
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(128))
    balance_after_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = created_at_col()


class BillingOrder(Base):
    """One checkout order. ``kind`` = subscription | points_topup. Order math
    (see services/stripe_client.compute_order) is frozen into the cent columns:
    amount_due = base + handling_fee − discount − balance_applied (+ add-ons)."""

    __tablename__ = "billing_orders"
    __table_args__ = (Index("ix_billing_orders_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # subscription/points_topup
    plan_code: Mapped[str | None] = mapped_column(String(32), ForeignKey("plans.code"))
    duration_days: Mapped[int | None] = mapped_column(Integer)
    addons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    points: Mapped[int | None] = mapped_column(BigInteger)  # for points_topup
    base_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    addons_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    handling_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    balance_applied_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_due_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd")
    stripe_ref: Mapped[str | None] = mapped_column(String(128))  # payment_intent / checkout session
    # pending/paid/failed/canceled
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StripeEvent(Base):
    """Idempotent webhook dedupe: the Stripe event id is the PK, so a replayed
    webhook is a no-op."""

    __tablename__ = "stripe_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class Invoice(Base):
    """Issued invoice record. Mirrors a Stripe invoice when the checkout used a
    Stripe Invoice/Subscription; self-hosted orders synthesise a number."""

    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoices_ws_created", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = workspace_fk(index=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("billing_orders.id", ondelete="SET NULL")
    )
    number: Mapped[str | None] = mapped_column(String(48))
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="open")  # paid/open/void
    stripe_invoice_id: Mapped[str | None] = mapped_column(String(64))
    hosted_invoice_url: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
