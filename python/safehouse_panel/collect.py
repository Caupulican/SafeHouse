"""Read the live machine, hand the readings to `status.py`, and apply changes.

This is the only impure module in the package: it spawns PowerShell, reads the registry,
opens UDP sockets and talks to AdGuard. Everything it learns is passed to `status.py`,
which turns it into the page's data without touching anything itself.

The rule it follows everywhere: **a reading that could not be taken is `None`, never a
default.** `None` becomes UNKNOWN on the page. Substituting a plausible value here - a
zero for an unreadable counter, an empty list for a filter set that did not answer - is
how a dashboard ends up quietly reassuring somebody, which is the one thing this page
must never do.
"""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from safehouse_ytbudget import browser_guard, roblox_guard
from safehouse_ytbudget.platform.adguard import AdGuardClient, AdGuardConfig

from . import DEFAULT_PORT, DEFAULT_ROOT, controls, dnsprobe, status as statusmod

PANEL_CONFIG = "panel.json"

# Where the AdGuard chain listens. AdGuard Home is on :53 in front of Pi-hole on :5353,
# reachable from Windows through WSL2's localhost forwarding.
RESOLVER_HOST = "127.0.0.1"
RESOLVER_PORT = 53
PIHOLE_PORT = 5353

# Ask PowerShell rather than schtasks: `State` is an enum that serialises to an integer
# and `LastTaskResult` is a number, so neither depends on the display language. This box
# is pt-BR, where `schtasks /FO CSV` prints "Pronto" instead of "Ready".
TASK_QUERY = r"""
$rows = foreach ($t in @(Get-ScheduledTask -TaskPath '\' -ErrorAction SilentlyContinue)) {
  if ($t.TaskName -notlike 'SafeHouse-*') { continue }
  $i = $null
  try { $i = $t | Get-ScheduledTaskInfo -ErrorAction Stop } catch { }
  $lr = ''; $nr = ''; $res = $null
  if ($i) {
    if ($i.LastRunTime) { $lr = $i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') }
    if ($i.NextRunTime) { $nr = $i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') }
    $res = [int64]$i.LastTaskResult
  }
  [pscustomobject]@{ name = $t.TaskName; state = [int]$t.State; lastRun = $lr;
                     nextRun = $nr; lastResult = $res }
}
ConvertTo-Json -InputObject @($rows) -Compress -Depth 3
"""


def _machine_name() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname() or "?"


def read_panel_config(root: Path) -> dict:
    return controls.read_config(Path(root) / PANEL_CONFIG)


def read_tasks() -> str | None:
    """Raw JSON from Task Scheduler, or None if it could not be asked."""
    try:
        from safehouse_setup.winutil import powershell
    except ImportError:
        return None
    rc, out = powershell(TASK_QUERY, timeout=60)
    return out if rc == 0 else None


