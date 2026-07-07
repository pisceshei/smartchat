"""Split-link attribution loop (plan B.3: 歸因閉環).

When a click's prefilled message reaches us as the first inbound WhatsApp (or
other channel) message, its ``{{code}}`` tracking token lets us tie the
conversation back to the click → the ad campaign that drove it. This helper is
exposed for the channel ingress to call on an inbound text; it does NOT rewrite
ingress. It writes a ``conversation_attribution`` row (source='splitlink') that
the ads report reads.
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.marketing import SplitLink, SplitLinkClick
from ..models.reports import ConversationAttribution

# split-link tracking codes are 8-char base62 (see split_links.service.tracking_code)
_TOKEN_RE = re.compile(r"[0-9A-Za-z]{8,12}")
LOOKBACK_DAYS = 14


async def attribute_inbound(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    text: str,
    now: datetime | None = None,
) -> bool:
    """Scan an inbound message for a split-link tracking code and, on a match,
    record the conversation↔click attribution. Returns True on a link. Safe to
    call on every inbound (no-op when there is no code / already attributed)."""
    if not text:
        return False
    now = now or datetime.now(UTC)
    candidates = list({m.group(0) for m in _TOKEN_RE.finditer(text)})
    if not candidates:
        return False
    since = now - timedelta(days=LOOKBACK_DAYS)
    click = (
        await session.execute(
            select(SplitLinkClick)
            .where(
                SplitLinkClick.workspace_id == workspace_id,
                SplitLinkClick.tracking_code.in_(candidates),
                SplitLinkClick.ts >= since,
            )
            .order_by(SplitLinkClick.ts.desc())
            .limit(1)
        )
    ).scalars().first()
    if click is None:
        return False
    link = await session.get(SplitLink, click.link_id)
    await session.execute(
        pg_insert(ConversationAttribution)
        .values(
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            source="splitlink",
            ref_code=click.tracking_code,
            campaign_id=str(click.link_id),
            meta={
                "split_link_id": str(click.link_id),
                "slug": link.slug if link else None,
                "name": link.name if link else None,
                "target_idx": click.target_idx,
                "clicked_at": click.ts.isoformat() if click.ts else None,
            },
        )
        .on_conflict_do_nothing(index_elements=["conversation_id"])
    )
    return True
