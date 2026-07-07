"""Event envelope: stream routing, XADD field encode/decode, row mapping."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from py_contracts.events import EVENT_TOPICS, STREAMS, Actor, Event

from apps.api.app.services.event_bus import (
    FALLBACK_STREAM,
    decode_event,
    encode_fields,
    event_to_row,
    row_to_event,
    stream_for,
)

WS = uuid.UUID("22222222-2222-7222-8222-222222222222")


def _event(type_: str = "message.created") -> Event:
    return Event(
        workspace_id=WS,
        type=type_,
        actor=Actor(type="member", id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        channel_type="widget",
        payload={"text": "hi", "n": 3},
    )


def test_every_registered_type_routes_to_a_known_stream():
    for etype, family in EVENT_TOPICS.items():
        assert stream_for(etype) == STREAMS[family]
        assert _event(etype).stream == STREAMS[family]


def test_unregistered_type_falls_back():
    assert stream_for("timer.custom_thing") == FALLBACK_STREAM
    assert stream_for("") == FALLBACK_STREAM


def test_encode_decode_roundtrip():
    ev = _event()
    fields = encode_fields(ev)
    assert set(fields) == {"id", "ws", "type", "data"}
    assert all(isinstance(v, str) for v in fields.values())
    back = decode_event(fields)
    assert back == ev


def test_row_mapping_roundtrip():
    ev = _event("conversation.assigned")
    row = event_to_row(ev)
    assert row.id == ev.id
    assert row.workspace_id == WS
    assert row.published is False
    assert row.actor_type == "member"
    back = row_to_event(row)
    assert back == ev


def test_uuid7_ids_are_time_ordered():
    ids = [_event().id for _ in range(50)]
    assert ids == sorted(ids)


def test_occurred_at_is_utc():
    ev = _event()
    assert ev.occurred_at.tzinfo is not None
    assert ev.occurred_at.utcoffset().total_seconds() == 0
    assert abs((datetime.now(UTC) - ev.occurred_at).total_seconds()) < 5
