"""ONE-ID merge/unmerge snapshot logic (plan A.4) — pure planners exercised
with fake contact/merge objects (no DB session)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.api.app.modules.contacts.service import (
    MOVED_KEY,
    MergeError,
    NotUndoableError,
    apply_field_overwrites,
    compute_field_overwrites,
    match_types,
    ordered_pair,
    plan_merge,
    plan_unmerge,
    revert_field_overwrites,
)

WS = uuid.uuid4()


def _contact(**kw) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        workspace_id=WS,
        display_name="",
        remark_name=None,
        avatar_url=None,
        email=None,
        phone=None,
        language=None,
        country=None,
        city=None,
        timezone=None,
        last_ip=None,
        device=None,
        browser=None,
        os=None,
        custom={},
        is_blacklisted=False,
        merged_into_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _plan(target, source, **kw):
    defaults = dict(
        source_identity_ids=[],
        source_conversation_ids=[],
        source_tag_ids=[],
        target_tag_ids=[],
        source_note_ids=[],
        source_order_ids=[],
        pair_candidate_ids=[],
    )
    defaults.update(kw)
    return plan_merge(target, source, **defaults)


# --------------------------------------------------------------------------
# field overwrites: fill-missing semantics
# --------------------------------------------------------------------------
def test_fill_missing_only_empty_target_fields():
    target = _contact(display_name="Amy", email=None, phone="")
    source = _contact(display_name="A. Wong", email="a@x.com", phone="+85291234567")
    ov = compute_field_overwrites(target, source)
    assert "display_name" not in ov  # target keeps its own value
    assert ov["email"] == {"old": None, "new": "a@x.com"}
    assert ov["phone"] == {"old": "", "new": "+85291234567"}


def test_custom_keys_merge_missing_only():
    target = _contact(custom={"vip": "yes", "empty": ""})
    source = _contact(custom={"vip": "no", "order_no": "A1", "empty": "filled"})
    ov = compute_field_overwrites(target, source)
    assert "custom.vip" not in ov
    assert ov["custom.order_no"]["new"] == "A1"
    assert ov["custom.empty"]["new"] == "filled"


def test_blacklist_is_union():
    ov = compute_field_overwrites(_contact(), _contact(is_blacklisted=True))
    assert ov["is_blacklisted"] == {"old": False, "new": True}
    assert "is_blacklisted" not in compute_field_overwrites(
        _contact(is_blacklisted=True), _contact()
    )


def test_apply_then_revert_roundtrip():
    target = _contact(display_name="Amy", custom={"a": "1"})
    source = _contact(email="a@x.com", phone="+852", custom={"b": "2"}, is_blacklisted=True)
    original = dict(vars(target))
    original_custom = dict(target.custom)
    ov = compute_field_overwrites(target, source)
    apply_field_overwrites(target, ov)
    assert target.email == "a@x.com" and target.custom["b"] == "2" and target.is_blacklisted
    revert_field_overwrites(target, ov)
    assert target.email is None
    assert target.phone is None
    assert target.is_blacklisted is False
    assert target.custom == original_custom  # added custom key removed again
    for f in ("display_name", "remark_name", "language"):
        assert getattr(target, f) == original[f]


# --------------------------------------------------------------------------
# merge planning
# --------------------------------------------------------------------------
def test_plan_merge_moves_everything_and_snapshots():
    target, source = _contact(), _contact(email="s@x.com")
    idents = [uuid.uuid4(), uuid.uuid4()]
    convs = [uuid.uuid4()]
    notes = [uuid.uuid4()]
    orders = [uuid.uuid4()]
    cands = [uuid.uuid4()]
    plan = _plan(
        target, source,
        source_identity_ids=idents, source_conversation_ids=convs,
        source_note_ids=notes, source_order_ids=orders, pair_candidate_ids=cands,
    )
    assert plan.moved_identity_ids == idents
    assert plan.moved_conversation_ids == convs
    assert plan.field_overwrites["email"]["new"] == "s@x.com"
    snap = plan.snapshot_overwrites()
    assert snap[MOVED_KEY]["notes"] == [str(notes[0])]
    assert snap[MOVED_KEY]["orders"] == [str(orders[0])]
    assert snap[MOVED_KEY]["candidates"] == [str(cands[0])]


def test_plan_merge_tag_dedupe():
    shared, only_source = uuid.uuid4(), uuid.uuid4()
    plan = _plan(
        _contact(), _contact(),
        source_tag_ids=[shared, only_source], target_tag_ids=[shared],
    )
    assert plan.moved_tag_ids == [only_source]


def test_plan_merge_rejects_self_merge():
    c = _contact()
    with pytest.raises(MergeError):
        _plan(c, c)


def test_plan_merge_rejects_cross_workspace():
    with pytest.raises(MergeError):
        _plan(_contact(), _contact(workspace_id=uuid.uuid4()))


def test_plan_merge_rejects_already_merged_endpoints():
    with pytest.raises(MergeError):
        _plan(_contact(), _contact(merged_into_id=uuid.uuid4()))
    with pytest.raises(MergeError):
        _plan(_contact(merged_into_id=uuid.uuid4()), _contact())


# --------------------------------------------------------------------------
# unmerge planning (exact replay, newest-only)
# --------------------------------------------------------------------------
def _merge_row(**kw) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        target_contact_id=uuid.uuid4(),
        source_contact_id=uuid.uuid4(),
        moved_identity_ids=[str(uuid.uuid4())],
        moved_conversation_ids=[str(uuid.uuid4())],
        field_overwrites={
            "email": {"old": None, "new": "s@x.com"},
            MOVED_KEY: {
                "tags": [str(uuid.uuid4())],
                "notes": [],
                "orders": [str(uuid.uuid4())],
                "candidates": [],
            },
        },
        undone_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_plan_unmerge_replays_snapshot():
    m = _merge_row()
    plan = plan_unmerge(m, is_latest_for_target=True, target_merged_away=False)
    assert plan.identity_ids == [uuid.UUID(m.moved_identity_ids[0])]
    assert plan.conversation_ids == [uuid.UUID(m.moved_conversation_ids[0])]
    assert plan.tag_ids == [uuid.UUID(m.field_overwrites[MOVED_KEY]["tags"][0])]
    assert plan.order_ids == [uuid.UUID(m.field_overwrites[MOVED_KEY]["orders"][0])]
    assert plan.field_overwrites["email"]["old"] is None


def test_plan_unmerge_rejects_already_undone():
    from datetime import UTC, datetime

    m = _merge_row(undone_at=datetime.now(UTC))
    with pytest.raises(NotUndoableError):
        plan_unmerge(m, is_latest_for_target=True, target_merged_away=False)


def test_plan_unmerge_only_newest_in_chain():
    with pytest.raises(NotUndoableError):
        plan_unmerge(_merge_row(), is_latest_for_target=False, target_merged_away=False)


def test_plan_unmerge_rejects_when_target_merged_away():
    with pytest.raises(NotUndoableError):
        plan_unmerge(_merge_row(), is_latest_for_target=True, target_merged_away=True)


# --------------------------------------------------------------------------
# 重複聯絡人 matchers + pair ordering
# --------------------------------------------------------------------------
def test_match_types_phone_email_loggedin():
    a = _contact(phone="+85291234567", email="Same@X.com")
    b = _contact(phone="+85291234567", email="same@x.com")
    got = match_types(a, b, logged_in_a={"u1"}, logged_in_b={"u1", "u2"})
    assert got == ["phone", "email", "logged_in_id"]


def test_match_types_empty_values_never_match():
    assert match_types(_contact(phone=""), _contact(phone="")) == []
    assert match_types(_contact(), _contact()) == []
    assert match_types(_contact(), _contact(), logged_in_a=set(), logged_in_b={"x"}) == []


def test_ordered_pair_is_canonical():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert ordered_pair(a, b) == ordered_pair(b, a)
    lo, hi = ordered_pair(a, b)
    assert str(lo) < str(hi)
