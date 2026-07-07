"""channel_type → adapter singleton registry."""
from __future__ import annotations

from .adapters.bridge import BridgeAdapter
from .adapters.email_imap import EmailAdapter
from .adapters.instagram import InstagramAdapter
from .adapters.line_oa import LineAdapter
from .adapters.messenger import MessengerAdapter
from .adapters.telegram import TelegramAdapter
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
register(MessengerAdapter())
register(InstagramAdapter())
register(LineAdapter())
register(EmailAdapter())
register(BridgeAdapter("whatsapp_app"))
register(BridgeAdapter("line_app"))
