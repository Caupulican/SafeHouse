"""Dashboard assembly: does the page say the true thing about a machine in state X?

The failures worth testing here are the ones that look fine on screen. A lane whose task
was deleted showing as "OFF" reads as a parent's decision. A resolver that did not answer
showing as "protection off" sends someone to fix the wrong thing. And a DNS check that
believes the returned IP calls a working parental block a leak - which happened twice on
this machine in one day, so it has a test of its own in test_panel_dnsprobe.py.
"""

from __future__ import annotations

import json

from safehouse_panel import collect, status
from safehouse_panel.i18n import render


NOW = 1_755_800_000  # 2025-08-21 in the local zone of whatever runs this
TODAY = "2026-08-21"


# Human-facing text is keys+params now (see i18n.py); rendering through the ONE catalog
# and asserting on the English keeps these tests pinned to both the key wiring and the
# catalog text, without hardcoding a second copy of the prose.
def facts_of(view) -> dict:
    return {render(f["label"]): render(f["value"]) for f in view["facts"]}


def notes_of(view) -> list[str]:
    return [render(n) for n in view["notes"]]


def tasks_json(*rows) -> str:
    return json.dumps(list(rows))


def a_task(name, state=3, last_result=0, last_run="2026-08-21 20:31:00"):
    return {"name": name, "state": state, "lastRun": last_run,
            "nextRun": "2026-08-21 20:32:00", "lastResult": last_result}


# --- scheduled tasks ---------------------------------------------------------

def test_parses_the_task_query_and_names_the_state():
    parsed = status.parse_tasks(tasks_json(a_task("SafeHouse-BrowserGuard")))
    assert parsed[0]["state"] == "Ready"
    assert parsed[0]["enabled"] is True
    assert parsed[0]["last_result_text"] == {"k": "task.res.ok"}
    assert render(parsed[0]["last_result_text"]) == "OK"


def test_a_single_task_arrives_as_a_bare_object():
    """ConvertTo-Json emits an object, not a list, when there is exactly one task."""
    parsed = status.parse_tasks(json.dumps(a_task("SafeHouse-Panel")))
    assert [task["name"] for task in parsed] == ["SafeHouse-Panel"]


def test_unreadable_task_output_is_none_not_empty():
    """None means 'could not ask' and becomes UNKNOWN; [] would mean 'no tasks exist',
    which is a completely different thing to tell a parent."""
    assert status.parse_tasks(None) is None
    assert status.parse_tasks("") is None
    assert status.parse_tasks("not json") is None
    assert status.parse_tasks("[]") == []


def test_signed_hresults_are_read_as_unsigned():
    parsed = status.parse_tasks(tasks_json(a_task("SafeHouse-DNS", last_result=-2147024894)))
    assert parsed[0]["last_result"] == 0x80070002
    assert parsed[0]["last_result_text"] == {"k": "task.res.notfound"}
    assert render(parsed[0]["last_result_text"]) == "the program could not be found"


def test_never_run_placeholder_dates_are_not_shown_as_dates():
    parsed = status.parse_tasks(tasks_json(a_task("SafeHouse-Panel",
                                                  last_run="1899-12-30 00:00:00")))
    assert parsed[0]["last_run"] is None


def test_task_rows_include_the_tasks_that_are_missing():
    rows = status.task_rows(status.parse_tasks(tasks_json(a_task("SafeHouse-Panel"))))
    by_name = {row["name"]: row for row in rows}
    assert by_name["SafeHouse-DNS"]["registered"] is False
    assert by_name["SafeHouse-Panel"]["registered"] is True


# --- lane status vocabulary --------------------------------------------------

def a_log(events_today=0, last_event=None):
    return {"last_at": None, "last_line": None, "last_event": last_event,
            "events_today": events_today, "tail": []}


def roblox(task, *, limit=0, bonus=0, used=0, defect=None):
    return status.roblox_lane(
        config={"limit_min": limit, "delay_min_sec": 1, "delay_max_sec": 5},
        config_defect=defect,
        state={"used_sec": used, "bonus_sec": bonus * 60, "date": TODAY},
        task=task, log=a_log(), now=NOW)


def test_unknown_is_not_off_when_the_scheduler_cannot_be_asked():
    lane = roblox(status.task_view(status.ROBLOX_TASK, None))
    assert lane["status"] == status.UNKNOWN


