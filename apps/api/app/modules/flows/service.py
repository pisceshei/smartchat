"""Flow management service (plan 附錄 B.1).

Publish freezes the draft graph into a flow_versions row and rebuilds the
denormalised flow_triggers index; in-flight sessions keep their pinned version
(never hot-swapped). Test-run snapshots the *draft* graph into a private version
and starts a mode=test session on an internal sandbox conversation (never a real
channel, excluded from stats). Stats read flow_stats_daily (session counts) +
flow_stats_users (exact distinct users); the funnel reads flow_session_steps.

`flow_bot_available` is the generalised bot-routing hook the routing service can
import so non-widget channels with a matching enabled trigger enter handler=bot.
"""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...flows.graph_schema import extract_triggers, parse_graph, validate_graph
from ...models.channels import ChannelAccount
from ...models.contacts import ChannelIdentity, Contact
from ...models.conversations import Conversation
from ...models.flows import (
    Flow,
    FlowSession,
    FlowSessionStep,
    FlowStatsDaily,
    FlowStatsUser,
    FlowTrigger,
    FlowVersion,
)

# trigger types that make a bot engage a fresh inbound conversation
BOT_ENGAGEMENT_TRIGGERS: tuple[str, ...] = (
    "visitor_message",
    "new_visitor",
    "returning_visitor",
    "widget_opened",
    "page_visited",
    "lead_submitted",
)


class PublishError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# ==========================================================================
# publish
# ==========================================================================
@dataclass
class PublishResult:
    version_id: uuid.UUID
    version_no: int
    trigger_count: int
    events: list[Any] = field(default_factory=list)


async def publish_flow(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    flow: Flow,
    member_id: uuid.UUID | None,
) -> PublishResult:
    """validate_graph → freeze a flow_versions row → rebuild flow_triggers.
    Raises PublishError with the validation errors on an invalid graph."""
    graph = parse_graph(flow.draft_graph or {})
    errors = validate_graph(graph, channel_type=flow.channel_type)
    if errors:
        raise PublishError(errors)

    max_no = (
        await session.execute(
            select(func.coalesce(func.max(FlowVersion.version_no), 0)).where(
                FlowVersion.flow_id == flow.id, FlowVersion.version_no > 0
            )
        )
    ).scalar_one()
    version = FlowVersion(
        workspace_id=workspace_id,
        flow_id=flow.id,
        version_no=int(max_no) + 1,
        graph=flow.draft_graph or {},
        published_by_member_id=member_id,
    )
    session.add(version)
    await session.flush()
    flow.published_version_id = version.id

    # rebuild the denormalised trigger index for this flow
    await session.execute(delete(FlowTrigger).where(FlowTrigger.flow_id == flow.id))
    extracted = extract_triggers(graph)
    for et in extracted:
        session.add(
            FlowTrigger(
                workspace_id=workspace_id,
                flow_id=flow.id,
                version_id=version.id,
                node_id=et.node_id,
                trigger_type=et.trigger_type,
                channel_type=flow.channel_type,
                priority=flow.priority,
                enabled=flow.enabled,
                config=et.config,
                freq_cap=et.freq_cap,
            )
        )
    await session.flush()
    return PublishResult(
        version_id=version.id, version_no=version.version_no, trigger_count=len(extracted)
    )


async def sync_trigger_flags(session: AsyncSession, flow: Flow) -> None:
    """After a flow's enabled/priority/channel edit, keep its denormalised
    flow_triggers rows in sync so the router's SQL filter stays correct."""
    await session.execute(
        update(FlowTrigger)
        .where(FlowTrigger.flow_id == flow.id)
        .values(enabled=flow.enabled, priority=flow.priority, channel_type=flow.channel_type)
    )


# ==========================================================================
# duplicate
# ==========================================================================
async def duplicate_flow(
    session: AsyncSession, *, workspace_id: uuid.UUID, source: Flow, member_id: uuid.UUID | None
) -> Flow:
    dup = Flow(
        workspace_id=workspace_id,
        channel_type=source.channel_type,
        name=f"{source.name} (副本)"[:160],
        description=source.description,
        category_id=source.category_id,
        enabled=False,  # a copy starts disabled + unpublished
        priority=source.priority,
        draft_graph=source.draft_graph or {},
        updated_by_member_id=member_id,
    )
    session.add(dup)
    await session.flush()
    return dup


