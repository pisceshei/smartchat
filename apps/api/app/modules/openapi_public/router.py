"""Public OpenAPI surface (/api/openapi/v1/*), project-token authenticated
(X-Api-Token). Mirrors SaleSmartly's thin action REST: assign-chat-user,
customers list. Plan-gated ≥Max at the token-issuance side; endpoints here
enforce token validity + workspace scoping.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from py_contracts.events import Actor, Event
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...deps import ApiTokenContext, current_api_token
from ...models.contacts import Contact
from ...models.conversations import Conversation, ConversationAssignment
from ...models.members import WorkspaceMember
from ...services import event_bus

router = APIRouter(prefix="/api/openapi/v1", tags=["openapi"])


class AssignChatUserIn(BaseModel):
    conversation_id: uuid.UUID
    member_id: uuid.UUID


class AssignChatUserOut(BaseModel):
    conversation_id: uuid.UUID
    assignee_member_id: uuid.UUID
    handler: str


class CustomerOut(BaseModel):
    id: uuid.UUID
    display_name: str
    email: str | None
    phone: str | None
    language: str | None
    country: str | None
    city: str | None
    is_blacklisted: bool
    created_at: datetime
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


class CustomerListOut(BaseModel):
    items: list[CustomerOut]
    total: int
    page: int
    page_size: int


@router.post("/assign-chat-user", response_model=AssignChatUserOut)
async def assign_chat_user(
    body: AssignChatUserIn,
    token: ApiTokenContext = Depends(current_api_token),
    session: AsyncSession = Depends(get_session),
) -> AssignChatUserOut:
    conv = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.id == body.conversation_id,
                Conversation.workspace_id == token.workspace_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    target = await session.get(WorkspaceMember, body.member_id)
    if target is None or target.workspace_id != token.workspace_id or target.status != "active":
        raise HTTPException(status_code=404, detail="member not found")
    from_handler, from_member = conv.handler, conv.assignee_member_id
    conv.assignee_member_id = target.id
    conv.handler = "ai_agent" if target.member_type == "ai_agent" else "member"
    session.add(
        ConversationAssignment(
            workspace_id=token.workspace_id,
            conversation_id=conv.id,
            from_handler=from_handler,
            from_member_id=from_member,
            to_handler=conv.handler,
            to_member_id=target.id,
            reason="api",
            actor_type="api",
        )
    )
    await event_bus.emit(
        session,
        Event(
            workspace_id=token.workspace_id,
            type="conversation.assigned",
            actor=Actor(type="api"),
            conversation_id=conv.id,
            contact_id=conv.contact_id,
            channel_type=conv.channel_type,
            channel_account_id=conv.channel_account_id,
            payload={"assignee_member_id": str(target.id), "reason": "api"},
        ),
    )
    await session.commit()
    return AssignChatUserOut(
        conversation_id=conv.id, assignee_member_id=target.id, handler=conv.handler
    )


@router.get("/customers", response_model=CustomerListOut)
async def list_customers(
    token: ApiTokenContext = Depends(current_api_token),
    session: AsyncSession = Depends(get_session),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    email: str | None = Query(default=None),
    phone: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=128, description="name substring"),
) -> CustomerListOut:
    base = select(Contact).where(
        Contact.workspace_id == token.workspace_id,
        Contact.merged_into_id.is_(None),  # ONE ID: hide tombstones
    )
    if email:
        base = base.where(Contact.email == email)
    if phone:
        base = base.where(Contact.phone == phone)
    if q:
        base = base.where(Contact.display_name.ilike(f"%{q}%"))
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(Contact.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return CustomerListOut(
        items=[CustomerOut.model_validate(c) for c in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


class CustomerDetailOut(CustomerOut):
    custom: dict = Field(default_factory=dict)


@router.get("/customers/{customer_id}", response_model=CustomerDetailOut)
async def get_customer(
    customer_id: uuid.UUID,
    token: ApiTokenContext = Depends(current_api_token),
    session: AsyncSession = Depends(get_session),
) -> CustomerDetailOut:
    contact = await session.get(Contact, customer_id)
    if contact is None or contact.workspace_id != token.workspace_id:
        raise HTTPException(status_code=404, detail="customer not found")
    return CustomerDetailOut.model_validate(contact)
