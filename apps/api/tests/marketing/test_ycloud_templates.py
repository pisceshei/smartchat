"""YCloud template provider module: body↔components golden mapping, named-var
rejection, provider I/O contracts (MockTransport), review application, and the
widened sync loop's YCloud branch."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest

from apps.api.app.marketing import ycloud_templates as yt
from apps.api.app.marketing.ycloud_templates import (
    TemplateError,
    TemplateSubmitConflict,
    TemplateSubmitError,
    apply_template_review,
    body_to_components,
    components_representable,
    components_to_body,
    normalize_remote,
)

FULL_BODY = {
    "label": "測試",
    "header": {"type": "text", "text": "Order {{1}}"},
    "body": {"text": "Hi {{1}}, your order {{2}} shipped!"},
    "footer": {"text": "CHILL LOVE"},
    "buttons": {
        "type": "call_to_action",
        "items": [
            {"type": "url", "text": "查看訂單", "value": "https://chill.love/o/123"},
            {"type": "phone_number", "text": "致電", "value": "+85266577437"},
        ],
    },
}


def test_body_to_components_full_golden():
    comps = body_to_components(FULL_BODY)
    assert [c["type"] for c in comps] == ["HEADER", "BODY", "FOOTER", "BUTTONS"]
    header = comps[0]
    assert header["format"] == "TEXT"
    assert header["example"] == {"header_text": ["Sample 1"]}
    body = comps[1]
    assert body["text"] == "Hi {{1}}, your order {{2}} shipped!"
    assert body["example"] == {"body_text": [["Sample 1", "Sample 2"]]}
    assert comps[2]["text"] == "CHILL LOVE"
    buttons = comps[3]["buttons"]
    assert buttons[0] == {"type": "URL", "text": "查看訂單", "url": "https://chill.love/o/123"}
    assert buttons[1] == {"type": "PHONE_NUMBER", "text": "致電", "phone_number": "+85266577437"}


def test_body_to_components_quick_reply_buttons():
    comps = body_to_components(
        {
            "body": {"text": "Pick one"},
            "buttons": {"type": "quick_reply", "items": [{"text": "Yes"}, {"text": "No"}]},
        }
    )
    btns = comps[-1]["buttons"]
    assert btns == [
        {"type": "QUICK_REPLY", "text": "Yes"},
        {"type": "QUICK_REPLY", "text": "No"},
    ]


def test_named_variables_rejected():
    with pytest.raises(TemplateError, match="positional"):
        body_to_components({"body": {"text": "Hi {{name}}!"}})


def test_dynamic_url_button_rejected():
    with pytest.raises(TemplateError, match="dynamic URL"):
        body_to_components(
            {
                "body": {"text": "hi"},
                "buttons": {
                    "type": "call_to_action",
                    "items": [{"type": "url", "text": "訂單", "value": "https://x/o/{{1}}"}],
                },
            }
        )


def test_components_representable_truth_table():
    assert components_representable([{"type": "BODY", "text": "hi"}]) is True
    assert components_representable(
        [{"type": "HEADER", "format": "TEXT", "text": "H"}, {"type": "BODY", "text": "b"}]
    ) is True
    # media header can't be stored in our body schema
    assert components_representable([{"type": "HEADER", "format": "IMAGE"}]) is False
    # OTP / copy-code buttons unsupported
    assert components_representable(
        [{"type": "BUTTONS", "buttons": [{"type": "OTP", "text": "x"}]}]
    ) is False
    assert components_representable([{"type": "CAROUSEL"}]) is False
    assert components_representable(
        [{"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "y"}]}]
    ) is True


def test_missing_body_text_rejected():
    with pytest.raises(TemplateError, match="body.text"):
        body_to_components({"body": {"text": ""}})


def test_components_to_body_roundtrip():
    comps = body_to_components(FULL_BODY)
    back = components_to_body(comps)
    assert back["header"] == {"type": "text", "text": "Order {{1}}"}
    assert back["body"] == {"text": "Hi {{1}}, your order {{2}} shipped!"}
    assert back["footer"] == {"text": "CHILL LOVE"}
    assert back["buttons"]["type"] == "call_to_action"
    assert back["buttons"]["items"][0] == {
        "type": "url",
        "text": "查看訂單",
        "value": "https://chill.love/o/123",
    }


def test_normalize_remote_maps_official_id_and_reason():
    n = normalize_remote(
        {
            "officialTemplateId": "1234",
            "name": "hello",
            "language": "en",
            "status": "REJECTED",
            "category": "MARKETING",
            "reason": "SCAM",
            "components": [{"type": "BODY", "text": "x"}],
        }
    )
    assert n["id"] == "1234"
    assert n["rejected_reason"] == "SCAM"
    assert n["status"] == "REJECTED"


# --------------------------------------------------------------------------
# provider I/O (MockTransport)
# --------------------------------------------------------------------------
def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_submit_success_returns_remote_template():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("X-API-Key")
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200, json={"officialTemplateId": "T1", "status": "PENDING", "name": "hello"}
        )

    remote = await yt.submit_template(
        _client(handler),
        api_key="key1",
        waba_id="WABA1",
        name="hello",
        language="en",
        category="utility",
        components=[{"type": "BODY", "text": "hi"}],
    )
    assert remote["status"] == "PENDING"
    assert seen["url"].endswith("/whatsapp/templates")
    assert seen["auth"] == "key1"
    assert seen["body"]["wabaId"] == "WABA1"
    assert seen["body"]["category"] == "UTILITY"


async def test_submit_conflict_raises_typed():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"message": "template already exists"}})

    with pytest.raises(TemplateSubmitConflict):
        await yt.submit_template(
            _client(handler), api_key="k", waba_id="W", name="dup", language="en",
            category="marketing", components=[{"type": "BODY", "text": "x"}],
        )


async def test_submit_provider_error_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(TemplateSubmitError):
        await yt.submit_template(
            _client(handler), api_key="k", waba_id="W", name="x", language="en",
            category="marketing", components=[{"type": "BODY", "text": "x"}],
        )


async def test_fetch_ycloud_templates_pages_and_survives_errors():
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page"))
        calls.append(page)
        if page == 1:
            return httpx.Response(
                200, json={"items": [{"name": f"t{i}", "language": "en"} for i in range(100)]}
            )
        return httpx.Response(200, json={"items": [{"name": "last", "language": "en"}]})

    out = await yt.fetch_ycloud_templates(_client(handler), waba_id="W", api_key="k")
    assert len(out) == 101
    assert calls == [1, 2]

    def broken(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    assert await yt.fetch_ycloud_templates(_client(broken), waba_id="W", api_key="k") == []


# --------------------------------------------------------------------------
# review application (FakeSession)
# --------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *_a, **_k):
        return _FakeResult(self.rows)


def _tpl(name="hello", language="en", status="pending", meta_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        language=language,
        channel="whatsapp",
        approval_status=status,
        meta_template_id=meta_id,
        rejected_reason=None,
    )


async def test_apply_review_matches_by_name_language_and_approves():
    tpl = _tpl()
    changed = await apply_template_review(
        _FakeSession([tpl]),
        workspace_id="ws",
        event={"name": "hello", "language": "en", "status": "APPROVED", "reason": "NONE"},
    )
    assert changed is True
    assert tpl.approval_status == "approved"


async def test_apply_review_rejected_sets_reason():
    tpl = _tpl()
    await apply_template_review(
        _FakeSession([tpl]),
        workspace_id="ws",
        event={"name": "hello", "language": "en", "status": "REJECTED", "reason": "SCAM"},
    )
    assert tpl.approval_status == "rejected"
    assert tpl.rejected_reason == "SCAM"


async def test_apply_review_matches_by_official_template_id_first():
    by_id = _tpl(name="other", language="zh_TW", meta_id="T9")
    decoy = _tpl(name="hello", language="en")
    changed = await apply_template_review(
        _FakeSession([decoy, by_id]),
        workspace_id="ws",
        event={
            "officialTemplateId": "T9",
            "name": "hello",
            "language": "en",
            "status": "APPROVED",
        },
    )
    assert changed is True
    assert by_id.approval_status == "approved"
    assert decoy.approval_status == "pending"


async def test_apply_review_unknown_template_is_noop():
    assert (
        await apply_template_review(
            _FakeSession([]),
            workspace_id="ws",
            event={"name": "ghost", "language": "en", "status": "APPROVED"},
        )
        is False
    )
