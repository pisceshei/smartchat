"""Widget social-entry derivation: per-channel deep-link/copy mapping + the
per-widget visibility filter (auto-show connected channels; personal/internal
opt-in). The mapping is pure; _assemble_social is driven with a stub account
list. Guards the privacy contract — only a derived link/handle is ever exposed
on the public bootstrap, never external_id/health verbatim."""
from __future__ import annotations

from types import SimpleNamespace

from apps.api.app.modules.widget import service as wsvc


def _acct(channel_type, *, external_id="", health=None):
    return SimpleNamespace(
        channel_type=channel_type, external_id=external_id, health=health or {}
    )


# ---- per-channel derivation ---------------------------------------------
def test_whatsapp_bsp_link_from_external_id():
    e = wsvc.channel_contact_entry(_acct("whatsapp_bsp", external_id="+85266577437"))
    assert e == {
        "channel_type": "whatsapp_bsp", "label": "WhatsApp", "kind": "link",
        "url": "https://wa.me/85266577437", "icon_key": "whatsapp",
    }


def test_whatsapp_cloud_link_from_health_display_number():
    e = wsvc.channel_contact_entry(
        _acct("whatsapp_cloud", external_id="123_phone_id",
              health={"display_phone_number": "+852 6657 7437"})
    )
    assert e["url"] == "https://wa.me/85266577437"  # not the internal phone_number_id


def test_telegram_link_from_health_username():
    e = wsvc.channel_contact_entry(
        _acct("telegram_bot", external_id="42", health={"username": "@ShopBot"})
    )
    assert e["url"] == "https://t.me/ShopBot"


def test_line_link_from_basic_id():
    e = wsvc.channel_contact_entry(_acct("line_oa", external_id="ch", health={"basic_id": "@shop"}))
    assert e["url"] == "https://line.me/R/ti/p/@shop"


def test_messenger_link_from_page_id():
    assert wsvc.channel_contact_entry(_acct("messenger", external_id="98765"))["url"] == (
        "https://m.me/98765"
    )


def test_email_mailto():
    assert wsvc.channel_contact_entry(_acct("email", external_id="hi@chill.love"))["url"] == (
        "mailto:hi@chill.love"
    )


def test_wechat_kf_never_leaks_corp_id():
    # external_id is the internal WeCom corp_id — must NOT be exposed publicly,
    # and there is no usable public handle → no entry.
    assert (
        wsvc.channel_contact_entry(
            _acct("wechat_kf", external_id="ww9f3a1b2c", health={"corp_id": "ww9f3a1b2c"})
        )
        is None
    )
    # a genuine captured contact handle DOES surface as a copy entry
    e = wsvc.channel_contact_entry(_acct("wechat_kf", external_id="ww9f", health={"contact": "chill_kf"}))
    assert e["kind"] == "copy" and e["value"] == "chill_kf"


def test_unresolvable_returns_none():
    # cloud phone probe failed → no display number → no link
    assert wsvc.channel_contact_entry(_acct("whatsapp_cloud", external_id="pid", health={})) is None
    # slack has no visitor deep link at all
    assert wsvc.channel_contact_entry(_acct("slack", external_id="T1")) is None
    # instagram has no stored public @username → no entry (never leaks the id)
    assert wsvc.channel_contact_entry(_acct("instagram", external_id="17841400000000009")) is None


# ---- _assemble_social filter / order ------------------------------------
def _stub_accounts(monkeypatch, accts):
    async def fake(session, ws):
        return accts

    monkeypatch.setattr(wsvc, "_connected_social_accounts", fake)


async def test_assemble_social_auto_shows_connected(monkeypatch):
    _stub_accounts(monkeypatch, [
        _acct("whatsapp_bsp", external_id="+85266577437"),
        _acct("telegram_bot", external_id="1", health={"username": "bot"}),
    ])
    out = await wsvc._assemble_social(None, "ws", {})
    assert out["enabled"] is True
    assert [c["channel_type"] for c in out["channels"]] == ["whatsapp_bsp", "telegram_bot"]


async def test_assemble_social_master_toggle_off():
    assert await wsvc._assemble_social(None, "ws", {"enabled": False}) == {
        "enabled": False, "channels": []
    }


async def test_assemble_social_hidden_and_default_off(monkeypatch):
    _stub_accounts(monkeypatch, [
        _acct("whatsapp_bsp", external_id="+1"),
        _acct("telegram_bot", external_id="1", health={"username": "b"}),
        _acct("whatsapp_app", external_id="dev", health={"phone": "+2"}),
    ])
    # telegram hidden; whatsapp_app is default-off (personal number) → not shown
    out = await wsvc._assemble_social(None, "ws", {"hidden": ["telegram_bot"]})
    assert [c["channel_type"] for c in out["channels"]] == ["whatsapp_bsp"]
    # opt the personal number in explicitly
    out2 = await wsvc._assemble_social(None, "ws", {"shown": ["whatsapp_app"]})
    assert {c["channel_type"] for c in out2["channels"]} == {
        "whatsapp_bsp", "telegram_bot", "whatsapp_app"
    }


async def test_assemble_social_order_and_label(monkeypatch):
    _stub_accounts(monkeypatch, [
        _acct("whatsapp_bsp", external_id="+1"),
        _acct("telegram_bot", external_id="1", health={"username": "b"}),
    ])
    out = await wsvc._assemble_social(
        None, "ws", {"order": ["telegram_bot", "whatsapp_bsp"], "labels": {"whatsapp_bsp": "客服"}}
    )
    assert [c["channel_type"] for c in out["channels"]] == ["telegram_bot", "whatsapp_bsp"]
    wa = next(c for c in out["channels"] if c["channel_type"] == "whatsapp_bsp")
    assert wa["label"] == "客服"
