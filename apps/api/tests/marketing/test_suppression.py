"""Suppression predicates (blacklist / unsubscribe / opt-out flag)."""
from __future__ import annotations

import types

from apps.api.app.marketing import suppression as supp


def _contact(**kw):
    base = {"is_blacklisted": False, "custom": {}}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_blacklist_reason():
    assert supp.contact_suppression_reason(_contact(is_blacklisted=True)) == supp.SKIP_BLACKLIST


def test_unsubscribe_reason_marketing_opt_out():
    c = _contact(custom={"marketing_opt_out": True})
    assert supp.contact_suppression_reason(c) == supp.SKIP_UNSUBSCRIBED


def test_unsubscribe_reason_legacy_key():
    c = _contact(custom={"unsubscribed": 1})
    assert supp.is_unsubscribed(c) is True


def test_clean_contact_passes():
    assert supp.contact_suppression_reason(_contact()) is None


def test_blacklist_precedes_unsubscribe():
    c = _contact(is_blacklisted=True, custom={"marketing_opt_out": True})
    assert supp.contact_suppression_reason(c) == supp.SKIP_BLACKLIST


def test_mark_unsubscribed_sets_flag():
    c = _contact()
    supp.mark_unsubscribed(c)
    assert c.custom["marketing_opt_out"] is True


def test_skip_reason_constants_fit_column():
    # skip_reason column is VARCHAR(24)
    for name, val in vars(supp).items():
        if name.startswith("SKIP_"):
            assert len(val) <= 24
