"""On-demand hosts-file blocks, by name.

This module is the only owner of manual parental host-file toggles.

    python -m safehouse_setup toggle youtube --status
    python -m safehouse_setup toggle youtube --block
    python -m safehouse_setup toggle youtube --allow

Each name maps to a host list under `parental-blocks/<name>.txt`, so adding a new one is
a text file, not code.

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

import os
import re
import sys
from pathlib import Path

# The watcher package is the reference implementation of the splice.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safehouse_ytbudget import hosts as hostsmod  # noqa: E402

from .winutil import flush_dns, require_admin  # noqa: E402

HOSTS_PATH = Path(
    os.environ.get(
        "YB_HOSTS",
        str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32" / "drivers" / "etc" / "hosts"),
    )
)

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


def status(name: str, hosts_path: Path | None = None) -> bool:
    path = hosts_path or HOSTS_PATH
    try:
        return _any_section(name).search(hostsmod.read(path)) is not None
    except OSError:
        return False


def block(name: str, hosts_path: Path | None = None) -> str:
    path = hosts_path or HOSTS_PATH
    require_admin(f"Blocking {display_name(name)}")
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


def allow(name: str, hosts_path: Path | None = None) -> str:
    """Remove the MANUAL section only.

    An automatic budget block is left in place: lifting it here would hand back time the
    budget has already spent, and the watcher would re-apply it within a sample anyway.
    """
    path = hosts_path or HOSTS_PATH
    require_admin(f"Unblocking {display_name(name)}")
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
