"""plan_backfill_actions — the pure planner behind the wa-lid phone backfill.
Classification comes ONLY from the bridge store result + operator input;
digit-length guessing must never happen."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from apps.api.app.backfill_wa_lid_phones import plan_backfill_actions

LID = "56985642876983"
LID2 = "24726848192675"
PHONE = "85266577437"


def _identity(external: str, contact_id=None, meta=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        external_user_id=external,
        contact_id=contact_id or uuid.uuid4(),
        meta=meta or {},
    )


def _contact(cid, phone=None, name="浠"):
    return SimpleNamespace(id=cid, display_name=name, phone=phone, merged_into_id=None)


def test_store_resolved_lid_becomes_migrate():
    ident = _identity(LID)
    contacts = {ident.contact_id: _contact(ident.contact_id, phone=f"+{LID}")}
    plan = plan_backfill_actions(
        [ident], contacts, {LID: {"kind": "lid", "pn": PHONE, "lid": LID}}, {}, set()
    )
    [a] = plan.actions
    assert a.kind == "migrate"
    assert a.new_external_user_id == PHONE
    assert a.new_phone == f"+{PHONE}"
    assert a.wa_lid == LID


def test_resolved_lid_with_existing_phone_identity_becomes_merge():
    lid_ident = _identity(LID)
    phone_ident = _identity(PHONE)
    contacts = {
        lid_ident.contact_id: _contact(lid_ident.contact_id, phone=f"+{LID}"),
        phone_ident.contact_id: _contact(phone_ident.contact_id, phone=f"+{PHONE}"),
    }
    plan = plan_backfill_actions(
        [lid_ident, phone_ident],
        contacts,
        {LID: {"kind": "lid", "pn": PHONE, "lid": LID}, PHONE: {"kind": "pn", "pn": PHONE}},
        {},
        set(),
    )
    merges = plan.by_kind("merge")
    assert len(merges) == 1
    assert merges[0].identity_id == lid_ident.id
    assert merges[0].merge_target_contact_id == phone_ident.contact_id
    # the phone identity itself is a plain annotate
    assert len(plan.by_kind("annotate")) == 1


def test_known_phone_becomes_annotate():
    ident = _identity(PHONE)
    contacts = {ident.contact_id: _contact(ident.contact_id, phone=None)}
    plan = plan_backfill_actions(
        [ident], contacts, {PHONE: {"kind": "pn", "pn": PHONE, "lid": LID}}, {}, set()
    )
    [a] = plan.actions
    assert a.kind == "annotate"
    assert a.wa_lid == LID
    assert a.new_phone == f"+{PHONE}"  # fills the empty phone


def test_unknown_with_map_becomes_migrate_with_mapped_phone():
    ident = _identity(LID)
    contacts = {ident.contact_id: _contact(ident.contact_id, phone=f"+{LID}")}
    plan = plan_backfill_actions(
        [ident], contacts, {LID: {"kind": "unknown"}}, {LID: f"+{PHONE}"}, set()
    )
    [a] = plan.actions
    assert a.kind == "migrate"
    assert a.new_external_user_id == PHONE
    assert a.new_phone == f"+{PHONE}"
    assert "--map" in a.note


def test_unknown_with_clear_phone_becomes_clear():
    ident = _identity(LID2)
    contacts = {ident.contact_id: _contact(ident.contact_id, phone=f"+{LID2}", name="Ken")}
    plan = plan_backfill_actions(
        [ident], contacts, {LID2: {"kind": "unknown"}}, {}, {LID2}
    )
    [a] = plan.actions
    assert a.kind == "clear_phone"
    assert a.wa_lid == LID2


def test_bare_unknown_is_report_only_never_guessed():
    ident = _identity(LID)  # 14 digits — looks lid-ish, but NO store/operator input
    contacts = {ident.contact_id: _contact(ident.contact_id, phone=f"+{LID}")}
    plan = plan_backfill_actions([ident], contacts, {}, {}, set())
    [a] = plan.actions
    assert a.kind == "report"


def test_empty_results_with_no_input_reports_everything():
    idents = [_identity(LID), _identity(PHONE)]
    contacts = {i.contact_id: _contact(i.contact_id) for i in idents}
    plan = plan_backfill_actions(idents, contacts, {}, {}, set())
    assert {a.kind for a in plan.actions} == {"report"}


def test_two_migrates_to_same_phone_second_downgrades_to_report():
    # two lids resolving/mapped to ONE phone would violate the identity unique
    # key on apply — the second must surface as report, never crash the txn
    a, b = _identity(LID), _identity(LID2)
    contacts = {
        a.contact_id: _contact(a.contact_id, phone=f"+{LID}"),
        b.contact_id: _contact(b.contact_id, phone=f"+{LID2}", name="Ken"),
    }
    plan = plan_backfill_actions(
        [a, b],
        contacts,
        {
            LID: {"kind": "lid", "pn": PHONE, "lid": LID},
            LID2: {"kind": "lid", "pn": PHONE, "lid": LID2},  # same pn!
        },
        {},
        set(),
    )
    kinds = [x.kind for x in plan.actions]
    assert kinds == ["migrate", "report"]
    assert "resolve manually" in plan.actions[1].note
