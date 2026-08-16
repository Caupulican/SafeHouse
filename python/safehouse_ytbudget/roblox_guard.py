"""Roblox daily time budget - the process-kill lane.

Why this lane exists at all, when YouTube gets by with DNS + hosts + firewall: a joined
Roblox session holds a raw UDP connection to Roblox's own address space
(128.116.0.0/17) and never resolves a name again. Blocking at the resolver only stops
NEW joins; the session the child is already in would run forever. The only enforcement
that works mid-session is ending the process.

This is a ONE-SHOT, not a loop. A scheduled task (registered by install.py) runs it at
boot and on registration, repeating every minute, so a launch while the budget is spent
dies within a minute and a budget that expires mid-game ends the game within a minute.
Each run:

    observe (is Roblox running?) -> advance the day's usage -> kill if over -> persist

Metering is process wall-clock, not traffic: for Roblox "the game is open" is the thing
being limited, and it is also the only signal this one-shot can measure honestly. The
accrual between two runs is capped (MAX_GAP_SEC) so a machine that slept for six hours
with the game open does not come back to a day-long charge, and the FIRST sighting of a
running game charges nothing - there is no honest way to know how long it was already
up, and guessing high hands the child a locked game they did not earn.

Config: `roblox-budget.json` next to the state, `{"limit_min": 60}`. Absent file means
the default 60. A present-but-broken file also means the default - never "unlimited",
for the same reason the YouTube config rejects bad values instead of applying them: a
typo must not hand out unlimited Roblox.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .atomicio import write_json

DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")
DEFAULT_LIMIT_MIN = 60
MAX_GAP_SEC = 180  # cap per-run accrual; the task repeats every minute when healthy

# The desktop player, its launcher, and the Microsoft Store app's host process (that
# generic-sounding name is Roblox's own UWP container, not a Windows component).
PROCESS_NAMES = (
    "RobloxPlayerBeta.exe",
    "RobloxPlayerLauncher.exe",
    "Windows10Universal.exe",
)


@dataclass(frozen=True)
class GuardState:
    date: str
    used_sec: int
    last_running_ts: int | None  # epoch of the last run that saw Roblox up, else None

    @staticmethod
    def fresh(date: str) -> "GuardState":
        return GuardState(date=date, used_sec=0, last_running_ts=None)


def advance(state: GuardState, *, today: str, now: int, running: bool,
            limit_sec: int) -> tuple[GuardState, bool]:
    """Pure: one observation in, (new state, should we kill) out.

    Reconciles rather than latches, like every SafeHouse enforcement decision: the kill
    verdict is recomputed from scratch each run, so nothing that clears a block by hand
    stays cleared past the next minute.
    """
    if state.date != today:
        state = GuardState.fresh(today)

    if running:
        if state.last_running_ts is not None:
            elapsed = min(max(now - state.last_running_ts, 0), MAX_GAP_SEC)
            state = replace(state, used_sec=state.used_sec + elapsed)
        state = replace(state, last_running_ts=now)
    else:
        state = replace(state, last_running_ts=None)

    return state, running and state.used_sec >= limit_sec


def load_limit_sec(path: Path) -> tuple[int, str | None]:
    """Read limit_min from the config file; fall back STRICT on any defect."""
    if not path.exists():
        return DEFAULT_LIMIT_MIN * 60, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get("limit_min")
        limit = int(raw)
        if not 1 <= limit <= 24 * 60:
            raise ValueError(f"limit_min out of range: {limit}")
        return limit * 60, None
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        return DEFAULT_LIMIT_MIN * 60, (
            f"bad {path.name} ({type(exc).__name__}: {exc}) - "
            f"using the default {DEFAULT_LIMIT_MIN} min, never unlimited"
        )


def load_state(path: Path, today: str) -> tuple[GuardState, str | None]:
    if not path.exists():
        return GuardState.fresh(today), None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return GuardState(
            date=str(raw["date"]),
            used_sec=max(int(raw["used_sec"]), 0),
            last_running_ts=(None if raw.get("last_running_ts") is None
                             else int(raw["last_running_ts"])),
        ), None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        # Unlike config, a broken STATE file has no strict fallback that is fair - the
        # true usage is unknowable. Start the day fresh, but say so in the log.
        return GuardState.fresh(today), f"corrupt state, starting fresh ({type(exc).__name__})"


def save_state(path: Path, state: GuardState) -> None:
    write_json(path, {
        "date": state.date,
        "used_sec": state.used_sec,
        "last_running_ts": state.last_running_ts,
    })


def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)


def running_processes(runner=_run) -> list[str]:
    """Which of the Roblox processes are up, via tasklist's CSV output.

    `/FI` returns rc 0 with an "INFO:" line when nothing matches, so match on the
    quoted image name in the CSV rather than on the return code.
    """
    found = []
    for name in PROCESS_NAMES:
        _, out = runner(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"])
        if f'"{name}"' in out:
            found.append(name)
    return found


def kill_processes(names: list[str], runner=_run) -> list[str]:
    """taskkill /T so the launcher's children die with it. Returns what failed."""
    failures = []
    for name in names:
        rc, out = runner(["taskkill", "/F", "/T", "/IM", name])
        if rc != 0 and "not found" not in out.lower():
            failures.append(f"{name}: {out.strip()[:120]}")
    return failures


def log(path: Path, message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\r\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass  # logging must never take the guard down


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--dry-run", action="store_true",
                   help="observe, decide and log, but neither kill nor persist")
    args = p.parse_args(argv)

    state_path = args.root / "roblox-budget.state.json"
    config_path = args.root / "roblox-budget.json"
    log_path = args.root / "roblox-budget.log"

    now = int(datetime.now().timestamp())
    today = f"{datetime.now():%Y-%m-%d}"

    limit_sec, limit_warn = load_limit_sec(config_path)
    if limit_warn:
        log(log_path, f"[warn] {limit_warn}")
    state, state_warn = load_state(state_path, today)
    if state_warn:
        log(log_path, f"[warn] {state_warn}")

    up = running_processes()
    state, kill = advance(state, today=today, now=now, running=bool(up),
                          limit_sec=limit_sec)

    prefix = "[dry-run] " if args.dry_run else ""
    if kill:
        log(log_path, f"{prefix}[kill] budget spent "
                      f"(used={state.used_sec}s/{limit_sec}s) - ending {', '.join(up)}")
        if not args.dry_run:
            failures = kill_processes(up)
            for f in failures:
                log(log_path, f"[error] taskkill failed: {f}")
    elif up:
        log(log_path, f"{prefix}[sample] running used={state.used_sec}s/{limit_sec}s "
                      f"({', '.join(up)})")
    # Nothing running: stay silent. This fires every minute forever; an idle machine
    # must not grind out a log line per minute for no news.

    if not args.dry_run:
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
