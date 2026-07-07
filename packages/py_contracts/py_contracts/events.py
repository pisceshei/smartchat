"""Event envelope + topic registry.

Every domain write that other subsystems care about emits one of these via the
transactional outbox (events table) which a relay tails into Redis Streams.
The events table doubles as the reports raw store — one write path feeds the
bus and analytics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

try:  # uuid7 package exposes uuid7()
    from uuid_extensions import uuid7  # type: ignore
except ImportError:  # pragma: no cover
    from uuid6 import uuid7  # type: ignore


# Topic families → Redis stream keys. Consumer groups: flow-engine / rollup /
# notifier / webhook-dispatch.
STREAMS: dict[str, str] = {
    "conversation": "events:conversation",
    "visitor": "events:visitor",
    "contact": "events:contact",
    "broadcast": "events:broadcast",
    "channel": "events:channel",
    "billing": "events:billing",
}

# type → topic family (stream routing). Keep sorted; append-only in spirit.
EVENT_TOPICS: dict[str, str] = {
    "message.created": "conversation",
    "message.updated": "conversation",
    "conversation.created": "conversation",
    "conversation.opened": "conversation",
    "conversation.assigned": "conversation",
    "conversation.first_responded": "conversation",
    "conversation.resolved": "conversation",
    "conversation.reopened": "conversation",
    "conversation.timeout.agent": "conversation",
    "conversation.timeout.visitor": "conversation",
    "csat.submitted": "conversation",
    "ai.reply": "conversation",
    "ai.handoff": "conversation",
    "translation.used": "conversation",
    "widget.opened": "visitor",
    "visitor.page_view": "visitor",
    "visitor.identified": "visitor",
    "lead.submitted": "visitor",
    "splitlink.click": "visitor",
    "contact.created": "contact",
    "contact.updated": "contact",
    "contact.merged": "contact",
    "broadcast.recipient_state": "broadcast",
    "channel.status": "channel",
    "points.consumed": "billing",
    "quota.exceeded": "billing",
}


class Actor(BaseModel):
    type: Literal["contact", "member", "ai_agent", "flow", "system", "api"]
    id: UUID | None = None


class Event(BaseModel):
    id: UUID = Field(default_factory=lambda: uuid7())
    workspace_id: UUID
    type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: Actor
    conversation_id: UUID | None = None
    contact_id: UUID | None = None
    channel_type: str | None = None
    channel_account_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def stream(self) -> str:
        return STREAMS[EVENT_TOPICS[self.type]]
