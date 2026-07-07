"""Live smoke (pg + redis): KB ingest→retrieve and handle_ai_inbound
reply / handoff / pause with a FakeLLM (deterministic completions + embeddings
— no network). Each scenario runs in its own uncommitted transaction that is
rolled back, so the DB is left clean; seeded Redis keys are deleted at the end.

Run as ONE test so the shared async engine + redis client live on a single
event loop (asyncpg pools are loop-bound). Auto-skips if pg/redis are down.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

import apps.api.app.db as dbmod
from apps.api.app.ai import agent_runtime, rag
from apps.api.app.models.ai import AIAgent, AIAgentUsage, KBChunk, KBCollection, KBDocument
from apps.api.app.models.channels import ChannelAccount
from apps.api.app.models.contacts import ChannelIdentity, Contact
from apps.api.app.models.conversations import Conversation
from apps.api.app.models.members import WorkspaceMember
from apps.api.app.models.messaging import Message
from apps.api.app.models.tenancy import Workspace
from apps.api.app.services import points
from apps.api.app.services.llm_client import get_default_llm, reset_default_llm, set_default_llm
from apps.api.app.services.redis_client import close_redis, get_redis

from .fakes import FakeLLM


async def _db_available() -> bool:
    try:
        async with dbmod.session_factory()() as s:
            await s.execute(sa.text("SELECT 1"))
        await get_redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


async def _seed(session, redis, *, skills=("product_card", "lead_capture")):
    """Seed a full AI-managed widget conversation + a product KB (uncommitted)."""
    ws = Workspace(name="ai-test", plan_code="free", status="active", settings={})
    session.add(ws)
    await session.flush()

    member = WorkspaceMember(
        workspace_id=ws.id, member_type="ai_agent", display_name="Aida",
        status="active", max_concurrent=0, ai_config={"receive_enabled": True},
    )
    session.add(member)
    await session.flush()

    collection = KBCollection(workspace_id=ws.id, name="Catalog")
    session.add(collection)
    await session.flush()

    agent = AIAgent(
        workspace_id=ws.id, member_id=member.id, name="Aida",
        persona={"role": "You are Acme support."}, model_tier="smart",
        kb_collection_ids=[str(collection.id)], skills=list(skills),
        monthly_msg_quota=0, mode="builtin", external={}, escalation_rules={}, enabled=True,
    )
    session.add(agent)

    doc = KBDocument(
        workspace_id=ws.id, collection_id=collection.id, source_type="product",
        title="Products", status="pending",
        meta={"items": [
            {"handle": "sku-1", "title": "Blue Widget", "price": "19.99", "currency": "USD",
             "url": "https://shop/sku-1", "description": "A sturdy blue widget."},
        ]},
    )
    session.add(doc)
    await session.flush()
    await rag.ingest_document(session, document_id=doc.id, client=get_default_llm())

    contact = Contact(workspace_id=ws.id, display_name="Cara")
    session.add(contact)
    await session.flush()

    account = ChannelAccount(workspace_id=ws.id, channel_type="widget",
                             external_id=f"w-{uuid.uuid4().hex[:8]}")
    session.add(account)
    await session.flush()

    identity = ChannelIdentity(
        workspace_id=ws.id, channel_account_id=account.id, channel_type="widget",
        external_user_id=f"v-{uuid.uuid4().hex[:8]}", contact_id=contact.id, meta={},
    )
    session.add(identity)
    await session.flush()

    conv = Conversation(
        workspace_id=ws.id, channel_identity_id=identity.id, channel_account_id=account.id,
        channel_type="widget", contact_id=contact.id, status="open", handler="ai_agent",
        assignee_member_id=member.id, ai_state="managed", bot_managed=True,
    )
    session.add(conv)
    await session.flush()

    msg = Message(
        workspace_id=ws.id, conversation_id=conv.id, channel_identity_id=identity.id,
        direction="in", sender_type="contact", msg_type="text",
        content={"blocks": [{"kind": "text", "text": "Do you sell a blue widget and how much?"}]},
        text_plain="Do you sell a blue widget and how much?", is_note=False,
        delivery_status="delivered", created_at=datetime.now(UTC),
    )
    session.add(msg)
    await session.flush()

    await redis.set(points.balance_key(ws.id), 100000)
    return SimpleNamespace(ws=ws, member=member, agent=agent, collection=collection,
                           doc=doc, contact=contact, conv=conv, msg=msg)


async def test_ai_live_smoke():
    if not await _db_available():
        pytest.skip("pg/redis not available")
    redis = get_redis()
    seen_keys: list[str] = []
    try:
        # ---- 1) KB ingest → hybrid retrieve → catalog grounding ----
        set_default_llm(FakeLLM())
        async with dbmod.session_factory()() as session:
            data = await _seed(session, redis)
            seen_keys.append(points.balance_key(data.ws.id))
            n = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(KBChunk)
                    .where(KBChunk.document_id == data.doc.id)
                )
            ).scalar_one()
            assert n >= 1 and data.doc.status == "ready"
            retrieved = await rag.retrieve(
                session, workspace_id=data.ws.id, collection_ids=[data.collection.id],
                query="blue widget price", client=get_default_llm(),
            )
            assert retrieved.hit is True
            assert any("Blue Widget" in c.text for c in retrieved.chunks)
            catalog = await rag.product_catalog(
                session, workspace_id=data.ws.id, collection_ids=[data.collection.id])
            assert "sku-1" in catalog
            await session.rollback()

        # ---- 2) reply with a grounded product card ----
        fake = FakeLLM(reply="Yes! Our blue widget is $19.99. [CARD:sku-1] [CARD:ghost-999]")
        set_default_llm(fake)
        async with dbmod.session_factory()() as session:
            data = await _seed(session, redis)
            seen_keys += [points.balance_key(data.ws.id), f"ai:done:{data.msg.id}",
                          f"ai:lock:{data.conv.id}", f"ai:miss:{data.conv.id}"]
            outcome = await agent_runtime.handle_ai_inbound(session, redis, data.conv, data.msg, client=fake)
            assert outcome is not None and outcome.action == "replied"
            assert outcome.card_handles == ["sku-1"]  # hallucinated ghost-999 dropped
            assert any(e.type == "ai.reply" for e in outcome.events)
            reply = (
                await session.execute(
                    sa.select(Message).where(
                        Message.conversation_id == data.conv.id, Message.sender_type == "ai_agent",
                        Message.direction == "out", Message.is_note.is_(False),
                    )
                )
            ).scalars().first()
            assert reply is not None
            kinds = [b["kind"] for b in reply.content["blocks"]]
            assert "text" in kinds and "product_card" in kinds
            usage = await session.get(AIAgentUsage, (data.agent.id, f"{datetime.now(UTC):%Y-%m}"))
            assert usage is not None and usage.replies == 1

            # idempotent re-entry: same message → no-op, no second LLM run
            calls = fake.complete_calls
            again = await agent_runtime.handle_ai_inbound(session, redis, data.conv, data.msg, client=fake)
            assert again is None and fake.complete_calls == calls
            await session.rollback()

        # ---- 3) handoff on [HANDOFF] marker ----
        fake = FakeLLM(reply="Let me connect you with a specialist. [HANDOFF:pricing]")
        set_default_llm(fake)
        async with dbmod.session_factory()() as session:
            data = await _seed(session, redis)
            seen_keys += [points.balance_key(data.ws.id), f"ai:done:{data.msg.id}",
                          f"ai:lock:{data.conv.id}", f"ai:miss:{data.conv.id}"]
            outcome = await agent_runtime.handle_ai_inbound(session, redis, data.conv, data.msg, client=fake)
            assert outcome is not None and outcome.action == "handoff"
            assert outcome.handoff_reason == "pricing"
            assert any(e.type == "ai.handoff" for e in outcome.events)
            assert data.conv.handler == "unassigned"
            assert data.conv.ai_state == "off"
            assert data.conv.bot_managed is False
            assert data.conv.assignee_member_id is None
            note = (
                await session.execute(
                    sa.select(Message).where(
                        Message.conversation_id == data.conv.id, Message.is_note.is_(True))
                )
            ).scalars().first()
            assert note is not None and "AI handoff" in note.content["blocks"][0]["text"]
            await session.rollback()

        # ---- 4) pause on human interjection ----
        set_default_llm(FakeLLM())
        async with dbmod.session_factory()() as session:
            data = await _seed(session, redis)
            seen_keys.append(points.balance_key(data.ws.id))
            events = await agent_runtime.pause_ai_for_human(session, redis, data.conv)
            assert data.conv.ai_state == "paused_human"
            assert events and events[0].type == "conversation.updated"
            await session.rollback()
    finally:
        reset_default_llm()
        if seen_keys:
            try:
                await redis.delete(*set(seen_keys))
            except Exception:  # noqa: BLE001
                pass
        await close_redis()
        await dbmod.engine().dispose()
        dbmod._engine = None
        dbmod._session_factory = None
