"""Event → aggregate classification (plan 附錄 B.4). Pure, no DB."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from py_contracts.events import Actor, Event

from apps.api.app.analytics import collectors

WS = uuid4()
MEMBER = uuid4()
AI = uuid4()


def _msg(direction, actor_type, actor_id=None, *, is_note=False, msg_type="text", channel="whatsapp"):
    return Event(
        workspace_id=WS,
        type="message.created",
        actor=Actor(type=actor_type, id=actor_id),
        channel_type=channel,
        occurred_at=datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        payload={"direction": direction, "msg_type": msg_type, "is_note": is_note},
    )


# --------------------------------------------------------------------------
# message volume
# --------------------------------------------------------------------------
def test_inbound_message_volume():
    mv = collectors.message_volume(_msg("in", "contact"))
    assert mv is not None
    assert mv.direction == "in"
    assert mv.agent_id == collectors.NIL_UUID_OBJ
    assert mv.ai_flag is False
    assert mv.channel_type == "whatsapp"


def test_outbound_member_message_attributes_agent():
    mv = collectors.message_volume(_msg("out", "member", MEMBER))
    assert mv.direction == "out"
    assert mv.agent_id == MEMBER
    assert mv.ai_flag is False
    assert collectors.agent_message_id(_msg("out", "member", MEMBER)) == MEMBER


def test_outbound_ai_message_sets_ai_flag():
    mv = collectors.message_volume(_msg("out", "ai_agent", AI))
    assert mv.agent_id == AI
    assert mv.ai_flag is True


def test_outbound_bot_flow_message_has_no_agent():
    mv = collectors.message_volume(_msg("out", "flow"))
    assert mv.agent_id == collectors.NIL_UUID_OBJ
    assert mv.ai_flag is False
    assert collectors.agent_message_id(_msg("out", "flow")) is None


def test_internal_note_excluded():
    assert collectors.message_volume(_msg("out", "member", MEMBER, is_note=True)) is None
    assert collectors.agent_message_id(_msg("out", "member", MEMBER, is_note=True)) is None


def test_system_event_chip_excluded():
    assert collectors.message_volume(_msg("out", "system", msg_type="system_event")) is None


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
def test_opened_created_and_reopened():
    created = Event(workspace_id=WS, type="conversation.created", actor=Actor(type="contact"),
                    channel_type="line", payload={})
    reopened = Event(workspace_id=WS, type="conversation.reopened", actor=Actor(type="contact"),
                     channel_type="line", payload={})
    assert collectors.opened_channel(created) == "line"
    assert collectors.opened_channel(reopened) == "line"
    assert collectors.reopened(reopened) is True
    assert collectors.reopened(created) is False


def test_assigned_agent_only_for_human_or_ai_handler():
    ev = Event(workspace_id=WS, type="conversation.assigned", actor=Actor(type="system"),
               payload={"handler": "member", "assignee_member_id": str(MEMBER)})
    assert collectors.assigned_agent_id(ev) == MEMBER
    unassigned = Event(workspace_id=WS, type="conversation.assigned", actor=Actor(type="system"),
                       payload={"handler": "unassigned", "assignee_member_id": None})
    assert collectors.assigned_agent_id(unassigned) is None


def test_resolved_and_first_responded_parse_times():
    sid = uuid4()
    resolved_ev = Event(
        workspace_id=WS, type="conversation.resolved", actor=Actor(type="member", id=MEMBER),
        channel_type="widget",
        payload={"session_id": str(sid), "closed_at": "2026-01-01T10:30:00+00:00"},
    )
    r = collectors.resolved(resolved_ev)
    assert r.session_id == sid
    assert r.closed_at == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)

    fr_ev = Event(
        workspace_id=WS, type="conversation.first_responded", actor=Actor(type="ai_agent", id=AI),
        channel_type="widget",
        payload={"session_id": str(sid), "first_response_at": "2026-01-01T10:04:00+00:00"},
    )
    fr = collectors.first_responded(fr_ev)
    assert fr.agent_id == AI
    assert fr.session_id == sid


def test_csat_validates_range():
    good = Event(workspace_id=WS, type="csat.submitted", actor=Actor(type="contact"),
                 payload={"score": 5, "agent_id": str(MEMBER)})
    c = collectors.csat(good)
    assert c.score == 5 and c.agent_id == MEMBER
    bad = Event(workspace_id=WS, type="csat.submitted", actor=Actor(type="contact"),
                payload={"score": 9})
    assert collectors.csat(bad) is None
