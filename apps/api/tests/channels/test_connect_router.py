"""Channel connect + widget CRUD router fixes.

No DB: a FakeSession records the statements the endpoints issue (so the
enabled==true soft-delete filters are asserted on the compiled SQL) and returns
canned results; the adapter + quota/credential collaborators are monkeypatched
in the router's namespace.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from apps.api.app.channels.base import HealthResult
from apps.api.app.deps import MemberContext
from apps.api.app.models.channels import ChannelAccount, Widget
from apps.api.app.modules.channels import router as ch_router


class FakeResult:
    def __init__(self, *, scalar=None, rows=None, count=0):
        self._scalar = scalar
        self._rows = rows or []
        self._count = count

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._count

    def scalars(self):
        rows = self._rows
        return SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None)


class FakeSession:
    def __init__(self, results=None, gets=None):
        self.stmts = []
        self.results = list(results or [])
        self.gets = gets or {}
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return self.results.pop(0) if self.results else FakeResult()

    async def get(self, model, pk):
        return self.gets.get(model)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True


def _member(ws_id: uuid.UUID | None = None) -> MemberContext:
    return MemberContext(
        member=SimpleNamespace(id=uuid.uuid4()),
        workspace=SimpleNamespace(id=ws_id or uuid.uuid4()),
        user=SimpleNamespace(id=uuid.uuid4()),
        permissions={"*"},
    )


class FakeAdapter:
    def __init__(self, *, validate_exc: Exception | None = None):
        self.validate_exc = validate_exc

    async def validate_token(self, token):
        if self.validate_exc is not None:
            raise self.validate_exc
        return {"id": 42, "username": "bot"}

    async def set_webhook(self, token, url, secret):
        return True

    async def check_health(self, acct, credentials):
        return HealthResult(ok=True, status="active", detail={})


@pytest.fixture
def no_quota(monkeypatch):
    async def _ok(session, member, channel_type):
        return None

    monkeypatch.setattr(ch_router, "_check_channel_quota", _ok)


@pytest.fixture
def captured_creds(monkeypatch):
    box: dict = {}

    async def _capture(session, acct, credentials):
        box.update(credentials)

    monkeypatch.setattr(ch_router, "set_credentials", _capture)
    return box


# --------------------------------------------------------------------------
# connect: telegram network failure → clean 422 (not a 500)
# --------------------------------------------------------------------------
async def test_telegram_network_error_is_422(monkeypatch, no_quota):
    monkeypatch.setattr(
        ch_router, "get_adapter",
        lambda ct: FakeAdapter(validate_exc=httpx.ConnectError("no route to host")),
    )
    body = ch_router.ConnectBody(name="b", bot_token="12345:abc")
    with pytest.raises(HTTPException) as ei:
        await ch_router.connect_account("telegram", body, _member(), FakeSession())
    assert ei.value.status_code == 422
    assert "網路連線失敗" in str(ei.value.detail)


async def test_telegram_invalid_token_still_422(monkeypatch, no_quota):
    monkeypatch.setattr(
        ch_router, "get_adapter",
        lambda ct: FakeAdapter(validate_exc=ValueError("Unauthorized")),
    )
    body = ch_router.ConnectBody(name="b", bot_token="bad")
    with pytest.raises(HTTPException) as ei:
        await ch_router.connect_account("telegram", body, _member(), FakeSession())
    assert ei.value.status_code == 422 and "telegram" in str(ei.value.detail)


# --------------------------------------------------------------------------
# connect: Meta credential key normalization + IG-Login mode
# --------------------------------------------------------------------------
async def test_messenger_page_token_copied_to_access_token(monkeypatch, no_quota, captured_creds):
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: FakeAdapter())
    body = ch_router.ConnectBody(name="Page", page_access_token="PT", page_id="123")
    out = await ch_router.connect_account(
        "messenger", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert out["external_id"] == "123"
    # both keys stored: adapters read access_token, the original key survives
    assert captured_creds["access_token"] == "PT"
    assert captured_creds["page_access_token"] == "PT"


async def test_instagram_via_page_mode(monkeypatch, no_quota, captured_creds):
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: FakeAdapter())
    body = ch_router.ConnectBody(name="ig", page_access_token="PT", page_id="99")
    out = await ch_router.connect_account(
        "instagram", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert out["external_id"] == "99"
    assert captured_creds["access_token"] == "PT"
    assert not out["config"].get("ig_login")


async def test_instagram_ig_login_mode_accepted(monkeypatch, no_quota, captured_creds):
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: FakeAdapter())
    body = ch_router.ConnectBody(
        name="ig", access_token="IG_TOK", ig_user_id="17841400000000001", login_type="ig"
    )
    out = await ch_router.connect_account(
        "instagram", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert out["external_id"] == "17841400000000001"
    assert out["config"]["ig_login"] is True  # adapter selects graph.instagram.com
    assert captured_creds["access_token"] == "IG_TOK"


async def test_instagram_missing_fields_is_clean_422(monkeypatch, no_quota):
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: FakeAdapter())
    body = ch_router.ConnectBody(name="ig")
    with pytest.raises(HTTPException) as ei:
        await ch_router.connect_account("instagram", body, _member(), FakeSession())
    assert ei.value.status_code == 422
    detail = str(ei.value.detail)
    assert "page_access_token" in detail and "ig_user_id" in detail


# --------------------------------------------------------------------------
# quota: category-aware keys + enabled-only counting
# --------------------------------------------------------------------------
async def test_quota_official_channels_key(monkeypatch):
    async def limits(session, redis, ws):
        return {"official_channels": 2, "hosted_devices": 5}

    monkeypatch.setattr(ch_router, "effective_limits", limits)
    monkeypatch.setattr(ch_router, "get_redis", lambda: None)
    s = FakeSession(results=[FakeResult(count=2)])
    with pytest.raises(HTTPException) as ei:
        await ch_router._check_channel_quota(s, _member(), "telegram_bot")
    assert ei.value.status_code == 402
    sql = str(s.stmts[0])
    assert "channel_accounts.enabled" in sql  # disabled accounts free their seat
    assert "NOT IN" in sql  # official = non-bridge, non-widget


async def test_quota_hosted_devices_key_for_bridges(monkeypatch):
    async def limits(session, redis, ws):
        return {"official_channels": 0, "hosted_devices": 1}

    monkeypatch.setattr(ch_router, "effective_limits", limits)
    monkeypatch.setattr(ch_router, "get_redis", lambda: None)
    s = FakeSession(results=[FakeResult(count=0)])
    await ch_router._check_channel_quota(s, _member(), "whatsapp_app")  # under cap → ok
    sql = str(s.stmts[0])
    assert "channel_accounts.enabled" in sql
    assert "NOT IN" not in sql and " IN " in sql  # bridge category only


async def test_quota_absent_or_unlimited_cap_passes(monkeypatch):
    async def limits(session, redis, ws):
        return {"hosted_devices": -1}

    monkeypatch.setattr(ch_router, "effective_limits", limits)
    monkeypatch.setattr(ch_router, "get_redis", lambda: None)
    s = FakeSession()
    await ch_router._check_channel_quota(s, _member(), "telegram_bot")  # no key → pass
    await ch_router._check_channel_quota(s, _member(), "whatsapp_app")  # -1 → unlimited
    assert s.stmts == []  # short-circuits before counting


# --------------------------------------------------------------------------
# widget CRUD: soft-delete filters + defaults
# --------------------------------------------------------------------------
async def test_list_widgets_filters_enabled_only():
    s = FakeSession(results=[FakeResult(rows=[])])
    await ch_router.list_widgets(_member(), s)
    assert "widgets.enabled" in str(s.stmts[0])


async def test_list_accounts_filters_enabled_only():
    s = FakeSession(results=[FakeResult(rows=[])])
    await ch_router.list_accounts(_member(), s)
    assert "channel_accounts.enabled" in str(s.stmts[0])


async def test_create_widget_counts_enabled_only_and_seeds_home(monkeypatch):
    async def limits(session, redis, ws):
        return {"widgets": 5}

    monkeypatch.setattr(ch_router, "effective_limits", limits)
    monkeypatch.setattr(ch_router, "get_redis", lambda: None)
    s = FakeSession(results=[FakeResult(count=0)])
    out = await ch_router.create_widget(
        ch_router.WidgetCreateBody(name="小綠"), _member(), s
    )
    assert "widgets.enabled" in str(s.stmts[0])  # quota ignores soft-deleted
    assert out["config"] == {"brand": {"name": "小綠"}, "home": {"enabled": True}}


async def test_create_widget_quota_reached_402(monkeypatch):
    async def limits(session, redis, ws):
        return {"widgets": 1}

    monkeypatch.setattr(ch_router, "effective_limits", limits)
    monkeypatch.setattr(ch_router, "get_redis", lambda: None)
    s = FakeSession(results=[FakeResult(count=1)])
    with pytest.raises(HTTPException) as ei:
        await ch_router.create_widget(ch_router.WidgetCreateBody(name="w"), _member(), s)
    assert ei.value.status_code == 402


async def test_remove_widget_account_disables_linked_widget():
    ws_id = uuid.uuid4()
    acct = ChannelAccount(
        workspace_id=ws_id, channel_type="widget", name="w", external_id="k", enabled=True
    )
    widget = Widget(workspace_id=ws_id, widget_key="k", name="w", enabled=True)
    s = FakeSession(gets={ChannelAccount: acct}, results=[FakeResult(rows=[widget])])
    await ch_router.remove_account(uuid.uuid4(), _member(ws_id), s)
    assert acct.enabled is False and acct.status == "disconnected"
    assert widget.enabled is False  # widget list + account list stay consistent
    assert s.committed


# --------------------------------------------------------------------------
# widget bootstrap: home config passthrough
# --------------------------------------------------------------------------
async def test_bootstrap_passes_home_through(monkeypatch):
    from apps.api.app.modules.widget import service as wsvc

    async def limits(session, redis, ws):
        return {"brand_removal": False}

    async def online(session, redis, ws):
        return False

    monkeypatch.setattr(wsvc, "effective_limits", limits)
    monkeypatch.setattr(wsvc, "any_agent_online", online)
    home = {"enabled": True, "banners": [{"image_url": "https://x/1.png"}], "reply_hint": "秒回"}
    w = Widget(widget_key="k", name="n", config={"home": home}, brand_removed=False)
    out = await wsvc.assemble_bootstrap(None, None, w)
    assert out["home"] == home
