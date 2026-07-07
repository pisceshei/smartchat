"""Message-template validation, variable substitution, MJML→HTML compile,
SMS GSM-7/UCS-2 segmentation, and per-channel content rendering (plan B.3).

The router persists the structural payload into ``msg_templates.body`` (jsonb)
plus the promoted columns (name/language/category/waba_account_id). The
fan-out calls :func:`build_content` to turn a stored template + a per-broadcast
``variable_mapping`` + a target ``Contact`` into a ``MessageContent`` the shared
outbound pipeline already knows how to send.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from typing import Any

from py_contracts.content import MessageContent
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.contacts import Contact
from ...models.marketing import MsgTemplate, SmsSignature

CHANNELS = ("whatsapp", "email", "messenger", "sms")
WA_CATEGORIES = ("marketing", "utility", "authentication")
WA_BUTTON_TYPES = ("none", "call_to_action", "quick_reply")

# GSM-7 default alphabet (+ extension chars that each cost 2 septets)
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXT = "^{}\\[~]|€"
_GSM7_SET = set(_GSM7_BASIC) | set(_GSM7_EXT)

_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


class TemplateError(ValueError):
    """Raised for an invalid channel-specific template body (router → 422)."""


# --------------------------------------------------------------------------
# body validation → promoted columns + normalised jsonb
# --------------------------------------------------------------------------
@dataclass
class TemplateColumns:
    name: str
    body: dict[str, Any]
    language: str | None = None
    category: str | None = None
    waba_account_id: str | None = None
    folder: str | None = None
    approval_status: str = "none"


def _require(body: dict[str, Any], key: str) -> Any:
    if key not in body or body[key] in (None, ""):
        raise TemplateError(f"missing required field: {key}")
    return body[key]


def validate_and_extract(channel: str, incoming: dict[str, Any]) -> TemplateColumns:
    """Validate a channel-specific template payload and split it into promoted
    columns + the structural jsonb body kept in ``msg_templates.body``."""
    if channel not in CHANNELS:
        raise TemplateError(f"unknown channel: {channel}")
    folder = incoming.get("folder")
    if channel == "whatsapp":
        name = str(_require(incoming, "name"))
        if not re.fullmatch(r"[a-z0-9_]{1,512}", name):
            raise TemplateError("whatsapp template name must be lowercase_with_underscores")
        category = str(_require(incoming, "category"))
        if category not in WA_CATEGORIES:
            raise TemplateError(f"category must be one of {WA_CATEGORIES}")
        language = str(_require(incoming, "language"))
        buttons = incoming.get("buttons") or {"type": "none", "items": []}
        if buttons.get("type") not in WA_BUTTON_TYPES:
            raise TemplateError(f"buttons.type must be one of {WA_BUTTON_TYPES}")
        body = {
            "label": incoming.get("label"),
            "header": incoming.get("header"),
            "body": {"text": str(((incoming.get("body") or {}).get("text")) or "")},
            "footer": incoming.get("footer"),
            "buttons": buttons,
        }
        if not body["body"]["text"]:
            raise TemplateError("whatsapp body.text is required")
        return TemplateColumns(
            name=name, body=body, language=language, category=category,
            waba_account_id=incoming.get("waba_account_id"), folder=folder,
            approval_status="draft",
        )
    if channel == "email":
        name = str(_require(incoming, "name"))
        subject = str(incoming.get("subject") or "")
        mjml = str(incoming.get("mjml_source") or incoming.get("html") or "")
        if not mjml:
            raise TemplateError("email template needs mjml_source (or html)")
        body = {
            "subject": subject,
            "mjml_source": mjml,
            "html": compile_mjml(mjml),
            "variables": list(incoming.get("variables") or []),
        }
        return TemplateColumns(name=name, body=body, folder=folder)
    if channel == "messenger":
        name = str(_require(incoming, "name"))
        payload = incoming.get("payload")
        if not payload:
            raise TemplateError("messenger template needs a payload")
        body = {"payload": payload, "message_tag": incoming.get("message_tag")}
        return TemplateColumns(name=name, body=body, folder=folder)
    # sms
    name = str(_require(incoming, "name"))
    text = str(_require(incoming, "text"))
    body = {"text": text, "signature_id": incoming.get("signature_id")}
    return TemplateColumns(name=name, body=body, folder=folder)


# --------------------------------------------------------------------------
# variable substitution
# --------------------------------------------------------------------------
def resolve_var(spec: Any, contact: Contact | None) -> str:
    """Resolve one variable-mapping value against a contact. A mapping value is
    either a literal string or a ``{"field": "display_name"}`` /
    ``{"field": "custom.city", "fallback": "there"}`` reference."""
    if isinstance(spec, dict):
        field = str(spec.get("field") or "")
        fallback = str(spec.get("fallback") or "")
        val = _contact_field(contact, field) if field else None
        return str(val) if val not in (None, "") else fallback
    return str(spec)


def _contact_field(contact: Contact | None, field: str) -> Any:
    if contact is None or not field:
        return None
    if field.startswith("custom."):
        return (contact.custom or {}).get(field.split(".", 1)[1])
    return getattr(contact, field, None)


def substitute(text: str, variable_mapping: dict[str, Any], contact: Contact | None) -> str:
    """Replace ``{{token}}`` placeholders using ``variable_mapping`` (token →
    literal or contact field). Unmapped tokens fall back to a direct contact
    attribute of the same name, else empty string."""
    def _sub(m: re.Match[str]) -> str:
        token = m.group(1)
        if token in variable_mapping:
            return resolve_var(variable_mapping[token], contact)
        direct = _contact_field(contact, token)
        return str(direct) if direct not in (None, "") else ""

    return _VAR_RE.sub(_sub, text or "")


# --------------------------------------------------------------------------
# MJML → HTML (minimal, dependency-free) + inline + plaintext
# --------------------------------------------------------------------------
_MJML_TAG_MAP = {
    "mj-section": "div",
    "mj-column": "div",
    "mj-text": "p",
    "mj-button": "a",
    "mj-image": "img",
    "mj-divider": "hr",
    "mj-body": "body",
    "mjml": "html",
    "mj-head": "head",
    "mj-title": "title",
}


def compile_mjml(src: str) -> str:
    """Compile a subset of MJML to HTML. Real MJML is not vendored (no network,
    no node); this maps the common ``mj-*`` tags to HTML so the stored source
    always has a renderable HTML twin (plan: MJML 源+編譯 HTML 同存). Plain HTML
    passes through unchanged."""
    if "<mj" not in src.lower():
        return src  # already HTML
    html = src
    for mj, tag in _MJML_TAG_MAP.items():
        html = re.sub(rf"<{mj}(\s[^>]*)?>", f"<{tag}>", html, flags=re.IGNORECASE)
        html = re.sub(rf"</{mj}>", f"</{tag}>", html, flags=re.IGNORECASE)
    if "<html>" not in html.lower():
        html = f"<html><body>{html}</body></html>"
    return html


_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def inline_css(html: str) -> str:
    """Best-effort CSS inlining for broadcast email (plan: 發送時內聯 CSS).
    Without a full CSS engine we simply keep the ``<style>`` block (email
    clients that support it render it) and ensure a document wrapper — enough to
    send a valid MIME html part; the real inliner runs at the ESP in EDM mode."""
    if "<html" in html.lower():
        return html
    return f"<html><head></head><body>{html}</body></html>"


def html_to_text(html: str) -> str:
    """Plaintext alternative for a multipart email (auto 純文本替代)."""
    text = _STYLE_RE.sub("", html)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    return _html.unescape(text).strip()


# --------------------------------------------------------------------------
# SMS segmentation (GSM-7 vs UCS-2) + cost preview
# --------------------------------------------------------------------------
@dataclass
class SmsSegmentation:
    encoding: str  # "GSM-7" | "UCS-2"
    char_count: int
    segments: int
    per_segment: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "encoding": self.encoding,
            "char_count": self.char_count,
            "segments": self.segments,
            "per_segment": self.per_segment,
        }


def sms_segments(text: str) -> SmsSegmentation:
    """Detect GSM-7 vs UCS-2 and count segments (plan: GSM-7/UCS-2 檢測+分段計數
    成本預覽). Extension chars cost 2 septets; multipart uses UDH so the
    per-segment budget shrinks (153 / 67)."""
    text = text or ""
    is_gsm = all(c in _GSM7_SET for c in text)
    if is_gsm:
        length = sum(2 if c in _GSM7_EXT else 1 for c in text)
        single, multi = 160, 153
        encoding = "GSM-7"
    else:
        length = sum(2 if ord(c) > 0xFFFF else 1 for c in text)  # surrogate pairs
        single, multi = 70, 67
        encoding = "UCS-2"
    if length <= single:
        segments, per = (1, single) if length else (0, single)
    else:
        per = multi
        segments = (length + multi - 1) // multi
    return SmsSegmentation(encoding=encoding, char_count=length, segments=segments, per_segment=per)


# --------------------------------------------------------------------------
# render a stored template → MessageContent for the send pipeline
# --------------------------------------------------------------------------
async def _signature_text(
    session: AsyncSession, workspace_id: Any, signature_id: Any
) -> str:
    if not signature_id:
        return ""
    sig = await session.get(SmsSignature, signature_id)
    if sig is None or sig.workspace_id != workspace_id or sig.status != "active":
        return ""
    return sig.text


def build_wa_components(
    body: dict[str, Any], variable_mapping: dict[str, Any], contact: Contact | None
) -> dict[str, Any]:
    """Build the WhatsApp Cloud API ``components`` array from the stored
    header/body structure + resolved variables. Returns the dict the
    TemplateBlock carries (adapter reads ``components['components']``)."""
    components: list[dict[str, Any]] = []
    header = body.get("header")
    if header and header.get("type") == "text" and "{{" in str(header.get("text", "")):
        params = _wa_positional_params(str(header.get("text")), variable_mapping, contact)
        if params:
            components.append({"type": "header", "parameters": params})
    body_text = str((body.get("body") or {}).get("text") or "")
    if "{{" in body_text:
        params = _wa_positional_params(body_text, variable_mapping, contact)
        if params:
            components.append({"type": "body", "parameters": params})
    return {"components": components}


def _wa_positional_params(
    text: str, variable_mapping: dict[str, Any], contact: Contact | None
) -> list[dict[str, str]]:
    params: list[dict[str, str]] = []
    for m in _VAR_RE.finditer(text):
        token = m.group(1)
        spec = variable_mapping.get(token, {"field": token})
        params.append({"type": "text", "text": resolve_var(spec, contact)})
    return params


async def build_content(
    session: AsyncSession,
    *,
    template: MsgTemplate,
    variable_mapping: dict[str, Any],
    contact: Contact | None,
    channel_type: str,
) -> MessageContent:
    """Render a stored template into a ``MessageContent`` for the target
    contact. WhatsApp yields a TemplateBlock (window-independent); the other
    channels yield resolved text/email blocks."""
    body = template.body or {}
    if template.channel == "whatsapp":
        return MessageContent.model_validate(
            {
                "blocks": [
                    {
                        "kind": "template",
                        "template_name": template.name,
                        "language": template.language or "en",
                        "category": template.category,
                        "components": build_wa_components(body, variable_mapping, contact),
                    }
                ]
            }
        )
    if template.channel == "sms":
        text = substitute(str(body.get("text") or ""), variable_mapping, contact)
        sig = await _signature_text(session, template.workspace_id, body.get("signature_id"))
        if sig:
            text = f"{text}\n{sig}"
        return MessageContent.model_validate({"blocks": [{"kind": "text", "text": text}]})
    if template.channel == "messenger":
        payload = body.get("payload")
        raw = payload if isinstance(payload, str) else str((payload or {}).get("text", ""))
        text = substitute(raw, variable_mapping, contact)
        return MessageContent.model_validate({"blocks": [{"kind": "text", "text": text}]})
    # email
    subject = substitute(str(body.get("subject") or ""), variable_mapping, contact)
    raw_html = str(body.get("html") or body.get("mjml_source") or "")
    html = inline_css(substitute(raw_html, variable_mapping, contact))
    text = html_to_text(html)
    return MessageContent.model_validate(
        {"blocks": [{"kind": "email", "subject": subject, "text": text or subject}]}
    )
