"""The 7 conditions (plan B.1): comparison ops, random_split stability,
weighted branches, schedule windows, and end-to-end branch evaluation."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from apps.api.app.flows.graph_schema import Node
from apps.flow_engine import conditions
from apps.flow_engine.context import ExecutionContext


# --------------------------------------------------------------------------
# comparison operators
# --------------------------------------------------------------------------
def test_compare_string_ops():
    assert conditions.compare("eq", "HK", "hk")  # NFKC casefold
    assert conditions.compare("neq", "a", "b")
    assert conditions.compare("contains", "hello world", "world")
    assert not conditions.compare("contains", "hello", "world")
    assert conditions.compare("in", "vip", ["vip", "gold"])
    assert conditions.compare("not_in", "bronze", ["vip", "gold"])


def test_compare_numeric_ops():
    assert conditions.compare("gt", 5, 3)
    assert conditions.compare("gte", 5, 5)
    assert conditions.compare("lt", 2, 3)
    assert not conditions.compare("gt", "notnum", 3)


def test_compare_exists():
    assert conditions.compare("exists", "x", None)
    assert not conditions.compare("exists", "", None)
    assert conditions.compare("not_exists", None, None)


# --------------------------------------------------------------------------
# random split stability
# --------------------------------------------------------------------------
def test_stable_bucket_deterministic():
    a = conditions.stable_bucket("sess-1:node-A")
    b = conditions.stable_bucket("sess-1:node-A")
    assert a == b
    assert 0 <= a < 100


def test_stable_bucket_varies_by_seed():
    seeds = {conditions.stable_bucket(f"s{i}:n") for i in range(50)}
    assert len(seeds) > 1  # not all identical


def test_weighted_branch_partition():
    branches = [{"weight": 50}, {"weight": 50}]
    # bucket 0..49 → branch 0, 50..99 → branch 1
    assert conditions.weighted_branch(0, branches) == 0
    assert conditions.weighted_branch(49, branches) == 0
    assert conditions.weighted_branch(50, branches) == 1
    assert conditions.weighted_branch(99, branches) == 1


def test_weighted_branch_uneven():
    branches = [{"weight": 80}, {"weight": 20}]
    assert conditions.weighted_branch(0, branches) == 0
    assert conditions.weighted_branch(79, branches) == 0
    assert conditions.weighted_branch(80, branches) == 1


def test_weighted_branch_zero_weights_even_split():
    branches = [{}, {}, {}]
    got = {conditions.weighted_branch(b, branches) for b in range(100)}
    assert got == {0, 1, 2}


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------
def test_in_schedule_weekday_window():
    # 2026-07-06 is Monday (weekday 0)
    mon_10 = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
    windows = [{"weekday": 0, "start_min": 9 * 60, "end_min": 18 * 60}]
    assert conditions.in_schedule(mon_10, windows, "UTC")
    assert not conditions.in_schedule(datetime(2026, 7, 6, 8, 0, tzinfo=UTC), windows, "UTC")
    assert not conditions.in_schedule(datetime(2026, 7, 7, 10, 0, tzinfo=UTC), windows, "UTC")  # Tue


def test_in_schedule_empty_always():
    assert conditions.in_schedule(datetime(2026, 7, 6, 3, 0, tzinfo=UTC), [], "UTC")


def test_in_schedule_tz_shift():
    # 02:00 UTC Monday = 10:00 Asia/Hong_Kong Monday
    dt = datetime(2026, 7, 6, 2, 0, tzinfo=UTC)
    windows = [{"weekday": 0, "start_min": 9 * 60, "end_min": 18 * 60}]
    assert conditions.in_schedule(dt, windows, "Asia/Hong_Kong")
    assert not conditions.in_schedule(dt, windows, "UTC")  # 02:00 UTC is outside 9-18


# --------------------------------------------------------------------------
# evaluate() branch selection
# --------------------------------------------------------------------------
def _ctx(contact=None, variables=None):
    fs = SimpleNamespace(id=uuid.uuid4(), variables=variables or {"vars": {}, "trigger": {}, "ext": {}})
    return ExecutionContext(
        session=None, redis=None, flow_session=fs, conversation=None, contact=contact,
        now=datetime(2026, 7, 6, 10, 0, tzinfo=UTC),
    )


def _contact(**kw):
    base = dict(
        id=uuid.uuid4(), display_name="x", remark_name=None, email=None, phone=None,
        language=None, country=None, city=None, timezone=None, device=None, browser=None,
        os=None, is_blacklisted=False, custom={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_evaluate_visitor_language_branch():
    node = Node(id="c1", type="visitor_language", data={"branches": [{"languages": ["en", "fr"]}]})
    assert conditions.evaluate(node, _ctx(_contact(language="en-US"))) == "branch:0"  # primary subtag
    assert conditions.evaluate(node, _ctx(_contact(language="zh"))) == "else"


def test_evaluate_contact_attribute():
    node = Node(id="c2", type="contact_attribute",
                data={"branches": [{"field": "country", "op": "eq", "value": "US"}]})
    assert conditions.evaluate(node, _ctx(_contact(country="US"))) == "branch:0"
    assert conditions.evaluate(node, _ctx(_contact(country="HK"))) == "else"


def test_evaluate_custom_attribute():
    node = Node(id="c3", type="contact_attribute",
                data={"branches": [{"field": "contact.custom.vip", "op": "eq", "value": "yes"}]})
    assert conditions.evaluate(node, _ctx(_contact(custom={"vip": "yes"}))) == "branch:0"


def test_evaluate_random_branch_stable_and_records():
    node = Node(id="r1", type="random_branch", data={"branches": [{"weight": 100}, {"weight": 0}]})
    ctx = _ctx()
    p1 = conditions.evaluate(node, ctx)
    assert p1 == "branch:0"  # weight 100 → always branch 0
    # bucket recorded to vars for explainability
    assert "_split_r1" in ctx.flow_session.variables["vars"]


def test_evaluate_external_variable():
    node = Node(id="e1", type="external_variable",
                data={"branches": [{"path": "ext.req1.status", "op": "eq", "value": "ok"}]})
    ctx = _ctx(variables={"vars": {}, "trigger": {}, "ext": {"req1": {"status": "ok"}}})
    assert conditions.evaluate(node, ctx) == "branch:0"
