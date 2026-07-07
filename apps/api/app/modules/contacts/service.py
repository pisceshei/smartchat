"""Contacts / ONE-ID service (plan A.4).

Invariant: messages & conversations always hang off channel_identities; a
merge only re-points identities and refreshes denormalized contact_id
pointers. The merge snapshot (contact_merges) records EXACTLY what moved and
what fields were overwritten, so unmerge is a precise replay — never a guess.

Layering: `plan_merge` / `plan_unmerge` are pure (unit-tested with plain
objects); `merge_contacts` / `unmerge_contacts` orchestrate the DB work inside
the caller's transaction.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from py_contracts.events import Actor, Event
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.contacts import (
    ChannelIdentity,
    Contact,
    ContactMerge,
    ContactMergeCandidate,
    ContactNote,
    ContactOrder,
)
from ...models.conversations import Conversation
from ...models.misc import AuditLog, ContactTag
from ...services import event_bus

# Contact scalar fields eligible for fill-missing during merge (target keeps
# its own values; empty target fields inherit the source's).
MERGE_FIELDS: tuple[str, ...] = (
    "display_name",
    "remark_name",
    "avatar_url",
    "email",
    "phone",
    "language",
    "country",
    "city",
    "timezone",
    "last_ip",
    "device",
    "browser",
    "os",
)

MOVED_KEY = "__moved"  # sub-dict inside field_overwrites holding moved row ids


class MergeError(Exception):
    code = "MERGE_ERROR"

    def __init__(self, detail: str = ""):
        super().__init__(detail or self.code)
        self.detail = detail or self.code


class NotUndoableError(MergeError):
    code = "MERGE_NOT_UNDOABLE"


# ==========================================================================
# pure planners (unit-tested)
# ==========================================================================
def _empty(v: Any) -> bool:
    return v is None or v == "" or v == {} or v == []


def compute_field_overwrites(target: Any, source: Any) -> dict[str, Any]:
    """Fill-missing semantics: for every mergeable field that is empty on the
    target and set on the source, plan an overwrite and snapshot the old
    value. Custom jsonb keys merge the same way under "custom.<key>"."""
    overwrites: dict[str, Any] = {}
    for f in MERGE_FIELDS:
        t_val, s_val = getattr(target, f, None), getattr(source, f, None)
        if _empty(t_val) and not _empty(s_val):
            overwrites[f] = {"old": t_val, "new": s_val}
    t_custom = dict(getattr(target, "custom", None) or {})
    s_custom = dict(getattr(source, "custom", None) or {})
    for k, s_val in s_custom.items():
        if _empty(t_custom.get(k)) and not _empty(s_val):
            overwrites[f"custom.{k}"] = {"old": t_custom.get(k), "new": s_val}
    # blacklist union: a merged-in blacklisted identity keeps the person blocked
    if not getattr(target, "is_blacklisted", False) and getattr(source, "is_blacklisted", False):
        overwrites["is_blacklisted"] = {"old": False, "new": True}
    return overwrites


def apply_field_overwrites(target: Any, overwrites: dict[str, Any]) -> None:
    custom = dict(getattr(target, "custom", None) or {})
    custom_touched = False
    for key, ov in overwrites.items():
        if key == MOVED_KEY:
            continue
        if key.startswith("custom."):
            custom[key.split(".", 1)[1]] = ov["new"]
            custom_touched = True
        else:
            setattr(target, key, ov["new"])
    if custom_touched:
        target.custom = custom


def revert_field_overwrites(target: Any, overwrites: dict[str, Any]) -> None:
    """Exact replay: restore every overwritten field to its snapshotted old
    value (custom keys whose old value was absent are removed again)."""
    custom = dict(getattr(target, "custom", None) or {})
    custom_touched = False
    for key, ov in overwrites.items():
        if key == MOVED_KEY:
            continue
        if key.startswith("custom."):
            ck = key.split(".", 1)[1]
            if _empty(ov.get("old")):
                custom.pop(ck, None)
            else:
                custom[ck] = ov["old"]
            custom_touched = True
        else:
            setattr(target, key, ov["old"])
    if custom_touched:
        target.custom = custom


@dataclass
class MergePlan:
    target_id: uuid.UUID
    source_id: uuid.UUID
    moved_identity_ids: list[uuid.UUID] = field(default_factory=list)
    moved_conversation_ids: list[uuid.UUID] = field(default_factory=list)
    field_overwrites: dict[str, Any] = field(default_factory=dict)
    moved_tag_ids: list[uuid.UUID] = field(default_factory=list)  # tags newly added to target
    moved_note_ids: list[uuid.UUID] = field(default_factory=list)
    moved_order_ids: list[uuid.UUID] = field(default_factory=list)
    linked_candidate_ids: list[uuid.UUID] = field(default_factory=list)

    def snapshot_overwrites(self) -> dict[str, Any]:
        """field_overwrites column value: overwrites + moved-row ledger."""
        return {
            **self.field_overwrites,
            MOVED_KEY: {
                "tags": [str(i) for i in self.moved_tag_ids],
                "notes": [str(i) for i in self.moved_note_ids],
                "orders": [str(i) for i in self.moved_order_ids],
                "candidates": [str(i) for i in self.linked_candidate_ids],
            },
        }


def plan_merge(
    target: Any,
    source: Any,
    *,
    source_identity_ids: list[uuid.UUID],
    source_conversation_ids: list[uuid.UUID],
    source_tag_ids: list[uuid.UUID],
    target_tag_ids: list[uuid.UUID],
    source_note_ids: list[uuid.UUID],
    source_order_ids: list[uuid.UUID],
    pair_candidate_ids: list[uuid.UUID],
) -> MergePlan:
    """Pure merge planner. Raises MergeError on invalid pairs."""
    if target.id == source.id:
        raise MergeError("cannot merge a contact into itself")
    if getattr(target, "workspace_id", None) != getattr(source, "workspace_id", None):
        raise MergeError("contacts belong to different workspaces")
    if getattr(source, "merged_into_id", None) is not None:
        raise MergeError("source contact is already merged")
    if getattr(target, "merged_into_id", None) is not None:
        raise MergeError("target contact is already merged")
    return MergePlan(
        target_id=target.id,
        source_id=source.id,
        moved_identity_ids=list(source_identity_ids),
        moved_conversation_ids=list(source_conversation_ids),
        field_overwrites=compute_field_overwrites(target, source),
        # only tags the target does not already carry actually move
        moved_tag_ids=[t for t in source_tag_ids if t not in set(target_tag_ids)],
        moved_note_ids=list(source_note_ids),
        moved_order_ids=list(source_order_ids),
        linked_candidate_ids=list(pair_candidate_ids),
    )


@dataclass
class UnmergePlan:
    merge_id: uuid.UUID
    target_id: uuid.UUID
    source_id: uuid.UUID
    identity_ids: list[uuid.UUID]
    conversation_ids: list[uuid.UUID]
    field_overwrites: dict[str, Any]
    tag_ids: list[uuid.UUID]
    note_ids: list[uuid.UUID]
    order_ids: list[uuid.UUID]
    candidate_ids: list[uuid.UUID]


def plan_unmerge(merge: Any, *, is_latest_for_target: bool, target_merged_away: bool) -> UnmergePlan:
    """Pure unmerge validation + replay plan. Only the newest merge in a chain
    is undoable (plan A.4)."""
    if getattr(merge, "undone_at", None) is not None:
        raise NotUndoableError("merge already undone")
    if not is_latest_for_target:
        raise NotUndoableError("only the most recent merge for this contact can be undone")
    if target_merged_away:
        raise NotUndoableError("target contact was merged into another contact")
    fo = dict(merge.field_overwrites or {})
    moved = fo.get(MOVED_KEY) or {}
    return UnmergePlan(
        merge_id=merge.id,
        target_id=merge.target_contact_id,
        source_id=merge.source_contact_id,
        identity_ids=[uuid.UUID(str(i)) for i in (merge.moved_identity_ids or [])],
        conversation_ids=[uuid.UUID(str(i)) for i in (merge.moved_conversation_ids or [])],
        field_overwrites=fo,
        tag_ids=[uuid.UUID(str(i)) for i in moved.get("tags", [])],
        note_ids=[uuid.UUID(str(i)) for i in moved.get("notes", [])],
        order_ids=[uuid.UUID(str(i)) for i in moved.get("orders", [])],
        candidate_ids=[uuid.UUID(str(i)) for i in moved.get("candidates", [])],
    )


def ordered_pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Canonical (contact_a, contact_b) order for the candidate unique key."""
    return (a, b) if str(a) < str(b) else (b, a)


