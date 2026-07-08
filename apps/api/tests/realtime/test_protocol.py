"""Protocol unit tests: seq resume math, event filtering, envelope
serialization, frame parsing. Pure logic — no Redis/DB."""
from __future__ import annotations

import uuid

import pytest

from apps.api.app.realtime.protocol import (
    AUDIENCE_AGENTS,
    AgentScope,
    FocusFrame,
    FrameError,
    PingFrame,
    ReadFrame,
    ResumeAction,
    ResumeFrame,
    RtEvent,
    TypingFrame,
    VisitorScope,
    filter_for_agent,
    filter_for_visitor,
    member_audience,
    parse_frame,
    pubsub_key,
    resume_decision,
    seq_key,
    stream_key,
    visitor_audience,
    visitor_pubsub_key,
    visitor_targets,
)

WS = uuid.UUID("33333333-3333-7333-8333-333333333333")
ME = uuid.UUID("44444444-4444-7444-8444-444444444444")
OTHER = uuid.UUID("55555555-5555-7555-8555-555555555555")
CONV = uuid.UUID("66666666-6666-7666-8666-666666666666")
IDENTITY = uuid.UUID("77777777-7777-7777-8777-777777777777")


def _event(
    type_: str = "message.created",
    *,
    seq: int | None = 7,
    data: dict | None = None,
    audiences: list[str] | None = None,
    conversation_id=CONV,
    channel_identity_id=None,
) -> RtEvent:
    return RtEvent(
        seq=seq,
        workspace_id=WS,
        type=type_,
        data=data if data is not None else {"id": "m1", "content": {"blocks": []}},
        audiences=audiences if audiences is not None else [AUDIENCE_AGENTS],
        conversation_id=conversation_id,
        channel_identity_id=channel_identity_id,
    )


def _agent(perms: set[str] | None = None, open_conv=None) -> AgentScope:
    return AgentScope(
        member_id=ME,
        workspace_id=WS,
        permissions=perms if perms is not None else {"inbox.view_all"},
        open_conversation_id=open_conv,
    )


# --------------------------------------------------------------------------
# envelope serialization
# --------------------------------------------------------------------------
def test_client_frame_shape():
    ev = _event()
    frame = ev.client_frame()
    # canonical envelope + client-compat aliases: id/payload/conversation_id
    # mirror event_id/data/routing metadata (the admin SPA and the widget read
    # the alias names — dropping them froze inbox realtime updates once).
    assert set(frame) == {
        "event_id", "id", "seq", "workspace_id", "type", "ts",
        "data", "payload", "conversation_id",
    }
    assert frame["seq"] == 7
    assert frame["workspace_id"] == str(WS)
    assert frame["type"] == "message.created"
    assert frame["data"]["id"] == "m1"
    assert frame["payload"] is frame["data"]
    assert frame["id"] == frame["event_id"]
    assert frame["conversation_id"] == (str(ev.conversation_id) if ev.conversation_id else None)


def test_stream_fields_roundtrip():
    ev = _event(seq=42)
    back = RtEvent.from_stream_fields(ev.stream_fields())
    assert back == ev


def test_stream_seq_field_overrides_envelope():
    # publisher's Lua XADDs the envelope BEFORE the seq is known — the seq
    # stream field is authoritative on decode
    ev = _event(seq=None)
    fields = {"seq": "99", "data": ev.model_dump_json()}
    back = RtEvent.from_stream_fields(fields)
    assert back.seq == 99


def test_from_stream_fields_accepts_bytes_keys():
    ev = _event(seq=5)
    fields = {b"seq": b"5", b"data": ev.model_dump_json().encode()}
    assert RtEvent.from_stream_fields(fields) == ev


def test_from_stream_fields_missing_data_raises():
    with pytest.raises(ValueError):
        RtEvent.from_stream_fields({"seq": "1"})


def test_key_layout():
    assert seq_key(WS) == f"seq:{WS}"
    assert stream_key(WS) == f"evt:{WS}"
    assert pubsub_key(WS) == f"evtps:{WS}"
    assert visitor_pubsub_key(IDENTITY) == f"evtps:vis:{IDENTITY}"


