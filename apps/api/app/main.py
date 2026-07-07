"""SmartChat API — app factory.

Routers auto-register: each module under app.modules exposes `router`
(APIRouter). Modules are listed here once; agents adding a module append to
MODULE_ROUTERS only.
"""
from __future__ import annotations

import importlib
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger("smartchat")

MODULE_ROUTERS = [
    "apps.api.app.modules.auth.router",
    "apps.api.app.modules.workspaces.router",
    "apps.api.app.modules.members.router",
    "apps.api.app.modules.contacts.router",
    "apps.api.app.modules.inbox.router",
    "apps.api.app.modules.channels.router",
    "apps.api.app.modules.widget.router",
    "apps.api.app.modules.hooks.router",
    "apps.api.app.modules.settings_mod.router",
    "apps.api.app.modules.openapi_public.router",
    "apps.api.app.modules.ai.router",
    "apps.api.app.modules.translate.router",
    "apps.api.app.modules.flows.router",
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


def _mount_widget_assets(app: FastAPI) -> None:
    """Serve the embeddable loader at /js/project_{key}.js and the iframe chat
    app under /widget-app/. The loader parses its widget key from its own URL,
    so one static artifact serves every widget (Cloudflare-cacheable). In
    production nginx can shadow these paths; this keeps a single-container
    deployment fully functional."""
    from pathlib import Path

    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parents[2] / "widget" / "dist"

    @app.get("/js/project_{widget_key}.js", include_in_schema=False)
    async def widget_loader(widget_key: str):  # noqa: ANN202
        loader = dist / "loader.js"
        if not loader.is_file() or not widget_key.isalnum():
            raise HTTPException(404)
        return FileResponse(
            loader,
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=3600, stale-while-revalidate=86400"},
        )

    chat_dir = dist / "chat"
    if chat_dir.is_dir():
        app.mount("/widget-app", StaticFiles(directory=str(chat_dir), html=True), name="widget-app")


app = create_app()
