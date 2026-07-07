"""Live end-to-end smoke for the reports pipeline (plan 附錄 B.4 verification).

Runs against the dockerised pg (5433) + redis (6380). Seeds a workspace with a
member, two contacts/conversations/sessions, emits a batch of *synthetic*
events into the raw events table (the same shapes the inbox/ingress emit), then:

  1. runs one rollup pass (+ presence fold)      → agg_*_hourly
  2. runs the nightly distinct-count day tables   → agg_customers_daily
  3. asserts queries.service_overview / customers / channels / summary /
     online_time return the expected aggregates
  4. exercises share freeze + AI-summary (FakeLLM + granted points)

Run:
    DATABASE_URL=postgresql+asyncpg://smartchat:smartchat@localhost:5433/smartchat \
    REDIS_URL=redis://localhost:6380/0 \
    .venv/Scripts/python -m apps.api.tests.reports.live_smoke
"""
from __future__ import annotations

import asyncio
import secrets
import sys
from datetime import UTC, datetime, timedelta

try:  # Windows consoles default to a legacy codepage; force UTF-8 for CJK output
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from py_contracts.events import Actor, Event
from sqlalchemy import select

from apps.api.app.analytics import ai_summary, attribution, daily, rollup
from apps.api.app.db import session_factory
from apps.api.app.models.channels import ChannelAccount
from apps.api.app.models.contacts import ChannelIdentity, Contact
from apps.api.app.models.conversations import Conversation, ConversationSession
from apps.api.app.models.members import User, WorkspaceMember
from apps.api.app.models.reports import AgentPresenceSession
from apps.api.app.models.tenancy import Plan, Workspace
from apps.api.app.modules.reports import queries, service
from apps.api.app.services import event_bus, points
from apps.api.app.services.llm_client import reset_default_llm, set_default_llm
from apps.api.app.services.redis_client import close_redis, get_redis

NOW = datetime.now(UTC).replace(microsecond=0)
_ok = True


def check(cond: bool, label: str, detail: str = "") -> None:
    global _ok
    status = "PASS" if cond else "FAIL"
    if not cond:
        _ok = False
    print(f"  [{status}] {label}{(' - ' + detail) if detail else ''}")


class FakeLLM:
    async def complete(self, *, tier, system, messages, max_tokens=1024, temperature=0.3):
        return "今日新會話較昨日上升，首次回應時間穩定。建議在高峰時段增派人力。"

    async def embed(self, texts):
        return [[0.0] for _ in texts]

    async def aclose(self):
        pass


async def _seed(session):
    plan = (await session.execute(select(Plan).where(Plan.code == "pro"))).scalars().first()
    if plan is None:
        plan = (await session.execute(select(Plan).limit(1))).scalars().first()
    if plan is None:
        plan = Plan(code="pro", name="Pro", limits={"ai_points_monthly": 100000})
        session.add(plan)
        await session.flush()
    user = User(email=f"rpt_{secrets.token_hex(4)}@ex.com", password_hash="x", display_name="Rpt")
    session.add(user)
    await session.flush()
    ws = Workspace(name="Reports Smoke", plan_code=plan.code, status="active",
                   settings={"timezone": "UTC"}, owner_user_id=user.id)
    session.add(ws)
    await session.flush()
    member = WorkspaceMember(workspace_id=ws.id, user_id=user.id, member_type="human",
                             display_name="Agent Smith", status="active")
    session.add(member)
    acct = ChannelAccount(workspace_id=ws.id, channel_type="whatsapp_cloud", name="WA",
                          external_id=f"wa_{secrets.token_hex(4)}", status="active", enabled=True)
    session.add(acct)
    await session.flush()

    convs = []
    # contact A: new today; contact B: seen 3 days ago (returning)
    for label, first_seen in (("A", NOW), ("B", NOW - timedelta(days=3))):
        contact = Contact(workspace_id=ws.id, display_name=f"Cust {label}", language="en",
                          first_seen_at=first_seen, last_seen_at=NOW)
        session.add(contact)
        await session.flush()
        ident = ChannelIdentity(workspace_id=ws.id, channel_account_id=acct.id,
                                channel_type="whatsapp_cloud", external_user_id=f"u_{secrets.token_hex(4)}",
                                contact_id=contact.id)
        session.add(ident)
        await session.flush()
        conv = Conversation(workspace_id=ws.id, channel_identity_id=ident.id,
                            channel_account_id=acct.id, channel_type="whatsapp_cloud",
                            contact_id=contact.id, status="open", handler="member",
                            assignee_member_id=member.id, session_count=1)
        session.add(conv)
        await session.flush()
        sess = ConversationSession(workspace_id=ws.id, conversation_id=conv.id,
                                   started_at=NOW - timedelta(minutes=5))
        session.add(sess)
        await session.flush()
        if label == "A":  # attribute contact A to a CTWA (messaging) ad
            await attribution.stamp(
                session, workspace_id=ws.id, conversation_id=conv.id,
                attribution=attribution.Attribution(source="ctwa", ad_id="ad_123", campaign_id="cmp_9"),
            )
        convs.append((contact, conv, sess))
    return ws, member, acct, convs


