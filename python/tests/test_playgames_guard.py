"""Tests for the no-Play-Games kill lane.

Two of these are safety properties rather than behaviour checks, and they are the reason
the module has tests at all:

* the identity test is a DIRECTORY-prefix match on the full executable path, because the
  product runs as `client.exe`, `Service.exe` and `crosvm.exe` - names so generic that
  matching on them would end other programs' processes;
* the sibling `Play Games Services` directory holds the sign-in/achievements helper
  ordinary Windows games depend on, and it must survive every sweep - structurally (the
  trailing backslash on the prefix) and by name (NEVER_KILL), each strap tested on its
  own.

Both failures look like a working guard in the log.
"""

from __future__ import annotations

import json
from pathlib import Path

from safehouse_ytbudget import playgames_guard
from safehouse_ytbudget.playgames_guard import (
    KILL_HISTORY_MAX,
    KILL_ROOT,
    NEVER_KILL,
    STORM_KILLS,
    STORM_WINDOW_SEC,
    Decision,
    GuardConfig,
    GuardState,
    banned,
    decide,
    kill_processes,
    load_config,
    load_state,
    running_playgames,
    save_state,
    suppressed,
)

NOW = 1_700_000_000

CURRENT = KILL_ROOT + "current\\26.7.1101.2\\"
SERVICES_DIR = "C:\\Program Files\\Google\\Play Games Services\\"

# A machine mid-game: Steam and its Chromium, Roblox, the Play Games Services sign-in
# helper that ordinary games load - and Play Games itself not running.
A_QUIET_MACHINE = [
    (4, "C:\\Windows\\System32\\svchost.exe"),
    (88, "C:\\Windows\\explorer.exe"),
    (101, "C:\\Program Files (x86)\\Steam\\steam.exe"),
    (102, "C:\\Program Files (x86)\\Steam\\bin\\cef\\steamwebhelper.exe"),
    (200, SERVICES_DIR + "GooglePlayGamesServices.exe"),
    (300, "C:\\Users\\child\\AppData\\Local\\Roblox\\Versions\\v-abc\\RobloxPlayerBeta.exe"),
]

# The same machine with Play Games opened: the bootstrapper, the client, its service, and
# the Android-VM shards - fourteen processes, three image names, all under one directory.
PLAY_GAMES_UP = A_QUIET_MACHINE + [
    (500, KILL_ROOT + "Bootstrapper.exe"),
    (501, CURRENT + "client.exe"),
    (502, CURRENT + "Service.exe"),
] + [(510 + i, CURRENT + "emulator\\crosvm.exe") for i in range(11)]


def fake_lister(table):
    """A `list_processes` stand-in that also records how often it was walked."""
    calls = []

    def lister():
        calls.append(True)
        return list(table)

    lister.calls = calls
    return lister


# --- the load-bearing safety properties ---------------------------------------

def test_the_kill_root_is_a_directory_prefix_ending_in_a_backslash():
    # The trailing backslash IS the sibling exclusion: without it, "Play Games" is a
    # string prefix of "Play Games Services" and the sign-in helper dies with the app.
    assert KILL_ROOT.endswith("\\")
    assert SERVICES_DIR.lower().startswith(KILL_ROOT.lower()[:-1])  # the near-miss...
    assert not SERVICES_DIR.lower().startswith(KILL_ROOT.lower())   # ...the backslash stops


def test_play_games_services_never_matches():
    # Strap one: structurally, the sibling directory is outside the prefix.
    assert not banned(SERVICES_DIR + "GooglePlayGamesServices.exe")
    # Strap two: even a path UNDER the kill root with that basename is spared, so a
    # future edit that breaks the prefix still cannot end the sign-in helper.
    assert "googleplaygamesservices.exe" in NEVER_KILL
    assert not banned(KILL_ROOT + "GooglePlayGamesServices.exe")


def test_matching_is_by_full_path_never_by_image_name():
    # client.exe and Service.exe are generic names. The same basename anywhere else on
    # the disk must not match - an image-name kill list here would be a disaster.
    assert banned(CURRENT + "Service.exe")
    assert not banned("C:\\Windows\\System32\\Service.exe")
    assert banned(CURRENT + "client.exe")
    assert not banned("C:\\Games\\SomeMMO\\client.exe")
    assert not banned("C:\\Users\\child\\Downloads\\crosvm.exe")


def test_matching_is_case_insensitive_because_ntfs_is():
    assert banned("c:\\program files\\google\\play games\\CURRENT\\CLIENT.EXE")
    assert not banned(("c:\\program files\\google\\play games services\\"
                       "GOOGLEPLAYGAMESSERVICES.EXE"))


