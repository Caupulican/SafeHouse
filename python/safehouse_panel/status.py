"""Assemble the dashboard from readings. Pure: inputs in, one JSON-able dict out.

Every function here takes already-read data - the text Task Scheduler printed, the dict
AdGuard's API returned, the lines at the end of a log file - and returns what the page
should say about it. Nothing in this module opens a socket, spawns a process or touches
the registry; `collect.py` does all of that and hands the results in. That split is what
makes the dashboard testable from fixtures, and it is worth more here than usual: the
readings this page gets wrong are the ones nobody notices.

**Four status words, and "unknown" is not "off".**

    ACTIVE    the thing is enforcing right now
    OFF       it is deliberately switched off - a parent chose this
    DEGRADED  it should be enforcing and something is wrong, or it is suspended
    UNKNOWN   we could not find out

A lane whose scheduled task we could not query is UNKNOWN. A lane whose task does not
exist is DEGRADED - nothing is enforcing it, which is a fault, not a setting. A lane
whose task exists but is disabled is OFF. Collapsing those three into one word is how a
dashboard ends up reassuring somebody about a protection that has not run in a month.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

ACTIVE, OFF, DEGRADED, UNKNOWN = "ACTIVE", "OFF", "DEGRADED", "UNKNOWN"

ROBLOX_TASK = "SafeHouse-RobloxBudget"
BROWSER_TASK = "SafeHouse-BrowserGuard"
PANEL_TASK = "SafeHouse-Panel"
RESOLVER_TASK = "SafeHouse-Resolver"
# Named here so the page prints the same domains the watchdog actually queries; the
# probes themselves live in safehouse_setup.resolver.
RESOLVER_PROBE = "doubleclick.net"
CONTROL_PROBE = "github.com"
# Every task this project registers, so the page can show the ones that are NOT running
# too. SafeHouse-DNS is on the list because when it is not running the relay is not in
# the path and nothing is filtered - the 2026-08-01 outage looked healthy everywhere else.
KNOWN_TASKS = (
    ROBLOX_TASK,
    BROWSER_TASK,
    PANEL_TASK,
    RESOLVER_TASK,
    "SafeHouse-DNS",
    "SafeHouse-YouTubeBudget-Py",
    "SafeHouse-YouTubeBudget",
)

TASK_STATES = {0: "Unknown", 1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}

# Last-result codes worth naming. Everything else is shown as hex, which is what the
# scheduler's own UI does and what a search engine wants.
TASK_RESULTS = {
    0: "OK",
    1: "the program returned 1 (a failure)",
    267009: "currently running",
    267011: "has never run",
    267014: "the last run was ended by hand",
    2147750687: "an instance was already running",
    2147942402: "the program could not be found",
}
# Results that are not a fault: success, running now.
TASK_RESULT_OK = (0, 267009)

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) (.*)$")


# --- scheduled tasks ---------------------------------------------------------

def parse_tasks(text: str | None) -> list[dict] | None:
    """Parse the JSON `collect.read_tasks` asks PowerShell for. None if unreadable.

    Deliberately NOT `schtasks /FO CSV`: that prints the state and the result in the
    machine's display language, and this box is pt-BR. `Get-ScheduledTask`'s State is an
    enum that serialises to an integer, and LastTaskResult is a number - both mean the
    same thing in every locale.
    """
    if not text or not text.strip():
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if isinstance(obj, dict):
        obj = [obj]  # ConvertTo-Json emits a bare object when there is exactly one task
    if not isinstance(obj, list):
        return None
    tasks = []
    for raw in obj:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        state = raw.get("state")
        state = state if isinstance(state, int) else 0
        result = raw.get("lastResult")
        # Task results are unsigned 32-bit HRESULTs; PowerShell hands some of them back
        # signed, so 0x80070002 arrives as -2147024894 and matches nothing.
        if isinstance(result, int) and result < 0:
            result += 1 << 32
        tasks.append({
            "name": str(raw["name"]),
            "state_code": state,
            "state": TASK_STATES.get(state, "Unknown"),
            "enabled": state not in (0, 1),
            "last_run": _clean(raw.get("lastRun")),
            "next_run": _clean(raw.get("nextRun")),
            "last_result": result if isinstance(result, int) else None,
            "last_result_text": describe_result(result),
        })
    return tasks


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.startswith("1899-") or text.startswith("1999-11-30"):
        # The scheduler's placeholder for "never", which must not be shown as a date.
        return None
    return text.replace("T", " ")


def describe_result(code: object) -> str:
    if not isinstance(code, int):
        return "unknown"
    if code in TASK_RESULTS:
        return TASK_RESULTS[code]
    return f"0x{code & 0xFFFFFFFF:08X}"


def task_view(name: str, tasks: list[dict] | None) -> dict:
    """One task's row. `registered` is None when the query itself failed."""
    if tasks is None:
        return {"name": name, "registered": None, "state": "Unknown", "enabled": None,
                "last_run": None, "next_run": None, "last_result": None,
                "last_result_text": "could not query Task Scheduler", "healthy": None}
    for task in tasks:
        if task["name"].lower() == name.lower():
            healthy = (task["enabled"]
                       and (task["last_result"] in TASK_RESULT_OK
                            if task["last_result"] is not None else False))
            return {**task, "registered": True, "healthy": healthy}
    return {"name": name, "registered": False, "state": "Not registered", "enabled": False,
            "last_run": None, "next_run": None, "last_result": None,
            "last_result_text": "no such task", "healthy": False}


