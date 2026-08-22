"""Keep the filtering resolver alive: is WSL up, and if not, bring it back.

Every block this project makes at the DNS layer - the ad lists, the parental categories,
Safe Browsing, safe search, the YouTube rules - lives inside two Docker containers in
WSL. When the WSL virtual machine is not running, none of it exists, and the Windows
relay **fails open to the router by design**: names keep resolving, browsing keeps
working, and nothing on the machine looks wrong. That is the 2026-08-01 failure, fourteen
hours unfiltered with every other signal green. This module is the lane that notices.

## Why this cannot live in the panel

The control panel runs as **LocalSystem**, and Windows refuses outright to run WSL there:

    A execucao do WSL como sistema local nao e suportada.
    Wsl/WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED

That is not a permissions problem to be worked around with a better token - the distro
registration lives in the *user's* registry hive, so SYSTEM has no distros to run in the
first place. So the work is split:

* **Reading** the state needs no WSL at all, and SYSTEM can do it: two DNS queries and
  two TCP connects to loopback, plus `vmmemWSL` in the process list. That is better
  evidence than `docker ps` anyway - it tests the chain end to end rather than reading a
  container's label - and it is the half the panel can do for itself.
* **Repairing** it runs from this module in the logged-on user's session, under the
  `SafeHouse-Resolver` scheduled task, exactly as `SafeHouse-DNS` already does.

The panel triggers that task with `schtasks /Run` and reads the record this module
leaves behind in `resolver.json`.

## "Something answered" is not "it is working"

The first version of this module asked whether `127.0.0.1:53` answered at all. That check
is worse than useless here, and the reason is the whole point of the module: **the
Windows relay owns that port and falls back to the router when WSL is gone.** During the
exact outage this exists to catch, the port answers cheerfully - it is just answering with
unfiltered results. `dns.apply()` already keeps `ok` and `filtering` as two separate keys
for this reason, with the same fourteen-hour outage in its docstring.

So the load-bearing probe is a **known ad domain that must come back sinkholed**, using
the repo's existing `dns._response_is_sinkholed` so there is one implementation of that
verdict. A control name that must resolve normally goes with it, because "everything is
blocked" usually means the resolver is refusing everything rather than filtering well.

The other trap, found the same way: **UDP to 127.0.0.1:5353 is not forwarded into WSL.**
Only the Windows relay listens on UDP 53; Pi-hole's port answers over TCP through WSL2's
localhost forwarding but not over UDP, so a UDP probe reports a perfectly healthy Pi-hole
as dead. Pi-hole and AdGuard are therefore checked with a TCP connect, and neither of
those is what decides whether the chain is up - the filtering probe is.

## An idle run must do nothing

Same rule as the two kill lanes. `ensure()` sends two UDP packets first; when the chain is
filtering, it returns without spawning a single process. Only a chain that is genuinely
broken costs a `wsl.exe`. That is what makes it safe to schedule this every fifteen
minutes on a machine whose job is running games.

## Nothing appears on screen

The task runs `pythonw.exe` (no console at all) and every subprocess goes through
`winutil.run`, which passes `CREATE_NO_WINDOW`. The child must not see a window flash
when a parent presses a button in another room.
"""

from __future__ import annotations

import json
import socket
import struct
import time
from pathlib import Path

from . import dns as dnsmod
from .winutil import WSL, run

# `wsl.exe -e true` is enough to start a stopped distro: any command starts it, and with
# `systemd=true` in /etc/wsl.conf that boots systemd, which starts dockerd, which restarts
# both containers (`restart: unless-stopped`). So the common repair is one cheap command.
START_TIMEOUT_SEC = 120
# The full repair, when merely starting the distro was not enough. This is the repo's own
# reconcile script: `docker compose up -d` for both stacks, converge the live restart
# policy (a container created before that line landed keeps `no` forever), re-bootstrap
# AdGuard's upstream, then prove the chain sinkholes a known ad domain. It can pull
# images, so it gets a long timeout.
RECONCILE = "$HOME/.local/bin/safehouse-reconcile-dns"
RECONCILE_TIMEOUT_SEC = 420

STATUS_FILE = "resolver.json"

