"""needs_reply / unread transitions + send-pipeline window & capability
validation (plan A.5 semantics, pure layer)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from py_contracts.content import MessageContent

from apps.api.app.services.messaging import (
    UnsupportedContentError,
    WindowExpiredError,
    apply_inbound_transition,
    apply_outbound_transition,
    ensure_sendable,
    make_snippet,
    msg_type_for,
    window_is_open,
)

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _conv(needs_reply=False, unread=0) -> SimpleNamespace:
    return SimpleNamespace(needs_reply=needs_reply, agent_unread_count=unread)


def _text(text="hi") -> MessageContent:
    return MessageContent.model_validate({"blocks": [{"kind": "text", "text": text}]})


def _template() -> MessageContent:
    return MessageContent.model_validate(
        {"blocks": [{"kind": "template", "template_name": "order_update", "language": "en"}]}
    )


# --------------------------------------------------------------------------
# needs_reply / unread state machine
# --------------------------------------------------------------------------
def test_inbound_sets_needs_reply_and_bumps_unread():
    c = _conv()
    apply_inbound_transition(c)
    assert c.needs_reply is True and c.agent_unread_count == 1
    apply_inbound_transition(c)
    assert c.agent_unread_count == 2


def test_outbound_reply_clears_needs_reply():
    c = _conv(needs_reply=True, unread=3)
    apply_outbound_transition(c, is_note=False)
    assert c.needs_reply is False
    # unread is the READ cursor's job, not the reply's
    assert c.agent_unread_count == 3


def test_internal_note_changes_nothing():
    c = _conv(needs_reply=True, unread=2)
    apply_outbound_transition(c, is_note=True)
    assert c.needs_reply is True and c.agent_unread_count == 2


def test_reply_then_inbound_needs_reply_again():
    c = _conv()
    apply_inbound_transition(c)
    apply_outbound_transition(c, is_note=False)
    assert c.needs_reply is False
    apply_inbound_transition(c)
    assert c.needs_reply is True


# --------------------------------------------------------------------------
# 24h window (WINDOW_EXPIRED typed error)
# --------------------------------------------------------------------------
def test_window_open_allows_send():
    ensure_sendable(
        channel_type="whatsapp_cloud",
        content=_text(),
        customer_window_expires_at=NOW + timedelta(hours=1),
        is_note=False,
        now=NOW,
    )


def test_window_expired_raises_typed_error():
    with pytest.raises(WindowExpiredError) as exc:
        ensure_sendable(
            channel_type="whatsapp_cloud",
            content=_text(),
            customer_window_expires_at=NOW - timedelta(minutes=1),
            is_note=False,
            now=NOW,
        )
    assert exc.value.code == "WINDOW_EXPIRED"


def test_window_never_opened_counts_as_expired():
    with pytest.raises(WindowExpiredError):
        ensure_sendable(
            channel_type="messenger",
            content=_text(),
            customer_window_expires_at=None,
            is_note=False,
            now=NOW,
        )


def test_template_bypasses_expired_window_on_whatsapp():
    ensure_sendable(
        channel_type="whatsapp_cloud",
        content=_template(),
        customer_window_expires_at=None,
        is_note=False,
        now=NOW,
    )


def test_template_does_not_bypass_on_messenger():
    with pytest.raises((WindowExpiredError, UnsupportedContentError)):
        ensure_sendable(
            channel_type="messenger",
            content=_template(),
            customer_window_expires_at=None,
            is_note=False,
            now=NOW,
        )


def test_note_bypasses_window_and_capability():
    ensure_sendable(
        channel_type="whatsapp_cloud",
        content=_text(),
        customer_window_expires_at=None,
        is_note=True,
        now=NOW,
    )


def test_windowless_channel_ignores_window():
    ensure_sendable(
        channel_type="widget",
        content=_text(),
        customer_window_expires_at=None,
        is_note=False,
        now=NOW,
    )


def test_hard_capability_template_only_on_whatsapp():
    with pytest.raises(UnsupportedContentError):
        ensure_sendable(
            channel_type="telegram_bot",
            content=_template(),
            customer_window_expires_at=None,
            is_note=False,
            now=NOW,
        )


def test_hard_capability_email_block_only_on_email_channel():
    email = MessageContent.model_validate(
        {"blocks": [{"kind": "email", "subject": "s", "text": "b"}]}
    )
    with pytest.raises(UnsupportedContentError):
        ensure_sendable(
            channel_type="widget", content=email,
            customer_window_expires_at=None, is_note=False, now=NOW,
        )
    ensure_sendable(
        channel_type="email", content=email,
        customer_window_expires_at=None, is_note=False, now=NOW,
    )


def test_window_is_open_handles_naive_datetimes():
    assert window_is_open(NOW.replace(tzinfo=None) + timedelta(hours=1), NOW)
    assert not window_is_open(None, NOW)


# --------------------------------------------------------------------------
# misc pure helpers
# --------------------------------------------------------------------------
def test_msg_type_follows_leading_block():
    assert msg_type_for(_text()) == "text"
    media = MessageContent.model_validate(
        {"blocks": [{"kind": "media", "media_type": "image",
                     "file_id": "018f0000-0000-7000-8000-000000000001"}]}
    )
    assert msg_type_for(media) == "image"
    assert msg_type_for(_template()) == "template"


def test_snippet_text_and_media_placeholder():
    assert make_snippet(_text("hello\nworld")) == "hello world"
    media = MessageContent.model_validate(
        {"blocks": [{"kind": "media", "media_type": "file",
                     "file_id": "018f0000-0000-7000-8000-000000000001"}]}
    )
    assert make_snippet(media) == "[附件]"
    assert len(make_snippet(_text("x" * 500))) == 140
