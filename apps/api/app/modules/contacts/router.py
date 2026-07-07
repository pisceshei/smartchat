"""客戶 (CRM) module: contact list + rich filters + custom-field predicates,
CSV export, contact 360, ONE-ID merge/unmerge, merge candidates (重複聯絡人),
tags (訪客/會話標籤管理), notes, blacklist, custom field definitions,
話術庫 (quick replies + folders), and saved audience segments (自訂受眾 stub).

Route order: static subpaths are declared before the /{contact_id} matchers.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from py_contracts.content import MessageContent
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, and_, cast, delete, exists, func, or_, select
from sqlalchemy import Float as SAFloat
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session, session_factory
from ...deps import MemberContext, current_member, require_permission
from ...models.contacts import (
    ChannelIdentity,
    Contact,
    ContactMerge,
    ContactMergeCandidate,
    ContactNote,
    ContactOrder,
)
from ...models.conversations import Conversation
from ...models.misc import (
    AuditLog,
    ContactTag,
    CustomFieldDefinition,
    QuickReply,
    QuickReplyFolder,
    SavedView,
    Tag,
)
from ...services.messaging import publish_realtime
from . import service as svc

router = APIRouter(prefix="/api/v1/contacts", tags=["contacts"])

MAX_PAGE = 200
EXPORT_CHUNK = 500


# ==========================================================================
# schemas
# ==========================================================================
class ContactOut(BaseModel):
    id: uuid.UUID
    display_name: str
    remark_name: str | None
    avatar_url: str | None
    email: str | None
    phone: str | None
    language: str | None
    country: str | None
    city: str | None
    timezone: str | None
    custom: dict[str, Any]
    is_blacklisted: bool
    merged_into_id: uuid.UUID | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class IdentityOut(BaseModel):
    id: uuid.UUID
    channel_account_id: uuid.UUID
    channel_type: str
    external_user_id: str
    display_name: str | None
    avatar_url: str | None
    logged_in_external_id: str | None
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


class ContactCreateIn(BaseModel):
    display_name: str = Field(default="", max_length=128)
    remark_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    language: str | None = None
    country: str | None = None
    city: str | None = None
    timezone: str | None = None
    custom: dict[str, Any] = Field(default_factory=dict)


class ContactUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    remark_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    language: str | None = None
    country: str | None = None
    city: str | None = None
    timezone: str | None = None
    custom: dict[str, Any] | None = None


PredicateOp = Literal[
    "eq", "neq", "contains", "not_contains", "exists", "not_exists", "in", "gt", "lt"
]


class Predicate(BaseModel):
    field: str  # name/email/phone/country/city/language/blacklisted/tag_id/custom.<key>/created_after…
    op: PredicateOp = "eq"
    value: Any = None


class ContactQueryIn(BaseModel):
    q: str | None = None
    predicates: list[Predicate] = Field(default_factory=list)
    logic: Literal["and", "or"] = "and"
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)
    offset: int = Field(default=0, ge=0)
    include_merged: bool = False


class ContactListOut(BaseModel):
    items: list[ContactOut]
    total: int
    limit: int
    offset: int


class MergeIn(BaseModel):
    source_contact_id: uuid.UUID


class MergeOut(BaseModel):
    merge_id: uuid.UUID
    target_contact_id: uuid.UUID
    source_contact_id: uuid.UUID
    undone_at: datetime | None = None


class CandidateOut(BaseModel):
    id: uuid.UUID
    match_type: str
    status: str
    other: ContactOut
    created_at: datetime


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class NoteOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    author_member_id: uuid.UUID | None
    body: str
    created_at: datetime

    model_config = {"from_attributes": True}


class BlacklistIn(BaseModel):
    blacklisted: bool


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["contact", "conversation"] = "contact"
    color: str | None = Field(default=None, max_length=16)


class TagOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    color: str | None

    model_config = {"from_attributes": True}


class SetTagsIn(BaseModel):
    tag_ids: list[uuid.UUID]


class CustomFieldIn(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    label: str = Field(min_length=1, max_length=128)
    field_type: Literal["text", "number", "date", "select", "multiselect", "bool"] = "text"
    options: list[Any] = Field(default_factory=list)
    required: bool = False
    sort_order: int = 0
    entity: Literal["contact", "conversation"] = "contact"


class CustomFieldOut(CustomFieldIn):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scope: Literal["personal", "public"] = "public"
    sort_order: int = 0


class FolderOut(FolderIn):
    id: uuid.UUID
    owner_member_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class QuickReplyIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    content: dict[str, Any]  # MessageContent blocks
    folder_id: uuid.UUID | None = None
    scope: Literal["personal", "public"] = "public"
    shortcut: str | None = Field(default=None, max_length=32)
    starred: bool = False


class QuickReplyOut(BaseModel):
    id: uuid.UUID
    folder_id: uuid.UUID | None
    scope: str
    owner_member_id: uuid.UUID | None
    title: str
    shortcut: str | None
    content: dict[str, Any]
    text_plain: str | None
    starred: bool
    usage_count: int

    model_config = {"from_attributes": True}


class SegmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    predicates: list[Predicate] = Field(default_factory=list)
    logic: Literal["and", "or"] = "and"


class SegmentOut(BaseModel):
    id: uuid.UUID
    name: str
    filters: dict[str, Any]


class ExportIn(BaseModel):
    q: str | None = None
    predicates: list[Predicate] = Field(default_factory=list)
    logic: Literal["and", "or"] = "and"


# ==========================================================================
# predicate compiler (whitelisted fields — never raw SQL)
# ==========================================================================
_SCALAR_COLS = {
    "display_name": Contact.display_name,
    "remark_name": Contact.remark_name,
    "email": Contact.email,
    "phone": Contact.phone,
    "language": Contact.language,
    "country": Contact.country,
    "city": Contact.city,
}


def _compile_predicate(workspace_id: uuid.UUID, p: Predicate) -> ColumnElement[bool]:
    if p.field == "name":
        needle = f"%{p.value}%"
        cond = or_(Contact.display_name.ilike(needle), Contact.remark_name.ilike(needle))
        return ~cond if p.op in ("neq", "not_contains") else cond
    if p.field in _SCALAR_COLS:
        col = _SCALAR_COLS[p.field]
        match p.op:
            case "eq":
                return col == p.value
            case "neq":
                return col != p.value
            case "contains":
                return col.ilike(f"%{p.value}%")
            case "not_contains":
                return ~col.ilike(f"%{p.value}%")
            case "exists":
                return and_(col.is_not(None), col != "")
            case "not_exists":
                return or_(col.is_(None), col == "")
            case "in":
                return col.in_(list(p.value or []))
            case _:
                raise HTTPException(422, detail=f"op {p.op} not valid for {p.field}")
    if p.field == "blacklisted":
        return Contact.is_blacklisted.is_(bool(p.value))
    if p.field == "tag_id":
        try:
            tag_id = uuid.UUID(str(p.value))
        except ValueError as e:
            raise HTTPException(422, detail="tag_id must be a uuid") from e
        sub = exists().where(
            ContactTag.contact_id == Contact.id, ContactTag.tag_id == tag_id
        )
        return ~sub if p.op == "neq" else sub
    if p.field == "channel_type":
        sub = exists().where(
            ChannelIdentity.contact_id == Contact.id,
            ChannelIdentity.channel_type == str(p.value),
        )
        return ~sub if p.op == "neq" else sub
    if p.field == "assignee_member_id":
        try:
            mid = uuid.UUID(str(p.value))
        except ValueError as e:
            raise HTTPException(422, detail="assignee_member_id must be a uuid") from e
        sub = exists().where(
            Conversation.contact_id == Contact.id,
            Conversation.workspace_id == workspace_id,
            Conversation.assignee_member_id == mid,
        )
        return ~sub if p.op == "neq" else sub
    if p.field in ("created_after", "created_before"):
        dt = datetime.fromisoformat(str(p.value))
        return Contact.created_at >= dt if p.field == "created_after" else Contact.created_at <= dt
    if p.field.startswith("custom."):
        key = p.field.split(".", 1)[1]
        el = Contact.custom[key].astext
        match p.op:
            case "eq":
                return el == str(p.value)
            case "neq":
                return el != str(p.value)
            case "contains":
                return el.ilike(f"%{p.value}%")
            case "not_contains":
                return ~el.ilike(f"%{p.value}%")
            case "exists":
                return Contact.custom.has_key(key)  # noqa: W601 — JSONB ? operator
            case "not_exists":
                return ~Contact.custom.has_key(key)  # noqa: W601
            case "in":
                return el.in_([str(v) for v in (p.value or [])])
            case "gt":
                return cast(el, SAFloat) > float(p.value)
            case "lt":
                return cast(el, SAFloat) < float(p.value)
    raise HTTPException(422, detail=f"unknown filter field: {p.field}")


def _base_query(workspace_id: uuid.UUID, *, include_merged: bool = False):
    q = select(Contact).where(Contact.workspace_id == workspace_id)
    if not include_merged:
        q = q.where(Contact.merged_into_id.is_(None))
    return q


def _apply_filters(
    q,
    workspace_id: uuid.UUID,
    *,
    search: str | None,
    predicates: list[Predicate],
    logic: str,
):
    if search:
        needle = f"%{search}%"
        q = q.where(
            or_(
                Contact.display_name.ilike(needle),
                Contact.remark_name.ilike(needle),
                Contact.email.ilike(needle),
                Contact.phone.ilike(needle),
            )
        )
    if predicates:
        conds = [_compile_predicate(workspace_id, p) for p in predicates]
        q = q.where(or_(*conds) if logic == "or" else and_(*conds))
    return q


# ==========================================================================
# tags 標籤管理 (contact + conversation kinds)
# ==========================================================================
@router.get("/tags", response_model=list[TagOut])
async def list_tags(
    kind: str | None = Query(default=None, pattern="^(contact|conversation)$"),
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> list[TagOut]:
    q = select(Tag).where(Tag.workspace_id == member.workspace_id).order_by(Tag.created_at)
    if kind:
        q = q.where(Tag.kind == kind)
    rows = (await session.execute(q)).scalars().all()
    return [TagOut.model_validate(t) for t in rows]


@router.post("/tags", response_model=TagOut, status_code=201)
async def create_tag(
    body: TagIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> TagOut:
    dup = (
        await session.execute(
            select(Tag.id).where(
                Tag.workspace_id == member.workspace_id, Tag.kind == body.kind, Tag.name == body.name
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, detail="tag already exists")
    tag = Tag(workspace_id=member.workspace_id, kind=body.kind, name=body.name, color=body.color)
    session.add(tag)
    await session.commit()
    return TagOut.model_validate(tag)


@router.patch("/tags/{tag_id}", response_model=TagOut)
async def update_tag(
    tag_id: uuid.UUID,
    body: TagIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> TagOut:
    tag = await session.get(Tag, tag_id)
    if tag is None or tag.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="tag not found")
    tag.name = body.name
    tag.color = body.color
    await session.commit()
    return TagOut.model_validate(tag)


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> None:
    tag = await session.get(Tag, tag_id)
    if tag is None or tag.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="tag not found")
    await session.delete(tag)
    await session.commit()


# ==========================================================================
# custom field definitions
# ==========================================================================
@router.get("/custom-fields", response_model=list[CustomFieldOut])
async def list_custom_fields(
    entity: str = Query(default="contact"),
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> list[CustomFieldOut]:
    rows = (
        await session.execute(
            select(CustomFieldDefinition)
            .where(
                CustomFieldDefinition.workspace_id == member.workspace_id,
                CustomFieldDefinition.entity == entity,
            )
            .order_by(CustomFieldDefinition.sort_order, CustomFieldDefinition.created_at)
        )
    ).scalars().all()
    return [CustomFieldOut.model_validate(r) for r in rows]


@router.post("/custom-fields", response_model=CustomFieldOut, status_code=201)
async def create_custom_field(
    body: CustomFieldIn,
    member: MemberContext = Depends(require_permission("settings.manage")),
    session: AsyncSession = Depends(get_session),
) -> CustomFieldOut:
    dup = (
        await session.execute(
            select(CustomFieldDefinition.id).where(
                CustomFieldDefinition.workspace_id == member.workspace_id,
                CustomFieldDefinition.entity == body.entity,
                CustomFieldDefinition.key == body.key,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, detail="field key already exists")
    row = CustomFieldDefinition(workspace_id=member.workspace_id, **body.model_dump())
    session.add(row)
    await session.commit()
    return CustomFieldOut.model_validate(row)


@router.patch("/custom-fields/{field_id}", response_model=CustomFieldOut)
async def update_custom_field(
    field_id: uuid.UUID,
    body: CustomFieldIn,
    member: MemberContext = Depends(require_permission("settings.manage")),
    session: AsyncSession = Depends(get_session),
) -> CustomFieldOut:
    row = await session.get(CustomFieldDefinition, field_id)
    if row is None or row.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="field not found")
    for k, v in body.model_dump().items():
        if k != "key":  # the key is the storage contract — immutable
            setattr(row, k, v)
    await session.commit()
    return CustomFieldOut.model_validate(row)


@router.delete("/custom-fields/{field_id}", status_code=204)
async def delete_custom_field(
    field_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("settings.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(CustomFieldDefinition, field_id)
    if row is None or row.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="field not found")
    await session.delete(row)
    await session.commit()


# ==========================================================================
# 話術庫 quick replies + folders
# ==========================================================================
@router.get("/quick-reply-folders", response_model=list[FolderOut])
async def list_folders(
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[FolderOut]:
    rows = (
        await session.execute(
            select(QuickReplyFolder)
            .where(
                QuickReplyFolder.workspace_id == member.workspace_id,
                or_(
                    QuickReplyFolder.scope == "public",
                    QuickReplyFolder.owner_member_id == member.member_id,
                ),
            )
            .order_by(QuickReplyFolder.sort_order, QuickReplyFolder.created_at)
        )
    ).scalars().all()
    return [FolderOut.model_validate(r) for r in rows]


@router.post("/quick-reply-folders", response_model=FolderOut, status_code=201)
async def create_folder(
    body: FolderIn,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> FolderOut:
    row = QuickReplyFolder(
        workspace_id=member.workspace_id,
        name=body.name,
        scope=body.scope,
        owner_member_id=member.member_id if body.scope == "personal" else None,
        sort_order=body.sort_order,
    )
    session.add(row)
    await session.commit()
    return FolderOut.model_validate(row)


@router.patch("/quick-reply-folders/{folder_id}", response_model=FolderOut)
async def update_folder(
    folder_id: uuid.UUID,
    body: FolderIn,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> FolderOut:
    row = await session.get(QuickReplyFolder, folder_id)
    if row is None or row.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="folder not found")
    if row.scope == "personal" and row.owner_member_id != member.member_id:
        raise HTTPException(403, detail="not your folder")
    row.name = body.name
    row.sort_order = body.sort_order
    await session.commit()
    return FolderOut.model_validate(row)


@router.delete("/quick-reply-folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(QuickReplyFolder, folder_id)
    if row is None or row.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="folder not found")
    if row.scope == "personal" and row.owner_member_id != member.member_id:
        raise HTTPException(403, detail="not your folder")
    await session.delete(row)
    await session.commit()


@router.get("/quick-replies", response_model=list[QuickReplyOut])
async def list_quick_replies(
    folder_id: uuid.UUID | None = None,
    q: str | None = None,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> list[QuickReplyOut]:
    query = (
        select(QuickReply)
        .where(
            QuickReply.workspace_id == member.workspace_id,
            or_(QuickReply.scope == "public", QuickReply.owner_member_id == member.member_id),
        )
        .order_by(QuickReply.starred.desc(), QuickReply.usage_count.desc(), QuickReply.created_at)
    )
    if folder_id:
        query = query.where(QuickReply.folder_id == folder_id)
    if q:
        needle = f"%{q}%"
        query = query.where(
            or_(QuickReply.title.ilike(needle), QuickReply.text_plain.ilike(needle),
                QuickReply.shortcut.ilike(needle))
        )
    rows = (await session.execute(query)).scalars().all()
    return [QuickReplyOut.model_validate(r) for r in rows]


def _validated_qr_content(raw: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    try:
        content = MessageContent.model_validate(raw)
    except Exception as e:
        raise HTTPException(422, detail={"code": "INVALID_CONTENT", "error": str(e)}) from e
    if not content.blocks:
        raise HTTPException(422, detail={"code": "INVALID_CONTENT", "error": "no blocks"})
    return content.model_dump(mode="json"), (content.plain_text() or None)


@router.post("/quick-replies", response_model=QuickReplyOut, status_code=201)
async def create_quick_reply(
    body: QuickReplyIn,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> QuickReplyOut:
    content, text_plain = _validated_qr_content(body.content)
    row = QuickReply(
        workspace_id=member.workspace_id,
        folder_id=body.folder_id,
        scope=body.scope,
        owner_member_id=member.member_id if body.scope == "personal" else None,
        title=body.title,
        shortcut=body.shortcut,
        content=content,
        text_plain=text_plain,
        starred=body.starred,
    )
    session.add(row)
    await session.commit()
    return QuickReplyOut.model_validate(row)


@router.patch("/quick-replies/{qr_id}", response_model=QuickReplyOut)
async def update_quick_reply(
    qr_id: uuid.UUID,
    body: QuickReplyIn,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> QuickReplyOut:
    row = await session.get(QuickReply, qr_id)
    if row is None or row.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="quick reply not found")
    if row.scope == "personal" and row.owner_member_id != member.member_id:
        raise HTTPException(403, detail="not your quick reply")
    content, text_plain = _validated_qr_content(body.content)
    row.title = body.title
    row.shortcut = body.shortcut
    row.folder_id = body.folder_id
    row.content = content
    row.text_plain = text_plain
    row.starred = body.starred
    await session.commit()
    return QuickReplyOut.model_validate(row)


@router.delete("/quick-replies/{qr_id}", status_code=204)
async def delete_quick_reply(
    qr_id: uuid.UUID,
    member: MemberContext = Depends(current_member),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(QuickReply, qr_id)
    if row is None or row.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="quick reply not found")
    if row.scope == "personal" and row.owner_member_id != member.member_id:
        raise HTTPException(403, detail="not your quick reply")
    await session.delete(row)
    await session.commit()


# ==========================================================================
# 自訂受眾 saved segments (stub over SavedView until B.3 segments land)
# ==========================================================================
@router.get("/segments", response_model=list[SegmentOut])
async def list_segments(
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> list[SegmentOut]:
    rows = (
        await session.execute(
            select(SavedView)
            .where(SavedView.workspace_id == member.workspace_id, SavedView.module == "segment")
            .order_by(SavedView.created_at)
        )
    ).scalars().all()
    return [SegmentOut(id=r.id, name=r.name, filters=r.filters) for r in rows]


@router.post("/segments", response_model=SegmentOut, status_code=201)
async def create_segment(
    body: SegmentIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> SegmentOut:
    for p in body.predicates:  # validate now so broadcasts can trust the stored tree
        _compile_predicate(member.workspace_id, p)
    row = SavedView(
        workspace_id=member.workspace_id,
        module="segment",
        name=body.name,
        visibility="public",
        owner_member_id=member.member_id,
        filters={"predicates": [p.model_dump() for p in body.predicates], "logic": body.logic},
    )
    session.add(row)
    await session.commit()
    return SegmentOut(id=row.id, name=row.name, filters=row.filters)


@router.delete("/segments/{segment_id}", status_code=204)
async def delete_segment(
    segment_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(SavedView, segment_id)
    if row is None or row.workspace_id != member.workspace_id or row.module != "segment":
        raise HTTPException(404, detail="segment not found")
    await session.delete(row)
    await session.commit()


# ==========================================================================
# merge undo (static path before /{contact_id})
# ==========================================================================
@router.post("/merges/{merge_id}/undo", response_model=MergeOut)
async def undo_merge(
    merge_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("contacts.merge")),
    session: AsyncSession = Depends(get_session),
) -> MergeOut:
    try:
        merge, events = await svc.unmerge_contacts(
            session,
            workspace_id=member.workspace_id,
            merge_id=merge_id,
            actor_member_id=member.member_id,
        )
    except svc.NotUndoableError as e:
        raise HTTPException(409, detail={"code": e.code, "error": e.detail}) from e
    except svc.MergeError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    await publish_realtime(events)
    return MergeOut(
        merge_id=merge.id,
        target_contact_id=merge.target_contact_id,
        source_contact_id=merge.source_contact_id,
        undone_at=merge.undone_at,
    )


# ==========================================================================
# list / query / export / create
# ==========================================================================
@router.get("", response_model=ContactListOut)
async def list_contacts(
    q: str | None = None,
    tag_id: uuid.UUID | None = None,
    blacklisted: bool | None = None,
    country: str | None = None,
    language: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> ContactListOut:
    preds: list[Predicate] = []
    if tag_id:
        preds.append(Predicate(field="tag_id", op="eq", value=str(tag_id)))
    if blacklisted is not None:
        preds.append(Predicate(field="blacklisted", op="eq", value=blacklisted))
    if country:
        preds.append(Predicate(field="country", op="eq", value=country))
    if language:
        preds.append(Predicate(field="language", op="eq", value=language))
    return await _run_query(
        session, member.workspace_id,
        ContactQueryIn(q=q, predicates=preds, limit=limit, offset=offset),
    )


@router.post("/query", response_model=ContactListOut)
async def query_contacts(
    body: ContactQueryIn,
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> ContactListOut:
    return await _run_query(session, member.workspace_id, body)


async def _run_query(
    session: AsyncSession, workspace_id: uuid.UUID, body: ContactQueryIn
) -> ContactListOut:
    q = _base_query(workspace_id, include_merged=body.include_merged)
    q = _apply_filters(q, workspace_id, search=body.q, predicates=body.predicates, logic=body.logic)
    total = (
        await session.execute(select(func.count()).select_from(q.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            q.order_by(Contact.last_seen_at.desc().nulls_last(), Contact.id.desc())
            .limit(body.limit)
            .offset(body.offset)
        )
    ).scalars().all()
    return ContactListOut(
        items=[ContactOut.model_validate(c) for c in rows],
        total=int(total),
        limit=body.limit,
        offset=body.offset,
    )


@router.post("/export")
async def export_contacts(
    body: ExportIn,
    member: MemberContext = Depends(require_permission("contacts.export")),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """CSV export streamed straight from keyset pagination — constant memory
    regardless of contact count. The audit entry commits before streaming."""
    workspace_id = member.workspace_id
    field_defs = (
        await session.execute(
            select(CustomFieldDefinition.key)
            .where(
                CustomFieldDefinition.workspace_id == workspace_id,
                CustomFieldDefinition.entity == "contact",
            )
            .order_by(CustomFieldDefinition.sort_order, CustomFieldDefinition.created_at)
        )
    ).scalars().all()
    session.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_type="member",
            actor_id=member.member_id,
            action="contacts.export",
            target_type="contact",
            target_id="*",
            detail={"predicates": [p.model_dump() for p in body.predicates], "q": body.q},
        )
    )
    await session.commit()

    header = [
        "id", "display_name", "remark_name", "email", "phone", "language",
        "country", "city", "timezone", "is_blacklisted", "first_seen_at",
        "last_seen_at", "created_at", "tags",
    ] + [f"custom.{k}" for k in field_defs]

    async def stream():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        last_id: uuid.UUID | None = None
        # dedicated session: the request-scoped one is closed once we return
        async with session_factory()() as s:
            while True:
                q = _base_query(workspace_id)
                q = _apply_filters(
                    q, workspace_id, search=body.q, predicates=body.predicates, logic=body.logic
                )
                if last_id is not None:
                    q = q.where(Contact.id > last_id)
                rows = (
                    await s.execute(q.order_by(Contact.id).limit(EXPORT_CHUNK))
                ).scalars().all()
                if not rows:
                    break
                ids = [c.id for c in rows]
                tag_rows = (
                    await s.execute(
                        select(ContactTag.contact_id, Tag.name)
                        .join(Tag, Tag.id == ContactTag.tag_id)
                        .where(ContactTag.contact_id.in_(ids))
                    )
                ).all()
                tags_by_contact: dict[uuid.UUID, list[str]] = {}
                for cid, name in tag_rows:
                    tags_by_contact.setdefault(cid, []).append(name)
                buf = io.StringIO()
                writer = csv.writer(buf)
                for c in rows:
                    writer.writerow(
                        [
                            str(c.id), c.display_name, c.remark_name or "", c.email or "",
                            c.phone or "", c.language or "", c.country or "", c.city or "",
                            c.timezone or "", "1" if c.is_blacklisted else "0",
                            c.first_seen_at.isoformat() if c.first_seen_at else "",
                            c.last_seen_at.isoformat() if c.last_seen_at else "",
                            c.created_at.isoformat() if c.created_at else "",
                            ";".join(tags_by_contact.get(c.id, [])),
                        ]
                        + [str((c.custom or {}).get(k, "")) for k in field_defs]
                    )
                yield buf.getvalue()
                last_id = rows[-1].id

    filename = f"contacts_{datetime.now(UTC):%Y%m%d_%H%M%S}.csv"
    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("", response_model=ContactOut, status_code=201)
async def create_contact(
    body: ContactCreateIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> ContactOut:
    now = datetime.now(UTC)
    contact = Contact(
        workspace_id=member.workspace_id,
        first_seen_at=now,
        last_seen_at=now,
        **body.model_dump(),
    )
    session.add(contact)
    await session.flush()
    await svc.generate_merge_candidates(
        session, workspace_id=member.workspace_id, contact_id=contact.id
    )
    await session.commit()
    return ContactOut.model_validate(contact)


# ==========================================================================
# contact detail 360 / update
# ==========================================================================
async def _get_contact(
    session: AsyncSession, workspace_id: uuid.UUID, contact_id: uuid.UUID
) -> Contact:
    contact = await session.get(Contact, contact_id)
    if contact is None or contact.workspace_id != workspace_id:
        raise HTTPException(404, detail="contact not found")
    return contact


class ConversationSummaryOut(BaseModel):
    id: uuid.UUID
    channel_type: str
    status: str
    handler: str
    assignee_member_id: uuid.UUID | None
    snippet: str | None
    last_message_at: datetime | None

    model_config = {"from_attributes": True}


class OrderOut(BaseModel):
    id: uuid.UUID
    store_platform: str
    external_order_id: str
    status: str | None
    currency: str | None
    total: str | None
    items: list[dict[str, Any]]
    ordered_at: datetime | None

    model_config = {"from_attributes": True}


class Contact360Out(BaseModel):
    contact: ContactOut
    identities: list[IdentityOut]
    tags: list[TagOut]
    notes: list[NoteOut]
    conversations: list[ConversationSummaryOut]
    orders: list[OrderOut]
    merge_history: list[MergeOut]
    suggested_candidates: int


@router.get("/{contact_id}", response_model=Contact360Out)
async def contact_detail(
    contact_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> Contact360Out:
    contact = await _get_contact(session, member.workspace_id, contact_id)
    identities = (
        await session.execute(
            select(ChannelIdentity)
            .where(ChannelIdentity.contact_id == contact_id)
            .order_by(ChannelIdentity.created_at)
        )
    ).scalars().all()
    tags = (
        await session.execute(
            select(Tag)
            .join(ContactTag, ContactTag.tag_id == Tag.id)
            .where(ContactTag.contact_id == contact_id)
            .order_by(Tag.name)
        )
    ).scalars().all()
    notes = (
        await session.execute(
            select(ContactNote)
            .where(ContactNote.contact_id == contact_id)
            .order_by(ContactNote.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    conversations = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.workspace_id == member.workspace_id,
                Conversation.contact_id == contact_id,
            )
            .order_by(Conversation.last_message_at.desc().nulls_last())
            .limit(20)
        )
    ).scalars().all()
    orders = (
        await session.execute(
            select(ContactOrder)
            .where(ContactOrder.contact_id == contact_id)
            .order_by(ContactOrder.ordered_at.desc().nulls_last())
            .limit(20)
        )
    ).scalars().all()
    merges = (
        await session.execute(
            select(ContactMerge)
            .where(
                ContactMerge.workspace_id == member.workspace_id,
                or_(
                    ContactMerge.target_contact_id == contact_id,
                    ContactMerge.source_contact_id == contact_id,
                ),
            )
            .order_by(ContactMerge.id.desc())
            .limit(20)
        )
    ).scalars().all()
    a_or_b = or_(
        ContactMergeCandidate.contact_a_id == contact_id,
        ContactMergeCandidate.contact_b_id == contact_id,
    )
    suggested = (
        await session.execute(
            select(func.count())
            .select_from(ContactMergeCandidate)
            .where(
                ContactMergeCandidate.workspace_id == member.workspace_id,
                a_or_b,
                ContactMergeCandidate.status == "suggested",
            )
        )
    ).scalar_one()
    return Contact360Out(
        contact=ContactOut.model_validate(contact),
        identities=[IdentityOut.model_validate(i) for i in identities],
        tags=[TagOut.model_validate(t) for t in tags],
        notes=[NoteOut.model_validate(n) for n in notes],
        conversations=[ConversationSummaryOut.model_validate(c) for c in conversations],
        orders=[OrderOut.model_validate(o) for o in orders],
        merge_history=[
            MergeOut(
                merge_id=m.id,
                target_contact_id=m.target_contact_id,
                source_contact_id=m.source_contact_id,
                undone_at=m.undone_at,
            )
            for m in merges
        ],
        suggested_candidates=int(suggested),
    )


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdateIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> ContactOut:
    contact = await _get_contact(session, member.workspace_id, contact_id)
    data = body.model_dump(exclude_unset=True)
    custom = data.pop("custom", None)
    for k, v in data.items():
        setattr(contact, k, v)
    if custom is not None:
        contact.custom = {**(contact.custom or {}), **custom}
    await session.flush()
    if "email" in data or "phone" in data:
        await svc.generate_merge_candidates(
            session, workspace_id=member.workspace_id, contact_id=contact_id
        )
    await session.commit()
    return ContactOut.model_validate(contact)


# ==========================================================================
# contact tags / notes / blacklist
# ==========================================================================
@router.put("/{contact_id}/tags", response_model=list[TagOut])
async def set_contact_tags(
    contact_id: uuid.UUID,
    body: SetTagsIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> list[TagOut]:
    await _get_contact(session, member.workspace_id, contact_id)
    tags = (
        await session.execute(
            select(Tag).where(
                Tag.workspace_id == member.workspace_id,
                Tag.kind == "contact",
                Tag.id.in_(body.tag_ids) if body.tag_ids else False,
            )
        )
    ).scalars().all()
    if len(tags) != len(set(body.tag_ids)):
        raise HTTPException(422, detail="unknown or non-contact tag ids")
    await session.execute(delete(ContactTag).where(ContactTag.contact_id == contact_id))
    for t in tags:
        session.add(
            ContactTag(workspace_id=member.workspace_id, contact_id=contact_id, tag_id=t.id)
        )
    await session.commit()
    return [TagOut.model_validate(t) for t in tags]


@router.get("/{contact_id}/notes", response_model=list[NoteOut])
async def list_notes(
    contact_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> list[NoteOut]:
    await _get_contact(session, member.workspace_id, contact_id)
    rows = (
        await session.execute(
            select(ContactNote)
            .where(ContactNote.contact_id == contact_id)
            .order_by(ContactNote.created_at.desc())
        )
    ).scalars().all()
    return [NoteOut.model_validate(n) for n in rows]


@router.post("/{contact_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(
    contact_id: uuid.UUID,
    body: NoteIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> NoteOut:
    await _get_contact(session, member.workspace_id, contact_id)
    note = ContactNote(
        workspace_id=member.workspace_id,
        contact_id=contact_id,
        author_member_id=member.member_id,
        body=body.body,
    )
    session.add(note)
    await session.commit()
    return NoteOut.model_validate(note)


@router.delete("/{contact_id}/notes/{note_id}", status_code=204)
async def delete_note(
    contact_id: uuid.UUID,
    note_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> None:
    note = await session.get(ContactNote, note_id)
    if note is None or note.workspace_id != member.workspace_id or note.contact_id != contact_id:
        raise HTTPException(404, detail="note not found")
    await session.delete(note)
    await session.commit()


@router.post("/{contact_id}/blacklist", response_model=ContactOut)
async def set_blacklist(
    contact_id: uuid.UUID,
    body: BlacklistIn,
    member: MemberContext = Depends(require_permission("contacts.edit")),
    session: AsyncSession = Depends(get_session),
) -> ContactOut:
    contact = await _get_contact(session, member.workspace_id, contact_id)
    contact.is_blacklisted = body.blacklisted
    session.add(
        AuditLog(
            workspace_id=member.workspace_id,
            actor_type="member",
            actor_id=member.member_id,
            action="contacts.blacklist" if body.blacklisted else "contacts.unblacklist",
            target_type="contact",
            target_id=str(contact_id),
        )
    )
    await session.commit()
    return ContactOut.model_validate(contact)


# ==========================================================================
# ONE-ID: merge candidates + merge
# ==========================================================================
@router.get("/{contact_id}/merge-candidates", response_model=list[CandidateOut])
async def list_merge_candidates(
    contact_id: uuid.UUID,
    status: str = Query(default="suggested", pattern="^(suggested|linked|dismissed|all)$"),
    member: MemberContext = Depends(require_permission("contacts.view")),
    session: AsyncSession = Depends(get_session),
) -> list[CandidateOut]:
    """重複聯絡人: status=suggested (未關聯) / linked (已關聯) / all."""
    await _get_contact(session, member.workspace_id, contact_id)
    q = select(ContactMergeCandidate).where(
        ContactMergeCandidate.workspace_id == member.workspace_id,
        or_(
            ContactMergeCandidate.contact_a_id == contact_id,
            ContactMergeCandidate.contact_b_id == contact_id,
        ),
    )
    if status != "all":
        q = q.where(ContactMergeCandidate.status == status)
    rows = (await session.execute(q.order_by(ContactMergeCandidate.created_at.desc()))).scalars().all()
    out: list[CandidateOut] = []
    for cand in rows:
        other_id = cand.contact_b_id if cand.contact_a_id == contact_id else cand.contact_a_id
        other = await session.get(Contact, other_id)
        if other is None:
            continue
        out.append(
            CandidateOut(
                id=cand.id,
                match_type=cand.match_type,
                status=cand.status,
                other=ContactOut.model_validate(other),
                created_at=cand.created_at,
            )
        )
    return out


@router.post("/{contact_id}/merge-candidates/{candidate_id}/dismiss", status_code=204)
async def dismiss_candidate(
    contact_id: uuid.UUID,
    candidate_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("contacts.merge")),
    session: AsyncSession = Depends(get_session),
) -> None:
    cand = await session.get(ContactMergeCandidate, candidate_id)
    if (
        cand is None
        or cand.workspace_id != member.workspace_id
        or contact_id not in (cand.contact_a_id, cand.contact_b_id)
    ):
        raise HTTPException(404, detail="candidate not found")
    cand.status = "dismissed"
    cand.resolved_at = datetime.now(UTC)
    await session.commit()


@router.post("/{contact_id}/merge", response_model=MergeOut)
async def merge_into(
    contact_id: uuid.UUID,
    body: MergeIn,
    member: MemberContext = Depends(require_permission("contacts.merge")),
    session: AsyncSession = Depends(get_session),
) -> MergeOut:
    """Merge source INTO {contact_id} (the survivor). Undo via
    POST /merges/{merge_id}/undo."""
    try:
        merge, events = await svc.merge_contacts(
            session,
            workspace_id=member.workspace_id,
            target_id=contact_id,
            source_id=body.source_contact_id,
            actor_member_id=member.member_id,
        )
    except svc.MergeError as e:
        raise HTTPException(422, detail={"code": e.code, "error": e.detail}) from e
    await session.commit()
    await publish_realtime(events)
    return MergeOut(
        merge_id=merge.id,
        target_contact_id=merge.target_contact_id,
        source_contact_id=merge.source_contact_id,
        undone_at=None,
    )