def test_visitor_targets_extraction():
    auds = [AUDIENCE_AGENTS, visitor_audience(IDENTITY), member_audience(ME)]
    assert visitor_targets(auds) == [str(IDENTITY)]


# --------------------------------------------------------------------------
# seq resume math
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("resume_from", "current", "oldest", "expected"),
    [
        (10, 10, 1, ResumeAction.NOOP),  # caught up
        (11, 10, 1, ResumeAction.NOOP),  # ahead (clock skew) — nothing to do
        (0, 0, None, ResumeAction.NOOP),  # fresh workspace, no events yet
        (0, 5, 1, ResumeAction.REPLAY),  # full replay from the beginning
        (4, 10, 1, ResumeAction.REPLAY),  # normal gap, stream covers it
        (4, 10, 5, ResumeAction.REPLAY),  # boundary: oldest == resume_from+1
        (3, 10, 5, ResumeAction.RESYNC),  # trimmed: oldest > resume_from+1
        (0, 10, None, ResumeAction.RESYNC),  # seq advanced but stream empty
    ],
)
def test_resume_decision(resume_from, current, oldest, expected):
    assert resume_decision(resume_from, current, oldest) is expected


# --------------------------------------------------------------------------
# agent-side filtering
# --------------------------------------------------------------------------
def test_agent_view_all_sees_other_agents_conversation():
    ev = _event(data={"id": "m1", "content": {}, "assignee_member_id": str(OTHER)})
    assert filter_for_agent(ev, _agent({"inbox.view_all"})) is not None


def test_agent_view_mine_hidden_when_assigned_elsewhere():
    ev = _event(data={"id": "m1", "content": {}, "assignee_member_id": str(OTHER)})
    assert filter_for_agent(ev, _agent({"inbox.view_mine"})) is None


def test_agent_view_mine_sees_own_and_unassigned():
    mine = _event(data={"id": "m1", "content": {}, "assignee_member_id": str(ME)})
    unassigned = _event(data={"id": "m2", "content": {}, "assignee_member_id": None})
    scope = _agent({"inbox.view_mine"})
    assert filter_for_agent(mine, scope) is not None
    assert filter_for_agent(unassigned, scope) is not None


def test_agent_wildcard_permission_counts_as_view_all():
    ev = _event(data={"id": "m1", "content": {}, "assignee_member_id": str(OTHER)})
    assert filter_for_agent(ev, _agent({"inbox.*"})) is not None
    assert filter_for_agent(ev, _agent({"*"})) is not None


def test_targeted_member_event_bypasses_audience_and_perm_checks():
    ev = _event(
        "unread.changed",
        data={"count": 3, "assignee_member_id": str(OTHER)},
        audiences=[member_audience(ME)],
    )
    assert filter_for_agent(ev, _agent({"inbox.view_mine"})) is not None
    # a different member is NOT addressed
    other_scope = AgentScope(member_id=OTHER, workspace_id=WS, permissions={"*"})
    assert filter_for_agent(ev, other_scope) is None


def test_visitor_only_event_not_delivered_to_agents():
    ev = _event(audiences=[visitor_audience(IDENTITY)])
    assert filter_for_agent(ev, _agent({"*"})) is None


def test_message_body_slimmed_unless_conversation_open():
    ev = _event(data={"id": "m1", "content": {"blocks": [1]}, "text_plain": "hi"})
    closed_frame = filter_for_agent(ev, _agent(open_conv=None))
    assert closed_frame is not None
    assert "content" not in closed_frame["data"]
    assert closed_frame["data"]["text_plain"] == "hi"  # preview survives

    open_frame = filter_for_agent(ev, _agent(open_conv=CONV))
    assert open_frame is not None
    assert open_frame["data"]["content"] == {"blocks": [1]}


