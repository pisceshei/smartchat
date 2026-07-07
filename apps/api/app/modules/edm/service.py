"""EDM (第三方代發) service + email-sender adapter family (plan B.3).

One UI / segment / template surface, two delivery backends: the native
broadcast pipeline (see broadcasts) and — here — a third-party ESP. The adapter
family (smtp / ses / sendgrid / edm_provider) exports the compiled HTML +
recipient list to the provider and polls delivery/open/click stats back onto the
``edm_campaigns`` counters.

The concrete SMTP adapter attempts a real send via ``aiosmtplib`` when SMTP
config is present; the hosted-ESP adapters are structured skeletons (accept the
batch, expose a provider ref, and report stats) so the flow is end-to-end
without vendor credentials in dev/test.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...models.contacts import Contact
from ...models.marketing import EdmCampaign, MsgTemplate, Segment
from ..msg_templates import service as tpl_svc

log = logging.getLogger("smartchat.edm")
PROVIDERS = ("smtp", "ses", "sendgrid", "edm_provider")
AUDIENCE_BATCH = 5000


class EdmError(ValueError):
    def __init__(self, detail: str, code: str = "invalid"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


@dataclass
class Recipient:
    contact_id: uuid.UUID
    email: str
    contact: Contact


@dataclass
class ExportResult:
    accepted: int
    provider_ref: str
    rejected: int = 0


@dataclass
class StatsResult:
    delivered: int = 0
    opened: int = 0
    clicked: int = 0


# --------------------------------------------------------------------------
# adapter family
# --------------------------------------------------------------------------
class EmailSenderAdapter:
    provider = "base"

    async def export(
        self, *, campaign: EdmCampaign, subject: str, html: str, recipients: list[Recipient],
        config: dict[str, Any],
    ) -> ExportResult:  # pragma: no cover - overridden
        raise NotImplementedError

    async def poll(self, *, campaign: EdmCampaign, config: dict[str, Any]) -> StatsResult:
        # hosted ESPs report async; the skeleton assumes full delivery once sent.
        return StatsResult(delivered=campaign.sent_count, opened=0, clicked=0)


class SmtpAdapter(EmailSenderAdapter):
    provider = "smtp"

    async def export(self, *, campaign, subject, html, recipients, config) -> ExportResult:
        host = config.get("host")
        if not host:
            # no SMTP configured → behave like a skeleton (accept, don't send)
            return ExportResult(accepted=len(recipients), provider_ref=f"smtp-noop-{uuid.uuid4().hex[:8]}")
        accepted = 0
        try:
            from email.message import EmailMessage

            import aiosmtplib

            smtp = aiosmtplib.SMTP(
                hostname=host, port=int(config.get("port", 587)),
                use_tls=bool(config.get("use_tls", False)),
            )
            await smtp.connect()
            if config.get("username"):
                await smtp.login(config["username"], config.get("password", ""))
            for r in recipients:
                msg = EmailMessage()
                msg["From"] = config.get("from", "no-reply@example.com")
                msg["To"] = r.email
                msg["Subject"] = subject
                msg.set_content(tpl_svc.html_to_text(html))
                msg.add_alternative(html, subtype="html")
                try:
                    await smtp.send_message(msg)
                    accepted += 1
                except Exception:  # noqa: BLE001
                    log.warning("smtp send failed to %s", r.email)
            await smtp.quit()
        except Exception:  # noqa: BLE001
            log.exception("smtp export failed for campaign %s", campaign.id)
        return ExportResult(accepted=accepted, provider_ref=f"smtp-{uuid.uuid4().hex[:8]}",
                            rejected=len(recipients) - accepted)


class _SkeletonEsp(EmailSenderAdapter):
    def __init__(self, provider: str):
        self.provider = provider

    async def export(self, *, campaign, subject, html, recipients, config) -> ExportResult:
        # A real integration would POST the batch to the ESP's transactional/
        # campaign API and store the returned batch id as provider_ref.
        log.info("ESP %s export: %d recipients (campaign %s)", self.provider, len(recipients), campaign.id)
        return ExportResult(accepted=len(recipients), provider_ref=f"{self.provider}-{uuid.uuid4().hex[:10]}")


_ADAPTERS: dict[str, EmailSenderAdapter] = {
    "smtp": SmtpAdapter(),
    "ses": _SkeletonEsp("ses"),
    "sendgrid": _SkeletonEsp("sendgrid"),
    "edm_provider": _SkeletonEsp("edm_provider"),
}


def get_adapter(provider: str) -> EmailSenderAdapter:
    if provider not in _ADAPTERS:
        raise EdmError(f"unknown provider {provider}", "bad_provider")
    return _ADAPTERS[provider]


# --------------------------------------------------------------------------
# audience + content
# --------------------------------------------------------------------------
async def _resolve_recipients(
    session: AsyncSession, *, workspace_id: uuid.UUID, segment: Segment | None
) -> list[Recipient]:
    from ..segments import service as seg_svc

    out: list[Recipient] = []
    definition = segment.definition if segment else None
    static_ids = segment.snapshot_ids if (segment and segment.mode == "static") else None
    async for batch in seg_svc.iter_audience(
        session, workspace_id=workspace_id, definition=definition, static_ids=static_ids,
        batch=AUDIENCE_BATCH,
    ):
        contacts = (
            await session.execute(select(Contact).where(Contact.id.in_(batch)))
        ).scalars().all()
        for c in contacts:
            if c.email and not c.is_blacklisted and not (c.custom or {}).get("marketing_opt_out"):
                out.append(Recipient(contact_id=c.id, email=c.email, contact=c))
    return out


async def _compile_content(
    session: AsyncSession, *, template: MsgTemplate | None, sample: Contact | None
) -> tuple[str, str]:
    if template is None or template.channel != "email":
        return ("", "<p></p>")
    content = await tpl_svc.build_content(
        session, template=template, variable_mapping={}, contact=sample, channel_type="email",
    )
    subject = ""
    html = ""
    for b in content.blocks:
        if getattr(b, "kind", None) == "email":
            subject = b.subject or ""
            html = tpl_svc.inline_css((template.body or {}).get("html") or b.text)
    return subject, html or "<p></p>"


# --------------------------------------------------------------------------
# launch + poll
# --------------------------------------------------------------------------
async def launch(sf: async_sessionmaker[AsyncSession], campaign_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    async with sf() as session:
        async with session.begin():
            campaign = await session.get(EdmCampaign, campaign_id)
            if campaign is None or campaign.status in ("running", "completed", "cancelled"):
                return "noop"
            segment = await session.get(Segment, campaign.segment_id) if campaign.segment_id else None
            template = await session.get(MsgTemplate, campaign.template_id) if campaign.template_id else None
            campaign.status = "running"
            provider = campaign.provider
            config = (campaign.schedule or {}).get("provider_config", {})
            workspace_id = campaign.workspace_id

    async with sf() as session:
        recipients = await _resolve_recipients(session, workspace_id=workspace_id, segment=segment)
        subject, html = await _compile_content(
            session, template=template, sample=recipients[0].contact if recipients else None
        )
    adapter = get_adapter(provider)
    result = await adapter.export(
        campaign=campaign, subject=subject, html=html, recipients=recipients, config=config
    )
    async with sf() as session:
        async with session.begin():
            campaign = await session.get(EdmCampaign, campaign_id)
            if campaign is None:
                return "gone"
            campaign.planned_count = len(recipients)
            campaign.sent_count = result.accepted
            campaign.status = "completed"
            campaign.schedule = {**(campaign.schedule or {}), "provider_ref": result.provider_ref,
                                 "exported_at": now.isoformat()}
    return "sent"


async def poll_campaign(sf: async_sessionmaker[AsyncSession], campaign_id: uuid.UUID) -> StatsResult:
    async with sf() as session:
        campaign = await session.get(EdmCampaign, campaign_id)
        if campaign is None:
            return StatsResult()
        config = (campaign.schedule or {}).get("provider_config", {})
        adapter = get_adapter(campaign.provider)
        stats = await adapter.poll(campaign=campaign, config=config)
    async with sf() as session:
        async with session.begin():
            campaign = await session.get(EdmCampaign, campaign_id)
            if campaign is not None:
                campaign.delivered_count = max(campaign.delivered_count, stats.delivered)
                campaign.opened_count = max(campaign.opened_count, stats.opened)
                campaign.clicked_count = max(campaign.clicked_count, stats.clicked)
    return stats


def validate(data: dict[str, Any]) -> None:
    if data.get("provider") not in PROVIDERS:
        raise EdmError(f"provider must be one of {PROVIDERS}", "bad_provider")


# --------------------------------------------------------------------------
# ARQ tasks + enqueue (registered on import; worker imports this module)
# --------------------------------------------------------------------------
_arq_pool: Any = None
JOB_LAUNCH = "edm_launch_task"


async def _pool() -> Any:
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool

        from ...settings import get_settings

        _arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _arq_pool


async def enqueue_launch(campaign_id: uuid.UUID | str) -> None:
    try:
        pool = await _pool()
        await pool.enqueue_job(JOB_LAUNCH, str(campaign_id))
    except Exception:  # noqa: BLE001
        log.exception("enqueue edm launch failed campaign=%s", campaign_id)


def _register_tasks() -> None:
    from arq import cron

    from ...jobs.worker import register_cron, task

    @task
    async def edm_launch_task(ctx: dict[str, Any], campaign_id: str) -> str:
        return await launch(ctx["session_factory"], uuid.UUID(campaign_id))

    @task
    async def edm_poll_stats_task(ctx: dict[str, Any]) -> int:
        sf: async_sessionmaker[AsyncSession] = ctx["session_factory"]
        async with sf() as session:
            ids = (
                await session.execute(
                    select(EdmCampaign.id).where(EdmCampaign.status == "completed")
                )
            ).scalars().all()
        for cid in ids:
            try:
                await poll_campaign(sf, cid)
            except Exception:  # noqa: BLE001
                log.exception("edm poll failed campaign=%s", cid)
        return len(ids)

    register_cron(cron(edm_poll_stats_task, minute={7, 37}, run_at_startup=False))


_register_tasks()
