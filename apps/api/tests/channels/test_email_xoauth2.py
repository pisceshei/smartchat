"""Email XOAUTH2: SASL string construction + IMAP/SMTP auth-path selection +
OAuth2 refresh. All pure/faked — no network, no IMAP/SMTP server.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest
from aiosmtplib.auth import auth_xoauth2_encode

from apps.api.app.channels.adapters.email_imap import (
    EmailAdapter,
    _authenticate_imap,
    _authenticate_smtp,
    build_xoauth2,
    uses_oauth2,
)


# --------------------------------------------------------------------------
# 1. SASL XOAUTH2 string construction
# --------------------------------------------------------------------------
def test_build_xoauth2_matches_spec():
    s = build_xoauth2("user@example.com", "tok123")
    # decodes to the exact SASL initial-client-response with 0x01 separators
    assert base64.b64decode(s) == b"user=user@example.com\x01auth=Bearer tok123\x01\x01"


def test_build_xoauth2_matches_aiosmtplib_encoder():
    # equivalence with the library's own encoder (Gmail/Outlook wire format)
    for user, token in [
        ("a@b.com", "ya29.abc"),
        ("mailbox@corp.onmicrosoft.com", "EwB0Aq.long.token-_="),
    ]:
        assert build_xoauth2(user, token) == auth_xoauth2_encode(user, token).decode("ascii")


# --------------------------------------------------------------------------
# 2. auth-path selection helper
# --------------------------------------------------------------------------
def test_uses_oauth2_true_only_with_flag_and_token():
    assert uses_oauth2({"auth_type": "oauth2", "oauth_access_token": "t"}) is True


def test_uses_oauth2_false_for_password_creds():
    assert uses_oauth2({"imap_user": "u", "imap_password": "p"}) is False


def test_uses_oauth2_false_when_flag_without_token():
    assert uses_oauth2({"auth_type": "oauth2"}) is False


def test_uses_oauth2_false_when_token_without_flag():
    # a stray token must not silently switch auth mode
    assert uses_oauth2({"oauth_access_token": "t"}) is False


# --------------------------------------------------------------------------
# 3. IMAP auth-path selection (fake client, no network)
# --------------------------------------------------------------------------
class _FakeIMAP:
    def __init__(self, result: str = "OK"):
        self.result = result
        self.calls: list[tuple] = []

    async def login(self, user, password):
        self.calls.append(("login", user, password))
        return SimpleNamespace(result=self.result, lines=[b"authenticated"])

    async def xoauth2(self, user, token):
        self.calls.append(("xoauth2", user, token))
        return SimpleNamespace(result=self.result, lines=[b"authenticated"])


async def test_imap_password_path():
    fake = _FakeIMAP()
    await _authenticate_imap(fake, {"imap_user": "u@x.com", "imap_password": "pw"})
    assert fake.calls == [("login", "u@x.com", "pw")]


async def test_imap_oauth_path_uses_xoauth2_with_oauth_user():
    fake = _FakeIMAP()
    await _authenticate_imap(
        fake,
        {"auth_type": "oauth2", "oauth_user": "box@gmail.com", "oauth_access_token": "ya29"},
    )
    assert fake.calls == [("xoauth2", "box@gmail.com", "ya29")]


async def test_imap_oauth_user_falls_back_to_email():
    fake = _FakeIMAP()
    await _authenticate_imap(
        fake, {"auth_type": "oauth2", "email": "box@gmail.com", "oauth_access_token": "ya29"}
    )
    assert fake.calls == [("xoauth2", "box@gmail.com", "ya29")]


async def test_imap_auth_failure_raises():
    fake = _FakeIMAP(result="NO")
    with pytest.raises(PermissionError):
        await _authenticate_imap(fake, {"imap_user": "u", "imap_password": "bad"})


# --------------------------------------------------------------------------
# 4. SMTP auth-path selection (fake client, no network)
# --------------------------------------------------------------------------
class _FakeSMTP:
    def __init__(self):
        self.calls: list[tuple] = []

    async def login(self, user, password):
        self.calls.append(("login", user, password))

    async def auth_xoauth2(self, user, token):
        self.calls.append(("auth_xoauth2", user, token))


async def test_smtp_password_path():
    fake = _FakeSMTP()
    await _authenticate_smtp(fake, {"imap_password": "p"}, "u@x.com", "pw")
    assert fake.calls == [("login", "u@x.com", "pw")]


async def test_smtp_oauth_path():
    fake = _FakeSMTP()
    await _authenticate_smtp(
        fake,
        {"auth_type": "oauth2", "oauth_user": "box@gmail.com", "oauth_access_token": "ya29"},
        "smtp-user",
        "ignored",
    )
    assert fake.calls == [("auth_xoauth2", "box@gmail.com", "ya29")]


async def test_smtp_no_credentials_skips_auth():
    # unauthenticated relay: neither login nor xoauth2 is invoked
    fake = _FakeSMTP()
    await _authenticate_smtp(fake, {}, "", "")
    assert fake.calls == []


# --------------------------------------------------------------------------
# 5. OAuth2 refresh_credentials (mocked http, no network)
# --------------------------------------------------------------------------
def _adapter_with_handler(handler) -> EmailAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EmailAdapter(http=client)


async def test_refresh_credentials_non_oauth_returns_none():
    adapter = EmailAdapter()
    assert await adapter.refresh_credentials(None, {"imap_password": "p"}) is None


async def test_refresh_credentials_missing_fields_returns_none():
    adapter = EmailAdapter()
    # auth_type set but no endpoint/refresh_token/client_id
    assert await adapter.refresh_credentials(None, {"auth_type": "oauth2"}) is None


async def test_refresh_credentials_success():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            200, json={"access_token": "NEW_ACCESS", "expires_in": 3600}
        )

    adapter = _adapter_with_handler(handler)
    creds = {
        "auth_type": "oauth2",
        "oauth_access_token": "OLD",
        "oauth_refresh_token": "RT",
        "oauth_token_endpoint": "https://oauth2.googleapis.com/token",
        "oauth_client_id": "cid",
        "oauth_client_secret": "csec",
    }
    updated = await adapter.refresh_credentials(None, creds)
    assert updated is not None
    assert updated["oauth_access_token"] == "NEW_ACCESS"
    assert updated["oauth_refresh_token"] == "RT"  # preserved when not rotated
    assert "oauth_token_expires_at" in updated
    # the grant is a refresh_token grant carrying client credentials
    assert captured["url"] == "https://oauth2.googleapis.com/token"
    assert "grant_type=refresh_token" in captured["body"]
    assert "refresh_token=RT" in captured["body"]
    assert "client_secret=csec" in captured["body"]


async def test_refresh_credentials_rotates_refresh_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": "NEW", "refresh_token": "RT2", "expires_in": 60}
        )

    adapter = _adapter_with_handler(handler)
    creds = {
        "auth_type": "oauth2",
        "oauth_refresh_token": "RT1",
        "oauth_token_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "oauth_client_id": "cid",
    }
    updated = await adapter.refresh_credentials(None, creds)
    assert updated is not None
    assert updated["oauth_refresh_token"] == "RT2"


async def test_refresh_credentials_http_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    adapter = _adapter_with_handler(handler)
    creds = {
        "auth_type": "oauth2",
        "oauth_refresh_token": "RT",
        "oauth_token_endpoint": "https://oauth2.googleapis.com/token",
        "oauth_client_id": "cid",
    }
    assert await adapter.refresh_credentials(None, creds) is None
