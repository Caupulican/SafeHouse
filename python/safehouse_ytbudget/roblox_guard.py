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

...and a run that finds the game closed does NOTHING: no log line, no state write, no
second subprocess. That is the condition the parent attached to letting this task fire
every minute of every day, and it is not a nicety - unconditional persistence here was
~1,440 atomic write-and-rename cycles a day on a machine where the game may never have
been opened.

Metering is process wall-clock, not traffic: for Roblox "the game is open" is the thing
being limited, and it is also the only signal this one-shot can measure honestly. The
accrual between two runs is capped (MAX_GAP_SEC) so a machine that slept for six hours
with the game open does not come back to a day-long charge, and the FIRST sighting of a
running game charges nothing - there is no honest way to know how long it was already
up, and guessing high hands the child a locked game they did not earn.

A run whose verdict is "kill" waits a random 1-5 seconds and looks again, ending only
what is still up (see `killdelay`). At a one-minute firing interval that pause is a small
part of the latency - up to ~60s to notice, then 1-5s - and it is not there to soften the
enforcement. It is there so the exact moment cannot be timed, and so a child who quits
the game during it is not reported as a kill that never happened.

Config: `roblox-budget.json` next to the state, `{"limit_min": 0}`. Zero is the standing
policy and is a legitimate value, exactly as it is for the YouTube budget: it means "not
allowed", not "unconfigured". It is also the strict fallback - an absent or broken config
file means zero, so a typo takes the game away rather than handing out an hour nobody
granted. A parent who wants to allow Roblox raises `limit_min` in that file, or drops a
one-off `bonus_sec` into the state file for today only. The same file carries
`delay_min_sec`/`delay_max_sec` for the pause above.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import killdelay
from .atomicio import write_json

DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")
DEFAULT_LIMIT_MIN = 0
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
    # Parent-granted extra time for THIS date only. Lives in state, not config, for the
    # same reason the YouTube budget separates them: a grant is a today decision, and
    # the date rollover must take it with it rather than quietly becoming the new limit.
    bonus_sec: int = 0

    @staticmethod
    def fresh(date: str) -> "GuardState":
        return GuardState(date=date, used_sec=0, last_running_ts=None, bonus_sec=0)


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

    return state, running and state.used_sec >= limit_sec + state.bonus_sec


def load_limit_sec(path: Path) -> tuple[int, str | None]:
    """Read limit_min from the config file; fall back STRICT on any defect."""
    if not path.exists():
        return DEFAULT_LIMIT_MIN * 60, None
    try:
        # utf-8-sig, not utf-8: Windows PowerShell 5.1's `Set-Content -Encoding UTF8`
        # writes a BOM, and a BOM made this file unparseable - so a parent who granted an
        # hour from a PowerShell prompt silently got zero. Failing closed is right for a
        # defect; an encoding artefact in a file that says 60 is not a defect.
        raw = json.loads(path.read_text(encoding="utf-8-sig")).get("limit_min")
        limit = int(raw)
        # Zero is allowed and is the standing policy. Rejecting it, as this once did,
        # meant `{"limit_min": 0}` fell through to the fallback and quietly granted an
        # hour - the exact opposite of what the parent had written down.
        if not 0 <= limit <= 24 * 60:
            raise ValueError(f"limit_min out of range: {limit}")
        return limit * 60, None
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        return DEFAULT_LIMIT_MIN * 60, (
            f"bad {path.name} ({type(exc).__name__}: {exc}) - "
            f"falling back to {DEFAULT_LIMIT_MIN} min, never unlimited"
        )