def task_rows(tasks: list[dict] | None) -> list[dict]:
    """Every SafeHouse task: the known ones in a fixed order, then any extras found."""
    rows = [task_view(name, tasks) for name in KNOWN_TASKS]
    seen = {name.lower() for name in KNOWN_TASKS}
    for task in tasks or []:
        if task["name"].lower() not in seen:
            rows.append({**task, "registered": True, "healthy": None})
    return rows


# --- log files ---------------------------------------------------------------

def log_summary(lines: list[str], *, today: str, marker: str = "[kill]") -> dict:
    """What a guard's log says about today and about the last thing it did."""
    last_line = None
    last_at = None
    last_event = None
    events_today = 0
    for line in lines:
        match = LOG_TS.match(line.strip())
        if not match:
            continue
        date, clock, body = match.groups()
        last_line, last_at = body, f"{date} {clock}"
        if marker in body:
            last_event = {"at": f"{date} {clock}", "text": body}
            if date == today:
                events_today += 1
    return {"last_at": last_at, "last_line": last_line, "last_event": last_event,
            "events_today": events_today, "tail": [line.rstrip() for line in lines[-8:]]}


# --- the enforcement lanes ---------------------------------------------------
#
# Both lanes are built through `_lane`, and a third is coming: the Google Play Games VM
# runs its own resolver inside crosvm and is reachable by neither the hosts file nor a
# process kill on Windows, so it will need a lane of its own (see KNOWN_GAPS). Adding it
# is one more call to `_lane` and one more entry in the list `lanes()` returns - the page
# renders whatever it is given rather than three hardcoded cards.

def _lane(*, key: str, label: str, how: str, task: dict, off_reason: str | None,
          suspended_reason: str | None, defects: list[str], facts: list[dict],
          summary: dict, notes: list[str]) -> dict:
    if task["registered"] is None:
        status, reason = UNKNOWN, ("could not read Task Scheduler, so it is not known "
                                   "whether this lane is running")
    elif off_reason:
        status, reason = OFF, off_reason
    elif task["registered"] is False:
        status, reason = DEGRADED, (f"no {task['name']} task is registered - nothing is "
                                    f"enforcing this lane")
    elif not task["enabled"]:
        status, reason = OFF, "the scheduled task is disabled"
    elif suspended_reason:
        status, reason = DEGRADED, suspended_reason
    elif defects:
        status, reason = DEGRADED, defects[0]
    elif task["last_result"] not in TASK_RESULT_OK:
        status, reason = DEGRADED, (f"the task's last run reported "
                                    f"{task['last_result_text']}")
    else:
        status, reason = ACTIVE, "enforcing"
    return {"key": key, "label": label, "how": how, "status": status, "reason": reason,
            "task": task, "facts": facts, "log": summary,
            "notes": notes + defects}


