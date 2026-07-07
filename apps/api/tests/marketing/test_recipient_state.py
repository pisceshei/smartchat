"""Recipient state machine + success rate + message→recipient map parsing."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from apps.api.app.marketing import recipients as rcpt


def test_linear_forward_transitions():
    assert rcpt.can_advance("planned", "queued")
    assert rcpt.can_advance("queued", "sent")
    assert rcpt.can_advance("sent", "delivered")
    assert rcpt.can_advance("delivered", "read")


def test_no_backwards_transitions():
    assert not rcpt.can_advance("sent", "queued")
    assert not rcpt.can_advance("read", "delivered")
    assert not rcpt.can_advance("delivered", "sent")


def test_same_state_is_noop():
    assert not rcpt.can_advance("sent", "sent")


def test_skipped_only_from_planned():
    assert rcpt.can_advance("planned", "skipped")
    assert not rcpt.can_advance("queued", "skipped")
    assert not rcpt.can_advance("sent", "skipped")


def test_failed_from_any_non_terminal():
    assert rcpt.can_advance("planned", "failed")
    assert rcpt.can_advance("queued", "failed")
    assert rcpt.can_advance("sent", "failed")
    assert rcpt.can_advance("delivered", "failed")


def test_failed_not_from_terminal():
    assert not rcpt.can_advance("read", "failed")
    assert not rcpt.can_advance("failed", "failed")
    assert not rcpt.can_advance("skipped", "failed")


def test_success_rate():
    assert rcpt.success_rate(0, 0) == 0.0
    assert rcpt.success_rate(100, 25) == 0.25
    assert rcpt.success_rate(3, 1) == round(1 / 3, 4)


def test_msgmap_roundtrip():
    run = uuid.uuid4()
    rid = uuid.uuid4()
    ws = uuid.uuid4()
    created = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    raw = f"{run}|{rid}|{created.isoformat()}|{ws}"
    parsed = rcpt._parse_msgmap(raw)
    assert parsed == (run, rid, created, ws)


def test_msgmap_bad_input():
    assert rcpt._parse_msgmap("garbage") is None
    assert rcpt._parse_msgmap("a|b|c") is None


def test_status_to_state_map():
    assert rcpt._STATUS_TO_STATE["delivered"] == "delivered"
    assert "sent" in rcpt._STATE_TS_FIELD
