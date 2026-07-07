"""Edge split-link redirect + click tracking (plan B.3, compose service 'edge').

``GET /s/{slug}`` resolves the link config (Redis-cached, DB fallback), picks a
target per the strategy (random / time_period / sequential), records a
``split_link_clicks`` row + a ``splitlink.click`` outbox event, bumps the click
counters, and 302s to the platform deep link (wa.me/…) with the prefilled text
carrying a fresh ``{{code}}`` tracking token.

This is a thin, separately-deployable ASGI app that reuses the API package's
DB/redis/models. Run:  ``uvicorn apps.edge.split_redirect:app --port 8002``.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from py_contracts.events import Actor, Event
from sqlalchemy import select, update

from apps.api.app.db import session_factory
from apps.api.app.marketing import split_strategy
from apps.api.app.models.base import uuid7
from apps.api.app.models.marketing import SplitLink, SplitLinkClick
from apps.api.app.modules.split_links import service as svc
from apps.api.app.services import event_bus
from apps.api.app.services.redis_client import close_redis, get_redis


def _now() -> datetime:
    return datetime.now(UTC)


def _ip_hash(ip: str | None) -> str | None:
    if not ip:
        return None
    return hashlib.sha256(ip.encode()).hexdigest()[:64]


def _device(ua: str | None) -> str | None:
    if not ua:
        return None
    try:
        from user_agents import parse as ua_parse

        p = ua_parse(ua)
        if p.is_mobile:
            return "mobile"
        if p.is_tablet:
            return "tablet"
        if p.is_pc:
            return "desktop"
        if p.is_bot:
            return "bot"
    except Exception:  # noqa: BLE001
        return None
    return "other"


async def _load_config(redis: Any, slug: str) -> dict[str, Any] | None:
    cached = await redis.get(svc.config_key(slug))
    if cached:
        try:
            return json.loads(cached)
        except ValueError:
            pass
    async with session_factory()() as session:
        link = (
            await session.execute(select(SplitLink).where(SplitLink.slug == slug))
        ).scalar_one_or_none()
        if link is None:
            return None
        cfg = svc.link_config(link)
    await redis.set(svc.config_key(slug), json.dumps(cfg), ex=svc.CONFIG_TTL_S)
    return cfg


def _daily_key(link_id: str) -> str:
    return f"splitlink:daily:{link_id}:{_now():%Y%m%d}"


def _seq_key(link_id: str) -> str:
    return f"splitlink:seq:{link_id}"


async def _daily_counts(redis: Any, link_id: str, n: int) -> dict[int, int]:
    raw = await redis.hgetall(_daily_key(link_id))
    out: dict[int, int] = {}
    for k, v in (raw or {}).items():
        try:
            out[int(k)] = int(v)
        except (ValueError, TypeError):
            continue
    return out


def create_app() -> FastAPI:
    app = FastAPI(title="SmartChat Edge", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/s/{slug}")
    async def redirect(slug: str, request: Request):  # noqa: ANN202
        redis = get_redis()
        cfg = await _load_config(redis, slug)
        if cfg is None or cfg.get("status") != "active":
            return JSONResponse({"error": "not found"}, status_code=404)
        targets: list[dict[str, Any]] = cfg.get("targets") or []
        channel_type = cfg.get("channel_type", "whatsapp")
        strategy = cfg.get("strategy", "random")
        link_id = cfg["id"]

        cursor = 0
        if strategy == "sequential":
            cursor = int(await redis.incr(_seq_key(link_id))) - 1
        daily = await _daily_counts(redis, link_id, len(targets))
        idx, _ = split_strategy.choose_target(
            targets, strategy=strategy, cursor=cursor, now=_now(), daily_counts=daily,
        )
        if idx is None:
            # graceful fallback: first enabled target ignoring caps/windows
            for i, t in enumerate(targets):
                if t.get("enabled") is not False:
                    idx = i
                    break
        if idx is None:
            return JSONResponse({"error": "no active target"}, status_code=503)

        target = targets[idx]
        code = svc.tracking_code()
        text = svc.render_prefill(cfg.get("prefill_text"), code)
        deeplink = svc.build_deeplink(channel_type, target, text)

        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else None)
        country = (request.headers.get("cf-ipcountry") or request.headers.get("x-country") or None)
        ua = request.headers.get("user-agent")
        now = _now()
        workspace_id = uuid.UUID(cfg["workspace_id"])
        async with session_factory()() as session:
            async with session.begin():
                session.add(
                    SplitLinkClick(
                        id=uuid7(), link_id=uuid.UUID(link_id), workspace_id=workspace_id,
                        ts=now, target_idx=idx, tracking_code=code, ip_hash=_ip_hash(ip),
                        ua=ua, device=_device(ua),
                        country=(country[:2].upper() if country else None),
                        referrer=request.headers.get("referer"),
                    )
                )
                await session.execute(
                    update(SplitLink).where(SplitLink.id == uuid.UUID(link_id))
                    .values(click_count=SplitLink.click_count + 1)
                )
                await event_bus.emit(
                    session,
                    Event(
                        workspace_id=workspace_id, type="splitlink.click", actor=Actor(type="contact"),
                        channel_type=channel_type,
                        payload={"link_id": link_id, "slug": slug, "target_idx": idx,
                                 "tracking_code": code, "device": _device(ua), "country": country},
                    ),
                )
        await redis.hincrby(_daily_key(link_id), str(idx), 1)
        await redis.expire(_daily_key(link_id), 3 * 24 * 3600)
        return RedirectResponse(deeplink, status_code=302)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await close_redis()

    return app


app = create_app()
