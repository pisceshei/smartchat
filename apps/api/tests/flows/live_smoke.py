"""Live end-to-end smoke for the flow engine (plan B.1 verification).

Runs against the dockerised pg (5433) + redis (6380). Seeds a workspace + widget
conversation + a published flow (trigger: visitor_message keyword 'hello';
action: send_message), then drives a *synthetic* message.created event through
``runtime.handle_event`` and asserts a live flow session ran and emitted an
outbound automation message.

Run:
    DATABASE_URL=postgresql+asyncpg://smartchat:smartchat@localhost:5433/smartchat \
    REDIS_URL=redis://localhost:6380/0 \
    .venv/Scripts/python -m apps.api.tests.flows.live_smoke
"""
from __future__ import annotations

import asyncio
import secrets
import uuid

from py_contracts.events import Actor, Event
from sqlalchemy import select

from apps.api.app.db import session_factory
from apps.api.app.models.channels import ChannelAccount
from apps.api.app.models.contacts import ChannelIdentity, Contact
from apps.api.app.models.conversations import Conversation
from apps.api.app.models.flows import Flow, FlowSession
from apps.api.app.models.members import User
from apps.api.app.models.messaging import Message
from apps.api.app.models.tenancy import Plan, Workspace
from apps.api.app.modules.flows import service
from apps.api.app.services.redis_client import close_redis, get_redis
from apps.flow_engine import runtime

GRAPH = {
    "schema_version": 1,
    "nodes": [
        {
            "id": "trigger",
            "type": "trigger",
            "data": {
                "triggers": [
                    {
                        "type": "visitor_message",
                        "config": {
                            "match_type": "keyword",
                            "match_mode": "contains",
                            "keyword_groups": [["hello", "hi"]],
                        },
                        "freq_cap": {},
                    }
                ]
            },
        },
        {
            "id": "greet",
            "type": "send_message",
            "data": {"blocks": [{"kind": "text", "text": "Hi {{ contact.display_name }}! 👋"}]},
        },
        {"id": "tag", "type": "add_contact_tag", "data": {"tag_names": ["flow-greeted"]}},
    ],
    "edges": [
        {"id": "e1", "source": "trigger", "target": "greet", "source_port": "out"},
        {"id": "e2", "source": "greet", "target": "tag", "source_port": "out"},
    ],
}


ASK_GRAPH = {
    "schema_version": 1,
    "nodes": [
        {
            "id": "trigger",
            "type": "trigger",
            "data": {
                "triggers": [
                    {
                        "type": "visitor_message",
                        "config": {"match_type": "keyword", "match_mode": "contains",
                                   "keyword_groups": [["start"]]},
                    }
                ]
            },
        },
        {
            "id": "ask_email",
            "type": "ask",
            "data": {
                "prompt": "What's your email?",
                "variable": "email",
                "save_to_contact": "email",
                "validation": "email",
            },
        },
        {
            "id": "thanks",
            "type": "send_message",
            "data": {"blocks": [{"kind": "text", "text": "Thanks, {{ vars.email }}"}]},
        },
    ],
    "edges": [
        {"id": "e1", "source": "trigger", "target": "ask_email", "source_port": "out"},
        {"id": "e2", "source": "ask_email", "target": "thanks", "source_port": "answered"},
    ],
}


async def _seed(session, graph=GRAPH):
    plan = (await session.execute(select(Plan).limit(1))).scalars().first()
    if plan is None:
        plan = Plan(code="free", name="Free", limits={})
        session.add(plan)
        await session.flush()
    user = User(
        email=f"smoke_{secrets.token_hex(4)}@example.com",
        password_hash="x",
        display_name="Smoke",
    )
    session.add(user)
    await session.flush()
    ws = Workspace(
        name="Flow Smoke WS",
        plan_code=plan.code,
        status="active",
        settings={"timezone": "UTC"},
        owner_user_id=user.id,
    )
    session.add(ws)
    await session.flush()
    acct = ChannelAccount(
        workspace_id=ws.id,
        channel_type="widget",
        name="Smoke Widget",
        external_id=f"smoke_{secrets.token_hex(6)}",
        status="active",
        enabled=True,
    )
    session.add(acct)
    await session.flush()
    contact = Contact(workspace_id=ws.id, display_name="Alice", language="en")
    session.add(contact)
    await session.flush()
    identity = ChannelIdentity(
        workspace_id=ws.id,
        channel_account_id=acct.id,
        channel_type="widget",
        external_user_id=f"v_{secrets.token_hex(6)}",
        contact_id=contact.id,
        display_name="Alice",
    )
    session.add(identity)
    await session.flush()
    conv = Conversation(
        workspace_id=ws.id,
        channel_identity_id=identity.id,
        channel_account_id=acct.id,
        channel_type="widget",
        contact_id=contact.id,
        status="open",
        handler="unassigned",
        session_count=1,
    )
    session.add(conv)
    await session.flush()
    flow = Flow(
        workspace_id=ws.id,
        channel_type="widget",
        name="Smoke Greet Flow",
        enabled=True,
        priority=10,
        draft_graph=graph,
    )
    session.add(flow)
    await session.flush()
    result = await service.publish_flow(
        session, workspace_id=ws.id, flow=flow, member_id=None
    )
    return ws, conv, contact, flow, result