def load_delay_sec(path: Path) -> tuple[tuple[int, int], list[str]]:
    """Read the kill-pause bounds from the same config file as the limit.

    Read separately, and only on a run that has already decided to kill: it is one more
    file read on the rarest path, rather than a read the idle path pays for every minute.

    The validation discipline is `load_limit_sec`'s, minus its paranoia - and deliberately
    so. Every failure mode of these two keys lands on a SHORT pause, so no value and no
    typo can widen what the child gets; the worst a defect does is make the game close a
    second sooner than the parent wrote down.
    """
    default = (killdelay.DEFAULT_MIN_SEC, killdelay.DEFAULT_MAX_SEC)
    if not path.exists():
        return default, []
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return default, [f"bad {path.name} ({type(exc).__name__}: {exc}) - "
                         f"pausing {default[0]}-{default[1]}s before the kill"]
    warnings: list[str] = []
    return killdelay.load_bounds(obj, warnings), warnings


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
            bonus_sec=max(int(raw.get("bonus_sec", 0)), 0),
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
        "bonus_sec": state.bonus_sec,
    })


def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)


def running_processes(runner=_run) -> list[str]:
    """Which of the Roblox processes are up, from ONE tasklist CSV dump.

    The match is `"<name>"` against the quoted CSV, never a substring of the line: an
    exact quoted-name comparison is what stops a longer image name (or, on the verbose
    listing, a window title) matching a shorter one. `/FI` used to do this filtering, one
    spawn per name; it returns rc 0 with an "INFO:" line when nothing matches, so the
    quoted match was needed even then.

    One dump replaced those three calls because every `/FI` enumerates the whole process
    table before filtering it - three filtered calls do the same work three times and pay
    three process spawns for it. This is the idle path: it runs every minute, forever, on
    a machine whose job is running games, and it is the only thing an idle run does at
    all. The browser lane made the same trade for the same reason.
    """
    _, out = runner(["tasklist", "/FO", "CSV", "/NH"])
    return [name for name in PROCESS_NAMES if f'"{name}"' in out]


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

    # The one thing an idle run does. Everything below is reads and arithmetic until
    # there is actual news.
    up = running_processes()

    limit_sec, limit_warn = load_limit_sec(config_path)
    loaded, state_warn = load_state(state_path, today)
    state, kill = advance(loaded, today=today, now=now, running=bool(up),
                          limit_sec=limit_sec)

    # Post-advance, so a rollover that just cleared yesterday's bonus is reflected.
    effective = limit_sec + state.bonus_sec
    budget = (f"{effective}s" if not state.bonus_sec
              else f"{effective}s ({limit_sec}+{state.bonus_sec} bonus)")
    prefix = "[dry-run] " if args.dry_run else ""

    if up:
        # Held back until there is something to say. A broken config would otherwise
        # write the same warning 1,440 times a day on a machine nobody played on.
        for warning in (limit_warn, state_warn):
            if warning:
                log(log_path, f"[warn] {warning}")

    if kill:
        (low, high), delay_warnings = load_delay_sec(config_path)
        for warning in delay_warnings:
            log(log_path, f"[warn] {warning}")
        # Announce first, then pause, then look again: a child who quits during those
        # seconds closed the game himself, and must not be logged as a kill.
        _, still = killdelay.pause_and_recheck(
            running_processes, low=low, high=high,
            announce=lambda d: log(log_path, f"{prefix}[kill] budget spent "
                                             f"(used={state.used_sec}s/{budget}) - "
                                             f"ending {', '.join(up)} in {d}s"),
        )
        if not still:
            log(log_path, f"{prefix}[stand-down] closed during the pause - "
                          f"nothing left to end")
        elif not args.dry_run:
            for failure in kill_processes(list(still)):
                log(log_path, f"[error] taskkill failed: {failure}")
    elif up:
        log(log_path, f"{prefix}[sample] running used={state.used_sec}s/{budget} "
                      f"({', '.join(up)})")
    # Nothing running: stay silent. This fires every minute forever; an idle machine
    # must not grind out a log line per minute for no news.

    # ...and must not grind out a WRITE per minute either. The idle path has to touch
    # nothing at all: an atomic write is a temp file, an fsync and a rename, and on a day
    # the game is never opened every one of them would persist a state identical to the
    # one just read. A corrupt state file is therefore left un-repaired until the lane
    # next has real work - it already reads as "no usage", which is the enforcing side.
    if not args.dry_run and state != loaded:
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
