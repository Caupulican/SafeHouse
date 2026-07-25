"""The real Windows platform.

Everything Windows-specific lives here and nowhere else. The parsing is deliberately
split out into module-level pure functions (`parse_dns_events`, `parse_pktmon_counters`)
so it can be tested on Linux against captured real output -- which is how the format
quirks below were found rather than guessed:

  * `QueryResults` is a ';'-separated CNAME chain *and* answer addresses. CNAME tokens
    carry a `type: 5 ` prefix; addresses are bare. IPv4 answers appear IPv4-mapped
    (`::ffff:40.84.97.4`) beside genuine IPv6.
  * `pktmon counters --json` localises its *values* (`"Type":"Fluxos"` on this
    pt-BR machine) but not its keys, so parsing by key name is safe and matching on
    values would not be.
  * pktmon counts the same packet at several layers of the stack, so summing the
    components multi-counts badly. The maximum across them approximates the NIC's own
    view, which is the number we want.

No third-party dependencies: this ships on a vendored embeddable Python and has to start
as SYSTEM at boot with nothing installed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

from .. import hosts as hostsmod
from ..pins import normalise_ip

SYSTEM_ROOT = os.environ.get("SystemRoot", r"C:\Windows")
PKTMON = Path(SYSTEM_ROOT) / "System32" / "PktMon.exe"
WEVTUTIL = Path(SYSTEM_ROOT) / "System32" / "wevtutil.exe"
IPCONFIG = Path(SYSTEM_ROOT) / "System32" / "ipconfig.exe"
NETSH = Path(SYSTEM_ROOT) / "System32" / "netsh.exe"

DNS_CHANNEL = "Microsoft-Windows-DNS-Client/Operational"
DNS_EVENT_ID = 3008

# Substring match on the query name, case-insensitive. Matches the PowerShell watcher so
# the two pin the same addresses during the shadow-mode overlap.
YT_NAME = re.compile(r"googlevideo|youtube|ytimg|ggpht|yt3", re.IGNORECASE)

# The subset that actually carries video bytes. Everything else in the family -- ytimg
# thumbnails, ggpht avatars, the API, the page itself -- is small and resolves
# constantly, so without this distinction it crowds the video endpoint out of the
# 32-filter budget and the meter goes blind. See pins.update().
YT_VIDEO_NAME = re.compile(r"googlevideo", re.IGNORECASE)

FIREWALL_RULE = "SafeHouse: YouTube budget block"
PKTMON_FILTER_TAG = "SafeHouseYB"
MAX_FILTERS = 32

_EVT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"


def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command, never raising. Returns (returncode, stdout).

    A watcher that dies because a helper misbehaved enforces nothing, so every external
    call here fails soft and the caller decides what a failure means.
    """
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode, proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return -1, ""


# --- pure parsers (tested on Linux against real captured output) -------------


def parse_dns_events(
    xml_text: str, name_pattern: re.Pattern[str] = YT_NAME
) -> list[tuple[str, bool]]:
    """Extract (answer IP, is_video) for matching query names from wevtutil XML output.

    `is_video` marks an address resolved from a googlevideo name, i.e. one that carries
    actual video rather than thumbnails or API chatter. The pin set uses it to decide
    what survives the 32-filter cap.

    wevtutil emits a bare sequence of <Event> elements with no document root, so they
    are wrapped before parsing.
    """
    if not xml_text.strip():
        return []
    try:
        root = ElementTree.fromstring(f"<Events>{xml_text}</Events>")
    except ElementTree.ParseError:
        return []

    found: list[tuple[str, bool]] = []
    seen: dict[str, int] = {}
    for event in root.iter(f"{_EVT_NS}Event"):
        data = {
            d.get("Name"): (d.text or "")
            for d in event.iter(f"{_EVT_NS}Data")
        }
        query = data.get("QueryName", "")
        if not query or not name_pattern.search(query):
            continue
        is_video = bool(YT_VIDEO_NAME.search(query))
        for token in data.get("QueryResults", "").split(";"):
            token = token.strip()
            if not token or token.lower().startswith("type:"):
                continue  # a CNAME hop, not an address
            ip = normalise_ip(token)
            if not ip:
                continue
            if ip in seen:
                # One address can be answered for both a video and a non-video name.
                # Video wins: losing the flag would put it back at the front of the
                # eviction queue.
                if is_video:
                    found[seen[ip]] = (ip, True)
                continue
            seen[ip] = len(found)
            found.append((ip, is_video))
    return found


def parse_pktmon_counters(raw: str) -> int | None:
    """Maximum cumulative inbound bytes across components, or None if unparseable.

    None means "skip this sample". Returning 0 for an unreadable counter would look
    exactly like an idle machine and silently stop the budget.
    """
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return None  # not JSON: the session is not running
    try:
        groups = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(groups, list):
        return None

    best = 0
    saw_counter = False
    for group in groups:
        for component in (group or {}).get("Components", []) or []:
            for counter in (component or {}).get("Counters", []) or []:
                inbound = (counter or {}).get("Inbound") or {}
                value = inbound.get("Bytes")
                if isinstance(value, int) and not isinstance(value, bool):
                    saw_counter = True
                    best = max(best, value)
    return best if saw_counter else None


