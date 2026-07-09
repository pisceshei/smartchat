"""Admin channel-account + widget management API.

Frontend contract (apps/web/src/api/endpoints.ts):
  GET    /api/v1/channels/accounts
  POST   /api/v1/channels/{channel_type}/accounts
  DELETE /api/v1/channels/accounts/{id}
  GET    /api/v1/widgets            POST /api/v1/widgets
  GET    /api/v1/widgets/{id}       PATCH/DELETE /api/v1/widgets/{id}

Credentials are envelope-encrypted at rest (channels.creds); responses never
echo secrets back. Connect performs a live credential validation through the
adapter before persisting, and registers provider webhooks where the
platform requires per-account URLs (telegram).
"""
from __future__ import annotations

import secrets
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...channels.creds import get_credentials, set_credentials
from ...channels.registry import get_adapter, registered_channel_types
from ...db import get_session
from ...deps import MemberContext, require_permission
from ...models.channels import ChannelAccount, Widget
from ...models.misc import AuditLog
from ...services.quotas import effective_limits
from ...services.redis_client import get_redis
from ...settings import get_settings

router = APIRouter(prefix="/api/v1", tags=["channels"])

# Frontend-facing channel names → canonical adapter channel_type. The gallery
# uses whatsapp_api/telegram_bot; adapters register as whatsapp_cloud/telegram_bot.
_CHANNEL_ALIASES = {
    "whatsapp_api": "whatsapp_cloud",
    "telegram": "telegram_bot",
}

# WhatsApp App / LINE App pair by QR scan (whatsmeow bridge), NOT a token form.
# They share the connect path but dispatch to modules.devices.service.
_BRIDGE_CHANNELS = frozenset({"whatsapp_app", "line_app"})

_CONNECTABLE = {
    "widget",
    "telegram_bot",
    "whatsapp_cloud",
    "messenger",
    "instagram",
    "line_oa",
    "email",
    "whatsapp_bsp",
    # QR-scan device bridges (routed to the devices QR flow, not the token branch)
    "whatsapp_app",
    "line_app",
    # Phase 4 — validated through adapter.connect_validate (see _DISPATCH_TYPES).
    "slack",
    "vk",
    "wechat_kf",
    "wecom",
    "tiktok_business",
    "youtube",
    "zalo_app",
}

# Phase 4 channels whose connect-time validation is delegated to the adapter's
# connect_validate() rather than an inline branch here. Their adapter must be
# registered (registry auto-discovery) before connect will accept them.
_DISPATCH_TYPES = frozenset(
    {"slack", "vk", "wechat_kf", "wecom", "tiktok_business", "youtube", "zalo_app", "whatsapp_bsp"}
)

# Per-account webhook path segment for channels that carry a path secret; used
# to surface the full webhook URL in the connect response when the adapter asks
# for one (needs_webhook_secret). Slack (app-level URL) and YouTube (polling)
# are intentionally absent.
_HOOK_PATH = {
    "line_oa": "line",
    "vk": "vk",
    "wechat_kf": "wechat",
    "wecom": "wechat",  # WeCom shares /hooks/wechat/{secret}, routed by channel_type
    "zalo_app": "zalo",
    "tiktok_business": "tiktok",
}

def _serialize_account(acct: ChannelAccount) -> dict[str, Any]:
    return {
        "id": str(acct.id),
        "channel_type": acct.channel_type,
        "name": acct.name,
        # the SPA types/pages read display_name (ChannelsPage manage drawer,
        # BroadcastWizard account picker, TemplatesPage WABA selector)
        "display_name": acct.name,
        "external_id": acct.external_id,
        "status": acct.status,
        "health": acct.health or {},
        "enabled": acct.enabled,
        "config": {k: v for k, v in (acct.config or {}).items() if "password" not in k},
        "created_at": acct.created_at.isoformat() if acct.created_at else None,
    }


