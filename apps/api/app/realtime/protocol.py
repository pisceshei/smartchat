"""Realtime wire protocol (plan 附錄 A.8).

Pure logic — no Redis/DB I/O — so every rule here is unit-testable:
- RtEvent envelope {event_id, seq, workspace_id, type, ts, data} + stream codec
- upstream (client→server) frame union: resume / typing / read / ping / away / focus
- audience scopes (AgentScope / VisitorScope) + per-audience server-side filtering
- seq resume math (replay vs resync_required)
- typing throttle (1 per 3s)
- widget visitor tokens (minted by widget bootstrap, verified by the gateway)

Delivery contract is at-least-once: per-workspace monotonic seq + replay on
resume + client-side event_id LRU dedup. Ephemeral events (typing, presence)
carry seq=None and are never persisted to the stream.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from jose import JWTError, jwt
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from ..deps import has_permission
from ..settings import get_settings

try:  # uuid7 package exposes uuid7()
    from uuid_extensions import uuid7  # type: ignore
except ImportError:  # pragma: no cover
    from uuid6 import uuid7  # type: ignore


# --------------------------------------------------------------------------
# Redis key layout
# --------------------------------------------------------------------------
STREAM_MAXLEN = 10_000
TYPING_THROTTLE_SECONDS = 3.0


def seq_key(workspace_id: UUID | str) -> str:
    """Per-workspace monotonic sequence counter (INCR)."""
    return f"seq:{workspace_id}"


def stream_key(workspace_id: UUID | str) -> str:
    """Per-workspace replayable event stream (XADD MAXLEN ~10k)."""
    return f"evt:{workspace_id}"


def pubsub_key(workspace_id: UUID | str) -> str:
    """Workspace fanout wakeup channel (agents)."""
    return f"evtps:{workspace_id}"


def visitor_pubsub_key(channel_identity_id: UUID | str) -> str:
    """Widget-scoped fanout channel — visitor connections subscribe only this."""
    return f"evtps:vis:{channel_identity_id}"


# --------------------------------------------------------------------------
# audiences
# --------------------------------------------------------------------------
AUDIENCE_AGENTS = "agents"


def member_audience(member_id: UUID | str) -> str:
    return f"member:{member_id}"


def visitor_audience(channel_identity_id: UUID | str) -> str:
    return f"visitor:{channel_identity_id}"


def visitor_targets(audiences: list[str]) -> list[str]:
    """channel_identity ids addressed by visitor:* audience selectors."""
    return [a.split(":", 1)[1] for a in audiences if a.startswith("visitor:")]


# --------------------------------------------------------------------------
# event envelope
# --------------------------------------------------------------------------
class RtEvent(BaseModel):
    """Realtime envelope. `audiences` / routing metadata are server-side only;
    clients receive the frame from client_frame()."""

    event_id: UUID = Field(default_factory=lambda: uuid7())
    seq: int | None = None  # None = ephemeral (not in the stream)
    workspace_id: UUID
    type: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)
    audiences: list[str] = Field(default_factory=lambda: [AUDIENCE_AGENTS])
    conversation_id: UUID | None = None
    channel_identity_id: UUID | None = None

    def client_frame(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Spec envelope sent to clients. Event types are dotted; control
        frames use undotted `type` values — that is how clients tell them
        apart on a single socket.

        `id`/`payload`/`conversation_id` mirror `event_id`/`data`/envelope
        routing metadata: the admin SPA and the widget read those names, and
        omitting conversation_id here once made every message.created frame
        unroutable client-side (inbox froze until a manual refresh)."""
        d = self.data if data is None else data
        return {
            "event_id": str(self.event_id),
            "id": str(self.event_id),
            "seq": self.seq,
            "workspace_id": str(self.workspace_id),
            "type": self.type,
            "ts": self.ts.isoformat(),
            "data": d,
            "payload": d,
            "conversation_id": str(self.conversation_id) if self.conversation_id else None,
        }

    def stream_fields(self) -> dict[str, str]:
        return {"seq": str(self.seq or 0), "data": self.model_dump_json()}

    @classmethod
    def from_stream_fields(cls, fields: dict[Any, Any]) -> RtEvent:
        raw = fields.get("data") or fields.get(b"data")
        if raw is None:
            raise ValueError("stream entry missing data field")
        ev = cls.model_validate_json(raw)
        seq = fields.get("seq")
        if seq is None:
            seq = fields.get(b"seq")
        if seq is not None:
            ev.seq = int(seq)
        return ev


# --------------------------------------------------------------------------
# upstream frames (client → server). Message SENDING is REST — never a frame.
# --------------------------------------------------------------------------
class ResumeFrame(BaseModel):
    type: Literal["resume"] = "resume"
    resume_from: int = Field(ge=0)


