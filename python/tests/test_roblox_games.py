"""Tests for the per-game Roblox lane.

The log fixtures below are VERBATIM lines from this machine's own client logs
(Roblox 0.736.0, 2026-08-29/30), trimmed only in width. That matters more here than in
the other lanes: this guard's entire input is a third-party log format nobody promised to
keep stable, so the tests are only worth anything if they are pinned to what the client
actually wrote rather than to what the parser hopes it wrote.

The cases that carry the most weight:

* one join writes TWO lines, the plainer one FIRST - the parser must fold them into one
  session, or every session is double-counted and the universe flickers in and out;
* a teleport inside one game changes the placeId and not the universeId, which is the
  whole reason the list is keyed on universes;
* every failure of the config lands on enforcing the rule or on doing nothing loudly -
  never on quietly waving a game through.
"""

from __future__ import annotations

import json

import pytest

from safehouse_ytbudget import roblox_games as rg
from safehouse_ytbudget.roblox_games import (
    MODE_ALLOW,
    MODE_DENY,
    GamePolicy,
    Presence,
    load_policy,
    log_dirs,
    minutes,
    newest_log,
    parse_line,
    parse_policy,
    read_new_lines,
    reduce_presence,
    scan_history,
    verdict,
)

# --- real lines --------------------------------------------------------------

JOIN_PLAIN = ("2026-08-30T15:33:40.153Z,46.153954,48a4,6 [FLog::Output] ! Joining game "
              "'3547d7ef-4b92-4391-b30e-f7d7a052d256' place 88083000467860 at 10.34.1.86")
JOIN_FULL = ("2026-08-30T15:33:40.153Z,46.153954,48a4,6 [FLog::GameJoinLoadTime] Report "
             "game_join_loadtime: placeid:88083000467860, join_time:0.669356719999, "
             # The account id and session id are redacted: this is a public repo and
             # the log they were copied from belongs to a child. Neither is parsed.
             "universeid:9681996188, referral_page:, sid:00000000, "
             "clienttime:1788104020.888, userid:00000000000, ")
LEAVE = ("2026-08-30T15:46:32.881Z,818.881775,1028,6 [FLog::SingleSurfaceApp] "
         "leaveUGCGameInternal")
NOISE = ("2026-08-30T15:46:33.355Z,819.355225,2578,6 [FLog::Warning] "
         "https://www.roblox.com/join/([\\w_-]+).* was already registered by Lua")
# Story Game A: same universe, second place, reached by teleport with no leave between.
TELEPORT_PLAIN = ("2026-08-30T16:04:59.096Z,1925.096802,1028,6 [FLog::Output] ! Joining "
                  "game 'aaaa' place 13864667823 at 10.9.5.19")
TELEPORT_FULL = ("2026-08-30T16:04:59.096Z,1925.096802,1028,6 [FLog::GameJoinLoadTime] "
                 "Report game_join_loadtime: placeid:13864667823, join_time:0.5, "
                 "universeid:4807308814, referral_page:, ")

BEACH = 9681996188
BREAKIN = 4807308814


# --- line parsing ------------------------------------------------------------

def test_plain_join_yields_the_place_but_no_universe():
    kind, found = parse_line(JOIN_PLAIN)
    assert kind == "join"
    assert found == Presence(88083000467860, None, "2026-08-30T15:33:40")


def test_loadtime_join_yields_both_ids():
    kind, found = parse_line(JOIN_FULL)
    assert kind == "join"
    assert (found.place, found.universe) == (88083000467860, BEACH)


def test_leave_is_recognised():
    assert parse_line(LEAVE)[0] == "leave"


def test_a_url_containing_join_is_not_a_join():
    """`/join/` appears in a Lua route registration a dozen times per session."""
    assert parse_line(NOISE)[0] == ""


def test_disconnect_alone_is_not_a_leave():
    """`Client:Disconnect` also fires mid-teleport; only leaveUGCGameInternal ends one."""
    line = ("2026-08-30T15:46:32.920Z,818.920959,2578,6,Info [DFLog::NetworkClient] "
            "Client:Disconnect")
    assert parse_line(line)[0] == ""


