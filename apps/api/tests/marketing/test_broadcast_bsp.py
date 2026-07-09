"""Broadcast template-channel gate: whatsapp templates must be sendable on
BOTH whatsapp_cloud (direct Meta) and whatsapp_bsp (YCloud proxy) accounts,
and the approval gate still blocks unapproved templates."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.api.app.modules.broadcasts.service import (
    TEMPLATE_CHANNEL_MAP,
    BroadcastError,
    _validate_refs,
)


def test_template_channel_map_accepts_both_whatsapp_transports():
    assert TEMPLATE_CHANNEL_MAP["whatsapp"] == {"whatsapp_cloud", "whatsapp_bsp"}


class _FakeSession:
    def __init__(self, objects: dict):
        self._objects = objects

    async def get(self, model, pk):
        return self._objects.get(pk)


def _ids():
    return uuid.uuid4(), uuid.uuid4()


WS = uuid.uuid4()


def _tpl(tid, status="approved", waba_account_id=None):
    return SimpleNamespace(
        id=tid, workspace_id=WS, channel="whatsapp", approval_status=status,
        waba_account_id=waba_account_id,
    )


def _acct(acct_id, ctype="whatsapp_bsp", waba_id=None):
    return SimpleNamespace(
        id=acct_id, workspace_id=WS, channel_type=ctype, config={"waba_id": waba_id} if waba_id else {}
    )


async def test_bsp_account_with_approved_template_passes():
    acct_id, tpl_id = _ids()
    session = _FakeSession(
        {
            acct_id: _acct(acct_id),
            # template bound to this account → WABA affinity satisfied
            tpl_id: _tpl(tpl_id, waba_account_id=str(acct_id)),
        }
    )
    await _validate_refs(
        session,
        workspace_id=WS,
        channel_type="whatsapp_bsp",
        channel_account_id=acct_id,
        segment_id=None,
        template_id=tpl_id,
    )


async def test_template_bound_to_other_account_rejected():
    acct_id, tpl_id = _ids()
    other = uuid.uuid4()
    session = _FakeSession(
        {acct_id: _acct(acct_id), tpl_id: _tpl(tpl_id, waba_account_id=str(other))}
    )
    with pytest.raises(BroadcastError) as ei:
        await _validate_refs(
            session,
            workspace_id=WS,
            channel_type="whatsapp_bsp",
            channel_account_id=acct_id,
            segment_id=None,
            template_id=tpl_id,
        )
    assert ei.value.code == "template_wrong_account"


async def test_unapproved_template_rejected_on_bsp():
    acct_id, tpl_id = _ids()
    session = _FakeSession(
        {acct_id: _acct(acct_id), tpl_id: _tpl(tpl_id, status="pending", waba_account_id=str(acct_id))}
    )
    with pytest.raises(BroadcastError) as ei:
        await _validate_refs(
            session,
            workspace_id=WS,
            channel_type="whatsapp_bsp",
            channel_account_id=acct_id,
            segment_id=None,
            template_id=tpl_id,
        )
    assert ei.value.code == "template_unapproved"


async def test_cloud_path_not_regressed():
    acct_id, tpl_id = _ids()
    session = _FakeSession(
        {acct_id: _acct(acct_id, ctype="whatsapp_cloud"), tpl_id: _tpl(tpl_id, waba_account_id=str(acct_id))}
    )
    await _validate_refs(
        session,
        workspace_id=WS,
        channel_type="whatsapp_cloud",
        channel_account_id=acct_id,
        segment_id=None,
        template_id=tpl_id,
    )


async def test_unlinked_template_still_passes():
    # a template with no waba_account_id is not affinity-checked
    acct_id, tpl_id = _ids()
    session = _FakeSession({acct_id: _acct(acct_id), tpl_id: _tpl(tpl_id)})
    await _validate_refs(
        session,
        workspace_id=WS,
        channel_type="whatsapp_bsp",
        channel_account_id=acct_id,
        segment_id=None,
        template_id=tpl_id,
    )