def test_a_missing_task_is_degraded_not_off():
    """Nothing is enforcing this lane. That is a fault, and it must not read like a
    setting somebody chose."""
    lane = roblox(status.task_view(status.ROBLOX_TASK, []))
    assert lane["status"] == status.DEGRADED
    assert lane["reason"]["k"] == "lane.reason.notask"
    assert "nothing is enforcing" in render(lane["reason"])


def test_a_disabled_task_is_off():
    tasks = status.parse_tasks(tasks_json(a_task(status.ROBLOX_TASK, state=1)))
    lane = roblox(status.task_view(status.ROBLOX_TASK, tasks))
    assert lane["status"] == status.OFF


def test_zero_minutes_is_active_because_it_is_the_strictest_setting():
    tasks = status.parse_tasks(tasks_json(a_task(status.ROBLOX_TASK)))
    lane = roblox(status.task_view(status.ROBLOX_TASK, tasks), limit=0)
    assert lane["status"] == status.ACTIVE
    assert any("zero minutes" in note for note in notes_of(lane))


def test_a_failing_last_run_degrades_the_lane():
    tasks = status.parse_tasks(tasks_json(a_task(status.ROBLOX_TASK, last_result=1)))
    lane = roblox(status.task_view(status.ROBLOX_TASK, tasks))
    assert lane["status"] == status.DEGRADED
    # the nested last-result message renders inside the reason
    assert "returned 1" in render(lane["reason"])


def test_a_broken_config_degrades_the_lane_and_says_so():
    tasks = status.parse_tasks(tasks_json(a_task(status.ROBLOX_TASK)))
    lane = roblox(status.task_view(status.ROBLOX_TASK, tasks),
                  defect="bad roblox-budget.json - falling back to 0 min")
    assert lane["status"] == status.DEGRADED
    assert "roblox-budget.json" in lane["reason"]


def test_bonus_is_shown_but_does_not_degrade_the_lane():
    tasks = status.parse_tasks(tasks_json(a_task(status.ROBLOX_TASK)))
    lane = roblox(status.task_view(status.ROBLOX_TASK, tasks), limit=30, bonus=15, used=60)
    assert lane["status"] == status.ACTIVE
    facts = facts_of(lane)
    assert facts["Bonus today"] == "15 min"
    assert facts["Used today"] == "1 min 0s"
    assert facts["Left today"] == "44 min 0s"  # 30 + 15 bonus, one minute spent
    assert any("expires at midnight" in note for note in notes_of(lane))


def browser(task, *, enabled=True, unlock_until=0, limit=None, notify=False, defects=()):
    return status.browser_lane(
        config={"enabled": enabled, "notify": notify, "delay_min_sec": 1,
                "delay_max_sec": 5, "limit_min": limit},
        config_defects=list(defects), state={"unlock_until_ts": unlock_until},
        task=task, log=a_log(events_today=2), now=NOW)


def test_a_parent_unlock_is_degraded_not_active():
    """Enforcement really is suspended right now. Calling that ACTIVE would be a lie
    with a timer on it."""
    tasks = status.parse_tasks(tasks_json(a_task(status.BROWSER_TASK)))
    lane = browser(status.task_view(status.BROWSER_TASK, tasks), unlock_until=NOW + 600)
    assert lane["status"] == status.DEGRADED
    assert lane["reason"]["k"] == "browser.reason.unlocked"
    assert "unlock" in render(lane["reason"])


def test_disabled_in_config_is_off():
    tasks = status.parse_tasks(tasks_json(a_task(status.BROWSER_TASK)))
    lane = browser(status.task_view(status.BROWSER_TASK, tasks), enabled=False)
    assert lane["status"] == status.OFF
    assert "browser-guard.json" in render(lane["reason"])


def test_the_browser_budget_is_labelled_as_not_enforced():
    """The field is written by the panel and read by nothing yet. Showing it without
    that caveat would be a slider that silently does nothing."""
    tasks = status.parse_tasks(tasks_json(a_task(status.BROWSER_TASK)))
    lane = browser(status.task_view(status.BROWSER_TASK, tasks), limit=45)
    assert any("does not meter yet" in note for note in notes_of(lane))
    facts = facts_of(lane)
    assert "not enforced yet" in facts["Daily budget"]


def test_silent_closing_is_stated_on_the_lane():
    tasks = status.parse_tasks(tasks_json(a_task(status.BROWSER_TASK)))
    lane = browser(status.task_view(status.BROWSER_TASK, tasks), notify=False)
    assert any("not told" in note for note in notes_of(lane))


