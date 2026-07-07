"""The 15 flow actions (plan section 4 / B.1).

Every action executes through the SAME internal P1 services an agent uses, so a
flow-sent message is indistinguishable in storage/behaviour from a human one
(sender_type='automation', source_flow_id set → the inbox shows the 「自動化」
source tag). Nothing here talks to a channel directly — messaging.send_message
writes the outbox event and the channel sender worker does the I/O.

Suspending actions (ask / quick_buttons / delay) return a ``wait`` NodeResult;
the interpreter persists the descriptor and schedules the resume timer. The
reply/button capture on resume lives in the interpreter (feed_event).

external_request carries the SSRF guard: the destination host is DNS-resolved
and every resolved IP (and every redirect hop) is rejected if private /
loopback / link-local / reserved.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from py_contracts.events import Actor, Event
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from apps.api.app.flows.graph_schema import Node
from apps.api.app.models.misc import ContactTag, ConversationTag, Tag
from apps.api.app.services import event_bus, messaging, routing

from .context import (
    STATUS_DELAYED,
    STATUS_WAITING_BUTTON,
    STATUS_WAITING_REPLY,
    ExecutionContext,
    NodeResult,
    render_value,
)

log = logging.getLogger("smartchat.flow.actions")

EXTERNAL_TIMEOUT_S = 10.0
EXTERNAL_RETRIES = 2
EXTERNAL_MAX_REDIRECTS = 5
FLOW_TEST_HEADER = "X-Flow-Test"


# ==========================================================================
# helpers
# ==========================================================================
def _contact_writable(field: str) -> bool:
    return field in {
        "display_name", "remark_name", "email", "phone", "language",
        "country", "city", "timezone", "device", "browser", "os",
    }


async def _send_content(ctx: ExecutionContext, content: dict[str, Any]) -> None:
    """Send a rendered MessageContent through the shared outbound pipeline."""
    if ctx.conversation is None:
        return
    result = await messaging.send_message(
        ctx.session,
        conversation=ctx.conversation,
        sender_type="automation",
        sender_id=None,
        content=content,
        sent_via="automation",
        source_flow_id=ctx.flow_session.flow_id,
        redis=ctx.redis,
        now=ctx.now,
    )
    ctx.events.extend(result.events)


def _prompt_content(prompt: Any, ns: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce an ask/quick_buttons prompt into a MessageContent dict."""
    if prompt is None:
        return None
    if isinstance(prompt, str):
        rendered = render_value(prompt, ns)
        return {"blocks": [{"kind": "text", "text": rendered}]}
    if isinstance(prompt, dict):
        if "blocks" in prompt:
            return render_value(prompt, ns)
        if "text" in prompt:
            return {"blocks": [{"kind": "text", "text": render_value(prompt["text"], ns)}]}
    return None


# ==========================================================================
# messaging actions
# ==========================================================================
async def act_send_message(ctx: ExecutionContext, node: Node) -> NodeResult:
    blocks = node.data.get("blocks")
    ns = ctx.namespaces()
    if blocks:
        content = {"blocks": render_value(blocks, ns)}
    elif node.data.get("text") is not None:
        content = {"blocks": [{"kind": "text", "text": ctx.render(str(node.data["text"]))}]}
    else:
        return NodeResult.next("out", step_status="skipped")
    try:
        await _send_content(ctx, content)
    except messaging.SendError as e:
        log.info("send_message action blocked: %s", e.code)
        return NodeResult.next("out", step_status="error")
    return NodeResult.next("out")


async def act_promo_card(ctx: ExecutionContext, node: Node) -> NodeResult:
    ns = ctx.namespaces()
    card = node.data.get("card") or node.data.get("product_card")
    if not card:
        return NodeResult.next("out", step_status="skipped")
    block = render_value({**card, "kind": "product_card"}, ns)
    try:
        await _send_content(ctx, {"blocks": [block]})
    except messaging.SendError:
        return NodeResult.next("out", step_status="error")
    return NodeResult.next("out")


