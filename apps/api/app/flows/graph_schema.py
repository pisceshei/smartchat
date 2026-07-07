"""Flow graph schema, port conventions, validation and trigger extraction
(plan 附錄 B.1).

The canvas is a directed graph of nodes + edges. There is exactly ONE start
node of type ``"trigger"`` whose ``data.triggers`` list holds one-or-more
trigger definitions (a flow may fire on several triggers, all leading into the
same graph — matches the observed SaleSmartly editor). Every other node is one
of the 7 condition types or 15 action types.

Port conventions (output ports an edge may leave from):
  - trigger, most actions ............ "out"
  - ask ............................... "answered" / "timeout" / "invalid"
  - quick_buttons ..................... "button:<id>" (per button) + "timeout" + "typed_reply"
  - external_request .................. "success" / "failed"
  - conditions ........................ "branch:<idx>" (per branch) + "else"
A missing edge on a used port is a dead end → the session ends (NOT an error).

Variable namespaces (Jinja2 sandbox at runtime): contact.* / conversation.* /
trigger.* / vars.* / ext.<node_id>.*  — validation checks every ``{{ … }}``
reference resolves to one of these (and that ext.<node> points at a real
external_request node).
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1
# runtime step budget — cycles (menu loops) are allowed in the graph but capped
# at execution time to prevent runaways.
MAX_STEPS = 100

# --------------------------------------------------------------------------
# node type catalogs (plan section 4 / B.1)
# --------------------------------------------------------------------------
TRIGGER_TYPES: tuple[str, ...] = (
    "new_visitor",          # 新訪客
    "returning_visitor",    # 舊訪客
    "visitor_message",      # 訪客發消息 (keyword / any)
    "visitor_intent",       # 訪客意圖識別 (AI)
    "widget_opened",        # 聊天窗口展開
    "page_visited",         # 訪問特定頁面
    "lead_submitted",       # 訪客留資
    "agent_timeout",        # 客服超時未回复
    "visitor_timeout",      # 訪客超時未回复
)

CONDITION_TYPES: tuple[str, ...] = (
    "visitor_language",     # 訪客語言
    "country",              # 國家/地區
    "time_schedule",        # 自動執行時段
    "random_branch",        # 隨機分支 (A/B)
    "device",               # 訪問設備
    "contact_attribute",    # 客戶屬性/行為
    "external_variable",    # 外部請求變數
)

ACTION_TYPES: tuple[str, ...] = (
    "send_message",         # 發送消息 (含商品卡片)
    "ask",                  # 問詢 (答案存屬性)
    "send_email",           # 發送郵件
    "quick_buttons",        # 快捷按鈕 (每按鈕一分支)
    "add_contact_tag",      # 添加訪客標籤
    "add_conversation_tag",  # 新增會話標籤
    "delay",                # 延時等候
    "invite_rating",        # 邀請評價
    "promo_card",           # 推廣卡片
    "transfer_unassigned",  # 轉未分配會話
    "assign_agent",         # 分配客服
    "close_conversation",   # 結束會話
    "external_request",     # 外部請求 (HTTP)
    "update_contact",       # 更新客戶資料
    "add_to_blacklist",     # 加入黑名單
)

TRIGGER_NODE_TYPE = "trigger"
NODE_TYPES: frozenset[str] = frozenset({TRIGGER_NODE_TYPE, *CONDITION_TYPES, *ACTION_TYPES})

# nodes that mark flow completion when reached (plan 完成度)
TERMINAL_NODE_TYPES: frozenset[str] = frozenset({"close_conversation"})
# nodes that suspend the session waiting for the visitor (engagement surface)
INTERACTIVE_NODE_TYPES: frozenset[str] = frozenset({"ask", "quick_buttons"})

VALID_NAMESPACES: frozenset[str] = frozenset({"contact", "conversation", "trigger", "vars", "ext"})

# channel capability matrix (mirror of the adapter layer, plan A.7). Missing
# features degrade at render time (card→text+link, buttons→numbered menu) so
# they are NOT validation errors; NON_DEGRADABLE features are.
CHANNEL_FEATURES: dict[str, frozenset[str]] = {
    "widget": frozenset({"quick_buttons", "product_card", "media", "location", "email"}),
    "telegram_bot": frozenset({"quick_buttons", "product_card", "media", "location"}),
    "telegram_app": frozenset({"media", "location"}),
    "messenger": frozenset({"quick_buttons", "product_card", "media"}),
    "instagram": frozenset({"quick_buttons", "media"}),
    "whatsapp_cloud": frozenset({"quick_buttons", "product_card", "media", "location", "template"}),
    "whatsapp_app": frozenset({"media", "location"}),
    "line_oa": frozenset({"quick_buttons", "product_card", "media", "location"}),
    "line_app": frozenset({"media"}),
    "email": frozenset({"email", "media"}),
}
NON_DEGRADABLE_FEATURES: frozenset[str] = frozenset({"template"})


# --------------------------------------------------------------------------
# graph models
# --------------------------------------------------------------------------
class Position(BaseModel):
    x: float = 0.0
    y: float = 0.0


class Node(BaseModel):
    id: str
    type: str
    position: Position = Field(default_factory=Position)
    data: dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    id: str
    source: str
    target: str
    source_port: str = "out"


class Graph(BaseModel):
    schema_version: int = SCHEMA_VERSION
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


class TriggerDef(BaseModel):
    """One entry in the start node's ``data.triggers`` list. ``type`` must be one
    of TRIGGER_TYPES (enforced by validate_graph)."""

    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    freq_cap: dict[str, Any] = Field(default_factory=dict)


class ExtractedTrigger(BaseModel):
    """Flattened row for the flow_triggers denorm table."""

    node_id: str
    trigger_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    freq_cap: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------
def _button_id(b: Any, idx: int) -> str:
    if isinstance(b, dict):
        return str(b.get("id") or b.get("value") or idx)
    return str(idx)


def output_ports(node: Node) -> list[str]:
    """The valid output ports for a node given its type and data."""
    t = node.type
    if t == TRIGGER_NODE_TYPE:
        return ["out"]
    if t == "ask":
        return ["answered", "timeout", "invalid"]
    if t == "external_request":
        return ["success", "failed"]
    if t == "quick_buttons":
        buttons = node.data.get("buttons") or []
        ports = [f"button:{_button_id(b, i)}" for i, b in enumerate(buttons)]
        ports += ["timeout", "typed_reply"]
        return ports
    if t in CONDITION_TYPES:
        branches = node.data.get("branches") or []
        return [f"branch:{i}" for i in range(len(branches))] + ["else"]
    # remaining actions
    return ["out"]


def is_terminal(node: Node) -> bool:
    return node.type in TERMINAL_NODE_TYPES


# --------------------------------------------------------------------------
# variable reference scanning
# --------------------------------------------------------------------------
_JINJA_BLOCK = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
# a dotted reference: root.segment(.segment)* — must contain at least one dot
_DOTTED_REF = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)\b")


def _iter_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_iter_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_iter_strings(v))
    return out


def variable_refs(data: Any) -> list[str]:
    """All dotted variable paths referenced inside ``{{ … }}`` blocks."""
    refs: list[str] = []
    for s in _iter_strings(data):
        for block in _JINJA_BLOCK.findall(s):
            for m in _DOTTED_REF.findall(block):
                refs.append(m)
    return refs


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
def _channel_supports(channel_type: str | None, feature: str) -> bool:
    if channel_type is None:
        return True
    feats = CHANNEL_FEATURES.get(channel_type)
    if feats is None:  # unknown channel — don't block
        return True
    return feature in feats


def _node_features(node: Node) -> set[str]:
    """Channel features a node requires (for capability check)."""
    feats: set[str] = set()
    t = node.type
    if t == "quick_buttons":
        feats.add("quick_buttons")
    elif t == "promo_card":
        feats.add("product_card")
    elif t == "send_email":
        feats.add("email")
    elif t == "send_message":
        for block in node.data.get("blocks") or []:
            kind = block.get("kind") if isinstance(block, dict) else None
            if kind == "product_card":
                feats.add("product_card")
            elif kind == "quick_buttons":
                feats.add("quick_buttons")
            elif kind == "template":
                feats.add("template")
            elif kind == "location":
                feats.add("location")
    return feats


def validate_graph(graph: Graph, *, channel_type: str | None = None) -> list[str]:
    """Return a list of publish-blocking errors (empty = valid).

    Checks: exactly one trigger node with ≥1 valid trigger def; every edge
    references existing nodes and a real output port on its source; no duplicate
    outgoing edge per (source, port); variable refs resolve to a known namespace
    (and ext.<node> points at an external_request node); channel capability for
    non-degradable features. Cycles are allowed (runtime MAX_STEPS cap)."""
    errors: list[str] = []
    nodes_by_id: dict[str, Node] = {}

    # ---- nodes ----
    for node in graph.nodes:
        if node.id in nodes_by_id:
            errors.append(f"duplicate node id: {node.id}")
            continue
        nodes_by_id[node.id] = node
        if node.type not in NODE_TYPES:
            errors.append(f"unknown node type '{node.type}' on node {node.id}")

    # ---- exactly one trigger node ----
    trigger_nodes = [n for n in graph.nodes if n.type == TRIGGER_NODE_TYPE]
    if len(trigger_nodes) == 0:
        errors.append("graph has no trigger node")
    elif len(trigger_nodes) > 1:
        errors.append(f"graph must have exactly one trigger node, found {len(trigger_nodes)}")

    for tn in trigger_nodes:
        triggers = tn.data.get("triggers")
        if not isinstance(triggers, list) or not triggers:
            errors.append(f"trigger node {tn.id} has no trigger definitions")
            continue
        for i, td in enumerate(triggers):
            ttype = td.get("type") if isinstance(td, dict) else None
            if ttype not in TRIGGER_TYPES:
                errors.append(f"trigger node {tn.id} def #{i}: invalid trigger type {ttype!r}")

    # ---- condition nodes need at least one branch ----
    for node in graph.nodes:
        if node.type in CONDITION_TYPES:
            branches = node.data.get("branches")
            if node.type != "random_branch" and (not isinstance(branches, list) or not branches):
                errors.append(f"condition node {node.id} ({node.type}) has no branches")
            if node.type == "random_branch" and (not isinstance(branches, list) or len(branches) < 2):
                errors.append(f"random_branch node {node.id} needs ≥2 weighted branches")

    # ---- edges: endpoints + ports + duplicates ----
    seen_ports: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if edge.source not in nodes_by_id:
            errors.append(f"edge {edge.id} source references missing node {edge.source}")
            continue
        if edge.target not in nodes_by_id:
            errors.append(f"edge {edge.id} target references missing node {edge.target}")
        src = nodes_by_id[edge.source]
        ports = output_ports(src)
        if edge.source_port not in ports:
            errors.append(
                f"edge {edge.id} uses port '{edge.source_port}' not valid for "
                f"node {edge.source} ({src.type}); valid: {ports}"
            )
        else:
            key = (edge.source, edge.source_port)
            if key in seen_ports:
                errors.append(
                    f"duplicate outgoing edge on port '{edge.source_port}' of node {edge.source}"
                )
            seen_ports.add(key)

    # ---- variable references ----
    for node in graph.nodes:
        for ref in variable_refs(node.data):
            root = ref.split(".")[0]
            if root not in VALID_NAMESPACES:
                errors.append(
                    f"node {node.id}: unresolvable variable '{{{{ {ref} }}}}' "
                    f"(unknown namespace '{root}')"
                )
                continue
            if root == "ext":
                parts = ref.split(".")
                if len(parts) < 3:
                    errors.append(f"node {node.id}: ext reference '{ref}' must be ext.<node>.<field>")
                    continue
                ext_node = nodes_by_id.get(parts[1])
                if ext_node is None or ext_node.type != "external_request":
                    errors.append(
                        f"node {node.id}: ext.{parts[1]} does not reference an external_request node"
                    )

    # ---- channel capability (non-degradable only) ----
    if channel_type is not None:
        for node in graph.nodes:
            for feature in _node_features(node):
                if feature in NON_DEGRADABLE_FEATURES and not _channel_supports(channel_type, feature):
                    errors.append(
                        f"node {node.id} ({node.type}) needs feature '{feature}' "
                        f"not supported by channel '{channel_type}'"
                    )

    return errors


def extract_triggers(graph: Graph) -> list[ExtractedTrigger]:
    """Flatten the trigger node's definitions into rows for flow_triggers.
    Skips invalid entries (validate_graph reports them separately)."""
    out: list[ExtractedTrigger] = []
    for node in graph.nodes:
        if node.type != TRIGGER_NODE_TYPE:
            continue
        for td in node.data.get("triggers") or []:
            if not isinstance(td, dict):
                continue
            ttype = td.get("type")
            if ttype not in TRIGGER_TYPES:
                continue
            out.append(
                ExtractedTrigger(
                    node_id=node.id,
                    trigger_type=ttype,
                    config=td.get("config") or {},
                    freq_cap=td.get("freq_cap") or {},
                )
            )
    return out


def parse_graph(raw: dict[str, Any]) -> Graph:
    """Parse a stored graph dict into the typed model (raises on structural
    schema errors; semantic checks are in validate_graph)."""
    return Graph.model_validate(raw)
