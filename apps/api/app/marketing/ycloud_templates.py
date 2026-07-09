"""YCloud WhatsApp template provider integration (plan round 10, B4).

The generic reconcile loop lives in ``wa_template_sync`` (it matches by
``meta_template_id`` then (name, language) regardless of provider); this module
owns everything YCloud-specific:

* ``fetch_ycloud_templates`` — GET /whatsapp/templates for a WABA (paged)
* ``normalize_remote``       — YCloud template → the graph-ish dict shape the
                               shared reconcile loop consumes
* ``body_to_components``     — our ``MsgTemplate.body`` JSONB
                               ({header,body,footer,buttons}) → Meta/YCloud
                               components (with the required examples)
* ``components_to_body``     — inverse mapping, used when importing remote
                               templates that don't exist locally
* ``submit_template``        — POST /whatsapp/templates (create → PENDING)
* ``find_template``          — lookup by (wabaId, name, language)
* ``apply_template_review``  — apply a ``whatsapp.template.reviewed`` webhook
                               event to the local MsgTemplate rows

WhatsApp only allows POSITIONAL variables ({{1}}..{{n}}); named variables are
rejected up front with a clear error instead of a Meta-side rejection.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.marketing import MsgTemplate
from .wa_template_sync import map_meta_status

log = logging.getLogger("smartchat.marketing.ycloud_templates")

YCLOUD_BASE = "https://api.ycloud.com/v2"

_POS_VAR = re.compile(r"\{\{(\d+)\}\}")
_ANY_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class TemplateError(ValueError):
    """Local template body cannot be expressed as a WhatsApp template."""


class TemplateSubmitConflict(RuntimeError):
    """name+language already exists on the WABA."""


class TemplateSubmitError(RuntimeError):
    """Provider rejected the create call (auth/validation/5xx)."""


def _http(client: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    if client is not None:
        return client, False
    return httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)), True


# --------------------------------------------------------------------------
# pure mappers (unit-tested)
# --------------------------------------------------------------------------
def _examples(text: str) -> list[str]:
    """Sample values for the positional variables in ``text`` (Meta requires
    examples on submission). Raises on named variables."""
    named = [v for v in _ANY_VAR.findall(text) if not v.isdigit()]
    if named:
        raise TemplateError(
            f"whatsapp templates need positional variables {{{{1}}}}…, got named: {named}"
        )
    n = len(set(_POS_VAR.findall(text)))
    return [f"Sample {i}" for i in range(1, n + 1)]


def body_to_components(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Our MsgTemplate.body JSONB → Meta/YCloud template components."""
    comps: list[dict[str, Any]] = []
    header = body.get("header") or {}
    if str(header.get("text") or "").strip():
        c: dict[str, Any] = {
            "type": "HEADER",
            "format": str(header.get("type") or "text").upper(),
            "text": header["text"],
        }
        ex = _examples(str(header["text"]))
        if ex:
            c["example"] = {"header_text": ex}
        comps.append(c)
    text = str((body.get("body") or {}).get("text") or "")
    if not text:
        raise TemplateError("whatsapp body.text is required")
    c = {"type": "BODY", "text": text}
    ex = _examples(text)
    if ex:
        c["example"] = {"body_text": [ex]}
    comps.append(c)
    footer = body.get("footer") or {}
    if str(footer.get("text") or "").strip():
        comps.append({"type": "FOOTER", "text": footer["text"]})
    buttons = body.get("buttons") or {}
    if buttons.get("type") in ("quick_reply", "call_to_action") and buttons.get("items"):
        out: list[dict[str, Any]] = []
        for it in buttons["items"]:
            btype = it.get("type") or (
                "quick_reply" if buttons["type"] == "quick_reply" else "url"
            )
            if btype == "quick_reply":
                out.append({"type": "QUICK_REPLY", "text": it.get("text", "")})
            elif btype == "url":
                url = str(it.get("value", ""))
                if _ANY_VAR.search(url):
                    # dynamic URL buttons need a full example URL Meta rejects
                    # without — we don't collect one, so fail early with a
                    # clear 422 instead of a provider-side 502
                    raise TemplateError(
                        "dynamic URL buttons (with {{n}}) are not supported yet — "
                        "use a static URL"
                    )
                out.append({"type": "URL", "text": it.get("text", ""), "url": url})
            elif btype == "phone_number":
                out.append(
                    {
                        "type": "PHONE_NUMBER",
                        "text": it.get("text", ""),
                        "phone_number": it.get("value", ""),
                    }
                )
        if out:
            comps.append({"type": "BUTTONS", "buttons": out})
    return comps


