"""YCloud webhook route: signature verification truth table, business-number
routing (+E.164 normalization), template-review application, and the
200-on-drop platform convention. All faked — no YCloud contact."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.db import get_session
from apps.api.app.modules.hooks import ycloud as yc_hook
from apps.api.app.modules.hooks.ycloud import verify_ycloud_signature

SECRET = "whsec_test"


def _sign(secret: str, body: bytes, ts: int | None = None) -> str:
    t = ts if ts is not None else int(time.time())
    s = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},s={s}"


# --------------------------------------------------------------------------
# 1. signature truth table
# --------------------------------------------------------------------------
def test_signature_valid():
    body = b'{"type":"x"}'
    assert verify_ycloud_signature(SECRET, body, _sign(SECRET, body)) is True


def test_signature_bad_hmac():
    body = b'{"type":"x"}'
    assert verify_ycloud_signature(SECRET, body, _sign("other-secret", body)) is False


def test_signature_stale_timestamp_past_and_future():
    body = b"{}"
    now = int(time.time())
    assert verify_ycloud_signature(SECRET, body, _sign(SECRET, body, now - 600)) is False
    assert verify_ycloud_signature(SECRET, body, _sign(SECRET, body, now + 600)) is False
    # inside tolerance passes
    assert verify_ycloud_signature(SECRET, body, _sign(SECRET, body, now - 100)) is True


def test_signature_missing_or_malformed_header():
    body = b"{}"
    assert verify_ycloud_signature(SECRET, body, None) is False
    assert verify_ycloud_signature(SECRET, body, "") is False
    assert verify_ycloud_signature(SECRET, body, "s=deadbeef") is False  # no t
    assert verify_ycloud_signature(SECRET, body, "t=123") is False  # no s
    assert verify_ycloud_signature(SECRET, body, "t=abc,s=deadbeef") is False  # non-int t


def test_signature_empty_secret_never_verifies():
    body = b"{}"
    assert verify_ycloud_signature("", body, _sign(SECRET, body)) is False


# --------------------------------------------------------------------------
# 2. route behavior (TestClient + monkeypatched lookups)
# --------------------------------------------------------------------------
def _acct(external_id: str = "+85266577437", *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id="acc-1",
        workspace_id="ws-1",
        channel_type="whatsapp_bsp",
        external_id=external_id,
        enabled=enabled,
        config={"waba_id": "WABA1"},
    )


class _FakeSession(SimpleNamespace):
    async def commit(self):  # the template-review branch commits
        return None


async def _no_session():
    yield _FakeSession()


def _client(monkeypatch, *, acct, secret: str = "", review_calls=None):
    enqueued: list = []
    lookups: list = []

    async def fake_by_external(session, channel_type, external_id):
        lookups.append((channel_type, external_id))
        if acct is not None and external_id in (acct.external_id, acct.external_id.lstrip("+")):
            return acct
        return None

    async def fake_get_credentials(session, a):
        return {"webhook_secret": secret} if secret else {}

    async def fake_enqueue(a, payload):
        enqueued.append((a, payload))

    async def fake_apply_review(session, *, workspace_id, event):
        if review_calls is not None:
            review_calls.append((workspace_id, event))
        return True

    monkeypatch.setattr(yc_hook, "_account_by_external", fake_by_external)
    monkeypatch.setattr(yc_hook, "get_credentials", fake_get_credentials)
    monkeypatch.setattr(yc_hook, "_enqueue", fake_enqueue)
    monkeypatch.setattr(yc_hook.ycloud_templates, "apply_template_review", fake_apply_review)

    async def fake_accounts_by_waba(session, waba_id):
        return [acct] if acct is not None and acct.enabled and str(waba_id) == "WABA1" else []

    monkeypatch.setattr(yc_hook, "_accounts_by_waba", fake_accounts_by_waba)

    app = FastAPI()
    app.include_router(yc_hook.router)
    app.dependency_overrides[get_session] = _no_session
    return TestClient(app), enqueued, lookups


def _inbound_event(to: str = "85266577437") -> dict:
    return {
        "id": "evt_1",
        "type": "whatsapp.inbound_message.received",
        "apiVersion": "v2",
        "whatsappInboundMessage": {
            "id": "m1",
            "wamid": "wamid.X1",
            "from": "85299998888",
            "to": to,
            "type": "text",
            "text": {"body": "hi"},
        },
    }


def test_inbound_routed_by_to_with_plus_normalization(monkeypatch):
    # account stores +E.164; the event carries bare digits
    client, enqueued, lookups = _client(monkeypatch, acct=_acct("+85266577437"))
    r = client.post("/hooks/ycloud", json=_inbound_event(to="85266577437"))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(enqueued) == 1
    # raw envelope passes through untouched — parse_inbound consumes it as-is
    assert enqueued[0][1]["type"] == "whatsapp.inbound_message.received"
    assert lookups[0] == ("whatsapp_bsp", "+85266577437")


def test_status_routed_by_from(monkeypatch):
    client, enqueued, _ = _client(monkeypatch, acct=_acct("+85266577437"))
    r = client.post(
        "/hooks/ycloud",
        json={
            "type": "whatsapp.message.updated",
            "whatsappMessage": {"wamid": "wamid.X2", "from": "85266577437", "status": "read"},
        },
    )
    assert r.status_code == 200
    assert len(enqueued) == 1


def test_bad_signature_200_drop_when_secret_stored(monkeypatch):
    # fail-closed (event not processed) but 200-drop, not 403 — denies a
    # presence/signature oracle and avoids YCloud endpoint suspension
    client, enqueued, _ = _client(monkeypatch, acct=_acct(), secret=SECRET)
    body = json.dumps(_inbound_event()).encode()
    r = client.post(
        "/hooks/ycloud",
        content=body,
        headers={"Content-Type": "application/json", "YCloud-Signature": "t=1,s=bad"},
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert enqueued == []


def test_good_signature_accepted_when_secret_stored(monkeypatch):
    client, enqueued, _ = _client(monkeypatch, acct=_acct(), secret=SECRET)
    body = json.dumps(_inbound_event()).encode()
    r = client.post(
        "/hooks/ycloud",
        content=body,
        headers={"Content-Type": "application/json", "YCloud-Signature": _sign(SECRET, body)},
    )
    assert r.status_code == 200
    assert len(enqueued) == 1


def test_no_stored_secret_accepts_with_warning(monkeypatch):
    client, enqueued, _ = _client(monkeypatch, acct=_acct(), secret="")
    r = client.post("/hooks/ycloud", json=_inbound_event())
    assert r.status_code == 200
    assert len(enqueued) == 1


def test_unmatched_number_200_drop(monkeypatch):
    client, enqueued, _ = _client(monkeypatch, acct=None)
    r = client.post("/hooks/ycloud", json=_inbound_event(to="10000000000"))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert enqueued == []


def test_disabled_account_200_drop(monkeypatch):
    client, enqueued, _ = _client(monkeypatch, acct=_acct(enabled=False))
    r = client.post("/hooks/ycloud", json=_inbound_event())
    assert r.status_code == 200
    assert enqueued == []


def test_unknown_event_type_200_noop(monkeypatch):
    client, enqueued, _ = _client(monkeypatch, acct=_acct())
    r = client.post("/hooks/ycloud", json={"type": "whatsapp.payment.updated", "x": 1})
    assert r.status_code == 200
    assert enqueued == []


def test_template_reviewed_applies_directly_not_via_ingress(monkeypatch):
    review_calls: list = []
    client, enqueued, _ = _client(monkeypatch, acct=_acct(), review_calls=review_calls)
    r = client.post(
        "/hooks/ycloud",
        json={
            "type": "whatsapp.template.reviewed",
            "whatsappTemplate": {
                "wabaId": "WABA1",
                "name": "order_update",
                "language": "en",
                "status": "APPROVED",
                "reason": "NONE",
            },
        },
    )
    assert r.status_code == 200
    assert enqueued == []  # never enqueued to ingress
    assert len(review_calls) == 1
    assert review_calls[0][1]["name"] == "order_update"


def test_invalid_json_400(monkeypatch):
    client, _, _ = _client(monkeypatch, acct=_acct())
    r = client.post(
        "/hooks/ycloud", content=b"not-json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400


def test_template_reviewed_fans_out_to_all_workspaces_on_shared_waba(monkeypatch):
    # two accounts (different workspaces) share one WABA — the review must
    # reach BOTH workspaces, not an arbitrary one
    a1 = _acct("+85211110000")
    a1.workspace_id = "ws-1"
    a2 = _acct("+85222220000")
    a2.workspace_id = "ws-2"
    review_calls: list = []

    async def fake_accounts_by_waba(session, waba_id):
        return [a1, a2] if str(waba_id) == "WABA1" else []

    async def fake_get_credentials(session, a):
        return {}

    async def fake_apply(session, *, workspace_id, event):
        review_calls.append(workspace_id)
        return True

    monkeypatch.setattr(yc_hook, "_accounts_by_waba", fake_accounts_by_waba)
    monkeypatch.setattr(yc_hook, "get_credentials", fake_get_credentials)
    monkeypatch.setattr(yc_hook.ycloud_templates, "apply_template_review", fake_apply)

    app = FastAPI()
    app.include_router(yc_hook.router)
    app.dependency_overrides[get_session] = _no_session
    client = TestClient(app)

    r = client.post(
        "/hooks/ycloud",
        json={
            "type": "whatsapp.template.reviewed",
            "whatsappTemplate": {"wabaId": "WABA1", "name": "t", "language": "en", "status": "APPROVED"},
        },
    )
    assert r.status_code == 200
    assert set(review_calls) == {"ws-1", "ws-2"}