async def act_send_email(ctx: ExecutionContext, node: Node) -> NodeResult:
    ns = ctx.namespaces()
    block = {
        "kind": "email",
        "subject": render_value(node.data.get("subject") or "", ns),
        "text": render_value(node.data.get("body") or node.data.get("text") or "", ns),
    }
    try:
        await _send_content(ctx, {"blocks": [block]})
    except messaging.SendError:
        return NodeResult.next("out", step_status="error")
    return NodeResult.next("out")


async def act_ask(ctx: ExecutionContext, node: Node) -> NodeResult:
    """問詢: send the prompt, then suspend waiting for the visitor's reply. The
    captured answer is written to vars.<variable> (and optionally a contact
    field) by the interpreter on resume."""
    ns = ctx.namespaces()
    prompt = _prompt_content(node.data.get("prompt") or node.data.get("question"), ns)
    if prompt is not None:
        try:
            await _send_content(ctx, prompt)
        except messaging.SendError:
            pass
    timeout_s = node.data.get("timeout_s")
    wakeup = ctx.now + timedelta(seconds=int(timeout_s)) if timeout_s else None
    waiting = {
        "type": "ask",
        "node_id": node.id,
        "variable": node.data.get("variable") or node.data.get("save_to") or "answer",
        "save_to_contact": node.data.get("save_to_contact"),
        "validation": node.data.get("validation"),
        "timeout_s": timeout_s,
    }
    return NodeResult.wait(STATUS_WAITING_REPLY, waiting=waiting, wakeup_at=wakeup)


async def act_quick_buttons(ctx: ExecutionContext, node: Node) -> NodeResult:
    """快捷按鈕: each button becomes a branch port button:<id>. Sends the button
    prompt then suspends until a tap (button:<id>), a free-text reply
    (typed_reply) or the optional timeout."""
    ns = ctx.namespaces()
    raw_buttons = node.data.get("buttons") or []
    buttons: list[dict[str, str]] = []
    for i, b in enumerate(raw_buttons):
        if isinstance(b, dict):
            bid = str(b.get("id") or b.get("value") or i)
            text = render_value(str(b.get("text") or b.get("label") or bid), ns)
        else:
            bid, text = str(i), render_value(str(b), ns)
        buttons.append({"id": bid, "text": text})
    text = ctx.render(str(node.data.get("text") or ""))
    if buttons:
        try:
            await _send_content(
                ctx, {"blocks": [{"kind": "quick_buttons", "text": text, "buttons": buttons}]}
            )
        except messaging.SendError:
            pass
    timeout_s = node.data.get("timeout_s")
    wakeup = ctx.now + timedelta(seconds=int(timeout_s)) if timeout_s else None
    waiting = {
        "type": "buttons",
        "node_id": node.id,
        "button_ids": [b["id"] for b in buttons],
        "timeout_s": timeout_s,
    }
    return NodeResult.wait(STATUS_WAITING_BUTTON, waiting=waiting, wakeup_at=wakeup)


# ==========================================================================
# timing
# ==========================================================================
async def act_delay(ctx: ExecutionContext, node: Node) -> NodeResult:
    seconds = int(node.data.get("seconds") or node.data.get("duration_s") or 0)
    if node.data.get("minutes"):
        seconds += int(node.data["minutes"]) * 60
    if node.data.get("hours"):
        seconds += int(node.data["hours"]) * 3600
    if node.data.get("days"):
        seconds += int(node.data["days"]) * 86400
    if seconds <= 0:
        return NodeResult.next("out")
    wakeup = ctx.now + timedelta(seconds=seconds)
    return NodeResult.wait(STATUS_DELAYED, waiting={"type": "delay", "node_id": node.id}, wakeup_at=wakeup)