# ------------------------------------------------------------------ accounts
@router.get("/channels/accounts")
async def list_accounts(
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(ChannelAccount)
                .where(
                    ChannelAccount.workspace_id == member.workspace.id,
                    ChannelAccount.enabled == True,  # noqa: E712 — soft-deleted stay hidden
                )
                .order_by(ChannelAccount.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_serialize_account(a) for a in rows]


class ConnectBody(BaseModel):
    """Accepts EITHER a flat form body ({bot_token, phone_number_id, ...} — what
    every connect modal POSTs) OR the legacy nested {credentials, config} shape.
    Extra fields are captured and split by _normalize_connect_body()."""

    model_config = {"extra": "allow"}

    name: str = Field(default="", max_length=128)
    credentials: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    external_id: str | None = None


# keys whose presence marks a value as a secret → envelope-encrypted credentials.
_SECRET_HINTS = ("token", "secret", "password", "key", "api_key")
_NON_SECRET = frozenset({"name", "external_id", "credentials", "config"})

# OAuth2 token endpoints for well-known email providers; a "custom" provider
# must supply oauth_token_endpoint explicitly (used by refresh_credentials).
_EMAIL_OAUTH_ENDPOINTS = {
    "gmail": "https://oauth2.googleapis.com/token",
    "outlook": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}


def _normalize_connect_body(body: ConnectBody) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (credentials, config). Nested body passes through; a flat body is
    split — secret-ish keys → credentials (encrypted at rest), the rest → config.
    Drift-tolerant: adapters receive a merged view for validation, so a field the
    UI names slightly differently than the adapter expects still resolves."""
    if body.credentials is not None or body.config is not None:
        return (
            {k: v for k, v in (body.credentials or {}).items() if v is not None},
            dict(body.config or {}),
        )
    extra = getattr(body, "model_extra", None) or {}
    flat = {k: v for k, v in extra.items() if k not in _NON_SECRET and v is not None}
    credentials = {k: v for k, v in flat.items() if any(h in k.lower() for h in _SECRET_HINTS)}
    config = {k: v for k, v in flat.items() if k not in credentials}
    return credentials, config


async def _check_channel_quota(
    session: AsyncSession, member: MemberContext, channel_type: str
) -> None:
    """Bridge channels (QR-scan hosted devices) count against hosted_devices;
    every other non-widget channel counts against official_channels. Only
    enabled accounts of the matching category occupy a seat."""
    limits = await effective_limits(session, get_redis(), member.workspace.id)
    is_bridge = channel_type in _BRIDGE_CHANNELS
    cap = limits.get("hosted_devices" if is_bridge else "official_channels")
    if cap is None or (isinstance(cap, (int, float)) and cap < 0):
        return
    category = (
        ChannelAccount.channel_type.in_(_BRIDGE_CHANNELS)
        if is_bridge
        else ChannelAccount.channel_type.notin_({*_BRIDGE_CHANNELS, "widget"})
    )
    count = (
        await session.execute(
            select(func.count())
            .select_from(ChannelAccount)
            .where(
                ChannelAccount.workspace_id == member.workspace.id,
                ChannelAccount.enabled == True,  # noqa: E712
                category,
            )
        )
    ).scalar_one()
    if count >= int(cap):
        raise HTTPException(402, "channel account quota reached — upgrade your plan")


class BspPreviewBody(BaseModel):
    api_key: str = Field(min_length=8)
    bsp: str = "ycloud"


@router.post("/channels/whatsapp_bsp/preview-numbers")
async def preview_bsp_numbers(
    body: BspPreviewBody,
    member: MemberContext = Depends(require_permission("channels.manage")),
) -> list[dict[str, Any]]:
    """List the BSP account's WhatsApp numbers for the connect modal's picker.
    No DB writes; the key is only used for this call."""
    if body.bsp != "ycloud":
        raise HTTPException(422, f"BSP '{body.bsp}' not implemented — only ycloud")
    adapter = get_adapter("whatsapp_bsp")
    try:
        return await adapter.list_phone_numbers(body.api_key)
    except ValueError as e:
        raise HTTPException(422, f"ycloud: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ycloud: {e}") from e


@router.post("/channels/{channel_type}/accounts")
async def connect_account(
    channel_type: str,
    body: ConnectBody,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    channel_type = _CHANNEL_ALIASES.get(channel_type, channel_type)
    if channel_type not in _CONNECTABLE or channel_type not in registered_channel_types():
        raise HTTPException(422, f"unsupported channel type: {channel_type}")
    await _check_channel_quota(session, member, channel_type)

    # QR-scan device bridges (whatsapp_app / line_app): no token form — create the
    # account + device_bridge and start whatsmeow QR login. Returns awaiting_qr
    # (or pending + a surfaced error when the bridge is offline/unconfigured).
    if channel_type in _BRIDGE_CHANNELS:
        from ..devices import service as device_service

        return await device_service.provision_device(
            session, member, channel_type, name=body.name.strip()
        )

    settings = get_settings()
    webhook_secret = secrets.token_urlsafe(24)
    credentials, config = _normalize_connect_body(body)
    # WhatsApp card posts channel_type=whatsapp_api for both direct Cloud API and
    # BSP proxies; a non-cloud bsp selector routes to the whatsapp_bsp adapter.
    if channel_type == "whatsapp_cloud" and str(config.get("bsp", "")).lower() not in (
        "", "cloud", "direct",
    ):
        channel_type = "whatsapp_bsp"
    external_id = (body.external_id or "").strip()
    name = body.name.strip()
    health_detail: dict[str, Any] = {}
    needs_hook_secret = False
    dispatch_status: str | None = None

    if channel_type == "widget":
        raise HTTPException(422, "create widgets via POST /api/v1/widgets")

    adapter = get_adapter(channel_type)

    if channel_type == "telegram_bot":
        token = credentials.get("bot_token", "")
        if not token:
            raise HTTPException(422, "bot_token required")
        try:
            me = await adapter.validate_token(token)
        except ValueError as e:
            raise HTTPException(422, f"telegram: {e}") from e
        except httpx.HTTPError as e:
            raise HTTPException(422, f"telegram: 網路連線失敗 {e}") from e
        external_id = str(me["id"])
        name = name or (me.get("username") or f"bot {external_id}")
        health_detail = {"username": me.get("username")}
        # duplicate check BEFORE setWebhook: connecting an already-active bot
        # must 409 with NO side effects — calling setWebhook first used to
        # rotate Telegram's registered secret to one that was never persisted,
        # silently black-holing all inbound (the hooks router 200s unmatched
        # secrets so Telegram never retries).
        dup_early = (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.channel_type == channel_type,
                    ChannelAccount.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        if dup_early is not None and (
            dup_early.enabled or dup_early.workspace_id != member.workspace.id
        ):
            raise HTTPException(409, "this account is already connected")
        hook_url = f"{settings.public_base_url}/hooks/telegram/{webhook_secret}"
        if not await adapter.set_webhook(token, hook_url, webhook_secret):
            raise HTTPException(502, "telegram setWebhook failed")
    elif channel_type == "whatsapp_cloud":
        if not credentials.get("access_token") or not config.get("phone_number_id"):
            raise HTTPException(422, "access_token and phone_number_id required")
        external_id = str(config["phone_number_id"])
    elif channel_type == "messenger":
        if not credentials.get("page_access_token") or not (external_id or config.get("page_id")):
            raise HTTPException(422, "page_access_token and page_id required")
        external_id = external_id or str(config.get("page_id"))
        # adapters read access_token; keep page_access_token as the original key too
        if not credentials.get("access_token"):
            credentials["access_token"] = credentials["page_access_token"]
        # subscribe our app to the Page so inbound flows to /hooks/meta without a
        # manual step in the Meta dashboard (best-effort; reported via health).
        health_detail["subscribed"] = await adapter.subscribe_page(
            credentials["access_token"], external_id
        )
    elif channel_type == "instagram":
        # Two connect modes: via-Page (page_access_token + page_id) or IG-Login
        # (access_token + ig_user_id — the modal's login_type=ig form).
        if credentials.get("page_access_token") and (external_id or config.get("page_id")):
            page_id = external_id or str(config.get("page_id"))
            if not credentials.get("access_token"):
                credentials["access_token"] = credentials["page_access_token"]
            # IG messaging webhooks arrive under entry.id = the linked IG Business
            # account id, NOT the page id — resolve + store it as external_id so
            # /hooks/meta routing matches (else all inbound is dropped). Fall back
            # to page_id when the link can't be resolved (still lets connect land).
            ig_id = await adapter.resolve_ig_account(credentials["access_token"], page_id)
            external_id = ig_id or page_id
            config["page_id"] = page_id  # kept for send (via-Page uses /me/messages)
            health_detail["subscribed"] = await adapter.subscribe_page(
                credentials["access_token"], page_id
            )
            if ig_id is None:
                health_detail["ig_link"] = "unresolved"
        elif credentials.get("access_token") and (external_id or config.get("ig_user_id")):
            external_id = external_id or str(config.get("ig_user_id"))
            config["ig_login"] = True  # send path: graph.instagram.com (adapter)
        else:
            missing = [
                k
                for k, v in (
                    ("page_access_token", credentials.get("page_access_token")),
                    ("page_id", config.get("page_id")),
                    ("access_token", credentials.get("access_token")),
                    ("ig_user_id", config.get("ig_user_id")),
                )
                if not v
            ]
            raise HTTPException(
                422,
                "instagram: provide page_access_token+page_id (via Page) or "
                f"access_token+ig_user_id (Instagram login); missing: {', '.join(missing)}",
            )
    elif channel_type == "line_oa":
        access_token = credentials.get("channel_access_token") or credentials.get("access_token")
        if not credentials.get("channel_secret") or not access_token:
            raise HTTPException(422, "channel_secret and channel_access_token required")
        external_id = external_id or str(config.get("channel_id") or "")
        if not external_id:
            raise HTTPException(422, "channel_id required")
        # duplicate check BEFORE set_webhook: LINE has a SINGLE registered
        # endpoint per channel, and set_webhook rotates it to a freshly generated
        # secret. If a later 409 aborts the commit, that secret is never
        # persisted and all inbound is silently black-holed (the hooks router
        # 200s unmatched secrets so LINE never retries) — the same trap the
        # telegram branch guards. A disabled same-workspace row still reactivates
        # via the global dup handling below (and re-registers correctly).
        dup_early = (
            await session.execute(
                select(ChannelAccount).where(
                    ChannelAccount.channel_type == channel_type,
                    ChannelAccount.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        if dup_early is not None and (
            dup_early.enabled or dup_early.workspace_id != member.workspace.id
        ):
            raise HTTPException(409, "this account is already connected")
        # adapters (send/check_health/fetch_media) read access_token; the modal
        # posts channel_access_token — mirror it so outbound + health work.
        credentials["access_token"] = access_token
        # auto-register our per-account inbound endpoint on the LINE channel so
        # the operator need not paste it into the console (best-effort; a failure
        # is reported via health, not a hard error — some tokens lack the scope).
        hook_url = f"{settings.public_base_url}/hooks/line/{webhook_secret}"
        health_detail["webhook"] = (
            "registered" if await adapter.set_webhook(access_token, hook_url) else "manual"
        )
        needs_hook_secret = True
    elif channel_type == "email":
        # The email adapter reads ALL connection settings (host/port/ssl/user/
        # password/oauth_*) from the ENCRYPTED credentials, but the flat modal
        # body splits by secret-hint — so host/port/user/auth_type would land in
        # config and never reach the adapter. Remap the merged body to the exact
        # credential keys the adapter expects (imap_user/imap_password/smtp_* …).
        merged = {**config, **credentials}
        for k in ("imap_host", "smtp_host", "address"):
            if not merged.get(k):
                raise HTTPException(422, f"{k} required")
        address = str(merged["address"]).lower()
        external_id = address
        auth_type = str(merged.get("auth_type") or "password")
        username = merged.get("username") or merged.get("imap_user") or address
        email_creds: dict[str, Any] = {
            "auth_type": auth_type,
            "email": address,
            "imap_host": merged.get("imap_host"),
            "imap_port": merged.get("imap_port") or 993,
            "imap_ssl": merged.get("imap_ssl", True),
            "smtp_host": merged.get("smtp_host"),
            "smtp_port": merged.get("smtp_port") or 465,
            # adapter reads smtp_tls (STARTTLS gate); modal sends smtp_ssl
            "smtp_tls": merged.get("smtp_ssl", True),
            "imap_user": username,
            "smtp_user": merged.get("smtp_user") or username,
        }
        if auth_type == "oauth2":
            for key in (
                "oauth_access_token", "oauth_refresh_token", "oauth_token_endpoint",
                "oauth_client_id", "oauth_client_secret", "oauth_user",
            ):
                if merged.get(key) is not None:
                    email_creds[key] = merged[key]
            endpoint = email_creds.get("oauth_token_endpoint") or _EMAIL_OAUTH_ENDPOINTS.get(
                str(merged.get("oauth_provider") or "").lower()
            )
            if endpoint:
                email_creds["oauth_token_endpoint"] = endpoint
        else:
            pwd = merged.get("password") or merged.get("imap_password") or ""
            email_creds["imap_password"] = pwd
            email_creds["smtp_password"] = merged.get("smtp_password") or pwd
        credentials = {k: v for k, v in email_creds.items() if v is not None}
        # config keeps only non-secret display fields (never secrets in JSONB)
        config = {"address": address, "auth_type": auth_type}
        if merged.get("oauth_provider"):
            config["oauth_provider"] = merged["oauth_provider"]
    elif channel_type in _DISPATCH_TYPES:
        # Phase 4: the adapter authenticates, resolves the provider account id,
        # registers the webhook and reports health. Pass a MERGED view so an
        # adapter that expects e.g. corp_id in config and secret in credentials
        # finds both regardless of how the flat body was bucketed.
        merged = {**config, **credentials}
        cr = await adapter.connect_validate(merged, merged)
        if not cr.health.ok and cr.health.status not in ("active", "pending"):
            detail = cr.health.detail or {}
            raise HTTPException(422, f"{channel_type}: {detail.get('error') or 'validation failed'}")
        external_id = (cr.external_id or external_id).strip()
        if not external_id:
            raise HTTPException(422, f"{channel_type}: could not determine account id")
        name = name or cr.name
        if cr.config_patch:
            config = {**config, **cr.config_patch}
        if cr.credentials_patch:
            # connect-time secrets (e.g. a provider webhook-endpoint secret)
            # join the envelope-encrypted credentials, never the JSONB config
            credentials = {**credentials, **cr.credentials_patch}
        health_detail = {**health_detail, **cr.health.detail}
        needs_hook_secret = cr.needs_webhook_secret
        dispatch_status = "active" if cr.health.ok else cr.health.status

        # YCloud: the webhook-endpoint secret is only disclosed once, at CREATE.
        # When the endpoint pre-exists we must recover it, or the hook degrades
        # to permanent unsigned-accept. Search ANY whatsapp_bsp account with
        # the same api_key — enabled OR disabled, across workspaces (the secret
        # is a property of the YCloud account, shared by all its numbers). This
        # covers: a 2nd number on the same key, and reconnecting a soft-deleted
        # account (whose own disabled row still holds the secret).
        if channel_type == "whatsapp_bsp" and not credentials.get("webhook_secret"):
            sibs = (
                await session.execute(
                    select(ChannelAccount).where(
                        ChannelAccount.channel_type == "whatsapp_bsp",
                    )
                )
            ).scalars().all()
            for sib in sibs:
                sc = await get_credentials(session, sib)
                if sc.get("api_key") == credentials.get("api_key") and sc.get("webhook_secret"):
                    credentials["webhook_secret"] = sc["webhook_secret"]
                    health_detail["webhook"] = "registered"
                    break

    # (channel_type, external_id) is globally UNIQUE (webhook routing key). A row
    # may still exist but be disabled from a previous "remove" (soft-delete) —
    # reconnecting the same account must REACTIVATE that row, not 409, otherwise
    # a deleted Telegram/LINE/etc. account can never be re-added.
    dup = (
        await session.execute(
            select(ChannelAccount).where(
                ChannelAccount.channel_type == channel_type,
                ChannelAccount.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if dup is not None and (dup.enabled or dup.workspace_id != member.workspace.id):
        raise HTTPException(409, "this account is already connected")

    if dup is not None:
        acct = dup  # reactivate the previously-removed account
        acct.name = name or acct.name or channel_type
        acct.config = config
        acct.webhook_secret = webhook_secret
        acct.enabled = True
        acct.status = "active"
        acct.health = health_detail
    else:
        acct = ChannelAccount(
            workspace_id=member.workspace.id,
            channel_type=channel_type,
            name=name or channel_type,
            external_id=external_id,
            config=config,
            webhook_secret=webhook_secret,
            status="active",
            health=health_detail,
        )
        session.add(acct)
    await session.flush()
    if credentials:
        await set_credentials(session, acct, credentials)

    # live health probe (best effort; failures surface as status). Phase 4
    # dispatch channels already validated in connect_validate — trust that
    # result instead of re-probing (avoids a second network round-trip).
    if dispatch_status is not None:
        acct.status = dispatch_status
        acct.health = health_detail
    else:
        try:
            result = await adapter.check_health(acct, credentials)
            acct.status = result.status if not result.ok else "active"
            acct.health = {**health_detail, **result.detail}
        except Exception:  # noqa: BLE001 — probe must not block connect
            acct.health = {**health_detail, "probe": "failed"}

    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="channel.connect",
            target_type="channel_account",
            target_id=str(acct.id),
            detail={"channel_type": channel_type, "external_id": external_id},
        )
    )
    await session.commit()
    out = _serialize_account(acct)
    # surface the per-account webhook URL+secret once, so the operator can paste
    # it into the provider console (VK/WeChat/Zalo/TikTok path-secret channels).
    if needs_hook_secret and channel_type in _HOOK_PATH:
        out["webhook_secret"] = acct.webhook_secret
        out["webhook_url"] = (
            f"{settings.public_base_url}/hooks/{_HOOK_PATH[channel_type]}/{acct.webhook_secret}"
        )
    # YCloud: auto-registration failed or the endpoint pre-existed with no
    # recoverable secret — tell the operator to finish setup in the console
    # (and optionally re-connect pasting the endpoint secret).
    if (
        channel_type == "whatsapp_bsp"
        and health_detail.get("webhook") in ("manual", "existing")
        and not credentials.get("webhook_secret")
    ):
        out["webhook_manual"] = True
        out["webhook_url"] = (
            health_detail.get("webhook_url") or f"{settings.public_base_url}/hooks/ycloud"
        )
    return out


@router.delete("/channels/accounts/{account_id}", status_code=204)
async def remove_account(
    account_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    acct = await session.get(ChannelAccount, account_id)
    if acct is None or acct.workspace_id != member.workspace.id:
        raise HTTPException(404, "account not found")
    # QR device bridges: best-effort stop + remove the whatsmeow session so the
    # Go process frees it (never blocks the disable if the bridge is down).
    if acct.channel_type in _BRIDGE_CHANNELS:
        from ..devices import service as device_service

        await device_service.teardown_device(session, acct)
    acct.enabled = False
    acct.status = "disconnected"
    # widget-type account: disable the linked Widget row too, so the widget
    # list and the account list stay consistent (both filter enabled==true)
    if acct.channel_type == "widget":
        linked = (
            await session.execute(select(Widget).where(Widget.channel_account_id == acct.id))
        ).scalars().first()
        if linked is not None:
            linked.enabled = False
    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="channel.disconnect",
            target_type="channel_account",
            target_id=str(acct.id),
            detail={"channel_type": acct.channel_type},
        )
    )
    await session.commit()


# ------------------------------------------------------------------- widgets
def _serialize_widget(w: Widget) -> dict[str, Any]:
    return {
        "id": str(w.id),
        "widget_key": w.widget_key,
        "name": w.name,
        "config": w.config or {},
        "allowed_domains": w.allowed_domains or [],
        "brand_removed": w.brand_removed,
        "enabled": w.enabled,
        "channel_account_id": str(w.channel_account_id) if w.channel_account_id else None,
        "embed_script_url": f"{get_settings().assets_base_url}/js/project_{w.widget_key}.js",
        "created_at": w.created_at.isoformat() if w.created_at else None,
    }


@router.get("/widgets")
async def list_widgets(
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(Widget)
                .where(
                    Widget.workspace_id == member.workspace.id,
                    Widget.enabled == True,  # noqa: E712 — soft-deleted stay hidden
                )
                .order_by(Widget.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_serialize_widget(w) for w in rows]


class WidgetCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    domain: str | None = None


@router.post("/widgets")
async def create_widget(
    body: WidgetCreateBody,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    limits = await effective_limits(session, get_redis(), member.workspace.id)
    cap = limits.get("widgets")
    if cap is not None:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Widget)
                .where(
                    Widget.workspace_id == member.workspace.id,
                    Widget.enabled == True,  # noqa: E712 — soft-deleted don't hold a seat
                )
            )
        ).scalar_one()
        if count >= int(cap):
            raise HTTPException(402, "widget quota reached — upgrade your plan")

    widget_key = secrets.token_hex(8)
    acct = ChannelAccount(
        workspace_id=member.workspace.id,
        channel_type="widget",
        name=body.name,
        external_id=widget_key,
        webhook_secret=secrets.token_urlsafe(24),
        status="active",
    )
    session.add(acct)
    await session.flush()
    widget = Widget(
        workspace_id=member.workspace.id,
        channel_account_id=acct.id,
        widget_key=widget_key,
        name=body.name,
        config={"brand": {"name": body.name}, "home": {"enabled": True}},
        allowed_domains=[body.domain] if body.domain else [],
    )
    session.add(widget)
    session.add(
        AuditLog(
            workspace_id=member.workspace.id,
            actor_type="member",
            actor_id=member.member.id,
            action="widget.create",
            target_type="widget",
            target_id=widget_key,
            detail={"name": body.name},
        )
    )
    await session.commit()
    return _serialize_widget(widget)


@router.get("/widgets/{widget_id}")
async def get_widget(
    widget_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    w = await session.get(Widget, widget_id)
    if w is None or w.workspace_id != member.workspace.id:
        raise HTTPException(404, "widget not found")
    return _serialize_widget(w)


class WidgetPatchBody(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    config: dict[str, Any] | None = None
    allowed_domains: list[str] | None = None
    brand_removed: bool | None = None
    enabled: bool | None = None


@router.patch("/widgets/{widget_id}")
async def update_widget(
    widget_id: uuid.UUID,
    body: WidgetPatchBody,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    w = await session.get(Widget, widget_id)
    if w is None or w.workspace_id != member.workspace.id:
        raise HTTPException(404, "widget not found")
    if body.brand_removed is not None and body.brand_removed:
        limits = await effective_limits(session, get_redis(), member.workspace.id)
        if not limits.get("brand_removal", False):
            raise HTTPException(402, "brand removal requires Pro plan or above")
        w.brand_removed = True
    elif body.brand_removed is not None:
        w.brand_removed = False
    if body.name is not None:
        w.name = body.name
    if body.config is not None:
        w.config = body.config
    if body.allowed_domains is not None:
        w.allowed_domains = body.allowed_domains
    if body.enabled is not None:
        w.enabled = body.enabled
    await session.commit()
    return _serialize_widget(w)


@router.delete("/widgets/{widget_id}", status_code=204)
async def delete_widget(
    widget_id: uuid.UUID,
    member: MemberContext = Depends(require_permission("channels.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    w = await session.get(Widget, widget_id)
    if w is None or w.workspace_id != member.workspace.id:
        raise HTTPException(404, "widget not found")
    w.enabled = False
    if w.channel_account_id:
        acct = await session.get(ChannelAccount, w.channel_account_id)
        if acct is not None:
            acct.enabled = False
    await session.commit()
