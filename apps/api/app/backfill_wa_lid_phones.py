"""One-off backfill: fix WhatsApp-App contacts whose identity/phone stored a
LID (WhatsApp privacy id) instead of the real phone number — the pre-fix
bridge emitted ``external_user_id=<lid digits>`` and ``phone="+<lid>"`` when
``SenderAlt`` was empty.

    python -m apps.api.app.backfill_wa_lid_phones [--apply]
        [--map <lid>=<+phone>]... [--clear-phone <lid>]... [--workspace <uuid>]

DRY-RUN by default: prints the per-identity action table and writes NOTHING.
``--apply`` executes, one transaction per channel account. Classification is
NEVER guessed from digit length — only two sources decide:

  (a) the bridge device's local lid<->phone store (``POST /devices/{id}/resolve``,
      offline-safe, no usync), and
  (b) explicit operator input: ``--map`` (this lid IS this phone) and
      ``--clear-phone`` (real number unknown — drop the fake "+<lid>" phone,
      keep the identity lid-keyed and addressable via @lid).

Actions produced (see plan_backfill_actions):
  migrate     lid-keyed identity re-keyed to the resolved/mapped phone digits
              (same contact), placeholder phone corrected
  merge       both a lid-keyed and a phone-keyed identity exist for the same
              person -> merge the CONTACTS (lid contact into phone contact);
              identities are never merged (conversations are 1:1 with them)
  annotate    digits are a known phone -> just record meta.wa_lid (+ fill an
              empty phone)
  clear_phone contact.phone == "+<lid>" and the real number is unknown ->
              phone cleared (UI shows "-"); outbound still works via @lid
  report      unknown to the store and no operator input -> printed only

Exit codes: 0 ok, 2 usage error.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from .db import session_factory
from .models.channels import ChannelAccount, DeviceBridge
from .models.contacts import ChannelIdentity, Contact
from .services.bridge_client import BridgeError, get_bridge_client

RESOLVE_CHUNK = 500


# --------------------------------------------------------------------------
# pure planning (unit-tested; no I/O)
# --------------------------------------------------------------------------
@dataclass
class Action:
    kind: str  # migrate | merge | annotate | clear_phone | report
    identity_id: uuid.UUID
    external_user_id: str
    contact_id: uuid.UUID
    display_name: str
    old_phone: str | None
    wa_lid: str | None = None
    new_phone: str | None = None
    new_external_user_id: str | None = None
    merge_target_contact_id: uuid.UUID | None = None
    note: str = ""


@dataclass
class BackfillPlan:
    actions: list[Action] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Action]:
        return [a for a in self.actions if a.kind == kind]


def _placeholder(phone: str | None, lid: str | None) -> bool:
    return bool(phone and lid and phone == f"+{lid}")


def plan_backfill_actions(
    identities: list[Any],
    contacts_by_id: dict[uuid.UUID, Any],
    results: dict[str, dict[str, Any]],
    manual_map: dict[str, str],
    clear_set: set[str],
) -> BackfillPlan:
    """Decide one Action per identity from the bridge-store classification +
    operator input. ``identities`` are the whatsapp_app ChannelIdentity rows of
    ONE channel account; ``results`` maps external_user_id -> resolve result
    ``{kind: lid|pn|unknown, pn?, lid?}``."""
    plan = BackfillPlan()
    by_external = {i.external_user_id: i for i in identities}
    claimed_pns: set[str] = set()  # phones already targeted by a migrate in THIS plan

    def _contact(i: Any) -> Any | None:
        return contacts_by_id.get(i.contact_id)

    for ident in identities:
        digits = ident.external_user_id
        contact = _contact(ident)
        display = getattr(contact, "display_name", "") or ""
        old_phone = getattr(contact, "phone", None) if contact is not None else None
        res = results.get(digits) or {"kind": "unknown"}
        kind = res.get("kind")

        pn: str | None = None
        lid: str | None = None
        source = ""
        if kind == "lid" and res.get("pn"):
            pn, lid, source = str(res["pn"]), digits, "store"
        elif kind == "pn":
            plan.actions.append(
                Action(
                    kind="annotate",
                    identity_id=ident.id,
                    external_user_id=digits,
                    contact_id=ident.contact_id,
                    display_name=display,
                    old_phone=old_phone,
                    wa_lid=str(res["lid"]) if res.get("lid") else None,
                    new_phone=f"+{digits}" if not old_phone else None,
                    note="digits are a known phone",
                )
            )
            continue
        elif digits in manual_map:
            pn, lid, source = manual_map[digits].lstrip("+"), digits, "--map"
        elif digits in clear_set:
            plan.actions.append(
                Action(
                    kind="clear_phone",
                    identity_id=ident.id,
                    external_user_id=digits,
                    contact_id=ident.contact_id,
                    display_name=display,
                    old_phone=old_phone,
                    wa_lid=digits,
                    new_phone=None,
                    note="--clear-phone"
                    if _placeholder(old_phone, digits)
                    else "--clear-phone (phone not the +<lid> placeholder — left as is)",
                )
            )
            continue
        else:
            plan.actions.append(
                Action(
                    kind="report",
                    identity_id=ident.id,
                    external_user_id=digits,
                    contact_id=ident.contact_id,
                    display_name=display,
                    old_phone=old_phone,
                    note="store does not know these digits; pass --map/--clear-phone if it is a lid",
                )
            )
            continue

        # digits are a lid with a known/mapped phone
        if pn in claimed_pns:
            # a second identity resolving/mapped to the SAME phone would
            # violate uq_channel_identities_acct_ext on apply — surface it
            # instead of crashing the account's transaction
            plan.actions.append(
                Action(
                    kind="report",
                    identity_id=ident.id,
                    external_user_id=digits,
                    contact_id=ident.contact_id,
                    display_name=display,
                    old_phone=old_phone,
                    wa_lid=lid,
                    note=f"another identity already migrates to {pn} — resolve manually",
                )
            )
            continue
        existing = by_external.get(pn)
        if existing is not None and existing.id != ident.id:
            plan.actions.append(
                Action(
                    kind="merge",
                    identity_id=ident.id,
                    external_user_id=digits,
                    contact_id=ident.contact_id,
                    display_name=display,
                    old_phone=old_phone,
                    wa_lid=lid,
                    new_phone=f"+{pn}",
                    merge_target_contact_id=existing.contact_id,
                    note=f"phone-keyed identity {existing.id} already exists ({source})",
                )
            )
        else:
            claimed_pns.add(pn)
            plan.actions.append(
                Action(
                    kind="migrate",
                    identity_id=ident.id,
                    external_user_id=digits,
                    contact_id=ident.contact_id,
                    display_name=display,
                    old_phone=old_phone,
                    wa_lid=lid,
                    new_phone=f"+{pn}",
                    new_external_user_id=pn,
                    note=source,
                )
            )
    return plan


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------
async def _apply_plan(session: Any, acct: ChannelAccount, plan: BackfillPlan) -> None:
    from .modules.contacts.service import MergeError, merge_contacts, ordered_pair

    for a in plan.actions:
        ident = await session.get(ChannelIdentity, a.identity_id)
        contact = await session.get(Contact, a.contact_id) if a.contact_id else None
        if ident is None:
            continue
        if a.kind == "migrate":
            ident.external_user_id = a.new_external_user_id
            ident.meta = {**(ident.meta or {}), "wa_lid": a.wa_lid}
            if contact is not None and (
                not contact.phone or _placeholder(contact.phone, a.wa_lid)
            ):
                contact.phone = a.new_phone
        elif a.kind == "merge":
            ident.meta = {**(ident.meta or {}), "wa_lid": a.wa_lid}
            if contact is None or a.merge_target_contact_id is None:
                continue
            # lock both contacts in merge_contacts' canonical order BEFORE any
            # mutation — the placeholder-clearing autoflush must never invert
            # the lock order against a concurrent UI merge
            first, second = ordered_pair(a.merge_target_contact_id, a.contact_id)
            locked: dict[Any, Any] = {}
            for cid in (first, second):
                row = (
                    await session.execute(
                        select(Contact)
                        .where(Contact.id == cid, Contact.workspace_id == acct.workspace_id)
                        .with_for_update()
                    )
                ).scalars().first()
                locked[cid] = row
            source = locked.get(a.contact_id)
            target = locked.get(a.merge_target_contact_id)
            if source is None or target is None or source.id == target.id:
                continue
            if source.merged_into_id is not None:
                continue  # already merged
            if _placeholder(source.phone, a.wa_lid):
                source.phone = None  # the fake phone must not win the merge fill-missing
            try:
                await merge_contacts(
                    session,
                    workspace_id=acct.workspace_id,
                    target_id=target.id,
                    source_id=source.id,
                    actor_member_id=None,
                )
            except MergeError as e:
                print(f"  !! merge skipped for lid {a.wa_lid}: {e}")
                continue
            if not target.phone and a.new_phone:
                target.phone = a.new_phone
        elif a.kind == "annotate":
            if a.wa_lid:
                ident.meta = {**(ident.meta or {}), "wa_lid": a.wa_lid}
            if contact is not None and a.new_phone and not contact.phone:
                contact.phone = a.new_phone
        elif a.kind == "clear_phone":
            ident.meta = {**(ident.meta or {}), "wa_lid": a.wa_lid}
            if contact is not None and _placeholder(contact.phone, a.wa_lid):
                contact.phone = None


def _print_plan(acct: ChannelAccount, plan: BackfillPlan) -> None:
    print(f"\n== channel_account {acct.id} ({acct.name or acct.channel_type}) ==")
    if not plan.actions:
        print("  (no whatsapp_app identities)")
        return
    hdr = (
        f"  {'action':<12} {'external_user_id':<18} {'contact':<22} "
        f"{'phone now':<18} {'phone new':<16} note"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for a in plan.actions:
        print(
            f"  {a.kind:<12} {a.external_user_id:<18} {a.display_name[:20]:<22} "
            f"{(a.old_phone or '-'):<18} {(a.new_phone or '-'):<16} {a.note}"
        )


def _parse_args(argv: list[str]) -> tuple[bool, dict[str, str], set[str], uuid.UUID | None]:
    apply = False
    manual_map: dict[str, str] = {}
    clear_set: set[str] = set()
    workspace: uuid.UUID | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--apply":
            apply = True
        elif arg == "--map":
            i += 1
            try:
                lid, phone = argv[i].split("=", 1)
            except (IndexError, ValueError):
                print("usage: --map <lid>=<+phone>")
                raise SystemExit(2) from None
            if not phone.lstrip("+").isdigit() or not lid.isdigit():
                print(f"--map: bad value {argv[i]!r} (want <digits>=<+digits>)")
                raise SystemExit(2)
            manual_map[lid] = phone if phone.startswith("+") else f"+{phone}"
        elif arg == "--clear-phone":
            i += 1
            try:
                clear_set.add(argv[i])
            except IndexError:
                print("usage: --clear-phone <lid>")
                raise SystemExit(2) from None
        elif arg == "--workspace":
            i += 1
            try:
                workspace = uuid.UUID(argv[i])
            except (IndexError, ValueError):
                print("usage: --workspace <uuid>")
                raise SystemExit(2) from None
        else:
            print(__doc__)
            raise SystemExit(2)
        i += 1
    return apply, manual_map, clear_set, workspace


async def main() -> None:
    apply, manual_map, clear_set, workspace = _parse_args(sys.argv[1:])
    mode = "APPLY" if apply else "DRY-RUN (pass --apply to write)"
    print(f"wa-lid phone backfill — {mode}")

    bridge = get_bridge_client()
    factory = session_factory()
    async with factory() as session:
        q = select(ChannelAccount).where(ChannelAccount.channel_type == "whatsapp_app")
        if workspace is not None:
            q = q.where(ChannelAccount.workspace_id == workspace)
        accounts = list((await session.execute(q)).scalars())
        if not accounts:
            print("no whatsapp_app channel accounts found")
            return

        for acct in accounts:
            db = (
                await session.execute(
                    select(DeviceBridge).where(DeviceBridge.channel_account_id == acct.id)
                )
            ).scalar_one_or_none()
            device_id = (db.config or {}).get("device_id") if db is not None else None
            device_id = device_id or str(acct.id)

            identities = list(
                (
                    await session.execute(
                        select(ChannelIdentity).where(
                            ChannelIdentity.channel_account_id == acct.id
                        )
                    )
                ).scalars()
            )
            contact_ids = {i.contact_id for i in identities}
            contacts = {
                c.id: c
                for c in (
                    await session.execute(select(Contact).where(Contact.id.in_(contact_ids)))
                ).scalars()
            } if contact_ids else {}

            results: dict[str, dict[str, Any]] = {}
            ids = [i.external_user_id for i in identities]
            for start in range(0, len(ids), RESOLVE_CHUNK):
                chunk = ids[start : start + RESOLVE_CHUNK]
                try:
                    resp = await bridge.resolve_ids(device_id, chunk)
                    results.update(resp.get("results") or {})
                except BridgeError as e:
                    print(f"  !! bridge resolve failed for device {device_id}: {e}")
                    print("  !! continuing with operator input (--map/--clear-phone) only")
                    break

            plan = plan_backfill_actions(identities, contacts, results, manual_map, clear_set)
            _print_plan(acct, plan)

            if apply:
                await _apply_plan(session, acct, plan)
                await session.commit()
                counts = {
                    k: len(plan.by_kind(k))
                    for k in ("migrate", "merge", "annotate", "clear_phone", "report")
                }
                print(f"  applied: {counts}")

    await bridge.aclose()
    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
