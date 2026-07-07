"""Conversation ad/referral attribution (plan 附錄 B.4 廣告分析).

Attribution is captured **once, at conversation creation**, from the inbound
payload's CTWA referral block / split-link tracking code / UTM params and frozen
into ``conversation_attribution``. The ad reports then read that table (a
conversation's channel/campaign never changes, so this is write-once).

``parse_payload`` is a pure classifier the channel ingress can call; ``stamp``
performs the idempotent upsert. Wiring is a single call at the
``conversation.created`` site in the channel ingress pipeline — kept minimal and
out-of-band so a missing referral simply yields ``source='direct'``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..models.reports import ConversationAttribution

# channels whose paid entry point shows under 訊息廣告 (CTWA / messaging ads)
_MESSENGER_CHANNELS = frozenset({"messenger", "instagram", "whatsapp_cloud", "whatsapp"})


@dataclass(frozen=True)
class Attribution:
    source: str = "direct"  # direct | facebook_ad | ctwa | referral | split_link
    ad_id: str | None = None
    campaign_id: str | None = None
    ref_code: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def platform(self, channel_type: str | None) -> str | None:
        """facebook | messenger for the ad reports, else None (non-ad)."""
        if self.source == "direct":
            return None
        if self.source in ("ctwa", "split_link") or (channel_type in _MESSENGER_CHANNELS):
            return "messenger"
        return "facebook"


def parse_payload(payload: dict[str, Any] | None, *, channel_type: str | None = None) -> Attribution:
    """Extract attribution from a normalized inbound payload. Recognises Meta
    CTWA ``referral`` blocks, split-link ``tracking_code`` and UTM params."""
    p = payload or {}
    ref = p.get("referral") or p.get("ad_referral") or {}
    if isinstance(ref, dict) and (ref.get("source_id") or ref.get("ad_id") or ref.get("ctwa_clid")):
        source = "ctwa" if (ref.get("ctwa_clid") or channel_type in _MESSENGER_CHANNELS) else "facebook_ad"
        return Attribution(
            source=source,
            ad_id=_s(ref.get("ad_id") or ref.get("source_id")),
            campaign_id=_s(ref.get("campaign_id") or ref.get("source_url")),
            ref_code=_s(ref.get("ref") or ref.get("ctwa_clid")),
            meta={k: v for k, v in ref.items() if v is not None},
        )
    tracking = p.get("tracking_code") or p.get("ref_code")
    if tracking:
        return Attribution(source="split_link", ref_code=_s(tracking), meta={"tracking_code": _s(tracking)})
    utm = p.get("utm") or {}
    if isinstance(utm, dict) and utm.get("utm_source"):
        return Attribution(
            source="referral",
            campaign_id=_s(utm.get("utm_campaign")),
            ref_code=_s(utm.get("utm_content")),
            meta={k: v for k, v in utm.items() if v is not None},
        )
    return Attribution()


async def stamp(
    session,
    *,
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    attribution: Attribution,
) -> None:
    """Idempotent write-once upsert (PK = conversation_id). A later inbound
    referral never overwrites the first-touch attribution."""
    stmt = pg_insert(ConversationAttribution).values(
        conversation_id=conversation_id,
        workspace_id=workspace_id,
        source=attribution.source,
        ad_id=attribution.ad_id,
        campaign_id=attribution.campaign_id,
        ref_code=attribution.ref_code,
        meta=attribution.meta or {},
    ).on_conflict_do_nothing(index_elements=["conversation_id"])
    await session.execute(stmt)


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s[:128] or None
