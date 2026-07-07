"""Segment predicate-tree → SQL compiler (nested AND/OR + custom fields)."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from apps.api.app.modules.segments import service as svc

WS = uuid.uuid4()


def _sql(node) -> str:
    return str(svc.compile_definition(WS, node).compile(compile_kwargs={"literal_binds": False}))


def test_empty_definition_matches_all():
    assert svc.compile_definition(WS, {}) is not None
    assert "true" in _sql({}).lower()


def test_leaf_scalar_eq():
    sql = _sql({"field": "country", "op": "eq", "value": "HK"})
    assert "country" in sql


def test_group_and_or_operators():
    tree = {
        "logic": "and",
        "predicates": [
            {"field": "country", "op": "eq", "value": "HK"},
            {"logic": "or", "predicates": [
                {"field": "language", "op": "eq", "value": "en"},
                {"field": "language", "op": "eq", "value": "zh"},
            ]},
        ],
    }
    sql = _sql(tree).upper()
    assert " AND " in sql
    assert " OR " in sql


def test_custom_field_predicate_uses_jsonb():
    sql = _sql({"field": "custom.vip", "op": "eq", "value": "gold"})
    assert "custom" in sql.lower()


def test_custom_numeric_gt():
    sql = _sql({"field": "custom.spend", "op": "gt", "value": 100})
    assert "custom" in sql.lower()


def test_tag_and_channel_subqueries_compile():
    tag = uuid.uuid4()
    sql = _sql({"field": "tag_id", "op": "eq", "value": str(tag)})
    assert "exists" in sql.lower()
    sql2 = _sql({"field": "channel_type", "op": "eq", "value": "widget"})
    assert "exists" in sql2.lower()


def test_bare_list_is_implicit_and():
    sql = _sql([
        {"field": "country", "op": "eq", "value": "HK"},
        {"field": "city", "op": "eq", "value": "Central"},
    ]).upper()
    assert " AND " in sql


def test_unknown_field_rejected():
    with pytest.raises(HTTPException):
        svc.compile_definition(WS, {"field": "not_a_field", "op": "eq", "value": "x"})


def test_depth_guard():
    node: dict = {"field": "country", "op": "eq", "value": "HK"}
    for _ in range(svc.MAX_DEPTH + 2):
        node = {"logic": "and", "predicates": [node]}
    with pytest.raises(svc.SegmentDefinitionError):
        svc.compile_definition(WS, node)


def test_audience_select_excludes_merged():
    q = svc.audience_select(WS, {"field": "country", "op": "eq", "value": "HK"})
    sql = str(q).lower()
    assert "merged_into_id" in sql
    assert "workspace_id" in sql