# --- folding lines into a presence -------------------------------------------

def test_the_two_lines_of_one_join_are_one_presence():
    presence = reduce_presence([JOIN_PLAIN, JOIN_FULL], None)
    assert presence == Presence(88083000467860, BEACH, "2026-08-30T15:33:40")


def test_the_plain_line_does_not_erase_a_known_universe():
    """Whichever order they arrive in, the richer view must survive."""
    assert reduce_presence([JOIN_FULL, JOIN_PLAIN], None).universe == BEACH


def test_leave_clears_the_presence():
    assert reduce_presence([JOIN_PLAIN, JOIN_FULL, LEAVE], None) is None


def test_teleport_keeps_the_universe_and_changes_the_place():
    presence = reduce_presence(
        [JOIN_PLAIN, JOIN_FULL, TELEPORT_PLAIN, TELEPORT_FULL], None)
    assert (presence.place, presence.universe) == (13864667823, BREAKIN)


def test_presence_carries_across_ticks():
    """The loop feeds only the newly appended lines, so nothing may need a re-read."""
    first = reduce_presence([JOIN_PLAIN, JOIN_FULL], None)
    assert reduce_presence([NOISE], first) == first


# --- tailing a file being written --------------------------------------------

def test_a_partial_last_line_is_left_for_the_next_tick(tmp_path):
    path = tmp_path / "a_Player_1_last.log"
    path.write_text(JOIN_PLAIN + "\n" + JOIN_FULL[:40])
    lines, offset = read_new_lines(path, 0)
    assert lines == [JOIN_PLAIN]
    assert offset == len(JOIN_PLAIN.encode()) + 1
    path.write_text(JOIN_PLAIN + "\n" + JOIN_FULL + "\n")
    lines, _ = read_new_lines(path, offset)
    assert lines == [JOIN_FULL]


def test_nothing_appended_reads_nothing(tmp_path):
    path = tmp_path / "a_Player_1_last.log"
    path.write_text(JOIN_PLAIN + "\n")
    _, offset = read_new_lines(path, 0)
    assert read_new_lines(path, offset) == ([], offset)


def test_a_shrunken_file_is_read_from_the_top(tmp_path):
    path = tmp_path / "a_Player_1_last.log"
    path.write_text(JOIN_PLAIN + "\n" + JOIN_FULL + "\n")
    _, offset = read_new_lines(path, 0)
    path.write_text(LEAVE + "\n")
    lines, _ = read_new_lines(path, offset)
    assert lines == [LEAVE]


def test_undecodable_bytes_do_not_take_the_reader_down(tmp_path):
    """These logs carry `C:\\Users\\Th?o` in a legacy code page, not UTF-8."""
    path = tmp_path / "a_Player_1_last.log"
    path.write_bytes(b"added file C:\\Users\\Th\xe9o\\AppData\n" + JOIN_FULL.encode())
    lines, _ = read_new_lines(path, 0)
    assert len(lines) == 1


def test_a_missing_file_reads_nothing_rather_than_raising(tmp_path):
    assert read_new_lines(tmp_path / "gone.log", 0) == ([], 0)


# --- finding the live log ----------------------------------------------------

def _log(tmp_path, name, age_sec):
    path = tmp_path / name
    path.write_text("x\n")
    import os
    stamp = 1_000_000.0 - age_sec
    os.utime(path, (stamp, stamp))
    return path


