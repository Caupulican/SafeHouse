"""The YouTube schedule: window arithmetic, the strict defaults, and the reconciler.

The failures worth testing are the quiet ones: a window that crosses midnight and stops
applying at 00:00, a schedule file that goes unreadable and silently means "allowed", a
reconciler that logs a line a minute or fights AdGuard while it is down. And the
weakening rule, because it is what decides when the password is asked for again.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from safehouse_panel import collect, controls, schedule
from safehouse_panel.controls import Outcome
from safehouse_panel.i18n import render


def at(year=2026, month=8, day=22, hour=14, minute=30) -> datetime:
    return datetime(year, month, day, hour, minute)


# 2026-08-22 is a Saturday (weekday 5); 2026-08-24 is a Monday (weekday 0).
SAT_1430 = at()
SAT_1330 = at(hour=13)
MON_1430 = at(day=24)

WEEKEND = {"mode": "schedule",
           "windows": [{"days": [5, 6], "start": "14:00", "end": "15:00"}]}


# --- validation ---------------------------------------------------------------

def test_a_good_schedule_is_normalised_with_days_sorted_and_deduped():
    cfg = schedule.validate({"mode": "schedule",
                             "windows": [{"days": [6, 5, 5], "start": "14:00",
                                          "end": "15:00"}]})
    assert cfg == {"mode": "schedule",
                   "windows": [{"days": [5, 6], "start": "14:00", "end": "15:00"}]}


@pytest.mark.parametrize("value", [
    None, [], "blocked",                                   # not even a dict
    {"mode": "banana"},                                    # unknown mode
    {"mode": True},                                        # a bool is not a mode
    {"mode": "schedule", "windows": "sat"},                # windows not a list
    {"mode": "schedule", "windows": [{"days": [], "start": "14:00", "end": "15:00"}]},
    {"mode": "schedule", "windows": [{"days": [7], "start": "14:00", "end": "15:00"}]},
    {"mode": "schedule", "windows": [{"days": [True], "start": "14:00", "end": "15:00"}]},
    {"mode": "schedule", "windows": [{"days": [5], "start": "25:00", "end": "15:00"}]},
    {"mode": "schedule", "windows": [{"days": [5], "start": "2pm", "end": "15:00"}]},
    {"mode": "schedule", "windows": [{"days": [5], "start": "14:00", "end": "14:00"}]},
])
def test_a_malformed_schedule_is_refused_not_guessed(value):
    with pytest.raises(schedule.ScheduleError):
        schedule.validate(value)


def test_more_than_the_window_cap_is_refused():
    windows = [{"days": [0], "start": "10:00", "end": "11:00"}] * 9
    with pytest.raises(schedule.ScheduleError) as caught:
        schedule.validate({"mode": "schedule", "windows": windows})
    assert "At most 8" in render(caught.value.payload)


def test_windows_are_kept_whatever_the_mode():
    """Switching to "blocked" for a week must not throw away the windows typed in."""
    cfg = schedule.validate({"mode": "blocked", "windows": WEEKEND["windows"]})
    assert cfg["windows"] == WEEKEND["windows"]


# --- what the clock says ------------------------------------------------------

def test_mode_blocked_and_allowed_ignore_the_clock():
    assert schedule.desired_state({"mode": "blocked", "windows": []}, SAT_1430) == "BLOCKED"
    assert schedule.desired_state({"mode": "allowed", "windows": []}, SAT_1430) == "ALLOWED"


def test_a_window_allows_inside_and_blocks_outside():
    assert schedule.desired_state(WEEKEND, SAT_1430) == "ALLOWED"
    assert schedule.desired_state(WEEKEND, SAT_1330) == "BLOCKED"
    assert schedule.desired_state(WEEKEND, MON_1430) == "BLOCKED"
    # boundaries: start is inside, end is outside
    assert schedule.desired_state(WEEKEND, at(hour=14, minute=0)) == "ALLOWED"
    assert schedule.desired_state(WEEKEND, at(hour=15, minute=0)) == "BLOCKED"


def test_a_window_crossing_midnight_runs_into_the_next_day():
    late = {"mode": "schedule",
            "windows": [{"days": [4], "start": "22:00", "end": "01:00"}]}  # Friday night
    assert schedule.desired_state(late, at(day=21, hour=23, minute=0)) == "ALLOWED"  # Fri
    assert schedule.desired_state(late, at(day=22, hour=0, minute=30)) == "ALLOWED"  # Sat
    assert schedule.desired_state(late, at(day=22, hour=1, minute=0)) == "BLOCKED"
    assert schedule.desired_state(late, at(day=22, hour=23, minute=0)) == "BLOCKED"


def test_a_sunday_window_crossing_midnight_wraps_into_monday():
    cfg = {"mode": "schedule",
           "windows": [{"days": [6], "start": "23:00", "end": "01:00"}]}
    assert schedule.desired_state(cfg, at(day=24, hour=0, minute=30)) == "ALLOWED"  # Mon
    assert schedule.desired_state(cfg, at(day=24, hour=1, minute=30)) == "BLOCKED"


def test_schedule_mode_with_no_windows_is_always_blocked():
    assert schedule.desired_state({"mode": "schedule", "windows": []},
                                  SAT_1430) == "BLOCKED"


# --- all-day windows: days are the primary unit --------------------------------

ALLDAY_WEEKEND = {"mode": "schedule", "windows": [{"days": [5, 6]}]}


def test_a_window_with_no_times_allows_the_whole_day():
    cfg = schedule.validate({"mode": "schedule", "windows": [{"days": [6, 5]}]})
    assert cfg["windows"] == [{"days": [5, 6]}]
    assert schedule.desired_state(cfg, at(hour=0, minute=0)) == "ALLOWED"    # Sat 00:00
    assert schedule.desired_state(cfg, at(hour=23, minute=59)) == "ALLOWED"  # Sat 23:59
    assert schedule.desired_state(cfg, MON_1430) == "BLOCKED"
    state, when = schedule.next_change(cfg, MON_1430)
    assert (state, when) == ("ALLOWED", at(day=29, hour=0, minute=0))


def test_empty_string_times_mean_all_day_too():
    """The page sends what the time inputs hold; empty inputs mean the whole day."""
    cfg = schedule.validate({"mode": "schedule",
                             "windows": [{"days": [5], "start": "", "end": ""}]})
    assert cfg["windows"] == [{"days": [5]}]


def test_one_time_without_the_other_is_refused():
    with pytest.raises(schedule.ScheduleError) as caught:
        schedule.validate({"mode": "schedule",
                           "windows": [{"days": [5], "start": "14:00"}]})
    assert "both times, or neither" in render(caught.value.payload)


# --- the manual override --------------------------------------------------------

def ovr(state, until):
    return {"state": state, "until_ts": int(until.timestamp())}


def test_an_active_override_wins_and_an_expired_one_does_not():
    cfg = {**WEEKEND, "override": ovr("ALLOWED", at(hour=15, minute=0))}
    assert schedule.effective_state(cfg, SAT_1330) == "ALLOWED"
    assert schedule.effective_state(cfg, at(hour=15, minute=1)) == "BLOCKED"
    assert schedule.override_active(cfg, int(SAT_1330.timestamp())) is not None
    assert schedule.override_active(cfg, int(at(hour=15, minute=0).timestamp())) is None


def test_a_malformed_override_is_simply_inactive():
    for bad in (None, "ALLOWED", {"state": "MAYBE", "until_ts": 99},
                {"state": "ALLOWED"}, {"state": "ALLOWED", "until_ts": True}):
        assert schedule.override_active({**WEEKEND, "override": bad}, 0) is None


def test_the_override_expires_at_the_first_boundary_window_edge_or_midnight():
    # Inside the Saturday window: the edge at 15:00 comes before midnight.
    assert schedule.override_until(WEEKEND, SAT_1430) == at(hour=15, minute=0)
    # Friday evening: the next window edge (Sat 14:00) is beyond the day change.
    friday = at(day=21, hour=19, minute=30)
    assert schedule.override_until(WEEKEND, friday) == at(day=22, hour=0, minute=0)
    # No windows at all: the day change is the only boundary there is.
    assert schedule.override_until({"mode": "schedule", "windows": []},
                                   SAT_1430) == at(day=23, hour=0, minute=0)


def test_validate_carries_a_wellformed_override_and_refuses_a_broken_one():
    keep = schedule.validate({**WEEKEND,
                              "override": {"state": "BLOCKED", "until_ts": 123}})
    assert keep["override"] == {"state": "BLOCKED", "until_ts": 123}
    with pytest.raises(schedule.ScheduleError):
        schedule.validate({**WEEKEND, "override": {"state": "BLOCKED"}})


def test_next_change_names_the_opening_and_the_closing():
    state, when = schedule.next_change(WEEKEND, SAT_1330)
    assert (state, when) == ("ALLOWED", at(hour=14, minute=0))
    state, when = schedule.next_change(WEEKEND, SAT_1430)
    assert (state, when) == ("BLOCKED", at(hour=15, minute=0))
    # Monday looks forward across the week to Saturday.
    state, when = schedule.next_change(WEEKEND, MON_1430)
    assert (state, when) == ("ALLOWED", at(day=29, hour=14, minute=0))


def test_next_change_is_none_when_nothing_ever_changes():
    assert schedule.next_change({"mode": "blocked", "windows": []}, SAT_1430) is None
    assert schedule.next_change({"mode": "schedule", "windows": []}, SAT_1430) is None


# --- the weakening rule -------------------------------------------------------

def test_opening_any_minute_that_was_closed_weakens():
    blocked = {"mode": "blocked", "windows": []}
    assert schedule.weakens({"mode": "allowed", "windows": []}, blocked) is True
    assert schedule.weakens(WEEKEND, blocked) is True                    # adds a window
    wider = {"mode": "schedule",
             "windows": [{"days": [5, 6], "start": "14:00", "end": "16:00"}]}
    assert schedule.weakens(wider, WEEKEND) is True                      # widens hours
    more_days = {"mode": "schedule",
                 "windows": [{"days": [0, 5, 6], "start": "14:00", "end": "15:00"}]}
    assert schedule.weakens(more_days, WEEKEND) is True                  # adds a day


def test_closing_minutes_or_standing_still_does_not_weaken():
    assert schedule.weakens(WEEKEND, WEEKEND) is False
    assert schedule.weakens({"mode": "blocked", "windows": []}, WEEKEND) is False
    narrower = {"mode": "schedule",
                "windows": [{"days": [5], "start": "14:00", "end": "14:30"}]}
    assert schedule.weakens(narrower, WEEKEND) is False
    assert schedule.weakens({"mode": "blocked", "windows": []},
                            {"mode": "allowed", "windows": []}) is False


# --- reading the file ---------------------------------------------------------

def test_no_file_means_no_schedule_and_nothing_to_enforce(tmp_path):
    assert controls.read_youtube_schedule(tmp_path) == (None, None)


def test_a_broken_file_falls_back_to_always_blocked_with_a_defect(tmp_path):
    (tmp_path / schedule.SCHEDULE_CONFIG).write_text('{"mode": "sometimes"}',
                                                     encoding="utf-8")
    cfg, defect = controls.read_youtube_schedule(tmp_path)
    assert cfg == {"mode": "blocked", "windows": []}
    assert defect is not None and "always blocked" in render(defect)


def test_a_good_file_reads_back_what_was_written(tmp_path):
    (tmp_path / schedule.SCHEDULE_CONFIG).write_text(json.dumps(WEEKEND),
                                                     encoding="utf-8")
    cfg, defect = controls.read_youtube_schedule(tmp_path)
    assert cfg == WEEKEND and defect is None


# --- the view the page renders ------------------------------------------------

def test_the_view_carries_state_now_and_the_next_change():
    view = schedule.view(WEEKEND, None, now=int(SAT_1330.timestamp()))
    assert view["mode"] == "schedule" and view["desired"] == "BLOCKED"
    assert view["next"]["k"] == "sched.next.allow"
    assert view["next"]["p"]["day"] == {"k": "day.5"} and view["next"]["p"]["t"] == "14:00"
    assert render(view["next"]) == "Opens Sat 14:00"
    assert render(view["next"], "pt") == "Libera sáb 14:00"
    inside = schedule.view(WEEKEND, None, now=int(SAT_1430.timestamp()))
    assert inside["desired"] == "ALLOWED" and inside["next"]["k"] == "sched.next.block"


def test_the_view_with_no_schedule_is_unconfigured_not_invented():
    assert schedule.view(None, None, now=int(SAT_1430.timestamp())) == {
        "configured": False, "mode": None, "windows": [], "desired": None,
        "next": None, "override": None, "defect": None}


def test_the_view_shows_an_active_override_and_its_note():
    until = int(at(hour=15, minute=0).timestamp())
    cfg = {**WEEKEND, "override": {"state": "ALLOWED", "until_ts": until}}
    view = schedule.view(cfg, None, now=int(SAT_1330.timestamp()))
    assert view["desired"] == "ALLOWED", "the override wins over the schedule"
    assert view["override"]["state"] == "ALLOWED"
    assert render(view["override"]["note"], "pt") == (
        "Liberado manualmente - volta ao horário às 15:00")
    expired = schedule.view(cfg, None, now=until + 60)
    assert expired["override"] is None and expired["desired"] == "BLOCKED"


# --- the reconciler -----------------------------------------------------------

class FakeCollector:
    """Just enough of collect.Collector for the reconciler: rules in, applies out."""

    def __init__(self, root, rules):
        self.root = root
        self.rules = rules
        self.applied = []
        self.outcome = Outcome(True, "done")

    def user_rules(self):
        return None if self.rules is None else list(self.rules)

    def apply(self, action, value, *, now=None):
        self.applied.append((action, value))
        return self.outcome


@pytest.fixture()
def logged():
    return []


def reconciler(collector, logged, *, when=SAT_1330):
    return collect.ScheduleReconciler(collector, log=logged.append,
                                      clock=lambda: when.timestamp())


def write_schedule(root, cfg):
    (root / schedule.SCHEDULE_CONFIG).write_text(json.dumps(cfg), encoding="utf-8")


def test_no_schedule_file_means_the_reconciler_does_nothing(tmp_path, logged):
    fake = FakeCollector(tmp_path, ["||youtube.com^"])
    reconciler(fake, logged).tick()
    assert fake.applied == [] and logged == []


def test_a_mismatch_is_applied_and_the_transition_logged(tmp_path, logged):
    write_schedule(tmp_path, WEEKEND)
    fake = FakeCollector(tmp_path, controls.add_youtube_rules([]))  # live BLOCKED
    reconciler(fake, logged, when=SAT_1430).tick()                  # inside the window
    assert fake.applied == [("youtube_blocked", False)]
    assert logged == ["[schedule] youtube: BLOCKED -> ALLOWED "
                      "(window sat,sun 14:00-15:00)"]


def test_outside_the_window_the_block_is_reapplied(tmp_path, logged):
    write_schedule(tmp_path, WEEKEND)
    fake = FakeCollector(tmp_path, [])                              # live ALLOWED
    reconciler(fake, logged, when=SAT_1330).tick()
    assert fake.applied == [("youtube_blocked", True)]
    assert logged == ["[schedule] youtube: ALLOWED -> BLOCKED (outside every window)"]


def test_a_tick_where_nothing_differs_applies_nothing_and_logs_nothing(tmp_path, logged):
    write_schedule(tmp_path, WEEKEND)
    fake = FakeCollector(tmp_path, controls.add_youtube_rules([]))  # live BLOCKED
    made = reconciler(fake, logged, when=SAT_1330)                  # outside the window
    for _ in range(5):
        made.tick()
    assert fake.applied == [] and logged == []


def test_the_manual_modes_are_absolute_and_never_reconciled(tmp_path, logged):
    """A parent who set "blocked" and then lifted the rules by hand - in the AdGuard
    UI, or via apply-filters.py going the other way - must not be fought by a robot.
    Manual modes were applied by the Apply that set them; the reconciler enforces
    only a schedule."""
    write_schedule(tmp_path, {"mode": "blocked", "windows": []})
    fake = FakeCollector(tmp_path, [])                              # live ALLOWED
    made = reconciler(fake, logged)
    made.tick()
    write_schedule(tmp_path, {"mode": "allowed", "windows": []})
    fake.rules = controls.add_youtube_rules([])                     # live BLOCKED
    made.tick()
    assert fake.applied == [] and logged == []


def test_a_manual_mode_change_between_ticks_is_seen_because_the_file_is_reread(
        tmp_path, logged):
    write_schedule(tmp_path, WEEKEND)
    fake = FakeCollector(tmp_path, [])                              # live ALLOWED
    made = reconciler(fake, logged, when=SAT_1330)                  # outside: block it
    made.tick()
    assert fake.applied == [("youtube_blocked", True)]
    write_schedule(tmp_path, {"mode": "allowed", "windows": []})    # manual, mid-flight
    fake.rules = controls.add_youtube_rules([])
    made.tick()
    assert len(fake.applied) == 1, "the fresh read must stop the reconciler cold"


def test_an_active_override_stands_the_reconciler_down_until_it_expires(tmp_path,
                                                                        logged):
    until = at(hour=15, minute=0)
    write_schedule(tmp_path, {**WEEKEND,
                              "override": {"state": "BLOCKED",
                                           "until_ts": int(until.timestamp())}})
    fake = FakeCollector(tmp_path, controls.add_youtube_rules([]))  # live BLOCKED
    made = reconciler(fake, logged, when=SAT_1430)  # inside the window, held blocked
    made.tick()
    assert fake.applied == [] and logged == [], "the hold must be respected"
    made.clock = lambda: at(hour=15, minute=1).timestamp()  # past expiry, still Sat
    made.tick()
    assert fake.applied == [], "at 15:01 the window is over: blocked is correct anyway"
    write_schedule(tmp_path, {**WEEKEND,
                              "override": {"state": "ALLOWED",
                                           "until_ts": int(until.timestamp())}})
    fake.rules = []                                                 # live ALLOWED
    made.tick()
    assert fake.applied == [("youtube_blocked", True)], \
        "an EXPIRED hold no longer protects the state it held"


def test_an_unreachable_adguard_is_logged_once_and_retried(tmp_path, logged):
    write_schedule(tmp_path, WEEKEND)
    fake = FakeCollector(tmp_path, None)                            # no answer
    made = reconciler(fake, logged)
    made.tick()
    made.tick()
    assert fake.applied == []
    assert len(logged) == 1 and "did not answer" in logged[0]
    fake.rules = controls.add_youtube_rules([])                     # it comes back
    made.tick()
    assert len(logged) == 1                                         # quiet again


def test_a_refused_apply_is_logged_once_and_retried(tmp_path, logged):
    write_schedule(tmp_path, WEEKEND)
    fake = FakeCollector(tmp_path, [])                              # live ALLOWED
    fake.outcome = Outcome(False, "AdGuard Home refused the change (ERROR:502).")
    made = reconciler(fake, logged, when=SAT_1330)                  # outside: block it
    made.tick()
    made.tick()
    assert len(fake.applied) == 2, "it must keep retrying"
    assert len(logged) == 1 and "could not make youtube BLOCKED" in logged[0]


def test_a_broken_schedule_file_is_reported_once_and_enforces_nothing(tmp_path, logged):
    """A policy that cannot be read is not a policy a robot may act on: the defect is
    logged (once), the page shows it, and the live rules stay whatever they were."""
    (tmp_path / schedule.SCHEDULE_CONFIG).write_text("{not json", encoding="utf-8")
    fake = FakeCollector(tmp_path, [])                              # live ALLOWED
    made = reconciler(fake, logged)
    made.tick()
    made.tick()
    assert fake.applied == []
    assert sum("could not be read" in line for line in logged) == 1


def test_a_tick_that_blows_up_is_contained_not_fatal(tmp_path, logged):
    fake = FakeCollector(tmp_path, [])
    made = reconciler(fake, logged)
    made.collector = None                                           # guarantee a crash
    made.tick()
    assert len(logged) == 1 and "[schedule] tick failed" in logged[0]