# ==========================================================================
# test run (mode=test on the DRAFT graph, internal sandbox)
# ==========================================================================
_SANDBOX_EXT = "__flowtest__"


async def _sandbox_widget_account(
    session: AsyncSession, workspace_id: uuid.UUID
) -> ChannelAccount:
    ext = f"{_SANDBOX_EXT}:{workspace_id}"
    acct = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type == "widget", ChannelAccount.external_id == ext
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        acct = ChannelAccount(
            workspace_id=workspace_id,
            channel_type="widget",
            name="Flow Test Sandbox",
            external_id=ext,
            status="active",
            enabled=True,
        )
        session.add(acct)
        await session.flush()
    return acct


async def _sandbox_conversation(
    session: AsyncSession, workspace_id: uuid.UUID
) -> tuple[Conversation, Contact]:
    """Fresh throwaway contact + identity + open widget conversation for a test
    run. Internal widget channel = no real channel delivery."""
    acct = await _sandbox_widget_account(session, workspace_id)
    contact = Contact(
        workspace_id=workspace_id,
        display_name="Flow Test Visitor",
        language="en",
        country="US",
        device="desktop",
    )
    session.add(contact)
    await session.flush()
    identity = ChannelIdentity(
        workspace_id=workspace_id,
        channel_account_id=acct.id,
        channel_type="widget",
        external_user_id=f"{_SANDBOX_EXT}_{secrets.token_hex(8)}",
        contact_id=contact.id,
        display_name=contact.display_name,
    )
    session.add(identity)
    await session.flush()
    conv = Conversation(
        workspace_id=workspace_id,
        channel_identity_id=identity.id,
        channel_account_id=acct.id,
        channel_type="widget",
        contact_id=contact.id,
        status="open",
        handler="bot",
        bot_managed=True,
        session_count=1,
    )
    session.add(conv)
    await session.flush()
    return conv, contact


async def _snapshot_test_version(session: AsyncSession, flow: Flow) -> FlowVersion:
    """Freeze the current draft into a private (negative version_no) FlowVersion
    so the mode=test session can pin to a real row + be resumed by the runtime."""
    min_no = (
        await session.execute(
            select(func.coalesce(func.min(FlowVersion.version_no), 0)).where(
                FlowVersion.flow_id == flow.id, FlowVersion.version_no < 0
            )
        )
    ).scalar_one()
    version = FlowVersion(
        workspace_id=flow.workspace_id,
        flow_id=flow.id,
        version_no=int(min_no) - 1,
        graph=flow.draft_graph or {},
    )
    session.add(version)
    await session.flush()
    return version


async def test_run(
    session: AsyncSession,
    redis: aioredis.Redis,
    *,
    workspace_id: uuid.UUID,
    flow: Flow,
    workspace_tz: str = "UTC",
    now: datetime | None = None,
) -> tuple[FlowSession, list[Any]]:
    """Start a mode=test session on the DRAFT graph (validated first) bound to a
    sandbox conversation. Excluded from stats; external requests carry the
    X-Flow-Test header (see actions.act_external_request)."""
    from apps.flow_engine import interpreter  # local import: avoids a cycle

    graph = parse_graph(flow.draft_graph or {})
    errors = validate_graph(graph, channel_type=flow.channel_type)
    if errors:
        raise PublishError(errors)
    version = await _snapshot_test_version(session, flow)
    conv, contact = await _sandbox_conversation(session, workspace_id)
    fs, events = await interpreter.start_session(
        session,
        redis,
        flow=flow,
        flow_version_id=version.id,
        graph=graph,
        conversation=conv,
        contact=contact,
        trigger_vars={"event_type": "test_run", "channel_type": "widget"},
        mode="test",
        workspace_tz=workspace_tz,
        now=now or datetime.now(UTC),
    )
    return fs, events


# ==========================================================================
# stats (7-day) + funnel drill-down
# ==========================================================================
@dataclass
class FlowStats:
    triggered_sessions: int = 0
    triggered_users: int = 0
    engaged_users: int = 0
    completed_sessions: int = 0

    @property
    def engagement_rate(self) -> float:
        return round(self.engaged_users / self.triggered_users, 4) if self.triggered_users else 0.0

    @property
    def completion_rate(self) -> float:
        return (
            round(self.completed_sessions / self.triggered_sessions, 4)
            if self.triggered_sessions
            else 0.0
        )


