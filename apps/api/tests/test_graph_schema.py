"""Flow graph schema: ports, validation, trigger extraction (plan B.1)."""
from __future__ import annotations

from apps.api.app.flows import graph_schema as gs


def _trigger_node(triggers=None):
    return gs.Node(
        id="t",
        type="trigger",
        data={"triggers": triggers or [{"type": "new_visitor", "config": {}, "freq_cap": {}}]},
    )


def _valid_graph() -> gs.Graph:
    return gs.Graph(
        nodes=[
            _trigger_node(),
            gs.Node(id="m", type="send_message",
                    data={"blocks": [{"kind": "text", "text": "hi {{ contact.display_name }}"}]}),
            gs.Node(id="cond", type="visitor_language", data={"branches": [{"lang": "en"}, {"lang": "zh"}]}),
            gs.Node(id="btn", type="quick_buttons",
                    data={"text": "pick", "buttons": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}]}),
            gs.Node(id="req", type="external_request",
                    data={"url": "https://api.example.com/x", "method": "GET"}),
            gs.Node(id="end", type="close_conversation", data={}),
        ],
        edges=[
            gs.Edge(id="e1", source="t", target="m"),
            gs.Edge(id="e2", source="m", target="cond"),
            gs.Edge(id="e3", source="cond", source_port="branch:0", target="btn"),
            gs.Edge(id="e4", source="cond", source_port="branch:1", target="btn"),
            gs.Edge(id="e5", source="cond", source_port="else", target="req"),
            gs.Edge(id="e6", source="btn", source_port="button:a", target="m"),
            gs.Edge(id="e7", source="btn", source_port="timeout", target="end"),
            gs.Edge(id="e8", source="req", source_port="success", target="end"),
            gs.Edge(id="e9", source="req", source_port="failed", target="end"),
        ],
    )


# --------------------------------------------------------------------------
# catalogs
# --------------------------------------------------------------------------
def test_catalog_counts():
    assert len(gs.TRIGGER_TYPES) == 9
    assert len(gs.CONDITION_TYPES) == 7
    assert len(gs.ACTION_TYPES) == 15


# --------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------
def test_ports_trigger_and_action():
    assert gs.output_ports(gs.Node(id="t", type="trigger", data={})) == ["out"]
    assert gs.output_ports(gs.Node(id="a", type="add_contact_tag", data={})) == ["out"]


def test_ports_ask():
    assert gs.output_ports(gs.Node(id="q", type="ask", data={})) == ["answered", "timeout", "invalid"]


def test_ports_external_request():
    assert gs.output_ports(gs.Node(id="r", type="external_request", data={})) == ["success", "failed"]


def test_ports_quick_buttons():
    n = gs.Node(id="b", type="quick_buttons",
                data={"buttons": [{"id": "yes", "text": "Y"}, {"id": "no", "text": "N"}]})
    assert gs.output_ports(n) == ["button:yes", "button:no", "timeout", "typed_reply"]


def test_ports_condition_branches_plus_else():
    n = gs.Node(id="c", type="country", data={"branches": [{"c": "HK"}, {"c": "US"}]})
    assert gs.output_ports(n) == ["branch:0", "branch:1", "else"]


# --------------------------------------------------------------------------
# valid path
# --------------------------------------------------------------------------
def test_valid_graph_has_no_errors():
    assert gs.validate_graph(_valid_graph(), channel_type="widget") == []


def test_extract_triggers():
    g = gs.Graph(nodes=[_trigger_node([
        {"type": "new_visitor", "config": {}, "freq_cap": {}},
        {"type": "visitor_message", "config": {"match": "keyword", "keywords": ["hi"]},
         "freq_cap": {"count": 1, "window_s": 60}},
    ])], edges=[])
    triggers = gs.extract_triggers(g)
    assert [t.trigger_type for t in triggers] == ["new_visitor", "visitor_message"]
    assert triggers[1].config["keywords"] == ["hi"]
    assert triggers[1].freq_cap["window_s"] == 60
    assert all(t.node_id == "t" for t in triggers)


def test_extract_triggers_skips_invalid_type():
    g = gs.Graph(nodes=[_trigger_node([
        {"type": "new_visitor"}, {"type": "bogus"}])], edges=[])
    assert [t.trigger_type for t in gs.extract_triggers(g)] == ["new_visitor"]


def test_is_terminal():
    assert gs.is_terminal(gs.Node(id="x", type="close_conversation", data={}))
    assert not gs.is_terminal(gs.Node(id="y", type="send_message", data={}))


# --------------------------------------------------------------------------
# invalid graphs
# --------------------------------------------------------------------------
def test_no_trigger_node():
    g = gs.Graph(nodes=[gs.Node(id="m", type="send_message", data={})], edges=[])
    assert any("no trigger node" in e for e in gs.validate_graph(g))


def test_multiple_trigger_nodes():
    g = gs.Graph(nodes=[_trigger_node(), gs.Node(id="t2", type="trigger",
                 data={"triggers": [{"type": "new_visitor"}]})], edges=[])
    assert any("exactly one trigger" in e for e in gs.validate_graph(g))


