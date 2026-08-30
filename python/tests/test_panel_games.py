"""The panel's half of the per-game Roblox lane.

Three things here are worth a test each, and all three are ways the control could look
right and be wrong:

* **which direction weakens depends on the mode.** Ticking a game is a weakening under
  an allow list and a tightening under a deny list. Get it backwards and the password
  prompt appears on exactly the changes that did not need it, and stays quiet on the
  ones that did.
* **the name in the file is not the name the browser sent.** A rule the parent will
  later read back has to be labelled from the guard's own ledger, or the file can be
  made to say anything next to any id.
* **an empty allow list that is switched ON is not "enforcing".** It ends every
  experience on the machine, which is a misconfiguration, and the card has to say so
  rather than showing a calm green pill.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from safehouse_panel import collect, controls, status
from safehouse_panel.i18n import render
from safehouse_ytbudget import roblox_games

NOW = int(datetime(2026, 8, 30, 13, 30).timestamp())
BEACH, BREAKIN = 9681996188, 4807308814


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def seed(root: Path, config: dict | None = None, seen: dict | None = None) -> None:
    if config is not None:
        (root / controls.GAMES_CONFIG).write_text(json.dumps(config), encoding="utf-8")
    if seen is not None:
        (root / controls.GAMES_SEEN).write_text(json.dumps(seen), encoding="utf-8")


LEDGER = {
    str(BEACH): {"places": [88083000467860], "joins": 3, "first": "2026-08-30T15:33:40",
                 "last": "2026-08-30T15:33:40", "name": "Tycoon Game G"},
    str(BREAKIN): {"places": [13864661000, 13864667823], "joins": 2,
                   "first": "2026-08-30T16:04:31", "last": "2026-08-30T16:15:46",
                   "name": "Story Game A"},
    "place:7606564092": {"places": [7606564092], "joins": 1,
                         "first": "2026-08-30T16:16:47",
                         "last": "2026-08-30T16:16:47", "name": ""},
}


# --- the switches ------------------------------------------------------------

def test_turning_the_filter_on_writes_the_file(tmp_path):
    outcome = controls.set_games_enabled(tmp_path, True, now=NOW)
    assert outcome.ok
    assert read(tmp_path / controls.GAMES_CONFIG)["enabled"] is True
    assert "ON" in render(outcome.message)


def test_the_mode_is_refused_rather_than_guessed(tmp_path):
    with pytest.raises(controls.ControlError):
        controls.set_games_mode(tmp_path, "block", now=NOW)
    assert not (tmp_path / controls.GAMES_CONFIG).exists()


def test_the_mode_is_written_and_reported_with_what_it_was(tmp_path):
    seed(tmp_path, {"enabled": True, "mode": "allow"})
    outcome = controls.set_games_mode(tmp_path, "deny", now=NOW)
    assert read(tmp_path / controls.GAMES_CONFIG)["mode"] == "deny"
    assert render(outcome.message) == ('Mode is now "all games except the listed ones" '
                                       '(was "only the listed games").')


def test_writing_the_mode_keeps_the_list(tmp_path):
    """Read-modify-write: a mode switch must not silently empty the allow list."""
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": [BEACH]})
    controls.set_games_mode(tmp_path, "deny", now=NOW)
    assert read(tmp_path / controls.GAMES_CONFIG)["universes"] == [BEACH]


# --- the list ----------------------------------------------------------------

@pytest.mark.parametrize("bad", [True, "9681996188", 0, -1, 1.5, None])
def test_a_posted_id_that_is_not_an_id_is_refused(bad):
    with pytest.raises(controls.ControlError):
        controls.coerce_universes([bad])


def test_a_list_that_is_not_a_list_is_refused():
    with pytest.raises(controls.ControlError):
        controls.coerce_universes({"id": BEACH})


def test_an_oversized_list_is_refused_rather_than_truncated():
    with pytest.raises(controls.ControlError):
        controls.coerce_universes(list(range(1, controls.GAMES_LIST_MAX + 2)))


def test_duplicates_collapse_and_the_list_is_sorted():
    assert controls.coerce_universes([BEACH, BREAKIN, BEACH]) == sorted([BEACH, BREAKIN])


def test_names_come_from_the_ledger_not_from_the_browser(tmp_path):
    seed(tmp_path, {"enabled": True, "mode": "allow"}, LEDGER)
    controls.set_games_list(tmp_path, [BEACH], now=NOW)
    assert read(tmp_path / controls.GAMES_CONFIG)["universes"] == [
        {"id": BEACH, "name": "Tycoon Game G"}]


def test_a_game_the_ledger_has_never_seen_is_stored_without_a_name(tmp_path):
    seed(tmp_path, {"enabled": True}, LEDGER)
    controls.set_games_list(tmp_path, [123456], now=NOW)
    assert read(tmp_path / controls.GAMES_CONFIG)["universes"] == [
        {"id": 123456, "name": ""}]


def test_the_message_names_what_changed_in_both_directions(tmp_path):
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": [BEACH]}, LEDGER)
    outcome = controls.set_games_list(tmp_path, [BREAKIN], now=NOW)
    text = render(outcome.message)
    assert f"Added: {BREAKIN}" in text and f"Removed: {BEACH}" in text
    assert outcome.before == [BEACH] and outcome.after == [BREAKIN]


def test_the_written_list_is_read_back_by_the_guard_itself(tmp_path):
    """The panel and the guard must agree about the file; this is the round trip."""
    seed(tmp_path, {"enabled": True, "mode": "allow"}, LEDGER)
    controls.set_games_list(tmp_path, [BEACH, BREAKIN], now=NOW)
    policy, _, fault = roblox_games.load_policy(tmp_path / controls.GAMES_CONFIG, None)
    assert not fault
    assert policy.universes == frozenset({BEACH, BREAKIN})
    assert policy.name_of(BEACH) == "Tycoon Game G"


# --- which changes need the password again -----------------------------------

def values(**over):
    base = {"games_enabled": True, "games_mode": "allow", "games_list": [BEACH]}
    base.update(over)
    return base


def test_turning_the_filter_off_asks_for_the_password():
    assert controls.requires_password("games_enabled", False, values())
    assert not controls.requires_password("games_enabled", True, values())


def test_moving_from_an_allow_list_to_a_deny_list_asks():
    """allow permits a named handful; deny permits everything but one. It widens."""
    assert controls.requires_password("games_mode", "deny", values())
    assert not controls.requires_password("games_mode", "allow",
                                          values(games_mode="deny"))


def test_allowing_one_more_game_asks_but_removing_one_does_not():
    assert controls.requires_password("games_list", [BEACH, BREAKIN], values())
    assert not controls.requires_password("games_list", [], values())


def test_under_a_deny_list_the_direction_is_reversed():
    current = values(games_mode="deny")
    assert controls.requires_password("games_list", [], current)          # un-banning
    assert not controls.requires_password("games_list", [BEACH, BREAKIN], current)


def test_a_malformed_list_asks_rather_than_slipping_through():
    assert controls.requires_password("games_list", "everything", values())


# --- what the card says ------------------------------------------------------

# A healthy, running task. Without one every lane reports UNKNOWN - "Task Scheduler
# could not be read" outranks everything else, deliberately - and none of the config
# states below would be visible behind it.
TASKS = json.dumps([{"name": status.GAMES_TASK, "state": 4, "lastResult": 0,
                     "lastRun": "2026-08-30 13:00:00", "nextRun": ""}])


def collector(tmp_path, monkeypatch):
    monkeypatch.setattr(collect.dnsprobe, "resolver_alive",
                        lambda *a, **k: {"server": "127.0.0.1:5353",
                                         "status": status.UNKNOWN, "detail": "x"})
    monkeypatch.setattr(collect, "read_tasks", lambda: TASKS)
    monkeypatch.setattr(collect, "read_browser_policy", lambda: None)
    made = collect.Collector(tmp_path, port=8787)
    made._clock = lambda: 0.0
    return made


def lane_of(tmp_path, monkeypatch):
    snap = collector(tmp_path, monkeypatch).snapshot(now=NOW)
    return next(l for l in snap["lanes"] if l["key"] == "games"), snap


def test_an_empty_allow_list_that_is_on_is_not_reported_as_enforcing(tmp_path,
                                                                    monkeypatch):
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": []})
    lane, _ = lane_of(tmp_path, monkeypatch)
    assert "EVERY experience will be ended" in render(lane["reason"])


def test_a_filter_that_is_off_says_so_rather_than_implying_a_rule(tmp_path, monkeypatch):
    seed(tmp_path, {"enabled": False, "mode": "allow", "universes": [BEACH]})
    lane, _ = lane_of(tmp_path, monkeypatch)
    assert lane["status"] == status.OFF
    assert "any experience may run" in render(lane["reason"])
    assert any("still applies" in render(n) for n in lane["notes"])


def test_a_broken_config_reaches_the_card_as_a_fault(tmp_path, monkeypatch):
    (tmp_path / controls.GAMES_CONFIG).write_text("{oops", encoding="utf-8")
    lane, _ = lane_of(tmp_path, monkeypatch)
    assert "no good policy" in render(lane["reason"])


def test_the_facts_count_the_list_and_the_ledger(tmp_path, monkeypatch):
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": [BEACH]}, LEDGER)
    lane, _ = lane_of(tmp_path, monkeypatch)
    facts = {render(f["label"]): render(f["value"]) for f in lane["facts"]}
    assert facts["Games on the list"] == "1"
    assert facts["Games seen so far"] == "3"
    assert facts["Mode"] == "only the listed games"


# --- the catalog the checkboxes are drawn from -------------------------------

def test_the_catalog_is_newest_first_and_marks_what_is_listed(tmp_path, monkeypatch):
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": [BEACH]}, LEDGER)
    _, snap = lane_of(tmp_path, monkeypatch)
    rows = snap["games"]["catalog"]
    assert [r["last"] for r in rows] == sorted((r["last"] for r in rows), reverse=True)
    assert {r["id"]: r["listed"] for r in rows if r["id"]} == {BEACH: True,
                                                               BREAKIN: False}


def test_a_join_with_no_universe_is_shown_but_cannot_be_ticked(tmp_path, monkeypatch):
    seed(tmp_path, {"enabled": True}, LEDGER)
    _, snap = lane_of(tmp_path, monkeypatch)
    orphan = next(r for r in snap["games"]["catalog"] if r["key"].startswith("place:"))
    assert orphan["id"] is None


def test_a_listed_game_the_ledger_never_saw_is_still_shown(tmp_path, monkeypatch):
    """Hand-edited, or its logs rotated away - either way it governs the rule."""
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": [4242]}, LEDGER)
    _, snap = lane_of(tmp_path, monkeypatch)
    row = next(r for r in snap["games"]["catalog"] if r["id"] == 4242)
    assert row["listed"] and row["joins"] == 0


def test_the_page_carries_the_card_and_its_wiring():
    from safehouse_panel import page
    html = page.render_panel()
    for hook in ('data-ctl="games_enabled"', 'data-ctl="games_mode"',
                 'data-ctl="games_list"', 'id="games-list"', 'id="pill-games"',
                 "function gamesRows"):
        assert hook in html, hook


# --- the rating in front of the decision -------------------------------------

def test_the_rating_rides_in_the_catalog_row(tmp_path, monkeypatch):
    ledger = {k: dict(v) for k, v in LEDGER.items()}
    ledger[str(BEACH)]["maturity"] = "minimal"
    ledger[str(BREAKIN)]["maturity"] = "mild"
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": []}, ledger)
    _, snap = lane_of(tmp_path, monkeypatch)
    rows = {r["id"]: r for r in snap["games"]["catalog"] if r["id"]}
    assert (rows[BEACH]["maturity"], rows[BEACH]["over"]) == ("minimal", False)
    assert (rows[BREAKIN]["maturity"], rows[BREAKIN]["over"]) == ("mild", True)
    assert snap["games"]["ceiling"] == "minimal"


def test_a_game_with_no_rating_is_marked_over_the_ceiling(tmp_path, monkeypatch):
    seed(tmp_path, {"enabled": True}, LEDGER)
    _, snap = lane_of(tmp_path, monkeypatch)
    assert all(r["over"] for r in snap["games"]["catalog"])


def test_a_listed_game_above_the_ceiling_is_noted_on_the_card(tmp_path, monkeypatch):
    """It still runs - the list wins - but the parent is told, every poll."""
    ledger = {k: dict(v) for k, v in LEDGER.items()}
    ledger[str(BREAKIN)]["maturity"] = "mild"
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": [BREAKIN]}, ledger)
    lane, _ = lane_of(tmp_path, monkeypatch)
    assert any("rated mild" in render(n) and "on the list" in render(n)
               for n in lane["notes"])
    assert lane["status"] == status.ACTIVE     # a note, not a degradation


def test_a_curated_game_never_played_still_shows_its_rating(tmp_path, monkeypatch):
    """The list's most deliberate entries are the ones with no play history at all."""
    seed(tmp_path, {"enabled": True, "mode": "allow", "universes": [537069295]}, LEDGER)
    (tmp_path / controls.GAMES_RATINGS).write_text(
        json.dumps({"537069295": "minimal"}), encoding="utf-8")
    _, snap = lane_of(tmp_path, monkeypatch)
    row = next(r for r in snap["games"]["catalog"] if r["id"] == 537069295)
    assert row["maturity"] == "minimal" and row["over"] is False


def test_a_ratings_cache_of_the_wrong_shape_is_ignored(tmp_path, monkeypatch):
    (tmp_path / controls.GAMES_RATINGS).write_text("[1,2,3]", encoding="utf-8")
    seed(tmp_path, {"enabled": True}, LEDGER)
    _, snap = lane_of(tmp_path, monkeypatch)
    assert all(r["maturity"] == "" for r in snap["games"]["catalog"])