def _base(ws, conv, contact, acct):
    return dict(workspace_id=ws.id, conversation_id=conv.id, contact_id=contact.id,
                channel_type="whatsapp_cloud", channel_account_id=acct.id)


async def main() -> int:
    redis = get_redis()
    sf = session_factory()

    async with sf() as session:
        async with session.begin():
            ws, member, acct, convs = await _seed(session)
            (cA, convA, sessA), (cB, convB, sessB) = convs

            # --- synthetic events (shapes match ingress/messaging emitters) ---
            evs = [
                Event(type="conversation.created", actor=Actor(type="contact", id=cA.id),
                      occurred_at=NOW - timedelta(minutes=5), payload={}, **_base(ws, convA, cA, acct)),
                Event(type="message.created", actor=Actor(type="contact", id=cA.id),
                      occurred_at=NOW - timedelta(minutes=5),
                      payload={"direction": "in", "msg_type": "text"}, **_base(ws, convA, cA, acct)),
                Event(type="message.created", actor=Actor(type="member", id=member.id),
                      occurred_at=NOW - timedelta(minutes=1),
                      payload={"direction": "out", "msg_type": "text", "is_note": False},
                      **_base(ws, convA, cA, acct)),
                Event(type="conversation.first_responded", actor=Actor(type="member", id=member.id),
                      occurred_at=NOW - timedelta(minutes=1),
                      payload={"session_id": str(sessA.id),
                               "first_response_at": (NOW - timedelta(minutes=1)).isoformat()},
                      **_base(ws, convA, cA, acct)),
                Event(type="conversation.assigned", actor=Actor(type="system"),
                      occurred_at=NOW - timedelta(minutes=5),
                      payload={"handler": "member", "assignee_member_id": str(member.id)},
                      **_base(ws, convA, cA, acct)),
                Event(type="conversation.resolved", actor=Actor(type="member", id=member.id),
                      occurred_at=NOW,
                      payload={"session_id": str(sessA.id), "closed_at": NOW.isoformat()},
                      **_base(ws, convA, cA, acct)),
                Event(type="csat.submitted", actor=Actor(type="contact", id=cA.id),
                      occurred_at=NOW, payload={"score": 5, "agent_id": str(member.id)},
                      **_base(ws, convA, cA, acct)),
                # contact B returns → reopened cycle today
                Event(type="conversation.reopened", actor=Actor(type="contact", id=cB.id),
                      occurred_at=NOW - timedelta(minutes=3), payload={}, **_base(ws, convB, cB, acct)),
                Event(type="message.created", actor=Actor(type="contact", id=cB.id),
                      occurred_at=NOW - timedelta(minutes=3),
                      payload={"direction": "in", "msg_type": "text"}, **_base(ws, convB, cB, acct)),
            ]
            await event_bus.emit_many(session, evs)
            # open presence session for the member (~1h online)
            session.add(AgentPresenceSession(workspace_id=ws.id, agent_id=member.id,
                                             started_at=NOW - timedelta(hours=1),
                                             last_heartbeat_at=NOW))
        ws_id, member_id = ws.id, member.id
    print(f"seeded ws={ws_id} member={member_id}")

    # ---- 1) rollup: recompute the trailing window from the raw events table
    # (deterministic regardless of the shared watermark; also folds presence) ----
    folded = await rollup.reaggregate_window(sf, hours=2, now=NOW)
    print(f"rollup folded {folded} events")

    # ---- 2) nightly distinct day tables ----
    await daily.run_daily(sf, now=NOW)

    # ---- 3) assertions via the query layer (what the endpoints call) ----
    f_hour = queries.parse_filters(from_=(NOW - timedelta(hours=6)).isoformat(), to=NOW.isoformat(),
                                   interval="hour", channel_type=None, channel_account_id=None,
                                   member_id=None)
    f_day = queries.parse_filters(from_=(NOW - timedelta(days=1)).isoformat(), to=NOW.isoformat(),
                                  interval="day", channel_type=None, channel_account_id=None,
                                  member_id=None)
    async with sf() as session:
        svc = await queries.service_overview(session, ws_id, f_hour, now=NOW)
        print("service-overview:", svc["kpis"])
        check(svc["kpis"]["new_conversations_today"] == 2, "new_conversations_today == 2",
              str(svc["kpis"]["new_conversations_today"]))
        check(svc["kpis"]["in_progress"] == 2, "in_progress == 2 open convs",
              str(svc["kpis"]["in_progress"]))
        check(svc["kpis"]["online_members"] == 1, "online_members == 1",
              str(svc["kpis"]["online_members"]))
        check(sum(p["conversations"] for p in svc["trend"]) >= 2, "trend has >=2 conversations")

        ch = await queries.channels(session, ws_id, f_hour)
        wa = next((r for r in ch["rows"] if r["channel_type"] == "whatsapp_cloud"), None)
        check(wa is not None, "channels row for whatsapp present")
        if wa:
            print("channels whatsapp:", wa)
            check(wa["messages_in"] == 2, "messages_in == 2", str(wa["messages_in"]))
            check(wa["messages_out"] == 1, "messages_out == 1", str(wa["messages_out"]))
            check(wa["conversations"] == 2, "channel conversations == 2", str(wa["conversations"]))

        summ = await queries.summary(session, ws_id, f_hour)
        row = next((a for a in summ["agents"] if a["member_id"] == str(member_id)), None)
        check(row is not None, "summary has the agent")
        if row:
            print("summary agent:", row)
            check(row["msgs"] == 1, "agent msgs == 1", str(row["msgs"]))
            check(row["frt_avg_ms"] > 0, "frt_avg_ms > 0", str(row["frt_avg_ms"]))
            check(abs(row["csat_avg"] - 1.0) < 1e-6, "csat_avg == 1.0 (5star)", str(row["csat_avg"]))
            check(row["online_seconds"] >= 3600, "online_seconds >= 3600", str(row["online_seconds"]))
            check(row["resolution_avg_ms"] > 0, "resolution_avg_ms > 0", str(row["resolution_avg_ms"]))

        ot = await queries.online_time(session, ws_id, f_hour)
        orow = next((r for r in ot["rows"] if r["member_id"] == str(member_id)), None)
        check(orow is not None and orow["online_seconds"] >= 3600, "online-time >= 3600",
              str(orow["online_seconds"] if orow else None))

        cust = await queries.customers(session, ws_id, f_day, "day")
        print("customers kpis:", cust["kpis"])
        check(cust["kpis"]["new"] == 1, "customers new == 1 (contact A)", str(cust["kpis"]["new"]))
        check(cust["kpis"]["repeat"] == 1, "customers repeat == 1 (contact B)",
              str(cust["kpis"]["repeat"]))
        check(len(cust["trend"]) >= 1, "customers trend non-empty")

        cust_member = await queries.customers(session, ws_id, f_day, "member")
        check(cust_member["detail"]["dimension"] == "member", "customers detail dimension echoes")
        check(len(cust_member["detail"]["rows"]) >= 1, "customers member pivot non-empty")

        # ads (CTWA attribution → 訊息廣告 / messenger platform)
        adsm = await queries.ads(session, ws_id, "messenger", f_day)
        print("ads messenger:", adsm["rows"])
        check(len(adsm["rows"]) >= 1, "ads messenger has a row")
        if adsm["rows"]:
            r0 = adsm["rows"][0]
            check(r0["會話數"] == 1, "ad conversations == 1", str(r0["會話數"]))
            check(r0["訊息數"] == 2, "ad messages == 2", str(r0["訊息數"]))

    # ---- 4) share freeze ----
    async with sf() as session:
        cfg = queries.config_dict(f_day, dimension="day")
        shared = await service.create_share(session, ws_id, member_id, "customers", cfg)
    async with sf() as session:
        rerun = await service.run_shared_report(session, shared["token"])
        check(rerun["report_key"] == "customers", "shared report re-runs frozen config")
        check(rerun["data"]["kpis"]["new"] == 1, "shared report data matches live",
              str(rerun["data"]["kpis"]["new"]))

    # ---- 5) AI summary (FakeLLM + granted points) ----
    async with sf() as session:
        async with session.begin():
            await points.grant_monthly(session, redis, workspace_id=ws_id, points=1000,
                                       period_month=points.current_period(NOW))
    set_default_llm(FakeLLM())
    try:
        text = await ai_summary.generate_for_workspace(sf, redis, ws_id, now=NOW + timedelta(days=1),
                                                       force=True)
    finally:
        reset_default_llm()
    check(bool(text), "ai summary generated (Pro plan + points)", (text or "")[:24])
    async with sf() as session:
        latest = await service.latest_ai_summary(session, ws_id)
        check(bool(latest["text"]), "ai summary stored + readable")
    async with sf() as session:
        bal = await points.load_balance(session, ws_id)
    check(bal == 1000 - ai_summary.AI_SUMMARY_COST, "20 AI points spent", str(bal))

    await close_redis()
    print("SMOKE PASS" if _ok else "SMOKE FAIL")
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
