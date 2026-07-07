"""Points enforcement wrapper (plan 附錄 B.2 「點數計量」).

Wraps services.points.check_and_decr with:
  - per-feature costs looked up from the `ai_point_prices` config table
    (price is config, not code — a small time-based cache avoids a DB hit per
    spend), with hard-coded fallbacks matching the migration seed;
  - unit multipliers for metered features (translate_llm_per500 → per 500 chars,
    embed_per10k → per 10k tokens); flat features use units=1;
  - a per-feature HARD-STOP behavior the caller applies on a floor-0 reject:
      ai_reply   → handoff   (apologise + escalate to a human)
      intent     → skip      (trigger stays silent)
      translation→ fallback  (next engine in the chain, then off)
      composer   → error     (surface an upgrade CTA)
      summary/embed/report → skip / error respectively.

Long operations reserve up front then settle (多退少補) via reserve()/settle().
The ledger row + points.consumed event are written into the caller's session by
services.points — this module never commits.
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.ai import AIPointPrice
from ..services import points

# feature_key → hard-stop behavior the caller applies when the spend is rejected
HANDOFF = "handoff"
SKIP = "skip"
FALLBACK = "fallback"
ERROR = "error"

FEATURE_HARDSTOP: dict[str, str] = {
    "ai_reply": HANDOFF,
    "intent": SKIP,
    "translate_llm_per500": FALLBACK,
    "composer": ERROR,
    "embed_per10k": ERROR,
    "summary": SKIP,
    "report_summary": ERROR,
}

# fallback prices (mirror the 0002 seed) used only if the config row is missing
DEFAULT_PRICES: dict[str, int] = {
    "ai_reply": 10,
    "intent": 1,
    "translate_llm_per500": 1,
    "composer": 2,
    "embed_per10k": 1,
    "summary": 5,
    "report_summary": 20,
}

# how many source units one price charge covers (1 = flat per call)
FEATURE_UNIT_SIZE: dict[str, int] = {
    "translate_llm_per500": 500,   # characters
    "embed_per10k": 10_000,        # tokens
}

_PRICE_CACHE_TTL = 60.0
_price_cache: dict[str, tuple[float, int]] = {}


def hardstop_for(feature_key: str) -> str:
    return FEATURE_HARDSTOP.get(feature_key, ERROR)


def units_for(feature_key: str, amount: int) -> int:
    """Number of price charges for `amount` source units of a metered feature
    (ceil division; flat features ignore `amount` and cost 1)."""
    size = FEATURE_UNIT_SIZE.get(feature_key)
    if size is None:
        return 1
    return max(1, math.ceil(max(0, amount) / size))


def clear_price_cache() -> None:
    _price_cache.clear()


async def price_for(session: AsyncSession, feature_key: str) -> int:
    """Points per charge for a feature. Config table wins; falls back to the
    seed default; unknown features cost 0 (never block)."""
    now = time.monotonic()
    cached = _price_cache.get(feature_key)
    if cached is not None and now - cached[0] < _PRICE_CACHE_TTL:
        return cached[1]
    row = (
        await session.execute(
            select(AIPointPrice.points).where(AIPointPrice.feature_key == feature_key)
        )
    ).scalar_one_or_none()
    price = int(row) if row is not None else DEFAULT_PRICES.get(feature_key, 0)
    _price_cache[feature_key] = (now, price)
    return price


@dataclass
class EnforceResult:
    ok: bool
    feature_key: str
    points_charged: int
    balance_after: int
    hardstop: str  # behavior to apply when ok is False ("" when ok)

    @property
    def blocked(self) -> bool:
        return not self.ok


async def spend(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    feature_key: str,
    amount: int = 1,
    reason: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> EnforceResult:
    """Charge points for one use of `feature_key`. `amount` is the source-unit
    count for metered features (chars/tokens); flat features charge the unit
    price once. On a floor-0 reject returns ok=False plus the feature's
    hard-stop behavior — the caller decides what to do."""
    price = await price_for(session, feature_key)
    cost = price * units_for(feature_key, amount)
    result = await points.check_and_decr(
        session,
        redis,
        workspace_id=workspace_id,
        cost=cost,
        reason=reason or feature_key,
        ref_type=ref_type or "ai_feature",
        ref_id=ref_id,
    )
    return EnforceResult(
        ok=result.ok,
        feature_key=feature_key,
        points_charged=cost if result.ok else 0,
        balance_after=result.balance_after,
        hardstop="" if result.ok else hardstop_for(feature_key),
    )


async def reserve(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    feature_key: str,
    amount: int = 1,
    ref_id: str | None = None,
) -> EnforceResult:
    """Reserve up-front for a long operation (先預留). Identical to spend();
    pair with settle() to refund the unused remainder."""
    return await spend(
        session,
        redis,
        workspace_id=workspace_id,
        feature_key=feature_key,
        amount=amount,
        reason=f"{feature_key}:reserve",
        ref_type="ai_reserve",
        ref_id=ref_id,
    )


async def settle(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    feature_key: str,
    reserved_amount: int,
    actual_amount: int,
    ref_id: str | None = None,
) -> int:
    """Refund the over-reserved remainder after a long operation (多退少補).
    Returns the points refunded (0 if the estimate was exact/under)."""
    price = await price_for(session, feature_key)
    reserved_pts = price * units_for(feature_key, reserved_amount)
    actual_pts = price * units_for(feature_key, actual_amount)
    refund_pts = reserved_pts - actual_pts
    if refund_pts <= 0:
        return 0
    await points.refund(
        session,
        redis,
        workspace_id=workspace_id,
        points=refund_pts,
        reason=f"{feature_key}:settle",
        ref_type="ai_settle",
        ref_id=ref_id,
    )
    return refund_pts
