"""Email channel: aioimaplib polling (+IDLE loop) inbound, aiosmtplib outbound.

Threading (plan A.7 order): plus-address (support+c_{conversation}@domain) →
In-Reply-To/References → sender's open conversation (conversations are 1:1
with the sender identity anyway) → new conversation. Subject changes never
fork a thread. Message-ID is the dedup key.

The poller stores attachments/HTML bodies in MinIO itself, then feeds
pre-normalized events straight into the ingress pipeline (email has no
webhook).

Credentials dict: {email, imap_host, imap_port, imap_ssl, imap_user,
imap_password, smtp_host, smtp_port, smtp_tls, smtp_user, smtp_password,
from_name?}.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parseaddr
from typing import Any, ClassVar

from py_contracts.content import (
    ContentBlock,
    EmailBlock,
    MediaBlock,
    MessageContent,
    TextBlock,
)

from ..base import (
    BaseAdapter,
    HealthResult,
    InboundEvent,
    MessageIn,
    ProfileHint,
    SendResult,
    degrade_content,
    email_to_text,
)

log = logging.getLogger("smartchat.channels.email")

_PLUS_RE = re.compile(r"^([^+@]+)\+c_([A-Za-z0-9\-]+)@(.+)$")
_MSGID_RE = re.compile(r"<([^>]+)>")


# --------------------------------------------------------------------------
# pure parsing (unit-tested)
# --------------------------------------------------------------------------
@dataclass
class ParsedAttachment:
    filename: str | None
    mime: str | None
    data: bytes


@dataclass
class ParsedEmail:
    message_id: str | None
    in_reply_to: str | None
    references: list[str] = field(default_factory=list)
    from_addr: str = ""
    from_name: str | None = None
    to_addrs: list[str] = field(default_factory=list)
    subject: str | None = None
    text: str = ""
    html: str | None = None
    date: datetime | None = None
    attachments: list[ParsedAttachment] = field(default_factory=list)


def normalize_message_id(raw: str | None) -> str | None:
    if not raw:
        return None
    m = _MSGID_RE.search(raw)
    return m.group(1) if m else raw.strip() or None


def parse_references(raw: str | None) -> list[str]:
    if not raw:
        return []
    return _MSGID_RE.findall(raw)


def conversation_hint_from_plus(to_addrs: list[str]) -> str | None:
    """support+c_<token>@domain → <token> (plus-address thread routing)."""
    for addr in to_addrs:
        m = _PLUS_RE.match(addr.strip().lower())
        if m:
            return m.group(2)
    return None


def parse_email_bytes(raw: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    from_name, from_addr = parseaddr(str(msg.get("From", "")))
    to_addrs: list[str] = []
    for header in ("To", "Cc", "Delivered-To", "X-Original-To"):
        value = msg.get_all(header) or []
        for v in value:
            for part in str(v).split(","):
                _, addr = parseaddr(part)
                if addr:
                    to_addrs.append(addr)
    date_hdr = msg.get("Date")
    date: datetime | None = None
    if date_hdr:
        try:
            from email.utils import parsedate_to_datetime

            date = parsedate_to_datetime(str(date_hdr))
            if date is not None and date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            date = None
    text = ""
    html: str | None = None
    attachments: list[ParsedAttachment] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = part.get_content_disposition()
            ctype = part.get_content_type()
            if disp == "attachment" or (disp == "inline" and part.get_filename()):
                try:
                    attachments.append(
                        ParsedAttachment(
                            filename=part.get_filename(),
                            mime=ctype,
                            data=part.get_payload(decode=True) or b"",
                        )
                    )
                except Exception:  # noqa: BLE001 — malformed parts must not kill ingest
                    log.warning("undecodable attachment part skipped")
            elif ctype == "text/plain" and not text:
                text = part.get_content()
            elif ctype == "text/html" and html is None:
                html = part.get_content()
    else:
        if msg.get_content_type() == "text/html":
            html = msg.get_content()
        else:
            text = msg.get_content()
    if not text and html:
        text = html_to_text(html)
    return ParsedEmail(
        message_id=normalize_message_id(str(msg.get("Message-ID", "")) or None),
        in_reply_to=normalize_message_id(str(msg.get("In-Reply-To", "")) or None),
        references=parse_references(str(msg.get("References", "")) or None),
        from_addr=from_addr,
        from_name=from_name or None,
        to_addrs=to_addrs,
        subject=str(msg.get("Subject")) if msg.get("Subject") is not None else None,
        text=text.strip(),
        html=html,
        date=date,
        attachments=attachments,
    )


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    stripped = _TAG_RE.sub(" ", html)
    stripped = re.sub(r"<br\s*/?>|</p>|</div>", "\n", stripped, flags=re.I)
    text = _HTML_RE.sub("", stripped)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def reply_subject(original: str | None) -> str:
    if not original:
        return "Re:"
    return original if original.lower().startswith("re:") else f"Re: {original}"


def plus_address(base_email: str, conversation_id: uuid.UUID | str) -> str:
    """support@x.com + conv → support+c_<hex>@x.com (thread return path)."""
    local, _, domain = base_email.partition("@")
    token = str(conversation_id).replace("-", "")
    return f"{local}+c_{token}@{domain}"


# --------------------------------------------------------------------------
# adapter
# --------------------------------------------------------------------------
class EmailAdapter(BaseAdapter):
    channel_type: ClassVar[str] = "email"

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundEvent]:
        # the IMAP poller pre-normalizes; hooks never hit this channel
        return super().parse_inbound(payload)

    @staticmethod
    def message_in_from_parsed(
        parsed: ParsedEmail,
        *,
        attachment_blocks: list[ContentBlock] | None = None,
        html_body_file_id: uuid.UUID | None = None,
    ) -> MessageIn:
        blocks: list[ContentBlock] = [
            EmailBlock(
                subject=parsed.subject,
                text=parsed.text,
                html_body_file_id=html_body_file_id,
                headers={
                    "message_id": parsed.message_id,
                    "in_reply_to": parsed.in_reply_to,
                    "references": parsed.references,
                    "conversation_hint": conversation_hint_from_plus(parsed.to_addrs),
                },
            )
        ]
        blocks.extend(attachment_blocks or [])
        return MessageIn(
            external_message_id=parsed.message_id or f"noid:{uuid.uuid4()}",
            external_user_id=parsed.from_addr.lower(),
            content=MessageContent(blocks=blocks),
            external_timestamp=parsed.date,
            profile=ProfileHint(display_name=parsed.from_name, email=parsed.from_addr.lower()),
        )

    # -- outbound ----------------------------------------------------------
    def render(self, content: MessageContent, *, window_open: bool = True) -> list[dict[str, Any]]:
        degraded = degrade_content(content, self.capabilities)
        subject: str | None = None
        text_parts: list[str] = []
        html: str | None = None
        attachment_file_ids: list[str] = []
        for block in degraded.blocks:
            if isinstance(block, EmailBlock):
                subject = subject or block.subject
                text_parts.append(block.text)
            elif isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, MediaBlock):
                attachment_file_ids.append(str(block.file_id))
            else:
                text_parts.append(email_to_text(block).text if isinstance(block, EmailBlock) else "")
        return [
            {
                "subject": subject,
                "text": "\n\n".join(p for p in text_parts if p),
                "html": html,
                "attachment_file_ids": attachment_file_ids,
                "headers": {},
            }
        ]

    async def enrich_outbound(
        self,
        session: Any,
        *,
        account: Any,
        credentials: dict[str, Any],
        conversation: Any,
        identity: Any,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Thread the reply: In-Reply-To/References from the last inbound
        email in this conversation, Re: subject, plus-address reply-to."""
        from sqlalchemy import select

        from ...models.messaging import Message

        last_in = (
            await session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.direction == "in",
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        in_reply_to: str | None = None
        references: list[str] = []
        last_subject: str | None = None
        if last_in is not None:
            for b in (last_in.content or {}).get("blocks", []):
                if b.get("kind") == "email":
                    headers = b.get("headers") or {}
                    in_reply_to = headers.get("message_id")
                    references = list(headers.get("references") or [])
                    if in_reply_to:
                        references.append(in_reply_to)
                    last_subject = b.get("subject")
                    break
        for p in payloads:
            headers = p.setdefault("headers", {})
            if in_reply_to:
                headers.setdefault("in_reply_to", in_reply_to)
            if references:
                headers.setdefault("references", references[-10:])
            if not p.get("subject"):
                p["subject"] = reply_subject(last_subject)
            p.setdefault("reply_to", plus_address(account.external_id, conversation.id))
        return payloads

    async def send(
        self, account: Any, credentials: dict[str, Any], to: str, payload: dict[str, Any]
    ) -> SendResult:
        import aiosmtplib

        host = credentials.get("smtp_host", "")
        port = int(credentials.get("smtp_port") or 587)
        use_tls = port == 465
        username = credentials.get("smtp_user") or credentials.get("imap_user") or account.external_id
        password = credentials.get("smtp_password") or credentials.get("imap_password") or ""
        from_addr = credentials.get("email") or account.external_id
        from_name = credentials.get("from_name") or account.name or from_addr

        msg = EmailMessage()
        msg["From"] = f"{from_name} <{from_addr}>"
        msg["To"] = to
        msg["Subject"] = payload.get("subject") or "(no subject)"
        domain = from_addr.partition("@")[2] or "smartchat.local"
        message_id = make_msgid(domain=domain)
        msg["Message-ID"] = message_id
        msg["Date"] = format_datetime(datetime.now(UTC))
        headers = payload.get("headers") or {}
        if headers.get("in_reply_to"):
            msg["In-Reply-To"] = f"<{headers['in_reply_to']}>"
        if headers.get("references"):
            msg["References"] = " ".join(f"<{r}>" for r in headers["references"])
        if payload.get("reply_to"):
            msg["Reply-To"] = payload["reply_to"]
        body_text = payload.get("text") or ""
        # attachments referenced by public URL (bytes stay in MinIO)
        for fid in payload.get("attachment_file_ids") or []:
            from ..media import file_public_url

            body_text += f"\n\nAttachment: {file_public_url(fid)}"
        msg.set_content(body_text)
        if payload.get("html"):
            msg.add_alternative(payload["html"], subtype="html")
        try:
            smtp = aiosmtplib.SMTP(hostname=host, port=port, use_tls=use_tls, timeout=30)
            await smtp.connect()
            if not use_tls and credentials.get("smtp_tls", True):
                try:
                    await smtp.starttls()
                except aiosmtplib.SMTPException:
                    pass  # server may not support STARTTLS
            if username and password:
                await smtp.login(username, password)
            await smtp.send_message(msg)
            try:
                await smtp.quit()
            except aiosmtplib.SMTPException:
                pass
            return SendResult(
                ok=True, external_message_id=normalize_message_id(message_id)
            )
        except aiosmtplib.SMTPAuthenticationError as e:
            return SendResult(ok=False, error_code="AUTH", error_message=str(e)[:500])
        except aiosmtplib.SMTPResponseException as e:
            code = "RETRYABLE" if 400 <= e.code < 500 else "PERMANENT"
            return SendResult(ok=False, error_code=code, error_message=str(e)[:500])
        except (aiosmtplib.SMTPException, OSError) as e:
            return SendResult(ok=False, error_code="NETWORK", error_message=str(e)[:500])

    async def check_health(self, account: Any, credentials: dict[str, Any]) -> HealthResult:
        detail: dict[str, Any] = {}
        try:
            client = await _open_imap(credentials)
            await client.logout()
            detail["imap"] = "ok"
        except Exception as e:  # noqa: BLE001
            return HealthResult(ok=False, status="error", detail={"imap": str(e)[:300]})
        return HealthResult(ok=True, status="active", detail=detail)


# --------------------------------------------------------------------------
# IMAP polling (worker tasks live in ..sender/..ingress registration module)
# --------------------------------------------------------------------------
async def _open_imap(credentials: dict[str, Any]) -> Any:
    import aioimaplib

    host = credentials.get("imap_host", "")
    port = int(credentials.get("imap_port") or 993)
    ssl = bool(credentials.get("imap_ssl", True))
    user = credentials.get("imap_user") or credentials.get("email") or ""
    password = credentials.get("imap_password") or ""
    client = (
        aioimaplib.IMAP4_SSL(host=host, port=port, timeout=30)
        if ssl
        else aioimaplib.IMAP4(host=host, port=port, timeout=30)
    )
    await client.wait_hello_from_server()
    resp = await client.login(user, password)
    if resp.result != "OK":
        raise PermissionError(f"IMAP login failed: {resp.lines}")
    return client


def _extract_rfc822(lines: list[Any]) -> bytes | None:
    """The FETCH literal is the largest bytes-ish element in the response."""
    best: bytes | None = None
    for line in lines:
        if isinstance(line, (bytes, bytearray)) and (best is None or len(line) > len(best)):
            best = bytes(line)
    if best is None or b"\r\n" not in best[:2000]:
        return best
    return best


def _parse_uid_list(lines: list[Any]) -> list[int]:
    for line in lines:
        s = line.decode() if isinstance(line, (bytes, bytearray)) else str(line)
        s = s.strip()
        if s and all(tok.isdigit() for tok in s.split()):
            return [int(tok) for tok in s.split()]
    return []


async def poll_email_account(session_factory: Any, redis: Any, account_id: uuid.UUID) -> int:
    """One poll pass: fetch new UIDs, normalize, feed the ingress pipeline.
    Returns the number of messages ingested."""
    from sqlalchemy.orm.attributes import flag_modified

    from ...models.channels import ChannelAccount
    from .. import creds as creds_mod
    from .. import ingress_pipeline
    from ..media import get_media_store

    async with session_factory() as session:
        acct = await session.get(ChannelAccount, account_id)
        if acct is None or not acct.enabled or acct.channel_type != "email":
            return 0
        credentials = await creds_mod.get_credentials(session, acct)
        last_uid = int((acct.config or {}).get("imap_last_uid") or 0)
        workspace_id = acct.workspace_id
    client = await _open_imap(credentials)
    ingested = 0
    max_uid = last_uid
    try:
        await client.select("INBOX")
        if last_uid:
            resp = await client.uid_search(f"UID {last_uid + 1}:*")
        else:
            resp = await client.uid_search("UNSEEN")
        uids = [u for u in _parse_uid_list(resp.lines) if u > last_uid]
        store = get_media_store()
        adapter = EmailAdapter()
        for uid_ in sorted(uids):
            fetch = await client.uid("fetch", str(uid_), "(RFC822)")
            raw = _extract_rfc822(fetch.lines)
            if not raw:
                continue
            try:
                parsed = parse_email_bytes(raw)
            except Exception:  # noqa: BLE001
                log.exception("unparseable email uid=%s account=%s", uid_, account_id)
                max_uid = max(max_uid, uid_)
                continue
            attachment_blocks: list[ContentBlock] = []
            html_file_id: uuid.UUID | None = None
            async with session_factory() as session:
                async with session.begin():
                    if parsed.html:
                        f = await store.store_bytes(
                            session,
                            workspace_id=workspace_id,
                            data=parsed.html.encode(),
                            mime="text/html",
                            filename="body.html",
                            created_by_type="contact",
                        )
                        html_file_id = f.id
                    for att in parsed.attachments:
                        if not att.data:
                            continue
                        f = await store.store_bytes(
                            session,
                            workspace_id=workspace_id,
                            data=att.data,
                            mime=att.mime,
                            filename=att.filename,
                            created_by_type="contact",
                        )
                        mt = "image" if (att.mime or "").startswith("image/") else "file"
                        attachment_blocks.append(
                            MediaBlock(
                                media_type=mt,  # type: ignore[arg-type]
                                file_id=f.id,
                                mime=att.mime,
                                size=len(att.data),
                            )
                        )
            ev = EmailAdapter.message_in_from_parsed(
                parsed, attachment_blocks=attachment_blocks, html_body_file_id=html_file_id
            )
            await ingress_pipeline.handle_events(
                session_factory, redis, account_id, [ev], adapter=adapter
            )
            ingested += 1
            max_uid = max(max_uid, uid_)
    finally:
        try:
            await client.logout()
        except Exception:  # noqa: BLE001
            pass
    if max_uid > last_uid:
        async with session_factory() as session:
            async with session.begin():
                acct = await session.get(ChannelAccount, account_id)
                if acct is not None:
                    acct.config = {**(acct.config or {}), "imap_last_uid": max_uid}
                    flag_modified(acct, "config")
    return ingested


async def run_email_idle_loop(
    session_factory: Any, redis: Any, account_id: uuid.UUID, stop: Any = None
) -> None:
    """Long-running IMAP IDLE trigger loop for one account: poll, then IDLE
    until the server pushes or 55s elapses, then poll again. The every-minute
    cron poll remains the fallback if this loop isn't running."""
    import asyncio

    while stop is None or not stop.is_set():
        try:
            await poll_email_account(session_factory, redis, account_id)
            async with session_factory() as session:
                from ...models.channels import ChannelAccount
                from .. import creds as creds_mod

                acct = await session.get(ChannelAccount, account_id)
                if acct is None or not acct.enabled:
                    return
                credentials = await creds_mod.get_credentials(session, acct)
            client = await _open_imap(credentials)
            try:
                await client.select("INBOX")
                idle_task = await client.idle_start(timeout=55)
                await client.wait_server_push(timeout=55)
                client.idle_done()
                await asyncio.wait_for(idle_task, timeout=10)
            finally:
                try:
                    await client.logout()
                except Exception:  # noqa: BLE001
                    pass
        except TimeoutError:
            continue
        except Exception:  # noqa: BLE001
            log.exception("email idle loop error account=%s", account_id)
            await asyncio.sleep(60)