async def stats_for_flows(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    flow_ids: list[uuid.UUID],
    days: int = 7,
    today: date | None = None,
) -> dict[uuid.UUID, FlowStats]:
    """7-day rollup per flow: session counts summed from flow_stats_daily,
    distinct users counted exactly from flow_stats_users."""
    out: dict[uuid.UUID, FlowStats] = {fid: FlowStats() for fid in flow_ids}
    if not flow_ids:
        return out
    today = today or datetime.now(UTC).date()
    since = today - timedelta(days=days - 1)

    for flow_id, ts, cs in (
        await session.execute(
            select(
                FlowStatsDaily.flow_id,
                func.coalesce(func.sum(FlowStatsDaily.triggered_sessions), 0),
                func.coalesce(func.sum(FlowStatsDaily.completed_sessions), 0),
            )
            .where(
                FlowStatsDaily.workspace_id == workspace_id,
                FlowStatsDaily.flow_id.in_(flow_ids),
                FlowStatsDaily.day >= since,
            )
            .group_by(FlowStatsDaily.flow_id)
        )
    ).all():
        st = out.setdefault(flow_id, FlowStats())
        st.triggered_sessions = int(ts)
        st.completed_sessions = int(cs)

    for flow_id, tu in (
        await session.execute(
            select(
                FlowStatsUser.flow_id,
                func.count(func.distinct(FlowStatsUser.contact_id)),
            )
            .where(
                FlowStatsUser.workspace_id == workspace_id,
                FlowStatsUser.flow_id.in_(flow_ids),
                FlowStatsUser.day >= since,
            )
            .group_by(FlowStatsUser.flow_id)
        )
    ).all():
        st = out.setdefault(flow_id, FlowStats())
        st.triggered_users = int(tu)

    # engaged distinct users = filtered distinct count over the same window
    for flow_id, eu in (
        await session.execute(
            select(
                FlowStatsUser.flow_id,
                func.count(func.distinct(FlowStatsUser.contact_id)),
            )
            .where(
                FlowStatsUser.workspace_id == workspace_id,
                FlowStatsUser.flow_id.in_(flow_ids),
                FlowStatsUser.day >= since,
                FlowStatsUser.engaged.is_(True),
            )
            .group_by(FlowStatsUser.flow_id)
        )
    ).all():
        st = out.setdefault(flow_id, FlowStats())
        st.engaged_users = int(eu)

    return out


async def flow_funnel(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    flow_id: uuid.UUID,
    days: int = 7,
) -> list[dict[str, Any]]:
    """Per-node funnel: distinct sessions that reached each node + error count,
    over the window (from flow_session_steps, live sessions only)."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await session.execute(
            select(
                FlowSessionStep.node_id,
                FlowSessionStep.node_type,
                func.count(func.distinct(FlowSessionStep.session_id)),
                func.count().filter(FlowSessionStep.status == "error"),
            )
            .join(FlowSession, FlowSession.id == FlowSessionStep.session_id)
            .where(
                FlowSessionStep.workspace_id == workspace_id,
                FlowSessionStep.flow_id == flow_id,
                FlowSession.mode == "live",
                FlowSessionStep.ts >= since,
            )
            .group_by(FlowSessionStep.node_id, FlowSessionStep.node_type)
        )
    ).all()
    return [
        {"node_id": nid, "node_type": ntype, "sessions": int(cnt), "errors": int(errs)}
        for nid, ntype, cnt, errs in rows
    ]


# ==========================================================================
# routing hook (imported by the routing service — plan: generalise bot routing)
# ==========================================================================
async def flow_bot_available(session: AsyncSession, conversation: Conversation) -> bool:
    """True if this conversation's channel has an enabled, published flow whose
    trigger would engage a fresh inbound (→ handler=bot). Routing can call this
    to generalise bot routing beyond widgets.default_flow_id."""
    exists = (
        await session.execute(
            select(FlowTrigger.id)
            .join(Flow, Flow.id == FlowTrigger.flow_id)
            .where(
                FlowTrigger.workspace_id == conversation.workspace_id,
                FlowTrigger.channel_type == conversation.channel_type,
                FlowTrigger.trigger_type.in_(BOT_ENGAGEMENT_TRIGGERS),
                FlowTrigger.enabled.is_(True),
                Flow.enabled.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return exists is not None
