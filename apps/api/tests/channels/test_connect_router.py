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
    def __init__(self, *, validate_exc: Exception | None = None, ig_account: str | None = None):
        self.validate_exc = validate_exc
        self.ig_account = ig_account
        self.subscribed = None

    async def validate_token(self, token):
        if self.validate_exc is not None:
            raise self.validate_exc
        return {"id": 42, "username": "bot"}

    async def set_webhook(self, *args):
        # telegram calls (token, url, secret); line_oa calls (access_token, url)
        self.set_webhook_args = args
        return True

    async def subscribe_page(self, access_token, page_id, fields=None):
        self.subscribed = (access_token, page_id)
        return True

    async def resolve_ig_account(self, access_token, page_id):
        return self.ig_account

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


async def test_instagram_via_page_mode_resolves_ig_external_id(monkeypatch, no_quota, captured_creds):
    # IG webhooks route by the linked IG Business account id, not the page id
    adapter = FakeAdapter(ig_account="17841400000000009")
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: adapter)
    body = ch_router.ConnectBody(name="ig", page_access_token="PT", page_id="99")
    out = await ch_router.connect_account(
        "instagram", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert out["external_id"] == "17841400000000009"  # resolved IG id, not page 99
    assert out["config"]["page_id"] == "99"  # page id kept for the send path
    assert captured_creds["access_token"] == "PT"
    assert adapter.subscribed == ("PT", "99")
    assert not out["config"].get("ig_login")


async def test_instagram_via_page_falls_back_to_page_id_when_ig_unresolved(
    monkeypatch, no_quota, captured_creds
):
    adapter = FakeAdapter(ig_account=None)  # link can't be resolved
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: adapter)
    body = ch_router.ConnectBody(name="ig", page_access_token="PT", page_id="99")
    out = await ch_router.connect_account(
        "instagram", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert out["external_id"] == "99"  # falls back so connect still lands


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
# connect: email modal fields → adapter credential keys (the send/auth path
# reads host/user/password from ENCRYPTED credentials, so the flat modal body
# must be remapped there — a secret-hint split alone strands host/user/auth).
# --------------------------------------------------------------------------
async def test_email_connect_maps_modal_fields_to_credentials(monkeypatch, no_quota, captured_creds):
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: FakeAdapter())
    body = ch_router.ConnectBody(
        name="Support", address="Support@Example.com", auth_type="password",
        imap_host="imap.example.com", imap_port=993, imap_ssl=True,
        smtp_host="smtp.example.com", smtp_port=465, smtp_ssl=True,
        username="support@example.com", password="app-pw",
    )
    out = await ch_router.connect_account(
        "email", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert out["external_id"] == "support@example.com"  # lowercased
    assert captured_creds["imap_host"] == "imap.example.com"
    assert captured_creds["smtp_host"] == "smtp.example.com"
    assert captured_creds["imap_user"] == captured_creds["smtp_user"] == "support@example.com"
    assert captured_creds["imap_password"] == captured_creds["smtp_password"] == "app-pw"
    assert captured_creds["smtp_tls"] is True
    # secrets never leak into the JSONB config (which is echoed back)
    assert all("password" not in k for k in out["config"])
    assert out["config"]["address"] == "support@example.com"


async def test_email_connect_oauth2_derives_token_endpoint(monkeypatch, no_quota, captured_creds):
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: FakeAdapter())
    body = ch_router.ConnectBody(
        name="Gmail", address="me@gmail.com", auth_type="oauth2", oauth_provider="gmail",
        imap_host="imap.gmail.com", smtp_host="smtp.gmail.com", username="me@gmail.com",
        oauth_access_token="AT", oauth_refresh_token="RT",
        oauth_client_id="cid", oauth_client_secret="csec",
    )
    await ch_router.connect_account(
        "email", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert captured_creds["auth_type"] == "oauth2"
    assert captured_creds["oauth_access_token"] == "AT"
    assert captured_creds["oauth_refresh_token"] == "RT"
    assert captured_creds["oauth_client_id"] == "cid"
    assert captured_creds["oauth_client_secret"] == "csec"
    # provider → token endpoint derived so refresh_credentials can run
    assert captured_creds["oauth_token_endpoint"] == "https://oauth2.googleapis.com/token"
    assert "imap_password" not in captured_creds  # oauth mode: no password path


# --------------------------------------------------------------------------
# connect: line_oa — mirror channel_access_token→access_token (adapters read
# access_token) + auto-register webhook + surface URL/secret to the operator.
# --------------------------------------------------------------------------
async def test_line_oa_connect_mirrors_token_and_surfaces_webhook(monkeypatch, no_quota, captured_creds):
    adapter = FakeAdapter()
    monkeypatch.setattr(ch_router, "get_adapter", lambda ct: adapter)
    body = ch_router.ConnectBody(
        name="OA", channel_id="1656500000", channel_secret="SEC", channel_access_token="LONG_TOKEN",
    )
    out = await ch_router.connect_account(
        "line_oa", body, _member(), FakeSession(results=[FakeResult(scalar=None)])
    )
    assert out["external_id"] == "1656500000"
    # adapters read access_token; modal posts channel_access_token
    assert captured_creds["access_token"] == "LONG_TOKEN"
    assert adapter.set_webhook_args[0] == "LONG_TOKEN"  # set_webhook(access_token, url)
    assert adapter.set_webhook_args[1].endswith(f"/hooks/line/{out['webhook_secret']}")
    assert out["webhook_url"].endswith(f"/hooks/line/{out['webhook_secret']}")


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

    async def no_social(session, workspace_id):
        return []

    monkeypatch.setattr(wsvc, "effective_limits", limits)
    monkeypatch.setattr(wsvc, "any_agent_online", online)
    monkeypatch.setattr(wsvc, "_connected_social_accounts", no_social)
    home = {"enabled": True, "banners": [{"image_url": "https://x/1.png"}], "reply_hint": "秒回"}
    w = Widget(widget_key="k", name="n", config={"home": home}, brand_removed=False)
    out = await wsvc.assemble_bootstrap(None, None, w)
    assert out["home"] == home
    assert out["social"] == {"enabled": True, "channels": []}
