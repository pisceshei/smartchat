"""Routing decision table (plan A.5): ①bot ②AI member ③online humans
(round_robin / least_busy, widget-pinned) ④unassigned. Pure layer — no IO."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from apps.api.app.services.routing import (
    AICandidate,
    HumanCandidate,
    decide_route,
    is_on_shift,
    pick_ai,
    pick_human,
)


def _ai(max_cc: int = 0, load: int = 0, receive: bool = True) -> AICandidate:
    return AICandidate(
        member_id=uuid.uuid4(), max_concurrent=max_cc, current_load=load, receive_enabled=receive
    )


def _human(
    max_cc: int = 0, load: int = 0, online: bool = True, on_shift: bool = True
) -> HumanCandidate:
    return HumanCandidate(
        member_id=uuid.uuid4(),
        max_concurrent=max_cc,
        current_load=load,
        online=online,
        on_shift=on_shift,
    )


# --------------------------------------------------------------------------
# decision table priority
# --------------------------------------------------------------------------
def test_bot_wins_when_available():
    d = decide_route(
        bot_available=True, ai_candidates=[_ai()], human_candidates=[_human()],
    )
    assert d.handler == "bot" and d.member_id is None


def test_prefer_bot_false_skips_bot():
    ai = _ai()
    d = decide_route(
        bot_available=True, ai_candidates=[ai], human_candidates=[_human()], prefer_bot=False,
    )
    assert d.handler == "ai_agent" and d.member_id == ai.member_id


def test_ai_before_humans():
    ai = _ai()
    d = decide_route(bot_available=False, ai_candidates=[ai], human_candidates=[_human()])
    assert d.handler == "ai_agent" and d.member_id == ai.member_id


def test_prefer_ai_false_goes_to_human():
    h = _human()
    d = decide_route(
        bot_available=False, ai_candidates=[_ai()], human_candidates=[h], prefer_ai_member=False,
    )
    assert d.handler == "member" and d.member_id == h.member_id


def test_humans_when_no_ai():
    h = _human()
    d = decide_route(bot_available=False, ai_candidates=[], human_candidates=[h])
    assert d.handler == "member" and d.member_id == h.member_id


def test_unassigned_when_nobody():
    d = decide_route(bot_available=False, ai_candidates=[], human_candidates=[])
    assert d.handler == "unassigned" and d.member_id is None


def test_auto_assign_off_skips_humans():
    d = decide_route(
        bot_available=False, ai_candidates=[], human_candidates=[_human()], auto_assign=False,
    )
    assert d.handler == "unassigned"


# --------------------------------------------------------------------------
# AI eligibility
# --------------------------------------------------------------------------
def test_ai_at_cap_skipped():
    full = _ai(max_cc=2, load=2)
    free = _ai(max_cc=2, load=1)
    assert pick_ai([full, free]) is free


def test_ai_receive_disabled_skipped():
    off = _ai(receive=False)
    on = _ai()
    assert pick_ai([off, on]) is on


def test_ai_zero_cap_means_unlimited():
    ai = _ai(max_cc=0, load=10_000)
    assert pick_ai([ai]) is ai


def test_ai_none_eligible_falls_to_humans():
    h = _human()
    d = decide_route(
        bot_available=False,
        ai_candidates=[_ai(max_cc=1, load=1), _ai(receive=False)],
        human_candidates=[h],
    )
    assert d.handler == "member" and d.member_id == h.member_id


# --------------------------------------------------------------------------
# human eligibility + strategies
# --------------------------------------------------------------------------
def test_offline_and_offshift_and_capped_filtered():
    offline = _human(online=False)
    offshift = _human(on_shift=False)
    capped = _human(max_cc=3, load=3)
    ok = _human(max_cc=3, load=2)
    assert pick_human([offline, offshift, capped, ok]) is ok


def test_round_robin_rotates():
    a, b, c = _human(), _human(), _human()
    pool = [a, b, c]
    picks = [pick_human(pool, strategy="round_robin", rr_counter=i) for i in range(6)]
    assert [p.member_id for p in picks[:3]] == [a.member_id, b.member_id, c.member_id]
    assert [p.member_id for p in picks[3:]] == [a.member_id, b.member_id, c.member_id]


def test_round_robin_skips_ineligible_without_gaps():
    a = _human(online=False)
    b, c = _human(), _human()
    assert pick_human([a, b, c], rr_counter=0) is b
    assert pick_human([a, b, c], rr_counter=1) is c


def test_least_busy_picks_min_load():
    busy = _human(load=7)
    idle = _human(load=1)
    mid = _human(load=3)
    assert pick_human([busy, idle, mid], strategy="least_busy") is idle


def test_pinned_restricts_pool():
    pinned = _human(load=9)
    other = _human(load=0)
    got = pick_human([pinned, other], strategy="least_busy",
                     pinned_member_ids=[pinned.member_id])
    assert got is pinned


def test_pinned_none_eligible_goes_unassigned_not_leaked():
    pinned_offline = _human(online=False)
    other = _human()
    d = decide_route(
        bot_available=False,
        ai_candidates=[],
        human_candidates=[pinned_offline, other],
        pinned_member_ids=[pinned_offline.member_id],
    )
    assert d.handler == "unassigned"


def test_empty_pinned_list_means_no_restriction():
    h = _human()
    assert pick_human([h], pinned_member_ids=[]) is h


# --------------------------------------------------------------------------
# shift math
# --------------------------------------------------------------------------
def test_no_shifts_always_on():
    assert is_on_shift([], datetime(2026, 7, 6, 3, 0, tzinfo=UTC), "UTC")


def test_shift_match_weekday_and_window():
    # 2026-07-06 is a Monday (weekday 0)
    monday_10 = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
    shifts = [(0, 9 * 60, 18 * 60)]
    assert is_on_shift(shifts, monday_10, "UTC")
    assert not is_on_shift(shifts, datetime(2026, 7, 7, 10, 0, tzinfo=UTC), "UTC")  # Tuesday
    assert not is_on_shift(shifts, datetime(2026, 7, 6, 8, 59, tzinfo=UTC), "UTC")


def test_shift_boundaries_start_inclusive_end_exclusive():
    shifts = [(0, 540, 1080)]  # Mon 09:00–18:00
    assert is_on_shift(shifts, datetime(2026, 7, 6, 9, 0, tzinfo=UTC), "UTC")
    assert not is_on_shift(shifts, datetime(2026, 7, 6, 18, 0, tzinfo=UTC), "UTC")


def test_unknown_timezone_fails_open_to_utc():
    shifts = [(0, 0, 1440)]  # all Monday
    assert is_on_shift(shifts, datetime(2026, 7, 6, 12, 0, tzinfo=UTC), "Not/AZone")
