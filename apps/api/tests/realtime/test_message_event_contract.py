"""Realtime message-payload contract: message_row_payload is the ONE builder
both the outbound `_message_event` and the inbound ingress pipeline use, and
its nested copy is what live agent clients render from (the gateway slims flat
content off non-open-conversation frames). Shape drift here = empty bubbles in
the admin inbox — lock it. Pure logic, in-memory ORM rows, no DB."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from py_contracts.events import Actor

from apps.api.app.models.messaging import Message
from apps.api.app.services import messaging

WS = uuid.UUID("33333333-3333-7333-8333-333333333333")
CONV = uuid.UUID("66666666-6666-7666-8666-666666666666")
IDENTITY = uuid.UUID("77777777-7777-7777-8777-777777777777")
MSG = uuid.UUID("88888888-8888-7888-8888-888888888888")
SENDER = uuid.UUID("99999999-9999-7999-8999-999999999999")
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

ROW_KEYS = {
    "id", "conversation_id", "channel_identity_id", "direction", "sender_type",
    "sender_id", "msg_type", "content", "text_plain", "is_note", "sent_via",
    "client_msg_id", "delivery_status", "created_at",
}


def _message(**over) -> Message:
    kw = dict(
        id=MSG,
        workspace_id=WS,
        conversation_id=CONV,
        channel_identity_id=IDENTITY,
        direction="out",
        sender_type="ai_agent",
        sender_id=SENDER,
        msg_type="text",
        content={"blocks": [{"kind": "text", "text": "您好！"}]},
        text_plain="您好！",
        is_note=False,
        sent_via=None,
        client_msg_id=None,
        delivery_status="pending",
        created_at=NOW,
    )
    kw.update(over)
    return Message(**kw)


def _conversation() -> SimpleNamespace:
    return SimpleNamespace(
        id=CONV,
        contact_id=uuid.uuid4(),
        channel_type="whatsapp_app",
        channel_account_id=uuid.uuid4(),
    )


def test_message_row_payload_shape_and_json_safe():
    row = messaging.message_row_payload(_message())
    assert set(row) == ROW_KEYS
    json.dumps(row)  # every value JSON-serializable
    assert row["id"] == str(MSG)
    assert row["conversation_id"] == str(CONV)
    assert row["content"] == {"blocks": [{"kind": "text", "text": "您好！"}]}
    assert row["created_at"] == NOW.isoformat()


def test_message_row_payload_created_at_falls_back_to_kwarg():
    # ingress builds the payload pre-flush, before the column default fires
    row = messaging.message_row_payload(_message(created_at=None), created_at=NOW)
    assert row["created_at"] == NOW.isoformat()


def test_message_row_payload_never_emits_null_content():
    row = messaging.message_row_payload(_message(content=None))
    assert row["content"] == {"blocks": []}


def test_outbound_message_event_nests_full_row():
    msg = _message()
    ev = messaging._message_event(
        msg, _conversation(), Actor(type="ai_agent", id=SENDER), requires_channel_send=True
    )
    row = messaging.message_row_payload(msg)
    # the nested copy is byte-identical to the flat row — it is what agent
    # clients render after the gateway strips flat content
    assert ev.payload["message"] == row
    assert ev.payload["id"] == ev.payload["message_id"] == str(MSG)
    assert ev.payload["content"] == row["content"]
    assert ev.payload["created_at"] == row["created_at"]
    assert ev.payload["requires_channel_send"] is True
    assert ev.type == "message.created"
    assert ev.conversation_id == CONV


def test_delivery_status_event_shape():
    msg = _message()
    ev = messaging.delivery_status_event(
        msg, status="read", external_message_id="3EB0X", channel_account_id=uuid.uuid4()
    )
    assert ev.type == "message.updated"
    assert ev.conversation_id == CONV
    assert ev.payload["message_id"] == ev.payload["id"] == str(MSG)
    assert ev.payload["conversation_id"] == str(CONV)
    assert ev.payload["delivery_status"] == "read"
    assert ev.payload["external_message_id"] == "3EB0X"
    # deliberately NO body keys: clients must patch, never rebuild
    assert "content" not in ev.payload
    assert "message" not in ev.payload
    json.dumps(ev.payload)
