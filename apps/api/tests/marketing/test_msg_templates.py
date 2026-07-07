"""Template validation, variable substitution, SMS segmentation, MJML/HTML,
WhatsApp component building, approval-status mapping."""
from __future__ import annotations

import types

import pytest

from apps.api.app.marketing import wa_template_sync as wa
from apps.api.app.modules.msg_templates import service as svc


def _contact(**kw):
    base = {"display_name": "Ada", "city": "Central", "custom": {"tier": "gold"},
            "email": "a@b.co", "phone": "+1"}
    base.update(kw)
    return types.SimpleNamespace(**base)


# ---- validation ----------------------------------------------------------
def test_whatsapp_valid():
    cols = svc.validate_and_extract("whatsapp", {
        "name": "order_update", "category": "marketing", "language": "en",
        "body": {"text": "Hello {{1}}"},
    })
    assert cols.name == "order_update" and cols.category == "marketing"
    assert cols.approval_status == "draft"


def test_whatsapp_bad_name_rejected():
    with pytest.raises(svc.TemplateError):
        svc.validate_and_extract("whatsapp", {
            "name": "Order Update", "category": "marketing", "language": "en",
            "body": {"text": "x"},
        })


def test_whatsapp_bad_category():
    with pytest.raises(svc.TemplateError):
        svc.validate_and_extract("whatsapp", {
            "name": "x", "category": "promo", "language": "en", "body": {"text": "x"},
        })


def test_email_compiles_html():
    cols = svc.validate_and_extract("email", {
        "name": "n", "subject": "Hi", "mjml_source": "<mj-text>Hello</mj-text>",
    })
    assert "<p>" in cols.body["html"]


def test_sms_requires_text():
    with pytest.raises(svc.TemplateError):
        svc.validate_and_extract("sms", {"name": "n"})


def test_messenger_requires_payload():
    with pytest.raises(svc.TemplateError):
        svc.validate_and_extract("messenger", {"name": "n"})


# ---- substitution --------------------------------------------------------
def test_substitute_maps_and_direct_fields():
    out = svc.substitute("Hi {{name}} from {{city}}", {"name": {"field": "display_name"}}, _contact())
    assert out == "Hi Ada from Central"


def test_substitute_fallback():
    out = svc.substitute("Hi {{name}}", {"name": {"field": "missing", "fallback": "there"}}, _contact())
    assert out == "Hi there"


def test_substitute_literal():
    out = svc.substitute("Code {{c}}", {"c": "ABC"}, None)
    assert out == "Code ABC"


# ---- SMS segmentation ----------------------------------------------------
def test_sms_gsm7_single_segment():
    seg = svc.sms_segments("Hello world")
    assert seg.encoding == "GSM-7" and seg.segments == 1 and seg.per_segment == 160


def test_sms_gsm7_multipart():
    seg = svc.sms_segments("a" * 200)
    assert seg.encoding == "GSM-7" and seg.segments == 2 and seg.per_segment == 153


def test_sms_ucs2_detected():
    seg = svc.sms_segments("你好世界")
    assert seg.encoding == "UCS-2" and seg.per_segment == 70


def test_sms_gsm7_extension_char_costs_two():
    seg = svc.sms_segments("€")  # extension char
    assert seg.encoding == "GSM-7" and seg.char_count == 2


# ---- MJML / html ---------------------------------------------------------
def test_compile_mjml_maps_tags():
    html = svc.compile_mjml("<mjml><mj-body><mj-text>Hi</mj-text></mj-body></mjml>")
    assert "<p>Hi</p>" in html and "<html>" in html


def test_compile_mjml_passthrough_html():
    assert svc.compile_mjml("<div>x</div>") == "<div>x</div>"


def test_html_to_text_strips_tags():
    assert "Hi" in svc.html_to_text("<p>Hi</p><style>x{}</style>")
    assert "x{}" not in svc.html_to_text("<p>Hi</p><style>x{}</style>")


# ---- WhatsApp components -------------------------------------------------
def test_build_wa_components_positional():
    body = {"header": {"type": "text", "text": "Hi {{1}}"}, "body": {"text": "Order {{2}} ready"}}
    comps = svc.build_wa_components(body, {"1": {"field": "display_name"}, "2": "A123"}, _contact())
    types_ = [c["type"] for c in comps["components"]]
    assert "header" in types_ and "body" in types_
    body_comp = next(c for c in comps["components"] if c["type"] == "body")
    assert body_comp["parameters"][0]["text"] == "A123"


# ---- WA approval status map ----------------------------------------------
def test_wa_status_mapping():
    assert wa.map_meta_status("APPROVED") == "approved"
    assert wa.map_meta_status("PENDING") == "pending"
    assert wa.map_meta_status("REJECTED") == "rejected"
    assert wa.map_meta_status("PAUSED") == "paused"
    assert wa.map_meta_status("DISABLED") == "disabled"
    assert wa.map_meta_status("IN_APPEAL") == "pending"
    assert wa.map_meta_status(None) == "pending"
    assert wa.map_meta_status("something_new") == "pending"
