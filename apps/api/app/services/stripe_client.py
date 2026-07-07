"""Stripe integration + pure order math (plan 2.3 + P3 計費模型實測).

Two concerns live here:

1. ``compute_order`` — a **pure, side-effect-free** function implementing the
   observed backend order math::

       amount_due = base(by duration) + handling_fee − duration_discount − balance

   Everything is integer **cents**. It is unit-tested against the captured
   example (Max 7-day: base $19.90, handling_fee $1.40 → amount_due $21.30).

2. A thin async wrapper over the ``stripe`` SDK that reads Settings and
   **degrades to "billing disabled" (never crashes) when no key is configured**
   or the SDK is not installed. ``get_stripe()`` returns ``None`` in that case;
   the billing module surfaces a 503. Tests mock these helpers — they never hit
   the network.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from ..settings import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover — typing only
    import stripe as _stripe_mod


# --------------------------------------------------------------------------
# pricing constants (config, not magic — mirrors the observed backend ladder)
# --------------------------------------------------------------------------
TRIAL_DAYS = 7
# 7-day option is a promotional trial priced at a fixed fraction of the monthly
# price (reproduces the observed Max 7-day base of $19.90 = 10% of $199).
TRIAL_FRACTION = 0.10

# duration (days) → discount rate applied to (base + add-ons)
DURATION_DISCOUNTS: dict[int, float] = {7: 0.0, 30: 0.0, 90: 0.10, 180: 0.15, 360: 0.20, 720: 0.25}

# default monthly add-on unit prices in cents (configurable per deploy). The
# observed backend does not publish these; sensible defaults that don't affect
# the base-plan example. official-channel default ≈ WhatsApp number rent.
DEFAULT_ADDON_MONTHLY_CENTS: dict[str, int] = {
    "seats": 500,
    "official_channels": 1200,
    "hosted_devices": 1200,
}

DEFAULT_HANDLING_FEE_PCT = 0.07

# points top-up price: $0.375 per 10,000 points (plan / contract).
POINTS_TOPUP_PRICE_CENTS_PER_10K = 37.5
POINTS_TOPUP_BLOCK = 10_000


@dataclass(frozen=True)
class OrderBreakdown:
    """All amounts in integer cents. ``base_cents`` is the plan portion by
    duration; ``addons_cents`` the add-on line items; ``discount_cents`` the
    duration discount; ``handling_fee_cents`` the processing fee (ceil);
    ``balance_applied_cents`` the prepaid balance used; ``amount_due_cents``
    what Stripe actually charges."""

    base_cents: int
    addons_cents: int
    discount_cents: int
    handling_fee_cents: int
    balance_applied_cents: int
    amount_due_cents: int
    currency: str = "usd"

    @property
    def subtotal_cents(self) -> int:
        """Priced amount before the balance is applied (base + add-ons − discount
        + handling fee)."""
        return (
            self.base_cents
            + self.addons_cents
            - self.discount_cents
            + self.handling_fee_cents
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _duration_multiplier(duration_days: int, *, trial_fraction: float) -> float:
    """Months-equivalent multiplier applied to monthly prices. The 7-day trial
    is a fixed fraction; every other duration is prorated as days/30."""
    if duration_days == TRIAL_DAYS:
        return trial_fraction
    return duration_days / 30.0


def _round_cents(value: float) -> int:
    """Round to nearest cent, half-up (deterministic for money)."""
    return int(math.floor(value + 0.5))


_BPS = 10_000  # basis-point scale for exact percentage math (no float drift)


def _pct_to_bps(pct: float) -> int:
    return round(pct * _BPS)


def _rate_round(amount_cents: int, bps: int) -> int:
    """amount × bps/10000, rounded half-up, in pure integer arithmetic."""
    return (amount_cents * bps + _BPS // 2) // _BPS


def _rate_ceil(amount_cents: int, bps: int) -> int:
    """amount × bps/10000, rounded UP, in pure integer arithmetic. Percentages
    like 0.07 are not exact floats (19900×0.07 = 1393.0000000000002 → ceil would
    wrongly give 1394); basis-point integer math avoids that."""
    if amount_cents <= 0 or bps <= 0:
        return 0
    return (amount_cents * bps + _BPS - 1) // _BPS


def compute_order(
    monthly_price_cents: int,
    duration_days: int,
    addons: dict[str, int] | None = None,
    balance_cents: int = 0,
    *,
    addon_monthly_cents: dict[str, int] | None = None,
    handling_fee_pct: float = DEFAULT_HANDLING_FEE_PCT,
    discounts: dict[int, float] | None = None,
    trial_fraction: float = TRIAL_FRACTION,
    currency: str = "usd",
) -> OrderBreakdown:
    """Compute a subscription order breakdown.

    ``monthly_price_cents`` — the plan's monthly price in cents (e.g. Max=19900).
    ``duration_days`` — 7 (trial) / 30 / 90 / 180 / 360 / 720.
    ``addons`` — additional units beyond the plan's included quota, e.g.
    ``{"seats": 2, "official_channels": 1}``.
    ``balance_cents`` — prepaid balance available to apply.

    Formula (all cents): base = monthly × months; addons = Σ(unit × qty) × months;
    discount = (base+addons) × rate[duration]; handling_fee =
    ceil((base+addons−discount) × pct); balance_applied = min(balance, subtotal);
    amount_due = subtotal − balance_applied.
    """
    if monthly_price_cents < 0:
        raise ValueError("monthly_price_cents must be >= 0")
    if duration_days <= 0:
        raise ValueError("duration_days must be positive")
    addon_prices = addon_monthly_cents or DEFAULT_ADDON_MONTHLY_CENTS
    discount_table = discounts or DURATION_DISCOUNTS

    multiplier = _duration_multiplier(duration_days, trial_fraction=trial_fraction)

    base_cents = _round_cents(monthly_price_cents * multiplier)

    addon_monthly_total = 0
    for key, qty in (addons or {}).items():
        if qty <= 0:
            continue
        unit = addon_prices.get(key)
        if unit is None:
            raise ValueError(f"unknown add-on: {key!r}")
        addon_monthly_total += unit * qty
    addons_cents = _round_cents(addon_monthly_total * multiplier)

    priced = base_cents + addons_cents
    discount_rate = discount_table.get(duration_days, 0.0)
    discount_cents = _rate_round(priced, _pct_to_bps(discount_rate))

    subtotal_before_fee = priced - discount_cents
    handling_fee_cents = _rate_ceil(subtotal_before_fee, _pct_to_bps(handling_fee_pct))

    subtotal = subtotal_before_fee + handling_fee_cents
    balance_applied_cents = max(0, min(balance_cents, subtotal))
    amount_due_cents = subtotal - balance_applied_cents

    return OrderBreakdown(
        base_cents=base_cents,
        addons_cents=addons_cents,
        discount_cents=discount_cents,
        handling_fee_cents=handling_fee_cents,
        balance_applied_cents=balance_applied_cents,
        amount_due_cents=amount_due_cents,
        currency=currency,
    )


def compute_points_topup(points: int) -> int:
    """Price (cents) for a points top-up. $0.375 per 10,000 points; must be a
    positive multiple of the 10k block (rounded up to the next block)."""
    if points <= 0:
        raise ValueError("points must be positive")
    blocks = math.ceil(points / POINTS_TOPUP_BLOCK)
    return _round_cents(blocks * POINTS_TOPUP_PRICE_CENTS_PER_10K)


# --------------------------------------------------------------------------
# Stripe SDK wrapper (billing-disabled-safe)
# --------------------------------------------------------------------------
class BillingDisabledError(RuntimeError):
    """Raised by charge helpers when Stripe is not configured. Callers should
    translate this into a 503 (billing unavailable), never a 500."""


def _import_stripe() -> _stripe_mod | None:
    try:
        import stripe  # type: ignore
    except ImportError:  # pragma: no cover — SDK optional in dev/test venv
        return None
    return stripe


def get_stripe(settings: Settings | None = None) -> _stripe_mod | None:
    """Return a configured ``stripe`` module, or ``None`` when billing is
    disabled (no secret key, or SDK not installed). Sets api_key on the module."""
    s = settings or get_settings()
    if not s.stripe_secret_key:
        return None
    stripe = _import_stripe()
    if stripe is None:
        return None
    stripe.api_key = s.stripe_secret_key
    return stripe


def billing_enabled(settings: Settings | None = None) -> bool:
    return get_stripe(settings) is not None


def _require_stripe(settings: Settings | None = None) -> _stripe_mod:
    stripe = get_stripe(settings)
    if stripe is None:
        raise BillingDisabledError(
            "billing disabled: STRIPE_SECRET_KEY not configured (or stripe SDK missing)"
        )
    return stripe


async def create_payment_intent(
    *,
    amount_cents: int,
    currency: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create a Stripe PaymentIntent for an on-session card/wallet charge.
    Returns ``{"id", "client_secret", "amount", "currency", "status"}``."""
    s = settings or get_settings()
    stripe = _require_stripe(s)
    kwargs: dict[str, Any] = {
        "amount": int(amount_cents),
        "currency": (currency or s.stripe_currency),
        "automatic_payment_methods": {"enabled": True},
        "metadata": metadata or {},
    }
    intent = await asyncio.to_thread(
        lambda: stripe.PaymentIntent.create(idempotency_key=idempotency_key, **kwargs)
    )
    return {
        "id": intent["id"],
        "client_secret": intent["client_secret"],
        "amount": intent["amount"],
        "currency": intent["currency"],
        "status": intent["status"],
    }


