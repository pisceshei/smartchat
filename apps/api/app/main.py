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

    return app


app = create_app()