def test_newest_log_picks_the_freshest_across_directories(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(), two.mkdir()
    _log(one, "a_Player_1_last.log", 100)
    newer = _log(two, "b_Player_2_last.log", 10)
    assert newest_log([one, two], now=1_000_000.0) == newer


def test_a_stale_log_is_not_live(tmp_path):
    _log(tmp_path, "a_Player_1_last.log", rg.STALE_SEC + 1)
    assert newest_log([tmp_path], now=1_000_000.0) is None


def test_non_player_logs_are_ignored(tmp_path):
    _log(tmp_path, "0.736_Studio_1_last.log", 5)
    assert newest_log([tmp_path], now=1_000_000.0) is None


def test_a_missing_directory_is_skipped(tmp_path):
    assert newest_log([tmp_path / "nope"], now=1_000_000.0) is None


def test_log_dirs_finds_both_installs(tmp_path):
    desktop = tmp_path / "child" / "AppData" / "Local" / "Roblox" / "logs"
    desktop.mkdir(parents=True)
    store = (tmp_path / "child" / "AppData" / "Local" / "Packages"
             / "ROBLOXCORPORATION.ROBLOX_55nm5eh3cm0pr" / "LocalState" / "logs")
    store.mkdir(parents=True)
    (tmp_path / "Public").mkdir()
    assert set(log_dirs(tmp_path)) == {desktop, store}


def test_log_dirs_on_a_machine_without_the_folder():
    assert log_dirs(rg.PROFILES_DIR / "definitely-not-here") == []


# --- the config --------------------------------------------------------------

def test_a_plain_list_of_ids_is_accepted():
    policy, warnings = parse_policy({"enabled": True, "universes": [BEACH, BREAKIN]})
    assert policy.universes == frozenset({BEACH, BREAKIN})
    assert warnings == []


def test_objects_carry_names_for_the_panel():
    policy, _ = parse_policy(
        {"enabled": True, "universes": [{"id": BEACH, "name": "Tycoon Game G"}]})
    assert policy.name_of(BEACH) == "Tycoon Game G"
    assert policy.name_of(BREAKIN) == f"universe {BREAKIN}"


@pytest.mark.parametrize("bad", [True, "9681996188", 0, -1, None, 1.5])
def test_a_non_id_is_dropped_with_a_warning(bad):
    """`true` is the one that matters: bool is an int subclass in Python."""
    policy, warnings = parse_policy({"enabled": True, "universes": [bad, BEACH]})
    assert policy.universes == frozenset({BEACH})
    assert any("id must be" in w for w in warnings)


def test_an_unknown_mode_falls_back_to_allow():
    policy, warnings = parse_policy({"enabled": True, "mode": "block"})
    assert policy.mode == MODE_ALLOW
    assert any("mode must be" in w for w in warnings)


def test_a_non_bool_enabled_stays_off():
    policy, warnings = parse_policy({"enabled": "yes"})
    assert policy.enabled is False
    assert any("enabled must be" in w for w in warnings)


def test_an_empty_allow_list_says_so_out_loud():
    _, warnings = parse_policy({"enabled": True, "mode": MODE_ALLOW, "universes": []})
    assert any("every experience will be ended" in w for w in warnings)


def test_a_disabled_empty_list_is_not_worth_a_warning():
    assert parse_policy({"enabled": False, "universes": []})[1] == []


def test_universes_of_the_wrong_shape_is_survivable():
    policy, warnings = parse_policy({"enabled": True, "universes": {"id": BEACH}})
    assert policy.universes == frozenset()
    assert any("must be a list" in w for w in warnings)


def test_delay_bounds_are_read_from_the_same_file():
    policy, _ = parse_policy({"delay_min_sec": 2, "delay_max_sec": 9})
    assert policy.delay == (2, 9)


def test_a_missing_config_leaves_the_lane_asleep(tmp_path):
    policy, warnings, fault = load_policy(tmp_path / "roblox-games.json", None)
    assert (policy.enabled, warnings, fault) == (False, [], False)


def test_a_broken_config_holds_the_last_good_one_and_reports_a_fault(tmp_path):
    path = tmp_path / "roblox-games.json"
    path.write_text('{"enabled": true, "universes": [9681996188]')  # truncated
    good = GamePolicy(enabled=True, mode=MODE_ALLOW, universes=frozenset({BEACH}))
    policy, warnings, fault = load_policy(path, good)
    assert policy is good
    assert fault and any("holding the last good policy" in w for w in warnings)


def test_a_broken_config_with_no_history_enforces_nothing(tmp_path):
    path = tmp_path / "roblox-games.json"
    path.write_text("{oops")
    policy, warnings, fault = load_policy(path, None)
    assert policy.enabled is False
    assert fault and any("no good policy" in w for w in warnings)


def test_a_json_array_is_not_a_config(tmp_path):
    path = tmp_path / "roblox-games.json"
    path.write_text("[9681996188]")
    assert load_policy(path, None)[2] is True


def test_a_bom_written_by_powershell_still_parses(tmp_path):
    path = tmp_path / "roblox-games.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"enabled": True, "universes": [BEACH]}).encode())
    policy, _, fault = load_policy(path, None)
    assert policy.universes == frozenset({BEACH}) and not fault


# --- the verdict -------------------------------------------------------------

ALLOWLIST = GamePolicy(enabled=True, mode=MODE_ALLOW, universes=frozenset({BEACH}))
DENYLIST = GamePolicy(enabled=True, mode=MODE_DENY, universes=frozenset({BEACH}))
IN_BEACH = Presence(88083000467860, BEACH)
IN_BREAKIN = Presence(13864661000, BREAKIN)
IN_UNKNOWN = Presence(88083000467860, None)


def test_a_disabled_lane_decides_nothing():
    assert verdict(IN_BREAKIN, GamePolicy(enabled=False))[0] == "idle"


def test_not_being_in_a_game_is_idle():
    assert verdict(None, ALLOWLIST)[0] == "idle"


def test_a_listed_game_is_allowed():
    assert verdict(IN_BEACH, ALLOWLIST)[0] == "allow"


def test_an_unlisted_game_is_killed():
    decision, reason = verdict(IN_BREAKIN, ALLOWLIST)
    assert decision == "kill" and "not on the allow list" in reason


def test_an_unreadable_universe_is_killed_under_an_allow_list():
    """A client update that drops the loadtime line must not open the gate."""
    decision, reason = verdict(IN_UNKNOWN, ALLOWLIST)
    assert decision == "kill" and "cannot vouch" in reason


def test_a_denied_game_is_killed():
    assert verdict(IN_BEACH, DENYLIST)[0] == "kill"


def test_an_unlisted_game_is_allowed_under_a_deny_list():
    assert verdict(IN_BREAKIN, DENYLIST)[0] == "allow"


def test_an_unreadable_universe_is_spared_under_a_deny_list():
    """The mirror of the allow case: unknown resolves to the rule, not to a guess."""
    decision, reason = verdict(IN_UNKNOWN, DENYLIST)
    assert decision == "allow" and "cannot convict" in reason


def test_a_sibling_place_of_a_listed_game_is_allowed():
    """The empirical case this whole lane is keyed on: Story Game A's two places."""
    policy = GamePolicy(enabled=True, mode=MODE_ALLOW, universes=frozenset({BREAKIN}))
    assert verdict(Presence(13864667823, BREAKIN), policy)[0] == "allow"


def test_a_place_id_is_never_mistaken_for_a_universe_id():
    policy = GamePolicy(enabled=True, mode=MODE_ALLOW,
                        universes=frozenset({13864661000}))  # a PLACE id, by mistake
    assert verdict(IN_BREAKIN, policy)[0] == "kill"


# --- history -----------------------------------------------------------------

def test_history_pairs_each_join_with_its_leave(tmp_path):
    (tmp_path / "0.736_Player_A_last.log").write_text(
        "\n".join([JOIN_PLAIN, JOIN_FULL, NOISE, LEAVE]) + "\n")
    sessions = scan_history([tmp_path])
    assert len(sessions) == 1
    assert sessions[0].universe == BEACH
    assert sessions[0].start == "2026-08-30T15:33:40"
    assert sessions[0].end == "2026-08-30T15:46:32"


def test_history_does_not_double_count_the_two_join_lines(tmp_path):
    (tmp_path / "0.736_Player_A_last.log").write_text(
        "\n".join([JOIN_PLAIN, JOIN_FULL, TELEPORT_PLAIN, TELEPORT_FULL]) + "\n")
    assert len(scan_history([tmp_path])) == 2


def test_an_unclosed_session_ends_at_the_last_line(tmp_path):
    (tmp_path / "0.736_Player_A_last.log").write_text(
        "\n".join([JOIN_PLAIN, JOIN_FULL, NOISE]) + "\n")
    assert scan_history([tmp_path])[0].end == "2026-08-30T15:46:33"


def test_minutes_of_a_session_with_no_stamps_is_zero():
    assert minutes(rg.Session("", "", 1, None)) == 0


# --- names -------------------------------------------------------------------

def test_names_come_from_the_cache_without_touching_the_network(tmp_path):
    cache = tmp_path / "roblox-games.cache.json"
    cache.write_text(json.dumps({str(BEACH): "Tycoon Game G"}))

    def forbidden(*_args, **_kw):
        raise AssertionError("the enforcing path must never reach the network")

    names, problem = rg.fetch_names([BEACH], cache, opener=forbidden)
    assert names[BEACH] == "Tycoon Game G" and problem is None


def test_an_unreachable_api_degrades_to_ids(tmp_path):
    def broken(*_args, **_kw):
        raise OSError("DNS")

    names, problem = rg.fetch_names([BEACH], tmp_path / "cache.json", opener=broken)
    assert names == {} and "could not reach" in problem


# --- the ledger --------------------------------------------------------------

def test_a_join_is_recorded_under_its_universe():
    seen = rg.record_seen({}, IN_BEACH, "2026-08-30T15:33:40", "Tycoon Game G")
    assert seen[str(BEACH)] == {
        "places": [88083000467860], "joins": 1,
        "first": "2026-08-30T15:33:40", "last": "2026-08-30T15:33:40",
        "name": "Tycoon Game G",
    }


def test_a_second_join_accumulates_rather_than_replaces():
    seen = rg.record_seen({}, Presence(13864661000, BREAKIN), "2026-08-30T13:04:31")
    seen = rg.record_seen(seen, Presence(13864667823, BREAKIN), "2026-08-30T13:04:59")
    entry = seen[str(BREAKIN)]
    assert entry["joins"] == 2
    assert entry["places"] == [13864667823, 13864661000] or entry["places"] == sorted(
        [13864661000, 13864667823])
    assert entry["first"] == "2026-08-30T13:04:31"
    assert entry["last"] == "2026-08-30T13:04:59"


def test_a_game_with_no_universe_is_kept_visible_not_dropped():
    """It is the case the parent most needs to see - allow mode will have ended it."""
    seen = rg.record_seen({}, IN_UNKNOWN, "2026-08-30T15:33:40")
    assert list(seen) == ["place:88083000467860"]


def test_a_known_name_is_not_lost_to_a_later_nameless_join():
    seen = rg.record_seen({}, IN_BEACH, "2026-08-30T15:33:40", "Tycoon Game G")
    seen = rg.record_seen(seen, IN_BEACH, "2026-08-30T16:00:00", "")
    assert seen[str(BEACH)]["name"] == "Tycoon Game G"


def test_the_ledger_is_capped_by_dropping_the_stalest():
    seen = {}
    for i in range(rg.SEEN_MAX + 5):
        seen = rg.record_seen(seen, Presence(i + 1, i + 1), f"2026-01-01T00:{i:02d}:00")
    assert len(seen) == rg.SEEN_MAX
    assert "1" not in seen  # the oldest `last` fell off
    assert str(rg.SEEN_MAX + 5) in seen


def test_rescanning_the_same_logs_does_not_double_the_joins():
    """`--history` reads the same files every run; running it twice must not lie."""
    scanned = rg.record_seen({}, IN_BEACH, "2026-08-30T15:33:40", "Beach")
    once = rg.merge_ledgers({}, scanned)
    twice = rg.merge_ledgers(once, scanned)
    assert twice == once
    assert twice[str(BEACH)]["joins"] == 1


def test_merging_keeps_the_earliest_first_and_the_latest_last():
    old = {str(BEACH): {"places": [1], "joins": 5, "first": "2026-08-01T00:00:00",
                        "last": "2026-08-02T00:00:00", "name": "Beach"}}
    new = {str(BEACH): {"places": [2], "joins": 2, "first": "2026-08-30T00:00:00",
                        "last": "2026-08-31T00:00:00", "name": ""}}
    merged = rg.merge_ledgers(old, new)[str(BEACH)]
    assert merged["first"] == "2026-08-01T00:00:00"
    assert merged["last"] == "2026-08-31T00:00:00"
    assert merged["joins"] == 5
    assert merged["places"] == [1, 2]
    assert merged["name"] == "Beach"


def test_a_corrupt_ledger_costs_checkboxes_not_enforcement(tmp_path):
    path = tmp_path / "roblox-games.seen.json"
    path.write_text("{not json")
    assert rg.load_seen(path) == {}


def test_a_missing_ledger_reads_empty(tmp_path):
    assert rg.load_seen(tmp_path / "nope.json") == {}


# --- the loop, end to end ----------------------------------------------------
#
# Everything above tests a pure function. These drive `run` itself, because the parts
# most likely to be wrong are the joins between those functions: that a join is noticed
# at all, that the verdict reaches taskkill, and above all that a second tick does not
# kill the same join a second time.

@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake Roblox: a log directory, a process list, and a record of what was killed."""
    logs = tmp_path / "logs"
    logs.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    killed: list[list[str]] = []
    up = ["RobloxPlayerBeta.exe"]
    monkeypatch.setattr(rg, "running_processes", lambda: list(up))
    monkeypatch.setattr(rg, "kill_processes", lambda names: killed.append(list(names))
                        or [])
    monkeypatch.setattr(rg.time, "sleep", lambda _s: None)

    def write(*lines):
        path = logs / "0.736_Player_A_last.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def policy(**over):
        # Zero delay bounds so the kill pause takes no wall-clock: killdelay binds
        # time.sleep as a default argument, so it cannot be monkeypatched away.
        cfg = {"enabled": True, "mode": rg.MODE_ALLOW, "universes": [],
               "delay_min_sec": 0, "delay_max_sec": 0}
        cfg.update(over)
        (root / "roblox-games.json").write_text(json.dumps(cfg), encoding="utf-8")

    return type("Machine", (), {"logs": logs, "root": root, "killed": killed, "up": up,
                                "write": staticmethod(write),
                                "policy": staticmethod(policy)})


def drive(machine, ticks=1):
    return rg.run(machine.root, dirs=[machine.logs], ticks=ticks)


def test_an_allowed_game_is_left_alone(machine):
    machine.policy(universes=[BEACH])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 2)
    assert machine.killed == []


def test_an_unlisted_game_is_ended(machine):
    machine.policy(universes=[BREAKIN])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 2)
    assert machine.killed == [["RobloxPlayerBeta.exe"]]


def test_the_same_join_is_not_killed_twice(machine):
    """The presence is forgotten after a kill; a stale join line must not re-fire."""
    machine.policy(universes=[])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 6)
    assert len(machine.killed) == 1


def test_a_teleport_into_a_banned_game_is_caught_mid_session(machine):
    machine.policy(universes=[BEACH])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 2)
    assert machine.killed == []
    machine.write(TELEPORT_PLAIN, TELEPORT_FULL)
    drive(machine, 2)
    assert machine.killed == [["RobloxPlayerBeta.exe"]]


def test_a_disabled_lane_kills_nothing(machine):
    machine.policy(enabled=False, universes=[])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 3)
    assert machine.killed == []


def test_a_missing_config_kills_nothing(machine):
    """Installing the lane on an unconfigured machine must not brick Roblox."""
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 3)
    assert machine.killed == []


def test_a_dry_run_decides_but_does_not_kill(machine):
    machine.policy(universes=[])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    rg.run(machine.root, dirs=[machine.logs], ticks=2, dry_run=True)
    assert machine.killed == []
    assert "[dry-run] [kill]" in (machine.root / "roblox-games.log").read_text()


def test_a_game_closed_during_the_pause_is_not_logged_as_a_kill(machine):
    machine.policy(universes=[])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    machine.up.clear()          # he quit while the guard was waiting
    drive(machine, 2)
    assert machine.killed == []
    assert "[stand-down]" in (machine.root / "roblox-games.log").read_text()


def test_an_idle_machine_writes_nothing_at_all(machine):
    """The parent's condition on every lane here: no news, no files, no log lines."""
    machine.policy(universes=[BEACH])
    drive(machine, 5)
    assert sorted(p.name for p in machine.root.iterdir()) == ["roblox-games.json"]


def test_a_played_game_reaches_the_ledger(machine):
    machine.policy(universes=[BEACH, BREAKIN])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 2)
    machine.write(TELEPORT_PLAIN, TELEPORT_FULL)   # ends the Beach session
    drive(machine, 2)
    ledger = rg.load_seen(machine.root / "roblox-games.seen.json")
    assert ledger[str(BEACH)]["joins"] == 1
    assert ledger[str(BEACH)]["first"] == "2026-08-30T15:33:40"


