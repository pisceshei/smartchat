"""Edge service ASGI entrypoint (compose: apps.edge.main:app).

The edge app is the thin, auth-less public surface: split-link redirects
(/s/{slug}) + click tracking. Kept separate from the main API so it can scale
and be cached independently behind Cloudflare.
"""
from __future__ import annotations

from .split_redirect import create_app

app = create_app()