async def create_checkout_session(
    *,
    amount_cents: int,
    currency: str | None = None,
    product_name: str,
    success_url: str,
    cancel_url: str,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create a hosted Stripe Checkout Session for a single ad-hoc line item.
    Returns ``{"id", "url"}``."""
    s = settings or get_settings()
    stripe = _require_stripe(s)
    line_items = [
        {
            "price_data": {
                "currency": (currency or s.stripe_currency),
                "product_data": {"name": product_name},
                "unit_amount": int(amount_cents),
            },
            "quantity": 1,
        }
    ]
    session = await asyncio.to_thread(
        lambda: stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata or {},
            idempotency_key=idempotency_key,
        )
    )
    return {"id": session["id"], "url": session["url"]}


def construct_event(
    payload: bytes | str,
    sig_header: str,
    settings: Settings | None = None,
) -> Any:
    """Verify a Stripe webhook signature and return the parsed Event. Raises
    ``BillingDisabledError`` if no webhook secret is configured, and Stripe's
    ``SignatureVerificationError`` on a bad signature (caller → 400)."""
    s = settings or get_settings()
    stripe = _import_stripe()
    if stripe is None or not s.stripe_webhook_secret:
        raise BillingDisabledError(
            "webhook verification unavailable: STRIPE_WEBHOOK_SECRET not configured"
        )
    return stripe.Webhook.construct_event(payload, sig_header, s.stripe_webhook_secret)


# Alias matching the route-contract naming.
verify_webhook_signature = construct_event