def test_typing_only_for_open_conversation():
    ev = _event("typing", seq=None, data={"conversation_id": str(CONV), "sender": "visitor"})
    assert filter_for_agent(ev, _agent(open_conv=CONV)) is not None
    assert filter_for_agent(ev, _agent(open_conv=None)) is None
    assert filter_for_agent(ev, _agent(open_conv=OTHER)) is None


# --------------------------------------------------------------------------
# visitor-side filtering (per-audience serializer)
# --------------------------------------------------------------------------
def _visitor() -> VisitorScope:
    return VisitorScope(workspace_id=WS, channel_identity_id=IDENTITY)


def _visitor_event(data: dict, type_: str = "message.created") -> RtEvent:
    return _event(
        type_,
        data=data,
        audiences=[AUDIENCE_AGENTS, visitor_audience(IDENTITY)],
        channel_identity_id=IDENTITY,
    )


def test_visitor_sees_own_message_with_whitelisted_fields_only():
    ev = _visitor_event(
        {
            "id": "m1",
            "conversation_id": str(CONV),
            "content": {"blocks": []},
            "is_note": False,
            "sent_via": "automation",
            "source_flow_id": "f1",
            "assignee_member_id": str(ME),
            "sender_type": "member",
        }
    )
    frame = filter_for_visitor(ev, _visitor())
    assert frame is not None
    assert "is_note" not in frame["data"]
    assert "sent_via" not in frame["data"]
    assert "source_flow_id" not in frame["data"]
    assert "assignee_member_id" not in frame["data"]
    assert frame["data"]["content"] == {"blocks": []}


def test_visitor_never_sees_internal_notes():
    ev = _visitor_event({"id": "m1", "is_note": True, "content": {}})
    assert filter_for_visitor(ev, _visitor()) is None


def test_visitor_requires_identity_stamp_and_audience():
    scope = _visitor()
    # right audience, wrong identity stamp
    wrong_identity = _event(
        data={"id": "m1"},
        audiences=[visitor_audience(IDENTITY)],
        channel_identity_id=OTHER,
    )
    assert filter_for_visitor(wrong_identity, scope) is None
    # right identity stamp, not addressed to the visitor
    not_addressed = _event(data={"id": "m1"}, audiences=[AUDIENCE_AGENTS], channel_identity_id=IDENTITY)
    assert filter_for_visitor(not_addressed, scope) is None


def test_visitor_event_type_whitelist():
    ev = _visitor_event({"id": "c1"}, type_="contact.updated")
    assert filter_for_visitor(ev, _visitor()) is None


def test_visitor_typing_only_from_agent():
    agent_typing = _visitor_event({"conversation_id": str(CONV), "sender": "agent"}, type_="typing")
    own_typing = _visitor_event({"conversation_id": str(CONV), "sender": "visitor"}, type_="typing")
    assert filter_for_visitor(agent_typing, _visitor()) is not None
    assert filter_for_visitor(own_typing, _visitor()) is None


# --------------------------------------------------------------------------
# frame parsing
# --------------------------------------------------------------------------
def test_parse_valid_frames():
    assert isinstance(parse_frame({"type": "resume", "resume_from": 12}), ResumeFrame)
    assert isinstance(parse_frame({"type": "ping"}), PingFrame)
    assert isinstance(parse_frame({"type": "typing", "conversation_id": str(CONV)}), TypingFrame)
    read = parse_frame({"type": "read", "conversation_id": str(CONV)})
    assert isinstance(read, ReadFrame) and read.message_id is None
    focus = parse_frame({"type": "focus", "conversation_id": None, "tab": "mine"})
    assert isinstance(focus, FocusFrame) and focus.tab == "mine"


def test_parse_rejects_unknown_and_invalid_frames():
    with pytest.raises(FrameError):
        parse_frame({"type": "send_message", "text": "no — sending is REST"})
    with pytest.raises(FrameError):
        parse_frame({"type": "resume", "resume_from": -1})
    with pytest.raises(FrameError):
        parse_frame({"type": "typing"})  # missing conversation_id
    with pytest.raises(FrameError):
        parse_frame("not a dict")