def test_a_sweep_finds_everything_under_the_root_and_nothing_beside_it():
    up = running_playgames(fake_lister(PLAY_GAMES_UP))
    assert len(up) == 14
    assert {pid for pid, _ in up} == {500, 501, 502} | set(range(510, 521))
    # Steam, Roblox and the Services helper were on the same table and survive.
    assert all(path.lower().startswith(KILL_ROOT.lower()) for _, path in up)


def test_a_quiet_machine_reads_as_nothing():
    lister = fake_lister(A_QUIET_MACHINE)
    assert running_playgames(lister) == []
    assert len(lister.calls) == 1  # one in-process walk, no subprocess anywhere


# --- the one-shot: an idle run must do nothing at all -------------------------

def stub_observer(*answers):
    """A `running_playgames` stand-in: one answer per call, the last one repeating."""
    calls = []

    def observer():
        calls.append(True)
        return list(answers[min(len(calls) - 1, len(answers) - 1)])

    observer.calls = calls
    return observer


def explode(*a, **kw):
    raise AssertionError(f"must not be called: {a} {kw}")


def arm(monkeypatch, tmp_path, *answers, delay=(0, 0), **config):
    """Point the guard at `tmp_path` with a scripted sequence of observations.

    The pause is configured to zero so the tests never sleep, while still running the
    real `killdelay.pause_and_recheck` - including its second observation, which is the
    whole point of the pause.
    """
    killed = []
    observer = stub_observer(*answers)
    monkeypatch.setattr(playgames_guard, "running_playgames", observer)
    monkeypatch.setattr(playgames_guard, "kill_processes",
                        lambda targets, *a, **kw: killed.append(tuple(targets)) or [])
    payload = {"delay_min_sec": delay[0], "delay_max_sec": delay[1]}
    payload.update(config)
    (tmp_path / "playgames-guard.json").write_text(json.dumps(payload))
    return observer, killed


def contents(root):
    return sorted(p.name for p in root.iterdir())


def test_an_idle_run_writes_nothing_logs_nothing_and_kills_nothing(tmp_path, monkeypatch):
    # The condition the parent attached to a task that fires every minute: a run that
    # finds nothing must observe and exit. No log line, no state file, no kill - and for
    # this lane not even a subprocess, since the walk is in-process.
    observer = stub_observer([])
    monkeypatch.setattr(playgames_guard, "running_playgames", observer)
    monkeypatch.setattr(playgames_guard, "kill_processes", explode)
    monkeypatch.setattr(playgames_guard, "save_state", explode)

    assert playgames_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == []          # not even the log was created
    assert len(observer.calls) == 1          # observed once, then gone


def test_a_broken_config_stays_silent_while_nothing_is_running(tmp_path, monkeypatch):
    (tmp_path / "playgames-guard.json").write_text("{broken")
    (tmp_path / "playgames-guard.state.json").write_text("{also broken")
    monkeypatch.setattr(playgames_guard, "running_playgames", stub_observer([]))
    monkeypatch.setattr(playgames_guard, "save_state", explode)

    assert playgames_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == ["playgames-guard.json", "playgames-guard.state.json"]


def test_a_disabled_guard_does_not_even_look(tmp_path, monkeypatch):
    monkeypatch.setattr(playgames_guard, "running_playgames", explode)
    (tmp_path / "playgames-guard.json").write_text(json.dumps({"enabled": False}))
    assert playgames_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == ["playgames-guard.json"]


def test_a_run_that_finds_the_app_kills_it_and_persists(tmp_path, monkeypatch):
    up = [(501, CURRENT + "client.exe"), (510, CURRENT + "emulator\\crosvm.exe")]
    observer, killed = arm(monkeypatch, tmp_path, up)
    assert playgames_guard.main(["--root", str(tmp_path)]) == 0
    assert killed == [tuple(sorted(up))]
    assert len(observer.calls) == 2  # once to notice it, once after the pause
    log = (tmp_path / "playgames-guard.log").read_text()
    assert "[wait] Play Games is up (2 process(es): client.exe, crosvm.exe)" in log
    assert "[kill] ending 2 process(es) (client.exe, crosvm.exe)" in log
    assert json.loads((tmp_path / "playgames-guard.state.json").read_text())["kills"]


def test_the_app_closed_during_the_pause_is_not_killed(tmp_path, monkeypatch):
    _, killed = arm(monkeypatch, tmp_path, [(501, CURRENT + "client.exe")], [])
    assert playgames_guard.main(["--root", str(tmp_path)]) == 0
    assert killed == []
    log = (tmp_path / "playgames-guard.log").read_text()
    assert "[stand-down] closed during the pause" in log
    assert "[kill]" not in log
    # Nothing was killed, so nothing outlived the run.
    assert "playgames-guard.state.json" not in contents(tmp_path)


