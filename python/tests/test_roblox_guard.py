"""Tests for the Roblox kill lane.

The `advance` cases mirror the YouTube regressions where they apply: reconcile every
run rather than latch, reset by date rather than by timer, and never let a bad config
value widen the budget.
"""

from __future__ import annotations

import json

from safehouse_ytbudget.roblox_guard import (
    DEFAULT_LIMIT_MIN,
    MAX_GAP_SEC,
    GuardState,
    advance,
    kill_processes,
    load_limit_sec,
    load_state,
    running_processes,
    save_state,
)

TODAY = "2026-08-16"
LIMIT = 3600


def adv(state, **kw):
    base = dict(today=TODAY, now=1000, running=True, limit_sec=LIMIT)
    base.update(kw)
    return advance(state, **base)


# --- accrual -----------------------------------------------------------------

def test_first_sighting_charges_nothing():
    state, kill = adv(GuardState.fresh(TODAY), now=5000)
    assert state.used_sec == 0
    assert state.last_running_ts == 5000
    assert not kill


def test_second_sighting_charges_the_delta():
    state, _ = adv(GuardState.fresh(TODAY), now=5000)
    state, _ = adv(state, now=5060)
    assert state.used_sec == 60


def test_gap_is_capped_so_sleep_is_not_charged():
    state, _ = adv(GuardState.fresh(TODAY), now=5000)
    state, _ = adv(state, now=5000 + 6 * 3600)  # machine slept 6h with the game open
    assert state.used_sec == MAX_GAP_SEC


def test_clock_going_backwards_charges_nothing():
    state, _ = adv(GuardState.fresh(TODAY), now=5000)
    state, _ = adv(state, now=4000)
    assert state.used_sec == 0


def test_not_running_clears_the_anchor_and_charges_nothing():
    state, _ = adv(GuardState.fresh(TODAY), now=5000)
    state, kill = adv(state, now=5060, running=False)
    assert state.used_sec == 0
    assert state.last_running_ts is None
    assert not kill
    # relaunch much later: the off-time is not charged
    state, _ = adv(state, now=9000)
    assert state.used_sec == 0


# --- the kill verdict --------------------------------------------------------

def test_kills_at_the_limit_but_not_under_it():
    state = GuardState(date=TODAY, used_sec=LIMIT - 61, last_running_ts=940)
    state, kill = adv(state, now=1000)
    assert state.used_sec == LIMIT - 1
    assert not kill
    state, kill = adv(state, now=1060)
    assert kill


def test_launch_with_budget_already_spent_is_killed_immediately():
    state = GuardState(date=TODAY, used_sec=LIMIT, last_running_ts=None)
    state, kill = adv(state, now=1000)
    assert kill  # first sighting charges nothing, but the verdict still reconciles


def test_no_kill_when_nothing_is_running():
    state = GuardState(date=TODAY, used_sec=LIMIT * 2, last_running_ts=None)
    _, kill = adv(state, running=False)
    assert not kill


def test_verdict_reconciles_every_run_never_latches():
    # Yesterday ended over budget; today must start clean, not carry the kill.
    state = GuardState(date="2026-08-15", used_sec=LIMIT * 2, last_running_ts=900)
    state, kill = adv(state, now=1000)
    assert state.date == TODAY
    assert state.used_sec == 0
    assert not kill


# --- rollover ----------------------------------------------------------------

def test_date_rollover_resets_usage_and_anchor():
    state = GuardState(date="2026-08-15", used_sec=1234, last_running_ts=900)
    state, _ = adv(state, now=1000, running=False)
    assert state == GuardState(date=TODAY, used_sec=0, last_running_ts=None)


# --- config: bad values must never widen the budget --------------------------

def test_limit_defaults_when_absent(tmp_path):
    limit, warn = load_limit_sec(tmp_path / "nope.json")
    assert limit == DEFAULT_LIMIT_MIN * 60
    assert warn is None


def test_limit_honours_a_valid_value(tmp_path):
    p = tmp_path / "roblox-budget.json"
    p.write_text(json.dumps({"limit_min": 90}))
    assert load_limit_sec(p) == (5400, None)


def test_limit_rejects_garbage_to_the_default(tmp_path):
    p = tmp_path / "roblox-budget.json"
    for bad in ("not json", '{"limit_min": "lots"}', '{"limit_min": 0}',
                '{"limit_min": -5}', '{"limit_min": 99999}', "[]"):
        p.write_text(bad)
        limit, warn = load_limit_sec(p)
        assert limit == DEFAULT_LIMIT_MIN * 60, bad
        assert warn is not None, bad


# --- state persistence -------------------------------------------------------

def test_state_round_trips(tmp_path):
    p = tmp_path / "state.json"
    state = GuardState(date=TODAY, used_sec=42, last_running_ts=999)
    save_state(p, state)
    assert load_state(p, TODAY) == (state, None)


def test_corrupt_state_starts_fresh_and_warns(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{broken")
    state, warn = load_state(p, TODAY)
    assert state == GuardState.fresh(TODAY)
    assert warn is not None


def test_negative_used_sec_is_clamped(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"date": TODAY, "used_sec": -500, "last_running_ts": None}))
    state, _ = load_state(p, TODAY)
    assert state.used_sec == 0


# --- process observation and kill, against a faked runner --------------------

def fake_runner(table):
    calls = []

    def runner(args, timeout=30):
        calls.append(args)
        for key, result in table.items():
            if key in " ".join(args):
                return result
        return 0, ""

    runner.calls = calls
    return runner


def test_running_processes_matches_the_quoted_csv_name():
    runner = fake_runner({
        "RobloxPlayerBeta.exe": (0, '"RobloxPlayerBeta.exe","1234",...\r\n'),
        # tasklist reports "no tasks" with rc 0 and an INFO line - must not match
        "RobloxPlayerLauncher.exe": (0, "INFO: No tasks are running which match...\r\n"),
        "Windows10Universal.exe": (0, "INFO: No tasks are running which match...\r\n"),
    })
    assert running_processes(runner) == ["RobloxPlayerBeta.exe"]


def test_kill_reports_real_failures_but_not_already_gone():
    runner = fake_runner({
        "RobloxPlayerBeta.exe": (1, 'ERROR: The process "RobloxPlayerBeta.exe" not found.'),
        "Windows10Universal.exe": (1, "ERROR: Access is denied."),
    })
    failures = kill_processes(["RobloxPlayerBeta.exe", "Windows10Universal.exe"], runner)
    assert len(failures) == 1
    assert "Access is denied" in failures[0]
