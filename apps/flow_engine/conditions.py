"""The 7 flow conditions (plan section 4 / B.1).

Each condition node carries ``data.branches`` (a list of branch configs) and
emits ports ``branch:<idx>`` (per branch) + a mandatory ``else``. An evaluator
returns the winning port string; the interpreter follows it.

Pure evaluation helpers are split out (no IO) so trigger matching / tests can
reuse the same matching primitives.
"""
from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from apps.api.app.flows.graph_schema import Node

from .context import ExecutionContext, resolve_path

ELSE_PORT = "else"


def _branches(node: Node) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in node.data.get("branches") or []:
        out.append(b if isinstance(b, dict) else {})
    return out


def _branch_port(idx: int) -> str:
    return f"branch:{idx}"


# ==========================================================================
# pure comparison primitives
# ==========================================================================
def norm(s: Any) -> str:
    """NFKC + casefold — the canonical form for all textual comparison."""
    return unicodedata.normalize("NFKC", str(s)).casefold().strip()


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def compare(op: str, left: Any, right: Any) -> bool:
    """Typed operator table shared by contact_attribute / external_variable."""
    op = (op or "eq").lower()
    if op in ("exists", "is_set"):
        return left is not None and left != "" and left != [] and left != {}
    if op in ("not_exists", "is_empty"):
        return left is None or left == "" or left == [] or left == {}
    if op == "in":
        return norm(left) in {norm(x) for x in _as_list(right)}
    if op == "not_in":
        return norm(left) not in {norm(x) for x in _as_list(right)}
    if op == "contains":
        return norm(right) in norm(left)
    if op == "not_contains":
        return norm(right) not in norm(left)
    if op in ("eq", "equals", "=="):
        return norm(left) == norm(right)
    if op in ("neq", "not_equals", "!="):
        return norm(left) != norm(right)
    # numeric comparisons (fall back to False on non-numeric)
    try:
        lf, rf = float(left), float(right)
    except (TypeError, ValueError):
        return False
    if op in ("gt", ">"):
        return lf > rf
    if op in ("gte", ">="):
        return lf >= rf
    if op in ("lt", "<"):
        return lf < rf
    if op in ("lte", "<="):
        return lf <= rf
    return False


def stable_bucket(seed: str, mod: int = 100) -> int:
    """Deterministic 0..mod-1 bucket from a seed (random_split stability)."""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest, 16) % mod


def weighted_branch(bucket: int, branches: list[dict[str, Any]], mod: int = 100) -> int:
    """Map a bucket to a weighted branch index. Weights need not sum to mod —
    they are normalised proportionally; a zero-weight total splits evenly."""
    weights = [max(0.0, float(b.get("weight", 0) or 0)) for b in branches]
    total = sum(weights)
    if total <= 0:
        # even split
        n = len(branches) or 1
        return min(bucket * n // mod, n - 1)
    acc = 0.0
    threshold = (bucket / mod) * total
    for i, w in enumerate(weights):
        acc += w
        if threshold < acc:
            return i
    return len(branches) - 1


def _minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def in_schedule(now: datetime, windows: list[dict[str, Any]], tz_name: str | None) -> bool:
    """time_schedule: any window {weekday, start_min, end_min} matches in the
    workspace/branch timezone. weekday 0=Mon..6=Sun; empty windows = always."""
    if not windows:
        return True
    try:
        local = now.astimezone(ZoneInfo(tz_name)) if tz_name and tz_name != "UTC" else now.astimezone(UTC)
    except Exception:  # noqa: BLE001 — unknown tz fails open to UTC
        local = now.astimezone(UTC)
    wd = local.weekday()
    minute = _minutes_of_day(local)
    for w in windows:
        weekdays = w.get("weekdays")
        if weekdays is not None:
            if wd not in {int(x) for x in weekdays}:
                continue
        elif "weekday" in w and int(w["weekday"]) != wd:
            continue
        start = int(w.get("start_min", 0))
        end = int(w.get("end_min", 1440))
        if start <= minute < end:
            return True
    return False


# ==========================================================================
# per-type branch matchers
# ==========================================================================
def _match_visitor_language(branch: dict[str, Any], ctx: ExecutionContext) -> bool:
    lang = norm(ctx.contact.language) if ctx.contact and ctx.contact.language else ""
    wanted = {norm(x) for x in _as_list(branch.get("languages") or branch.get("value"))}
    if not wanted:
        return False
    # match on full tag or primary subtag (en-US ~ en)
    primary = lang.split("-")[0]
    return lang in wanted or primary in {w.split("-")[0] for w in wanted}


def _match_country(branch: dict[str, Any], ctx: ExecutionContext) -> bool:
    country = norm(ctx.contact.country) if ctx.contact and ctx.contact.country else ""
    wanted = {norm(x) for x in _as_list(branch.get("countries") or branch.get("value"))}
    return bool(wanted) and country in wanted


def _match_device(branch: dict[str, Any], ctx: ExecutionContext) -> bool:
    device = norm(ctx.contact.device) if ctx.contact and ctx.contact.device else ""
    wanted = {norm(x) for x in _as_list(branch.get("devices") or branch.get("value"))}
    return bool(wanted) and device in wanted


def _match_time_schedule(branch: dict[str, Any], ctx: ExecutionContext, tz: str | None) -> bool:
    windows = branch.get("windows") or []
    branch_tz = branch.get("timezone") or tz
    return in_schedule(ctx.now, windows, branch_tz)


def _match_attribute(branch: dict[str, Any], ctx: ExecutionContext) -> bool:
    ns = ctx.namespaces()
    field = branch.get("field") or branch.get("path") or ""
    # a bare field name resolves against the contact namespace
    if field and "." not in field:
        field = f"contact.{field}"
    left = resolve_path(ns, field)
    return compare(branch.get("op", "eq"), left, branch.get("value"))


def _match_external_var(branch: dict[str, Any], ctx: ExecutionContext) -> bool:
    ns = ctx.namespaces()
    path = branch.get("path") or branch.get("field") or ""
    left = resolve_path(ns, path)
    return compare(branch.get("op", "eq"), left, branch.get("value"))


# ==========================================================================
# public evaluator
# ==========================================================================
def evaluate(node: Node, ctx: ExecutionContext, *, workspace_tz: str | None = None) -> str:
    """Return the output port to follow for a condition node."""
    ntype = node.type
    branches = _branches(node)

    if ntype == "random_branch":
        seed = f"{ctx.flow_session.id}:{node.id}"
        bucket = stable_bucket(seed)
        idx = weighted_branch(bucket, branches) if branches else 0
        ctx.set_var(f"_split_{node.id}", {"bucket": bucket, "branch": idx})
        return _branch_port(idx) if branches else ELSE_PORT

    for idx, branch in enumerate(branches):
        matched = False
        if ntype == "visitor_language":
            matched = _match_visitor_language(branch, ctx)
        elif ntype == "country":
            matched = _match_country(branch, ctx)
        elif ntype == "device":
            matched = _match_device(branch, ctx)
        elif ntype == "time_schedule":
            matched = _match_time_schedule(branch, ctx, workspace_tz)
        elif ntype == "contact_attribute":
            matched = _match_attribute(branch, ctx)
        elif ntype == "external_variable":
            matched = _match_external_var(branch, ctx)
        if matched:
            return _branch_port(idx)
    return ELSE_PORT