class TypingFrame(BaseModel):
    type: Literal["typing"] = "typing"
    conversation_id: UUID
    # agent composer includes it so the visitor sees "agent is typing"
    channel_identity_id: UUID | None = None


class ReadFrame(BaseModel):
    """Read-cursor advance. message_id omitted = mark whole conversation read."""

    type: Literal["read"] = "read"
    conversation_id: UUID
    message_id: UUID | None = None


class PingFrame(BaseModel):
    type: Literal["ping"] = "ping"


class AwayFrame(BaseModel):
    """Manual away toggle (agents only)."""

    type: Literal["away"] = "away"
    away: bool = True


class FocusFrame(BaseModel):
    """Scope update: which conversation panel is open / which inbox tab is
    active. Control-plane, like resume — changes what the server sends, so it
    cannot be REST."""

    type: Literal["focus"] = "focus"
    conversation_id: UUID | None = None
    tab: str | None = None


UpstreamFrame = Annotated[
    ResumeFrame | TypingFrame | ReadFrame | PingFrame | AwayFrame | FocusFrame,
    Field(discriminator="type"),
]

_frame_adapter: TypeAdapter[UpstreamFrame] = TypeAdapter(UpstreamFrame)


class FrameError(Exception):
    pass


def parse_frame(raw: Any) -> UpstreamFrame:
    try:
        return _frame_adapter.validate_python(raw)
    except ValidationError as e:
        raise FrameError(f"invalid frame: {e.errors()[0].get('msg', 'validation error')}") from e


# server → client control frames (undotted types, distinct from event types)
def hello_frame(seq: int, session_id: str) -> dict[str, Any]:
    return {"type": "hello", "seq": seq, "session_id": session_id}


def pong_frame() -> dict[str, Any]:
    return {"type": "pong", "ts": datetime.now(UTC).isoformat()}


def resync_frame(current_seq: int) -> dict[str, Any]:
    """Client fell behind the stream retention — re-fetch state via REST,
    then resume from current_seq."""
    return {"type": "resync_required", "seq": current_seq}


def resume_ok_frame(seq: int, replayed: int) -> dict[str, Any]:
    return {"type": "resume_ok", "seq": seq, "replayed": replayed}


def error_frame(code: str, detail: str = "") -> dict[str, Any]:
    return {"type": "error", "code": code, "detail": detail}


# --------------------------------------------------------------------------
# scopes
# --------------------------------------------------------------------------
@dataclass
class AgentScope:
    member_id: UUID
    workspace_id: UUID
    permissions: set[str] = field(default_factory=set)
    group_ids: set[UUID] = field(default_factory=set)
    open_conversation_id: UUID | None = None
    active_tab: str | None = None
    display_name: str = ""


@dataclass
class VisitorScope:
    workspace_id: UUID
    channel_identity_id: UUID
    conversation_id: UUID | None = None


# --------------------------------------------------------------------------
# agent-side filtering
# --------------------------------------------------------------------------
# message body fields stripped when the conversation is NOT the open panel
_MESSAGE_HEAVY_FIELDS = ("content", "translations")
_MESSAGE_EVENT_TYPES = ("message.created", "message.updated")


def _slim_message_data(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in _MESSAGE_HEAVY_FIELDS}


def filter_for_agent(event: RtEvent, scope: AgentScope) -> dict[str, Any] | None:
    """Returns the client frame for this agent, or None if not visible.

    Rules (plan A.8): audience targeting → permission scope (inbox.view_all vs
    own/unassigned conversations) → tab scoping (typing only for the open
    panel) → body slimming (full message content only for the open panel).
    """
    targeted_me = member_audience(scope.member_id) in event.audiences
    if AUDIENCE_AGENTS not in event.audiences and not targeted_me:
        return None

    # permission scope for conversation-bound events. Publishers stamp
    # data.assignee_member_id on conversation events; when present and the
    # conversation belongs to another member, view_mine agents don't see it.
    if event.conversation_id is not None and not targeted_me:
        if not has_permission(scope.permissions, "inbox.view_all"):
            assignee = event.data.get("assignee_member_id")
            if (
                assignee is not None
                and str(assignee) != str(scope.member_id)
                and event.conversation_id != scope.open_conversation_id
            ):
                return None

    if event.type == "typing":
        # typing is noise outside the open panel
        if event.conversation_id != scope.open_conversation_id:
            return None
        return event.client_frame()

    if event.type in _MESSAGE_EVENT_TYPES and event.conversation_id != scope.open_conversation_id:
        return event.client_frame(_slim_message_data(event.data))

    return event.client_frame()


# --------------------------------------------------------------------------
# visitor-side filtering (per-audience serializer — strips internals)
# --------------------------------------------------------------------------
VISITOR_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "message.created",
        "message.updated",
        "message.read",
        "typing",
        "conversation.updated",
        "presence.member",
        "csat.requested",
    }
)