def test_a_game_that_was_ended_is_still_recorded(machine):
    """What he tried to play is exactly what the parent needs to see afterwards."""
    machine.policy(universes=[])
    machine.write(JOIN_PLAIN, JOIN_FULL)
    drive(machine, 2)
    assert str(BEACH) in rg.load_seen(machine.root / "roblox-games.seen.json")


def test_a_restart_into_a_session_already_in_progress_still_decides(machine):
    """The join is behind us in the file, so a first sighting reads it whole."""
    machine.write(JOIN_PLAIN, JOIN_FULL)
    machine.policy(universes=[])
    drive(machine, 2)
    assert machine.killed == [["RobloxPlayerBeta.exe"]]


def test_leaving_the_game_clears_the_verdict(machine):
    machine.policy(universes=[BEACH])
    machine.write(JOIN_PLAIN, JOIN_FULL, LEAVE)
    drive(machine, 2)
    assert machine.killed == []


def test_a_tick_that_swallows_a_whole_session_still_reports_it():
    """Three seconds of log can hold a leave and a re-join; neither may be lost."""
    final, ended = rg.fold_presence(
        [JOIN_PLAIN, JOIN_FULL, LEAVE, TELEPORT_PLAIN, TELEPORT_FULL], None)
    assert final.universe == BREAKIN
    assert [p.universe for p in ended] == [BEACH]