def roblox_lane(*, config: dict, config_defect: str | None, state: dict,
                task: dict, log: dict, now: int) -> dict:
    """The Roblox budget lane.

    Zero minutes is ACTIVE, not OFF: it is the standing policy on this machine and the
    strictest setting the control has. Showing "OFF" for the tightest possible state
    would be exactly backwards.
    """
    limit_min = config.get("limit_min")
    bonus_min = int(state.get("bonus_sec", 0)) // 60
    used_sec = int(state.get("used_sec", 0))
    effective_min = (limit_min or 0) + bonus_min
    remaining_sec = max(0, effective_min * 60 - used_sec)
    notes = []
    if bonus_min:
        notes.append(f"a bonus of {bonus_min} min was granted for today; it expires at "
                     f"midnight")
    if not bonus_min and effective_min == 0:
        notes.append("zero minutes: the game is closed within about a minute of launching")
    facts = [
        {"label": "Daily budget", "value": f"{limit_min if limit_min is not None else 0} min"},
        {"label": "Bonus today", "value": f"{bonus_min} min" if bonus_min else "none"},
        {"label": "Used today", "value": f"{used_sec // 60} min {used_sec % 60}s"},
        {"label": "Left today", "value": f"{remaining_sec // 60} min {remaining_sec % 60}s"},
        {"label": "Closed today", "value": f"{log['events_today']}"},
    ]
    return _lane(key="roblox", label="Roblox", how="ends the game process",
                 task=task, off_reason=None, suspended_reason=None,
                 defects=[config_defect] if config_defect else [],
                 facts=facts, summary=log, notes=notes)


def browser_lane(*, config: dict, config_defects: list[str], state: dict, task: dict,
                 log: dict, now: int) -> dict:
    enabled = config.get("enabled", True)
    unlock_left = max(0, int(state.get("unlock_until_ts", 0)) - now)
    limit_min = config.get("limit_min")
    notes = []
    if limit_min is not None:
        notes.append(f"a daily budget of {limit_min} min is written in the config, but "
                     f"the browser lane does not meter yet - it closes every browser it "
                     f"finds. Metering is separate work.")
    if not config.get("notify", False):
        notes.append("browsers close silently: the child is not told what closed them")
    facts = [
        {"label": "Enforcement", "value": "closes every browser" if enabled else "off"},
        {"label": "Daily budget", "value": (f"{limit_min} min (not enforced yet)"
                                            if limit_min is not None else "none written")},
        {"label": "Unlock", "value": (f"{unlock_left // 60} min {unlock_left % 60}s left"
                                      if unlock_left else "none")},
        {"label": "Tells the child", "value": "yes" if config.get("notify") else "no"},
        {"label": "Closed today", "value": f"{log['events_today']}"},
    ]
    return _lane(key="browser", label="Web browsing", how="closes any browser process",
                 task=task,
                 off_reason=None if enabled else "the guard is disabled in "
                                                 "browser-guard.json",
                 suspended_reason=(f"a parent unlock is in force for another "
                                   f"{unlock_left // 60} min {unlock_left % 60}s - "
                                   f"browsers are not being closed"
                                   if unlock_left else None),
                 defects=list(config_defects), facts=facts, summary=log, notes=notes)


# --- the resolver host (WSL) -------------------------------------------------

