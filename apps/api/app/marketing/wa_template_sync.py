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
    import_missing: bool = False,
) -> int:
    """Reconcile one WhatsApp account's local templates against the provider
    (Meta Graph for whatsapp_cloud, YCloud for whatsapp_bsp). Matches by
    ``meta_template_id`` first, then (name, language). ``import_missing``
    additionally creates local rows for remote templates we don't have (used
    for BSP accounts whose templates were built in the provider console).
    Returns rows updated/created."""
    from . import ycloud_templates  # local import: avoid cycle at module load

    creds = await get_credentials(session, account)
    if account.channel_type == "whatsapp_bsp":
        api_key = creds.get("api_key", "")
        # BSP external_id is a phone number — NEVER a valid waba fallback
        waba_id = (account.config or {}).get("waba_id") or ""
        if not api_key or not waba_id:
            log.warning("wa template sync skipped account=%s (missing api_key/waba_id)", account.id)
            return 0
        raw = await ycloud_templates.fetch_ycloud_templates(
            http, waba_id=str(waba_id), api_key=api_key
        )
        remote = [ycloud_templates.normalize_remote(t) for t in raw]
    else:
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

    # This WABA's own id-set (an account UUID for our rows; the raw waba id
    # for legacy rows). A template belongs to THIS account when its
    # waba_account_id is one of these OR unset — never touch a sibling
    # account's templates that merely share a (name, language).
    account_ids = {str(account.id), str(waba_id)}
    locals_ = (
        await session.execute(
            select(MsgTemplate).where(
                MsgTemplate.workspace_id == account.workspace_id,
                MsgTemplate.channel == "whatsapp",
            )
        )
    ).scalars().all()
    mine = [t for t in locals_ if not t.waba_account_id or str(t.waba_account_id) in account_ids]
    updated = 0
    for tpl in mine:
        meta = None
        if tpl.meta_template_id and tpl.meta_template_id in by_id:
            meta = by_id[tpl.meta_template_id]
        elif (tpl.name, str(tpl.language)) in by_key:
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
        # bind an unlinked row to this account so future syncs stay scoped
        if not tpl.waba_account_id:
            tpl.waba_account_id = str(account.id)
            changed = True
        reason = meta.get("rejected_reason")
        if new_status == "rejected" and reason and tpl.rejected_reason != reason:
            tpl.rejected_reason = str(reason)
            changed = True
        if changed:
            updated += 1

    if import_missing:
        # dedupe against THIS account's rows only (a sibling account may
        # legitimately hold a same-named template on its own WABA)
        seen = {(t.name, str(t.language or "")) for t in mine}
        for t in remote:
            name = str(t.get("name") or "")
            key = (name, str(t.get("language") or ""))
            if not name or key in seen:
                continue
            components = t.get("components") or []
            if not ycloud_templates.components_representable(components):
                # media-header / OTP / unmapped-button templates can't be
                # losslessly stored in our body schema — importing them as
                # "approved" would make every send fail. Skip + log instead.
                log.warning(
                    "wa sync: skipping unrepresentable remote template %s/%s (account=%s)",
                    name, key[1], account.id,
                )
                continue
            session.add(
                MsgTemplate(
                    workspace_id=account.workspace_id,
                    channel="whatsapp",
                    name=name,
                    language=key[1] or None,
                    category=(str(t.get("category") or "").lower() or None),
                    waba_account_id=str(account.id),
                    body=ycloud_templates.components_to_body(components),
                    approval_status=map_meta_status(t.get("status")),
                    meta_template_id=str(t["id"]) if t.get("id") else None,
                    rejected_reason=(
                        str(t.get("rejected_reason"))
                        if map_meta_status(t.get("status")) == "rejected" and t.get("rejected_reason")
                        else None
                    ),
                )
            )
            seen.add(key)
            updated += 1
    return updated


async def sync_workspace(
    session: AsyncSession, *, workspace_id: uuid.UUID, http: httpx.AsyncClient | None = None
) -> int:
    accounts = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.workspace_id == workspace_id,
                ChannelAccount.channel_type.in_(("whatsapp_cloud", "whatsapp_bsp")),
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
                    ChannelAccount.channel_type.in_(("whatsapp_cloud", "whatsapp_bsp")),
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
