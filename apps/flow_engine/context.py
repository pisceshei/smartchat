"""Shared execution primitives for the flow interpreter (plan 附錄 B.1).

Holds the graph navigation index, the variable namespace assembly + Jinja2
sandbox renderer, and the ``NodeResult`` that every action/condition returns to
the interpreter. Kept dependency-light so conditions/actions import it without a
cycle through the interpreter.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
from jinja2 import StrictUndefined
from jinja2.exceptions import UndefinedError
from jinja2.sandbox import SandboxedEnvironment
from py_contracts.events import Event
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.flows.graph_schema import (
    TRIGGER_NODE_TYPE,
    Graph,
    Node,
    output_ports,
)

if TYPE_CHECKING:
    from apps.api.app.models.contacts import Contact
    from apps.api.app.models.conversations import Conversation
    from apps.api.app.models.flows import FlowSession

# session status buckets ----------------------------------------------------
STATUS_RUNNING = "running"
STATUS_DELAYED = "delayed"
STATUS_WAITING_REPLY = "waiting_reply"
STATUS_WAITING_BUTTON = "waiting_button"
STATUS_COMPLETED = "completed"
STATUS_ENDED = "ended"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES: frozenset[str] = frozenset(
    {STATUS_RUNNING, STATUS_DELAYED, STATUS_WAITING_REPLY, STATUS_WAITING_BUTTON}
)
WAITING_STATUSES: frozenset[str] = frozenset({STATUS_WAITING_REPLY, STATUS_WAITING_BUTTON})
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {STATUS_COMPLETED, STATUS_ENDED, STATUS_FAILED, STATUS_EXPIRED, STATUS_CANCELLED}
)


# ==========================================================================
# node result
# ==========================================================================
@dataclass
class NodeResult:
    """What a node execution tells the interpreter to do next."""

    kind: str  # "next" | "wait" | "end"
    port: str | None = None  # follow this output port (kind == next)
    status: str | None = None  # session status while suspended (kind == wait)
    waiting: dict[str, Any] | None = None  # suspend descriptor persisted on the session
    wakeup_at: datetime | None = None  # schedule a flow.resume timer at this time
    end_reason: str | None = None  # kind == end
    step_status: str = "ok"  # ok / error / skipped  (flow_session_steps.status)
    error: str | None = None

    @classmethod
    def next(cls, port: str = "out", *, step_status: str = "ok") -> NodeResult:
        return cls(kind="next", port=port, step_status=step_status)

    @classmethod
    def wait(
        cls,
        status: str,
        *,
        waiting: dict[str, Any],
        wakeup_at: datetime | None = None,
    ) -> NodeResult:
        return cls(kind="wait", status=status, waiting=waiting, wakeup_at=wakeup_at)

    @classmethod
    def end(cls, reason: str, *, step_status: str = "ok", error: str | None = None) -> NodeResult:
        return cls(kind="end", end_reason=reason, step_status=step_status, error=error)


# ==========================================================================
# graph navigation
# ==========================================================================
@dataclass
class GraphNav:
    """Adjacency index over a parsed Graph: O(1) node lookup + edge following
    by (source, port). A missing edge on a used port is a *dead end* (the
    session ends cleanly, not an error) per the port conventions."""

    graph: Graph
    nodes: dict[str, Node] = field(default_factory=dict)
    _edges: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def build(cls, graph: Graph) -> GraphNav:
        nav = cls(graph=graph)
        for n in graph.nodes:
            nav.nodes[n.id] = n
        for e in graph.edges:
            nav._edges[(e.source, e.source_port)] = e.target
        return nav

    @property
    def trigger_node(self) -> Node | None:
        for n in self.graph.nodes:
            if n.type == TRIGGER_NODE_TYPE:
                return n
        return None

    def start_node_id(self) -> str | None:
        """First real node the flow runs (trigger's ``out`` target)."""
        tn = self.trigger_node
        if tn is None:
            return None
        return self._edges.get((tn.id, "out"))

    def get(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def next_id(self, node_id: str, port: str) -> str | None:
        return self._edges.get((node_id, port))

    def valid_ports(self, node: Node) -> list[str]:
        return output_ports(node)


# ==========================================================================
# variable namespaces + Jinja2 sandbox
# ==========================================================================
_ENV = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
# filter whitelist (plan B.1: filters白名單). SandboxedEnvironment already blocks
# attribute access to dunders / callables that mutate; we further trim filters.
_ALLOWED_FILTERS = {
    "upper", "lower", "title", "capitalize", "trim", "truncate", "default",
    "length", "replace", "int", "float", "round", "abs", "join", "first", "last",
}
_ENV.filters = {k: v for k, v in _ENV.filters.items() if k in _ALLOWED_FILTERS}


def _contact_ns(contact: Contact | None) -> dict[str, Any]:
    if contact is None:
        return {}
    return {
        "id": str(contact.id),
        "display_name": contact.display_name,
        "remark_name": contact.remark_name,
        "email": contact.email,
        "phone": contact.phone,
        "language": contact.language,
        "country": contact.country,
        "city": contact.city,
        "timezone": contact.timezone,
        "device": contact.device,
        "browser": contact.browser,
        "os": contact.os,
        "is_blacklisted": contact.is_blacklisted,
        "custom": dict(contact.custom or {}),
    }


def _conversation_ns(conversation: Conversation | None) -> dict[str, Any]:
    if conversation is None:
        return {}
    return {
        "id": str(conversation.id),
        "channel_type": conversation.channel_type,
        "status": conversation.status,
        "handler": conversation.handler,
        "assignee_member_id": str(conversation.assignee_member_id)
        if conversation.assignee_member_id
        else None,
        "needs_reply": conversation.needs_reply,
        "priority": conversation.priority,
    }


def build_namespaces(
    fs: FlowSession, conversation: Conversation | None, contact: Contact | None
) -> dict[str, Any]:
    """Assemble the {contact, conversation, trigger, vars, ext} namespaces for
    templating + condition evaluation. trigger/vars/ext live in the session's
    ``variables`` jsonb; contact/conversation are read live off the ORM rows."""
    stored = fs.variables or {}
    return {
        "vars": dict(stored.get("vars", {})),
        "trigger": dict(stored.get("trigger", {})),
        "ext": dict(stored.get("ext", {})),
        "contact": _contact_ns(contact),
        "conversation": _conversation_ns(conversation),
    }


def render_template(template: str, namespaces: dict[str, Any]) -> str:
    """Render a Jinja2 template string in the sandbox. Unknown variables render
    as empty (we swallow UndefinedError to keep runtime robust — validation at
    publish time already flagged unresolvable namespaces)."""
    if not template or "{{" not in template:
        return template
    try:
        return _ENV.from_string(template).render(**namespaces)
    except UndefinedError:
        # fall back to lenient rendering (undefined → empty)
        lenient = SandboxedEnvironment(autoescape=False)
        lenient.filters = _ENV.filters
        try:
            return lenient.from_string(template).render(**namespaces)
        except Exception:  # noqa: BLE001
            return template
    except Exception:  # noqa: BLE001 — a bad template must not crash a session
        return template


def render_value(value: Any, namespaces: dict[str, Any]) -> Any:
    """Recursively render strings inside a dict/list structure."""
    if isinstance(value, str):
        return render_template(value, namespaces)
    if isinstance(value, dict):
        return {k: render_value(v, namespaces) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, namespaces) for v in value]
    return value


def resolve_path(namespaces: dict[str, Any], path: str) -> Any:
    """Dotted lookup across the namespaces, e.g. ``ext.node1.total`` or
    ``contact.custom.vip``. Returns None if any segment is missing."""
    cur: Any = namespaces
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


# ==========================================================================
# execution context
# ==========================================================================
@dataclass
class ExecutionContext:
    """Passed to every node executor. ``events`` accumulates outbox events the
    runtime publishes via publish_realtime after commit."""

    session: AsyncSession
    redis: aioredis.Redis | None
    flow_session: FlowSession
    conversation: Conversation | None
    contact: Contact | None
    now: datetime
    test_mode: bool = False
    events: list[Event] = field(default_factory=list)

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.flow_session.workspace_id

    def namespaces(self) -> dict[str, Any]:
        return build_namespaces(self.flow_session, self.conversation, self.contact)

    def render(self, template: str) -> str:
        return render_template(template, self.namespaces())

    # --- variable writes (persisted on flow_session.variables) -------------
    def _ns_bucket(self, bucket: str) -> dict[str, Any]:
        variables = dict(self.flow_session.variables or {})
        b = dict(variables.get(bucket, {}))
        variables[bucket] = b
        self.flow_session.variables = variables
        return b

    def set_var(self, key: str, value: Any) -> None:
        b = self._ns_bucket("vars")
        b[key] = value

    def set_ext(self, node_id: str, value: Any) -> None:
        b = self._ns_bucket("ext")
        b[node_id] = value

    def set_trigger_var(self, key: str, value: Any) -> None:
        b = self._ns_bucket("trigger")
        b[key] = value
