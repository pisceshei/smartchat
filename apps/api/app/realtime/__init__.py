"""Realtime layer (plan 附錄 A.8): ws-gateway, seq/resume, presence, unread.

Other modules import from here:

    from apps.api.app.realtime import publish            # push events to clients
    from apps.api.app.realtime import mint_visitor_token # widget bootstrap
    from apps.api.app.realtime import unread, presence   # counters / online state
"""
from . import presence, unread
from .protocol import (
    AUDIENCE_AGENTS,
    member_audience,
    mint_visitor_token,
    verify_visitor_token,
    visitor_audience,
)
from .publisher import publish

__all__ = [
    "AUDIENCE_AGENTS",
    "member_audience",
    "mint_visitor_token",
    "presence",
    "publish",
    "unread",
    "verify_visitor_token",
    "visitor_audience",
]
