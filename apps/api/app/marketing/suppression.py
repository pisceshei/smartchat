"""Suppression pass for the broadcast fan-out (plan B.3).

Before a materialised audience is chunked and sent, every candidate runs the
suppression gate. A candidate that trips any rule becomes a ``skipped``
recipient with a typed reason (never sent):

  dedupe          — same channel identity already targeted in THIS run
  invalid_identity— the contact has no reachable identity on the send channel
  unsubscribed    — the contact opted out of marketing
  blacklist       — the contact is blacklisted
  freq_cap        — the contact already got >= N marketing sends this week

The identity-level dedupe is handled by the caller (it holds the per-run set);
this module owns the contact/identity predicates + the weekly frequency count.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.contacts import Contact
from ..models.marketing import BroadcastRecipient

# terminal skip reasons (recipient.skip_reason, <=24 chars per the column)
SKIP_DEDUPE = "dedupe"
SKIP_INVALID_IDENTITY = "invalid_identity"
SKIP_UNSUBSCRIBED = "unsubscribed"
SKIP_BLACKLIST = "blacklist"
SKIP_FREQ_CAP = "freq_cap"
SKIP_QUOTA = "quota"
SKIP_OUT_OF_WINDOW = "out_of_window"

SENT_STATES = ("sent", "delivered", "read")


def is_blacklisted(contact: Contact) -> bool:
    return bool(contact.is_blacklisted)


def is_unsubscribed(contact: Contact) -> bool:
    """Marketing opt-out. Stored on the contact custom bag
    (``marketing_opt_out`` / ``unsubscribed``) — set by the OptOut inbound
    event handler or a preference-centre webhook."""
    custom = contact.custom or {}
    return bool(custom.get("marketing_opt_out") or custom.get("unsubscribed"))


@dataclass(frozen=True)
class SuppressionConfig:
    freq_cap_per_week: int = 0  # 0 disables the weekly cap
    window_days: int = 7


async def weekly_send_count(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
    window_days: int = 7,
    now: datetime | None = None,
) -> int:
    """How many marketing messages this contact has already been *sent* inside
    the rolling window (feeds the per-contact frequency cap)."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=window_days)
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(BroadcastRecipient)
                .where(
                    BroadcastRecipient.workspace_id == workspace_id,
                    BroadcastRecipient.contact_id == contact_id,
                    BroadcastRecipient.state.in_(SENT_STATES),
                    BroadcastRecipient.created_at >= since,
                )
            )
        ).scalar_one()
    )


def contact_suppression_reason(contact: Contact) -> str | None:
    """Pure contact-level checks (blacklist / unsubscribe). Returns a skip
    reason or None. Frequency cap + dedupe + identity are evaluated by the
    caller because they need the run set / DB / identity resolution."""
    if is_blacklisted(contact):
        return SKIP_BLACKLIST
    if is_unsubscribed(contact):
        return SKIP_UNSUBSCRIBED
    return None


def mark_unsubscribed(contact: Contact, *, value: bool = True) -> None:
    """Flip the marketing opt-out flag on a contact (used by the OptOut handler
    and the unsubscribe endpoint)."""
    custom: dict[str, Any] = dict(contact.custom or {})
    custom["marketing_opt_out"] = value
    contact.custom = custom