RESOLVER_HOST = "127.0.0.1"
# UDP 53 on Windows is the SafeHouse relay, NOT AdGuard - see the module docstring. It is
# the right place to ask "is anything being filtered", and the wrong place to ask "is
# AdGuard up".
DNS_PORT = 53
# TCP, because WSL2 forwards TCP from Windows loopback into the distro and does not
# forward UDP. AdGuard's admin UI and Pi-hole's DNS listener both answer TCP.
ADGUARD_UI_PORT = 3000
PIHOLE_PORT = 5353

# A domain every ad list in this repo blocks, and the same one `safehouse-reconcile-dns`
# asserts on, so the watchdog and the reconcile agree about what "filtering" means.
FILTER_PROBE = "doubleclick.net"
# ...and a name that must resolve normally. Without it, a resolver that refuses
# everything is indistinguishable from a filter set doing its job.
CONTROL_PROBE = "github.com"

# The WSL2 utility VM as the process list names it. Note there is NO `.exe`: a
# `tasklist /FI "IMAGENAME eq vmmemWSL.exe"` filter matches nothing at all and reports a
# running VM as stopped, which is why this matches the CSV field exactly instead.
VM_PROCESSES = ("vmmemWSL", "vmmem")


# --- reading (safe from SYSTEM, no WSL involved) -----------------------------

def query(name: str, host: str = RESOLVER_HOST, port: int = DNS_PORT, *,
          timeout: float = 2.0) -> bytes | None:
    """One raw DNS/A query, or None. Port-aware, unlike `dns._query_server`.

    Raw UDP rather than `getaddrinfo` because `getaddrinfo` is answered out of the Windows
    DNS client cache, so it can return an address with nothing listening at all - and it
    hides which resolver answered, when that is the entire question.
    """
    labels = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\x00"
    packet = (b"\x5a\xfe\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + labels
              + b"\x00\x01\x00\x01")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (host, port))
            data, _ = sock.recvfrom(1024)
    except OSError:
        return None
    # A reply carrying somebody else's transaction id is noise, not an answer.
    return data if len(data) > 12 and data[:2] == b"\x5a\xfe" else None


def _rcode(packet: bytes) -> int:
    return packet[3] & 0x0F


def _answer_count(packet: bytes) -> int:
    try:
        return struct.unpack("!H", packet[6:8])[0]
    except struct.error:
        return 0


def is_blocked(packet: bytes | None) -> bool:
    """Did the resolver refuse this name?

    Two shapes, because AdGuard's blocking mode is configurable: the default answers with
    the unspecified address (0.0.0.0 / ::), and `nxdomain` mode answers NXDOMAIN. The
    sinkhole test itself is `dns._response_is_sinkholed`, reused rather than reimplemented
    so there is one definition of it in the repo.
    """
    if packet is None:
        return False
    return _rcode(packet) == 3 or dnsmod._response_is_sinkholed(packet)


def resolved_normally(packet: bytes | None) -> bool:
    """Did this name come back with a real answer, rather than nothing or a sinkhole?"""
    if packet is None:
        return False
    return (_rcode(packet) == 0 and _answer_count(packet) > 0
            and not dnsmod._response_is_sinkholed(packet))


