"""On-demand parental blocks, by name, across both enforcement lanes.

This module is the only owner of manual parental toggles.

    python -m safehouse_setup toggle youtube --status
    python -m safehouse_setup toggle youtube --block
    python -m safehouse_setup toggle youtube --allow

Each name maps to a host list under `parental-blocks/<name>.txt`, so adding a new one is
a text file, not code.

**Both lanes, one command.** A block has to be applied in two places or it is not a block:

  * the **hosts file**, which covers Windows applications using the Windows resolver;
  * the **resolver** (AdGuard Home `blocked_services`), which covers everything else --
    above all the Google Play Games VM, which runs its own resolver inside crosvm and
    cannot see the hosts file at all, plus every phone and tablet on the network.

Driving only the hosts file is what this toggle used to do, and it is a half-measure that
reports success: the child opens Play Games and walks straight around it. The reverse is
just as bad -- a parent lifting a block and finding it still enforced, because the budget
lane is the one holding it. So `block` and `allow` each return the outcome of *both* lanes
and the caller must report both.

This is the **manual** block, distinct from the YouTube watch-time budget. The two share
the same marker convention so neither double-inserts on top of the other, but they carry
different marker text — the budget's automatic block says "(auto: daily budget)" and the
watcher will lift only its own. A block applied here is left alone by the watcher, which
is the point: a parent saying "no, now" must not be undone a minute later by a reconcile
that notices usage is under the limit.

The splice itself is reused from the watcher (`safehouse_ytbudget.hosts`) rather than
reimplemented, because byte-preservation there was hard-won: the hosts file on a real
machine is hundreds of kilobytes of adblock entries maintained by other tooling, and
Windows silently ignores one it considers malformed.
"""

from __future__ import annotations

import dataclasses
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The watcher package is the reference implementation of the splice.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safehouse_ytbudget import hosts as hostsmod  # noqa: E402
from safehouse_ytbudget.platform import adguard as adguardmod  # noqa: E402

from .winutil import flush_dns, require_admin  # noqa: E402

HOSTS_PATH = Path(
    os.environ.get(
        "YB_HOSTS",
        str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32" / "drivers" / "etc" / "hosts"),
    )
)

# Where install.py puts adguard.json and the resolver block marker.
SAFEHOUSE_ROOT = Path(os.environ.get("SAFEHOUSE_ROOT", r"C:\ProgramData\SafeHouse"))
BLOCK_MARKER = "adguard-block-state.json"

# Friendly names for the log/marker; anything not listed is title-cased.
KNOWN = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "roblox": "Roblox",
    "twitch": "Twitch",
    "discord": "Discord",
}


def display_name(name: str) -> str:
    return KNOWN.get(name.lower(), name.replace("-", " ").title())


def start_marker(name: str) -> bytes:
    return f"# === Parental block: {display_name(name)} (manual) ===".encode()


def end_marker(name: str) -> bytes:
    return f"# === end {display_name(name)} block ===".encode()


def _any_section(name: str) -> re.Pattern[bytes]:
    """Matches ANY block for this name, manual or automatic.

    Deliberately broad for detection, so `--block` never stacks a second section on top
    of one the watcher already placed.
    """
    return re.compile(
        rb"(?m)^# === Parental block: " + re.escape(display_name(name).encode()) + rb"\b"
    )