def test_a_process_started_during_the_pause_dies_with_the_first(tmp_path, monkeypatch):
    first = [(501, CURRENT + "client.exe")]
    both = [(501, CURRENT + "client.exe"), (502, CURRENT + "Service.exe")]
    _, killed = arm(monkeypatch, tmp_path, first, both)
    playgames_guard.main(["--root", str(tmp_path)])
    assert killed == [tuple(sorted(both))]


def test_dry_run_observes_and_logs_but_neither_kills_nor_persists(tmp_path, monkeypatch):
    _, killed = arm(monkeypatch, tmp_path, [(500, KILL_ROOT + "Bootstrapper.exe")])
    assert playgames_guard.main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert killed == []
    assert "[dry-run] [kill] ending 1 process(es) (Bootstrapper.exe)" in (
        tmp_path / "playgames-guard.log").read_text()
    assert "playgames-guard.state.json" not in contents(tmp_path)


def test_an_active_unlock_skips_the_run_entirely(tmp_path, monkeypatch):
    monkeypatch.setattr(playgames_guard, "running_playgames", explode)
    save_state(tmp_path / "playgames-guard.state.json",
               GuardState(unlock_until_ts=2_000_000_000))
    assert playgames_guard.main(["--root", str(tmp_path)]) == 0
    assert "playgames-guard.log" not in contents(tmp_path)


# --- the decision -------------------------------------------------------------

def test_the_kill_set_is_what_is_up_after_the_pause():
    running = ((501, CURRENT + "client.exe"), (502, CURRENT + "Service.exe"))
    state, decision = decide(GuardConfig(), GuardState(), running=running, now=NOW)
    assert decision.kill == tuple(sorted(running))
    assert decision.stood_down is None
    # The tally records image basenames, not pids or paths.
    assert {n for n, _ in state.kills} == {"client.exe", "service.exe"}


def test_nothing_left_after_the_pause_stands_the_guard_down():
    state, decision = decide(GuardConfig(), GuardState(), running=(), now=NOW)
    assert decision == Decision(stood_down="closed during the pause")
    assert state == GuardState()  # nothing to persist


def test_disabled_suppresses_the_kill():
    cfg = GuardConfig(enabled=False)
    assert "disabled" in suppressed(cfg, GuardState(), NOW)
    _, decision = decide(cfg, GuardState(),
                         running=((501, CURRENT + "client.exe"),), now=NOW)
    assert decision.kill == ()
    assert "disabled" in decision.stood_down


def test_an_active_unlock_suppresses_the_kill():
    state = GuardState(unlock_until_ts=NOW + 600)
    assert "600s" in suppressed(GuardConfig(), state, NOW)
    _, decision = decide(GuardConfig(), state,
                         running=((501, CURRENT + "client.exe"),), now=NOW)
    assert decision.kill == ()
    assert "600s" in decision.stood_down


def test_an_expired_unlock_does_not_suppress_the_kill():
    state = GuardState(unlock_until_ts=NOW - 1)
    assert suppressed(GuardConfig(), state, NOW) is None
    _, decision = decide(GuardConfig(), state,
                         running=((501, CURRENT + "client.exe"),), now=NOW)
    assert decision.kill == ((501, CURRENT + "client.exe"),)


# --- respawn storms (the service manager, or Bootstrapper) --------------------

def test_eleven_vm_shards_dying_together_are_one_tally_tick_not_eleven():
    # Counting per PROCESS would let a single afternoon of double-clicking read as a
    # storm: one launch is ~11 crosvm.exe shards. The tally is per image per run.
    running = tuple((510 + i, CURRENT + "emulator\\crosvm.exe") for i in range(11))
    state, decision = decide(GuardConfig(), GuardState(), running=running, now=NOW)
    assert decision.storm is None
    assert state.kills == (("crosvm.exe", NOW),)


def test_a_respawn_loop_is_reported_once():
    cfg = GuardConfig()
    state = GuardState()
    seen = []
    for i in range(STORM_KILLS + 3):
        state, decision = decide(cfg, state,
                                 running=((501, CURRENT + "client.exe"),),
                                 now=NOW + i * 60)
        seen.append(decision)
    assert [d.storm for d in seen if d.storm] == ["client.exe"]  # loudly, exactly once

    # ...and once the machine goes quiet for a window, the storm is forgotten.
    state, _ = decide(cfg, state, running=(), now=NOW + STORM_WINDOW_SEC * 2)
    assert state.kills == ()


