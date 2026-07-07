"""Pure billing-service helpers: price conversion, order-response mapping,
add-on cleaning, subscription add-on override math. No DB, no Stripe."""
from __future__ import annotations

import pytest

from apps.api.app.billing.subscription import compute_overrides
from apps.api.app.models.tenancy import Plan
from apps.api.app.modules.billing import service
from apps.api.app.services.stripe_client import compute_order


# --------------------------------------------------------------------------
# plan price → cents
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "usd,cents",
    [(0, 0), (15.9, 1590), (199, 19900), ("15.90", 1590), ("199.00", 19900)],
)
def test_plan_monthly_cents(usd, cents):
    assert service.plan_monthly_cents(Plan(code="x", name="x", price_usd_month=usd)) == cents


def test_plan_monthly_cents_none_for_priceless_plan():
    assert service.plan_monthly_cents(Plan(code="custom", name="c", price_usd_month=None)) is None


# --------------------------------------------------------------------------
# order-response contract shape
# --------------------------------------------------------------------------
def test_order_breakdown_dict_matches_contract_keys():
    import uuid

    o = compute_order(19_900, 7)  # observed Max 7-day example
    oid = uuid.uuid4()
    d = service.order_breakdown_dict(o, oid)
    assert d["order_id"] == str(oid)
    assert d["base_price"] == 1990
    assert d["handling_fee"] == 140
    assert d["amount_due"] == 2130
    assert d["balance_applied"] == 0
    assert d["discount"] == 0
    assert d["currency"] == "usd"
    # CONTRACT-required keys all present
    for k in ("base_price", "discount", "handling_fee", "balance_applied", "amount_due", "currency"):
        assert k in d


# --------------------------------------------------------------------------
# add-on cleaning
# --------------------------------------------------------------------------
def test_clean_addons_drops_zero_and_unknown():
    assert service._clean_addons({"seats": 2, "official_channels": 0, "bogus": 5}) == {"seats": 2}
    assert service._clean_addons(None) == {}
    assert service._clean_addons({}) == {}


def test_valid_durations_are_the_ladder():
    assert service.VALID_DURATIONS == (7, 30, 90, 180, 360, 720)


# --------------------------------------------------------------------------
# subscription add-on override expansion
# --------------------------------------------------------------------------
def test_compute_overrides_expands_numeric_caps():
    plan = {"seats": 10, "official_channels": 5, "hosted_devices": 3}
    ov = compute_overrides(plan, {"seats": 2, "official_channels": 1})
    assert ov["seats"] == 12
    assert ov["official_channels"] == 6
    assert "hosted_devices" not in ov  # no add-on purchased
    assert ov["_addons"] == {"seats": 2, "official_channels": 1}


def test_compute_overrides_unlimited_base_stays_unbounded():
    ov = compute_overrides({"seats": -1}, {"seats": 5})
    assert "seats" not in ov  # -1 unlimited → no numeric override
    assert ov["_addons"] == {"seats": 5}


def test_compute_overrides_empty_when_no_addons():
    assert compute_overrides({"seats": 10}, {}) == {}
    assert compute_overrides({"seats": 10}, {"seats": 0}) == {}
    assert compute_overrides({"seats": 10}, None) == {}