def find_block_list(name: str, search_from: Path | None = None) -> Path | None:
    """Locate parental-blocks/<name>.txt across the repo and deployed layouts."""
    base = search_from or Path(__file__).resolve().parent.parent.parent
    candidates = [
        base / "windows" / "parental-blocks" / f"{name}.txt",
        base / "parental-blocks" / f"{name}.txt",
        Path(r"C:\SafeHouse\windows\parental-blocks") / f"{name}.txt",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@dataclass(frozen=True)
class ToggleResult:
    """The outcome of one toggle, per lane.

    Two fields rather than one string because the lanes fail independently and a caller
    that sees only the hosts lane cannot tell a real block from one the Play Games VM
    ignores.

    `hosts`    -- BLOCKED:<n> | ALREADY | NOLIST | UNBLOCKED | NONE
    `resolver` -- BLOCKED | ALREADY | UNBLOCKED | NONE | BUDGET_OWNED
                  | DISARMED | UNREACHABLE | NO_SERVICE:<id> | ERROR:<status>
    """

    hosts: str
    resolver: str

    # Lane states that mean "the resolver is not enforcing what was asked".
    _RESOLVER_GAPS = ("DISARMED", "UNREACHABLE", "NO_SERVICE", "ERROR")

    @property
    def hosts_failed(self) -> bool:
        return self.hosts == "NOLIST"

    @property
    def resolver_covered(self) -> bool:
        """False when the resolver lane could not be made to agree with the request.

        BUDGET_OWNED is deliberately *covered*: the resolver is enforcing, just not
        because of this command.
        """
        return not self.resolver.startswith(self._RESOLVER_GAPS)


def resolver_client(name: str, root: Path | None = None):
    """(client, status) for the resolver lane with the service pinned to `name`.

    Returns (None, reason) whenever the lane cannot enforce, so every caller has to deal
    with a disarmed or unreachable resolver explicitly rather than by omission.
    """
    root = root or SAFEHOUSE_ROOT
    cfg = adguardmod.AdGuardConfig.from_file(root / adguardmod.DEFAULT_CREDENTIALS)
    if cfg is None:
        return None, "DISARMED"
    service = name.lower()
    client = adguardmod.AdGuardClient(dataclasses.replace(cfg, service_id=service))
    known = client.known_service_ids()
    if known is None:
        return None, "UNREACHABLE"
    if service not in known:
        return None, f"NO_SERVICE:{service}"
    return client, "OK"


def _budget_service(root: Path) -> str | None:
    """The service the budget watcher owns, whose block this module must not lift."""
    cfg = adguardmod.AdGuardConfig.from_file(root / adguardmod.DEFAULT_CREDENTIALS)
    return cfg.service_id if cfg is not None else None


def block_resolver(name: str, root: Path | None = None) -> str:
    root = root or SAFEHOUSE_ROOT
    client, why = resolver_client(name, root)
    if client is None:
        return why
    result = client.set_blocked(True)
    if (result in ("BLOCKED", "ALREADY")
            and name.lower() == _budget_service(root)):
        # Add rather than replace: the daily budget may already own this same flat
        # AdGuard entry.  Keeping both reasons is what lets either controller release
        # its own claim without reopening YouTube for the other.
        adguardmod.add_block_owner(
            root / BLOCK_MARKER,
            datetime.now().strftime("%Y-%m-%d"),
            adguardmod.MANUAL_OWNER,
        )
    return result


def allow_resolver(name: str, root: Path | None = None) -> str:
    """Lift the resolver block, unless the budget is the one holding it.

    The budget's block is left alone for the same reason the hosts lane leaves its auto
    section alone: lifting it hands back time that has already been spent, and the watcher
    would re-apply it within one sample anyway. `ctl grant <minutes>` is the way to give
    more time.
    """
    root = root or SAFEHOUSE_ROOT
    client, why = resolver_client(name, root)
    if client is None:
        return why
    marker = root / BLOCK_MARKER
    owns_budget_service = name.lower() == _budget_service(root)
    owners = adguardmod.read_block_owners(marker) if owns_budget_service else set()
    if adguardmod.BUDGET_OWNER in owners and client.is_blocked():
        # This command releases only the parent's claim.  The budget still requires the
        # shared service entry, so do not create even a one-sample access window.
        adguardmod.remove_block_owner(marker, adguardmod.MANUAL_OWNER)
        return "BUDGET_OWNED"
    result = client.set_blocked(False)
    if result in ("UNBLOCKED", "NONE") and owns_budget_service:
        adguardmod.remove_block_owner(marker, adguardmod.MANUAL_OWNER)
    return result


def resolver_status(name: str, root: Path | None = None) -> str:
    """'BLOCKED' | 'allowed' | the reason the lane cannot answer."""
    client, why = resolver_client(name, root)
    if client is None:
        return why
    state = client.is_blocked()
    if state is None:
        return "UNREACHABLE"
    return "BLOCKED" if state else "allowed"


def status(name: str, hosts_path: Path | None = None) -> bool:
    path = hosts_path or HOSTS_PATH
    try:
        return _any_section(name).search(hostsmod.read(path)) is not None
    except OSError:
        return False


def block(name: str, hosts_path: Path | None = None,
          root: Path | None = None) -> ToggleResult:
    path = hosts_path or HOSTS_PATH
    require_admin(f"Blocking {display_name(name)}")
    # The resolver lane runs regardless of what the hosts lane does: a missing host list
    # must not leave the Play Games VM unblocked as well.
    return ToggleResult(hosts=_block_hosts(name, path),
                        resolver=block_resolver(name, root))


def _block_hosts(name: str, path: Path) -> str:
    if status(name, path):
        return "ALREADY"

    list_path = find_block_list(name)
    if list_path is None:
        return "NOLIST"
    entries = hostsmod.parse_block_list(list_path.read_bytes())
    if not entries:
        return "NOLIST"

    raw = hostsmod.read(path)
    newline = hostsmod.detect_newline(raw)
    section = (newline if raw else b"") + start_marker(name) + newline
    for host in entries:
        section += b"0.0.0.0 " + host.encode("utf-8") + newline
    section += end_marker(name) + newline

    if raw and not raw.endswith(b"\n"):
        raw += newline
    hostsmod.write_atomic(path, raw + section)
    flush_dns()
    return f"BLOCKED:{len(entries)}"


def allow(name: str, hosts_path: Path | None = None,
          root: Path | None = None) -> ToggleResult:
    """Lift the manual block in both lanes.

    In each lane an automatic budget block is left in place: lifting it would hand back
    time the budget has already spent, and the watcher would re-apply it within a sample
    anyway.
    """
    path = hosts_path or HOSTS_PATH
    require_admin(f"Unblocking {display_name(name)}")
    return ToggleResult(hosts=_allow_hosts(name, path),
                        resolver=allow_resolver(name, root))


def _allow_hosts(name: str, path: Path) -> str:
    """Remove the MANUAL section only."""
    raw = hostsmod.read(path)
    pattern = re.compile(
        rb"(?ms)(?:(?<=\n)\r?\n)?^" + re.escape(start_marker(name))
        + rb".*?^" + re.escape(end_marker(name)) + rb"[^\r\n]*(?:\r?\n)?"
    )
    new = pattern.sub(b"", raw)
    if new == raw:
        return "NONE"
    hostsmod.write_atomic(path, new)
    flush_dns()
    return "UNBLOCKED"
