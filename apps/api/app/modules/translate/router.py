"""Translation execution + composer AI assist (/api/v1/ai).

Endpoints:
  POST /translate                                   — on-demand translate text
  POST /conversations/{cid}/messages/{mid}/translate — translate an inbound
        message to the agent language, persist message_translations, push an
        inline realtime patch (message.updated)
  POST /conversations/{cid}/detect-language          — lingua detect the last
        customer message (seed customer_lang when enabling translation)
  POST /assist                                       — composer op, SSE-streamed
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...ai import assist, translation
from ...db import get_session, session_factory
from ...deps import MemberContext, require_permission
from ...models.conversations import Conversation
from ...models.messaging import Message
from ...models.tenancy import Workspace
from ...realtime import publisher
from ...realtime.protocol import AUDIENCE_AGENTS
from ...services.redis_client import get_redis

router = APIRouter(prefix="/api/v1/ai", tags=["translate"])


# ==========================================================================
# schemas
# ==========================================================================
class TranslateIn(BaseModel):
    text: str = Field(min_length=1)
    dst_lang: str = Field(min_length=2, max_length=16)
    src_lang: str | None = Field(default=None, max_length=16)


class TranslateOut(BaseModel):
    text: str
    engine: str
    cached: bool
    ok: bool
    detected_src: str | None = None


class MessageTranslateIn(BaseModel):
    agent_lang: str | None = Field(default=None, max_length=16)


class DetectOut(BaseModel):
    language: str | None


class AssistIn(BaseModel):
    op: Literal["rewrite", "expand", "shorten", "tone", "fix_grammar", "translate_draft"]
    text: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


# ==========================================================================
# helpers
# ==========================================================================
async def _get_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        raise HTTPException(404, detail="conversation not found")
    return conv


async def _translation_chain(session: AsyncSession, workspace_id: uuid.UUID) -> list[Any]:
    ws = await session.get(Workspace, workspace_id)
    engines = ((ws.settings or {}).get("translation") or {}).get("engines") if ws else None
    return translation.build_chain(engines)


# ==========================================================================
# on-demand translate
# ==========================================================================
@router.post("/translate", response_model=TranslateOut)
async def translate_text(
    body: TranslateIn,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> TranslateOut:
    chain = await _translation_chain(session, member.workspace_id)
    result = await translation.translate_text(
        session, get_redis(), workspace_id=member.workspace_id, text=body.text,
        dst_lang=body.dst_lang, src_lang=body.src_lang, chain=chain,
    )
    await session.commit()
    return TranslateOut(
        text=result.text, engine=result.engine, cached=result.cached,
        ok=result.ok, detected_src=result.detected_src,
    )


@router.post("/conversations/{conversation_id}/messages/{message_id}/translate", response_model=TranslateOut)
async def translate_message(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    body: MessageTranslateIn,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> TranslateOut:
    """Translate an inbound message into the agent language and persist it;
    pushes an inline message.updated patch so the panel shows it live."""
    conv = await _get_conversation(session, member.workspace_id, conversation_id)
    msg = await session.get(Message, message_id)
    if msg is None or msg.conversation_id != conversation_id:
        raise HTTPException(404, detail="message not found")
    agent_lang = body.agent_lang or (conv.translation or {}).get("agent_lang") or "en"
    if not (msg.text_plain or "").strip():
        raise HTTPException(422, detail="message has no translatable text")

    chain = await _translation_chain(session, member.workspace_id)
    src_hint = (conv.translation or {}).get("customer_lang")
    text, detected = await translation.translate_inbound(
        session, get_redis(), workspace_id=member.workspace_id, message_id=message_id,
        text=msg.text_plain, agent_lang=agent_lang, src_lang=src_hint, chain=chain,
    )
    await session.commit()
    await publisher.publish(
        member.workspace_id, "message.updated",
        {"message_id": str(message_id), "translations": {agent_lang: text}},
        [AUDIENCE_AGENTS], conversation_id=conversation_id,
    )
    return TranslateOut(text=text, engine="stored", cached=False, ok=True, detected_src=detected)


@router.post("/conversations/{conversation_id}/detect-language", response_model=DetectOut)
async def detect_conversation_language(
    conversation_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("inbox.reply")),
    session: AsyncSession = Depends(get_session),
) -> DetectOut:
    """Detect the customer's language from their most recent inbound message
    (used to seed customer_lang when enabling translation)."""
    await _get_conversation(session, member.workspace_id, conversation_id)
    last = (
        await session.execute(
            select(Message.text_plain)
            .where(
                Message.conversation_id == conversation_id,
                Message.direction == "in",
                Message.is_note.is_(False),
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return DetectOut(language=translation.detect_language(last or ""))


# ==========================================================================
# composer assist (SSE)
# ==========================================================================
@router.post("/assist")
async def composer_assist(
    body: AssistIn,
    member: MemberContext = Depends(require_permission("inbox.reply")),
) -> StreamingResponse:
    """Stream a composer op as server-sent events. Charges 2 points; on a
    hard-stop the stream emits a single {type:error, code:insufficient_points}.
    Manages its own DB session so it can commit the points ledger mid-stream."""
    workspace_id = member.workspace_id

    async def event_source() -> Any:
        async with session_factory()() as session:
            produced = False
            try:
                async for chunk in assist.stream_assist(
                    session, get_redis(), workspace_id=workspace_id,
                    op=body.op, text=body.text, params=body.params,
                ):
                    produced = True
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                await session.commit()
            except Exception as e:  # noqa: BLE001
                await session.rollback()
                if not produced:
                    yield f"data: {json.dumps({'type': 'error', 'code': 'internal', 'detail': str(e)})}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
