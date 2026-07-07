"""Unit tests for visitor tokens, typing throttle, presence key parsing and
the pure unread tab-total fold. No Redis/DB."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from apps.api.app.realtime.presence import member_key, parse_presence_key, visitor_key
from apps.api.app.realtime.protocol import (
    Throttle,
    VisitorTokenInvalid,
    mint_visitor_token,
    verify_visitor_token,
)
from apps.api.app.realtime.unread import tab_totals, unread_key
from apps.api.app.services.security import issue_token

WS = uuid.UUID("33333333-3333-7333-8333-333333333333")
IDENTITY = uuid.UUID("77777777-7777-7777-8777-777777777777")
CONV = uuid.UUID("66666666-6666-7666-8666-666666666666")


# --------------------------------------------------------------------------
# visitor tokens
# --------------------------------------------------------------------------
def test_visitor_token_roundtrip():
    token = mint_visitor_token(WS, IDENTITY, conversation_id=CONV)
    scope = verify_visitor_token(token)
    assert scope.workspace_id == WS
    assert scope.channel_identity_id == IDENTITY
    assert scope.conversation_id == CONV


def test_visitor_token_without_conversation():
    scope = verify_visitor_token(mint_visitor_token(WS, IDENTITY))
    assert scope.conversation_id is None


def test_expired_visitor_token_rejected():
    token = mint_visitor_token(WS, IDENTITY, ttl=timedelta(seconds=-5))
    with pytest.raises(VisitorTokenInvalid):
        verify_visitor_token(token)


def test_agent_access_token_is_not_a_visitor_token():
    # same signing key, wrong typ claim — must be rejected on /ws/widget
    agent_token = issue_token(uuid.uuid4(), token_type="access")
    with pytest.raises(VisitorTokenInvalid):
        verify_visitor_token(agent_token)


def test_garbage_token_rejected():
    with pytest.raises(VisitorTokenInvalid):
        verify_visitor_token("not.a.jwt")


# --------------------------------------------------------------------------
# typing throttle (1 per 3s)
# --------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_throttle_allows_then_blocks_then_allows():
    clock = FakeClock()
    gate = Throttle(interval=3.0, clock=clock)
    assert gate.allow("c1") is True
    clock.t = 1.0
    assert gate.allow("c1") is False
    clock.t = 2.999
    assert gate.allow("c1") is False
    clock.t = 3.0
    assert gate.allow("c1") is True


def test_throttle_is_per_key():
    clock = FakeClock()
    gate = Throttle(interval=3.0, clock=clock)
    assert gate.allow("c1") is True
    assert gate.allow("c2") is True  # different conversation, independent gate
    assert gate.allow("c1") is False


# --------------------------------------------------------------------------
# presence key layout + expiry parsing
# --------------------------------------------------------------------------
def test_presence_key_roundtrip():
    m = member_key(WS, IDENTITY)
    v = visitor_key(WS, IDENTITY)
    assert parse_presence_key(m) == ("member", str(WS), str(IDENTITY))
    assert parse_presence_key(v) == ("visitor", str(WS), str(IDENTITY))


@pytest.mark.parametrize(
    "key",
    [
        "unread:x:y",  # foreign prefix
        "presence:z:a:b",  # unknown kind
        "presence:m:onlypart",  # missing segment
        f"seq:{WS}",
        "",
    ],
)
def test_parse_presence_key_ignores_foreign_keys(key):
    assert parse_presence_key(key) is None


# --------------------------------------------------------------------------
# unread pure math
# --------------------------------------------------------------------------
def test_unread_key_layout():
    assert unread_key(WS, IDENTITY) == f"unread:{WS}:{IDENTITY}"


def test_tab_totals_fold():
    unread = {"c1": 2, "c2": 1, "c3": 5}
    tabs = {"c1": "mine", "c2": "mine", "c3": "unassigned"}
    totals = tab_totals(unread, tabs)
    assert totals == {"mine": 3, "unassigned": 5, "_total": 8}


def test_tab_totals_unclassified_goes_to_default():
    totals = tab_totals({"c1": 4}, {}, default_tab="all")
    assert totals == {"all": 4, "_total": 4}


def test_tab_totals_empty():
    assert tab_totals({}, {}) == {"_total": 0}