def resolver_view(*, chain: dict, last_run: dict | None, task: dict, now: int) -> dict:
    """Is the machine that does the filtering actually filtering?

    This is the section that would have caught 2026-08-01. Everything blocked at the DNS
    layer lives in two containers inside WSL; when that VM is not running the Windows
    relay **fails open to the router**, so names resolve, browsing works, and every other
    signal on this page stays green while nothing whatsoever is filtered.

    Which is why the verdict is the FILTERING probe and nothing else. Not "port 53
    answered" - the relay answers during the outage, that is what failing open means. Not
    "both containers are listening" - they can be up while filtering is switched off
    inside AdGuard. A known ad domain either comes back sinkholed or it does not, and
    everything else on this card is there to say *why* when it does not.
    """
    filtering, resolves = chain.get("filtering"), chain.get("resolves")
    adguard, pihole, vm = chain.get("adguard"), chain.get("pihole"), chain.get("vm")
    relay = chain.get("relay")

    if filtering:
        status = ACTIVE
        reason = (f"{RESOLVER_PROBE} comes back sinkholed, so the chain is blocking"
                  + ("" if resolves else " - but the control name did not resolve, which "
                                         "usually means the resolver is refusing "
                                         "everything rather than filtering well"))
    elif not relay:
        status = DEGRADED
        reason = ("nothing is answering DNS on this machine at all - not even the relay. "
                  "This is the one failure the child would actually notice")
    elif vm is False:
        status = DEGRADED
        reason = ("the WSL virtual machine is not running, so nothing is filtered - and "
                  "the relay falls back to the router, so nothing else looks wrong")
    else:
        status = DEGRADED
        reason = ("DNS is answering but NOTHING IS BEING BLOCKED. This is the failure "
                  "that looks healthy: the relay has fallen back to the router, so "
                  "browsing works with no filtering behind it")

    facts = [
        {"label": f"{RESOLVER_PROBE} (must be blocked)",
         "value": "blocked" if filtering else "RESOLVED - not filtered"},
        {"label": f"{CONTROL_PROBE} (must resolve)",
         "value": "resolves" if resolves else "did not resolve"},
        {"label": "AdGuard Home (tcp 3000)",
         "value": "listening" if adguard else "no answer"},
        {"label": "Pi-hole (tcp 5353)",
         "value": "listening" if pihole else "no answer"},
        {"label": "WSL virtual machine",
         "value": "running" if vm else ("not running" if vm is False
                                        else "could not be read")},
    ]
    notes = []
    if filtering and not (adguard and pihole):
        # Worth saying rather than hiding behind a green pill: the chain is blocking, but
        # one of the two is missing, so part of the coverage is gone.
        notes.append("Filtering is working, but one of the two resolvers is not "
                     "listening. Blocking is coming from whichever is left, so part of "
                     "the coverage - the Pi-hole gravity lists, or AdGuard's categories "
                     "- is not in the path.")
    if last_run:
        did = last_run.get("action")
        facts.append({"label": "Last watchdog run",
                      "value": f"{last_run.get('stamp', '?')} "
                               f"({'nothing needed doing' if did == 'none' else did})"})
        if last_run.get("detail"):
            notes.append(str(last_run["detail"]))
        for step in last_run.get("steps") or []:
            if not step.get("ok"):
                notes.append(f"{step.get('name')}: {step.get('detail')}")
    else:
        notes.append("The watchdog has not written a record yet, so this is the panel's "
                     "own probe rather than the watchdog's account of a repair.")

    if task["registered"] is False:
        status = DEGRADED if status == ACTIVE else status
        notes.append(f"No {task['name']} task is registered, so nothing restarts the "
                     f"chain by itself. Register it with install.py --resolver-only.")
    elif task["registered"] and not task["enabled"]:
        notes.append(f"{task['name']} is disabled - the chain is up now, but nothing "
                     f"will bring it back if it stops.")

    # Stated whatever the state, because it is the thing a parent will otherwise discover
    # at the worst moment - pressing Repair from a phone while the PC sits at the lock
    # screen and nothing happens.
    notes.append("Repairs run in the logged-on user's session: Windows refuses to run "
                 "WSL as LocalSystem, so nothing can start the chain while nobody is "
                 "signed in to the PC. That is also the only time it matters.")
    return {"key": "resolver", "label": "Filtering resolver (WSL)", "status": status,
            "reason": reason, "facts": facts, "task": task, "notes": notes,
            "chain": chain, "last_run": last_run}


# --- DNS filtering -----------------------------------------------------------