def match_types(
    a: Any,
    b: Any,
    *,
    logged_in_a: set[str] | None = None,
    logged_in_b: set[str] | None = None,
) -> list[str]:
    """重複聯絡人 matchers: same phone / email / merchant logged-in id."""
    out: list[str] = []
    if not _empty(getattr(a, "phone", None)) and a.phone == getattr(b, "phone", None):
        out.append("phone")
    a_email = getattr(a, "email", None)
    b_email = getattr(b, "email", None)
    if not _empty(a_email) and not _empty(b_email) and str(a_email).lower() == str(b_email).lower():
        out.append("email")
    if logged_in_a and logged_in_b and (logged_in_a & logged_in_b):
        out.append("logged_in_id")
    return out


# ==========================================================================
# orchestration (caller's transaction; caller commits)
# ==========================================================================
async def _tag_ids_of(session: AsyncSession, contact_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(ContactTag.tag_id).where(ContactTag.contact_id == contact_id)
            )
        ).scalars()
    )


async def merge_contacts(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target_id: uuid.UUID,
    source_id: uuid.UUID,
    actor_member_id: uuid.UUID | None,
    now: datetime | None = None,
) -> tuple[ContactMerge, list[Event]]:
    """ONE-ID merge in one transaction: snapshot → re-point identities →
    refresh conversation denorm → migrate tags/notes/orders → tombstone the
    source. Returns (merge row, realtime events)."""
    now = now or datetime.now(UTC)
    # deterministic lock order avoids deadlocks between concurrent merges
    first, second = ordered_pair(target_id, source_id)
    locked: dict[uuid.UUID, Contact] = {}
    for cid in (first, second):
        row = (
            await session.execute(
                select(Contact)
                .where(Contact.id == cid, Contact.workspace_id == workspace_id)
                .with_for_update()
            )
        ).scalars().first()
        if row is None:
            raise MergeError(f"contact {cid} not found")
        locked[cid] = row
    target, source = locked[target_id], locked[source_id]

    identity_ids = list(
        (
            await session.execute(
                select(ChannelIdentity.id).where(ChannelIdentity.contact_id == source_id)
            )
        ).scalars()
    )
    conversation_ids = list(
        (
            await session.execute(
                select(Conversation.id).where(
                    Conversation.workspace_id == workspace_id,
                    Conversation.contact_id == source_id,
                )
            )
        ).scalars()
    )
    source_tags = await _tag_ids_of(session, source_id)
    target_tags = await _tag_ids_of(session, target_id)
    note_ids = list(
        (
            await session.execute(
                select(ContactNote.id).where(ContactNote.contact_id == source_id)
            )
        ).scalars()
    )
    order_ids = list(
        (
            await session.execute(
                select(ContactOrder.id).where(ContactOrder.contact_id == source_id)
            )
        ).scalars()
    )
    a, b = ordered_pair(target_id, source_id)
    candidate_ids = list(
        (
            await session.execute(
                select(ContactMergeCandidate.id).where(
                    ContactMergeCandidate.workspace_id == workspace_id,
                    ContactMergeCandidate.contact_a_id == a,
                    ContactMergeCandidate.contact_b_id == b,
                    ContactMergeCandidate.status == "suggested",
                )
            )
        ).scalars()
    )

    plan = plan_merge(
        target,
        source,
        source_identity_ids=identity_ids,
        source_conversation_ids=conversation_ids,
        source_tag_ids=source_tags,
        target_tag_ids=target_tags,
        source_note_ids=note_ids,
        source_order_ids=order_ids,
        pair_candidate_ids=candidate_ids,
    )

    # 1) re-point identities (the ONLY mutable edge)
    if plan.moved_identity_ids:
        await session.execute(
            update(ChannelIdentity)
            .where(ChannelIdentity.id.in_(plan.moved_identity_ids))
            .values(contact_id=target_id)
        )
    # 2) refresh conversation denormalized pointer
    if plan.moved_conversation_ids:
        await session.execute(
            update(Conversation)
            .where(Conversation.id.in_(plan.moved_conversation_ids))
            .values(contact_id=target_id)
        )
    # 3) fill-missing field overwrites
    apply_field_overwrites(target, plan.field_overwrites)
    # 4) tags: move non-duplicates, drop the rest
    if plan.moved_tag_ids:
        await session.execute(
            update(ContactTag)
            .where(ContactTag.contact_id == source_id, ContactTag.tag_id.in_(plan.moved_tag_ids))
            .values(contact_id=target_id)
        )
    await session.execute(delete(ContactTag).where(ContactTag.contact_id == source_id))
    # 5) notes + orders follow the person
    if plan.moved_note_ids:
        await session.execute(
            update(ContactNote)
            .where(ContactNote.id.in_(plan.moved_note_ids))
            .values(contact_id=target_id)
        )
    if plan.moved_order_ids:
        await session.execute(
            update(ContactOrder)
            .where(ContactOrder.id.in_(plan.moved_order_ids))
            .values(contact_id=target_id)
        )
    # 6) the pair's suggestion becomes 已關聯
    if plan.linked_candidate_ids:
        await session.execute(
            update(ContactMergeCandidate)
            .where(ContactMergeCandidate.id.in_(plan.linked_candidate_ids))
            .values(status="linked", resolved_at=now)
        )
    # 7) tombstone the source
    source.merged_into_id = target_id

    merge = ContactMerge(
        workspace_id=workspace_id,
        target_contact_id=target_id,
        source_contact_id=source_id,
        moved_identity_ids=[str(i) for i in plan.moved_identity_ids],
        moved_conversation_ids=[str(i) for i in plan.moved_conversation_ids],
        field_overwrites=plan.snapshot_overwrites(),
        merged_by_member_id=actor_member_id,
    )
    session.add(merge)
    session.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_type="member" if actor_member_id else "system",
            actor_id=actor_member_id,
            action="contacts.merge",
            target_type="contact",
            target_id=str(target_id),
            detail={"source_contact_id": str(source_id),
                    "moved_identities": len(plan.moved_identity_ids),
                    "moved_conversations": len(plan.moved_conversation_ids)},
        )
    )
    ev = Event(
        workspace_id=workspace_id,
        type="contact.merged",
        actor=Actor(type="member" if actor_member_id else "system", id=actor_member_id),
        contact_id=target_id,
        payload={
            "merge_id": str(merge.id),
            "target_contact_id": str(target_id),
            "source_contact_id": str(source_id),
            "undo": False,
        },
    )
    await event_bus.emit(session, ev)
    await session.flush()
    return merge, [ev]