def test_trigger_node_without_defs():
    g = gs.Graph(nodes=[gs.Node(id="t", type="trigger", data={"triggers": []})], edges=[])
    assert any("no trigger definitions" in e for e in gs.validate_graph(g))


def test_unknown_node_type():
    g = gs.Graph(nodes=[_trigger_node(), gs.Node(id="x", type="frobnicate", data={})], edges=[])
    assert any("unknown node type" in e for e in gs.validate_graph(g))


def test_edge_to_missing_node():
    g = gs.Graph(nodes=[_trigger_node()],
                 edges=[gs.Edge(id="e", source="t", target="ghost")])
    assert any("missing node ghost" in e for e in gs.validate_graph(g))


def test_edge_invalid_port():
    g = gs.Graph(
        nodes=[_trigger_node(), gs.Node(id="m", type="send_message", data={})],
        edges=[gs.Edge(id="e", source="t", source_port="banana", target="m")],
    )
    assert any("not valid for" in e for e in gs.validate_graph(g))


def test_duplicate_outgoing_port():
    g = gs.Graph(
        nodes=[_trigger_node(),
               gs.Node(id="m", type="send_message", data={}),
               gs.Node(id="n", type="add_contact_tag", data={})],
        edges=[gs.Edge(id="e1", source="t", source_port="out", target="m"),
               gs.Edge(id="e2", source="t", source_port="out", target="n")],
    )
    assert any("duplicate outgoing edge" in e for e in gs.validate_graph(g))


def test_unresolvable_variable_namespace():
    g = gs.Graph(
        nodes=[_trigger_node(),
               gs.Node(id="m", type="send_message",
                       data={"blocks": [{"kind": "text", "text": "hi {{ bogus.field }}"}]})],
        edges=[gs.Edge(id="e", source="t", target="m")],
    )
    assert any("unknown namespace" in e for e in gs.validate_graph(g))


def test_ext_reference_to_non_external_request():
    g = gs.Graph(
        nodes=[_trigger_node(),
               gs.Node(id="m", type="send_message",
                       data={"blocks": [{"kind": "text", "text": "{{ ext.m.value }}"}]})],
        edges=[gs.Edge(id="e", source="t", target="m")],
    )
    assert any("does not reference an external_request" in e for e in gs.validate_graph(g))


def test_ext_reference_valid_when_node_is_external_request():
    g = gs.Graph(
        nodes=[_trigger_node(),
               gs.Node(id="req", type="external_request", data={"url": "https://x"}),
               gs.Node(id="m", type="send_message",
                       data={"blocks": [{"kind": "text", "text": "{{ ext.req.data.name }}"}]})],
        edges=[gs.Edge(id="e1", source="t", target="req"),
               gs.Edge(id="e2", source="req", source_port="success", target="m")],
    )
    assert gs.validate_graph(g) == []


def test_condition_without_branches():
    g = gs.Graph(
        nodes=[_trigger_node(), gs.Node(id="c", type="country", data={})],
        edges=[gs.Edge(id="e", source="t", target="c")],
    )
    assert any("has no branches" in e for e in gs.validate_graph(g))


def test_random_branch_needs_two():
    g = gs.Graph(
        nodes=[_trigger_node(), gs.Node(id="c", type="random_branch", data={"branches": [{"w": 100}]})],
        edges=[gs.Edge(id="e", source="t", target="c")],
    )
    assert any("random_branch" in e for e in gs.validate_graph(g))


# --------------------------------------------------------------------------
# channel capability
# --------------------------------------------------------------------------
def test_template_blocked_on_widget():
    g = gs.Graph(
        nodes=[_trigger_node(),
               gs.Node(id="m", type="send_message",
                       data={"blocks": [{"kind": "template", "template_name": "welcome"}]})],
        edges=[gs.Edge(id="e", source="t", target="m")],
    )
    assert any("template" in e for e in gs.validate_graph(g, channel_type="widget"))
    # allowed on whatsapp_cloud
    assert gs.validate_graph(g, channel_type="whatsapp_cloud") == []


def test_quick_buttons_degrade_not_error_on_email():
    # buttons degrade (numbered menu), so no hard error even where unsupported
    g = gs.Graph(
        nodes=[_trigger_node(),
               gs.Node(id="b", type="quick_buttons",
                       data={"text": "pick", "buttons": [{"id": "a", "text": "A"}]})],
        edges=[gs.Edge(id="e", source="t", target="b")],
    )
    assert gs.validate_graph(g, channel_type="email") == []


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def test_variable_refs_extraction():
    refs = gs.variable_refs({"a": "{{ contact.name }} and {{ vars.x.y }}", "b": ["{{ trigger.msg }}"]})
    assert set(refs) == {"contact.name", "vars.x.y", "trigger.msg"}


def test_parse_graph_roundtrip():
    raw = _valid_graph().model_dump()
    g = gs.parse_graph(raw)
    assert isinstance(g, gs.Graph)
    assert gs.validate_graph(g, channel_type="widget") == []


def test_cycles_are_allowed():
    # menu loop btn→msg→...→btn must NOT be an error
    assert gs.validate_graph(_valid_graph(), channel_type="widget") == []
    assert gs.MAX_STEPS == 100
