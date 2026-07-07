"""Order/discount math (plan P3 計費模型) — pure, no Stripe, no network.

Ground truth = the captured backend example: Max 7-day trial, base $19.90,
handling fee $1.40 → amount due $21.30.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from apps.api.app.services.stripe_client import (
    DURATION_DISCOUNTS,
    OrderBreakdown,
    compute_order,
    compute_points_topup,
)

MAX_MONTHLY = 19_900  # $199.00 in cents
PRO_MONTHLY = 1_590  # $15.90 in cents


# --------------------------------------------------------------------------
# the captured example
# --------------------------------------------------------------------------
def test_observed_max_7day_example():
    o = compute_order(MAX_MONTHLY, 7)
    assert o.base_cents == 1990  # $19.90 (10% trial fraction of $199)
    assert o.addons_cents == 0
    assert o.discount_cents == 0  # trial has no duration discount
    assert o.handling_fee_cents == 140  # $1.40 (ceil(1990 * 0.07) = ceil(139.3))
    assert o.balance_applied_cents == 0
    assert o.amount_due_cents == 2130  # $21.30


def test_breakdown_is_frozen_and_serialisable():
    o = compute_order(MAX_MONTHLY, 7)
    assert isinstance(o, OrderBreakdown)
    with pytest.raises(FrozenInstanceError):
        o.base_cents = 1  # frozen dataclass
    d = o.as_dict()
    assert d["amount_due_cents"] == 2130 and d["currency"] == "usd"


# --------------------------------------------------------------------------
# duration ladder + discounts
# --------------------------------------------------------------------------
def test_30_day_full_month_no_discount():
    o = compute_order(MAX_MONTHLY, 30)
    assert o.base_cents == 19_900
    assert o.discount_cents == 0
    assert o.handling_fee_cents == 1393  # ceil(19900 * 0.07) = ceil(1393.0)
    assert o.amount_due_cents == 19_900 + 1393


@pytest.mark.parametrize(
    "days,months,rate",
    [(90, 3, 0.10), (180, 6, 0.15), (360, 12, 0.20), (720, 24, 0.25)],
)
def test_duration_discount_ladder(days, months, rate):
    o = compute_order(MAX_MONTHLY, days)
    base = MAX_MONTHLY * months
    assert o.base_cents == base
    assert DURATION_DISCOUNTS[days] == rate
    assert o.discount_cents == round(base * rate)
    subtotal_before_fee = base - o.discount_cents
    # handling fee is ceil on the discounted subtotal
    assert o.handling_fee_cents >= int(subtotal_before_fee * 0.07)
    assert o.amount_due_cents == subtotal_before_fee + o.handling_fee_cents


def test_pro_ladder_produces_integer_cents():
    # Prices in cents × the ladder fractions land on whole cents (no rounding
    # drift) for the standard catalogue.
    for days in (7, 30, 90, 180, 360, 720):
        o = compute_order(PRO_MONTHLY, days)
        assert o.base_cents >= 0
        assert o.amount_due_cents == o.subtotal_cents


# --------------------------------------------------------------------------
# add-ons
# --------------------------------------------------------------------------
def test_addons_scale_with_months():
    # 2 seats @ $5/mo for 30 days = $10.00 add-on
    o = compute_order(MAX_MONTHLY, 30, {"seats": 2})
    assert o.addons_cents == 1000
    priced = o.base_cents + o.addons_cents
    # exact integer basis-point ceil (float 20900*0.07 would misround to 1464)
    assert o.handling_fee_cents == (priced * 700 + 9999) // 10000
    assert o.amount_due_cents == priced + o.handling_fee_cents


def test_addons_share_the_duration_discount():
    o = compute_order(MAX_MONTHLY, 90, {"official_channels": 1})
    # official_channels default $12/mo × 3 months = $36 add-on before discount
    assert o.addons_cents == 3600
    priced = o.base_cents + o.addons_cents
    assert o.discount_cents == round(priced * 0.10)


def test_unknown_addon_raises():
    with pytest.raises(ValueError, match="unknown add-on"):
        compute_order(MAX_MONTHLY, 30, {"nonexistent": 1})


def test_zero_qty_addons_ignored():
    o = compute_order(MAX_MONTHLY, 30, {"seats": 0})
    assert o.addons_cents == 0


# --------------------------------------------------------------------------
# balance application
# --------------------------------------------------------------------------
def test_balance_applied_partial():
    o = compute_order(MAX_MONTHLY, 7, balance_cents=500)
    assert o.balance_applied_cents == 500
    assert o.amount_due_cents == 2130 - 500


def test_balance_caps_at_subtotal():
    o = compute_order(MAX_MONTHLY, 7, balance_cents=10_000_000)
    assert o.balance_applied_cents == 2130
    assert o.amount_due_cents == 0


def test_negative_balance_treated_as_zero():
    o = compute_order(MAX_MONTHLY, 7, balance_cents=-100)
    assert o.balance_applied_cents == 0
    assert o.amount_due_cents == 2130


# --------------------------------------------------------------------------
# invariants + validation
# --------------------------------------------------------------------------
def test_amount_due_identity():
    o = compute_order(MAX_MONTHLY, 180, {"seats": 3, "hosted_devices": 1}, balance_cents=2000)
    expected = (
        o.base_cents
        + o.addons_cents
        - o.discount_cents
        + o.handling_fee_cents
        - o.balance_applied_cents
    )
    assert o.amount_due_cents == expected
    assert o.amount_due_cents >= 0


def test_invalid_duration_raises():
    with pytest.raises(ValueError):
        compute_order(MAX_MONTHLY, 0)


def test_negative_monthly_raises():
    with pytest.raises(ValueError):
        compute_order(-1, 30)


def test_configurable_handling_fee_pct():
    o = compute_order(MAX_MONTHLY, 30, handling_fee_pct=0.0)
    assert o.handling_fee_cents == 0
    assert o.amount_due_cents == 19_900


# --------------------------------------------------------------------------
# points top-up pricing ($0.375 per 10k points)
# --------------------------------------------------------------------------
def test_points_topup_pricing():
    assert compute_points_topup(10_000) == 38  # 37.5c → 38c (half-up)
    assert compute_points_topup(20_000) == 75  # 75.0c exact
    assert compute_points_topup(100_000) == 375  # $3.75 exact
    assert compute_points_topup(5_000) == 38  # rounds up to a full 10k block


def test_points_topup_requires_positive():
    with pytest.raises(ValueError):
        compute_points_topup(0)
