"""channel_type → adapter singleton registry."""
from __future__ import annotations

import logging
from importlib import import_module

from .adapters.bridge import BridgeAdapter
from .adapters.email_imap import EmailAdapter
from .adapters.instagram import InstagramAdapter
from .adapters.line_oa import LineAdapter
from .adapters.messenger import MessengerAdapter
from .adapters.telegram import TelegramAdapter
from .adapters.whatsapp_bsp import WhatsAppBspAdapter
from .adapters.whatsapp_cloud import WhatsAppCloudAdapter
from .adapters.widget import WidgetAdapter
from .base import ChannelAdapter


class UnknownChannelError(Exception):
    pass


_REGISTRY: dict[str, ChannelAdapter] = {}


def register(adapter: ChannelAdapter) -> None:
    _REGISTRY[adapter.channel_type] = adapter


def get_adapter(channel_type: str) -> ChannelAdapter:
    try:
        return _REGISTRY[channel_type]
    except KeyError:
        raise UnknownChannelError(f"no adapter registered for channel_type={channel_type!r}") from None


def registered_channel_types() -> list[str]:
    return sorted(_REGISTRY)


# default singletons
register(WidgetAdapter())
register(TelegramAdapter())
register(WhatsAppCloudAdapter())
register(WhatsAppBspAdapter())
register(MessengerAdapter())
register(InstagramAdapter())
register(LineAdapter())
register(EmailAdapter())
register(BridgeAdapter("whatsapp_app"))
register(BridgeAdapter("line_app"))


_log = logging.getLogger("smartchat.channels.registry")

# --------------------------------------------------------------------------
# Phase 4 channel adapters (guarded auto-registration)
# --------------------------------------------------------------------------
# These adapters land incrementally, one agent per channel. Import + register
# each if its module already exists so the app boots BEFORE every adapter agent
# has finished (a missing module / not-yet-added class is skipped; a real error
# inside a finished adapter still surfaces). The (module, class) names below are
# the expected convention — an adapter agent may also append an explicit
# register() line at the bottom of this file; register() is idempotent by
# channel_type, so double-registration is harmless.
_OPTIONAL_ADAPTERS: list[tuple[str, str]] = [
    ("slack", "SlackAdapter"),
    ("vk", "VKAdapter"),
    ("wechat_kf", "WeChatKfAdapter"),
    ("wecom", "WeComAdapter"),
    ("tiktok_business", "TikTokBusinessAdapter"),
    ("youtube", "YouTubeAdapter"),
    ("zalo", "ZaloAdapter"),
]

for _mod_name, _cls_name in _OPTIONAL_ADAPTERS:
    try:
        _mod = import_module(f".adapters.{_mod_name}", __package__)
        register(getattr(_mod, _cls_name)())
        _log.debug("registered phase-4 adapter: %s", _mod_name)
    except (ImportError, AttributeError):
        continue  # adapter not present yet (or a different class name is used)

# explicit registrations (idempotent by channel_type; also covered by the guard).
from .adapters.slack import SlackAdapter  # noqa: E402
from .adapters.vk import VKAdapter  # noqa: E402
from .adapters.wechat_kf import WeChatKfAdapter  # noqa: E402
from .adapters.wecom import WeComAdapter  # noqa: E402

register(SlackAdapter())
register(VKAdapter())
register(WeComAdapter())
register(WeChatKfAdapter())

from .adapters.tiktok_business import TikTokBusinessAdapter  # noqa: E402
from .adapters.youtube import YouTubeAdapter  # noqa: E402
from .adapters.zalo import ZaloAdapter  # noqa: E402

register(ZaloAdapter())
register(YouTubeAdapter())
register(TikTokBusinessAdapter())