def components_to_body(components: list[dict[str, Any]]) -> dict[str, Any]:
    """Inverse of body_to_components — used to import remote templates."""
    body: dict[str, Any] = {
        "label": None,
        "header": None,
        "body": {"text": ""},
        "footer": None,
        "buttons": {"type": "none", "items": []},
    }
    for c in components or []:
        ctype = str(c.get("type") or "").upper()
        if ctype == "HEADER" and str(c.get("format") or "TEXT").upper() == "TEXT":
            body["header"] = {"type": "text", "text": c.get("text") or ""}
        elif ctype == "BODY":
            body["body"] = {"text": c.get("text") or ""}
        elif ctype == "FOOTER":
            body["footer"] = {"text": c.get("text") or ""}
        elif ctype == "BUTTONS":
            items: list[dict[str, Any]] = []
            kinds: set[str] = set()
            for b in c.get("buttons") or []:
                btype = str(b.get("type") or "").upper()
                if btype == "QUICK_REPLY":
                    items.append({"type": "quick_reply", "text": b.get("text", "")})
                    kinds.add("quick_reply")
                elif btype == "URL":
                    items.append({"type": "url", "text": b.get("text", ""), "value": b.get("url", "")})
                    kinds.add("call_to_action")
                elif btype == "PHONE_NUMBER":
                    items.append(
                        {
                            "type": "phone_number",
                            "text": b.get("text", ""),
                            "value": b.get("phone_number", ""),
                        }
                    )
                    kinds.add("call_to_action")
            if items:
                body["buttons"] = {
                    "type": "quick_reply" if kinds == {"quick_reply"} else "call_to_action",
                    "items": items,
                }
    return body


def components_representable(components: list[dict[str, Any]]) -> bool:
    """True when every component maps LOSSLESSLY into our body schema
    ({header:{type:text,text}, body, footer, buttons quick_reply|url|phone}).
    Media/location headers and OTP/COPY_CODE/CATALOG/FLOW buttons cannot be
    stored/re-rendered, so importing them would make every send fail — the
    importer skips those instead."""
    for c in components or []:
        ctype = str(c.get("type") or "").upper()
        if ctype == "HEADER" and str(c.get("format") or "TEXT").upper() != "TEXT":
            return False
        if ctype == "BUTTONS":
            for b in c.get("buttons") or []:
                if str(b.get("type") or "").upper() not in ("QUICK_REPLY", "URL", "PHONE_NUMBER"):
                    return False
        if ctype in ("CAROUSEL", "LIMITED_TIME_OFFER"):
            return False
    return True


def normalize_remote(t: dict[str, Any]) -> dict[str, Any]:
    """YCloud template object → the graph-ish dict the shared reconcile loop
    (wa_template_sync.sync_account_templates) consumes."""
    return {
        "name": t.get("name"),
        "language": t.get("language"),
        "status": t.get("status"),
        "category": t.get("category"),
        "id": t.get("officialTemplateId"),
        "rejected_reason": t.get("reason"),
        "components": t.get("components") or [],
    }


# --------------------------------------------------------------------------
# provider I/O
# --------------------------------------------------------------------------
async def fetch_ycloud_templates(
    client: httpx.AsyncClient | None, *, waba_id: str, api_key: str
) -> list[dict[str, Any]]:
    """List a WABA's templates (paged, bounded). [] on any transport error —
    the reconcile is best-effort and retried by the 6h cron."""
    http, close = _http(client)
    out: list[dict[str, Any]] = []
    try:
        for page in range(1, 11):
            try:
                r = await http.get(
                    f"{YCLOUD_BASE}/whatsapp/templates",
                    params={"filter.wabaId": waba_id, "page": page, "limit": 100},
                    headers={"X-API-Key": api_key},
                )
                r.raise_for_status()
                items = r.json().get("items") or []
            except (httpx.HTTPError, ValueError):
                break
            out.extend(i for i in items if isinstance(i, dict))
            if len(items) < 100:
                break
    finally:
        if close:
            await http.aclose()
    return out