def test_a_hop_straight_from_one_game_to_another_ends_the_first():
    _, ended = rg.fold_presence(
        [JOIN_PLAIN, JOIN_FULL, TELEPORT_PLAIN, TELEPORT_FULL], None)
    assert [p.place for p in ended] == [88083000467860]


def test_nothing_ends_when_nothing_was_open():
    assert rg.fold_presence([LEAVE, NOISE], None) == (None, [])


# --- content maturity --------------------------------------------------------
#
# Measured against this machine's own history on 2026-08-30 and the two results that
# shaped the design are both counter-intuitive: seven of fifteen played games were rated
# above the account's "Minimal" setting and launched anyway, and the horror game the
# parent named first is rated `minimal`. So the rating is shown, never trusted.

@pytest.mark.parametrize("rating,ceiling,over", [
    ("minimal", "minimal", False),
    ("mild", "minimal", True),
    ("mild", "mild", False),
    ("minimal", "mild", False),
    ("restricted", "moderate", True),
    (None, "minimal", True),            # never rated
    ("", "restricted", True),           # blank is not a rating
    ("unrated", "restricted", True),    # nor is a word Roblox does not use
])
def test_an_unknown_rating_sorts_with_the_ones_over_the_line(rating, ceiling, over):
    assert rg.above_ceiling(rating, ceiling) is over