# ==========================================================================
# tags
# ==========================================================================
async def _resolve_tag_ids(ctx: ExecutionContext, node: Node, kind: str) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for raw in node.data.get("tag_ids") or []:
        try:
            ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    names = [ctx.render(str(n)) for n in (node.data.get("tag_names") or [])]
    for name in names:
        name = name.strip()
        if not name:
            continue
        stmt = (
            pg_insert(Tag)
            .values(workspace_id=ctx.workspace_id, kind=kind, name=name[:64])
            .on_conflict_do_nothing(index_elements=["workspace_id", "kind", "name"])
            .returning(Tag.id)
        )
        res = (await ctx.session.execute(stmt)).scalar_one_or_none()
        if res is not None:
            ids.append(res)
        else:
            existing = (
                await ctx.session.execute(
                    select(Tag.id).where(
                        Tag.workspace_id == ctx.workspace_id, Tag.kind == kind, Tag.name == name[:64]
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                ids.append(existing)
    return ids


async def act_add_contact_tag(ctx: ExecutionContext, node: Node) -> NodeResult:
    if ctx.contact is None:
        return NodeResult.next("out", step_status="skipped")
    ids = await _resolve_tag_ids(ctx, node, "contact")
    for tid in ids:
        await ctx.session.execute(
            pg_insert(ContactTag)
            .values(workspace_id=ctx.workspace_id, contact_id=ctx.contact.id, tag_id=tid)
            .on_conflict_do_nothing(index_elements=["contact_id", "tag_id"])
        )
    return NodeResult.next("out")


async def act_add_conversation_tag(ctx: ExecutionContext, node: Node) -> NodeResult:
    if ctx.conversation is None:
        return NodeResult.next("out", step_status="skipped")
    ids = await _resolve_tag_ids(ctx, node, "conversation")
    for tid in ids:
        await ctx.session.execute(
            pg_insert(ConversationTag)
            .values(
                workspace_id=ctx.workspace_id, conversation_id=ctx.conversation.id, tag_id=tid
            )
            .on_conflict_do_nothing(index_elements=["conversation_id", "tag_id"])
        )
    return NodeResult.next("out")


# ==========================================================================
# contact mutations
# ==========================================================================
async def act_update_contact(ctx: ExecutionContext, node: Node) -> NodeResult:
    if ctx.contact is None:
        return NodeResult.next("out", step_status="skipped")
    ns = ctx.namespaces()
    fields = node.data.get("fields") or {}
    custom = dict(ctx.contact.custom or {})
    custom_touched = False
    changed: dict[str, Any] = {}
    for key, raw in fields.items():
        value = render_value(raw, ns)
        if key.startswith("custom."):
            custom[key.split(".", 1)[1]] = value
            custom_touched = True
            changed[key] = value
        elif _contact_writable(key):
            setattr(ctx.contact, key, value)
            changed[key] = value
    if custom_touched:
        ctx.contact.custom = custom
    if changed:
        ev = Event(
            workspace_id=ctx.workspace_id,
            type="contact.updated",
            actor=Actor(type="flow", id=ctx.flow_session.flow_id),
            contact_id=ctx.contact.id,
            payload={"contact_id": str(ctx.contact.id), "changed": list(changed.keys()),
                     "source": "flow"},
        )
        await event_bus.emit(ctx.session, ev)
        ctx.events.append(ev)
    return NodeResult.next("out")


async def act_add_to_blacklist(ctx: ExecutionContext, node: Node) -> NodeResult:
    if ctx.contact is None:
        return NodeResult.next("out", step_status="skipped")
    if not ctx.contact.is_blacklisted:
        ctx.contact.is_blacklisted = True
        ev = Event(
            workspace_id=ctx.workspace_id,
            type="contact.updated",
            actor=Actor(type="flow", id=ctx.flow_session.flow_id),
            contact_id=ctx.contact.id,
            payload={"contact_id": str(ctx.contact.id), "is_blacklisted": True, "source": "flow"},
        )
        await event_bus.emit(ctx.session, ev)
        ctx.events.append(ev)
    return NodeResult.next("out")


# ==========================================================================
# conversation lifecycle (via routing service)
# ==========================================================================
async def act_assign_agent(ctx: ExecutionContext, node: Node) -> NodeResult:
    if ctx.conversation is None or ctx.redis is None:
        return NodeResult.next("out", step_status="skipped")
    member_id: uuid.UUID | None = None
    raw = node.data.get("member_id")
    if raw:
        try:
            member_id = uuid.UUID(str(raw))
        except ValueError:
            member_id = None
    try:
        result = await routing.transfer(
            ctx.session,
            ctx.redis,
            workspace_id=ctx.workspace_id,
            conversation_id=ctx.conversation.id,
            to_member_id=member_id,
            actor=Actor(type="flow", id=ctx.flow_session.flow_id),
            reason="flow_assign",
        )
        ctx.events.extend(result.events)
    except LookupError:
        return NodeResult.next("out", step_status="error")
    return NodeResult.next("out")


async def act_transfer_unassigned(ctx: ExecutionContext, node: Node) -> NodeResult:
    if ctx.conversation is None or ctx.redis is None:
        return NodeResult.next("out", step_status="skipped")
    try:
        result = await routing.transfer(
            ctx.session,
            ctx.redis,
            workspace_id=ctx.workspace_id,
            conversation_id=ctx.conversation.id,
            to_member_id=None,
            actor=Actor(type="flow", id=ctx.flow_session.flow_id),
            reason="flow_unassign",
        )
        ctx.events.extend(result.events)
    except LookupError:
        return NodeResult.next("out", step_status="error")
    return NodeResult.next("out")


async def act_close_conversation(ctx: ExecutionContext, node: Node) -> NodeResult:
    """結束會話 — also a terminal node (flow completes)."""
    if ctx.conversation is None or ctx.redis is None:
        return NodeResult.end("completed")
    try:
        result = await routing.close_conversation(
            ctx.session,
            ctx.redis,
            workspace_id=ctx.workspace_id,
            conversation_id=ctx.conversation.id,
            actor=Actor(type="flow", id=ctx.flow_session.flow_id),
            closed_by_type="flow",
            now=ctx.now,
        )
        if result is not None:
            ctx.events.extend(result.events)
    except LookupError:
        pass
    return NodeResult.end("completed")


async def act_invite_rating(ctx: ExecutionContext, node: Node) -> NodeResult:
    """邀請評價 (CSAT invite): drop a system chip + optional invite message."""
    if ctx.conversation is None:
        return NodeResult.next("out", step_status="skipped")
    prompt = _prompt_content(node.data.get("prompt"), ctx.namespaces())
    if prompt is not None:
        try:
            await _send_content(ctx, prompt)
        except messaging.SendError:
            pass
    msg = await messaging.add_system_event(
        ctx.session,
        conversation=ctx.conversation,
        event="csat_invited",
        meta={"scale": node.data.get("scale", 5), "source_flow_id": str(ctx.flow_session.flow_id)},
        actor=Actor(type="flow", id=ctx.flow_session.flow_id),
        now=ctx.now,
    )
    ev = messaging._message_event(
        msg, ctx.conversation, Actor(type="flow", id=ctx.flow_session.flow_id),
        requires_channel_send=False,
    )
    ctx.events.append(ev)
    return NodeResult.next("out")


# ==========================================================================
# external request (HTTP) — SSRF-guarded
# ==========================================================================
class SsrfBlocked(Exception):
    pass


def is_blocked_ip(ip_str: str) -> bool:
    """Reject any non-public address (SSRF guard)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_host_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return list({info[4][0] for info in infos})


def assert_public_url(url: str) -> None:
    """Raise SsrfBlocked unless the URL is http(s) to a public host whose every
    resolved IP is public."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SsrfBlocked(f"scheme not allowed: {parts.scheme}")
    host = parts.hostname
    if not host:
        raise SsrfBlocked("missing host")
    # a literal IP is checked directly; a name is DNS-resolved (all records)
    try:
        ips = resolve_host_ips(host)
    except socket.gaierror as e:
        raise SsrfBlocked(f"dns resolution failed: {host}") from e
    if not ips:
        raise SsrfBlocked(f"no addresses for {host}")
    for ip in ips:
        if is_blocked_ip(ip):
            raise SsrfBlocked(f"blocked address {ip} for {host}")


def json_extract(data: Any, path: str) -> Any:
    """Minimal JSONPath: ``$.a.b[0].c`` / ``a.b`` — dotted keys + [index]."""
    if path in ("", "$", "$."):
        return data
    cur = data
    p = path[2:] if path.startswith("$.") else (path[1:] if path.startswith("$") else path)
    token = ""

    def _step(node: Any, key: str) -> Any:
        if key == "":
            return node
        if isinstance(node, dict):
            return node.get(key)
        return None

    i = 0
    while i < len(p):
        ch = p[i]
        if ch == ".":
            cur = _step(cur, token)
            token = ""
        elif ch == "[":
            cur = _step(cur, token)
            token = ""
            j = p.index("]", i)
            idx_raw = p[i + 1 : j].strip().strip("'\"")
            if isinstance(cur, list):
                try:
                    cur = cur[int(idx_raw)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(cur, dict):
                cur = cur.get(idx_raw)
            else:
                return None
            i = j
        else:
            token += ch
        i += 1
    return _step(cur, token)


async def _external_once(
    method: str, url: str, *, headers: dict[str, str], content: Any
) -> httpx.Response:
    """One request with manual redirect following + per-hop SSRF re-check."""
    assert_public_url(url)
    async with httpx.AsyncClient(follow_redirects=False, timeout=EXTERNAL_TIMEOUT_S) as client:
        current = url
        for _ in range(EXTERNAL_MAX_REDIRECTS + 1):
            resp = await client.request(method, current, headers=headers, content=content)
            if resp.is_redirect and resp.headers.get("location"):
                current = str(resp.next_request.url) if resp.next_request else resp.headers["location"]
                assert_public_url(current)  # re-check after redirect
                continue
            return resp
        raise SsrfBlocked("too many redirects")


async def act_external_request(ctx: ExecutionContext, node: Node) -> NodeResult:
    """外部請求: templated HTTP call, SSRF-guarded, JSONPath extraction into
    ext.<node>. Follows success / failed (default failed on any error)."""
    ns = ctx.namespaces()
    method = str(node.data.get("method") or "GET").upper()
    url = ctx.render(str(node.data.get("url") or "")).strip()
    headers = {str(k): str(render_value(v, ns)) for k, v in (node.data.get("headers") or {}).items()}
    if ctx.test_mode:
        headers[FLOW_TEST_HEADER] = "1"
    body = node.data.get("body")
    content: Any = None
    if body is not None:
        rendered = render_value(body, ns)
        if isinstance(rendered, (dict, list)):
            import json as _json

            content = _json.dumps(rendered)
            headers.setdefault("Content-Type", "application/json")
        else:
            content = str(rendered)

    if not url:
        ctx.set_ext(node.id, {"ok": False, "error": "no_url"})
        return NodeResult.next("failed", step_status="error")

    resp: httpx.Response | None = None
    error: str | None = None
    for attempt in range(EXTERNAL_RETRIES + 1):
        try:
            resp = await _external_once(method, url, headers=headers, content=content)
            break
        except SsrfBlocked as e:
            error = f"ssrf_blocked:{e}"
            break  # never retry a blocked host
        except (httpx.HTTPError, OSError) as e:
            error = str(e)
            if attempt >= EXTERNAL_RETRIES:
                break

    if resp is None:
        ctx.set_ext(node.id, {"ok": False, "error": error})
        return NodeResult.next("failed", step_status="error")

    parsed: Any = None
    try:
        parsed = resp.json()
    except Exception:  # noqa: BLE001
        parsed = resp.text

    ext_bucket: dict[str, Any] = {
        "ok": resp.is_success,
        "status": resp.status_code,
        "body": parsed if isinstance(parsed, (dict, list)) else None,
        "text": parsed if isinstance(parsed, str) else None,
    }
    for var_name, jpath in (node.data.get("extract") or {}).items():
        ext_bucket[str(var_name)] = json_extract(parsed, str(jpath))
    ctx.set_ext(node.id, ext_bucket)

    return NodeResult.next("success" if resp.is_success else "failed",
                           step_status="ok" if resp.is_success else "error")


# ==========================================================================
# dispatch table
# ==========================================================================
ACTION_DISPATCH = {
    "send_message": act_send_message,
    "promo_card": act_promo_card,
    "send_email": act_send_email,
    "ask": act_ask,
    "quick_buttons": act_quick_buttons,
    "delay": act_delay,
    "add_contact_tag": act_add_contact_tag,
    "add_conversation_tag": act_add_conversation_tag,
    "update_contact": act_update_contact,
    "add_to_blacklist": act_add_to_blacklist,
    "assign_agent": act_assign_agent,
    "transfer_unassigned": act_transfer_unassigned,
    "close_conversation": act_close_conversation,
    "invite_rating": act_invite_rating,
    "external_request": act_external_request,
}