# --- logs --------------------------------------------------------------------

def test_log_summary_counts_only_todays_kills_and_keeps_the_last_one():
    lines = [
        "2026-08-20 21:00:00 [kill] ending msedge.exe",
        "2026-08-21 08:15:00 [wait] msedge.exe is open - ending it in 3s",
        "2026-08-21 08:15:03 [kill] ending msedge.exe",
        "2026-08-21 19:02:11 [kill] ending chrome.exe",
        "rubbish that is not a log line",
    ]
    summary = status.log_summary(lines, today="2026-08-21")
    assert summary["events_today"] == 2
    assert summary["last_event"]["at"] == "2026-08-21 19:02:11"
    assert summary["last_at"] == "2026-08-21 19:02:11"


def test_log_summary_of_an_empty_log_says_nothing_happened():
    summary = status.log_summary([], today="2026-08-21")
    assert summary["events_today"] == 0 and summary["last_event"] is None


# --- DNS ---------------------------------------------------------------------

FILTERING = {
    "enabled": True,
    "filters": [{"name": "HaGeZi Pro++", "url": "https://x/pro.txt", "enabled": True,
                 "rules_count": 250000, "last_updated": "2026-08-21T03:00:00Z"},
                {"name": "off list", "url": "https://x/off.txt", "enabled": False,
                 "rules_count": 10, "last_updated": ""}],
    "whitelist_filters": [{"name": "allowlist", "url": "https://x/allow.txt",
                           "enabled": True, "rules_count": 12}],
    "user_rules": ["||youtube.com^", "@@||firebaseinstallations.googleapis.com^"],
}


def dns(**kwargs):
    base = dict(configured=True, base_url="http://127.0.0.1:3000",
                server={"protection_enabled": True, "running": True, "version": "v0.107"},
                filtering=FILTERING, parental={"enabled": True},
                safebrowsing={"enabled": True},
                safesearch={"enabled": True, "google": True, "youtube": False},
                youtube_service=False, youtube_rules="PARTIAL",
                pihole={"server": "127.0.0.1:5353", "status": "ACTIVE", "detail": "ok"})
    base.update(kwargs)
    return status.dns_view(**base)


def test_dns_reports_lists_rule_counts_and_per_engine_safesearch():
    view = dns()
    assert view["status"] == status.ACTIVE
    assert view["lists_enabled"] == 2
    assert view["rules_total"] == 250012
    assert view["user_rules_count"] == 2
    assert view["safesearch"]["engines"] == {"google": True, "youtube": False}
    assert view["parental"]["status"] == status.ACTIVE


def test_an_unreachable_resolver_is_unknown_and_warns_about_failing_open():
    view = dns(server=None, filtering=None, parental=None, safebrowsing=None,
               safesearch=None, youtube_service=None)
    assert view["status"] == status.UNKNOWN
    assert view["reason"]["k"] == "dns.reason.noanswer"
    assert "fails OPEN" in render(view["reason"])
    assert view["parental"]["status"] == status.UNKNOWN


def test_protection_switched_off_is_off_not_unknown():
    view = dns(server={"protection_enabled": False, "running": True})
    assert view["status"] == status.OFF


def test_both_youtube_lanes_are_reported_separately():
    view = dns(youtube_service=True, youtube_rules="ALLOWED")
    assert view["youtube"] == {"service": "BLOCKED", "user_rules": "ALLOWED"}


# --- browser policy ----------------------------------------------------------

EXPECTED = {r"HKLM\SOFTWARE\Policies\Microsoft\Edge": {
    "DnsOverHttpsMode": "off", "StartupBoostEnabled": 0,
    r"ExtensionInstallForcelist\1": None}}


def test_policy_compares_dword_text_against_the_wanted_number():
    view = status.policy_view(
        {r"HKLM\SOFTWARE\Policies\Microsoft\Edge": {
            "DnsOverHttpsMode": "off", "StartupBoostEnabled": "0x0",
            r"ExtensionInstallForcelist\1": "odfa...;https://update"}},
        EXPECTED)
    assert view["status"] == status.ACTIVE
    assert all(row["ok"] for row in view["keys"][0]["rows"])