async def find_template(
    client: httpx.AsyncClient | None, *, api_key: str, waba_id: str, name: str, language: str
) -> dict[str, Any] | None:
    http, close = _http(client)
    try:
        r = await http.get(
            f"{YCLOUD_BASE}/whatsapp/templates",
            params={"filter.wabaId": waba_id, "filter.name": name, "filter.language": language,
                    "limit": 10},
            headers={"X-API-Key": api_key},
        )
        r.raise_for_status()
        items = r.json().get("items") or []
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if close:
            await http.aclose()
    # exact match ONLY — a fuzzy items[0] fallback could adopt an unrelated
    # template's status/id after a conflict
    for it in items:
        if isinstance(it, dict) and it.get("name") == name and it.get("language") == language:
            return it
    return None


async def submit_template(
    client: httpx.AsyncClient | None,
    *,
    api_key: str,
    waba_id: str,
    name: str,
    language: str,
    category: str,
    components: list[dict[str, Any]],
) -> dict[str, Any]:
    """POST /whatsapp/templates. Returns the created template dict (usually
    status PENDING). Raises TemplateSubmitConflict on name+language duplicates
    so the caller can adopt the existing remote template instead."""
    http, close = _http(client)
    try:
        try:
            r = await http.post(
                f"{YCLOUD_BASE}/whatsapp/templates",
                json={
                    "wabaId": waba_id,
                    "name": name,
                    "language": language,
                    "category": category.upper(),
                    "components": components,
                },
                headers={"X-API-Key": api_key},
            )
        except httpx.HTTPError as e:
            raise TemplateSubmitError(str(e)[:300]) from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code < 400:
            return data if isinstance(data, dict) else {}
        msg = ""
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("code") or "")
        msg = msg or (data.get("message") if isinstance(data, dict) else "") or r.text[:300]
        if r.status_code == 409 or "exist" in str(msg).lower():
            raise TemplateSubmitConflict(str(msg))
        raise TemplateSubmitError(f"{r.status_code}: {msg}"[:300])
    finally:
        if close:
            await http.aclose()


# --------------------------------------------------------------------------
# webhook review application
# --------------------------------------------------------------------------
async def apply_template_review(
    session: AsyncSession, *, workspace_id: Any, event: dict[str, Any]
) -> bool:
    """Apply a ``whatsapp.template.reviewed`` payload (``whatsappTemplate``)
    to the workspace's local rows. Matches by officialTemplateId first, then
    (name, language). Returns True when a row changed."""
    name = str(event.get("name") or "")
    language = str(event.get("language") or "")
    official = str(event.get("officialTemplateId") or "") or None
    if not name and not official:
        return False
    rows = (
        await session.execute(
            select(MsgTemplate).where(
                MsgTemplate.workspace_id == workspace_id,
                MsgTemplate.channel == "whatsapp",
            )
        )
    ).scalars().all()
    target = None
    if official:
        target = next((t for t in rows if t.meta_template_id == official), None)
    if target is None:
        target = next(
            (t for t in rows if t.name == name and str(t.language or "") == language), None
        )
    if target is None:
        log.info("ycloud template review for unknown template %s/%s", name, language)
        return False
    new_status = map_meta_status(event.get("status"))
    changed = target.approval_status != new_status
    target.approval_status = new_status
    if official and target.meta_template_id != official:
        target.meta_template_id = official
        changed = True
    reason = event.get("reason")
    if new_status == "rejected" and reason and str(reason) != "NONE":
        target.rejected_reason = str(reason)
        changed = True
    elif new_status == "approved" and target.rejected_reason:
        target.rejected_reason = None
        changed = True
    return changed
