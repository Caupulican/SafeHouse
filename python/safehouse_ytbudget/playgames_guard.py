"""No Google Play Games - the third process-kill lane.

Google Play Games (the Android-games-on-PC product) is banned outright on this machine,
decided by the parent on 2026-08-19 alongside the games-only revision. It has no budget
and no schedule: the app is simply not to run. Like Roblox mid-session, nothing at the
resolver can enforce that - the client and its Android VM are already on disk and start
without asking DNS for permission - so the enforcement is the same one the other two
lanes use: end the process.

Like those lanes, this is a ONE-SHOT. A scheduled task (registered by install.py) runs it
at boot and on registration, repeating every minute; each run observes, acts and exits,
and the condition the parent attached to that shape is unchanged and load-bearing:

    a run that finds nothing does NOTHING - no log line, no state write, no kill, no
    subprocess - and exits.

Each run:

    observe -> pause 1-5s -> observe again -> kill what is still up

What is different here is HOW a run observes, and the reason is the product's own naming:

* **Matching is by full executable path, never by image name.** Play Games runs as
  `client.exe`, `Service.exe` and around eleven `crosvm.exe` Android-VM shards - names so
  generic that an image-name kill list would be a standing invitation to end some other
  program's Service.exe. The product installs everything under ONE directory, so "runs
  from under `C:\\Program Files\\Google\\Play Games\\`" is the whole identity test. The
  trailing backslash on that prefix is load-bearing: it is what makes the match a
  directory boundary, so the SIBLING `C:\\Program Files\\Google\\Play Games Services\\` -
  a separate product ordinary Windows games use for sign-in and achievements - can never
  match. NEVER_KILL holds its basename anyway, as belt and braces with a test on each
  strap.
* **Enumeration is in-process ctypes, not a spawned tool.** `tasklist` reports image
  names, not paths, so it cannot make this match; `wmic` is gone from this Windows build;
  PowerShell's spawn alone costs ~1.9s. `K32EnumProcesses` + `QueryFullProcessImageNameW`
  answer in-process, which makes the idle path even cheaper than the other kill lanes' -
  a run that finds nothing spawns NO subprocess at all. The one `taskkill /T` on the
  action path is the only spawn this lane ever makes; `/T` takes the crosvm shards down
  with their parent.

A respawn storm here has a specific suspect: the GooglePlayGamesServices *service* was
stopped and disabled on 2026-08-22, and if kills repeat every minute something - the
service manager or Bootstrapper.exe - is restarting the client behind the guard's back.
The storm log line says exactly that, once per storm, so the fight cannot hide in a log
that looks like healthy enforcement.

Config: `playgames-guard.json` next to the state. Absent or broken means enforcing with
the defaults - it fails closed, like every lane here. A typo must not hand back a banned
app.

The honest limit is the same as the other lanes': the child has local admin on this
machine, kept by the parent's own choice, so a determined bypass (ending the task,
renaming the install directory) is possible. This raises the cost of running Play Games
from "double-click" to "deliberately defeat a control", which is the point - and the kind
of act that is a conversation, not a patch.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import killdelay

# Borrowed from the sibling lanes rather than copied, for the reason browser_guard gives:
# a fix to the shared plumbing belongs in one place. `_run`/`log` come from the Roblox
# lane like the browser lane's do; the admin gate and the storm tally come from the
# browser lane, which invented them, with the storm threshold passed explicitly so the
# lanes can retune independently. Nothing else is shared: own config, own state, own
# task, own log.
from .browser_guard import _require_admin, _storming
from .roblox_guard import _run, log

DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")

# Everything under this directory is Google Play Games and nothing else is: the
# bootstrapper and uninstaller at the top, and a version-numbered subdirectory holding
# client.exe, Service.exe, the tracer and the crosvm.exe Android-VM shards. The TRAILING
# BACKSLASH is load-bearing - it makes the prefix a directory boundary, which is what
# structurally excludes the sibling `...\Google\Play Games Services\` directory. (A raw
# string cannot end in a backslash, hence the doubled form.)
KILL_ROOT = "C:\\Program Files\\Google\\Play Games\\"

# Belt and braces under the structural exclusion above. GooglePlayGamesServices.exe is a
# SEPARATE product - the sign-in/achievements helper ordinary Windows games load - and
# killing it breaks games, which is the one outcome this whole policy exists to avoid.
# The prefix match already cannot reach it; this tuple exists so that even a future edit
# that breaks the prefix (say, by dropping the trailing backslash) still cannot end it,
# and so a test can hold both straps down. Lower-case, compared against lower-cased
# basenames.
NEVER_KILL = ("googleplaygamesservices.exe",)

# Same framing as the browser lane's storm detection: a respawn fight is one kill per
# fire, forever, which looks exactly like healthy enforcement in the log. The history is
# persisted because a one-shot sees one moment; the thresholds match the browser lane's
# reasoning (a child double-clicking is not a storm; a fight that lasts half an hour is).
STORM_WINDOW_SEC = 1800
STORM_KILLS = 12
# The window prunes first, so this only bites on a hand-edited or corrupt state file. A
# bound on how much a bad file can cost, not a tuning knob.
KILL_HISTORY_MAX = 128


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    # Seconds between seeing the app and ending it; see `killdelay` for what the pause is
    # for and why it is deliberately small at a one-minute firing cadence.
    delay_min_sec: int = killdelay.DEFAULT_MIN_SEC
    delay_max_sec: int = killdelay.DEFAULT_MAX_SEC


@dataclass(frozen=True)
class GuardState:
    """What has to outlive a run. Both fields are written; nothing here is decoration."""

    # Parent escape hatch: enforcement is suspended until this epoch. Same semantics as
    # the browser lane - a today decision that expires by itself, not a config edit
    # someone has to remember to undo.
    unlock_until_ts: int = 0
    # (image basename, epoch) of recent kills, pruned to STORM_WINDOW_SEC. Basenames, not
    # paths: the storm question is "which IMAGE keeps coming back", and a versioned
    # directory that changes on update must not reset the tally.
    kills: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Decision:
    """What this run wants the caller to do.

    Only ever built on a run that already saw the app - the idle path returns before
    `decide` is reached at all - so anything set here is safe to log.
    """

    kill: tuple[tuple[int, str], ...] = ()
    # Enforcement was suppressed, or the app went away during the pause, and why.
    stood_down: str | None = None
    # The image basename that looks like something is restarting it. Reported once per
    # storm, not once per kill.
    storm: str | None = None


def _image(path: str) -> str:
    """The basename of a full Windows path, as the log and the tally name it."""
    return path.replace("/", "\\").rpartition("\\")[2]


def banned(path: str) -> bool:
    """Is this full executable path Google Play Games - and safe to end?

    A case-insensitive directory-prefix test, because NTFS is case-insensitive and the
    path comes back from the kernel in whatever case the launcher used. The prefix keeps
    `...\\Play Games Services\\...` out structurally (see KILL_ROOT); the NEVER_KILL check
    keeps its basename out even if the prefix ever stops doing that.
    """
    lowered = path.lower()
    if not lowered.startswith(KILL_ROOT.lower()):
        return False
    return _image(lowered) not in NEVER_KILL


def suppressed(cfg: GuardConfig, state: GuardState, now: int) -> str | None:
    """Why enforcement is standing down right now, or None.

    Checked before the process enumeration as well as inside `decide`: when the answer is
    "disabled" or "unlocked" there is no reason to walk the process table at all, and a
    run that touches nothing is the whole bargain of this task shape.
    """
    if not cfg.enabled:
        return "the guard is disabled in playgames-guard.json"
    if now < state.unlock_until_ts:
        return f"a parent unlock is active for another {state.unlock_until_ts - now}s"
    return None


def decide(cfg: GuardConfig, state: GuardState, *, running: tuple[tuple[int, str], ...],
           now: int) -> tuple[GuardState, Decision]:
    """Pure: the observation taken AFTER the pause in, (new state, what to do) out.

    Reconciles rather than latches, like every SafeHouse enforcement decision: the kill
    set is recomputed from what is up RIGHT NOW, so a process started during the pause
    dies with the rest, and one that exited during it is not reported as a kill it never
    was.
    """
    running = tuple(sorted(running))
    kept = tuple((n, ts) for n, ts in state.kills if now - ts <= STORM_WINDOW_SEC)
    state = replace(state, kills=kept[-KILL_HISTORY_MAX:])

    reason = suppressed(cfg, state, now)
    if reason is not None:
        return state, Decision(stood_down=reason)
    if not running:
        return state, Decision(stood_down="closed during the pause")

    # One tally tick per IMAGE per run, not one per process: eleven crosvm.exe VM shards
    # dying together are one launch of the app, and counting them separately would let a
    # single afternoon of double-clicking impersonate a respawn storm. A storm is
    # reported when the tally CROSSES the threshold, not on every kill past it.
    before = _storming(state, STORM_KILLS)
    names = tuple(sorted({_image(path).lower() for _, path in running}))
    history = state.kills + tuple((name, now) for name in names)
    state = replace(state, kills=history[-KILL_HISTORY_MAX:])
    after = _storming(state, STORM_KILLS)
    return state, Decision(kill=running,
                           storm=after if (after is not None and before is None) else None)


# --- config ------------------------------------------------------------------

def load_config(path: Path) -> tuple[GuardConfig, list[str]]:
    """Read playgames-guard.json. Every defect falls back to ENFORCING with the defaults.

    `enabled: false` is honoured, because a parent must be able to stand the lane down -
    but only as a deliberate boolean. A truncated file, a stray comma or a string where a
    number belongs all mean "enforce", never "allow": the failure mode of a config typo
    has to be an app that closes, not a banned app quietly handed back.
    """
    if not path.exists():
        return GuardConfig(), []
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return GuardConfig(), [
            f"bad {path.name} ({type(exc).__name__}: {exc}) - enforcing with the defaults"
        ]

    warnings: list[str] = []

    enabled = obj.get("enabled", True)
    if not isinstance(enabled, bool):
        warnings.append(f"enabled must be true or false (got {enabled!r}), enforcing")
        enabled = True

    low, high = killdelay.load_bounds(obj, warnings)
    return GuardConfig(enabled=enabled, delay_min_sec=low, delay_max_sec=high), warnings


# --- state -------------------------------------------------------------------

def load_state(path: Path) -> tuple[GuardState, str | None]:
    if not path.exists():
        return GuardState(), None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        kills = tuple(
            (str(name), int(ts)) for name, ts in (raw.get("kills") or [])
        )[-KILL_HISTORY_MAX:]
        return GuardState(
            unlock_until_ts=max(int(raw.get("unlock_until_ts", 0)), 0),
            kills=kills,
        ), None
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        # A broken state file means no unlock, which is the enforcing side. Say so anyway
        # - but only on a run that has something to say (see main): repairing it costs a
        # write, and a write per minute on an idle machine is what this shape avoids.
        return GuardState(), f"corrupt state, no unlock in force ({type(exc).__name__})"


def save_state(path: Path, state: GuardState) -> None:
    from .atomicio import write_json

    write_json(path, {
        "unlock_until_ts": state.unlock_until_ts,
        "kills": [[name, ts] for name, ts in state.kills],
    })


# --- the Windows side --------------------------------------------------------

def list_processes() -> list[tuple[int, str]]:
    """Every (pid, full executable path) this process may query. Windows only.

    Raw ctypes because the vendored runtime is stdlib-only, and in-process because this
    IS the idle path: it runs every minute forever, and the whole bargain of the task
    shape is that a run that finds nothing spawns nothing. All Windows types stay inside
    this function so the module imports cleanly on Linux, where the tests run.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    # K32EnumProcesses reports only how many DWORDs it FILLED: a full array means there
    # may be more, so grow until there is slack. 4096 pids is already several times a
    # busy desktop.
    size = 4096
    while True:
        pids = (wintypes.DWORD * size)()
        filled = wintypes.DWORD()
        if not kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(filled)):
            return []
        count = filled.value // ctypes.sizeof(wintypes.DWORD)
        if count < size:
            break
        size *= 2

    query_limited = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    found: list[tuple[int, str]] = []
    for pid in pids[:count]:
        if not pid:
            continue  # the Idle pseudo-process
        handle = kernel32.OpenProcess(query_limited, False, pid)
        if not handle:
            # Protected system processes refuse even the limited query. Nothing that
            # lives under Program Files\Google is among them, so skipping is correct.
            continue
        try:
            length = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(length.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
                found.append((int(pid), buf.value))
        finally:
            kernel32.CloseHandle(handle)
    return found


def running_playgames(lister=list_processes) -> list[tuple[int, str]]:
    """Which Play Games processes are up, as (pid, full path).

    `lister` is injectable the way the other lanes' `runner` is, so the identity test -
    the part that decides what dies - runs under tests on Linux against recorded process
    tables, with the ctypes walk swapped out.
    """
    return [(pid, path) for pid, path in lister() if banned(path)]


def kill_processes(targets: tuple[tuple[int, str], ...], runner=_run) -> list[str]:
    """ONE `taskkill /F /T /PID n [/PID m]...` for the whole set. Returns what failed.

    By PID, necessarily: the images are generic names, so `/IM client.exe` would reach
    beyond the directory the identity test just confined us to. `/T` takes each crosvm
    shard's children down with it, and one invocation - the only subprocess this lane
    ever spawns - covers the lot. "not found" is not a failure: a pid that exited
    between the recheck and the kill was closed by the child, which is fine.
    """
    if not targets:
        return []
    args = ["taskkill", "/F", "/T"]
    for pid, _ in targets:
        args += ["/PID", str(pid)]
    rc, out = runner(args)
    if rc != 0 and "not found" not in out.lower():
        return [out.strip()[:200]]
    return []


# --- entry point -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safehouse-playgames-guard", description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--dry-run", action="store_true",
                   help="observe, decide and log, but neither kill nor persist")
    p.add_argument("--status", action="store_true",
                   help="print whether the guard is enforcing and any unlock left")
    p.add_argument("--unlock", type=int, metavar="MINUTES", default=None,
                   help="suspend enforcement for this many minutes (0 cancels)")
    p.add_argument("--lock", action="store_true", help="cancel any active unlock now")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config_path = args.root / "playgames-guard.json"
    state_path = args.root / "playgames-guard.state.json"
    log_path = args.root / "playgames-guard.log"

    cfg, warnings = load_config(config_path)
    loaded, state_warn = load_state(state_path)
    now = int(datetime.now().timestamp())

    if args.status:
        left = max(0, loaded.unlock_until_ts - now)
        recent = sum(1 for _, ts in loaded.kills if now - ts <= STORM_WINDOW_SEC)
        print(f"Play Games guard - "
              f"{'ENFORCING' if cfg.enabled else 'DISABLED in ' + config_path.name}")
        print(f"  Closes     : everything running from under {KILL_ROOT}")
        print(f"  Spares     : {', '.join(NEVER_KILL)} - and all of the sibling "
              f"'Play Games Services' directory, which games need for sign-in")
        print(f"  Fires      : every minute (scheduled task), then waits "
              f"{cfg.delay_min_sec}-{cfg.delay_max_sec}s before ending anything")
        print(f"  Unlock     : {f'{left // 60}m{left % 60:02d}s left' if left else 'none'}")
        print(f"  Recent     : {recent} close(s) in the last "
              f"{STORM_WINDOW_SEC // 60} minutes")
        for w in warnings + ([state_warn] if state_warn else []):
            print(f"  [warn] {w}")
        return 0

    if args.lock or args.unlock is not None:
        minutes = 0 if args.lock else max(0, args.unlock)
        _require_admin("Changing the playgames-guard unlock")
        state = replace(loaded, unlock_until_ts=now + minutes * 60 if minutes else 0)
        save_state(state_path, state)
        note = (f"unlocked for {minutes}m" if minutes else "unlock cancelled")
        log(log_path, f"[parent] {note}")
        print(f"Play Games guard {note}.")
        return 0

    try:
        # THE IDLE PATH, and the reason this lane may fire every minute at all: when the
        # guard is off, unlocked, or Play Games is not up, the run must touch NOTHING -
        # no log line, no state write, no subprocess (the enumeration above is
        # in-process). Everything below only happens on a run that has actually seen the
        # app, which is why it can afford to sleep, log, spawn and write.
        if suppressed(cfg, loaded, now) is not None:
            return 0
        up = tuple(running_playgames())
        if not up:
            return 0

        prefix = "[dry-run] " if args.dry_run else ""
        # Warnings are held until here for the same reason. A config typo on an idle
        # machine would otherwise write the same line 1,440 times a day.
        for w in warnings + ([state_warn] if state_warn else []):
            log(log_path, f"[warn] {w}")

        names = ", ".join(sorted({_image(path) for _, path in up}))
        _, still = killdelay.pause_and_recheck(
            running_playgames,
            low=cfg.delay_min_sec, high=cfg.delay_max_sec,
            announce=lambda d: log(log_path, f"{prefix}[wait] Play Games is up "
                                             f"({len(up)} process(es): {names}) - "
                                             f"ending it in {d}s"),
        )
        now = int(datetime.now().timestamp())
        state, decision = decide(cfg, loaded, running=tuple(still), now=now)

        if decision.stood_down:
            log(log_path, f"{prefix}[stand-down] {decision.stood_down}")
        if decision.storm:
            log(log_path,
                f"[error] {decision.storm} has been ended {STORM_KILLS}+ times in "
                f"{STORM_WINDOW_SEC // 60} minutes - something is restarting it, not the "
                f"child. The GooglePlayGamesServices service was stopped and disabled on "
                f"2026-08-22; check its StartType (`sc qc GooglePlayGamesServices`) and "
                f"whether Bootstrapper.exe is re-launching the client.")
        if decision.kill:
            ended = ", ".join(sorted({_image(path) for _, path in decision.kill}))
            log(log_path, f"{prefix}[kill] ending {len(decision.kill)} process(es) "
                          f"({ended})")
            if not args.dry_run:
                for failure in kill_processes(decision.kill):
                    log(log_path, f"[error] taskkill failed: {failure}")

        # Only on a real change: the kill history and the unlock are the only things that
        # outlive a run, and a run that changed neither has nothing to persist.
        if not args.dry_run and state != loaded:
            save_state(state_path, state)
        return 0
    except Exception as exc:  # noqa: BLE001 - a failure must be visible, not silent
        log(log_path, f"[error] run failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