def tail(path: Path, *, max_bytes: int = 20_000) -> list[str]:
    """The end of a log file. Missing is `[]`, which the page shows as 'nothing logged'."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


def read_browser_policy() -> dict | None:
    """`{key: {policy: value}}` from HKLM, or None off Windows / when reg fails."""
    if os.name != "nt":
        return None
    try:
        from safehouse_setup import browser
    except ImportError:
        return None
    try:
        return browser.status()
    except Exception:  # noqa: BLE001 - a registry read must not take the page down
        return None


def expected_policies() -> dict[str, dict]:
    """What `safehouse_setup browser` writes, so the page compares against one source."""
    from safehouse_setup import browser

    wanted: dict[str, object] = {}
    wanted.update(browser.STRING_POLICIES)
    wanted.update(browser.DWORD_POLICIES)
    for name, entries in browser.LIST_POLICIES.items():
        for index, entry in enumerate(entries, start=1):
            wanted[rf"{name}\{index}"] = entry
    # The uBlock Origin id differs between the Edge add-ons store and the Chrome Web
    # Store, so any value counts as applied - only its absence is a finding.
    wanted[r"ExtensionInstallForcelist\1"] = None
    return {key: dict(wanted) for key in browser.OPERA_KEYS + browser.CHROMIUM_KEYS}


# How long a reading taken by SPAWNING A PROCESS is reused. The page polls every 10
# seconds, per open tab, and this runs on a PC whose job is running games:
#
#   * the task query is one PowerShell start-up (~1s of CPU) - 20s means a tab costs
#     three of those a minute rather than six;
#   * the browser policy is worse. `safehouse_setup.browser.status()` runs one `reg.exe`
#     per value per key: six keys times eight values is ~48 process spawns. Uncached,
#     two open tabs would be nearly 600 process launches a minute, forever, to re-read
#     registry values that change when somebody runs the installer.
#
# Everything else in the snapshot is a file read or a local HTTP call and stays live. The
# staleness is bounded and shown - the page carries the time the snapshot was taken.
TASK_TTL_SEC = 20
POLICY_TTL_SEC = 120


class Collector:
    """Holds the AdGuard client, the process-spawn caches and the last probe run."""

    def __init__(self, root: Path = DEFAULT_ROOT, *, port: int = DEFAULT_PORT,
                 bound: list[str] | None = None, clock=time.monotonic) -> None:
        self.root = Path(root)
        self.port = port
        self.bound = bound or []
        self._client: AdGuardClient | None = None
        self._client_checked = False
        self.probes: dict | None = None
        self._clock = clock
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, ttl: int, produce):
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        value = produce()
        self._cache[key] = (now, value)
        return value

    # --- AdGuard ---------------------------------------------------------
    def client(self) -> AdGuardClient | None:
        """One client, built lazily, retried while it is missing.

        Not cached as "absent" forever: `adguard.json` is written by the installer and
        may appear after the panel started.
        """
        if self._client is None:
            cfg = AdGuardConfig.from_file(self.root / "adguard.json")
            self._client = AdGuardClient(cfg) if cfg else None
            self._client_checked = True
        return self._client

    # --- probes ----------------------------------------------------------
    def run_probes(self, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else now
        probes = dnsprobe.probe_list(read_panel_config(self.root).get("probes"))
        results = dnsprobe.run_probes(probes, client=self.client(),
                                      server=RESOLVER_HOST, port=RESOLVER_PORT)
        self.probes = {"ran_at": statusmod.now_strings(now)["stamp"], "results": results,
                       "resolver": f"{RESOLVER_HOST}:{RESOLVER_PORT}"}
        return self.probes

    # --- the snapshot ----------------------------------------------------
    def snapshot(self, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else now
        clock = statusmod.now_strings(now)
        today = clock["date"]
        tasks = self._cached("tasks", TASK_TTL_SEC,
                             lambda: statusmod.parse_tasks(read_tasks()))

        lanes = [self._roblox_lane(tasks, now, today), self._browser_lane(tasks, now, today)]
        client = self.client()
        return {
            "now": clock,
            "host": {
                "machine": _machine_name(),
                "root": str(self.root),
                "bound": self.bound,
                "port": self.port,
            },
            "lanes": lanes,
            "tasks": statusmod.task_rows(tasks),
            "dns": self._dns(client),
            "probes": self.probes,
            "policy": self._cached(
                "policy", POLICY_TTL_SEC,
                lambda: statusmod.policy_view(read_browser_policy(),
                                              expected_policies())),
            "gaps": list(statusmod.KNOWN_GAPS),
            "controls": self.control_state(now, client),
        }

    def _roblox_lane(self, tasks, now: int, today: str) -> dict:
        config_path = self.root / controls.ROBLOX_CONFIG
        limit_sec, warning = roblox_guard.load_limit_sec(config_path)
        (low, high), delay_warnings = roblox_guard.load_delay_sec(config_path)
        state, state_warning = roblox_guard.load_state(
            self.root / controls.ROBLOX_STATE, today)
        stale = state.date != today
        log = statusmod.log_summary(tail(self.root / "roblox-budget.log"), today=today)
        lane = statusmod.roblox_lane(
            config={"limit_min": limit_sec // 60, "delay_min_sec": low,
                    "delay_max_sec": high},
            config_defect=warning or (delay_warnings[0] if delay_warnings else None),
            # A state file from an earlier day has not been rolled over yet - the guard
            # does that on its next run. Today's usage is zero, not yesterday's number.
            state={"used_sec": 0 if stale else state.used_sec,
                   "bonus_sec": 0 if stale else state.bonus_sec, "date": state.date},
            task=statusmod.task_view(statusmod.ROBLOX_TASK, tasks), log=log, now=now)
        if state_warning:
            lane["notes"].append(state_warning)
        return lane

    def _browser_lane(self, tasks, now: int, today: str) -> dict:
        config_path = self.root / controls.BROWSER_CONFIG
        cfg, warnings = browser_guard.load_config(config_path)
        raw = controls.read_config(config_path)
        state, state_warning = browser_guard.load_state(
            self.root / controls.BROWSER_STATE)
        limit = raw.get("limit_min")
        log = statusmod.log_summary(tail(self.root / "browser-guard.log"), today=today)
        lane = statusmod.browser_lane(
            config={"enabled": cfg.enabled, "notify": cfg.notify,
                    "delay_min_sec": cfg.delay_min_sec, "delay_max_sec": cfg.delay_max_sec,
                    "limit_min": limit if isinstance(limit, int)
                    and not isinstance(limit, bool) else None},
            config_defects=list(warnings),
            state={"unlock_until_ts": state.unlock_until_ts}, task=statusmod.task_view(
                statusmod.BROWSER_TASK, tasks), log=log, now=now)
        if state_warning:
            lane["notes"].append(state_warning)
        return lane

    def _dns(self, client) -> dict:
        if client is None:
            return statusmod.dns_view(
                configured=False, base_url=str(self.root / "adguard.json"), server=None,
                filtering=None, parental=None, safebrowsing=None, safesearch=None,
                youtube_service=None, youtube_rules="UNKNOWN",
                pihole=self._pihole())
        server = client.server_status()
        filtering = client.filtering_status() if server is not None else None
        user_rules = None
        if isinstance(filtering, dict) and isinstance(filtering.get("user_rules"), list):
            user_rules = [str(rule) for rule in filtering["user_rules"]]
        return statusmod.dns_view(
            configured=True, base_url=client.cfg.base_url, server=server,
            filtering=filtering,
            parental=client.parental_status() if server is not None else None,
            safebrowsing=client.safebrowsing_status() if server is not None else None,
            safesearch=client.safesearch_status() if server is not None else None,
            youtube_service=client.is_blocked() if server is not None else None,
            youtube_rules=controls.youtube_state(user_rules),
            pihole=self._pihole())

    def _pihole(self) -> dict:
        """Pi-hole sits upstream of AdGuard on :5353; both have to answer."""
        return dnsprobe.resolver_alive(RESOLVER_HOST, PIHOLE_PORT)

    # --- controls --------------------------------------------------------
    def user_rules(self, client=None) -> list[str] | None:
        client = client if client is not None else self.client()
        return client.user_rules() if client is not None else None

    def control_state(self, now: int, client=None) -> dict:
        """Current value + range of every control, so the page never invents a bound."""
        rules = self.user_rules(client)
        current = controls.current_values(self.root, now=now, user_rules=rules)
        return {
            "values": current,
            "ranges": {
                "roblox_limit_min": controls.ROBLOX_LIMIT_RANGE,
                "roblox_bonus_min": controls.ROBLOX_BONUS_RANGE,
                "browser_limit_min": controls.BROWSER_LIMIT_RANGE,
                "browser_unlock_min": controls.BROWSER_UNLOCK_RANGE,
                "delay_sec": controls.DELAY_RANGE,
            },
        }

    def apply(self, action: str, value: object, *, now: int | None = None):
        now = int(time.time()) if now is None else now
        handler = controls.ACTIONS.get(action)
        if handler is None:
            raise controls.ControlError(f"unknown control {action!r}")
        if action == "youtube_blocked":
            return handler(self.root, value, now=now, client=self.client())
        return handler(self.root, value, now=now)
