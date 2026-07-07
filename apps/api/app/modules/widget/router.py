"""Visitor-facing widget REST API (contract: apps/widget/README.md).

All authenticated endpoints carry `Authorization: Bearer <visitor_token>`
(minted at /session, scoped to exactly one channel_identity). Message sends
run the shared channel ingress pipeline SYNCHRONOUSLY so the response can
include the persisted message while keeping the identical routing / unread /
event path used by webhook channels.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, UploadFile
from py_contracts.content import MessageContent
from pydantic import BaseModel, Field
from redis import asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.base import MessageIn, ProfileHint
from ...channels.ingress_pipeline import handle_events
from ...channels.media import file_public_url, get_media_store
from ...channels.registry import get_adapter
from ...db import get_session, session_factory
from ...models.contacts import ChannelIdentity, Contact
from ...models.conversations import Conversation
from ...models.messaging import Message, MessageDedup
from ...realtime import presence
from ...realtime.hub import collect_replay
from ...realtime.protocol import (
    ResumeAction,
    VisitorScope,
    VisitorTokenInvalid,
    filter_for_visitor,
    mint_visitor_token,
    verify_visitor_token,
)
from ...realtime.publisher import current_seq
from ...services.redis_client import get_redis
from . import service

router = APIRouter(prefix="/api/v1/widget", tags=["widget"])

_MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB
_MAX_TEXT = 4000


# --------------------------------------------------------------------- auth
async def visitor_scope(authorization: str = Header(default="")) -> VisitorScope:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing visitor token")
    try:
        return verify_visitor_token(authorization.removeprefix("Bearer ").strip())
    except VisitorTokenInvalid as e:
        raise HTTPException(401, "invalid visitor token") from e


async def scoped_identity(
    scope: VisitorScope = Depends(visitor_scope),
    session: AsyncSession = Depends(get_session),
) -> ChannelIdentity:
    identity = await service.get_identity(session, scope.channel_identity_id)
    if identity is None or identity.workspace_id != scope.workspace_id:
        raise HTTPException(401, "unknown visitor identity")
    return identity


# ---------------------------------------------------------------- bootstrap
@router.get("/bootstrap")
async def bootstrap(
    key: str = Query(min_length=4, max_length=64),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    widget = await service.get_widget_by_key(session, key)
    if widget is None:
        raise HTTPException(404, "widget not found")
    return await service.assemble_bootstrap(session, get_redis(), widget)


# ------------------------------------------------------------------ session
class SessionBody(BaseModel):
    widget_key: str
    visitor_token: str | None = None
    login_info: dict[str, Any] | None = None
    page: dict[str, Any] | None = None
    lang: str | None = None


@router.post("/session")
async def open_session(
    body: SessionBody, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    widget = await service.get_widget_by_key(session, body.widget_key)
    if widget is None:
        raise HTTPException(404, "widget not found")
    acct = await service.widget_channel_account(session, widget)
    if acct is None or not acct.enabled:
        raise HTTPException(404, "widget channel unavailable")

    identity: ChannelIdentity | None = None
    conversation: Conversation | None = None
    if body.visitor_token:
        try:
            scope = verify_visitor_token(body.visitor_token)
            candidate = await service.get_identity(session, scope.channel_identity_id)
            if candidate is not None and candidate.channel_account_id == acct.id:
                identity = candidate
                conversation = await service.touch_returning_visitor(session, identity)
        except VisitorTokenInvalid:
            identity = None
    if identity is None:
        identity, _contact, conversation = await service.create_visitor(
            session, acct, login_info=body.login_info, lang=body.lang, page=body.page
        )
    elif body.login_info:
        await service.apply_login_info(session, identity, body.login_info)
    if body.page and body.page.get("url"):
        await service.record_visitor_event(
            session,
            identity,
            event="page_view",
            url=body.page.get("url"),
            title=body.page.get("title"),
            referrer=body.page.get("referrer"),
        )
    await session.commit()

    redis = get_redis()
    await presence.mark_visitor_online(redis, identity.workspace_id, identity.id)
    token = mint_visitor_token(
        identity.workspace_id,
        identity.id,
        conversation_id=conversation.id if conversation else None,
    )
    return {
        "visitor_token": token,
        "contact_id": str(identity.contact_id),
        "conversation_id": str(conversation.id) if conversation else None,
        "seq": await current_seq(redis, identity.workspace_id),
    }


# ----------------------------------------------------------------- identify
class IdentifyBody(BaseModel):
    login_info: dict[str, Any]


@router.post("/identify")
async def identify(
    body: IdentifyBody,
    identity: ChannelIdentity = Depends(scoped_identity),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await service.apply_login_info(session, identity, body.login_info)
    await session.commit()
    return {"ok": True}


# ----------------------------------------------------------------- messages
def _serialize_message(m: Message) -> dict[str, Any]:
    content = dict(m.content or {"blocks": []})
    for block in content.get("blocks", []):
        if isinstance(block, dict) and block.get("file_id") and not block.get("url"):
            block["url"] = file_public_url(block["file_id"])
        if isinstance(block, dict) and block.get("image_file_id") and not block.get("image_url"):
            block["image_url"] = file_public_url(block["image_file_id"])
    return {
        "id": str(m.id),
        "conversation_id": str(m.conversation_id),
        "sender_type": m.sender_type,
        "content": content,
        "client_msg_id": m.client_msg_id,
        "created_at": (m.created_at or datetime.now(UTC)).isoformat(),
        "delivery_status": m.delivery_status,
        "seq": None,
    }


@router.get("/messages")
async def list_messages(
    before: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    identity: ChannelIdentity = Depends(scoped_identity),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    conv = (
        await session.execute(
            select(Conversation).where(Conversation.channel_identity_id == identity.id)
        )
    ).scalar_one_or_none()
    if conv is None:
        return {"messages": []}
    q = (
        select(Message)
        .where(
            Message.conversation_id == conv.id,
            Message.is_note.is_(False),
        )
        .order_by(Message.id.desc())
        .limit(limit)
    )
    if before:
        try:
            q = q.where(Message.id < uuid.UUID(before))
        except ValueError as e:
            raise HTTPException(422, "bad cursor") from e
    rows = list((await session.execute(q)).scalars())
    rows.reverse()  # ascending created_at for the widget
    return {"messages": [_serialize_message(m) for m in rows]}


class SendBody(BaseModel):
    client_msg_id: str = Field(min_length=8, max_length=64)
    content: MessageContent


@router.post("/messages")
async def send_message(
    body: SendBody,
    identity: ChannelIdentity = Depends(scoped_identity),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    text = body.content.plain_text()
    if len(text) > _MAX_TEXT:
        raise HTTPException(422, "message too long")
    contact = await session.get(Contact, identity.contact_id)
    if contact is not None and contact.is_blacklisted:
        raise HTTPException(403, "blocked")

    external_message_id = f"wg:{body.client_msg_id}"
    ev = MessageIn(
        external_message_id=external_message_id,
        external_user_id=identity.external_user_id,
        content=body.content,
        external_timestamp=datetime.now(UTC),
        profile=ProfileHint(display_name=identity.display_name),
    )
    redis = get_redis()
    # Synchronous run of the shared ingress pipeline (dedup makes it
    # idempotent on client_msg_id retries).
    await handle_events(
        session_factory(),
        redis,
        identity.channel_account_id,
        [ev],
        adapter=get_adapter("widget"),
    )
    dedup = await session.get(MessageDedup, (identity.channel_account_id, external_message_id))
    if dedup is None:
        raise HTTPException(500, "message not accepted")
    message = await session.get(Message, dedup.message_id)
    if message is None:
        raise HTTPException(500, "message not found after ingest")
    await presence.heartbeat_visitor(redis, identity.workspace_id, identity.id)
    return {
        "message": _serialize_message(message),
        "seq": await current_seq(redis, identity.workspace_id),
    }


# ------------------------------------------------------------------ uploads
@router.post("/uploads")
async def upload(
    file: UploadFile,
    identity: ChannelIdentity = Depends(scoped_identity),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        raise HTTPException(413, "file too large")
    if not data:
        raise HTTPException(422, "empty file")
    store = get_media_store()
    row = await store.store_bytes(
        session,
        workspace_id=identity.workspace_id,
        data=data,
        mime=file.content_type,
        filename=file.filename,
        created_by_type="contact",
        created_by_id=identity.contact_id,
    )
    await session.commit()
    return {
        "file_id": str(row.id),
        "url": file_public_url(row.id),
        "mime": row.mime,
        "size": row.size,
        "name": row.original_name,
    }


# --------------------------------------------------------------------- lead
class LeadBody(BaseModel):
    fields: dict[str, Any]
    page: dict[str, Any] | None = None


@router.post("/lead")
async def submit_lead(
    body: LeadBody,
    identity: ChannelIdentity = Depends(scoped_identity),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    login_like = {
        "user_name": body.fields.get("name") or body.fields.get("user_name"),
        "email": body.fields.get("email"),
        "phone": body.fields.get("phone"),
    }
    await service.apply_login_info(session, identity, {k: v for k, v in login_like.items() if v})
    contact = await session.get(Contact, identity.contact_id)
    if contact is not None:
        extra = {
            k: v
            for k, v in body.fields.items()
            if k not in ("name", "user_name", "email", "phone") and v is not None
        }
        if extra:
            contact.custom = {**(contact.custom or {}), **extra}
    await service.record_visitor_event(
        session,
        identity,
        event="lead_submit",
        url=(body.page or {}).get("url"),
        props=body.fields,
    )
    await session.commit()
    return {"ok": True}


# -------------------------------------------------------------------- track
class TrackBody(BaseModel):
    event: str = Field(min_length=1, max_length=48)
    props: dict[str, Any] | None = None
    page: dict[str, Any] | None = None


@router.post("/track")
async def track(
    body: TrackBody,
    identity: ChannelIdentity = Depends(scoped_identity),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    page = body.page or {}
    await service.record_visitor_event(
        session,
        identity,
        event=body.event if body.event in ("page_view", "widget_open") else body.event[:48],
        url=page.get("url"),
        title=page.get("title"),
        referrer=page.get("referrer"),
        props=body.props,
    )
    await session.commit()
    redis = get_redis()
    await presence.heartbeat_visitor(redis, identity.workspace_id, identity.id)
    return {"ok": True}


# ------------------------------------------------------------ events (poll)
@router.get("/events")
async def poll_events(
    cursor: int = Query(default=0, ge=0),
    wait: int = Query(default=25, ge=0, le=25),
    scope: VisitorScope = Depends(visitor_scope),
) -> dict[str, Any]:
    """Long-poll fallback: replay from the workspace stream filtered to this
    visitor; when nothing is pending, block on new stream entries up to
    `wait` seconds (same seq protocol as the WS gateway)."""
    redis = get_redis()
    await presence.heartbeat_visitor(redis, scope.workspace_id, scope.channel_identity_id)

    async def replay() -> tuple[list[dict[str, Any]], int, bool]:
        action, events, safe_cursor = await collect_replay(redis, scope.workspace_id, cursor)
        if action is ResumeAction.RESYNC:
            return [], safe_cursor, True
        out = []
        for ev in events:
            visible = filter_for_visitor(ev, scope)
            if visible is not None and ev.seq is not None:
                out.append({"seq": ev.seq, "event": visible})
        return out, safe_cursor, False

    items, new_cursor, resync = await replay()
    if resync:
        return {"events": [], "cursor": new_cursor, "resync_required": True}
    if items or wait == 0:
        return {"events": items, "cursor": new_cursor}

    # nothing pending — block for new entries then re-filter
    stream = f"evt:{scope.workspace_id}"
    try:
        await redis.xread({stream: "$"}, block=wait * 1000, count=1)
    except aioredis.RedisError:
        pass
    items, new_cursor, resync = await replay()
    if resync:
        return {"events": [], "cursor": new_cursor, "resync_required": True}
    return {"events": items, "cursor": new_cursor}