def test_the_ceiling_defaults_to_minimal():
    assert parse_policy({})[0].max_maturity == "minimal"


def test_an_unknown_ceiling_falls_back_and_warns():
    policy, warnings = parse_policy({"max_maturity": "gentle"})
    assert policy.max_maturity == "minimal"
    assert any("max_maturity must be" in w for w in warnings)


def test_a_listed_game_above_the_ceiling_is_reported_not_vetoed():
    """The parent's list wins. The module's job is to say what is in it, every time."""
    policy = GamePolicy(enabled=True, universes=frozenset({BEACH, BREAKIN}),
                        names={BEACH: "Beach", BREAKIN: "Story Game A"})
    seen = {str(BEACH): {"maturity": "minimal"}, str(BREAKIN): {"maturity": "mild"}}
    flags = rg.flag_ceiling(policy, seen)
    assert len(flags) == 1
    assert "Story Game A is rated mild" in flags[0]
    assert "allowed because it is on the list" in flags[0]


def test_a_listed_game_with_no_rating_is_reported_too():
    policy = GamePolicy(enabled=True, universes=frozenset({BEACH}))
    assert "rated unknown" in rg.flag_ceiling(policy, {})[0]


def test_ratings_only_land_on_games_the_ledger_knows():
    seen = {str(BEACH): {"joins": 1}}
    out = rg.apply_maturity(seen, {BEACH: "mild", 999: "restricted"})
    assert out[str(BEACH)]["maturity"] == "mild"
    assert "999" not in out


