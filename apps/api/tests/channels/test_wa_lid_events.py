"""WhatsApp-App lid event contract: the bridge attaches meta.lid for
LID-addressed senders and NEVER claims "+<lid>" as a phone; the Python side
must parse the meta, reject placeholder phones from an old bridge, and keep
the null-tolerance regression guards. Pure parsing — no DB."""
from __future__ import annotations

import pytest

from apps.api.app.channels.base import parse_normalized_events
from apps.api.app.channels.ingress_pipeline import (
    _clean_hint_phone,
    _is_lid_placeholder_phone,
    _wa_lid_from_event,
)

LID = "56985642876983"
PHONE = "85266577437"


def _envelope(**message_over) -> dict:
    msg = {
        "kind": "message_in",
        "external_message_id": "3EB0A1",
        "external_user_id": PHONE,
        "content": {"blocks": [{"kind": "text", "text": "hi"}]},
        "profile": {"display_name": "浠", "phone": f"+{PHONE}"},
        "media_refs": [],
    }
    msg.update(message_over)
    return {"events": [msg]}


def test_meta_lid_parses_into_message_in():
    [ev] = parse_normalized_events(_envelope(meta={"lid": LID}))
    assert ev.meta == {"lid": LID}
    assert _wa_lid_from_event(ev) == LID


def test_meta_absent_and_meta_null_both_coerce_to_empty():
    [ev] = parse_normalized_events(_envelope())
    assert ev.meta == {}
    assert _wa_lid_from_event(ev) is None
    # Go nil map serializes as null — the null->default validator must hold
    [ev2] = parse_normalized_events(_envelope(meta=None))
    assert ev2.meta == {}


def test_unresolved_lid_event_has_no_phone():
    # what the fixed bridge emits when the phone is unknowable: lid digits as
    # the external id, meta.lid set, and NO profile.phone at all
    [ev] = parse_normalized_events(
        _envelope(
            external_user_id=LID,
            profile={"display_name": "浠"},
            meta={"lid": LID},
        )
    )
    assert ev.external_user_id == LID
    assert ev.profile.phone is None
    assert _wa_lid_from_event(ev) == LID


@pytest.mark.parametrize(
    ("phone", "lid", "expected"),
    [
        (f"+{LID}", LID, True),  # the exact known-bad placeholder
        (f"+{PHONE}", LID, False),  # a real phone is never a placeholder
        (f"+{LID}", None, False),  # no lid known -> cannot classify
        (None, LID, False),
        ("", LID, False),
        (LID, LID, False),  # missing "+" is not the placeholder shape
    ],
)
def test_is_lid_placeholder_phone(phone, lid, expected):
    assert _is_lid_placeholder_phone(phone, lid) is expected


def test_clean_hint_phone_rejects_old_bridge_placeholder():
    # rolling deploy: an OLD bridge still sends phone="+<lid>" together with a
    # new-API understanding of meta.lid — never accept it as a phone
    assert _clean_hint_phone(f"+{LID}", LID) is None
    assert _clean_hint_phone(f"+{PHONE}", LID) == f"+{PHONE}"
    assert _clean_hint_phone(None, LID) is None
    assert _clean_hint_phone("", None) is None


def test_wa_lid_from_event_ignores_non_string_and_blank():
    [ev] = parse_normalized_events(_envelope(meta={"lid": 123}))
    assert _wa_lid_from_event(ev) is None
    [ev2] = parse_normalized_events(_envelope(meta={"lid": "  "}))
    assert _wa_lid_from_event(ev2) is None
