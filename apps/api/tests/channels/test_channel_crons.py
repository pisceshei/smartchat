"""Channel-maintenance crons: OAuth token expiry parsing, proactive refresh,
and the YouTube comment poll (YouTube has no webhook). All faked — no network,
no DB, no ARQ; the helpers/tasks are driven directly with stub collaborators.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from apps.api.app.channels import sender
from apps.api.app.models.channels import ChannelAccount


def _creds_expiring(minutes: int) -> dict:
    exp = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()
    return {
        "oauth_access_token": "OLD",
        "oauth_refresh_token": "RT",
        "oauth_token_expires_at": exp,
    }


# --------------------------------------------------------------------------
# _token_expiry
# --------------------------------------------------------------------------
def test_token_expiry_handles_aware_naive_and_missing():
    assert sender._token_expiry({}) is None
    assert sender._token_expiry({"token_expires_at": "not-a-date"}) is None
    aware = sender._token_expiry({"token_expires_at": "2030-01-01T00:00:00+00:00"})
    assert aware is not None and aware.tzinfo is not None
    naive = sender._token_expiry({"oauth_token_expires_at": "2030-01-01T00:00:00"})
    assert naive is not None and naive.tzinfo is not None  # coerced to UTC


# --------------------------------------------------------------------------
# _refresh_if_expiring
# --------------------------------------------------------------------------
class _FakeSession:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


async def test_refresh_when_due_persists_and_returns_new(monkeypatch):
    acct = SimpleNamespace(id=uuid.uuid4(), channel_type="youtube")
    saved: dict = {}

    async def fake_set(session, a, creds):
        saved.update(creds)

    class FakeAdapter:
        async def refresh_credentials(self, a, creds):
            return {**creds, "oauth_access_token": "NEW"}

    monkeypatch.setattr(sender, "set_credentials", fake_set)
    monkeypatch.setattr(sender, "get_adapter", lambda ct: FakeAdapter())
    session = _FakeSession()
    out = await sender._refresh_if_expiring(session, acct, _creds_expiring(2))
    assert out["oauth_access_token"] == "NEW"
    assert saved["oauth_access_token"] == "NEW"
    assert session.committed


async def test_refresh_noop_when_token_not_near_expiry(monkeypatch):
    acct = SimpleNamespace(id=uuid.uuid4(), channel_type="youtube")
    calls = {"n": 0}

    class FakeAdapter:
        async def refresh_credentials(self, a, creds):
            calls["n"] += 1
            return {**creds, "oauth_access_token": "NEW"}

    monkeypatch.setattr(sender, "get_adapter", lambda ct: FakeAdapter())
    out = await sender._refresh_if_expiring(_FakeSession(), acct, _creds_expiring(60))
    assert out["oauth_access_token"] == "OLD" and calls["n"] == 0


async def test_refresh_noop_without_expiry_stamp():
    acct = SimpleNamespace(id=uuid.uuid4(), channel_type="email")
    creds = {"imap_password": "x"}
    assert await sender._refresh_if_expiring(_FakeSession(), acct, creds) == creds


# --------------------------------------------------------------------------
# youtube_poll_task
# --------------------------------------------------------------------------
class _SessionCtx:
    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *a):
        return False


class _PollSession:
    def __init__(self, acct, ids):
        self.acct = acct
        self.ids = ids
        self.committed = False

    async def execute(self, stmt):
        rows = self.ids
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, model, pk):
        return self.acct

    async def commit(self):
        self.committed = True


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    async def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return False
        self.store[k] = v
        return True

    async def delete(self, k):
        self.store.pop(k, None)


async def test_youtube_poll_enqueues_and_persists_cursor(monkeypatch):
    aid = uuid.uuid4()
    acct = ChannelAccount(
        workspace_id=uuid.uuid4(), channel_type="youtube", name="yt",
        external_id="UC1", config={}, enabled=True,
    )
    acct.id = aid
    session = _PollSession(acct=acct, ids=[aid])

    async def fake_get_credentials(s, a):
        return {"oauth_access_token": "T"}  # no expiry → refresh no-op

    class FakeYT:
        async def poll_comments(self, a, creds):
            return SimpleNamespace(
                count=2, payload={"items": [1, 2]}, cursor="2026-07-09T00:00:00Z"
            )

    enq: dict = {}

    async def fake_enqueue(redis, *, account_id, workspace_id, channel_type, payload):
        enq.update(channel_type=channel_type, payload=payload)

    monkeypatch.setattr(sender, "get_credentials", fake_get_credentials)
    monkeypatch.setattr(sender, "get_adapter", lambda ct: FakeYT())
    monkeypatch.setattr(
        "apps.api.app.channels.ingress_pipeline.enqueue_inbound", fake_enqueue
    )
    ctx = {"session_factory": lambda: _SessionCtx(session), "redis": _FakeRedis()}
    total = await sender.youtube_poll_task(ctx)
    assert total == 2
    assert enq["channel_type"] == "youtube"
    assert acct.config["youtube_poll_cursor"] == "2026-07-09T00:00:00Z"
    assert session.committed
