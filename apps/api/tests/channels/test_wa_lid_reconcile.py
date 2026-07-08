"""_upsert_identity WhatsApp-lid reconciliation paths (pure, faked session):

(a) fresh unresolved lid  -> identity keyed by the lid, meta.wa_lid set,
                             contact created WITHOUT a phone (UI shows "-")
(b) heal/migrate          -> a later phone-keyed event re-keys the lid identity
                             in place (same contact) and fixes the placeholder
                             phone, emitting a contact.updated heal event
(c) duplicate             -> phone- and lid-keyed identities both exist ->
                             a PENDING merge is returned for the caller to run
                             AFTER commit (never inline: merge_contacts takes
                             ordered row locks and must not run inside the
                             ingest transaction)
(d) a REAL phone on the contact is never overwritten by a later hint
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from py_contracts.content import MessageContent

from apps.api.app.channels import ingress_pipeline
from apps.api.app.channels.base import MessageIn, ProfileHint
from apps.api.app.channels.ingress_pipeline import _upsert_identity
from apps.api.app.models.contacts import ChannelIdentity, Contact

LID = "56985642876983"
PHONE = "85266577437"
WS = uuid.UUID("33333333-3333-7333-8333-333333333333")
ACCT = uuid.UUID("44444444-4444-7444-8444-444444444444")
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


class FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class FakeSession:
    """Just enough AsyncSession for _upsert_identity: sequenced execute()
    results, a get() lookup table, id-assigning flush()."""

    def __init__(self, execute_rows: list, contacts: dict):
        self.execute_rows = list(execute_rows)
        self.contacts = contacts
        self.added: list = []

    async def execute(self, *_a, **_k):
        return FakeResult(self.execute_rows.pop(0))

    async def get(self, _model, pk):
        return self.contacts.get(pk)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


def _acct() -> SimpleNamespace:
    return SimpleNamespace(id=ACCT, workspace_id=WS, channel_type="whatsapp_app")


def _ev(external_user_id: str, *, phone: str | None, lid: str | None, name="浠") -> MessageIn:
    return MessageIn(
        external_message_id="3EB0A1",
        external_user_id=external_user_id,
        content=MessageContent.model_validate({"blocks": [{"kind": "text", "text": "hi"}]}),
        profile=ProfileHint(display_name=name, phone=phone),
        meta={"lid": lid} if lid else {},
    )


def _identity(external_user_id: str, contact_id: uuid.UUID, meta: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=WS,
        channel_account_id=ACCT,
        channel_type="whatsapp_app",
        external_user_id=external_user_id,
        contact_id=contact_id,
        display_name=None,
        avatar_url=None,
        meta=meta or {},
        last_seen_at=None,
    )


def _contact(contact_id: uuid.UUID, *, phone: str | None, name="浠"):
    return SimpleNamespace(
        id=contact_id,
        display_name=name,
        avatar_url=None,
        email=None,
        phone=phone,
        language=None,
        country=None,
        last_seen_at=None,
        merged_into_id=None,
    )


@pytest.fixture(autouse=True)
def _quiet_event_bus(monkeypatch):
    monkeypatch.setattr(ingress_pipeline.event_bus, "emit", AsyncMock())


async def test_fresh_unresolved_lid_creates_identity_without_phone():
    session = FakeSession(execute_rows=[None], contacts={})
    identity, contact, created, heal, pending = await _upsert_identity(
        session, _acct(), _ev(LID, phone=None, lid=LID)
    )
    assert created is True
    assert heal == []
    assert pending is None
    assert isinstance(identity, ChannelIdentity)
    assert isinstance(contact, Contact)
    assert identity.external_user_id == LID
    assert identity.meta["wa_lid"] == LID
    assert contact.phone is None  # NEVER "+<lid>"


async def test_lid_to_phone_heal_rekeys_identity_and_fixes_placeholder():
    contact_id = uuid.uuid4()
    lid_identity = _identity(LID, contact_id, meta={})
    contact = _contact(contact_id, phone=f"+{LID}")  # the pre-fix placeholder
    session = FakeSession(
        execute_rows=[None, lid_identity],  # primary miss, lid hit
        contacts={contact_id: contact},
    )
    identity, got_contact, created, heal, pending = await _upsert_identity(
        session, _acct(), _ev(PHONE, phone=f"+{PHONE}", lid=LID)
    )
    assert created is False
    assert pending is None
    assert identity is lid_identity  # same row, no duplicate contact
    assert identity.external_user_id == PHONE
    assert identity.meta["wa_lid"] == LID
    assert got_contact is contact
    assert contact.phone == f"+{PHONE}"
    assert any(e.payload.get("changed", {}).get("phone") == f"+{PHONE}" for e in heal)


async def test_duplicate_lid_and_phone_identities_return_pending_merge():
    phone_contact_id, lid_contact_id = uuid.uuid4(), uuid.uuid4()
    phone_identity = _identity(PHONE, phone_contact_id)
    lid_identity = _identity(LID, lid_contact_id)
    phone_contact = _contact(phone_contact_id, phone=f"+{PHONE}")
    lid_contact = _contact(lid_contact_id, phone=f"+{LID}")

    session = FakeSession(
        execute_rows=[phone_identity, lid_identity],
        contacts={phone_contact_id: phone_contact, lid_contact_id: lid_contact},
    )
    identity, contact, created, heal, pending = await _upsert_identity(
        session, _acct(), _ev(PHONE, phone=f"+{PHONE}", lid=LID)
    )
    assert created is False
    assert identity is phone_identity
    # the merge must NOT run inside the ingest transaction (ordered-lock
    # inversion vs concurrent UI merges could deadlock and drop the message)
    # — it is handed back to the caller to run after commit:
    assert pending == {
        "workspace_id": _acct().workspace_id,
        "target_id": phone_contact_id,
        "source_id": lid_contact_id,
        "lid": LID,
    }
    # and the source contact is NOT pre-mutated inside this transaction
    assert lid_contact.phone == f"+{LID}"
    assert lid_identity.meta["wa_lid"] == LID


async def test_duplicate_with_same_contact_returns_no_pending_merge():
    contact_id = uuid.uuid4()
    phone_identity = _identity(PHONE, contact_id)
    lid_identity = _identity(LID, contact_id)  # already same person
    contact = _contact(contact_id, phone=f"+{PHONE}")

    session = FakeSession(
        execute_rows=[phone_identity, lid_identity], contacts={contact_id: contact}
    )
    *_rest, pending = await _upsert_identity(
        session, _acct(), _ev(PHONE, phone=f"+{PHONE}", lid=LID)
    )
    assert pending is None


async def test_real_phone_is_never_overwritten():
    contact_id = uuid.uuid4()
    identity = _identity(PHONE, contact_id, meta={"wa_lid": LID})
    contact = _contact(contact_id, phone="+85299998888")  # real, user-set
    session = FakeSession(execute_rows=[identity, None], contacts={contact_id: contact})
    _, got_contact, _, heal, _ = await _upsert_identity(
        session, _acct(), _ev(PHONE, phone=f"+{PHONE}", lid=LID)
    )
    assert got_contact.phone == "+85299998888"
    assert not any("phone" in e.payload.get("changed", {}) for e in heal)


async def test_placeholder_phone_with_known_lid_rejected_on_create():
    # defense in depth: any adapter that names the lid AND claims "+<lid>" as
    # the phone gets the placeholder rejected. (A real OLD bridge sends the
    # placeholder WITHOUT meta.lid — for brand-new senders that can only be
    # caught by deploy ordering + the backfill; see the update-path test below
    # for the case the API can catch.)
    session = FakeSession(execute_rows=[None], contacts={})
    _, contact, created, _, _ = await _upsert_identity(
        session, _acct(), _ev(LID, phone=f"+{LID}", lid=LID)
    )
    assert created is True
    assert contact.phone is None


async def test_old_bridge_cannot_repoison_annotated_identity():
    # the REAL rolling-deploy scenario the API can defend: an identity already
    # annotated with meta.wa_lid (healed/backfilled), then an OLD bridge event
    # arrives claiming "+<lid>" as the phone WITHOUT meta.lid. The remembered
    # wa_lid must reject the placeholder so the cleared phone stays empty.
    contact_id = uuid.uuid4()
    identity = _identity(LID, contact_id, meta={"wa_lid": LID})
    contact = _contact(contact_id, phone=None)  # cleared by backfill
    session = FakeSession(execute_rows=[identity], contacts={contact_id: contact})
    _, got_contact, _, heal, _ = await _upsert_identity(
        session, _acct(), _ev(LID, phone=f"+{LID}", lid=None)  # old bridge: no meta
    )
    assert got_contact.phone is None  # not re-poisoned
    assert not any("phone" in e.payload.get("changed", {}) for e in heal)