def test_maturity_comes_from_the_cache_without_touching_the_network(tmp_path):
    cache = tmp_path / "roblox-games.maturity.json"
    cache.write_text(json.dumps({str(BEACH): "mild"}))

    def forbidden(*_a, **_k):
        raise AssertionError("the enforcing path must never reach the network")

    ratings, problem = rg.fetch_maturity({BEACH: "Beach"}, cache, opener=forbidden)
    assert ratings == {BEACH: "mild"} and problem is None


def _search_response(payload):
    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return lambda *_a, **_k: Fake()


def test_a_search_hit_is_taken_only_when_the_universe_id_matches(tmp_path):
    """Two games can share a name; the id is the only thing that identifies one."""
    opener = _search_response({"searchResults": [{"contents": [
        {"universeId": 111, "contentMaturity": "restricted"},
        {"universeId": BEACH, "contentMaturity": "minimal"},
    ]}]})
    ratings, _ = rg.fetch_maturity({BEACH: "Tycoon Game G"},
                                   tmp_path / "m.json", opener=opener,
                                   pause=lambda _s: None)
    assert ratings == {BEACH: "minimal"}


def test_a_name_that_finds_nothing_leaves_the_rating_unknown(tmp_path):
    opener = _search_response({"searchResults": []})
    ratings, problem = rg.fetch_maturity({BEACH: "Beach"}, tmp_path / "m.json",
                                         opener=opener, pause=lambda _s: None)
    assert ratings == {} and problem is None
    assert rg.above_ceiling(ratings.get(BEACH), "minimal") is True


