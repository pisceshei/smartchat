"""Concrete channel adapters. Import via ..registry.get_adapter()."""
from .bridge import BridgeAdapter
from .email_imap import EmailAdapter
from .instagram import InstagramAdapter
from .line_oa import LineAdapter
from .messenger import MessengerAdapter
from .telegram import TelegramAdapter
from .whatsapp_cloud import WhatsAppCloudAdapter
from .widget import WidgetAdapter

__all__ = [
    "BridgeAdapter",
    "EmailAdapter",
    "InstagramAdapter",
    "LineAdapter",
    "MessengerAdapter",
    "TelegramAdapter",
    "WhatsAppCloudAdapter",
    "WidgetAdapter",
]
