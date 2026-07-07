"""收件匣 (inbox workbench) API.

Left column: system tabs 我的/機器人/AI成員/待分配/全部/團隊 + saved views.
Middle: conversation list (search 名稱/備註/手機/郵箱; filters 全部/未讀/待回覆),
cursor-paginated. Right: conversation detail — messages (cursor pagination),
reply / internal note, read-cursor advance, assign / transfer / claim /
close / reopen, conversation tags, translation toggle, 託管 toggle.

Uplink is REST-only (plan A.8): sends carry client_msg_id for idempotency;
realtime events go out via the ws-gateway publisher after commit.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from py_contracts.events import Actor
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import MemberContext, current_member, require_permission
from ...models.contacts import Contact
from ...models.conversations import Conversation, ConversationSession
from ...models.members import MemberGroupMember
from ...models.messaging import Message
from ...models.misc import ConversationTag, SavedView, Tag
from ...services import messaging, routing
from ...services.messaging import SendError, publish_realtime
from ...services.redis_client import get_redis

router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])

MAX_PAGE = 100

Tab = Literal["mine", "bot", "ai", "unassigned", "all", "team"]
ListFilter = Literal["all", "unread", "needs_reply"]


# ==========================================================================
# schemas
# ==========================================================================
class ContactBrief(BaseModel):
    id: uuid.UUID
    display_name: str
    remark_name: str | None
    avatar_url: str | None
    email: str | None
    phone: str | None
    language: str | None
    country: str | None

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: uuid.UUID
    channel_type: str
    channel_account_id: uuid.UUID
    contact_id: uuid.UUID
    status: str
    handler: str
    assignee_member_id: uuid.UUID | None
    bot_managed: bool
    ai_state: str
    needs_reply: bool
    agent_unread_count: int
    customer_window_expires_at: datetime | None
    snippet: str | None
    translation: dict[str, Any]
    session_count: int
    last_message_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationListItem(BaseModel):
    conversation: ConversationOut
    contact: ContactBrief
    tag_ids: list[uuid.UUID] = Field(default_factory=list)


class ConversationListOut(BaseModel):
    items: list[ConversationListItem]
    next_cursor: str | None = None


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    contact: ContactBrief
    tags: list[dict[str, Any]]


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    direction: str
    sender_type: str
    sender_id: uuid.UUID | None
    msg_type: str
    content: dict[str, Any]
    text_plain: str | None
    is_note: bool
    sent_via: str | None
    delivery_status: str
    client_msg_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageListOut(BaseModel):
    items: list[MessageOut]  # newest → oldest
    next_cursor: str | None = None


class SendIn(BaseModel):
    content: dict[str, Any] | None = None  # MessageContent {blocks: [...]}
    quick_reply_id: uuid.UUID | None = None
    is_note: bool = False
    client_msg_id: str | None = Field(default=None, max_length=64)


class ReadIn(BaseModel):
    last_read_message_id: uuid.UUID | None = None


class AssignIn(BaseModel):
    member_id: uuid.UUID | None = None  # None → 轉未分配


class TagsIn(BaseModel):
    tag_ids: list[uuid.UUID]


class TranslationIn(BaseModel):
    enabled: bool | None = None
    agent_lang: str | None = Field(default=None, max_length=16)
    customer_lang: str | None = Field(default=None, max_length=16)


class ManagedIn(BaseModel):
    managed: bool


class SessionOut(BaseModel):
    id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    first_response_at: datetime | None
    opened_by: str
    closed_by_type: str | None
    handler_at_close: str | None
    message_count: int
    contact_message_count: int
    agent_message_count: int
    csat_score: int | None

    model_config = {"from_attributes": True}


class ViewIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    visibility: Literal["private", "public"] = "private"
    filters: dict[str, Any] = Field(default_factory=dict)
    sort_order: int = 0


class ViewOut(ViewIn):
    id: uuid.UUID
    owner_member_id: uuid.UUID | None

    model_config = {"from_attributes": True}


# ==========================================================================
# cursor helpers
# ==========================================================================
def _encode_cursor(ts: datetime, row_id: uuid.UUID) -> str:
    raw = f"{ts.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_s, id_s = raw.split("|", 1)
        return datetime.fromisoformat(ts_s), uuid.UUID(id_s)
    except Exception as e:
        raise HTTPException(422, detail="invalid cursor") from e


# ==========================================================================
# saved views CRUD
# ==========================================================================
@router.get("/views", response_model=list[ViewOut])
async def list_views(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[ViewOut]:
    rows = (
        await session.execute(
            select(SavedView)
            .where(
                SavedView.workspace_id == member.workspace_id,
                SavedView.module == "inbox",
                or_(
                    SavedView.visibility == "public",
                    SavedView.owner_member_id == member.member_id,
                ),
            )
            .order_by(SavedView.sort_order, SavedView.created_at)
        )
    ).scalars().all()
    return [ViewOut.model_validate(v) for v in rows]


@router.post("/views", response_model=ViewOut, status_code=201)
async def create_view(
    body: ViewIn,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> ViewOut:
    row = SavedView(
        workspace_id=member.workspace_id,
        module="inbox",
        name=body.name,
        visibility=body.visibility,
        owner_member_id=member.member_id,
        filters=body.filters,
        sort_order=body.sort_order,
    )
    session.add(row)
    await session.commit()
    return ViewOut.model_validate(row)


@router.patch("/views/{view_id}", response_model=ViewOut)
async def update_view(
    view_id: uuid.UUID,
    body: ViewIn,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> ViewOut:
    row = await session.get(SavedView, view_id)
    if row is None or row.workspace_id != member.workspace_id or row.module != "inbox":
        raise HTTPException(404, detail="view not found")
    if row.owner_member_id != member.member_id and not member.can("settings.manage"):
        raise HTTPException(403, detail="not your view")
    row.name = body.name
    row.visibility = body.visibility
    row.filters = body.filters
    row.sort_order = body.sort_order
    await session.commit()
    return ViewOut.model_validate(row)


@router.delete("/views/{view_id}", status_code=204)
async def delete_view(
    view_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(SavedView, view_id)
    if row is None or row.workspace_id != member.workspace_id or row.module != "inbox":
        raise HTTPException(404, detail="view not found")
    if row.owner_member_id != member.member_id and not member.can("settings.manage"):
        raise HTTPException(403, detail="not your view")
    await session.delete(row)
    await session.commit()


# ==========================================================================
# conversation list
# ==========================================================================
async def _team_member_ids(session: AsyncSession, member: MemberContext) -> list[uuid.UUID]:
    group_ids = (
        await session.execute(
            select(MemberGroupMember.group_id).where(
                MemberGroupMember.workspace_id == member.workspace_id,
                MemberGroupMember.member_id == member.member_id,
            )
        )
    ).scalars().all()
    if not group_ids:
        return [member.member_id]
    return list(
        (
            await session.execute(
                select(MemberGroupMember.member_id)
                .where(MemberGroupMember.group_id.in_(group_ids))
                .distinct()
            )
        ).scalars()
    )


@router.get("/conversations", response_model=ConversationListOut)
async def list_conversations(
    tab: Tab = Query(default="mine"),
    filter: ListFilter = Query(default="all"),
    q: str | None = None,
    channel_type: str | None = None,
    tag_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, pattern="^(open|closed)$"),
    view_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=MAX_PAGE),
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> ConversationListOut:
    # saved view = stored defaults; explicit query params win
    if view_id is not None:
        view = await session.get(SavedView, view_id)
        if view is None or view.workspace_id != member.workspace_id or view.module != "inbox":
            raise HTTPException(404, detail="view not found")
        f = view.filters or {}
        tab = tab if tab != "mine" or "tab" not in f else f.get("tab", tab)
        filter = filter if filter != "all" or "filter" not in f else f.get("filter", filter)
        q = q or f.get("q")
        channel_type = channel_type or f.get("channel_type")
        status = status or f.get("status")
        if tag_id is None and f.get("tag_id"):
            try:
                tag_id = uuid.UUID(str(f["tag_id"]))
            except ValueError:
                tag_id = None

    if tab == "all" and not member.can("inbox.view_all"):
        raise HTTPException(403, detail={"code": "permission_denied", "permission": "inbox.view_all"})

    sort_ts = func.coalesce(Conversation.last_message_at, Conversation.created_at)
    query = (
        select(Conversation, Contact)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.workspace_id == member.workspace_id)
    )
    match tab:
        case "mine":
            query = query.where(Conversation.assignee_member_id == member.member_id)
        case "bot":
            query = query.where(Conversation.handler == "bot")
        case "ai":
            query = query.where(Conversation.handler == "ai_agent")
        case "unassigned":
            query = query.where(
                Conversation.handler == "unassigned", Conversation.status == "open"
            )
        case "team":
            team_ids = await _team_member_ids(session, member)
            query = query.where(Conversation.assignee_member_id.in_(team_ids))
        case "all":
            pass

    if status:
        query = query.where(Conversation.status == status)
    elif tab != "all":
        query = query.where(Conversation.status == "open")
    if filter == "unread":
        query = query.where(Conversation.agent_unread_count > 0)
    elif filter == "needs_reply":
        query = query.where(Conversation.needs_reply.is_(True))
    if channel_type:
        query = query.where(Conversation.channel_type == channel_type)
    if tag_id:
        query = query.where(
            select(ConversationTag.tag_id)
            .where(
                ConversationTag.conversation_id == Conversation.id,
                ConversationTag.tag_id == tag_id,
            )
            .exists()
        )
    if q:
        needle = f"%{q}%"
        query = query.where(
            or_(
                Contact.display_name.ilike(needle),
                Contact.remark_name.ilike(needle),
                Contact.phone.ilike(needle),
                Contact.email.ilike(needle),
            )
        )
    if cursor:
        ts, cid = _decode_cursor(cursor)
        query = query.where(or_(sort_ts < ts, and_(sort_ts == ts, Conversation.id < cid)))

    rows = (
        await session.execute(query.order_by(sort_ts.desc(), Conversation.id.desc()).limit(limit + 1))
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    conv_ids = [c.id for c, _ in rows]
    tags_by_conv: dict[uuid.UUID, list[uuid.UUID]] = {}
    if conv_ids:
        for conv_id, t_id in (
            await session.execute(
                select(ConversationTag.conversation_id, ConversationTag.tag_id).where(
                    ConversationTag.conversation_id.in_(conv_ids)
                )
            )
        ).all():
            tags_by_conv.setdefault(conv_id, []).append(t_id)

    items = [
        ConversationListItem(
            conversation=ConversationOut.model_validate(c),
            contact=ContactBrief.model_validate(ct),
            tag_ids=tags_by_conv.get(c.id, []),
        )
        for c, ct in rows
    ]
    next_cursor = None
    if has_more and rows:
        last_conv = rows[-1][0]
        next_cursor = _encode_cursor(
            last_conv.last_message_at or last_conv.created_at, last_conv.id
        )
    return ConversationListOut(items=items, next_cursor=next_cursor)


# ==========================================================================
# conversation detail + messages
# ==========================================================================
async def _get_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        raise HTTPException(404, detail="conversation not found")
    return conv


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
async def conversation_detail(
    conversation_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> ConversationDetailOut:
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    contact = await session.get(Contact, conv.contact_id)
    if contact is None:
        raise HTTPException(404, detail="contact not found")
    tags = (
        await session.execute(
            select(Tag)
            .join(ConversationTag, ConversationTag.tag_id == Tag.id)
            .where(ConversationTag.conversation_id == conversation_id)
            .order_by(Tag.name)
        )
    ).scalars().all()
    return ConversationDetailOut(
        conversation=ConversationOut.model_validate(conv),
        contact=ContactBrief.model_validate(contact),
        tags=[{"id": str(t.id), "name": t.name, "color": t.color} for t in tags],
    )


@router.get("/conversations/{conversation_id}/messages", response_model=MessageListOut)
async def list_messages(
    conversation_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> MessageListOut:
    await _get_conversation(session, member.workspace_id, conversation_id)
    query = select(Message).where(
        Message.workspace_id == member.workspace_id,
        Message.conversation_id == conversation_id,
    )
    if cursor:
        ts, mid = _decode_cursor(cursor)
        query = query.where(
            or_(Message.created_at < ts, and_(Message.created_at == ts, Message.id < mid))
        )
    rows = (
        await session.execute(
            query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return MessageListOut(items=[MessageOut.model_validate(m) for m in rows], next_cursor=next_cursor)


@router.post("/conversations/{conversation_id}/messages", response_model=MessageOut, status_code=201)
async def send_reply(
    conversation_id: uuid.UUID,
    body: SendIn,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """回覆 / 內部備註. Notes bypass the channel entirely. WINDOW_EXPIRED and
    other pipeline rejections come back as 422 {code, error}."""
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    try:
        result = await messaging.send_message(
            session,
            conversation=conv,
            sender_type="member",
            sender_id=member.member_id,
            content=body.content,
            quick_reply_id=body.quick_reply_id,
            is_note=body.is_note,
            client_msg_id=body.client_msg_id,
            redis=get_redis(),
        )
    except SendError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    await publish_realtime(result.events)
    return MessageOut.model_validate(result.message)


@router.post("/conversations/{conversation_id}/read")
async def advance_read(
    conversation_id: uuid.UUID,
    body: ReadIn,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    events = await messaging.advance_read_cursor(
        session,
        conversation=conv,
        member_id=member.member_id,
        last_read_message_id=body.last_read_message_id,
        redis=get_redis(),
    )
    await session.commit()
    await publish_realtime(events)
    return {"ok": True, "agent_unread_count": conv.agent_unread_count}


# ==========================================================================
# assignment lifecycle
# ==========================================================================
@router.post("/conversations/{conversation_id}/assign", response_model=ConversationOut)
async def assign_conversation(
    conversation_id: uuid.UUID,
    body: AssignIn,
    member: MemberContext = Depends(require_permission("inbox.assign")),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """轉接 / 分配 (member_id=null → 轉未分配)."""
    try:
        result = await routing.transfer(
            session,
            get_redis(),
            workspace_id=member.workspace_id,
            conversation_id=conversation_id,
            to_member_id=body.member_id,
            actor=Actor(type="member", id=member.member_id),
            reason="transfer",
        )
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from e
    await session.commit()
    await publish_realtime(result.events)
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    return ConversationOut.model_validate(conv)


@router.post("/conversations/{conversation_id}/claim", response_model=ConversationOut)
async def claim_conversation(
    conversation_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """Agent pulls the conversation from the 待分配 pool to themselves."""
    try:
        result = await routing.claim(
            session,
            get_redis(),
            workspace_id=member.workspace_id,
            conversation_id=conversation_id,
            member_id=member.member_id,
        )
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from e
    await session.commit()
    await publish_realtime(result.events)
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    return ConversationOut.model_validate(conv)


@router.post("/conversations/{conversation_id}/close", response_model=ConversationOut)
async def close_conversation(
    conversation_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("inbox.close")),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    try:
        result = await routing.close_conversation(
            session,
            get_redis(),
            workspace_id=member.workspace_id,
            conversation_id=conversation_id,
            actor=Actor(type="member", id=member.member_id),
        )
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from e
    await session.commit()
    if result is not None:
        await publish_realtime(result.events)
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    return ConversationOut.model_validate(conv)


@router.post("/conversations/{conversation_id}/reopen", response_model=ConversationOut)
async def reopen_conversation(
    conversation_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    try:
        result = await routing.reopen_conversation(
            session,
            get_redis(),
            workspace_id=member.workspace_id,
            conversation_id=conversation_id,
            actor=Actor(type="member", id=member.member_id),
            opened_by="member",
        )
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from e
    await session.commit()
    if result is not None:
        await publish_realtime(result.events)
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    return ConversationOut.model_validate(conv)


# ==========================================================================
# conversation tags / translation / 託管
# ==========================================================================
@router.put("/conversations/{conversation_id}/tags")
async def set_conversation_tags(
    conversation_id: uuid.UUID,
    body: TagsIn,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _get_conversation(session, member.workspace_id, conversation_id)
    tags = (
        await session.execute(
            select(Tag).where(
                Tag.workspace_id == member.workspace_id,
                Tag.kind == "conversation",
                Tag.id.in_(body.tag_ids) if body.tag_ids else False,
            )
        )
    ).scalars().all()
    if len(tags) != len(set(body.tag_ids)):
        raise HTTPException(422, detail="unknown or non-conversation tag ids")
    await session.execute(
        delete(ConversationTag).where(ConversationTag.conversation_id == conversation_id)
    )
    for t in tags:
        session.add(
            ConversationTag(
                workspace_id=member.workspace_id, conversation_id=conversation_id, tag_id=t.id
            )
        )
    await session.commit()
    return [{"id": str(t.id), "name": t.name, "color": t.color} for t in tags]


@router.patch("/conversations/{conversation_id}/translation", response_model=ConversationOut)
async def set_translation(
    conversation_id: uuid.UUID,
    body: TranslationIn,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """翻譯開關 — conversation-level {enabled, agent_lang, customer_lang}.
    The translation worker (P2) reads this state; toggling is P1."""
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    conv.translation = {**(conv.translation or {}), **patch}
    from ...services import event_bus

    ev = messaging._conversation_event(conv, Actor(type="member", id=member.member_id))
    await event_bus.emit(session, ev)
    await session.commit()
    await publish_realtime([ev])
    return ConversationOut.model_validate(conv)


@router.patch("/conversations/{conversation_id}/managed", response_model=ConversationOut)
async def set_managed(
    conversation_id: uuid.UUID,
    body: ManagedIn,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> ConversationOut:
    """託管 toggle: hand the conversation to (or take it back from) the
    bot/AI member without changing the assignee."""
    try:
        result = await routing.set_managed(
            session,
            workspace_id=member.workspace_id,
            conversation_id=conversation_id,
            managed=body.managed,
            actor=Actor(type="member", id=member.member_id),
        )
    except LookupError as e:
        raise HTTPException(404, detail=str(e)) from e
    await session.commit()
    await publish_realtime(result.events)
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    return ConversationOut.model_validate(conv)


@router.get("/conversations/{conversation_id}/sessions", response_model=list[SessionOut])
async def list_sessions(
    conversation_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    """歷史會話 — service cycles of this conversation, newest first."""
    await _get_conversation(session, member.workspace_id, conversation_id)
    rows = (
        await session.execute(
            select(ConversationSession)
            .where(ConversationSession.conversation_id == conversation_id)
            .order_by(ConversationSession.started_at.desc())
        )
    ).scalars().all()
    return [SessionOut.model_validate(s) for s in rows]


# ==========================================================================
# unread bootstrap (list badge totals; live updates come over WS)
# ==========================================================================
@router.get("/unread-summary")
async def unread_summary(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    ws = member.workspace_id
    mine = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.workspace_id == ws,
                Conversation.assignee_member_id == member.member_id,
                Conversation.status == "open",
                Conversation.agent_unread_count > 0,
            )
        )
    ).scalar_one()
    unassigned = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.workspace_id == ws,
                Conversation.handler == "unassigned",
                Conversation.status == "open",
            )
        )
    ).scalar_one()
    needs_reply = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.workspace_id == ws,
                Conversation.assignee_member_id == member.member_id,
                Conversation.status == "open",
                Conversation.needs_reply.is_(True),
            )
        )
    ).scalar_one()
    return {
        "mine_unread": int(mine),
        "unassigned_open": int(unassigned),
        "mine_needs_reply": int(needs_reply),
    }
