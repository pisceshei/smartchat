"""Phase 4 foundation: capability matrix flags, unknown-channel fallback,
degradation for a text-only channel, and the connect_validate default.
"""
from __future__ import annotations

from py_contracts.content import (
    MediaBlock,
    MessageContent,
    QuickButton,
    QuickButtonsBlock,
    TextBlock,
)

from apps.api.app.channels.base import (
    CAPABILITIES,
    BaseAdapter,
    ConnectResult,
    capabilities_for,
    degrade_content,
)


def test_new_channels_present_with_expected_shape():
    for ct in ("slack", "vk", "wechat_kf", "wecom", "tiktok_business", "youtube", "zalo_app"):
        assert ct in CAPABILITIES

    # Slack: Block Kit buttons + cards, no session window.
    assert CAPABILITIES["slack"].buttons and CAPABILITIES["slack"].product_cards
    assert CAPABILITIES["slack"].session_window_hours is None
    # VK: keyboards + typing.
    assert CAPABILITIES["vk"].buttons and CAPABILITIES["vk"].typing_indicator
    # WeChat KF: menu buttons + 48h session window.
    assert CAPABILITIES["wechat_kf"].buttons
    assert CAPABILITIES["wechat_kf"].session_window_hours == 48
    # WeCom: rich cards, no interactive buttons.
    assert CAPABILITIES["wecom"].product_cards and not CAPABILITIES["wecom"].buttons
    # YouTube: text only, no typing/receipts.
    yt = CAPABILITIES["youtube"]
    assert not yt.buttons and not yt.typing_indicator and not yt.read_receipts
    assert yt.media_types == set()
    # TikTok: text only, business-gated.
    assert not CAPABILITIES["tiktok_business"].buttons
    assert CAPABILITIES["tiktok_business"].media_types == set()
    # Zalo: text + template + cards.
    assert CAPABILITIES["zalo_app"].templates and CAPABILITIES["zalo_app"].product_cards


def test_unknown_channel_conservative_default():
    # generic wechat/tiktok (personal variants) have no matrix entry → text-only
    caps = capabilities_for("wechat")
    assert caps.max_text_len == 2000
    assert not caps.buttons and not caps.product_cards and caps.media_types == set()


def test_youtube_degrades_buttons_and_media_to_text():
    caps = capabilities_for("youtube")
    content = MessageContent(
        blocks=[
            QuickButtonsBlock(
                text="Pick",
                buttons=[QuickButton(id="a", text="A"), QuickButton(id="b", text="B")],
            ),
            MediaBlock(media_type="image", file_id="00000000-0000-0000-0000-000000000001"),
        ]
    )
    out = degrade_content(content, caps, media_url=lambda fid: f"https://cdn/{fid}")
    # everything collapses to text blocks (no buttons, no media on YouTube)
    assert all(isinstance(b, TextBlock) for b in out.blocks)
    joined = "\n".join(b.text for b in out.blocks)
    assert "1. A" in joined and "2. B" in joined  # numbered menu
    assert "https://cdn/" in joined  # media became a link


async def test_connect_validate_default_accepts_and_requests_secret():
    adapter = BaseAdapter()
    cr = await adapter.connect_validate({"external_id": "T123", "name": "Acme"}, {})
    assert isinstance(cr, ConnectResult)
    assert cr.external_id == "T123"
    assert cr.name == "Acme"
    assert cr.health.ok is True
    assert cr.needs_webhook_secret is True


async def test_connect_validate_default_generates_external_id_when_absent():
    adapter = BaseAdapter()
    cr = await adapter.connect_validate({}, {})
    assert cr.external_id  # a uuid fallback, non-empty
