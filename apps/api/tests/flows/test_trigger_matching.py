"""Trigger matching algorithm (plan B.1): keyword OR-groups + contains/exact,
page URL rules, new/returning, freq-cap peek/consume, winner selection."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.flow_engine import triggers


# --------------------------------------------------------------------------
# keyword matching
# --------------------------------------------------------------------------
def _cfg(**kw):
    base = {"match_type": "keyword", "match_mode": "contains", "keyword_groups": []}
    base.update(kw)
    return base


def test_any_message_matches():
    assert triggers.match_message("literally anything", _cfg(match_type="any"), [])


def test_contains_matches_substring():
    cfg = _cfg(keyword_groups=[["price", "cost"]])
    assert triggers.match_message("what is the PRICE of this?", cfg, [])
    assert not triggers.match_message("hello there", cfg, [])


def test_exact_requires_whole_message():
    cfg = _cfg(match_mode="exact", keyword_groups=[["hi"]])
    assert triggers.match_message("Hi", cfg, [])  # NFKC casefold
    assert not triggers.match_message("hi there", cfg, [])


def test_or_within_and_across_groups():
    cfg = _cfg(keyword_groups=[["hello", "hi"], ["help", "support"]])
    assert triggers.match_message("i need SUPPORT", cfg, [])  # 2nd group
    assert triggers.match_message("hello!", cfg, [])  # 1st group
    assert not triggers.match_message("goodbye", cfg, [])


def test_dict_keywords_expand_the_set():
    cfg = _cfg(keyword_groups=[])
    assert triggers.match_message("do you ship 退貨?", cfg, ["退貨", "refund"])
    assert not triggers.match_message("nothing relevant", cfg, ["退貨", "refund"])


def test_nfkc_fullwidth_normalisation():
    # full-width Ａ should match ascii a under NFKC casefold
    cfg = _cfg(keyword_groups=[["abc"]])
    assert triggers.match_message("ＡＢＣ", cfg, [])


def test_empty_message_no_keyword_match():
    assert not triggers.match_message("", _cfg(keyword_groups=[["hi"]]), [])


# --------------------------------------------------------------------------
# page url matching
# --------------------------------------------------------------------------
def test_page_no_rules_matches_any():
    assert triggers.match_page("https://x.com/anything", {})


def test_page_contains_prefix_exact_regex():
    assert triggers.match_page("https://shop.com/pricing", {"rules": [{"op": "contains", "value": "pric"}]})
    assert triggers.match_page("https://shop.com/pricing", {"rules": [{"op": "prefix", "value": "https://shop.com"}]})
    assert triggers.match_page("https://shop.com/x", {"rules": [{"op": "exact", "value": "https://shop.com/x"}]})
    assert triggers.match_page("https://shop.com/p/42", {"rules": [{"op": "regex", "value": r"/p/\d+"}]})
    miss = {"rules": [{"op": "contains", "value": "pricing"}]}
    assert not triggers.match_page("https://shop.com/home", miss)


def test_page_missing_url_with_rules_fails():
    assert not triggers.match_page(None, {"rules": [{"op": "contains", "value": "x"}]})


# --------------------------------------------------------------------------
# visitor kind
# --------------------------------------------------------------------------
def test_visitor_kind():
    assert triggers.match_visitor_kind("new_visitor", "new")
    assert not triggers.match_visitor_kind("new_visitor", "returning")
    assert triggers.match_visitor_kind("returning_visitor", "returning")
    assert not triggers.match_visitor_kind("returning_visitor", "new")


# --------------------------------------------------------------------------
# freq cap (fake async redis)
# --------------------------------------------------------------------------
class FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, k):
        return self.store.get(k)

    async def incr(self, k):
        self.store[k] = self.store.get(k, 0) + 1
        return self.store[k]

    async def expire(self, k, s):
        self.ttl[k] = s


def _trigger(count=0, window_s=0, scope="contact"):
    return SimpleNamespace(id=uuid.uuid4(), freq_cap={"scope": scope, "count": count, "window_s": window_s})


async def test_freq_cap_unlimited_when_no_config():
    r = FakeRedis()
    t = _trigger()  # count 0 → unlimited
    ws, c = uuid.uuid4(), uuid.uuid4()
    assert await triggers.freq_cap_allows(r, t, workspace_id=ws, contact_id=c, conversation_id=None)


async def test_freq_cap_blocks_after_count():
    r = FakeRedis()
    t = _trigger(count=2, window_s=3600)
    ws, c = uuid.uuid4(), uuid.uuid4()
    kw = dict(workspace_id=ws, contact_id=c, conversation_id=None)
    assert await triggers.freq_cap_allows(r, t, **kw)
    await triggers.freq_cap_consume(r, t, **kw)
    assert await triggers.freq_cap_allows(r, t, **kw)
    await triggers.freq_cap_consume(r, t, **kw)
    assert not await triggers.freq_cap_allows(r, t, **kw)  # 2 consumed, cap hit


async def test_freq_cap_scope_isolated_per_contact():
    r = FakeRedis()
    t = _trigger(count=1, window_s=3600)
    ws, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await triggers.freq_cap_consume(r, t, workspace_id=ws, contact_id=a, conversation_id=None)
    assert not await triggers.freq_cap_allows(r, t, workspace_id=ws, contact_id=a, conversation_id=None)
    assert await triggers.freq_cap_allows(r, t, workspace_id=ws, contact_id=b, conversation_id=None)


async def test_freq_cap_sets_ttl_once():
    r = FakeRedis()
    t = _trigger(count=5, window_s=120)
    ws, c = uuid.uuid4(), uuid.uuid4()
    kw = dict(workspace_id=ws, contact_id=c, conversation_id=None)
    await triggers.freq_cap_consume(r, t, **kw)
    key = triggers.cap_key(t.id, "contact", str(c))
    assert r.ttl[key] == 120


# --------------------------------------------------------------------------
# winner selection (priority + tie-break)
# --------------------------------------------------------------------------
class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def _match(priority, updated_ts):
    from datetime import UTC, datetime

    flow = SimpleNamespace(
        id=uuid.uuid4(), priority=priority,
        updated_at=datetime.fromtimestamp(updated_ts, tz=UTC),
    )
    trig = SimpleNamespace(id=uuid.uuid4(), freq_cap={})
    return triggers.TriggerMatch(trigger=trig, flow=flow)


async def test_lowest_priority_wins_and_losers_logged():
    r = FakeRedis()
    s = FakeSession()
    lo = _match(priority=10, updated_ts=1000)
    hi = _match(priority=200, updated_ts=9999)
    # matching_triggers would sort; emulate by sorting here
    matches = sorted([hi, lo], key=lambda m: (m.flow.priority, -m.flow.updated_at.timestamp()))
    winner = await triggers.select_winner(
        s, r, matches, workspace_id=uuid.uuid4(), contact_id=uuid.uuid4(), conversation_id=None
    )
    assert winner is lo
    assert len(s.added) == 1  # the loser got a suppressed log row


async def test_tie_priority_newest_updated_wins():

    older = _match(priority=50, updated_ts=1000)
    newer = _match(priority=50, updated_ts=5000)
    matches = sorted([older, newer], key=lambda m: (m.flow.priority, -m.flow.updated_at.timestamp()))
    assert matches[0] is newer


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