_VISITOR_FIELD_WHITELIST: dict[str, frozenset[str]] = {
    "message.created": frozenset(
        {
            "id",
            "conversation_id",
            "direction",
            "msg_type",
            "content",
            "text_plain",
            "created_at",
            "sender_type",
            "sender_name",
            "sender_avatar",
            "delivery_status",
            "client_msg_id",
        }
    ),
    "conversation.updated": frozenset({"id", "status", "assignee_name", "assignee_avatar"}),
    "typing": frozenset({"conversation_id", "sender"}),
    "presence.member": frozenset({"state", "member_id", "display_name"}),
    "message.read": frozenset({"conversation_id", "message_id"}),
    "csat.requested": frozenset({"conversation_id"}),
}
_VISITOR_FIELD_WHITELIST["message.updated"] = _VISITOR_FIELD_WHITELIST["message.created"]


def filter_for_visitor(event: RtEvent, scope: VisitorScope) -> dict[str, Any] | None:
    """Visitor sees only events stamped with their own channel_identity and
    explicitly addressed to them; internal notes and agent-only fields never
    cross the boundary."""
    if event.channel_identity_id != scope.channel_identity_id:
        return None
    if visitor_audience(scope.channel_identity_id) not in event.audiences:
        return None
    if event.type not in VISITOR_EVENT_TYPES:
        return None
    if event.type in _MESSAGE_EVENT_TYPES and event.data.get("is_note"):
        return None
    if event.type == "typing" and event.data.get("sender") != "agent":
        return None  # never echo the visitor's own typing back

    allowed = _VISITOR_FIELD_WHITELIST.get(event.type, frozenset())
    return event.client_frame({k: v for k, v in event.data.items() if k in allowed})


# --------------------------------------------------------------------------
# seq / resume math
# --------------------------------------------------------------------------
class ResumeAction(StrEnum):
    NOOP = "noop"  # client already caught up
    REPLAY = "replay"  # stream covers the gap — replay seq > resume_from
    RESYNC = "resync"  # gap trimmed out of the stream — client must REST-refetch


def resume_decision(resume_from: int, current_seq: int, oldest_available: int | None) -> ResumeAction:
    """Pure resume math. `oldest_available` = seq of the oldest stream entry
    (None when the stream is empty)."""
    if resume_from >= current_seq:
        return ResumeAction.NOOP
    if oldest_available is None:
        # seq advanced but the stream holds nothing → everything trimmed
        return ResumeAction.RESYNC
    if oldest_available > resume_from + 1:
        return ResumeAction.RESYNC
    return ResumeAction.REPLAY


# --------------------------------------------------------------------------
# typing throttle (1 per 3s per key)
# --------------------------------------------------------------------------
class Throttle:
    """Sliding min-interval gate. clock injectable for tests."""

    def __init__(self, interval: float = TYPING_THROTTLE_SECONDS, clock=time.monotonic):
        self.interval = interval
        self._clock = clock
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            return False
        self._last[key] = now
        if len(self._last) > 1024:  # bound memory on long-lived connections
            cutoff = now - self.interval
            self._last = {k: v for k, v in self._last.items() if v > cutoff}
        return True


# --------------------------------------------------------------------------
# widget visitor tokens
# --------------------------------------------------------------------------
_VISITOR_TOKEN_ALGO = "HS256"
VISITOR_TOKEN_TTL = timedelta(days=30)


class VisitorTokenInvalid(Exception):
    pass


def mint_visitor_token(
    workspace_id: UUID,
    channel_identity_id: UUID,
    *,
    conversation_id: UUID | None = None,
    ttl: timedelta | None = None,
) -> str:
    """Called by the widget bootstrap after upserting the channel_identity;
    scopes the socket / long-poll to exactly one visitor identity."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": str(channel_identity_id),
        "typ": "visitor",
        "ws": str(workspace_id),
        "jti": secrets.token_hex(8),
        "iat": int(now.timestamp()),
        "exp": int((now + (ttl or VISITOR_TOKEN_TTL)).timestamp()),
    }
    if conversation_id is not None:
        claims["conv"] = str(conversation_id)
    return jwt.encode(claims, get_settings().secret_key, algorithm=_VISITOR_TOKEN_ALGO)


def verify_visitor_token(token: str) -> VisitorScope:
    try:
        claims = jwt.decode(token, get_settings().secret_key, algorithms=[_VISITOR_TOKEN_ALGO])
    except JWTError as e:
        raise VisitorTokenInvalid(str(e)) from e
    if claims.get("typ") != "visitor":
        raise VisitorTokenInvalid("not a visitor token")
    try:
        return VisitorScope(
            workspace_id=UUID(claims["ws"]),
            channel_identity_id=UUID(claims["sub"]),
            conversation_id=UUID(claims["conv"]) if claims.get("conv") else None,
        )
    except (KeyError, ValueError) as e:
        raise VisitorTokenInvalid("malformed visitor token claims") from e
