"""Split-link service: slug/base62, target validation, deep-link building,
Redis config cache for the edge redirect, tracking-code generation, QR (segno),
and click stats (plan B.3).
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.marketing import SplitLink
from ...settings import get_settings

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SLUG_LEN = 7
TRACKING_LEN = 8
STRATEGIES = ("random", "time_period", "sequential")
CONFIG_TTL_S = 300


def base62(n: int = SLUG_LEN) -> str:
    return "".join(secrets.choice(_B62) for _ in range(n))


def tracking_code() -> str:
    return base62(TRACKING_LEN)


async def unique_slug(session: AsyncSession, *, tries: int = 8) -> str:
    for _ in range(tries):
        slug = base62()
        exists = (
            await session.execute(select(SplitLink.id).where(SplitLink.slug == slug))
        ).first()
        if exists is None:
            return slug
    return base62(SLUG_LEN + 3)  # widen on repeated collision (astronomically rare)


class SplitLinkError(ValueError):
    def __init__(self, detail: str, code: str = "invalid"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def validate_targets(targets: list[dict[str, Any]], *, channel_type: str) -> list[dict[str, Any]]:
    if not targets:
        raise SplitLinkError("at least one target is required", "no_targets")
    out: list[dict[str, Any]] = []
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            raise SplitLinkError(f"target {i} must be an object", "bad_target")
        norm: dict[str, Any] = {
            "channel_account_id": (str(t["channel_account_id"]) if t.get("channel_account_id") else None),
            "weight": max(0, int(t.get("weight", 1) or 0)),
            "enabled": bool(t.get("enabled", True)),
        }
        if t.get("phone"):
            norm["phone"] = str(t["phone"]).lstrip("+")
        if t.get("username"):
            norm["username"] = str(t["username"]).lstrip("@")
        if t.get("url"):
            norm["url"] = str(t["url"])
        if t.get("daily_cap") is not None:
            norm["daily_cap"] = int(t["daily_cap"])
        if t.get("time_windows"):
            norm["time_windows"] = t["time_windows"]
        if not any(norm.get(k) for k in ("phone", "username", "url", "channel_account_id")):
            raise SplitLinkError(f"target {i} needs a phone/username/url/account", "bad_target")
        out.append(norm)
    return out


def build_deeplink(channel_type: str, target: dict[str, Any], text: str) -> str:
    """Build the platform deep link a click should 302 to. An explicit
    ``target.url`` wins; otherwise a per-channel builder (wa.me / t.me / m.me /
    line) is used."""
    if target.get("url"):
        base = target["url"]
        if text:
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}text={quote(text)}"
        return base
    q = f"?text={quote(text)}" if text else ""
    if channel_type in ("whatsapp", "whatsapp_cloud", "whatsapp_app"):
        phone = target.get("phone", "")
        return f"https://wa.me/{phone}{q}"
    if channel_type in ("telegram", "telegram_bot"):
        return f"https://t.me/{target.get('username', '')}{q}"
    if channel_type in ("messenger",):
        ref = f"?ref={quote(text)}" if text else ""
        return f"https://m.me/{target.get('username', '')}{ref}"
    if channel_type in ("line", "line_oa"):
        return f"https://line.me/R/ti/p/{target.get('username', '')}"
    return target.get("url") or f"https://wa.me/{target.get('phone', '')}{q}"


def render_prefill(prefill_text: str | None, code: str) -> str:
    """Substitute the {{code}} tracking token into the prefilled message."""
    if not prefill_text:
        return ""
    if "{{code}}" in prefill_text or "{{ code }}" in prefill_text:
        return prefill_text.replace("{{code}}", code).replace("{{ code }}", code)
    return prefill_text


# --------------------------------------------------------------------------
# Redis config cache (edge reads this hot; DB is the source of truth)
# --------------------------------------------------------------------------
def config_key(slug: str) -> str:
    return f"splitlink:cfg:{slug}"


def link_config(link: SplitLink) -> dict[str, Any]:
    return {
        "id": str(link.id),
        "workspace_id": str(link.workspace_id),
        "slug": link.slug,
        "channel_type": link.channel_type,
        "strategy": link.strategy,
        "targets": link.targets or [],
        "prefill_text": link.prefill_text,
        "status": link.status,
    }


async def cache_config(redis: Any, link: SplitLink) -> None:
    try:
        await redis.set(config_key(link.slug), json.dumps(link_config(link)), ex=CONFIG_TTL_S)
    except Exception:  # noqa: BLE001 — cache is best-effort
        pass


async def invalidate_config(redis: Any, slug: str) -> None:
    try:
        await redis.delete(config_key(slug))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# public URLs
# --------------------------------------------------------------------------
def short_url(slug: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/s/{slug}"


def qr_url(link_id: uuid.UUID | str) -> str:
    return f"{get_settings().assets_base_url.rstrip('/')}/api/v1/split-links/{link_id}/qr.png"


def render_qr_png(slug: str) -> bytes:
    """Server-side QR of the short link (segno; pure-python, offline)."""
    import io

    import segno

    buf = io.BytesIO()
    segno.make(short_url(slug), error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


async def cache_qr(session: AsyncSession, link: SplitLink) -> str | None:
    """Generate + cache the QR to MinIO (best-effort). Returns the storage key
    (also stored on ``split_links.qr_key``)."""
    try:
        from ...channels.media import get_media_store

        png = render_qr_png(link.slug)
        row = await get_media_store().store_bytes(
            session, workspace_id=link.workspace_id, data=png, mime="image/png",
            filename=f"qr_{link.slug}.png",
        )
        return row.storage_key
    except Exception:  # noqa: BLE001 — MinIO may be down; qr.png route still serves live
        return None


# --------------------------------------------------------------------------
# click stats
# --------------------------------------------------------------------------
async def click_series(
    session: AsyncSession,
    *,
    link_id: uuid.UUID,
    frm: date | None = None,
    to: date | None = None,
) -> dict[str, Any]:
    from ...models.marketing import SplitLinkClick

    day = func.date(SplitLinkClick.ts)
    q = (
        select(day.label("d"), SplitLinkClick.target_idx, func.count().label("c"))
        .where(SplitLinkClick.link_id == link_id)
        .group_by(day, SplitLinkClick.target_idx)
        .order_by(day)
    )
    if frm is not None:
        q = q.where(SplitLinkClick.ts >= datetime(frm.year, frm.month, frm.day, tzinfo=UTC))
    if to is not None:
        q = q.where(SplitLinkClick.ts < datetime(to.year, to.month, to.day, 23, 59, 59, tzinfo=UTC))
    rows = (await session.execute(q)).all()
    series = [
        {"date": (d.isoformat() if hasattr(d, "isoformat") else str(d)),
         "target_idx": idx, "clicks": int(c)}
        for d, idx, c in rows
    ]
    total = sum(s["clicks"] for s in series)
    return {"series": series, "total": total}
