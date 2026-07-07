"""Sum+count rollup fold merge (plan 附錄 B.4). Pure — exercises the in-memory
accumulator without a DB (apply_accumulator's SQL upsert is covered by the live
smoke)."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from py_contracts.events import Actor, Event

from apps.api.app.analytics import collectors
from apps.api.app.analytics.rollup import Accumulator, fold_event

WS = uuid4()
MEMBER = uuid4()
HOUR = datetime(2026, 1, 1, 10, tzinfo=UTC)


def _at(minute):
    return datetime(2026, 1, 1, 10, minute, tzinfo=UTC)


def _out_msg(actor_type, actor_id, minute=5):
    return Event(
        workspace_id=WS, type="message.created", actor=Actor(type=actor_type, id=actor_id),
        channel_type="whatsapp", occurred_at=_at(minute),
        payload={"direction": "out", "msg_type": "text"},
    )


def _in_msg(minute=6):
    return Event(
        workspace_id=WS, type="message.created", actor=Actor(type="contact"),
        channel_type="whatsapp", occurred_at=_at(minute),
        payload={"direction": "in", "msg_type": "text"},
    )


def test_message_volume_is_additive_within_hour():
    acc = Accumulator()
    for _ in range(3):
        fold_event(acc, _out_msg("member", MEMBER))
    fold_event(acc, _in_msg())
    out_key = (WS, HOUR, "whatsapp", MEMBER, "out", False)
    in_key = (WS, HOUR, "whatsapp", collectors.NIL_UUID_OBJ, "in", False)
    assert acc.messages[out_key] == 3
    assert acc.messages[in_key] == 1
    assert acc.agents[(WS, HOUR, MEMBER)].msgs == 3  # agent productivity


def test_second_fold_pass_merges_additively():
    acc = Accumulator()
    fold_event(acc, _out_msg("member", MEMBER))
    # a later batch reuses the SAME accumulator dict → additive merge
    fold_event(acc, _out_msg("member", MEMBER))
    assert acc.messages[(WS, HOUR, "whatsapp", MEMBER, "out", False)] == 2


def test_opened_resolved_frt_resolution_sums():
    sid = uuid4()
    acc = Accumulator()
    fold_event(acc, Event(workspace_id=WS, type="conversation.created",
                          actor=Actor(type="contact"), channel_type="widget",
                          occurred_at=_at(0), payload={}))
    fold_event(
        acc,
        Event(workspace_id=WS, type="conversation.first_responded",
              actor=Actor(type="member", id=MEMBER), channel_type="widget", occurred_at=_at(4),
              payload={"session_id": str(sid), "first_response_at": _at(4).isoformat()}),
        session_started_at={sid: _at(0)},
    )
    fold_event(
        acc,
        Event(workspace_id=WS, type="conversation.resolved", actor=Actor(type="member", id=MEMBER),
              channel_type="widget", occurred_at=_at(30),
              payload={"session_id": str(sid), "closed_at": _at(30).isoformat()}),
        session_started_at={sid: _at(0)},
    )
    conv = acc.convs[(WS, HOUR, "widget")]
    assert conv.opened == 1
    assert conv.resolved == 1
    assert conv.frt_sum_s == 240 and conv.frt_n == 1  # 4 min
    assert conv.resolution_sum_s == 1800 and conv.resolution_n == 1  # 30 min
    # FRT also attributed to the responding agent
    agent = acc.agents[(WS, HOUR, MEMBER)]
    assert agent.frt_sum_s == 240 and agent.frt_n == 1


def test_frt_without_session_start_counts_no_duration():
    sid = uuid4()
    acc = Accumulator()
    fold_event(
        acc,
        Event(workspace_id=WS, type="conversation.first_responded",
              actor=Actor(type="member", id=MEMBER), channel_type="widget", occurred_at=_at(4),
              payload={"session_id": str(sid), "first_response_at": _at(4).isoformat()}),
        session_started_at={},  # unknown start → no frt sample
    )
    assert acc.convs.get((WS, HOUR, "widget")) is None or acc.convs[(WS, HOUR, "widget")].frt_n == 0


def test_csat_and_assigned_agent_productivity():
    acc = Accumulator()
    fold_event(acc, Event(workspace_id=WS, type="conversation.assigned", actor=Actor(type="system"),
                          occurred_at=_at(1),
                          payload={"handler": "member", "assignee_member_id": str(MEMBER)}))
    fold_event(acc, Event(workspace_id=WS, type="csat.submitted", actor=Actor(type="contact"),
                          occurred_at=_at(50), payload={"score": 4, "agent_id": str(MEMBER)}))
    agent = acc.agents[(WS, HOUR, MEMBER)]
    assert agent.convs == 1
    assert agent.csat_sum == 4 and agent.csat_n == 1


def test_events_in_different_hours_bucket_separately():
    acc = Accumulator()
    fold_event(acc, _out_msg("member", MEMBER, minute=5))  # hour 10
    later = _out_msg("member", MEMBER)
    later.occurred_at = datetime(2026, 1, 1, 11, 5, tzinfo=UTC)  # hour 11
    fold_event(acc, later)
    assert acc.messages[(WS, HOUR, "whatsapp", MEMBER, "out", False)] == 1
    assert acc.messages[(WS, datetime(2026, 1, 1, 11, tzinfo=UTC), "whatsapp", MEMBER, "out", False)] == 1
