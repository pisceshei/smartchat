"""Split-link (分流連結) target selection — PURE and unit-tested.

Three strategies (plan B.3):
- ``random``       weighted random over eligible targets (alias-table-free; a
                   single uniform draw walks the cumulative weights)
- ``sequential``   lock-free round-robin: ``cursor mod len(eligible)`` (the
                   caller persists the incremented cursor via INCR / rr_cursor)
- ``time_period``  route by the current local time: only targets whose
                   ``time_windows`` contain "now" are eligible, then weighted
                   random among them

Every strategy honours per-target ``enabled`` and ``daily_cap`` (skip a target
that has hit its cap today). Selection is deterministic when an ``rng`` is
injected, so wraparound / weighting / window matching are all testable.

A target is a dict::

    {"channel_account_id": "<uuid>", "weight": 3, "enabled": true,
     "daily_cap": 500, "time_windows": [
        {"days": [0,1,2,3,4], "start": "09:00", "end": "18:00", "tz": "Asia/Hong_Kong"}]}
"""
from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .schedule import get_tz


def _parse_hhmm(value: Any, default: int) -> int:
    """"HH:MM" → minutes since local midnight."""
    if value is None:
        return default
    try:
        h, _, m = str(value).partition(":")
        return int(h) * 60 + (int(m) if m else 0)
    except (ValueError, TypeError):
        return default


def target_open_now(target: dict[str, Any], now: datetime) -> bool:
    """True when ``now`` (UTC) is inside ANY of the target's time windows.
    A target with no ``time_windows`` is always open."""
    windows = target.get("time_windows") or []
    if not windows:
        return True
    now = now if now.tzinfo else now.replace(tzinfo=UTC)
    for w in windows:
        tzinfo = get_tz(w.get("tz"))
        local = now.astimezone(tzinfo)
        days = w.get("days")
        if days and local.weekday() not in [int(d) for d in days]:
            continue
        start = _parse_hhmm(w.get("start"), 0)
        end = _parse_hhmm(w.get("end"), 24 * 60)
        cur = local.hour * 60 + local.minute
        if start == end:
            return True
        if start < end:
            if start <= cur < end:
                return True
        elif cur >= start or cur < end:  # wraps midnight
            return True
    return False


def eligible_indices(
    targets: list[dict[str, Any]],
    *,
    strategy: str,
    now: datetime,
    daily_counts: dict[int, int] | None = None,
) -> list[int]:
    """Indices of targets that may receive a click right now: enabled, under
    their daily cap, and (for time_period) currently inside a window."""
    daily_counts = daily_counts or {}
    out: list[int] = []
    for i, t in enumerate(targets):
        if t.get("enabled") is False:
            continue
        cap = t.get("daily_cap")
        if cap is not None and daily_counts.get(i, 0) >= int(cap):
            continue
        if strategy == "time_period" and not target_open_now(t, now):
            continue
        out.append(i)
    return out


def _weighted_pick(targets: list[dict[str, Any]], indices: list[int], draw: float) -> int:
    """Pick one index from ``indices`` weighted by each target's ``weight``
    (default 1). ``draw`` is a uniform [0,1) value."""
    weights = [max(0.0, float(targets[i].get("weight", 1) or 0)) for i in indices]
    total = sum(weights)
    if total <= 0:
        # all-zero weights → uniform
        pos = min(int(draw * len(indices)), len(indices) - 1)
        return indices[pos]
    threshold = draw * total
    acc = 0.0
    for idx, w in zip(indices, weights, strict=True):
        acc += w
        if threshold < acc:
            return idx
    return indices[-1]


def choose_target(
    targets: list[dict[str, Any]],
    *,
    strategy: str = "random",
    cursor: int = 0,
    now: datetime | None = None,
    daily_counts: dict[int, int] | None = None,
    rng: Callable[[], float] | None = None,
) -> tuple[int | None, int]:
    """Select a target index for one click.

    Returns ``(index, next_cursor)``; ``index`` is None when no target is
    eligible (all disabled / capped / outside every window). ``next_cursor`` is
    the cursor to persist (only advanced for sequential; unchanged otherwise so
    a lock-free INCR stays the single source of monotonicity).
    """
    now = now or datetime.now(UTC)
    if not targets:
        return None, cursor
    idxs = eligible_indices(targets, strategy=strategy, now=now, daily_counts=daily_counts)
    if not idxs:
        return None, cursor
    if strategy == "sequential":
        pos = cursor % len(idxs)
        return idxs[pos], cursor + 1
    draw = (rng or random.random)()
    return _weighted_pick(targets, idxs, draw), cursor