class WindowsPlatform:
    def __init__(self, *, root: Path, hosts_path: Path, block_list: Path, log_path: Path) -> None:
        self.root = root
        self.hosts_path = hosts_path
        self.block_list = block_list
        self.log_path = log_path
        self._metered: list[str] = []

    # --- clock ---------------------------------------------------------------

    def today(self) -> str:
        # Local, not UTC: the budget resets at the child's midnight.
        return datetime.now().strftime("%Y-%m-%d")

    def now_epoch(self) -> int:
        return int(time.time())

    # --- logging -------------------------------------------------------------

    def log(self, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\r\n"
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # logging must never take the watcher down

    # --- hosts ---------------------------------------------------------------

    def hosts_blocked(self) -> bool:
        try:
            return hostsmod.is_blocked(hostsmod.read(self.hosts_path))
        except OSError:
            return False

    def block_hosts(self) -> str:
        try:
            raw = hostsmod.read(self.hosts_path)
        except OSError:
            return "NOHOSTS"
        if hostsmod.is_blocked(raw):
            return "ALREADY"
        try:
            entries = hostsmod.parse_block_list(self.block_list.read_bytes())
        except OSError:
            return "NOLIST"
        if not entries:
            return "NOLIST"
        try:
            hostsmod.write_atomic(self.hosts_path, hostsmod.insert_block(raw, entries))
        except OSError:
            return "ERROR"
        return f"BLOCKED:{len(entries)}"

    def unblock_hosts(self, scope: str = "auto") -> str:
        try:
            raw = hostsmod.read(self.hosts_path)
        except OSError:
            return "NONE"
        new = hostsmod.remove_block(raw, scope)
        if new == raw:
            return "NONE"
        try:
            hostsmod.write_atomic(self.hosts_path, new)
        except OSError:
            return "ERROR"
        return "UNBLOCKED"

    # --- DNS -----------------------------------------------------------------

    def resolved_youtube_ips(self, since_epoch: int) -> list[tuple[str, bool]]:
        window_ms = max(1000, (self.now_epoch() - since_epoch) * 1000)
        query = (
            f"*[System[EventID={DNS_EVENT_ID} and "
            f"TimeCreated[timediff(@SystemTime) <= {window_ms}]]]"
        )
        rc, out = _run(
            [str(WEVTUTIL), "qe", DNS_CHANNEL, f"/q:{query}", "/f:xml", "/rd:true", "/c:500"]
        )
        if rc != 0:
            return []
        return parse_dns_events(out)

    def enable_dns_channel(self) -> bool:
        """The channel is off by default and is the only real-time source of resolved
        IPs, so this runs once at startup."""
        rc, _ = _run([str(WEVTUTIL), "sl", DNS_CHANNEL, "/e:true"])
        return rc == 0

    def flush_dns(self) -> None:
        _run([str(IPCONFIG), "/flushdns"])

    # --- byte metering -------------------------------------------------------

    def inbound_bytes(self) -> int | None:
        """Read whatever counters session currently exists.

        Deliberately NOT gated on this object having configured the session. In shadow
        mode `set_metered_ips` is suppressed -- pktmon is one global session and
        reconfiguring it would tear down the live watcher's filters -- so this object's
        own `_metered` is empty while a perfectly good session is running. Gating here
        made shadow mode read None forever, so it never accrued a second and the whole
        comparison was vacuous.

        The Ticker holds the guard that actually matters: it skips the read when *it*
        has no pins.
        """
        rc, out = _run([str(PKTMON), "counters", "--json"])
        if rc != 0:
            return None
        return parse_pktmon_counters(out)

    def set_metered_ips(self, ips: list[str]) -> bool:
        """Rebuild the counters-only pktmon session around exactly these IPs.

        Counters-only (`start -c -o`) means per-component byte counters and no packet
        capture to disk, so the overhead stays negligible on a child's gaming PC.

        Always returns True: tearing the session down restarts the counter whether or
        not the rebuild then succeeded, so the caller must drop its baseline either way.
        """
        self.stop_metering()
        if not ips:
            self._metered = []
            return True
        for index, ip in enumerate(ips[:MAX_FILTERS], start=1):
            _run([str(PKTMON), "filter", "add", f"{PKTMON_FILTER_TAG}{index}", "-i", ip])
        rc, _ = _run([str(PKTMON), "start", "-c", "-o"])
        self._metered = list(ips[:MAX_FILTERS]) if rc == 0 else []
        return True

    def stop_metering(self) -> None:
        _run([str(PKTMON), "stop"])
        _run([str(PKTMON), "filter", "remove"])

    # --- firewall ------------------------------------------------------------
    # Hosts blocking stops new lookups but cannot terminate an already-open QUIC
    # stream. Blocking the pinned endpoints can.

    def firewall_ips(self) -> list[str]:
        rc, out = _run(
            [str(NETSH), "advfirewall", "firewall", "show", "rule",
             f"name={FIREWALL_RULE}", "verbose"]
        )
        if rc != 0:
            return []
        found: list[str] = []
        for line in out.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            # The label is localised; the value shape is not, so parse addresses out of
            # any line whose value is a comma-separated list of them.
            for token in value.strip().split(","):
                ip = normalise_ip(token.strip())
                if ip and ip not in found:
                    found.append(ip)
        return found

    def set_firewall(self, ips: list[str]) -> str:
        self.clear_firewall()
        if not ips:
            return "NOIPS"
        joined = ",".join(ips)
        ok = 0
        for direction in ("out", "in"):
            rc, _ = _run(
                [str(NETSH), "advfirewall", "firewall", "add", "rule",
                 f"name={FIREWALL_RULE}", f"dir={direction}", "action=block",
                 f"remoteip={joined}", "protocol=any", "profile=any", "enable=yes"]
            )
            if rc == 0:
                ok += 1
        return f"BLOCKED:{len(ips)}" if ok == 2 else "ERROR"

    def clear_firewall(self) -> str:
        rc, _ = _run(
            [str(NETSH), "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"]
        )
        return "UNBLOCKED" if rc == 0 else "NONE"