def test_a_child_reopening_the_app_a_few_times_is_not_a_storm():
    cfg = GuardConfig()
    state = GuardState()
    for i in range(STORM_KILLS - 1):
        state, decision = decide(cfg, state,
                                 running=((501, CURRENT + "client.exe"),),
                                 now=NOW + i * 60)
        assert decision.storm is None


def test_the_kill_history_survives_a_run_and_is_bounded(tmp_path):
    p = tmp_path / "playgames-guard.state.json"
    state = GuardState(unlock_until_ts=NOW + 60,
                       kills=(("client.exe", NOW), ("crosvm.exe", NOW + 1)))
    save_state(p, state)
    assert load_state(p) == (state, None)

    huge = tuple(("client.exe", NOW) for _ in range(KILL_HISTORY_MAX * 3))
    save_state(p, GuardState(kills=huge))
    assert len(load_state(p)[0].kills) == KILL_HISTORY_MAX


def test_a_corrupt_state_file_means_no_unlock(tmp_path):
    p = tmp_path / "playgames-guard.state.json"
    p.write_text("{broken")
    state, warn = load_state(p)
    assert state.unlock_until_ts == 0
    assert warn is not None


# --- config: every defect must still enforce ----------------------------------

def test_the_config_fallbacks_are_the_dataclass_defaults(tmp_path):
    assert load_config(tmp_path / "nope.json") == (GuardConfig(), [])
    p = tmp_path / "playgames-guard.json"
    p.write_text("{}")
    assert load_config(p) == (GuardConfig(), [])


def test_corrupt_config_still_enforces(tmp_path):
    p = tmp_path / "playgames-guard.json"
    for bad in ("{broken", "[]", "null", '{"enabled": "no"}'):
        p.write_text(bad)
        cfg, warnings = load_config(p)
        assert cfg.enabled, bad
        assert (cfg.delay_min_sec, cfg.delay_max_sec) == (1, 5), bad
        assert warnings, bad


def test_a_valid_config_is_honoured(tmp_path):
    p = tmp_path / "playgames-guard.json"
    p.write_text(json.dumps({"enabled": False, "delay_min_sec": 10,
                             "delay_max_sec": 20}))
    assert load_config(p) == (GuardConfig(enabled=False, delay_min_sec=10,
                                          delay_max_sec=20), [])


def test_out_of_range_delays_are_clamped_not_obeyed(tmp_path):
    from safehouse_ytbudget import killdelay

    p = tmp_path / "playgames-guard.json"
    p.write_text(json.dumps({"delay_min_sec": 99999, "delay_max_sec": -5}))
    cfg, warnings = load_config(p)
    assert cfg.delay_min_sec == killdelay.MAX_DELAY_SEC
    assert cfg.delay_max_sec == killdelay.MAX_DELAY_SEC  # raised to the floor, never below
    assert len(warnings) == 3


# --- the kill is one invocation, by pid ---------------------------------------

def test_the_kill_is_one_taskkill_by_pid_with_tree():
    calls = []

    def runner(args, timeout=30):
        calls.append(args)
        return 0, "SUCCESS"

    targets = ((501, CURRENT + "client.exe"), (510, CURRENT + "emulator\\crosvm.exe"))
    assert kill_processes(targets, runner) == []
    # ONE spawn for the whole set, /T so the VM children die with their parents, and by
    # PID - never /IM, which would reach past the directory the identity test confined
    # the match to.
    assert calls == [["taskkill", "/F", "/T", "/PID", "501", "/PID", "510"]]


def test_kill_reports_real_failures_but_not_already_gone():
    gone = lambda args, timeout=30: (128, 'ERROR: The process "501" not found.')
    denied = lambda args, timeout=30: (1, "ERROR: Access is denied.")
    assert kill_processes(((501, CURRENT + "client.exe"),), gone) == []
    failures = kill_processes(((501, CURRENT + "client.exe"),), denied)
    assert len(failures) == 1 and "Access is denied" in failures[0]


def test_an_empty_kill_set_spawns_nothing():
    assert kill_processes((), explode) == []


# --- the task shape -----------------------------------------------------------

def test_the_playgames_task_is_the_repeating_one_shot_not_the_resident_loop():
    installer = (Path(__file__).resolve().parents[1] / "install.py").read_text()
    body = installer.split("def register_playgames_guard")[1].split("\ndef ")[0]
    assert "xml_template=GUARD_TASK_XML" in body  # PT1M repetition, PT5M, IgnoreNew
    assert "xml_template=TASK_XML" not in body    # never the resident-loop template
    assert "start=False" in body                  # the RegistrationTrigger starts it
    assert "-m safehouse_ytbudget.playgames_guard" in body
