"""Instagram Messaging adapter — same Send API surface as Messenger with an
Instagram capability profile (1000-char text, image/video/audio media).
Webhook entries arrive under object=instagram and are routed by the IG
account id on /hooks/meta.
"""
from __future__ import annotations

from typing import ClassVar

from .messenger import MessengerAdapter


class InstagramAdapter(MessengerAdapter):
    channel_type: ClassVar[str] = "instagram"