def _inbound(ws_id, conv_id, contact_id, text) -> Event:
    return Event(
        workspace_id=ws_id, type="message.created",
        actor=Actor(type="contact", id=contact_id),
        conversation_id=conv_id, contact_id=contact_id, channel_type="widget",
        payload={"message_id": str(uuid.uuid4()), "direction": "in", "msg_type": "text",
                 "text_plain": text},
    )


async def main() -> int:
    redis = get_redis()
    sf = session_factory()
    async with sf() as session:
        async with session.begin():
            ws, conv, contact, flow, pub = await _seed(session)
        ws_id, conv_id, contact_id, flow_id = ws.id, conv.id, contact.id, flow.id
    print(f"seeded ws={ws_id} conv={conv_id} flow={flow_id} triggers={pub.trigger_count}")

    # synthetic inbound message.created (shape matches ingress_pipeline)
    event = Event(
        workspace_id=ws_id,
        type="message.created",
        actor=Actor(type="contact", id=contact_id),
        conversation_id=conv_id,
        contact_id=contact_id,
        channel_type="widget",
        payload={
            "message_id": str(uuid.uuid4()),
            "direction": "in",
            "msg_type": "text",
            "text_plain": "hello there, I need help",
            "channel_identity_id": None,
        },
    )
    events = await runtime.handle_event(sf, redis, event)
    print(f"handle_event returned {len(events)} realtime events")

    ok = True
    async with sf() as session:
        fs = (
            await session.execute(
                select(FlowSession).where(FlowSession.conversation_id == conv_id)
            )
        ).scalars().first()
        if fs is None:
            print("FAIL: no flow session created")
            ok = False
        else:
            print(f"session status={fs.status} steps={fs.step_count} end_reason={fs.end_reason}")
            if fs.status not in ("completed", "ended", "running"):
                print(f"FAIL: unexpected session status {fs.status}")
                ok = False

        out_msgs = (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == conv_id,
                    Message.direction == "out",
                    Message.sender_type == "automation",
                )
            )
        ).scalars().all()
        if not out_msgs:
            print("FAIL: no automation message sent")
            ok = False
        else:
            m = out_msgs[0]
            print(f"automation message sent: msg_type={m.msg_type} text={m.text_plain!r} "
                  f"source_flow_id={m.source_flow_id}")
            if m.source_flow_id != flow_id:
                print("FAIL: source_flow_id not stamped")
                ok = False
            if "Alice" not in (m.text_plain or ""):
                print("FAIL: template variable not rendered")
                ok = False

        # the second action ran too (tag applied) → session completed the graph
        steps = (
            await session.execute(
                select(FlowSession.step_count).where(FlowSession.id == fs.id)
            )
        ).scalar_one()
        print(f"total steps executed: {steps}")

    # ------------------------------------------------------------------
    # scenario 2: ask → wait → feed reply → capture into contact → complete
    # ------------------------------------------------------------------
    print("--- scenario 2: ask/wait/feed ---")
    async with sf() as session:
        async with session.begin():
            ws2, conv2, contact2, flow2, _ = await _seed(session, ASK_GRAPH)
        ws2_id, conv2_id, contact2_id, ident2 = ws2.id, conv2.id, contact2.id, conv2.channel_identity_id

    await runtime.handle_event(sf, redis, _inbound(ws2_id, conv2_id, contact2_id, "start"))
    async with sf() as session:
        fs2 = (
            await session.execute(select(FlowSession).where(FlowSession.conversation_id == conv2_id))
        ).scalars().first()
        print(f"after 'start': session status={fs2.status} (expect waiting_reply)")
        if fs2.status != "waiting_reply":
            print("FAIL: ask node did not suspend into waiting_reply")
            ok = False

    # persist the visitor's reply message (ingress does this before emitting)
    reply_id = uuid.uuid4()
    async with sf() as session:
        async with session.begin():
            session.add(
                Message(
                    id=reply_id, workspace_id=ws2_id, conversation_id=conv2_id,
                    channel_identity_id=ident2, direction="in", sender_type="contact",
                    sender_id=contact2_id, msg_type="text",
                    content={"blocks": [{"kind": "text", "text": "bob@example.com"}]},
                    text_plain="bob@example.com", delivery_status="delivered",
                )
            )
    ev = _inbound(ws2_id, conv2_id, contact2_id, "bob@example.com")
    ev.payload["message_id"] = str(reply_id)
    await runtime.handle_event(sf, redis, ev)

    async with sf() as session:
        fs2 = (
            await session.execute(select(FlowSession).where(FlowSession.conversation_id == conv2_id))
        ).scalars().first()
        c2 = await session.get(Contact, contact2_id)
        thanks = (
            await session.execute(
                select(Message).where(
                    Message.conversation_id == conv2_id, Message.direction == "out",
                    Message.sender_type == "automation", Message.text_plain.ilike("Thanks%"),
                )
            )
        ).scalars().first()
        print(f"after reply: session status={fs2.status} captured contact.email={c2.email!r}")
        if fs2.status != "completed":
            print("FAIL: session did not complete after reply")
            ok = False
        if c2.email != "bob@example.com":
            print("FAIL: reply not captured to contact.email")
            ok = False
        if thanks is None:
            print("FAIL: thanks message (with captured var) not sent")
            ok = False
        else:
            print(f"thanks message: {thanks.text_plain!r}")

    await close_redis()
    print("SMOKE PASS" if ok else "SMOKE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
