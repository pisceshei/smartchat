"""Live P3 broadcast smoke (needs the dockerised pg:5433 + redis:6380).

Creates a Pro workspace + a widget channel account + a mixed audience (normal /
blacklisted / unsubscribed / no-identity), a dynamic segment, and a one_time
broadcast; runs the fan-out (``fanout.execute_run``) inline and asserts:

  * survivors reach ``sent`` via messaging.send_message(sender_type='campaign')
  * suppression produces skipped(blacklist|unsubscribed|invalid_identity)
  * run + broadcast counters roll up, success_rate is delivered÷sent
  * the delivery bridge advances a recipient sent → delivered

Run:  python -m apps.api.tests.marketing.live_smoke
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://smartchat:smartchat@localhost:5433/smartchat")
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")

from sqlalchemy import delete, func, select  # noqa: E402

from apps.api.app.db import session_factory  # noqa: E402
from apps.api.app.marketing import fanout  # noqa: E402
from apps.api.app.marketing import recipients as rcpt
from apps.api.app.models.channels import ChannelAccount  # noqa: E402
from apps.api.app.models.contacts import ChannelIdentity, Contact  # noqa: E402
from apps.api.app.models.marketing import Broadcast, BroadcastRecipient, BroadcastRun  # noqa: E402
from apps.api.app.models.messaging import Message  # noqa: E402
from apps.api.app.models.tenancy import Workspace  # noqa: E402
from apps.api.app.modules.broadcasts import service as bsvc  # noqa: E402
from apps.api.app.modules.segments import service as seg_svc  # noqa: E402
from apps.api.app.services.redis_client import close_redis, get_redis  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), msg)
    if not cond:
        FAILS.append(msg)


async def main() -> int:
    sf = session_factory()
    redis = get_redis()
    ws_id = uuid.uuid4()

    async with sf() as s:
        async with s.begin():
            s.add(Workspace(id=ws_id, name="MktSmoke", plan_code="pro", status="active"))
            await s.flush()
            acct = ChannelAccount(
                workspace_id=ws_id, channel_type="widget",
                external_id=f"smoke-{ws_id.hex[:8]}", name="Smoke Widget",
            )
            s.add(acct)
            await s.flush()
            acct_id = acct.id
            # audience: 2 normal, 1 blacklisted, 1 unsubscribed, 1 without identity
            specs = [
                ("c1", False, {}, True),
                ("c2", False, {}, True),
                ("c3", True, {}, True),                          # blacklist
                ("c4", False, {"marketing_opt_out": True}, True),  # unsubscribe
                ("c5", False, {}, False),                        # no identity
            ]
            for name, black, custom, has_ident in specs:
                c = Contact(workspace_id=ws_id, display_name=name, country="HK",
                            is_blacklisted=black, custom=custom)
                s.add(c)
                await s.flush()
                if has_ident:
                    s.add(ChannelIdentity(
                        workspace_id=ws_id, channel_account_id=acct_id, channel_type="widget",
                        external_user_id=f"ext-{name}", contact_id=c.id,
                    ))

    # segment estimate (dynamic, all HK contacts)
    async with sf() as s:
        est = await seg_svc.estimate_count(s, ws_id, {"field": "country", "op": "eq", "value": "HK"})
    check(est == 5, f"segment estimate counts 5 contacts (got {est})")

    # create the segment + broadcast (immediate one_time) via the real service
    async with sf() as s:
        seg = __import__("apps.api.app.models.marketing", fromlist=["Segment"]).Segment(
            workspace_id=ws_id, name="all-hk", mode="dynamic",
            definition={"field": "country", "op": "eq", "value": "HK"},
        )
        s.add(seg)
        await s.flush()
        seg_id = seg.id
        bc, run_id = await bsvc.create(
            s, workspace_id=ws_id, created_by_member_id=None,
            data={
                "name": "Smoke Blast", "type": "one_time", "channel_type": "widget",
                "channel_account_id": acct_id, "segment_id": seg_id, "template_id": None,
                "variable_mapping": {"text": "Hi {{display_name}}"}, "schedule": {},
                "send_rules": {},
            },
        )
        await s.commit()
    check(run_id is not None, "immediate one_time broadcast created a run")

    # run the fan-out inline
    result = await fanout.execute_run(sf, redis, run_id)
    check(result == "completed", f"fan-out completed (got {result})")

    # assert recipient states
    async with sf() as s:
        rows = (
            await s.execute(
                select(BroadcastRecipient.state, BroadcastRecipient.skip_reason, func.count())
                .where(BroadcastRecipient.run_id == run_id)
                .group_by(BroadcastRecipient.state, BroadcastRecipient.skip_reason)
            )
        ).all()
        by = {(st, sr): int(c) for st, sr, c in rows}
        run = await s.get(BroadcastRun, run_id)
        bc = await s.get(Broadcast, bc.id)
        n_campaign_msgs = (
            await s.execute(
                select(func.count()).select_from(Message)
                .where(Message.workspace_id == ws_id, Message.sender_type == "campaign")
            )
        ).scalar_one()

    sent = by.get(("sent", None), 0)
    check(sent == 2, f"2 recipients reached 'sent' via send_message (got {sent})")
    check(by.get(("skipped", "blacklist"), 0) == 1, "1 skipped(blacklist)")
    check(by.get(("skipped", "unsubscribed"), 0) == 1, "1 skipped(unsubscribed)")
    check(by.get(("skipped", "invalid_identity"), 0) == 1, "1 skipped(invalid_identity)")
    check(run.status == "completed", f"run marked completed (got {run.status})")
    check(run.sent == 2 and run.skipped == 3, f"run counters sent=2 skipped=3 (got {run.sent}/{run.skipped})")
    check(bc.sent_count == 2 and bc.skipped_count == 3, "broadcast rollup sent=2 skipped=3")
    check(int(n_campaign_msgs) == 2, f"2 campaign messages persisted (got {n_campaign_msgs})")

    # delivery bridge: advance one sent recipient to delivered via a status event
    async with sf() as s:
        msg_id = (
            await s.execute(
                select(Message.id).where(
                    Message.workspace_id == ws_id, Message.sender_type == "campaign"
                ).limit(1)
            )
        ).scalar_one()
    async with sf() as s:
        async with s.begin():
            advanced = await rcpt.handle_delivery_status(
                s, redis, message_id=msg_id, status="delivered"
            )
            await rcpt.flush_run_counters(s, run_id)
    async with sf() as s:
        run = await s.get(BroadcastRun, run_id)
    check(advanced and run.delivered == 1, f"delivery bridge advanced 1 → delivered (got {run.delivered})")
    check(rcpt.success_rate(run.sent, run.delivered) == round(1 / 2, 4), "success_rate = delivered÷sent")

    # cleanup
    async with sf() as s:
        async with s.begin():
            await s.execute(delete(Workspace).where(Workspace.id == ws_id))
    await close_redis()

    print("\n== RESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