def tcp_open(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """Is something accepting TCP here? How Pi-hole and AdGuard are seen from Windows."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def vm_running() -> bool | None:
    """True/False if the WSL2 VM is up, or None if the process list could not be read.

    None matters: "I could not look" and "it is not running" lead to different actions,
    and a dashboard that shows the second when it means the first is how this machine
    looked healthy while nothing was filtered.
    """
    rc, out = run(["tasklist", "/FO", "CSV", "/NH"], timeout=30)
    if rc != 0:
        return None
    wanted = {name.lower() for name in VM_PROCESSES}
    for line in out.splitlines():
        if not line.startswith('"'):
            continue
        image = line.split('","', 1)[0].lstrip('"').strip().lower()
        if image in wanted:
            return True
    return False


def chain_state(*, timeout: float = 2.0) -> dict:
    """What is true right now. Pure reading; nothing here starts anything.

    `up` is `filtering` alone, deliberately. Not "both containers answer TCP", which can
    be true while filtering is off, and not "DNS resolves", which is true *because* the
    relay failed open. The only question worth answering is whether things that should be
    blocked are being blocked.
    """
    filter_answer = query(FILTER_PROBE, timeout=timeout)
    control_answer = query(CONTROL_PROBE, timeout=timeout)
    filtering = is_blocked(filter_answer)
    return {
        "filtering": filtering,
        "resolves": resolved_normally(control_answer),
        # The relay itself: something owns UDP 53 on this machine and replied to at least
        # one of the two. Separates "the chain is broken" from "DNS is entirely dead".
        "relay": filter_answer is not None or control_answer is not None,
        "adguard": tcp_open(RESOLVER_HOST, ADGUARD_UI_PORT, timeout=timeout),
        "pihole": tcp_open(RESOLVER_HOST, PIHOLE_PORT, timeout=timeout),
        "vm": vm_running(),
        "up": filtering,
    }


# --- the record the panel reads ----------------------------------------------

def read_status(root: Path) -> dict | None:
    try:
        obj = json.loads((Path(root) / STATUS_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def write_status(root: Path, payload: dict) -> None:
    from safehouse_ytbudget.atomicio import write_json

    try:
        write_json(Path(root) / STATUS_FILE, payload)
    except OSError:
        # Losing the record must never fail the repair. The repair is the point; the
        # record only explains it afterwards.
        pass


# --- repairing (needs the logged-on user's session) --------------------------

def _step(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def start_distro(distro: str | None = None) -> dict:
    """Start the distro by running the cheapest possible command inside it.

    With `systemd=true` this boots systemd, which starts dockerd, which restarts both
    containers on their own. Most outages need nothing more than this.
    """
    args = [WSL] + (["-d", distro] if distro else []) + ["-e", "true"]
    rc, out = run(args, timeout=START_TIMEOUT_SEC)
    return _step("start the WSL distro", rc == 0,
                 "started" if rc == 0 else f"wsl.exe returned {rc}: {out.strip()[:200]}")


def reconcile(distro: str | None = None) -> dict:
    """Run the repo's own DNS reconcile inside WSL and report what it said."""
    args = [WSL] + (["-d", distro] if distro else []) + ["bash", "-lc", RECONCILE]
    rc, out = run(args, timeout=RECONCILE_TIMEOUT_SEC)
    return _step("reconcile the DNS stack", rc == 0,
                 out.strip()[-300:] if out.strip() else
                 ("reconciled" if rc == 0 else f"returned {rc}"))


def ensure(root: Path, *, distro: str | None = None, trigger: str = "schedule",
           force: bool = False, clock=time.time, settle_sec: float = 3.0) -> dict:
    """Make sure the filtering chain is answering. Returns the record it wrote.

    `force` skips the "already healthy, do nothing" shortcut, which is what the panel's
    button uses: a parent who presses *Repair now* while the page says everything is fine
    is usually chasing something the probes cannot see, and a button that answers "no need"
    teaches them the button is a lie.
    """
    started = clock()
    before = chain_state()
    record = {
        "stamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": int(started),
        "trigger": trigger,
        "distro": distro or "(default)",
        "before": before,
        "steps": [],
    }

    if before["up"] and not force:
        # The whole reason this is safe to run every fifteen minutes: two UDP packets and
        # no process at all.
        record.update({"action": "none", "ok": True, "after": before, "took_sec": 0,
                       "detail": "The chain is filtering; nothing needed starting."})
        write_status(root, record)
        return record

    record["steps"].append(start_distro(distro))
    time.sleep(settle_sec)
    mid = chain_state()

    # Only reconcile if starting the distro was not enough. It is much the more expensive
    # of the two - it can pull images - and on the common outage (a WSL shutdown) systemd
    # and `restart: unless-stopped` have already done the whole job by now.
    if not mid["up"] or force:
        record["steps"].append(reconcile(distro))

    after = chain_state()
    record.update({
        "action": "repaired",
        "ok": after["up"],
        "after": after,
        "took_sec": int(clock() - started),
        "detail": _describe(before, after),
    })
    write_status(root, record)
    return record


def _describe(before: dict, after: dict) -> str:
    if after["up"] and not before["up"]:
        return "The filtering chain was down and is blocking again."
    if after["up"]:
        return "The filtering chain is blocking."
    missing = [name for name, key in (("AdGuard Home", "adguard"), ("Pi-hole", "pihole"))
               if not after.get(key)]
    which = (" ".join(missing) + " did not answer." if missing else
             "Both containers answer, but nothing is being blocked - filtering is off "
             "inside AdGuard, or its lists are empty.")
    return ("STILL DOWN. " + which + " Nothing is being filtered, and the Windows relay "
            "falls back to the router, so browsing keeps working with no blocking behind "
            "it.")
