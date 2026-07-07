"""Segment predicate-tree → SQL compiler (plan B.3).

The ``definition`` is a nested AND/OR tree whose LEAVES use the exact same
predicate grammar as ``contacts/query`` — so this module REUSES
``contacts.router._compile_predicate`` (whitelisted fields, parameterised,
never raw SQL) rather than reinventing it. A group node is
``{"logic": "and"|"or", "predicates": [...nodes...]}``; a leaf is
``{"field","op","value"}``. An empty definition matches every (non-merged)
contact in the workspace.

``estimate`` runs the ``count(*)`` under a 5s ``statement_timeout``. Dynamic
segments recompile at send time; static segments freeze ``snapshot_ids`` at
creation and iterate those.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select, text, true
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.contacts import Contact
from ..contacts.router import Predicate, _compile_predicate

MAX_DEPTH = 8
DEFAULT_TIMEOUT_MS = 5000
_GROUP_KEYS = ("predicates", "conditions", "children", "rules")


class SegmentDefinitionError(ValueError):
    """Invalid definition tree (router → 422)."""


class EstimateTimeout(Exception):
    """The count exceeded the statement timeout (router → 422 too-complex)."""


def _is_group(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and "field" not in node
        and any(k in node for k in _GROUP_KEYS + ("logic",))
    )


def compile_node(
    workspace_id: uuid.UUID, node: Any, *, depth: int = 0
) -> ColumnElement[bool]:
    """Recursively compile a group/leaf node to a SQLAlchemy boolean."""
    if depth > MAX_DEPTH:
        raise SegmentDefinitionError("definition nested too deep")
    if not node:
        return true()
    if isinstance(node, list):  # bare list ⇒ implicit AND
        conds = [compile_node(workspace_id, c, depth=depth + 1) for c in node]
        return and_(*conds) if conds else true()
    if _is_group(node):
        logic = str(node.get("logic", "and")).lower()
        children: list[Any] = []
        for k in _GROUP_KEYS:
            if isinstance(node.get(k), list):
                children = node[k]
                break
        if not children:
            return true()
        conds = [compile_node(workspace_id, c, depth=depth + 1) for c in children]
        return or_(*conds) if logic == "or" else and_(*conds)
    try:
        pred = Predicate.model_validate(node)
    except Exception as e:  # pydantic ValidationError
        raise SegmentDefinitionError(f"invalid predicate: {e}") from e
    return _compile_predicate(workspace_id, pred)


def compile_definition(workspace_id: uuid.UUID, definition: Any) -> ColumnElement[bool]:
    return compile_node(workspace_id, definition or {})


def _base_conditions(workspace_id: uuid.UUID) -> list[ColumnElement[bool]]:
    # audience excludes ONE-ID tombstones; blacklist/unsubscribe is applied by
    # the send-time suppression pass, not the audience definition.
    return [Contact.workspace_id == workspace_id, Contact.merged_into_id.is_(None)]


def audience_select(workspace_id: uuid.UUID, definition: Any, *, columns: tuple[Any, ...] = (Contact.id,)):
    where = compile_definition(workspace_id, definition)
    return select(*columns).where(*_base_conditions(workspace_id), where)


async def estimate_count(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    definition: Any,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> int:
    """count(*) of matching contacts under a per-statement timeout."""
    where = compile_definition(workspace_id, definition)
    q = select(func.count()).select_from(Contact).where(*_base_conditions(workspace_id), where)

    async def _run() -> int:
        await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
        return int((await session.execute(q)).scalar_one())

    try:
        if session.in_transaction():
            return await _run()
        async with session.begin():
            return await _run()
    except Exception as e:  # asyncpg QueryCanceledError surfaces as DBAPIError
        if "statement timeout" in str(e).lower() or "canceling statement" in str(e).lower():
            raise EstimateTimeout() from e
        raise


async def snapshot_ids(
    session: AsyncSession, workspace_id: uuid.UUID, definition: Any, *, cap: int | None = None
) -> list[str]:
    """Freeze the matching contact ids for a static segment."""
    q = audience_select(workspace_id, definition).order_by(Contact.id)
    if cap is not None:
        q = q.limit(cap)
    rows = (await session.execute(q)).scalars().all()
    return [str(r) for r in rows]


async def iter_audience(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    definition: Any = None,
    static_ids: list[Any] | None = None,
    batch: int = 10_000,
) -> AsyncIterator[list[uuid.UUID]]:
    """Yield contact ids for the audience in batches (plan: 萬級分批物化).
    Static segments iterate their frozen snapshot; dynamic segments keyset-
    paginate the compiled query so a huge audience never loads at once."""
    if static_ids is not None:
        buf: list[uuid.UUID] = []
        for raw in static_ids:
            try:
                buf.append(uuid.UUID(str(raw)))
            except (ValueError, TypeError):
                continue
            if len(buf) >= batch:
                yield buf
                buf = []
        if buf:
            yield buf
        return
    where = compile_definition(workspace_id, definition)
    last: uuid.UUID | None = None
    while True:
        q = select(Contact.id).where(*_base_conditions(workspace_id), where)
        if last is not None:
            q = q.where(Contact.id > last)
        rows = (await session.execute(q.order_by(Contact.id).limit(batch))).scalars().all()
        if not rows:
            return
        yield list(rows)
        last = rows[-1]
        if len(rows) < batch:
            return
