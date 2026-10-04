"""Read the Play Games guard's state - never change it.

Play Games is banned on this machine, and `safehouse_ytbudget.playgames_guard` ends it
within about a minute of it starting. This tool cannot work while that is true, and it
deliberately does NOT get around it: unlocking is a parent's act, done from an elevated
prompt, logged by the guard as `[parent]`. All this module does is read the same two
files the guard reads and say whether a launch would survive, plus print the exact
commands for the parent to type.

The parsing is a small copy of the guard's `load_config`/`load_state`, not an import:
this tool runs on its own venv with its own dependencies, and the guard's package is the
stdlib-only runtime that must not grow a caller it has to stay compatible with. The copy
keeps the guard's one rule that matters - FAIL CLOSED. A missing, broken or odd file
reads as "enforcing, no unlock", because a wrong "go ahead" here means Play Games gets
killed mid-uninstall.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")
CONFIG_NAME = "playgames-guard.json"
STATE_NAME = "playgames-guard.state.json"

# The deployed runtime and the directory install.py registers the guard task with. The
# runtime's ._pth already puts that directory on sys.path, so `-m` works from anywhere;
# the `cd` is printed anyway because it is what windows/README.md teaches.
RUNTIME = r"C:\SafeHouse\python\python.exe"
WORKDIR = r"C:\SafeHouse\windows\youtube-budget"
MODULE = "safehouse_ytbudget.playgames_guard"

# Asking for less than this is asking to be killed half-way through the list. The loop
# re-checks before every game anyway; this only makes the first warning earlier.
DEFAULT_UNLOCK_MINUTES = 90
LOW_TIME_SEC = 15 * 60


@dataclass(frozen=True)
class GuardVerdict:
    ok: bool             # a launched Play Games will survive (for now)
    reason: str          # one line, for the console
    seconds_left: int    # of the unlock; 0 when disabled or locked
    disabled: bool       # the guard is off in config, not merely unlocked


def guard_command(arg: str) -> str:
    """The exact PowerShell line a parent types for `--unlock N` / `--lock` / `--status`."""
    return f"cd {WORKDIR}; & '{RUNTIME}' -m {MODULE} {arg}"


def unlock_command(minutes: int = DEFAULT_UNLOCK_MINUTES) -> str:
    return guard_command(f"--unlock {minutes}")


def lock_command() -> str:
    return guard_command("--lock")


def read_enabled(path: Path) -> bool:
    """`enabled` from playgames-guard.json. Anything but a literal `false` is True."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):  # missing, unreadable, or not JSON: enforcing
        return True
    if not isinstance(obj, dict):
        return True
    return obj.get("enabled", True) is not False


def read_unlock_until(path: Path) -> int:
    """`unlock_until_ts` from the state file, or 0 (= no unlock) on any defect."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return max(int(raw.get("unlock_until_ts", 0)), 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def verdict(*, enabled: bool, unlock_until_ts: int, now: int) -> GuardVerdict:
    """Pure: would a Play Games launch survive the guard right now?"""
    if not enabled:
        return GuardVerdict(True, f"guard is DISABLED in {CONFIG_NAME}", 0, True)
    left = unlock_until_ts - now
    if left > 0:
        mins, secs = divmod(left, 60)
        note = f"guard UNLOCKED, {mins}m{secs:02d}s left"
        if left < LOW_TIME_SEC:
            note += " (short - it re-locks by itself and Play Games will be closed)"
        return GuardVerdict(True, note, left, False)
    return GuardVerdict(False, "guard is ENFORCING - Play Games would be closed within "
                               "about a minute of starting", 0, False)


def check(root: Path, now: int) -> GuardVerdict:
    """Read both files under `root` and judge. Read-only; writes nothing."""
    return verdict(enabled=read_enabled(root / CONFIG_NAME),
                   unlock_until_ts=read_unlock_until(root / STATE_NAME),
                   now=now)
