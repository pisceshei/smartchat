"""Interpreter pure layer (plan B.1): graph navigation, node-result contract,
on_error policy, answer validation, button/text extraction."""
from __future__ import annotations

from types import SimpleNamespace

from py_contracts.content import MessageContent

from apps.api.app.flows.graph_schema import parse_graph
from apps.flow_engine import interpreter
from apps.flow_engine.context import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    WAITING_STATUSES,
    GraphNav,
    NodeResult,
)


def _graph():
    return parse_graph(
        {
            "schema_version": 1,
            "nodes": [
                {"id": "t", "type": "trigger", "data": {"triggers": [{"type": "visitor_message"}]}},
                {"id": "a", "type": "send_message", "data": {"text": "hi"}},
                {"id": "q", "type": "ask", "data": {"variable": "email"}},
                {"id": "done", "type": "close_conversation", "data": {}},
            ],
            "edges": [
                {"id": "e1", "source": "t", "target": "a", "source_port": "out"},
                {"id": "e2", "source": "a", "target": "q", "source_port": "out"},
                {"id": "e3", "source": "q", "target": "done", "source_port": "answered"},
            ],
        }
    )


# --------------------------------------------------------------------------
# graph navigation
# --------------------------------------------------------------------------
def test_start_node_is_trigger_out_target():
    nav = GraphNav.build(_graph())
    assert nav.start_node_id() == "a"


def test_next_id_follows_ports():
    nav = GraphNav.build(_graph())
    assert nav.next_id("a", "out") == "q"
    assert nav.next_id("q", "answered") == "done"


def test_dead_end_port_returns_none():
    nav = GraphNav.build(_graph())
    assert nav.next_id("q", "timeout") is None  # no edge on that port
    assert nav.next_id("done", "out") is None


def test_start_node_none_when_no_trigger_edge():
    nav = GraphNav.build(
        parse_graph({"schema_version": 1,
                     "nodes": [{"id": "t", "type": "trigger",
                                "data": {"triggers": [{"type": "widget_opened"}]}}],
                     "edges": []})
    )
    assert nav.start_node_id() is None


# --------------------------------------------------------------------------
# status sets (state machine buckets)
# --------------------------------------------------------------------------
def test_status_bucket_membership():
    assert "running" in ACTIVE_STATUSES
    assert "delayed" in ACTIVE_STATUSES
    assert WAITING_STATUSES <= ACTIVE_STATUSES
    assert "completed" in TERMINAL_STATUSES
    assert ACTIVE_STATUSES.isdisjoint(TERMINAL_STATUSES)


# --------------------------------------------------------------------------
# NodeResult contract
# --------------------------------------------------------------------------
def test_node_result_constructors():
    assert NodeResult.next("success").kind == "next"
    assert NodeResult.next("success").port == "success"
    w = NodeResult.wait("waiting_reply", waiting={"type": "ask"})
    assert w.kind == "wait" and w.status == "waiting_reply"
    e = NodeResult.end("completed")
    assert e.kind == "end" and e.end_reason == "completed"


# --------------------------------------------------------------------------
# on_error policy
# --------------------------------------------------------------------------
def test_on_error_skip_default_follows_out():
    node = SimpleNamespace(type="send_message", data={})
    r = interpreter._on_error(node, RuntimeError("boom"))
    assert r.kind == "next" and r.port == "out" and r.step_status == "error"


def test_on_error_external_request_defaults_failed():
    node = SimpleNamespace(type="external_request", data={})
    r = interpreter._on_error(node, RuntimeError("boom"))
    assert r.port == "failed"


def test_on_error_abort_ends_failed():
    node = SimpleNamespace(type="send_message", data={"on_error": "abort"})
    r = interpreter._on_error(node, RuntimeError("boom"))
    assert r.kind == "end" and r.end_reason == "failed"


# --------------------------------------------------------------------------
# answer validation
# --------------------------------------------------------------------------
def test_validate_answer_email():
    assert interpreter._validate_answer("a@b.com", "email")
    assert not interpreter._validate_answer("nope", "email")


def test_validate_answer_phone_number():
    assert interpreter._validate_answer("+852 1234 5678", "phone")
    assert interpreter._validate_answer("42.5", "number")
    assert not interpreter._validate_answer("abc", "number")


def test_validate_answer_none_always_valid():
    assert interpreter._validate_answer("anything", None)
    assert not interpreter._validate_answer("", "email")


# --------------------------------------------------------------------------
# message extraction (button vs free text)
# --------------------------------------------------------------------------
def _msg(content: dict, text=None):
    return SimpleNamespace(content=content, text_plain=text)


def test_extract_button_payload():
    content = MessageContent.model_validate(
        {"blocks": [{"kind": "button_reply", "payload": "opt_a", "text": "Option A"}]}
    ).model_dump(mode="json")
    text, payload = interpreter._extract_message(_msg(content))
    assert payload == "opt_a"


def test_extract_free_text():
    content = MessageContent.model_validate(
        {"blocks": [{"kind": "text", "text": "hello there"}]}
    ).model_dump(mode="json")
    text, payload = interpreter._extract_message(_msg(content, text="hello there"))
    assert payload is None
    assert text == "hello there"


def test_extract_none_message():
    text, payload = interpreter._extract_message(None)
    assert text == "" and payload is None