def dns_view(*, configured: bool, base_url: str, server: dict | None,
             filtering: dict | None, parental: dict | None, safebrowsing: dict | None,
             safesearch: dict | None, youtube_service: bool | None,
             youtube_rules: str, pihole: dict) -> dict:
    """What the resolver is actually doing, from AdGuard's own API."""
    if not configured:
        status, reason = UNKNOWN, ("no usable adguard.json on this machine, so the "
                                   "resolver cannot be read from here")
    elif server is None:
        status, reason = UNKNOWN, (f"{base_url} did not answer. If AdGuard is really "
                                   f"down, the relay fails OPEN to the router and "
                                   f"nothing is being filtered at all")
    elif not server.get("protection_enabled", False):
        status, reason = OFF, "protection is switched off in AdGuard Home"
    elif filtering is None:
        status, reason = DEGRADED, "AdGuard answered, but its filtering status did not"
    elif not filtering.get("enabled", False):
        status, reason = DEGRADED, "filtering is switched off in AdGuard Home"
    else:
        status, reason = ACTIVE, "filtering"

    lists = _filter_rows(filtering)
    user_rules = (filtering or {}).get("user_rules")
    user_rule_count = len(user_rules) if isinstance(user_rules, list) else None
    return {
        "status": status,
        "reason": reason,
        "base_url": base_url,
        "version": (server or {}).get("version"),
        "running": (server or {}).get("running"),
        "protection_enabled": (server or {}).get("protection_enabled"),
        "filtering_enabled": (filtering or {}).get("enabled"),
        "lists": lists,
        "lists_enabled": sum(1 for row in lists if row["enabled"]),
        "rules_total": sum(row["rules_count"] or 0 for row in lists if row["enabled"]),
        "user_rules_count": user_rule_count,
        "parental": _switch(parental),
        "safebrowsing": _switch(safebrowsing),
        "safesearch": _safesearch(safesearch),
        "youtube": {
            # Two independent lanes, both shown: `blocked_services` is what the watch-time
            # budget and `safehouse_setup toggle youtube` drive; the user rules are what
            # this panel drives. Showing only one would make a live block look lifted.
            "service": ("UNKNOWN" if youtube_service is None
                        else "BLOCKED" if youtube_service else "ALLOWED"),
            "user_rules": youtube_rules,
        },
        "pihole": pihole,
    }


def _filter_rows(filtering: dict | None) -> list[dict]:
    rows = []
    for kind, key in (("block", "filters"), ("allow", "whitelist_filters")):
        for raw in (filtering or {}).get(key) or []:
            if not isinstance(raw, dict):
                continue
            rows.append({
                "kind": kind,
                "name": str(raw.get("name") or raw.get("url") or "?"),
                "url": str(raw.get("url") or ""),
                "enabled": bool(raw.get("enabled")),
                "rules_count": raw.get("rules_count"),
                "last_updated": _clean(raw.get("last_updated")),
            })
    return rows


def _switch(payload: dict | None) -> dict:
    if payload is None:
        return {"status": UNKNOWN, "enabled": None}
    enabled = bool(payload.get("enabled"))
    return {"status": ACTIVE if enabled else OFF, "enabled": enabled}


def _safesearch(payload: dict | None) -> dict:
    """Per engine where the build reports it, overall where it does not."""
    if payload is None:
        return {"status": UNKNOWN, "enabled": None, "engines": {}}
    engines = {name: bool(value) for name, value in payload.items()
               if name != "enabled" and isinstance(value, bool)}
    enabled = bool(payload.get("enabled"))
    return {"status": ACTIVE if enabled else OFF, "enabled": enabled, "engines": engines}


# --- browser policy ----------------------------------------------------------

def policy_view(values: dict[str, dict] | None, expected: dict) -> dict:
    """Per browser key: what the policy says, and whether it is what we asked for.

    The key-name trap is why this is shown per key rather than as one verdict. Opera
    reads `HKLM\\SOFTWARE\\Policies\\OperaSoftware\\Opera` - no space - and writing to the
    spaced variant succeeds, looks right in regedit, and does nothing at all.
    """
    if values is None:
        return {"status": UNKNOWN, "reason": "could not read the registry", "keys": []}
    keys = []
    for key, wanted in expected.items():
        got = values.get(key) or {}
        rows = []
        for name, want in wanted.items():
            actual = got.get(name)
            rows.append({
                "name": name,
                "value": actual,
                "expected": "any" if want is None else str(want),
                "ok": (actual is not None if want is None
                       else _same_reg_value(actual, want)),
            })
        applied = sum(1 for row in rows if row["ok"])
        keys.append({"key": key, "rows": rows, "applied": applied, "total": len(rows),
                     "status": (ACTIVE if applied == len(rows)
                                else OFF if applied == 0 else DEGRADED)})
    worst = (UNKNOWN if not keys else
             ACTIVE if all(k["status"] == ACTIVE for k in keys) else
             OFF if all(k["status"] == OFF for k in keys) else DEGRADED)
    return {"status": worst,
            "reason": ("every browser policy is applied" if worst == ACTIVE else
                       "no browser policy is applied" if worst == OFF else
                       "some browser policies are missing - a browser without them can "
                       "still resolve names itself and bypass the filter"),
            "keys": keys}


