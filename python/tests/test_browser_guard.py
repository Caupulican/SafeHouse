"""Tests for the no-browsing kill lane.

Three of these are safety properties rather than behaviour checks, and they are the
reason the module has tests at all:

* an image-name match that is not exact takes WebView2 (and with it Windows Widgets,
  Outlook and Teams) down with Edge;
* a kill list that grows a Chromium helper takes Steam down with it;
* a run that finds nothing and still writes, logs or spawns is a task firing 1,440 times
  a day on an idle machine - the condition the parent attached to this task shape.

All three failures look like a working guard in the log.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from safehouse_ytbudget import browser_guard, killdelay
from safehouse_ytbudget.browser_guard import (
    BROWSER_PROCESSES,
    DEFAULT_MESSAGE,
    KILL_HISTORY_MAX,
    NEVER_KILL,
    STORM_KILLS,
    STORM_WINDOW_SEC,
    GuardConfig,
    GuardState,
    decide,
    kill_processes,
    load_config,
    load_state,
    running_browsers,
    save_state,
    suppressed,
)

NOW = 1_700_000_000


def fake_runner(table):
    """`args` -> (rc, output), matched on any substring of the joined command line."""
    calls = []

    def runner(args, timeout=30):
        calls.append(args)
        for key, result in table.items():
            if key in " ".join(args):
                return result
        return 0, ""

    runner.calls = calls
    return runner


def csv_row(name, pid="1234", user="DESKTOP-ABC\\child", title="N/A"):
    return (f'"{name}","{pid}","Console","1","250,000 K","Running",'
            f'"{user}","0:00:12","{title}"\r\n')


def tasklist(*rows):
    return fake_runner({"tasklist": (0, "".join(rows))})


# A machine mid-game: Steam and Epic running their embedded Chromium, Roblox up, Edge
# resident as WebView2 for Widgets/Outlook - and not one actual browser open.
QUIET_MACHINE = (
    csv_row("msedgewebview2.exe"),
    csv_row("steamwebhelper.exe"),
    csv_row("EpicWebHelper.exe"),
    csv_row("RobloxPlayerBeta.exe"),
    csv_row("explorer.exe"),
)


# --- the load-bearing safety properties ---------------------------------------

def test_msedge_does_not_match_msedgewebview2():
    # A machine with WebView2 up and no browser open must read as no browser open. A
    # startswith/substring test here would end Widgets, Outlook and Teams every time the
    # child opened nothing at all.
    assert running_browsers(tasklist(csv_row("msedgewebview2.exe"))) == []
    assert running_browsers(tasklist(*QUIET_MACHINE)) == []


def test_embedded_chromium_helpers_are_never_in_the_kill_list():
    assert not set(NEVER_KILL) & set(BROWSER_PROCESSES)
    # steamwebhelper.exe, EpicWebHelper.exe and RobloxPlayerBeta.exe are how the child
    # reaches his games, so no sweep may match a row that names one of them.
    for helper in NEVER_KILL:
        row = csv_row(helper)
        for browser in BROWSER_PROCESSES:
            assert f'"{browser}"' not in row, f"{browser} would match {helper}"


def test_steam_and_roblox_survive_a_sweep_that_finds_a_browser():
    up = running_browsers(tasklist(*QUIET_MACHINE, csv_row("chrome.exe")))
    assert up == ["chrome.exe"]
    _, decision = decide(GuardConfig(), GuardState(), running=tuple(up), now=NOW)
    # Chrome closes; the game launchers it was sitting next to do not.
    assert decision.kill == ("chrome.exe",)
    assert not set(decision.kill) & set(NEVER_KILL)


def test_the_sweep_is_one_process_not_one_per_browser():
    # Every `/FI` enumerates the whole process table before filtering it, so nine
    # filtered calls do the same work nine times and pay nine spawns for it. This is the
    # idle path, on a machine whose job is running games.
    runner = tasklist(*QUIET_MACHINE)
    running_browsers(runner)
    assert runner.calls == [["tasklist", "/FO", "CSV", "/NH"]]


# --- the one-shot: an idle run must do nothing at all -------------------------

def stub_observer(*answers):
    """A `running_browsers` stand-in: one answer per call, the last one repeating."""
    calls = []

    def observer(users=()):
        calls.append(tuple(users))
        return list(answers[min(len(calls) - 1, len(answers) - 1)])

    observer.calls = calls
    return observer


def explode(*a, **kw):
    raise AssertionError(f"must not be called: {a} {kw}")


def arm(monkeypatch, tmp_path, *answers, delay=(0, 0), **config):
    """Point the guard at `tmp_path` with a scripted sequence of observations.

    The pause is configured to zero so the tests never actually sleep, while still
    running the real `killdelay.pause_and_recheck` - including its second observation,
    which is the whole point of the pause.
    """
    killed = []
    observer = stub_observer(*answers)
    monkeypatch.setattr(browser_guard, "running_browsers", observer)
    monkeypatch.setattr(browser_guard, "kill_processes",
                        lambda names, *a, **kw: killed.append(tuple(names)) or [])
    monkeypatch.setattr(browser_guard, "notify", lambda *a, **kw: None)
    if delay is not None or config:
        payload = {"delay_min_sec": delay[0], "delay_max_sec": delay[1]}
        payload.update(config)
        (tmp_path / "browser-guard.json").write_text(json.dumps(payload))
    return observer, killed


def contents(root):
    return sorted(p.name for p in root.iterdir())


def test_an_idle_run_writes_nothing_logs_nothing_and_kills_nothing(tmp_path, monkeypatch):
    # The condition the parent attached to a task that fires every minute: a run that
    # finds nothing running must observe and exit. No log line, no state file, no
    # notification, no second subprocess.
    observer = stub_observer([])
    monkeypatch.setattr(browser_guard, "running_browsers", observer)
    monkeypatch.setattr(browser_guard, "kill_processes", explode)
    monkeypatch.setattr(browser_guard, "notify", explode)
    monkeypatch.setattr(browser_guard, "save_state", explode)

    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == []          # not even the log was created
    assert len(observer.calls) == 1          # observed once, then gone


def test_a_broken_config_stays_silent_while_nothing_is_running(tmp_path, monkeypatch):
    # The warning paths are part of "does nothing": a typo would otherwise write the same
    # line 1,440 times a day on a machine nobody browsed on.
    (tmp_path / "browser-guard.json").write_text("{broken")
    (tmp_path / "browser-guard.state.json").write_text("{also broken")
    monkeypatch.setattr(browser_guard, "running_browsers", stub_observer([]))
    monkeypatch.setattr(browser_guard, "save_state", explode)

    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == ["browser-guard.json", "browser-guard.state.json"]


def test_a_disabled_guard_does_not_even_look(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_guard, "running_browsers", explode)
    (tmp_path / "browser-guard.json").write_text(json.dumps({"enabled": False}))
    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert contents(tmp_path) == ["browser-guard.json"]


def test_a_run_that_finds_a_browser_kills_it_and_persists(tmp_path, monkeypatch):
    observer, killed = arm(monkeypatch, tmp_path, ["chrome.exe"])
    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert killed == [("chrome.exe",)]
    assert len(observer.calls) == 2  # once to notice it, once after the pause
    log = (tmp_path / "browser-guard.log").read_text()
    assert "[wait] chrome.exe is open" in log
    assert "[kill] ending chrome.exe" in log
    assert json.loads((tmp_path / "browser-guard.state.json").read_text())["kills"]


def test_a_browser_closed_during_the_pause_is_not_killed(tmp_path, monkeypatch):
    _, killed = arm(monkeypatch, tmp_path, ["msedge.exe"], [])
    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert killed == []
    log = (tmp_path / "browser-guard.log").read_text()
    assert "[stand-down] closed during the pause" in log
    assert "[kill]" not in log
    # Nothing was killed, so nothing outlived the run.
    assert "browser-guard.state.json" not in contents(tmp_path)


def test_a_browser_opened_during_the_pause_dies_with_the_first(tmp_path, monkeypatch):
    _, killed = arm(monkeypatch, tmp_path, ["msedge.exe"], ["firefox.exe", "msedge.exe"])
    browser_guard.main(["--root", str(tmp_path)])
    assert killed == [("firefox.exe", "msedge.exe")]


def test_dry_run_observes_and_logs_but_neither_kills_nor_persists(tmp_path, monkeypatch):
    _, killed = arm(monkeypatch, tmp_path, ["opera_gx.exe"])
    assert browser_guard.main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert killed == []
    assert "[dry-run] [kill] ending opera_gx.exe" in (
        tmp_path / "browser-guard.log").read_text()
    assert "browser-guard.state.json" not in contents(tmp_path)


def test_an_active_unlock_skips_the_run_entirely(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_guard, "running_browsers", explode)
    save_state(tmp_path / "browser-guard.state.json",
               GuardState(unlock_until_ts=2_000_000_000))
    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert "browser-guard.log" not in contents(tmp_path)


# --- the pause ----------------------------------------------------------------

def test_the_pause_always_lands_inside_the_configured_window():
    cfg = GuardConfig()
    assert (cfg.delay_min_sec, cfg.delay_max_sec) == (1, 5)
    for _ in range(500):
        delay, _ = killdelay.pause_and_recheck(
            lambda: (), low=cfg.delay_min_sec, high=cfg.delay_max_sec,
            sleep=lambda s: None)
        assert cfg.delay_min_sec <= delay <= cfg.delay_max_sec


def test_the_pause_is_slept_and_then_re_observed_in_that_order():
    events = []
    delay, still = killdelay.pause_and_recheck(
        lambda: events.append("observe") or ("chrome.exe",),
        low=3, high=3, announce=lambda d: events.append(f"announce {d}"),
        sleep=lambda s: events.append(f"sleep {s}"))
    assert delay == 3
    assert still == ("chrome.exe",)
    assert events == ["announce 3", "sleep 3", "observe"]


def test_the_pause_ceiling_stays_under_the_tasks_execution_limit():
    # GUARD_TASK_XML carries ExecutionTimeLimit PT5M. A pause the scheduler terminates
    # mid-run is a browser that survives, so the configurable ceiling must stay well
    # under it.
    installer = (Path(__file__).resolve().parents[1] / "install.py").read_text()
    assert "<ExecutionTimeLimit>PT5M</ExecutionTimeLimit>" in installer
    assert killdelay.MAX_DELAY_SEC < 5 * 60


def test_the_browser_task_is_the_repeating_one_shot_not_the_resident_loop():
    installer = (Path(__file__).resolve().parents[1] / "install.py").read_text()
    body = installer.split("def register_browser_guard")[1].split("\ndef ")[0]
    assert "xml_template=GUARD_TASK_XML" in body  # PT1M repetition, PT5M, IgnoreNew
    assert "xml_template=TASK_XML" not in body    # never the resident-loop template
    assert "start=False" in body                  # the RegistrationTrigger starts it


# --- the decision -------------------------------------------------------------

def test_the_kill_set_is_what_is_up_after_the_pause():
    state, decision = decide(GuardConfig(), GuardState(),
                             running=("chrome.exe", "msedge.exe"), now=NOW)
    assert decision.kill == ("chrome.exe", "msedge.exe")
    assert decision.notify
    assert len(state.kills) == 2


def test_nothing_left_after_the_pause_stands_the_guard_down():
    state, decision = decide(GuardConfig(), GuardState(), running=(), now=NOW)
    assert decision.kill == ()
    assert decision.stood_down == "closed during the pause"
    assert state == GuardState()  # nothing to persist


def test_disabled_suppresses_the_kill():
    cfg = GuardConfig(enabled=False)
    assert "disabled" in suppressed(cfg, GuardState(), NOW)
    _, decision = decide(cfg, GuardState(), running=("msedge.exe",), now=NOW)
    assert decision.kill == ()
    assert "disabled" in decision.stood_down


def test_an_active_unlock_suppresses_the_kill():
    state = GuardState(unlock_until_ts=NOW + 600)
    assert "600s" in suppressed(GuardConfig(), state, NOW)
    _, decision = decide(GuardConfig(), state, running=("msedge.exe",), now=NOW)
    assert decision.kill == ()
    assert "600s" in decision.stood_down


def test_an_expired_unlock_does_not_suppress_the_kill():
    state = GuardState(unlock_until_ts=NOW - 1)
    assert suppressed(GuardConfig(), state, NOW) is None
    _, decision = decide(GuardConfig(), state, running=("msedge.exe",), now=NOW)
    assert decision.kill == ("msedge.exe",)


# --- respawn storms (Edge Startup Boost, background mode) ---------------------

def test_a_respawn_loop_is_reported_once_and_mutes_the_message():
    # A storm under this task shape is one kill per fire, forever. A single run cannot
    # see that, so the history has to survive between runs - which is the only reason
    # GuardState carries `kills` at all.
    cfg = GuardConfig()
    state = GuardState()
    seen = []
    for i in range(STORM_KILLS + 3):
        state, decision = decide(cfg, state, running=("msedge.exe",), now=NOW + i * 60)
        seen.append(decision)
    assert [d.storm for d in seen if d.storm] == ["msedge.exe"]  # loudly, exactly once
    assert seen[STORM_KILLS - 2].notify  # before the threshold the child is told
    assert not seen[-1].notify           # after it, he is not spammed

    # ... and once the machine goes quiet for a window, the storm is forgotten.
    state, _ = decide(cfg, state, running=(), now=NOW + STORM_WINDOW_SEC * 2)
    assert state.kills == ()


def test_a_child_reopening_a_browser_a_few_times_is_not_a_storm():
    cfg = GuardConfig()
    state = GuardState()
    for i in range(STORM_KILLS - 1):
        state, decision = decide(cfg, state, running=("msedge.exe",), now=NOW + i * 60)
        assert decision.storm is None
        assert decision.notify


def test_the_storm_threshold_is_out_of_reach_of_a_minutes_cadence_alone():
    # The task fires once a minute, so a storm can produce at most one kill per minute.
    # The threshold has to be more than a few minutes of a child double-clicking,
    # otherwise the log accuses Windows of something the child did.
    assert STORM_KILLS >= 10
    assert STORM_WINDOW_SEC >= 20 * 60


def test_the_kill_history_survives_a_run_and_is_bounded(tmp_path):
    p = tmp_path / "browser-guard.state.json"
    state = GuardState(unlock_until_ts=NOW + 60,
                       kills=(("msedge.exe", NOW), ("chrome.exe", NOW + 1)))
    save_state(p, state)
    assert load_state(p) == (state, None)

    huge = tuple(("msedge.exe", NOW) for _ in range(KILL_HISTORY_MAX * 3))
    save_state(p, GuardState(kills=huge))
    assert len(load_state(p)[0].kills) == KILL_HISTORY_MAX


# --- config: every defect must still enforce ----------------------------------

def test_the_config_fallbacks_are_the_dataclass_defaults(tmp_path):
    assert load_config(tmp_path / "nope.json") == (GuardConfig(), [])
    p = tmp_path / "browser-guard.json"
    p.write_text("{}")
    assert load_config(p) == (GuardConfig(), [])


def test_polling_is_gone_from_the_config_surface():
    # There is no loop to pace any more; a `poll_sec` left in the dataclass would be a
    # setting a parent could change with no effect whatsoever.
    assert "poll_sec" not in {f.name for f in fields(GuardConfig)}
    p = "python/safehouse_ytbudget/browser_guard.py"
    source = (Path(__file__).resolve().parents[2] / p).read_text()
    assert "poll_sec" not in source
    assert "while True" not in source


def test_corrupt_config_still_enforces(tmp_path):
    p = tmp_path / "browser-guard.json"
    for bad in ("{broken", "[]", "null", '{"enabled": "no"}'):
        p.write_text(bad)
        cfg, warnings = load_config(p)
        assert cfg.enabled, bad
        assert (cfg.delay_min_sec, cfg.delay_max_sec) == (1, 5), bad
        assert warnings, bad


def test_a_valid_config_is_honoured(tmp_path):
    p = tmp_path / "browser-guard.json"
    p.write_text(json.dumps({"enabled": False, "delay_min_sec": 20, "delay_max_sec": 30,
                             "users": ["child", " "], "message": "no web"}))
    cfg, warnings = load_config(p)
    assert cfg == GuardConfig(enabled=False, delay_min_sec=20, delay_max_sec=30,
                              users=("child",), message="no web")
    assert warnings == []


def test_out_of_range_values_are_clamped_not_obeyed(tmp_path):
    p = tmp_path / "browser-guard.json"
    p.write_text(json.dumps({"delay_min_sec": 99999, "delay_max_sec": -5}))
    cfg, warnings = load_config(p)
    assert cfg.delay_min_sec == killdelay.MAX_DELAY_SEC
    assert cfg.delay_max_sec == killdelay.MAX_DELAY_SEC  # raised to the floor, never below
    assert len(warnings) == 3


def test_a_bad_users_value_acts_on_everyone_rather_than_no_one(tmp_path):
    # Failing this one open would mean a guard that silently enforces nothing.
    p = tmp_path / "browser-guard.json"
    p.write_text(json.dumps({"users": "child"}))
    cfg, warnings = load_config(p)
    assert cfg.users == ()
    assert warnings


def test_an_empty_message_falls_back_to_the_default(tmp_path):
    p = tmp_path / "browser-guard.json"
    p.write_text(json.dumps({"message": "   "}))
    cfg, warnings = load_config(p)
    assert cfg.message == DEFAULT_MESSAGE
    assert warnings


# --- the user filter ---------------------------------------------------------

def test_the_user_filter_asks_for_the_owner_column():
    runner = tasklist(csv_row("chrome.exe"))
    assert running_browsers(runner, users=("child",)) == ["chrome.exe"]
    assert runner.calls == [["tasklist", "/V", "/FO", "CSV", "/NH"]]


def test_another_users_browser_is_left_alone():
    runner = tasklist(csv_row("chrome.exe", user="DESKTOP-ABC\\dad"))
    assert running_browsers(runner, users=("child",)) == []


def test_one_users_browser_is_found_among_several_owners():
    runner = tasklist(csv_row("chrome.exe", pid="1", user="DESKTOP-ABC\\dad"),
                      csv_row("chrome.exe", pid="2", user="DESKTOP-ABC\\child"),
                      csv_row("firefox.exe", user="DESKTOP-ABC\\dad"))
    assert running_browsers(runner, users=("child",)) == ["chrome.exe"]


def test_an_unqualified_owner_name_still_matches():
    runner = tasklist(csv_row("chrome.exe", user="child"))
    assert running_browsers(runner, users=("DESKTOP-ABC\\child",)) == ["chrome.exe"]


def test_a_hidden_window_title_does_not_hide_the_owner():
    # A SYSTEM-context `tasklist /V` reports N/A for another session's window title. The
    # owner column is still populated, and it is the only column this reads.
    runner = tasklist(csv_row("chrome.exe", title="N/A"))
    assert running_browsers(runner, users=("child",)) == ["chrome.exe"]


def test_a_window_title_cannot_impersonate_a_browser():
    # The verbose listing is the one place an arbitrary string reaches the CSV, so the
    # owner path compares parsed fields rather than searching the text.
    runner = tasklist(csv_row("notepad.exe", title="chrome.exe"))
    assert running_browsers(runner, users=("child",)) == []


def test_the_default_sweep_does_not_pay_for_the_verbose_listing():
    runner = tasklist(*QUIET_MACHINE)
    running_browsers(runner)
    assert not any("/V" in args for args in runner.calls)


def test_the_user_filter_is_applied_to_both_observations(tmp_path, monkeypatch):
    observer, _ = arm(monkeypatch, tmp_path, ["chrome.exe"], users=["child"])
    browser_guard.main(["--root", str(tmp_path)])
    assert observer.calls == [("child",), ("child",)]


# --- kill + unlock persistence -----------------------------------------------

def test_kill_reports_real_failures_but_not_already_gone():
    runner = fake_runner({
        "/IM msedge.exe": (1, 'ERROR: The process "msedge.exe" not found.'),
        "/IM chrome.exe": (1, "ERROR: Access is denied."),
    })
    failures = kill_processes(("msedge.exe", "chrome.exe"), runner)
    assert len(failures) == 1
    assert "Access is denied" in failures[0]


def test_kill_takes_the_child_processes_with_it():
    runner = fake_runner({})
    kill_processes(("msedge.exe",), runner)
    assert runner.calls == [["taskkill", "/F", "/T", "/IM", "msedge.exe"]]


def test_the_unlock_round_trips(tmp_path):
    p = tmp_path / "browser-guard.state.json"
    save_state(p, GuardState(unlock_until_ts=NOW + 900))
    assert load_state(p) == (GuardState(unlock_until_ts=NOW + 900), None)


def test_no_countdown_survives_a_run(tmp_path):
    # The pause happens inside one run now. A persisted countdown would have meant a
    # browser killed the moment the machine came back from a reboot.
    assert {f.name for f in fields(GuardState)} == {"unlock_until_ts", "kills"}


def test_a_corrupt_state_file_means_no_unlock(tmp_path):
    p = tmp_path / "browser-guard.state.json"
    p.write_text("{broken")
    state, warn = load_state(p)
    assert state.unlock_until_ts == 0
    assert warn is not None


# --- the on-screen message is off by default ---------------------------------
# The parent's instruction is that the child must not be told a control is closing his
# browser. That is a default, not a hard removal, so it is pinned by tests in both
# directions: silent unless explicitly switched on.

def test_notify_defaults_to_off(tmp_path):
    cfg, warnings = load_config(tmp_path / "absent.json")
    assert cfg.notify is False
    assert warnings == []


def test_notify_stays_off_for_a_config_that_does_not_mention_it(tmp_path):
    p = tmp_path / "browser-guard.json"
    p.write_text(json.dumps({"enabled": True, "delay_min_sec": 1, "delay_max_sec": 5}))
    assert load_config(p)[0].notify is False


def test_notify_can_be_switched_back_on(tmp_path):
    p = tmp_path / "browser-guard.json"
    p.write_text(json.dumps({"notify": True}))
    cfg, warnings = load_config(p)
    assert cfg.notify is True and warnings == []


def test_a_non_boolean_notify_stays_silent_and_warns(tmp_path):
    p = tmp_path / "browser-guard.json"
    p.write_text(json.dumps({"notify": "yes"}))
    cfg, warnings = load_config(p)
    assert cfg.notify is False
    assert any("notify" in w for w in warnings)


def test_a_real_kill_puts_nothing_on_screen_by_default(tmp_path, monkeypatch):
    """End to end: a browser is ended and nothing is ever put on screen."""
    _, killed = arm(monkeypatch, tmp_path, ["msedge.exe"])
    monkeypatch.setattr(browser_guard, "notify", explode)  # fails loudly if called
    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert killed == [("msedge.exe",)]
    assert "[kill] ending msedge.exe" in (tmp_path / "browser-guard.log").read_text()


def test_switching_notify_on_does_put_the_message_on_screen(tmp_path, monkeypatch):
    """The other direction: the switch has to actually still work."""
    said = []
    arm(monkeypatch, tmp_path, ["msedge.exe"], notify=True)
    monkeypatch.setattr(browser_guard, "notify", lambda msg, *a, **kw: said.append(msg))
    assert browser_guard.main(["--root", str(tmp_path)]) == 0
    assert said == [DEFAULT_MESSAGE]
