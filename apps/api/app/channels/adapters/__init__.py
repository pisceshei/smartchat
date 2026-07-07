"""Concrete channel adapters. Import via ..registry.get_adapter()."""
from .bridge import BridgeAdapter
from .email_imap import EmailAdapter
from .instagram import InstagramAdapter
from .line_oa import LineAdapter
from .messenger import MessengerAdapter
from .slack import SlackAdapter
from .telegram import TelegramAdapter
from .tiktok_business import TikTokBusinessAdapter
from .vk import VKAdapter
from .wechat_kf import WeChatKfAdapter
from .wecom import WeComAdapter
from .whatsapp_bsp import WhatsAppBspAdapter
from .whatsapp_cloud import WhatsAppCloudAdapter
from .widget import WidgetAdapter
from .youtube import YouTubeAdapter
from .zalo import ZaloAdapter

__all__ = [
    "BridgeAdapter",
    "EmailAdapter",
    "InstagramAdapter",
    "LineAdapter",
    "MessengerAdapter",
    "SlackAdapter",
    "TelegramAdapter",
    "TikTokBusinessAdapter",
    "VKAdapter",
    "WeChatKfAdapter",
    "WeComAdapter",
    "WhatsAppBspAdapter",
    "WhatsAppCloudAdapter",
    "WidgetAdapter",
    "YouTubeAdapter",
    "ZaloAdapter",
]