def _same_reg_value(actual: object, want: object) -> bool:
    """`reg query` prints DWORDs as `0x1` and strings as text; compare them as text."""
    if actual is None:
        return False
    got = str(actual).strip().lower()
    if isinstance(want, int):
        try:
            return int(got, 0) == want
        except ValueError:
            return False
    return got == str(want).strip().lower()


# --- the gaps ----------------------------------------------------------------
#
# On the page, next to everything that IS enforced. These are known and accepted, not
# errors, and they are here because a dashboard that only shows green is the failure mode
# this repo exists to fight: this machine has looked entirely healthy while nothing at all
# was being filtered.

KNOWN_GAPS = (
    {
        "id": "plain-dns-bypass",
        "title": "Any device can use 8.8.8.8 directly and skip all filtering",
        "detail": "Nothing forces DNS through the filter: there is no firewall rule "
                  "blocking outbound port 53 to anything but the resolver. A program - "
                  "or a child who reads one web page - can point at 8.8.8.8 and every "
                  "block on this page stops applying to it.",
        "verify": "nslookup doubleclick.net 8.8.8.8 (a real address comes back), then "
                  "nslookup doubleclick.net 127.0.0.1 (0.0.0.0)",
        "severity": "high",
    },
    {
        "id": "relay-fails-open",
        "title": "When WSL is down, DNS falls back to the router and nothing is filtered",
        "detail": "The loopback relay fails OPEN by design - resolution keeps working "
                  "with no filtering behind it, which is why an outage looks exactly "
                  "like a healthy machine. This is the 2026-08-01 failure: fourteen "
                  "hours unfiltered with every other signal green. NOW WATCHED, not "
                  "fixed: the resolver section at the top of this page probes both "
                  "resolvers directly, and SafeHouse-Resolver restarts them every 15 "
                  "minutes - but only while somebody is signed in to the PC, because "
                  "Windows will not run WSL as LocalSystem.",
        "verify": "The 'Filtering resolver (WSL)' section on this page, or in WSL: "
                  "docker ps (both containers Up)",
        "severity": "high",
    },
    {
        "id": "local-admin",
        "title": "The child has local administrator on this PC",
        "detail": "By the parent's choice. He can stop either scheduled task, edit the "
                  "config files, or clear the registry policy. Nothing here prevents "
                  "that - the lanes make it a visible, deliberate act rather than an "
                  "impossible one, and this panel is one more thing that would have to "
                  "be switched off.",
        "verify": "The guard logs stop growing while a browser stays open.",
        "severity": "accepted",
    },
    {
        "id": "play-games-lane",
        "title": "The Google Play Games VM has no process-kill lane",
        "detail": "It runs its own resolver inside crosvm, so it sees neither the hosts "
                  "file nor a Windows process kill. Only the AdGuard lane reaches it. A "
                  "third enforcement lane for it is planned and not built.",
        "verify": "Open Play Games and watch AdGuard's query log for its lookups.",
        "severity": "planned",
    },
    {
        "id": "panel-no-tls",
        "title": "This panel has no TLS - the password crosses the LAN in clear",
        "detail": "Serving HTTPS needs a certificate, and making one needs tooling this "
                  "vendored Python does not have. Anyone already able to sniff this "
                  "network can read the login. It is on the house LAN only, never the "
                  "internet, and it is not port-forwarded.",
        "verify": "The address bar says http://, not https://.",
        "severity": "accepted",
    },
)


def now_strings(now: int) -> dict:
    stamp = datetime.fromtimestamp(now)
    return {"epoch": now, "clock": stamp.strftime("%H:%M:%S"),
            "date": stamp.strftime("%Y-%m-%d"),
            "stamp": stamp.strftime("%Y-%m-%d %H:%M:%S")}