def test_an_unreachable_search_api_reports_and_keeps_what_it_had(tmp_path):
    cache = tmp_path / "m.json"
    cache.write_text(json.dumps({str(BEACH): "minimal"}))

    def broken(*_a, **_k):
        raise OSError("DNS")

    ratings, problem = rg.fetch_maturity({BEACH: "Beach", BREAKIN: "Story Game A"}, cache,
                                         opener=broken, pause=lambda _s: None)
    assert ratings == {BEACH: "minimal"}
    assert "lookup failed" in problem


def test_a_curated_games_rating_is_found_in_the_cache_not_only_the_ledger():
    """The regression: a list's best entries have never been played, so the ledger has
    nothing on them, and reading only the ledger flagged them as unvetted."""
    policy = GamePolicy(enabled=True, universes=frozenset({537069295}),
                        names={537069295: "Lua Learning"})
    assert rg.flag_ceiling(policy, {}) != []                      # ledger alone: flagged
    assert rg.flag_ceiling(policy, {}, {"537069295": "minimal"}) == []


def test_the_ledger_wins_over_the_cache_when_both_know():
    assert rg.maturity_for(BEACH, {str(BEACH): {"maturity": "mild"}},
                           {str(BEACH): "minimal"}) == "mild"


def test_the_console_is_made_utf8_so_an_emoji_name_cannot_kill_the_report():
    """Found live, not in CI: `--history` died on `Builder Game K 💀` in a cp1252 console.

    The tests all run on a UTF-8 stdout, so this pins the call rather than the symptom -
    and pins that a stream which refuses to be reconfigured is survived, not raised on.
    """
    seen = []

    class Stream:
        def reconfigure(self, **kw):
            seen.append(kw)

    class Stubborn:
        def reconfigure(self, **kw):
            raise ValueError("underlying buffer detached")

    import contextlib
    import io
    with contextlib.redirect_stdout(Stream()), contextlib.redirect_stderr(Stubborn()):
        rg.readable_console()
    assert seen == [{"encoding": "utf-8", "errors": "replace"}]
    # and a plain StringIO (no reconfigure at all) must not raise either
    with contextlib.redirect_stdout(io.StringIO()):
        rg.readable_console()
