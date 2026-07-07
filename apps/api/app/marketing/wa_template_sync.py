"""WhatsApp template Meta-approval sync (plan B.3: 審核狀態 webhook + 6h 對帳輪詢).

Pulls the Graph ``{waba_id}/message_templates`` list for each WhatsApp channel
account, maps the Meta review status onto our ``approval_status``, and writes
it back (plus ``meta_template_id`` / ``rejected_reason``). A 6-hour cron runs
the reconcile across all workspaces; the module status-map is pure and tested.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..channels.adapters.whatsapp_cloud import GRAPH_BASE
from ..channels.creds import get_credentials
from ..models.channels import ChannelAccount
from ..models.marketing import MsgTemplate

log = logging.getLogger("smartchat.marketing.wa_sync")

# Meta review status → our approval_status (msg_templates.approval_status)
_STATUS_MAP = {
    "APPROVED": "approved",
    "PENDING": "pending",
    "IN_APPEAL": "pending",
    "PENDING_DELETION": "pending",
    "REJECTED": "rejected",
    "PAUSED": "paused",
    "DISABLED": "disabled",
    "DELETED": "disabled",
    "FLAGGED": "paused",
    "LIMIT_EXCEEDED": "paused",
}


def map_meta_status(meta_status: str | None) -> str:
    return _STATUS_MAP.get(str(meta_status or "").upper(), "pending")


async def fetch_meta_templates(
    http: httpx.AsyncClient, *, waba_id: str, token: str
) -> list[dict[str, Any]]:
    """GET the WABA's message templates. Returns [] on any transport/auth error
    (the reconcile is best-effort and retried every 6h)."""
    out: list[dict[str, Any]] = []
    url: str | None = f"{GRAPH_BASE}/{waba_id}/message_templates"
    params: dict[str, Any] | None = {
        "fields": "name,status,category,language,id,rejected_reason",
        "limit": 200,
        "access_token": token,
    }
    for _ in range(10):  # follow paging, bounded
        if url is None:
            break
        try:
            r = await http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            break
        out.extend(data.get("data") or [])
        url = ((data.get("paging") or {}).get("next"))
        params = None  # `next` is a full URL
    return out


def _waba_id_for(account: ChannelAccount, template: MsgTemplate | None = None) -> str | None:
    if template is not None and template.waba_account_id:
        return template.waba_account_id
    cfg = account.config or {}
    return cfg.get("waba_id") or cfg.get("waba_account_id") or account.external_id


async def sync_account_templates(
    session: AsyncSession,
    *,
    account: ChannelAccount,
    http: httpx.AsyncClient | None = None,
) -> int:
    """Reconcile one WhatsApp account's local templates against Meta. Matches by
    ``meta_template_id`` first, then (name, language). Returns rows updated."""
    creds = await get_credentials(session, account)
    token = creds.get("access_token", "")
    waba_id = _waba_id_for(account)
    if not token or not waba_id:
        return 0
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
    close = http is None
    try:
        remote = await fetch_meta_templates(client, waba_id=waba_id, token=token)
    finally:
        if close:
            await client.aclose()
    by_id = {str(t.get("id")): t for t in remote if t.get("id")}
    by_key = {(str(t.get("name")), str(t.get("language"))): t for t in remote}

    locals_ = (
        await session.execute(
            select(MsgTemplate).where(
                MsgTemplate.workspace_id == account.workspace_id,
                MsgTemplate.channel == "whatsapp",
            )
        )
    ).scalars().all()
    updated = 0
    for tpl in locals_:
        meta = None
        if tpl.meta_template_id and tpl.meta_template_id in by_id:
            meta = by_id[tpl.meta_template_id]
        elif (tpl.name, tpl.language) in by_key:
            meta = by_key[(tpl.name, str(tpl.language))]
        if meta is None:
            continue
        new_status = map_meta_status(meta.get("status"))
        changed = False
        if tpl.approval_status != new_status:
            tpl.approval_status = new_status
            changed = True
        if meta.get("id") and tpl.meta_template_id != str(meta["id"]):
            tpl.meta_template_id = str(meta["id"])
            changed = True
        reason = meta.get("rejected_reason")
        if new_status == "rejected" and reason and tpl.rejected_reason != reason:
            tpl.rejected_reason = str(reason)
            changed = True
        if changed:
            updated += 1
    return updated


async def sync_workspace(
    session: AsyncSession, *, workspace_id: uuid.UUID, http: httpx.AsyncClient | None = None
) -> int:
    accounts = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.workspace_id == workspace_id,
                ChannelAccount.channel_type == "whatsapp_cloud",
                ChannelAccount.enabled.is_(True),
            )
        )
    ).scalars().all()
    total = 0
    for acct in accounts:
        try:
            total += await sync_account_templates(session, account=acct, http=http)
        except Exception:  # noqa: BLE001 — one bad account must not stop the sweep
            log.exception("wa template sync failed account=%s", acct.id)
    return total


async def reconcile_all(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """6-hour cron: reconcile every enabled WhatsApp account across all
    workspaces."""
    async with session_factory() as session:
        ids = (
            await session.execute(
                select(ChannelAccount.id).where(
                    ChannelAccount.channel_type == "whatsapp_cloud",
                    ChannelAccount.enabled.is_(True),
                )
            )
        ).scalars().all()
    total = 0
    for acct_id in ids:
        async with session_factory() as session:
            async with session.begin():
                acct = await session.get(ChannelAccount, acct_id)
                if acct is None:
                    continue
                try:
                    total += await sync_account_templates(session, account=acct)
                except Exception:  # noqa: BLE001
                    log.exception("wa reconcile failed account=%s", acct_id)
    return total
