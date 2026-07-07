"""Split-link service pure bits: slug, deep-link building, prefill, targets."""
from __future__ import annotations

import pytest

from apps.api.app.modules.split_links import service as svc


def test_base62_length_and_charset():
    slug = svc.base62(7)
    assert len(slug) == 7
    assert all(c in svc._B62 for c in slug)


def test_tracking_code_length():
    assert len(svc.tracking_code()) == svc.TRACKING_LEN


def test_build_deeplink_whatsapp():
    url = svc.build_deeplink("whatsapp", {"phone": "85212345678"}, "Hi there")
    assert url.startswith("https://wa.me/85212345678")
    assert "text=Hi%20there" in url


def test_build_deeplink_telegram():
    url = svc.build_deeplink("telegram", {"username": "mybot"}, "hey")
    assert url.startswith("https://t.me/mybot")


def test_build_deeplink_explicit_url_wins():
    url = svc.build_deeplink("whatsapp", {"url": "https://example.com/x"}, "hi")
    assert url.startswith("https://example.com/x")
    assert "text=hi" in url


def test_render_prefill_substitutes_code():
    assert svc.render_prefill("Hi ref {{code}}", "AB12CD34") == "Hi ref AB12CD34"


def test_render_prefill_no_placeholder():
    assert svc.render_prefill("Hi", "X") == "Hi"
    assert svc.render_prefill(None, "X") == ""


def test_validate_targets_normalizes():
    out = svc.validate_targets([{"phone": "+85212345678", "weight": 2}], channel_type="whatsapp")
    assert out[0]["phone"] == "85212345678" and out[0]["weight"] == 2 and out[0]["enabled"] is True


def test_validate_targets_requires_endpoint():
    with pytest.raises(svc.SplitLinkError):
        svc.validate_targets([{"weight": 1}], channel_type="whatsapp")


def test_validate_targets_empty():
    with pytest.raises(svc.SplitLinkError):
        svc.validate_targets([], channel_type="whatsapp")


def test_link_config_shape():
    class L:
        id = "11111111-1111-1111-1111-111111111111"
        workspace_id = "22222222-2222-2222-2222-222222222222"
        slug = "abc1234"
        channel_type = "whatsapp"
        strategy = "random"
        targets = [{"phone": "1"}]
        prefill_text = "hi {{code}}"
        status = "active"

    cfg = svc.link_config(L())
    assert cfg["slug"] == "abc1234" and cfg["strategy"] == "random"
    assert cfg["targets"] == [{"phone": "1"}]
