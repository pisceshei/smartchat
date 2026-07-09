"""SmartChat API — app factory.

Routers auto-register: each module under app.modules exposes `router`
(APIRouter). Modules are listed here once; agents adding a module append to
MODULE_ROUTERS only.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("smartchat")

MODULE_ROUTERS = [
    "apps.api.app.modules.auth.router",
    "apps.api.app.modules.workspaces.router",
    "apps.api.app.modules.members.router",
    "apps.api.app.modules.contacts.router",
    "apps.api.app.modules.inbox.router",
    "apps.api.app.modules.channels.router",
    "apps.api.app.modules.devices.router",
    "apps.api.app.modules.widget.router",
    "apps.api.app.modules.hooks.router",
    # Phase 4 per-channel webhook stubs (adapter agents fill the bodies).
    # YouTube has NO webhook route — it is polled via the Data API (see the
    # email-style poller pattern), so it is intentionally absent here.
    "apps.api.app.modules.hooks.slack",
    "apps.api.app.modules.hooks.vk",
    "apps.api.app.modules.hooks.wechat",
    "apps.api.app.modules.hooks.zalo",
    "apps.api.app.modules.hooks.tiktok",
    "apps.api.app.modules.hooks.ycloud",
    "apps.api.app.modules.settings_mod.router",
    "apps.api.app.modules.openapi_public.router",
    "apps.api.app.modules.ai.router",
    "apps.api.app.modules.translate.router",
    "apps.api.app.modules.flows.router",
    "apps.api.app.modules.billing.router",
    "apps.api.app.modules.reports.router",
    "apps.api.app.modules.segments.router",
    "apps.api.app.modules.broadcasts.router",
    "apps.api.app.modules.msg_templates.router",
    "apps.api.app.modules.split_links.router",
    "apps.api.app.modules.edm.router",
]


def create_app() -> FastAPI:
    app = FastAPI(title="SmartChat API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tightened in prod via env
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for mod_path in MODULE_ROUTERS:
        try:
            mod = importlib.import_module(mod_path)
            app.include_router(mod.router)
        except ModuleNotFoundError:
            log.warning("module not present yet: %s", mod_path)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    _mount_widget_assets(app)
    return app


def _mount_widget_assets(app: FastAPI, dist_dir: Path | None = None) -> None:
    """Serve the embeddable loader at /js/project_{key}.js and the iframe chat
    app under /widget-app/ (plus a legacy /chat alias — loaders cached before
    the /widget-app path fix keep requesting /chat/index.html for up to a day).
    The loader parses its widget key from its own URL, so one static artifact
    serves every widget (Cloudflare-cacheable). In production nginx can shadow
    these paths; this keeps a single-container deployment fully functional."""
    from fastapi import Depends, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    from .db import get_session
    from .modules.widget import service as widget_service

    dist = dist_dir or Path(__file__).resolve().parents[2] / "widget" / "dist"

    @app.get("/js/project_{widget_key}.js", include_in_schema=False)
    async def widget_loader(
        widget_key: str,
        session: AsyncSession = Depends(get_session),
    ):  # noqa: ANN202
        loader = dist / "loader.js"
        if not loader.is_file() or not widget_key.isalnum():
            raise HTTPException(404, headers={"Cache-Control": "no-store"})
        known = True
        try:
            known = (await widget_service.get_widget_by_key(session, widget_key)) is not None
        except Exception:  # noqa: BLE001 — DB hiccup: fail open, never kill live widgets
            log.warning("widget_loader: enabled-check skipped", exc_info=True)
        if not known:
            # unknown/disabled key — 404 uncached so re-enabling works instantly
            raise HTTPException(404, headers={"Cache-Control": "no-store"})
        return FileResponse(
            loader,
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=3600, stale-while-revalidate=86400"},
        )

    chat_dir = dist / "chat"
    if chat_dir.is_dir():
        chat_static = StaticFiles(directory=str(chat_dir), html=True)
        app.mount("/widget-app", chat_static, name="widget-app")
        # Legacy alias: old cached loaders still request /chat/index.html.
        app.mount("/chat", chat_static, name="widget-app-legacy")


app = create_app()