def test_a_missing_policy_value_is_a_finding_not_a_pass():
    view = status.policy_view(
        {r"HKLM\SOFTWARE\Policies\Microsoft\Edge": {"DnsOverHttpsMode": "off"}},
        EXPECTED)
    assert view["status"] == status.DEGRADED
    assert view["keys"][0]["applied"] == 1


def test_no_registry_read_is_unknown():
    assert status.policy_view(None, EXPECTED)["status"] == status.UNKNOWN


# --- the gaps ----------------------------------------------------------------

def test_the_known_gaps_are_on_the_page_and_name_the_real_ones():
    ids = {gap["id"] for gap in status.KNOWN_GAPS}
    assert {"plain-dns-bypass", "relay-fails-open", "local-admin", "play-games-lane",
            "panel-no-tls"} <= ids
    for gap in status.KNOWN_GAPS:
        # Every gap says how to check it independently. That habit is why this repo
        # catches its own regressions.
        assert gap["verify"] and gap["detail"] and gap["title"]


def test_the_dns_bypass_gap_names_the_resolver_that_walks_around_everything():
    gap = next(g for g in status.KNOWN_GAPS if g["id"] == "plain-dns-bypass")
    assert "8.8.8.8" in render(gap["detail"]) and "8.8.8.8" in gap["verify"]
    assert "8.8.8.8" in render(gap["detail"], "pt")


# --- the collector's cost on a machine whose job is running games ------------
#
# The page polls every 10 seconds per open tab. Two of the readings are taken by
# spawning processes - one PowerShell for the task query, and ~48 `reg.exe` calls for the
# browser policy - so an uncached snapshot would be hundreds of process launches a minute
# on the child's gaming PC, forever. Everything else stays live.

def collector(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(collect.dnsprobe, "resolver_alive",
                        lambda *a, **k: {"server": "127.0.0.1:5353",
                                         "status": status.UNKNOWN, "detail": "not asked"})
    made = collect.Collector(tmp_path, port=8787)
    made._clock = clock
    return made


def test_the_task_query_and_the_registry_are_not_re_read_on_every_poll(tmp_path,
                                                                       monkeypatch):
    spawns = {"tasks": 0, "policy": 0}
    monkeypatch.setattr(collect, "read_tasks",
                        lambda: spawns.__setitem__("tasks", spawns["tasks"] + 1))
    monkeypatch.setattr(collect, "read_browser_policy",
                        lambda: spawns.__setitem__("policy", spawns["policy"] + 1))
    now = [0.0]
    made = collector(tmp_path, monkeypatch, lambda: now[0])

    for _ in range(6):          # a minute of polling at 10s
        made.snapshot()
        now[0] += 10
    assert spawns["tasks"] == 3   # 20s TTL
    assert spawns["policy"] == 1  # 120s TTL


def test_a_snapshot_of_a_bare_machine_reports_unknown_rather_than_crashing(tmp_path,
                                                                          monkeypatch):
    """No config, no state, no logs, no AdGuard - the state a fresh install is in."""
    monkeypatch.setattr(collect, "read_tasks", lambda: None)
    monkeypatch.setattr(collect, "read_browser_policy", lambda: None)
    snapshot = collector(tmp_path, monkeypatch, lambda: 0.0).snapshot()
    assert [lane["status"] for lane in snapshot["lanes"]] == [status.UNKNOWN,
                                                              status.UNKNOWN]
    assert snapshot["dns"]["status"] == status.UNKNOWN
    assert snapshot["policy"]["status"] == status.UNKNOWN
    assert snapshot["probes"] is None          # probes are on demand, never on a poll
    assert snapshot["controls"]["values"]["roblox_limit_min"] == 0


def test_yesterdays_usage_is_not_charged_against_today(tmp_path, monkeypatch):
    """The guard rolls the state over on its next run; until then the panel must show
    today's zero, not yesterday's total."""
    from safehouse_ytbudget import roblox_guard

    monkeypatch.setattr(collect, "read_tasks", lambda: None)
    monkeypatch.setattr(collect, "read_browser_policy", lambda: None)
    roblox_guard.save_state(tmp_path / "roblox-budget.state.json",
                            roblox_guard.GuardState(date="2000-01-01", used_sec=7200,
                                                    last_running_ts=None, bonus_sec=600))
    lane = collector(tmp_path, monkeypatch, lambda: 0.0).snapshot()["lanes"][0]
    facts = facts_of(lane)
    assert facts["Used today"] == "0 min 0s" and facts["Bonus today"] == "none"