async def unmerge_contacts(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    merge_id: uuid.UUID,
    actor_member_id: uuid.UUID | None,
    now: datetime | None = None,
) -> tuple[ContactMerge, list[Event]]:
    """Exact replay of the merge snapshot. Only the newest merge in the
    target's chain is undoable."""
    now = now or datetime.now(UTC)
    merge = (
        await session.execute(
            select(ContactMerge)
            .where(ContactMerge.id == merge_id, ContactMerge.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalars().first()
    if merge is None:
        raise MergeError("merge record not found")

    newer = (
        await session.execute(
            select(ContactMerge.id)
            .where(
                ContactMerge.workspace_id == workspace_id,
                ContactMerge.target_contact_id == merge.target_contact_id,
                ContactMerge.undone_at.is_(None),
                ContactMerge.id > merge.id,  # UUIDv7 = time-ordered
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    target = (
        await session.execute(
            select(Contact)
            .where(Contact.id == merge.target_contact_id, Contact.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalars().first()
    source = (
        await session.execute(
            select(Contact)
            .where(Contact.id == merge.source_contact_id, Contact.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalars().first()
    if target is None or source is None:
        raise MergeError("merged contacts no longer exist")

    plan = plan_unmerge(
        merge,
        is_latest_for_target=newer is None,
        target_merged_away=target.merged_into_id is not None,
    )

    if plan.identity_ids:
        await session.execute(
            update(ChannelIdentity)
            .where(ChannelIdentity.id.in_(plan.identity_ids))
            .values(contact_id=plan.source_id)
        )
    if plan.conversation_ids:
        await session.execute(
            update(Conversation)
            .where(Conversation.id.in_(plan.conversation_ids))
            .values(contact_id=plan.source_id)
        )
    revert_field_overwrites(target, plan.field_overwrites)
    if plan.tag_ids:
        await session.execute(
            update(ContactTag)
            .where(ContactTag.contact_id == plan.target_id, ContactTag.tag_id.in_(plan.tag_ids))
            .values(contact_id=plan.source_id)
        )
    if plan.note_ids:
        await session.execute(
            update(ContactNote)
            .where(ContactNote.id.in_(plan.note_ids))
            .values(contact_id=plan.source_id)
        )
    if plan.order_ids:
        await session.execute(
            update(ContactOrder)
            .where(ContactOrder.id.in_(plan.order_ids))
            .values(contact_id=plan.source_id)
        )
    if plan.candidate_ids:
        await session.execute(
            update(ContactMergeCandidate)
            .where(ContactMergeCandidate.id.in_(plan.candidate_ids))
            .values(status="suggested", resolved_at=None)
        )
    source.merged_into_id = None
    merge.undone_at = now
    merge.undone_by_member_id = actor_member_id

    session.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_type="member" if actor_member_id else "system",
            actor_id=actor_member_id,
            action="contacts.unmerge",
            target_type="contact",
            target_id=str(plan.target_id),
            detail={"merge_id": str(merge.id), "source_contact_id": str(plan.source_id)},
        )
    )
    ev = Event(
        workspace_id=workspace_id,
        type="contact.merged",
        actor=Actor(type="member" if actor_member_id else "system", id=actor_member_id),
        contact_id=plan.target_id,
        payload={
            "merge_id": str(merge.id),
            "target_contact_id": str(plan.target_id),
            "source_contact_id": str(plan.source_id),
            "undo": True,
        },
    )
    await event_bus.emit(session, ev)
    await session.flush()
    return merge, [ev]


# ==========================================================================
# 重複聯絡人 candidate generation (call after identity upsert / contact edit)
# ==========================================================================
async def _logged_in_ids(session: AsyncSession, contact_id: uuid.UUID) -> set[str]:
    return {
        v
        for v in (
            await session.execute(
                select(ChannelIdentity.logged_in_external_id).where(
                    ChannelIdentity.contact_id == contact_id,
                    ChannelIdentity.logged_in_external_id.is_not(None),
                )
            )
        ).scalars()
        if v
    }


async def generate_merge_candidates(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    contact_id: uuid.UUID,
) -> int:
    """Find duplicate-contact suggestions for one contact (phone / email /
    merchant logged-in id). INSERT ON CONFLICT DO NOTHING keeps re-runs cheap
    and idempotent. Returns number of new suggestion rows."""
    contact = await session.get(Contact, contact_id)
    if contact is None or contact.workspace_id != workspace_id or contact.merged_into_id:
        return 0

    others: dict[uuid.UUID, Contact] = {}
    if not _empty(contact.phone):
        for row in (
            await session.execute(
                select(Contact).where(
                    Contact.workspace_id == workspace_id,
                    Contact.phone == contact.phone,
                    Contact.id != contact_id,
                    Contact.merged_into_id.is_(None),
                )
            )
        ).scalars():
            others[row.id] = row
    if not _empty(contact.email):
        for row in (
            await session.execute(
                select(Contact).where(
                    Contact.workspace_id == workspace_id,
                    Contact.email == contact.email,
                    Contact.id != contact_id,
                    Contact.merged_into_id.is_(None),
                )
            )
        ).scalars():
            others[row.id] = row
    my_logged = await _logged_in_ids(session, contact_id)
    if my_logged:
        peer_ids = [
            cid
            for cid in (
                await session.execute(
                    select(ChannelIdentity.contact_id)
                    .where(
                        ChannelIdentity.workspace_id == workspace_id,
                        ChannelIdentity.logged_in_external_id.in_(my_logged),
                        ChannelIdentity.contact_id != contact_id,
                    )
                    .distinct()
                )
            ).scalars()
        ]
        for row in (
            await session.execute(
                select(Contact).where(
                    Contact.id.in_(peer_ids), Contact.merged_into_id.is_(None)
                )
            )
        ).scalars():
            others[row.id] = row

    created = 0
    for other in others.values():
        peer_logged = await _logged_in_ids(session, other.id)
        for mt in match_types(contact, other, logged_in_a=my_logged, logged_in_b=peer_logged):
            a, b = ordered_pair(contact_id, other.id)
            stmt = (
                pg_insert(ContactMergeCandidate)
                .values(
                    workspace_id=workspace_id,
                    contact_a_id=a,
                    contact_b_id=b,
                    match_type=mt,
                    status="suggested",
                )
                .on_conflict_do_nothing(
                    index_elements=["workspace_id", "contact_a_id", "contact_b_id", "match_type"]
                )
            )
            res = await session.execute(stmt)
            created += int(res.rowcount or 0)
    return created
