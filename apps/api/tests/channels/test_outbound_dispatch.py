"""Regression guard for the outbound-dispatch gap (the bug where agent / AI /
flow replies were written delivery_status='pending' but never enqueued to the
channel, so they never reached WhatsApp/Telegram/etc.).

``messaging.dispatch_channel_sends`` is the low-latency hot path every outbound
caller now runs after commit; it must enqueue exactly the message.created events
flagged ``requires_channel_send`` and skip everything else (notes, inbound,
non-message events). The ``drain_pending_sends_task`` cron is the at-least-once
safety net behind it — both feed the same idempotent ``enqueue_send``.
"""
from __future__ import annotations

import uuid

import pytest
from py_contracts.events import Actor, Event

from apps.api.app.channels import sender
from apps.api.app.services import messaging


def _msg_event(*, requires_channel_send: bool, is_note: bool = False,
               etype: str = "message.created") -> Event:
    mid = uuid.uuid4()
    return Event(
        workspace_id=uuid.uuid4(),
        type=etype,
        actor=Actor(type="ai_agent", id=uuid.uuid4()),
        conversation_id=uuid.uuid4(),
        channel_type="whatsapp_app",
        payload={
            "message_id": str(mid),
            "is_note": is_note,
            "requires_channel_send": requires_channel_send,
        },
    )


@pytest.mark.asyncio
async def test_dispatch_enqueues_only_channel_sends(monkeypatch):
    enqueued: list[str] = []

    async def fake_enqueue(message_id, *, defer_by=None):
        enqueued.append(str(message_id))

    monkeypatch.setattr(sender, "enqueue_send", fake_enqueue)

    send_ev = _msg_event(requires_channel_send=True)
    note_ev = _msg_event(requires_channel_send=False, is_note=True)
    other_ev = _msg_event(requires_channel_send=True, etype="conversation.updated")

    await messaging.dispatch_channel_sends([send_ev, note_ev, other_ev])

    # only the real channel send is enqueued; the note and the non-message event
    # are ignored.
    assert enqueued == [send_ev.payload["message_id"]]


@pytest.mark.asyncio
async def test_dispatch_noop_on_empty_and_swallows_errors(monkeypatch):
    calls: list[str] = []

    async def boom(message_id, *, defer_by=None):
        calls.append(str(message_id))
        raise RuntimeError("redis down")

    monkeypatch.setattr(sender, "enqueue_send", boom)

    # empty list never touches enqueue_send
    await messaging.dispatch_channel_sends([])
    assert calls == []

    # a failing enqueue must not propagate (the drain cron re-picks the row)
    await messaging.dispatch_channel_sends([_msg_event(requires_channel_send=True)])
    assert len(calls) == 1
