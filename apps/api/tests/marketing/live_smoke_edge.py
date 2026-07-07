"""Live edge + lifecycle smoke (needs pg:5433 + redis:6380).

Covers the split-link edge redirect (302 → wa.me + click row + counter), the
attribution loop (inbound tracking code → conversation_attribution), and the
broadcast pause→resume→cancel transitions (exercises the sql_update path).

Run:  python -m apps.api.tests.marketing.live_smoke_edge
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://smartchat:smartchat@localhost:5433/smartchat")
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")

import httpx  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from apps.api.app.db import session_factory  # noqa: E402
from apps.api.app.marketing import attribution  # noqa: E402
from apps.api.app.models.channels import ChannelAccount  # noqa: E402
from apps.api.app.models.contacts import ChannelIdentity, Contact  # noqa: E402
from apps.api.app.models.conversations import Conversation  # noqa: E402
from apps.api.app.models.marketing import SplitLink, SplitLinkClick  # noqa: E402
from apps.api.app.models.reports import ConversationAttribution  # noqa: E402
from apps.api.app.models.tenancy import Workspace  # noqa: E402
from apps.api.app.modules.broadcasts import service as bsvc  # noqa: E402
from apps.api.app.modules.split_links import service as slsvc  # noqa: E402
from apps.api.app.services.redis_client import close_redis, get_redis  # noqa: E402
from apps.edge.split_redirect import app as edge_app  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), msg)
    if not cond:
        FAILS.append(msg)


async def main() -> int:
    sf = session_factory()
    redis = get_redis()
    ws_id = uuid.uuid4()
    slug = slsvc.base62(9)

    async with sf() as s:
        async with s.begin():
            s.add(Workspace(id=ws_id, name="EdgeSmoke", plan_code="pro", status="active"))
            await s.flush()
            link = SplitLink(
                workspace_id=ws_id, slug=slug, name="promo", channel_type="whatsapp",
                strategy="sequential",
                targets=[{"phone": "85212345678", "enabled": True, "weight": 1}],
                prefill_text="Order ref {{code}}", status="active",
            )
            s.add(link)
            await s.flush()
            link_id = link.id
    await slsvc.cache_config(redis, await _load(sf, link_id))

    # 1) edge redirect
    transport = httpx.ASGITransport(app=edge_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://edge") as client:
        r = await client.get(f"/s/{slug}", follow_redirects=False)
    check(r.status_code == 302, f"edge returns 302 (got {r.status_code})")
    loc = r.headers.get("location", "")
    check(loc.startswith("https://wa.me/85212345678"), f"302 → wa.me target (got {loc[:40]})")
    check("Order%20ref%20" in loc, "prefill text carried into deep link")

    # 2) click recorded + counter bumped
    async with sf() as s:
        clicks = (
            await s.execute(select(SplitLinkClick).where(SplitLinkClick.link_id == link_id))
        ).scalars().all()
        link = await s.get(SplitLink, link_id)
    check(len(clicks) == 1, f"1 split_link_click row (got {len(clicks)})")
    code = clicks[0].tracking_code if clicks else None
    check(bool(code) and clicks[0].target_idx == 0, "click has tracking_code + target_idx")
    check(link.click_count == 1, f"click_count bumped to 1 (got {link.click_count})")

    # 3) attribution loop: inbound message containing the code
    async with sf() as s:
        async with s.begin():
            acct = ChannelAccount(workspace_id=ws_id, channel_type="whatsapp_cloud",
                                  external_id=f"pn-{ws_id.hex[:8]}", name="wa")
            s.add(acct)
            c = Contact(workspace_id=ws_id, display_name="lead")
            s.add(c)
            await s.flush()
            ident = ChannelIdentity(workspace_id=ws_id, channel_account_id=acct.id,
                                    channel_type="whatsapp_cloud", external_user_id="8520001",
                                    contact_id=c.id)
            s.add(ident)
            await s.flush()
            conv = Conversation(workspace_id=ws_id, channel_identity_id=ident.id,
                                channel_account_id=acct.id, channel_type="whatsapp_cloud",
                                contact_id=c.id, status="open", handler="unassigned")
            s.add(conv)
            await s.flush()
            conv_id = conv.id
            linked = await attribution.attribute_inbound(
                s, workspace_id=ws_id, conversation_id=conv_id, text=f"Order ref {code} please",
            )
    check(linked, "attribution linked the inbound code to a click")
    async with sf() as s:
        attr = await s.get(ConversationAttribution, conv_id)
    check(attr is not None and attr.source == "splitlink" and attr.ref_code == code,
          "conversation_attribution row written (source=splitlink)")

    # 4) broadcast lifecycle: scheduled → pause → resume → cancel (sql_update path)
    async with sf() as s:
        async with s.begin():
            bc, _ = await bsvc.create(
                s, workspace_id=ws_id, created_by_member_id=None,
                data={"name": "future", "type": "one_time", "channel_type": "whatsapp_cloud",
                      "channel_account_id": None, "segment_id": None, "template_id": None,
                      "variable_mapping": {}, "schedule": {"send_at": "2999-01-01T00:00:00+00:00"},
                      "send_rules": {}},
            )
            bc_id = bc.id
    async with sf() as s:
        async with s.begin():
            bc = await bsvc.get(s, ws_id, bc_id)
            check(bc.status == "scheduled", f"future broadcast is scheduled (got {bc.status})")
            await bsvc.pause(s, bc)
    async with sf() as s:
        bc = await bsvc.get(s, ws_id, bc_id)
    check(bc.status == "paused", f"paused (got {bc.status})")
    async with sf() as s:
        async with s.begin():
            bc = await bsvc.get(s, ws_id, bc_id)
            await bsvc.resume(s, bc)
    async with sf() as s:
        async with s.begin():
            bc = await bsvc.get(s, ws_id, bc_id)
            await bsvc.cancel(s, bc)
    async with sf() as s:
        bc = await bsvc.get(s, ws_id, bc_id)
    check(bc.status == "cancelled", f"cancelled (got {bc.status})")

    # cleanup
    async with sf() as s:
        async with s.begin():
            await s.execute(delete(Workspace).where(Workspace.id == ws_id))
    await slsvc.invalidate_config(redis, slug)
    await close_redis()
    print("\n== RESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED")
    return 1 if FAILS else 0


async def _load(sf, link_id):
    async with sf() as s:
        return await s.get(SplitLink, link_id)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
