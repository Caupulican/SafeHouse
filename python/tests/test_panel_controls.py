"""The writing half: clamping, atomic config edits, and the YouTube rule filter.

Two of these guard failures that would be discovered by a child, not by a log line:

* a value that is not clamped server-side means a POST of `roblox_limit_min=1440` is a
  whole day of Roblox, and the panel now listens on the house LAN;
* a YouTube rule-removal that is one `in` away from correct takes
  `@@||firebaseinstallations.googleapis.com^` with it, and then the *games* stop working
  - with nothing connecting that to a YouTube toggle.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from safehouse_panel import controls
from safehouse_panel.i18n import render
from safehouse_ytbudget import browser_guard, roblox_guard

REPO = Path(__file__).resolve().parents[2]
NOW = int(datetime(2026, 8, 21, 19, 30).timestamp())
TODAY = "2026-08-21"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- coercion and clamping ---------------------------------------------------

def test_out_of_range_clamps_and_says_so():
    value, note = controls.clamp(1440, controls.ROBLOX_LIMIT_RANGE, field="x")
    assert value == 240
    assert note == {"k": "clamp.high", "p": {"field": "x", "got": 1440, "applied": 240}}
    assert render(note) == "x 1440 is above the maximum, applied 240"
    assert controls.clamp(-5, controls.ROBLOX_LIMIT_RANGE, field="x")[0] == 0
    assert controls.clamp(30, controls.ROBLOX_LIMIT_RANGE, field="x") == (30, None)


@pytest.mark.parametrize("value", ["lots", None, "", 3.5, [1], {"a": 1}])
def test_a_value_that_is_not_a_number_is_refused_not_guessed(value):
    with pytest.raises(controls.ControlError):
        controls.coerce_int(value, field="minutes")


def test_true_is_not_one_minute():
    """`int(True)` is 1, so an unchecked coercion turns a checkbox posted into a minutes
    field into a value nobody chose."""
    with pytest.raises(controls.ControlError):
        controls.coerce_int(True, field="minutes")


def test_numeric_strings_are_accepted_because_forms_send_strings():
    assert controls.coerce_int("45", field="minutes") == 45
    assert controls.coerce_bool("true", field="on") is True
    assert controls.coerce_bool("no", field="on") is False


def test_a_non_boolean_toggle_is_refused():
    with pytest.raises(controls.ControlError):
        controls.coerce_bool("maybe", field="on")


# --- writing the guard configs -----------------------------------------------

def test_setting_the_roblox_limit_keeps_the_rest_of_the_file(tmp_path):
    path = tmp_path / controls.ROBLOX_CONFIG
    path.write_text(json.dumps({"limit_min": 0, "delay_min_sec": 2, "delay_max_sec": 9,
                                "_comment": "hand written"}), encoding="utf-8")
    outcome = controls.set_roblox_limit(tmp_path, 60, now=NOW)
    assert outcome.ok and read(path)["limit_min"] == 60
    assert read(path)["delay_max_sec"] == 9
    assert read(path)["_comment"] == "hand written"


def test_the_written_limit_is_what_the_guard_reads_back(tmp_path):
    """The panel and the guard must agree, or the page reports a budget nothing enforces."""
    controls.set_roblox_limit(tmp_path, 90, now=NOW)
    assert roblox_guard.load_limit_sec(tmp_path / controls.ROBLOX_CONFIG) == (5400, None)


def test_an_absurd_limit_is_clamped_before_it_reaches_the_file(tmp_path):
    outcome = controls.set_roblox_limit(tmp_path, 1440, now=NOW)
    assert read(tmp_path / controls.ROBLOX_CONFIG)["limit_min"] == 240
    assert "above the maximum" in render(outcome.message)


def test_the_bonus_lands_in_the_state_so_it_dies_at_the_rollover(tmp_path):
    path = tmp_path / controls.ROBLOX_STATE
    roblox_guard.save_state(path, roblox_guard.GuardState(date=TODAY, used_sec=600,
                                                          last_running_ts=None))
    outcome = controls.set_roblox_bonus(tmp_path, 20, now=NOW)
    state, _ = roblox_guard.load_state(path, TODAY)
    assert state.bonus_sec == 1200 and state.used_sec == 600
    assert "expires at midnight" in render(outcome.message)
    # ...and the config, which is the standing policy, is untouched.
    assert not (tmp_path / controls.ROBLOX_CONFIG).exists()


def test_granting_a_bonus_on_a_stale_state_starts_today_fresh(tmp_path):
    """Yesterday's usage must not be charged against today's grant."""
    path = tmp_path / controls.ROBLOX_STATE
    roblox_guard.save_state(path, roblox_guard.GuardState(date="2026-08-20",
                                                          used_sec=7200,
                                                          last_running_ts=None))
    controls.set_roblox_bonus(tmp_path, 30, now=NOW)
    state, _ = roblox_guard.load_state(path, TODAY)
    assert (state.date, state.used_sec, state.bonus_sec) == (TODAY, 0, 1800)


def test_the_unlock_is_an_expiring_timestamp_the_guard_understands(tmp_path):
    controls.set_browser_unlock(tmp_path, 15, now=NOW)
    state, _ = browser_guard.load_state(tmp_path / controls.BROWSER_STATE)
    assert state.unlock_until_ts == NOW + 900
    assert browser_guard.suppressed(browser_guard.GuardConfig(), state, NOW) is not None
    assert browser_guard.suppressed(browser_guard.GuardConfig(), state, NOW + 901) is None


def test_unlocking_for_zero_cancels_rather_than_unlocking_forever(tmp_path):
    controls.set_browser_unlock(tmp_path, 15, now=NOW)
    outcome = controls.set_browser_unlock(tmp_path, 0, now=NOW)
    state, _ = browser_guard.load_state(tmp_path / controls.BROWSER_STATE)
    assert state.unlock_until_ts == 0 and "cancelled" in render(outcome.message)


def test_disabling_the_browser_guard_is_what_the_guard_reads(tmp_path):
    controls.set_browser_enabled(tmp_path, False, now=NOW)
    cfg, warnings = browser_guard.load_config(tmp_path / controls.BROWSER_CONFIG)
    assert cfg.enabled is False and not warnings


def test_the_browser_daily_budget_is_written_but_flagged_as_inert(tmp_path):
    outcome = controls.set_browser_limit(tmp_path, 45, now=NOW)
    assert read(tmp_path / controls.BROWSER_CONFIG)["limit_min"] == 45
    assert "NOT ENFORCED YET" in render(outcome.message)
    assert "AINDA NÃO APLICADO" in render(outcome.message, "pt")
    # And the guard still loads cleanly with the unknown key present, unchanged in
    # behaviour - which is the promise made when the field was added.
    cfg, warnings = browser_guard.load_config(tmp_path / controls.BROWSER_CONFIG)
    assert cfg.enabled is True and not warnings


def test_the_kill_delay_is_clamped_to_what_the_scheduler_allows(tmp_path):
    outcome = controls._set_delay(tmp_path / controls.BROWSER_CONFIG,
                                  {"min_sec": 0, "max_sec": 600}, label="Browser")
    written = read(tmp_path / controls.BROWSER_CONFIG)
    assert (written["delay_min_sec"], written["delay_max_sec"]) == (1, 60)
    assert "above the maximum" in render(outcome.message)


def test_an_inverted_delay_range_is_repaired_not_written(tmp_path):
    controls._set_delay(tmp_path / controls.BROWSER_CONFIG,
                        {"min_sec": 20, "max_sec": 5}, label="Browser")
    written = read(tmp_path / controls.BROWSER_CONFIG)
    assert written["delay_min_sec"] == written["delay_max_sec"] == 20


def test_notify_is_written_as_a_real_boolean(tmp_path):
    controls.set_browser_notify(tmp_path, True, now=NOW)
    cfg, warnings = browser_guard.load_config(tmp_path / controls.BROWSER_CONFIG)
    assert cfg.notify is True and not warnings


# --- YouTube user rules ------------------------------------------------------

LIVE_RULES = [
    "! SafeHouse: YouTube blocked (control panel)",
    "||youtube.com^",
    "||youtu.be^",
    "||youtubekids.com^",
    "||youtube-nocookie.com^",
    "||googlevideo.com^",
    "||ytimg.com^",
    "||youtubei.googleapis.com^",
    "||youtube.googleapis.com^",
    "! SafeHouse: allowlist - false positives the games need",
    "@@||firebaseinstallations.googleapis.com^",
    "! SafeHouse: Roblox child-safety hardening",
    "||voice.roblox.com^",
    "||adconfiguration.roblox.com^",
    "||adserver.roblox.com^",
    "||ads.roblox.com^",
    "! a rule the parent typed in the AdGuard UI",
    "||some-game-ads.example^",
]


def test_removing_youtube_keeps_the_firebase_allow_rule():
    """Blocking Firebase Installations makes games fail to launch or sign in. It is an
    allow rule; nothing in a YouTube toggle may touch it."""
    left = controls.remove_youtube_rules(LIVE_RULES)
    assert "@@||firebaseinstallations.googleapis.com^" in left


def test_removing_youtube_keeps_every_roblox_rule():
    left = controls.remove_youtube_rules(LIVE_RULES)
    assert [rule for rule in left if "roblox" in rule] == [
        "||voice.roblox.com^", "||adconfiguration.roblox.com^",
        "||adserver.roblox.com^", "||ads.roblox.com^"]


def test_removing_youtube_keeps_rules_a_parent_added_by_hand():
    left = controls.remove_youtube_rules(LIVE_RULES)
    assert "||some-game-ads.example^" in left
    assert "! a rule the parent typed in the AdGuard UI" in left


def test_removing_youtube_removes_exactly_the_youtube_rules():
    left = controls.remove_youtube_rules(LIVE_RULES)
    assert not [rule for rule in left if rule in controls.YOUTUBE_RULES]
    assert controls.youtube_state(left) == "ALLOWED"


def test_a_hand_written_youtube_rule_is_not_ours_to_remove():
    """Exact-line matching, deliberately: a rule the parent typed - even about YouTube -
    is theirs."""
    rules = ["||m.youtube.com^", "||youtube.com^"]
    assert controls.remove_youtube_rules(rules) == ["||m.youtube.com^"]


def test_adding_is_idempotent_and_appends_nothing_twice():
    once = controls.add_youtube_rules(["||voice.roblox.com^"])
    assert controls.add_youtube_rules(once) == once
    assert controls.youtube_state(once) == "BLOCKED"


def test_add_then_remove_returns_the_list_it_started_with():
    start = ["||voice.roblox.com^", "@@||firebaseinstallations.googleapis.com^"]
    assert controls.remove_youtube_rules(controls.add_youtube_rules(start)) == start


def test_youtube_state_distinguishes_partial_from_allowed_and_unknown():
    assert controls.youtube_state(None) == "UNKNOWN"
    assert controls.youtube_state([]) == "ALLOWED"
    assert controls.youtube_state(["||youtube.com^"]) == "PARTIAL"


def test_the_panel_rules_match_the_repos_own_user_rules_file():
    """One source of truth. adguard/apply-filters.py reconciles user-rules.txt into the
    running instance, so a rule that drifts out of this list is one the panel can never
    remove - it would come straight back and look like the toggle did nothing."""
    payload = (REPO / "adguard" / "user-rules.txt").read_text().partition("\n\n")[2]
    repo_youtube = {line.strip() for line in payload.splitlines()
                    if line.strip().startswith("||")
                    and ("youtu" in line or "googlevideo" in line or "ytimg" in line)}
    assert repo_youtube == set(controls.YOUTUBE_RULES)


class FakeAdGuard:
    def __init__(self, rules, ok=True):
        self.rules = None if rules is None else list(rules)
        self.ok = ok
        self.written = None

    def user_rules(self):
        return list(self.rules) if self.rules is not None else None

    def set_user_rules(self, rules):
        self.written = list(rules)
        if not self.ok:
            return "ERROR:502"
        self.rules = list(rules)
        return "OK"


def test_blocking_youtube_writes_the_merged_list_not_a_fixed_one(tmp_path):
    client = FakeAdGuard(["||voice.roblox.com^"])
    outcome = controls.set_youtube_blocked(tmp_path, True, now=NOW, client=client)
    assert outcome.ok and "||voice.roblox.com^" in client.written
    assert controls.youtube_state(client.rules) == "BLOCKED"


def test_unblocking_reports_that_the_reconciler_will_put_it_back(tmp_path):
    client = FakeAdGuard(LIVE_RULES)
    outcome = controls.set_youtube_blocked(tmp_path, False, now=NOW, client=client)
    assert outcome.ok and "apply-filters.py" in render(outcome.message)
    assert "apply-filters.py" in render(outcome.message, "pt")


def test_a_resolver_that_refuses_is_reported_as_a_failure_not_a_success(tmp_path):
    client = FakeAdGuard(LIVE_RULES, ok=False)
    outcome = controls.set_youtube_blocked(tmp_path, False, now=NOW, client=client)
    assert not outcome.ok
    text = render(outcome.message)
    assert "refused the change" in text and "still BLOCKED" in text


def test_no_resolver_configured_fails_loudly(tmp_path):
    outcome = controls.set_youtube_blocked(tmp_path, True, now=NOW, client=None)
    assert not outcome.ok and "not configured" in render(outcome.message)


def test_an_unreachable_resolver_changes_nothing_and_says_so(tmp_path):
    client = FakeAdGuard(None)
    outcome = controls.set_youtube_blocked(tmp_path, True, now=NOW, client=client)
    assert not outcome.ok and client.written is None


# --- the YouTube schedule control ---------------------------------------------

WEEKEND = {"mode": "schedule",
           "windows": [{"days": [5, 6], "start": "14:00", "end": "15:00"}]}
# NOW is 2026-08-21 19:30, a Friday evening - outside the weekend window.


def read_schedule_file(root) -> dict:
    from safehouse_panel import schedule
    return read(root / schedule.SCHEDULE_CONFIG)


def test_setting_mode_blocked_stores_the_policy_and_blocks_now(tmp_path):
    client = FakeAdGuard([])
    outcome = controls.set_youtube_schedule(tmp_path, {"mode": "blocked", "windows": []},
                                            now=NOW, client=client)
    assert outcome.ok and (outcome.before, outcome.after) == ("unset", "blocked")
    assert read_schedule_file(tmp_path)["mode"] == "blocked"
    assert controls.youtube_state(client.rules) == "BLOCKED"
    assert "always blocked" in render(outcome.message)


def test_setting_mode_allowed_lifts_the_rules_now(tmp_path):
    client = FakeAdGuard(LIVE_RULES)
    outcome = controls.set_youtube_schedule(tmp_path, {"mode": "allowed", "windows": []},
                                            now=NOW, client=client)
    assert outcome.ok and controls.youtube_state(client.rules) == "ALLOWED"
    # ...and the games' allow rule survived, exactly as with the old checkbox.
    assert "@@||firebaseinstallations.googleapis.com^" in client.rules


def test_a_schedule_applies_whatever_its_windows_say_about_right_now(tmp_path):
    client = FakeAdGuard([])
    outcome = controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=client)
    assert outcome.ok and controls.youtube_state(client.rules) == "BLOCKED"
    assert read_schedule_file(tmp_path)["windows"] == WEEKEND["windows"]
    saturday = int(datetime(2026, 8, 22, 14, 30).timestamp())
    outcome = controls.set_youtube_schedule(tmp_path, WEEKEND, now=saturday,
                                            client=client)
    assert outcome.ok and controls.youtube_state(client.rules) == "ALLOWED"


def test_a_malformed_schedule_is_refused_and_nothing_is_written(tmp_path):
    client = FakeAdGuard([])
    with pytest.raises(controls.ControlError):
        controls.set_youtube_schedule(tmp_path, {"mode": "sometimes"}, now=NOW,
                                      client=client)
    assert client.written is None
    from safehouse_panel import schedule
    assert not (tmp_path / schedule.SCHEDULE_CONFIG).exists()


def test_a_failed_manual_mode_saves_nothing_and_says_nothing_changed(tmp_path):
    """Manual modes are absolute and immediate: if AdGuard refused, the mode is NOT
    stored - no robot retries a manual act the parent can simply press again."""
    from safehouse_panel import schedule
    client = FakeAdGuard(LIVE_RULES, ok=False)
    outcome = controls.set_youtube_schedule(tmp_path, {"mode": "allowed", "windows": []},
                                            now=NOW, client=client)
    assert not outcome.ok
    assert not (tmp_path / schedule.SCHEDULE_CONFIG).exists(), "nothing may be saved"
    assert (outcome.before, outcome.after) == ("unset", "unset")
    assert "refused the change" in render(outcome.message)


def test_a_refusing_resolver_still_saves_a_schedule_and_says_the_panel_retries(tmp_path):
    """A schedule is a standing policy: saved even when the apply failed, because the
    reconciler exists to retry it."""
    client = FakeAdGuard([], ok=False)
    outcome = controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=client)
    assert not outcome.ok
    assert read_schedule_file(tmp_path)["mode"] == "schedule", "the policy must be kept"
    text = render(outcome.message)
    assert "retries every minute" in text and "refused the change" in text


def test_setting_any_mode_clears_a_standing_override(tmp_path):
    from safehouse_panel import schedule
    client = FakeAdGuard([])
    controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=client)
    controls.set_youtube_override(tmp_path, {"state": "ALLOWED"}, now=NOW,
                                  client=client)
    assert read_schedule_file(tmp_path)["override"] is not None
    controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=client)
    assert read_schedule_file(tmp_path)["override"] is None


# --- the manual override on top of a schedule ---------------------------------

def test_an_override_needs_a_schedule_to_override(tmp_path):
    outcome = controls.set_youtube_override(tmp_path, {"state": "ALLOWED"}, now=NOW,
                                            client=FakeAdGuard([]))
    assert not outcome.ok and "no active schedule" in render(outcome.message)
    controls.set_youtube_schedule(tmp_path, {"mode": "blocked", "windows": []},
                                  now=NOW, client=FakeAdGuard([]))
    outcome = controls.set_youtube_override(tmp_path, {"state": "ALLOWED"}, now=NOW,
                                            client=FakeAdGuard([]))
    assert not outcome.ok, "a manual mode is not a schedule"


def test_allow_now_applies_immediately_and_expires_at_the_next_boundary(tmp_path):
    client = FakeAdGuard([])
    controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=client)
    assert controls.youtube_state(client.rules) == "BLOCKED"  # Friday evening
    outcome = controls.set_youtube_override(tmp_path, {"state": "ALLOWED"}, now=NOW,
                                            client=client)
    assert outcome.ok and controls.youtube_state(client.rules) == "ALLOWED"
    stored = read_schedule_file(tmp_path)["override"]
    # Friday 19:30 -> the next boundary is the day change at midnight, not Sat 14:00.
    assert stored == {"state": "ALLOWED",
                      "until_ts": int(datetime(2026, 8, 22, 0, 0).timestamp())}
    assert "até 00:00" in render(outcome.message, "pt")


def test_the_override_expiry_prefers_a_window_edge_before_midnight(tmp_path):
    client = FakeAdGuard([])
    saturday = int(datetime(2026, 8, 22, 14, 30).timestamp())  # inside the window
    controls.set_youtube_schedule(tmp_path, WEEKEND, now=saturday, client=client)
    outcome = controls.set_youtube_override(tmp_path, {"state": "BLOCKED"},
                                            now=saturday, client=client)
    assert outcome.ok and controls.youtube_state(client.rules) == "BLOCKED"
    stored = read_schedule_file(tmp_path)["override"]
    assert stored["until_ts"] == int(datetime(2026, 8, 22, 15, 0).timestamp())


def test_a_failed_override_saves_nothing(tmp_path):
    client = FakeAdGuard([])
    controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=client)
    client.ok = False
    outcome = controls.set_youtube_override(tmp_path, {"state": "ALLOWED"}, now=NOW,
                                            client=client)
    assert not outcome.ok
    assert read_schedule_file(tmp_path)["override"] is None


def test_clearing_the_override_returns_to_the_schedule_state(tmp_path):
    client = FakeAdGuard([])
    controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=client)
    controls.set_youtube_override(tmp_path, {"state": "ALLOWED"}, now=NOW,
                                  client=client)
    assert controls.youtube_state(client.rules) == "ALLOWED"
    outcome = controls.set_youtube_override(tmp_path, {"state": None}, now=NOW,
                                            client=client)
    assert outcome.ok and controls.youtube_state(client.rules) == "BLOCKED"
    assert read_schedule_file(tmp_path)["override"] is None


def test_a_nonsense_override_is_refused(tmp_path):
    with pytest.raises(controls.ControlError):
        controls.set_youtube_override(tmp_path, {"state": "MAYBE"}, now=NOW,
                                      client=FakeAdGuard([]))
    with pytest.raises(controls.ControlError):
        controls.set_youtube_override(tmp_path, "ALLOWED", now=NOW,
                                      client=FakeAdGuard([]))


def test_schedule_mode_with_no_windows_says_it_is_always_blocked(tmp_path):
    outcome = controls.set_youtube_schedule(tmp_path,
                                            {"mode": "schedule", "windows": []},
                                            now=NOW, client=FakeAdGuard([]))
    assert outcome.ok and "until you add one" in render(outcome.message)


def test_the_schedule_write_preserves_hand_added_keys(tmp_path):
    from safehouse_panel import schedule
    path = tmp_path / schedule.SCHEDULE_CONFIG
    path.write_text(json.dumps({"mode": "blocked", "windows": [],
                                "_comment": "hand written"}), encoding="utf-8")
    controls.set_youtube_schedule(tmp_path, WEEKEND, now=NOW, client=FakeAdGuard([]))
    assert read(path)["_comment"] == "hand written"


# --- current values and the weakening test -----------------------------------

def test_current_values_read_the_files_the_guards_read(tmp_path):
    controls.set_roblox_limit(tmp_path, 30, now=NOW)
    controls.set_roblox_bonus(tmp_path, 10, now=NOW)
    controls.set_browser_unlock(tmp_path, 5, now=NOW)
    controls.set_browser_notify(tmp_path, False, now=NOW)
    values = controls.current_values(tmp_path, now=NOW, user_rules=["||youtube.com^"])
    assert values["roblox_limit_min"] == 30
    assert values["roblox_bonus_min"] == 10
    assert values["browser_unlock_min"] == 5
    assert values["browser_limit_min"] is None
    assert values["youtube_blocked"] == "PARTIAL"


def test_a_broken_roblox_config_reads_as_zero_like_the_guard_does(tmp_path):
    (tmp_path / controls.ROBLOX_CONFIG).write_text('{"limit_min": "sixty"}',
                                                   encoding="utf-8")
    assert controls.current_values(tmp_path, now=NOW)["roblox_limit_min"] == 0


CURRENT = {"roblox_limit_min": 30, "roblox_bonus_min": 0, "browser_enabled": True,
           "browser_limit_min": None, "browser_unlock_min": 0, "browser_notify": False,
           "roblox_delay_sec": [1, 5], "browser_delay_sec": [1, 5],
           "youtube_blocked": "BLOCKED", "youtube_schedule": None,
           "youtube_schedule_now": None}


@pytest.mark.parametrize("action,value", [
    ("roblox_limit_min", 60),          # more game time
    ("roblox_bonus_min", 15),          # extra time today
    ("browser_enabled", False),        # stop closing browsers
    ("browser_unlock_min", 30),        # a timed pass
    ("youtube_blocked", False),        # YouTube back on
    ("youtube_schedule", {"mode": "allowed", "windows": []}),   # YouTube back on
    ("youtube_schedule", WEEKEND),     # windows open minutes a standing block closed
    ("youtube_schedule", {"mode": "banana"}),  # malformed: fails towards asking
    ("youtube_override", {"state": "ALLOWED"}),  # allow now
    ("youtube_override", {"state": "SIDEWAYS"}), # malformed: fails towards asking
    ("browser_limit_min", 30),         # more browsing than the nothing written today
    ("roblox_delay_sec", {"min_sec": 1, "max_sec": 30}),  # a longer grace before the kill
])
def test_weakening_a_protection_needs_the_password_again(action, value):
    assert controls.requires_password(action, value, CURRENT) is True


@pytest.mark.parametrize("action,value", [
    ("roblox_limit_min", 0),           # less game time
    ("roblox_bonus_min", 0),           # take the grant back
    ("browser_enabled", True),         # start closing browsers
    ("browser_unlock_min", 0),         # cancel the unlock
    ("youtube_blocked", True),         # block it now
    ("youtube_schedule", {"mode": "blocked", "windows": []}),   # block it now
    ("youtube_override", {"state": "BLOCKED"}),  # block now: only ever tightens
    ("browser_notify", True),          # tells the child; enforces nothing
    ("roblox_delay_sec", {"min_sec": 1, "max_sec": 3}),
])
def test_tightening_a_protection_only_needs_the_session(action, value):
    assert controls.requires_password(action, value, CURRENT) is False


def test_clearing_an_override_asks_exactly_when_it_would_open_youtube_now():
    # Blocked live, and the schedule would allow right now -> clearing opens: ask.
    opens = {**CURRENT, "youtube_schedule": WEEKEND, "youtube_schedule_now": "ALLOWED"}
    assert controls.requires_password("youtube_override", {"state": None}, opens) is True
    # The schedule agrees with the live block -> clearing changes nothing: no password.
    stays = {**CURRENT, "youtube_schedule": WEEKEND, "youtube_schedule_now": "BLOCKED"}
    assert controls.requires_password("youtube_override", {"state": None}, stays) is False


def test_editing_a_stored_schedule_asks_only_when_it_opens_minutes():
    stored = {**CURRENT, "youtube_schedule": WEEKEND}
    wider = {"mode": "schedule",
             "windows": [{"days": [5, 6], "start": "14:00", "end": "16:00"}]}
    narrower = {"mode": "schedule",
                "windows": [{"days": [5], "start": "14:00", "end": "14:30"}]}
    assert controls.requires_password("youtube_schedule", wider, stored) is True
    assert controls.requires_password("youtube_schedule", narrower, stored) is False
    assert controls.requires_password("youtube_schedule",
                                      {"mode": "blocked", "windows": []},
                                      stored) is False


def test_with_no_schedule_stored_the_baseline_is_the_live_rules_state():
    live_allowed = {**CURRENT, "youtube_blocked": "ALLOWED"}
    # Everything is already open, so even "allowed" opens nothing new...
    assert controls.requires_password("youtube_schedule",
                                      {"mode": "allowed", "windows": []},
                                      live_allowed) is False
    # ...and an unreadable live state counts as blocked: fail towards asking.
    unknown = {**CURRENT, "youtube_blocked": "UNKNOWN"}
    assert controls.requires_password("youtube_schedule", WEEKEND, unknown) is True


def test_an_unknown_control_fails_towards_asking():
    assert controls.requires_password("something_new", 1, CURRENT) is True


def test_a_malformed_value_fails_towards_asking():
    assert controls.requires_password("roblox_limit_min", "lots", CURRENT) is True
