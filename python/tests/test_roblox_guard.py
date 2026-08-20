"""Tests for the Roblox kill lane.

The `advance` cases mirror the YouTube regressions where they apply: reconcile every
run rather than latch, reset by date rather than by timer, and never let a bad config
value widen the budget.

The `main` cases hold down the condition the parent attached to a task that fires every
minute of every day: a run that finds the game closed must do nothing at all - no log
line, no state write, no second subprocess.
"""

from __future__ import annotations

import json
from dataclasses import replace

from safehouse_ytbudget import killdelay, roblox_guard
from safehouse_ytbudget.roblox_guard import (
    DEFAULT_LIMIT_MIN,
    MAX_GAP_SEC,
    PROCESS_NAMES,
    GuardState,
    advance,
    kill_processes,
    load_delay_sec,
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


def test_a_zero_limit_kills_on_the_very_first_sighting():
    # The standing policy is 0 min/day. The first sighting charges nothing, so used_sec
    # is still 0 - and 0 >= 0, so the verdict is kill. If this ever regressed to a strict
    # ">", a zero allowance would mean unlimited Roblox.
    state, kill = adv(GuardState.fresh(TODAY), now=5000, limit_sec=0)
    assert state.used_sec == 0
    assert kill


def test_a_zero_limit_still_lets_a_bonus_grant_play():
    state = GuardState(date=TODAY, used_sec=0, last_running_ts=None, bonus_sec=1800)
    state, kill = adv(state, now=5000, limit_sec=0)
    assert not kill
    state = replace(state, used_sec=1800)
    _, kill = adv(state, now=5060, limit_sec=0)
    assert kill


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


# --- bonus: a today-only grant -----------------------------------------------

def test_bonus_extends_todays_budget():
    state = GuardState(date=TODAY, used_sec=LIMIT + 100, last_running_ts=940,
                       bonus_sec=3600)
    state, kill = adv(state, now=1000)
    assert not kill  # over the base limit, under limit+bonus
    state = GuardState(date=TODAY, used_sec=LIMIT + 3600, last_running_ts=940,
                       bonus_sec=3600)
    _, kill = adv(state, now=1000)
    assert kill


def test_bonus_dies_with_the_date_rollover():
    state = GuardState(date="2026-08-15", used_sec=0, last_running_ts=None,
                       bonus_sec=3600)
    state, _ = adv(state, running=False)
    assert state.bonus_sec == 0


def test_bonus_round_trips_and_defaults_for_old_state_files(tmp_path):
    p = tmp_path / "state.json"
    state = GuardState(date=TODAY, used_sec=5, last_running_ts=None, bonus_sec=1800)
    save_state(p, state)
    assert load_state(p, TODAY)[0].bonus_sec == 1800
    # a pre-bonus state file (no key) must load as zero, not crash
    p.write_text(json.dumps({"date": TODAY, "used_sec": 5, "last_running_ts": None}))
    assert load_state(p, TODAY)[0].bonus_sec == 0


# --- rollover ----------------------------------------------------------------

def test_date_rollover_resets_usage_and_anchor():
    state = GuardState(date="2026-08-15", used_sec=1234, last_running_ts=900)
    state, _ = adv(state, now=1000, running=False)
    assert state == GuardState(date=TODAY, used_sec=0, last_running_ts=None)


# --- config: bad values must never widen the budget --------------------------

def test_the_strict_fallback_is_zero_not_an_hour():
    # An absent or broken config must take the game away, not hand back the hour the
    # default used to be. Raising the allowance is a deliberate edit, never an accident.
    assert DEFAULT_LIMIT_MIN == 0


def test_limit_defaults_when_absent(tmp_path):
    limit, warn = load_limit_sec(tmp_path / "nope.json")
    assert limit == 0
    assert warn is None


def test_limit_honours_a_valid_value(tmp_path):
    p = tmp_path / "roblox-budget.json"
    p.write_text(json.dumps({"limit_min": 90}))
    assert load_limit_sec(p) == (5400, None)


def test_zero_is_a_legitimate_limit_not_a_defect(tmp_path):
    # `{"limit_min": 0}` is the standing policy. It once failed validation and fell
    # through to the 60-minute fallback, which granted exactly what it was written to
    # forbid.
    p = tmp_path / "roblox-budget.json"
    p.write_text(json.dumps({"limit_min": 0}))
    assert load_limit_sec(p) == (0, None)


def test_limit_rejects_garbage_to_the_default(tmp_path):
    p = tmp_path / "roblox-budget.json"
    for bad in ("not json", '{"limit_min": "lots"}',
                '{"limit_min": -5}', '{"limit_min": 99999}', "[]"):
        p.write_text(bad)
        limit, warn = load_limit_sec(p)
        assert limit == 0, bad
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


def csv_row(name, pid="1234"):
    return f'"{name}","{pid}","Console","1","250,000 K"\r\n'


def tasklist(*rows):
    return fake_runner({"tasklist": (0, "".join(rows))})


def test_running_processes_matches_the_quoted_csv_name():
    runner = tasklist(csv_row("explorer.exe"), csv_row("RobloxPlayerBeta.exe"),
                      csv_row("steamwebhelper.exe"))
    assert running_processes(runner) == ["RobloxPlayerBeta.exe"]
    # An empty table is "not running", never a crash.
    assert running_processes(tasklist()) == []
    assert running_processes(
        fake_runner({"tasklist": (0, "INFO: No tasks are running...\r\n")})) == []


def test_the_sweep_is_one_process_not_one_per_image_name():
    # Every `/FI` enumerates the whole process table before filtering it, so three
    # filtered calls do the same work three times and pay three spawns for it. This runs
    # every minute forever and is the ONLY thing an idle run does.
    runner = tasklist(csv_row("explorer.exe"))
    running_processes(runner)
    assert runner.calls == [["tasklist", "/FO", "CSV", "/NH"]]


def test_an_exact_quoted_match_cannot_be_widened_by_a_longer_name():
    # The safety property the browser lane has too: a longer image name that merely
    # contains a shorter one must not match it.
    runner = tasklist(csv_row("NotRobloxPlayerBeta.exeX"))
    assert running_processes(runner) == []
    for name in PROCESS_NAMES:
        assert running_processes(tasklist(csv_row(name))) == [name]


def test_kill_reports_real_failures_but_not_already_gone():
    runner = fake_runner({
        "RobloxPlayerBeta.exe": (1, 'ERROR: The process "RobloxPlayerBeta.exe" not found.'),
        "Windows10Universal.exe": (1, "ERROR: Access is denied."),
    })
    failures = kill_processes(["RobloxPlayerBeta.exe", "Windows10Universal.exe"], runner)
    assert len(failures) == 1
    assert "Access is denied" in failures[0]


# --- the kill pause ----------------------------------------------------------

def test_the_pause_bounds_default_to_one_to_five_seconds(tmp_path):
    assert load_delay_sec(tmp_path / "nope.json") == ((1, 5), [])
    p = tmp_path / "roblox-budget.json"
    p.write_text(json.dumps({"limit_min": 60}))
    assert load_delay_sec(p) == ((1, 5), [])


def test_the_pause_bounds_are_configurable_alongside_the_limit(tmp_path):
    p = tmp_path / "roblox-budget.json"
    p.write_text(json.dumps({"limit_min": 60, "delay_min_sec": 2, "delay_max_sec": 9}))
    assert load_delay_sec(p) == ((2, 9), [])
    assert load_limit_sec(p) == (3600, None)


def test_bad_pause_bounds_warn_and_fall_back_to_a_short_pause(tmp_path):
    p = tmp_path / "roblox-budget.json"
    for bad in ("{broken", "[]", json.dumps({"delay_min_sec": "soon"}),
                json.dumps({"delay_max_sec": True})):
        p.write_text(bad)
        (low, high), warnings = load_delay_sec(p)
        assert (low, high) == (1, 5), bad
        assert warnings, bad
    # Out of range is clamped, never obeyed: a pause longer than the task's
    # ExecutionTimeLimit would be terminated mid-run, and the game would survive.
    p.write_text(json.dumps({"delay_min_sec": 0, "delay_max_sec": 99999}))
    (low, high), warnings = load_delay_sec(p)
    assert (low, high) == (0, killdelay.MAX_DELAY_SEC)
    assert warnings


def test_a_config_written_with_a_bom_is_still_read(tmp_path):
    # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM. It used to make
    # this file unparseable, so a parent who granted an hour from a PowerShell prompt
    # silently got the strict fallback of zero instead.
    p = tmp_path / "roblox-budget.json"
    p.write_text(json.dumps({"limit_min": 60, "delay_max_sec": 3}), encoding="utf-8-sig")
    assert load_limit_sec(p) == (3600, None)
    assert load_delay_sec(p) == ((1, 3), [])


# --- one run of the task, end to end -----------------------------------------

def stub_observer(*answers):
    """A `running_processes` stand-in: one answer per call, the last one repeating."""
    calls = []

    def observer(*a, **kw):
        calls.append(True)
        return list(answers[min(len(calls) - 1, len(answers) - 1)])

    observer.calls = calls
    return observer


def explode(*a, **kw):
    raise AssertionError(f"must not be called: {a} {kw}")


def arm(monkeypatch, tmp_path, *answers, limit_min=0, delay=(0, 0)):
    """Point the lane at `tmp_path` with a scripted sequence of observations.

    The pause is configured to zero so the tests never sleep, while still running the
    real `killdelay.pause_and_recheck` - including its second observation, which is the
    whole point of the pause.
    """
    killed = []
    monkeypatch.setattr(roblox_guard, "running_processes", stub_observer(*answers))
    monkeypatch.setattr(roblox_guard, "kill_processes",
                        lambda names, *a, **kw: killed.append(tuple(names)) or [])
    (tmp_path / "roblox-budget.json").write_text(json.dumps(
        {"limit_min": limit_min, "delay_min_sec": delay[0], "delay_max_sec": delay[1]}))
    return killed


def contents(root):
    return sorted(p.name for p in root.iterdir())


def test_a_run_with_the_game_closed_writes_nothing_and_logs_nothing(tmp_path, monkeypatch):
    # Unconditional persistence here was ~1,440 atomic write-and-rename cycles a day on
    # a machine where the game may never have been opened.
    observer = stub_observer([])
    monkeypatch.setattr(roblox_guard, "running_processes", observer)
    monkeypatch.setattr(roblox_guard, "kill_processes", explode)
    monkeypatch.setattr(roblox_guard, "save_state", explode)

    assert roblox_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == []
    assert len(observer.calls) == 1


def test_a_broken_config_stays_silent_while_the_game_is_closed(tmp_path, monkeypatch):
    (tmp_path / "roblox-budget.json").write_text("{broken")
    (tmp_path / "roblox-budget.state.json").write_text("{also broken")
    monkeypatch.setattr(roblox_guard, "running_processes", stub_observer([]))
    monkeypatch.setattr(roblox_guard, "save_state", explode)

    assert roblox_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == ["roblox-budget.json", "roblox-budget.state.json"]


def test_a_spent_budget_kills_after_the_pause_and_persists(tmp_path, monkeypatch):
    killed = arm(monkeypatch, tmp_path, ["RobloxPlayerBeta.exe"])
    assert roblox_guard.main(["--root", str(tmp_path)]) == 0
    assert killed == [("RobloxPlayerBeta.exe",)]
    log = (tmp_path / "roblox-budget.log").read_text()
    assert "[kill] budget spent" in log and "ending RobloxPlayerBeta.exe in 0s" in log
    assert json.loads((tmp_path / "roblox-budget.state.json").read_text())["used_sec"] == 0


def test_a_game_quit_during_the_pause_is_not_reported_as_a_kill(tmp_path, monkeypatch):
    killed = arm(monkeypatch, tmp_path, ["RobloxPlayerBeta.exe"], [])
    assert roblox_guard.main(["--root", str(tmp_path)]) == 0
    assert killed == []
    log = (tmp_path / "roblox-budget.log").read_text()
    assert "[stand-down] closed during the pause" in log


def test_dry_run_logs_the_kill_but_neither_ends_nor_persists(tmp_path, monkeypatch):
    killed = arm(monkeypatch, tmp_path, ["RobloxPlayerBeta.exe"])
    assert roblox_guard.main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert killed == []
    assert "[dry-run] [kill] budget spent" in (tmp_path / "roblox-budget.log").read_text()
    assert "roblox-budget.state.json" not in contents(tmp_path)


def test_playing_inside_the_budget_samples_and_persists(tmp_path, monkeypatch):
    arm(monkeypatch, tmp_path, ["RobloxPlayerBeta.exe"], limit_min=60)
    assert roblox_guard.main(["--root", str(tmp_path)]) == 0
    assert "[sample] running used=0s/3600s" in (tmp_path / "roblox-budget.log").read_text()
    assert "roblox-budget.state.json" in contents(tmp_path)


def test_a_state_file_that_did_not_change_is_not_rewritten(tmp_path, monkeypatch):
    arm(monkeypatch, tmp_path, ["RobloxPlayerBeta.exe"], limit_min=60)
    roblox_guard.main(["--root", str(tmp_path)])
    state_path = tmp_path / "roblox-budget.state.json"
    before = state_path.read_bytes()

    # The game closes. That IS a change (the anchor clears), so exactly one more write...
    monkeypatch.setattr(roblox_guard, "running_processes", stub_observer([]))
    roblox_guard.main(["--root", str(tmp_path)])
    after = state_path.read_bytes()
    assert after != before

    # ...and then nothing, for as long as the machine stays idle.
    monkeypatch.setattr(roblox_guard, "save_state", explode)
    for _ in range(3):
        assert roblox_guard.main(["--root", str(tmp_path)]) == 0
    assert state_path.read_bytes() == after
