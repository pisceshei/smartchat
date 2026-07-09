"""Message-template CRUD (WhatsApp/Email/Messenger/SMS) + SMS signatures +
WhatsApp Meta approval sync (plan B.3 / route contract).

Route order: the specific ``/sms/signatures`` and ``/whatsapp/sync`` subpaths are
declared before the ``/{channel}/{id}`` matcher.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.creds import get_credentials
from ...db import get_session
from ...deps import MemberContext, require_permission
from ...marketing import wa_template_sync, ycloud_templates
from ...models.channels import ChannelAccount
from ...models.marketing import MsgTemplate, SmsSignature
from . import service as svc

router = APIRouter(prefix="/api/v1/msg-templates", tags=["msg_templates"])


# ==========================================================================
# schemas
# ==========================================================================
class TemplateOut(BaseModel):
    id: uuid.UUID
    channel: str
    folder: str | None
    name: str
    body: dict[str, Any]
    language: str | None
    category: str | None
    waba_account_id: str | None
    approval_status: str
    meta_template_id: str | None
    rejected_reason: str | None
    usage_count: int
    created_at: datetime
    updated_at: datetime
    segmentation: dict[str, Any] | None = None
    # whatsapp templates lift their structural sub-fields to the top level so
    # the SPA reads label/header/footer/buttons directly (body then carries
    # only {text}); other channels leave these None and keep the full body.
    label: str | None = None
    header: dict[str, Any] | None = None
    footer: dict[str, Any] | None = None
    buttons: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class SignatureIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=255)


class SignatureOut(BaseModel):
    id: uuid.UUID
    name: str
    text: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SyncIn(BaseModel):
    channel_account_id: uuid.UUID


def _out(tpl: MsgTemplate) -> TemplateOut:
    seg = None
    body = tpl.body or {}
    label = header = footer = buttons = None
    if tpl.channel == "sms":
        seg = svc.sms_segments(str(body.get("text") or "")).as_dict()
    elif tpl.channel == "whatsapp":
        # stored body = {label, header, body:{text}, footer, buttons}; the SPA
        # reads these at the top level and body as {text}
        label = body.get("label")
        header = body.get("header")
        footer = body.get("footer")
        buttons = body.get("buttons")
        body = {"text": (body.get("body") or {}).get("text", "")}
    return TemplateOut(
        id=tpl.id, channel=tpl.channel, folder=tpl.folder, name=tpl.name, body=body,
        language=tpl.language, category=tpl.category, waba_account_id=tpl.waba_account_id,
        approval_status=tpl.approval_status, meta_template_id=tpl.meta_template_id,
        rejected_reason=tpl.rejected_reason, usage_count=tpl.usage_count,
        created_at=tpl.created_at, updated_at=tpl.updated_at, segmentation=seg,
        label=label, header=header, footer=footer, buttons=buttons,
    )


def _check_channel(channel: str) -> None:
    if channel not in svc.CHANNELS:
        raise HTTPException(404, detail=f"unknown channel {channel}")


# ==========================================================================
# SMS signatures (declared before /{channel}/{id})
# ==========================================================================
@router.get("/sms/signatures", response_model=list[SignatureOut])
async def list_signatures(
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[SignatureOut]:
    rows = (
        await session.execute(
            select(SmsSignature).where(SmsSignature.workspace_id == member.workspace_id)
            .order_by(SmsSignature.created_at)
        )
    ).scalars().all()
    return [SignatureOut.model_validate(r) for r in rows]


@router.post("/sms/signatures", response_model=SignatureOut, status_code=201)
async def create_signature(
    body: SignatureIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> SignatureOut:
    row = SmsSignature(workspace_id=member.workspace_id, name=body.name, text=body.text)
    session.add(row)
    await session.commit()
    return SignatureOut.model_validate(row)


# ==========================================================================
# WhatsApp approval sync (declared before /{channel}/{id})
# ==========================================================================
@router.post("/whatsapp/sync")
async def sync_whatsapp(
    body: SyncIn,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    acct = await session.get(ChannelAccount, body.channel_account_id)
    if acct is None or acct.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="channel account not found")
    if acct.channel_type not in ("whatsapp_cloud", "whatsapp_bsp"):
        raise HTTPException(422, detail="not a whatsapp account")
    synced = await wa_template_sync.sync_account_templates(
        session,
        account=acct,
        # BSP consoles are where templates get built pre-integration — pull
        # them in so the local library reflects the WABA on first sync
        import_missing=(acct.channel_type == "whatsapp_bsp"),
    )
    await session.commit()
    return {"synced": synced}


class SubmitIn(BaseModel):
    channel_account_id: uuid.UUID | None = None


@router.post("/whatsapp/{template_id}/submit", response_model=TemplateOut)
async def submit_whatsapp(
    template_id: uuid.UUID,
    body: SubmitIn = Body(default=SubmitIn()),
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> TemplateOut:
    """Submit a locally-built WhatsApp template to the provider for Meta review
    (BSP/YCloud accounts only — Cloud API templates are created in Meta
    Business Manager). A name+language conflict ADOPTS the existing remote
    template (links its status/id) instead of erroring."""
    tpl = await _get(session, member.workspace_id, "whatsapp", template_id)
    acct: ChannelAccount | None = None
    acct_id: uuid.UUID | None = body.channel_account_id
    if acct_id is None and tpl.waba_account_id:
        try:
            acct_id = uuid.UUID(str(tpl.waba_account_id))
        except ValueError:
            # legacy rows stored the raw WABA id string, not an account UUID —
            # resolve the account whose config.waba_id matches
            acct = (
                await session.execute(
                    select(ChannelAccount).where(
                        ChannelAccount.workspace_id == member.workspace_id,
                        ChannelAccount.channel_type == "whatsapp_bsp",
                        ChannelAccount.config["waba_id"].astext == str(tpl.waba_account_id),
                    )
                )
            ).scalars().first()
    if acct is None and acct_id is None:
        raise HTTPException(422, detail="template has no linked channel account")
    if acct is None:
        acct = await session.get(ChannelAccount, acct_id)
    if acct is None or acct.workspace_id != member.workspace_id:
        raise HTTPException(404, detail="channel account not found")
    if acct.channel_type != "whatsapp_bsp":
        raise HTTPException(
            422,
            detail="submit-for-review is only supported on BSP (YCloud) accounts; "
            "Cloud API templates are created in Meta Business Manager",
        )
    creds = await get_credentials(session, acct)
    api_key = str(creds.get("api_key") or "")
    waba_id = str((acct.config or {}).get("waba_id") or "")
    if not api_key or not waba_id:
        raise HTTPException(422, detail="account is missing api_key/waba_id")
    language = tpl.language or "en"
    try:
        components = ycloud_templates.body_to_components(tpl.body or {})
        remote = await ycloud_templates.submit_template(
            None,
            api_key=api_key,
            waba_id=waba_id,
            name=tpl.name,
            language=language,
            category=(tpl.category or "marketing"),
            components=components,
        )
    except ycloud_templates.TemplateError as e:
        raise HTTPException(422, detail={"code": "invalid_template", "error": str(e)}) from e
    except ycloud_templates.TemplateSubmitConflict:
        remote = await ycloud_templates.find_template(
            None, api_key=api_key, waba_id=waba_id, name=tpl.name, language=language
        )
        if remote is None:
            raise HTTPException(
                409, detail="template name+language already exists on this WABA"
            ) from None
    except ycloud_templates.TemplateSubmitError as e:
        raise HTTPException(502, detail=f"ycloud: {e}") from e
    tpl.approval_status = wa_template_sync.map_meta_status(remote.get("status"))
    if remote.get("officialTemplateId"):
        tpl.meta_template_id = str(remote["officialTemplateId"])
    tpl.rejected_reason = (
        str(remote.get("reason"))
        if tpl.approval_status == "rejected" and remote.get("reason") not in (None, "NONE")
        else None
    )
    tpl.waba_account_id = str(acct.id)
    await session.commit()
    return _out(tpl)


# ==========================================================================
# per-channel CRUD
# ==========================================================================
@router.get("/{channel}", response_model=list[TemplateOut])
async def list_templates(
    channel: str,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[TemplateOut]:
    _check_channel(channel)
    rows = (
        await session.execute(
            select(MsgTemplate).where(
                MsgTemplate.workspace_id == member.workspace_id, MsgTemplate.channel == channel
            ).order_by(MsgTemplate.created_at.desc())
        )
    ).scalars().all()
    return [_out(t) for t in rows]


@router.post("/{channel}", response_model=TemplateOut, status_code=201)
async def create_template(
    channel: str,
    body: dict[str, Any] = Body(...),
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> TemplateOut:
    _check_channel(channel)
    try:
        cols = svc.validate_and_extract(channel, body)
    except svc.TemplateError as e:
        raise HTTPException(422, detail={"code": "invalid_template", "error": str(e)}) from e
    tpl = MsgTemplate(
        workspace_id=member.workspace_id, channel=channel, name=cols.name, folder=cols.folder,
        body=cols.body, language=cols.language, category=cols.category,
        waba_account_id=cols.waba_account_id, approval_status=cols.approval_status,
    )
    session.add(tpl)
    await session.commit()
    return _out(tpl)


async def _get(session: AsyncSession, workspace_id: uuid.UUID, channel: str, tid: uuid.UUID) -> MsgTemplate:
    tpl = await session.get(MsgTemplate, tid)
    if tpl is None or tpl.workspace_id != workspace_id or tpl.channel != channel:
        raise HTTPException(404, detail="template not found")
    return tpl


@router.get("/{channel}/{template_id}", response_model=TemplateOut)
async def get_template(
    channel: str,
    template_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> TemplateOut:
    _check_channel(channel)
    return _out(await _get(session, member.workspace_id, channel, template_id))


@router.patch("/{channel}/{template_id}", response_model=TemplateOut)
async def update_template(
    channel: str,
    template_id: uuid.UUID,
    body: dict[str, Any] = Body(...),
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> TemplateOut:
    _check_channel(channel)
    tpl = await _get(session, member.workspace_id, channel, template_id)
    # merge onto the current structural payload then re-validate the whole thing
    merged = {"name": tpl.name, "folder": tpl.folder, "language": tpl.language,
              "category": tpl.category, "waba_account_id": tpl.waba_account_id, **(tpl.body or {})}
    merged.update(body)
    try:
        cols = svc.validate_and_extract(channel, merged)
    except svc.TemplateError as e:
        raise HTTPException(422, detail={"code": "invalid_template", "error": str(e)}) from e
    tpl.name = cols.name
    tpl.folder = cols.folder
    tpl.body = cols.body
    tpl.language = cols.language
    tpl.category = cols.category
    tpl.waba_account_id = cols.waba_account_id
    if channel == "whatsapp":
        # any edit resets Meta review; a re-sync repopulates the real status
        tpl.approval_status = "draft"
        tpl.meta_template_id = None
        tpl.rejected_reason = None
    await session.commit()
    return _out(tpl)


@router.delete("/{channel}/{template_id}", status_code=204)
async def delete_template(
    channel: str,
    template_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("broadcasts.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    _check_channel(channel)
    tpl = await _get(session, member.workspace_id, channel, template_id)
    await session.delete(tpl)
    await session.commit()
