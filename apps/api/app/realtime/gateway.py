"""ws-gateway — separate FastAPI app (plan A.8), run as its own service:

    uvicorn apps.api.app.realtime.gateway:app --port 8001

API deploys never drop sockets; no sticky sessions — any replica serves any
connection (subscriptions are per-process, replay state lives in Redis).

Routes:
- WS  /ws/agent   ?token=<JWT access>&workspace_id=…[&conversation_id=…&tab=…]
- WS  /ws/widget  ?token=<visitor token from widget bootstrap>
- GET /widget/events?token=…&cursor=<seq>   long-poll fallback, holds ≤25s

Upstream frames are limited to typing / read-cursor / ping / resume (+ away
and focus scope updates). Message SENDING is REST with client_msg_id — never
a socket frame. All downstream writes go through the per-connection queue so
the pump task is the only websocket writer.

Close codes: 4401 bad/expired token · 4403 not a workspace member · 4408 idle.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import session_factory
from ..models.members import MemberGroupMember, Role, WorkspaceMember
from ..models.tenancy import Workspace
from ..services.redis_client import close_redis, get_redis
from ..services.security import TokenInvalid, verify_token
from . import presence, unread
from .hub import Connection, Hub, collect_replay
from .presence import PresenceWatcher
from .protocol import (
    AUDIENCE_AGENTS,
    AgentScope,
    AwayFrame,
    FocusFrame,
    FrameError,
    PingFrame,
    ReadFrame,
    ResumeAction,
    ResumeFrame,
    Throttle,
    TypingFrame,
    UpstreamFrame,
    VisitorScope,
    VisitorTokenInvalid,
    error_frame,
    filter_for_visitor,
    hello_frame,
    parse_frame,
    pong_frame,
    pubsub_key,
    resume_ok_frame,
    resync_frame,
    verify_visitor_token,
    visitor_audience,
    visitor_pubsub_key,
)
from .publisher import current_seq, publish

log = logging.getLogger("smartchat.realtime.gateway")

RECEIVE_TIMEOUT = 90.0  # 3 missed heartbeats → close idle socket
LONGPOLL_HOLD = 24.0  # ≤25s per plan (safe under Cloudflare's 100s limit)

CLOSE_BAD_TOKEN = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_IDLE = 4408


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = get_redis()
    hub = Hub(redis)
    watcher = PresenceWatcher(redis)
    await hub.start()
    await watcher.start()
    app.state.redis = redis
    app.state.hub = hub
    app.state.watcher = watcher
    try:
        yield
    finally:
        await watcher.stop()
        await hub.stop()
        await close_redis()


app = FastAPI(title="SmartChat WS Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # widget long-poll is embedded on merchant sites
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "connections": app.state.hub.connection_count()}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _parse_uuid(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def _load_agent_scope(
    session: AsyncSession,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    open_conversation_id: uuid.UUID | None,
    active_tab: str | None,
) -> AgentScope | None:
    row = (
        await session.execute(
            select(WorkspaceMember, Role, Workspace)
            .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
            .outerjoin(Role, Role.id == WorkspaceMember.role_id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.status == "active",
                WorkspaceMember.member_type == "human",
            )
        )
    ).first()
    if row is None:
        return None
    member, role, workspace = row
    if workspace.status != "active":
        return None
    group_ids = set(
        (
            await session.execute(
                select(MemberGroupMember.group_id).where(
                    MemberGroupMember.workspace_id == workspace_id,
                    MemberGroupMember.member_id == member.id,
                )
            )
        ).scalars()
    )
    return AgentScope(
        member_id=member.id,
        workspace_id=workspace_id,
        permissions=set(role.permissions) if role is not None else set(),
        group_ids=group_ids,
        open_conversation_id=open_conversation_id,
        active_tab=active_tab,
        display_name=member.display_name,
    )


async def _pump(conn: Connection, websocket: WebSocket) -> None:
    """Sole websocket writer: drains the connection queue; if the hub dropped
    frames (slow consumer) it tells the client to resync instead of lying."""
    redis = get_redis()
    while True:
        frame = await conn.queue.get()
        await websocket.send_json(frame)
        if conn.overflowed and conn.queue.empty():
            conn.overflowed = False
            websocket_seq = await current_seq(redis, conn.workspace_id)
            await websocket.send_json(resync_frame(websocket_seq))


async def _replay_to_conn(conn: Connection, resume_from: int) -> None:
    redis = get_redis()
    action, events, cursor = await collect_replay(redis, conn.workspace_id, resume_from)
    if action is ResumeAction.RESYNC:
        conn.enqueue(resync_frame(cursor))
        return
    replayed = 0
    for ev in events:
        frame = conn.frame_for(ev)
        if frame is not None:
            conn.enqueue(frame)
            replayed += 1
    conn.enqueue(resume_ok_frame(cursor, replayed))


# --------------------------------------------------------------------------
# agent socket
# --------------------------------------------------------------------------
@app.websocket("/ws/agent")
async def ws_agent(
    websocket: WebSocket,
    token: str = Query(default=""),
    workspace_id: str = Query(default=""),
    conversation_id: str | None = Query(default=None),
    tab: str | None = Query(default=None),
) -> None:
    await websocket.accept()
    try:
        claims = verify_token(token, expected_type="access")
        user_id = uuid.UUID(claims["sub"])
        ws_id = uuid.UUID(workspace_id)
    except (TokenInvalid, KeyError, ValueError):
        await websocket.close(code=CLOSE_BAD_TOKEN)
        return

    async with session_factory()() as session:
        scope = await _load_agent_scope(
            session, user_id, ws_id, _parse_uuid(conversation_id), tab
        )
    if scope is None:
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    redis = get_redis()
    hub: Hub = websocket.app.state.hub
    conn = Connection(kind="agent", workspace_id=ws_id, scope=scope, channels=(pubsub_key(ws_id),))
    conn.enqueue(hello_frame(await current_seq(redis, ws_id), conn.id))
    await hub.attach(conn)
    pump = asyncio.create_task(_pump(conn, websocket), name=f"pump-{conn.id}")
    typing_throttle = Throttle()
    try:
        await presence.mark_member_online(redis, ws_id, scope.member_id, display_name=scope.display_name)
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=RECEIVE_TIMEOUT)
            except TimeoutError:
                await websocket.close(code=CLOSE_IDLE)
                break
            try:
                frame = parse_frame(raw)
            except FrameError as e:
                conn.enqueue(error_frame("bad_frame", str(e)))
                continue
            await _handle_agent_frame(conn, scope, frame, typing_throttle)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("agent socket error (conn %s)", conn.id)
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await hub.detach(conn)
        # presence: never DEL (other replicas may hold sockets) — TTL expires


async def _handle_agent_frame(
    conn: Connection, scope: AgentScope, frame: UpstreamFrame, typing_throttle: Throttle
) -> None:
    redis = get_redis()
    match frame:
        case PingFrame():
            await presence.heartbeat_member(
                redis, scope.workspace_id, scope.member_id, display_name=scope.display_name
            )
            conn.enqueue(pong_frame())
        case TypingFrame(conversation_id=conv_id, channel_identity_id=identity_id):
            if not typing_throttle.allow(str(conv_id)):
                return
            audiences: list[str] = [AUDIENCE_AGENTS]
            if identity_id is not None:
                audiences.append(visitor_audience(identity_id))
            await publish(
                scope.workspace_id,
                "typing",
                {
                    "conversation_id": str(conv_id),
                    "sender": "agent",
                    "member_id": str(scope.member_id),
                },
                audiences,
                conversation_id=conv_id,
                channel_identity_id=identity_id,
                persist=False,
                redis=redis,
            )
        case ReadFrame(conversation_id=conv_id, message_id=msg_id):
            async with session_factory()() as session:
                await unread.advance_read_cursor(
                    session, redis, scope.workspace_id, scope.member_id, conv_id, msg_id
                )
                await session.commit()
        case ResumeFrame(resume_from=resume_from):
            await _replay_to_conn(conn, resume_from)
        case AwayFrame(away=away):
            await presence.set_member_away(
                redis, scope.workspace_id, scope.member_id, away, display_name=scope.display_name
            )
        case FocusFrame(conversation_id=conv_id, tab=tab):
            scope.open_conversation_id = conv_id
            if tab is not None:
                scope.active_tab = tab


# --------------------------------------------------------------------------
# widget socket
# --------------------------------------------------------------------------
@app.websocket("/ws/widget")
async def ws_widget(websocket: WebSocket, token: str = Query(default="")) -> None:
    await websocket.accept()
    try:
        scope = verify_visitor_token(token)
    except VisitorTokenInvalid:
        await websocket.close(code=CLOSE_BAD_TOKEN)
        return

    redis = get_redis()
    hub: Hub = websocket.app.state.hub
    conn = Connection(
        kind="visitor",
        workspace_id=scope.workspace_id,
        scope=scope,
        channels=(visitor_pubsub_key(scope.channel_identity_id),),
    )
    conn.enqueue(hello_frame(await current_seq(redis, scope.workspace_id), conn.id))
    await hub.attach(conn)
    pump = asyncio.create_task(_pump(conn, websocket), name=f"pump-{conn.id}")
    typing_throttle = Throttle()
    try:
        await presence.mark_visitor_online(redis, scope.workspace_id, scope.channel_identity_id)
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_json(), timeout=RECEIVE_TIMEOUT)
            except TimeoutError:
                await websocket.close(code=CLOSE_IDLE)
                break
            try:
                frame = parse_frame(raw)
            except FrameError as e:
                conn.enqueue(error_frame("bad_frame", str(e)))
                continue
            await _handle_visitor_frame(conn, scope, frame, typing_throttle)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("widget socket error (conn %s)", conn.id)
    finally:
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        await hub.detach(conn)


async def _handle_visitor_frame(
    conn: Connection, scope: VisitorScope, frame: UpstreamFrame, typing_throttle: Throttle
) -> None:
    redis = get_redis()
    match frame:
        case PingFrame():
            await presence.heartbeat_visitor(redis, scope.workspace_id, scope.channel_identity_id)
            conn.enqueue(pong_frame())
        case TypingFrame(conversation_id=conv_id):
            if not typing_throttle.allow(str(conv_id)):
                return
            await publish(
                scope.workspace_id,
                "typing",
                {
                    "conversation_id": str(conv_id),
                    "sender": "visitor",
                    "channel_identity_id": str(scope.channel_identity_id),
                },
                (AUDIENCE_AGENTS,),
                conversation_id=conv_id,
                channel_identity_id=scope.channel_identity_id,
                persist=False,
                redis=redis,
            )
        case ReadFrame(conversation_id=conv_id, message_id=msg_id):
            # visitor read receipt → agents' UI; message row update is inbox-side
            await publish(
                scope.workspace_id,
                "visitor.read",
                {
                    "conversation_id": str(conv_id),
                    "message_id": str(msg_id) if msg_id else None,
                    "channel_identity_id": str(scope.channel_identity_id),
                },
                (AUDIENCE_AGENTS,),
                conversation_id=conv_id,
                channel_identity_id=scope.channel_identity_id,
                persist=False,
                redis=redis,
            )
        case ResumeFrame(resume_from=resume_from):
            await _replay_to_conn(conn, resume_from)
        case AwayFrame() | FocusFrame():
            conn.enqueue(error_frame("not_allowed", "frame not available on widget sockets"))


# --------------------------------------------------------------------------
# widget long-poll fallback (same seq/dedup semantics as the socket)
# --------------------------------------------------------------------------
@app.get("/widget/events")
async def widget_events(
    token: str = Query(default=""),
    cursor: int = Query(default=0, ge=0),
) -> JSONResponse:
    try:
        scope = verify_visitor_token(token)
    except VisitorTokenInvalid:
        return JSONResponse({"detail": "invalid token"}, status_code=401)

    redis = get_redis()
    hub: Hub = app.state.hub
    await presence.mark_visitor_online(redis, scope.workspace_id, scope.channel_identity_id)

    action, events, replay_cursor = await collect_replay(redis, scope.workspace_id, cursor)
    if action is ResumeAction.RESYNC:
        return JSONResponse({"events": [], "cursor": replay_cursor, "resync_required": True})

    frames = [f for f in (filter_for_visitor(ev, scope) for ev in events) if f is not None]
    if frames:
        return JSONResponse({"events": frames, "cursor": replay_cursor, "resync_required": False})

    # nothing pending — hold the request on a throwaway hub connection
    conn = Connection(
        kind="visitor",
        workspace_id=scope.workspace_id,
        scope=scope,
        channels=(visitor_pubsub_key(scope.channel_identity_id),),
    )
    await hub.attach(conn)
    try:
        live = await hub.wait_frames(conn, LONGPOLL_HOLD)
    finally:
        await hub.detach(conn)
    seqs = [f["seq"] for f in live if isinstance(f.get("seq"), int)]
    out_cursor = max([replay_cursor, *seqs]) if seqs else replay_cursor
    return JSONResponse({"events": live, "cursor": out_cursor, "resync_required": False})
