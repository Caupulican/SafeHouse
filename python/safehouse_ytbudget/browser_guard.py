"""No web browsing - the second process-kill lane.

The standing policy on this machine is that an 8-year-old plays games and does not
browse the web. No resolver can express that: blocking every site a browser could reach
is an infinite list, and the browser is the thing being ruled out, not any one page. So
the enforcement is the same one Roblox gets - end the process - applied to the browser
itself.

Like the Roblox guard, this is a ONE-SHOT. A scheduled task (registered by install.py)
runs it at boot and on registration, repeating every minute; each run observes, acts and
exits. It began as a resident loop polling every few seconds, so that the gap between a
browser opening and closing could be sub-minute. The parent chose this shape over that
one, and the condition attached to the choice is the load-bearing part:

    a run that finds no browser does NOTHING - no log line, no state write, no kill, no
    second subprocess - and exits.

The price is honest: up to a minute passes before a launch is noticed. What is bought is
that on a machine where nobody is browsing, which is every ordinary day here, the guard
costs one `tasklist` a minute and nothing else - no process resident all day on a PC
whose job is running games.

Each run:

    observe -> pause 1-5s -> observe again -> kill what is still up

Two properties are load-bearing and have tests holding them down:

1. **Only real browsers die.** The kill list is exact image names matched against
   `tasklist`'s quoted CSV output, never a prefix or substring. `msedgewebview2.exe` is
   WebView2 - Windows Widgets, Outlook and Teams are drawn with it - and `msedge.exe`
   must not match it. `steamwebhelper.exe` is the Steam client's own Chromium, and Steam
   is how the child launches games. The whole point of this policy is that games keep
   working; a guard that breaks them is a guard the parent turns off.

2. **The child closing his own browser is not a kill.** The pause is followed by a second
   observation (see `killdelay`), so a window that goes away while the guard is waiting
   simply stands it down.

Config: `browser-guard.json` next to the state. Absent or broken means enforcing with the
defaults - it fails closed, for the same reason the Roblox limit falls back to zero
rather than to unlimited. A typo must not hand out an afternoon of browsing.

This lane depends on `python -m safehouse_setup browser` having been applied first. Edge
Startup Boost and Chrome/Edge background mode keep a browser process resident with no
window at all; without those policies off, "a browser is running" is not a statement
about anything the child did, and this guard spends the day fighting Windows. It detects
that fight (see STORM_KILLS) rather than assuming it away.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import killdelay

# Borrowed from the Roblox lane rather than copied: both guards drive tasklist/taskkill
# under the same contract and append to a log with the same format, so a fix to either
# belongs in one place. The pause between seeing and killing is shared too, but through
# `killdelay`, which imports neither guard - importing it from roblox_guard would make
# the dependency a ring. Nothing else is shared: own config, own state, own task.
from .roblox_guard import _run, log

DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")

# Standalone browsers. Every entry here is a program whose entire purpose is browsing the
# web; nothing on this list is a component of something else.
BROWSER_PROCESSES = (
    "msedge.exe",
    "chrome.exe",
    "opera.exe",
    "opera_gx.exe",
    "firefox.exe",
    "brave.exe",
    "vivaldi.exe",
    "iexplore.exe",
    "chromium.exe",
)

# Chromium wearing someone else's clothes. None of these is a browser the child can
# browse in, and each one belongs to something that must keep working:
#
#   msedgewebview2.exe  WebView2. Windows Widgets, the Outlook and Teams clients and a
#                       long tail of ordinary apps render their UI in it. Killing it
#                       breaks the shell experience, not a browser - and note that
#                       "msedge.exe" is a prefix of this name, which is exactly why the
#                       matching below is an exact quoted-CSV comparison.
#   steamwebhelper.exe  Steam's embedded Chromium: the store, the library and the friends
#                       list. Steam is how the child launches games.
#   EpicWebHelper.exe   the same thing for the Epic launcher.
#   RobloxPlayerBeta.exe  Roblox draws parts of its UI with a web engine. Roblox has its
#                       own lane (roblox_guard) with its own budget and must not be
#                       collateral damage here.
#
# Nothing reads this tuple at runtime. It exists so the reasoning is written down next to
# the kill list, and so a test can assert the two never overlap.
NEVER_KILL = (
    "msedgewebview2.exe",
    "steamwebhelper.exe",
    "EpicWebHelper.exe",
    "RobloxPlayerBeta.exe",
)

# A respawn fight looks like healthy enforcement in the log - a kill every minute or so,
# forever - which is precisely the kind of failure this repo refuses to let hide. Edge
# Startup Boost restarts the process the moment it is killed, so under this task shape a
# storm is exactly one kill per fire, indefinitely.
#
# The thresholds were retuned when the lane stopped being a resident loop. A single run
# can no longer see a pattern at all, so the history is PERSISTED in the state file; and
# the old "6 kills in 10 minutes" is only six minutes of a child double-clicking at the
# new cadence, which would misreport him as Windows. Twelve kills over half an hour is a
# fight that does not stop. A determined child can still trip it, and the cost of that
# false positive is one wrong log line plus muted toasts - against a false negative that
# is a guard fighting the OS all day in silence.
STORM_WINDOW_SEC = 1800
STORM_KILLS = 12
# The window prunes first, so this only bites on a hand-edited or corrupt state file. It
# is a bound on how much a bad file can cost, not a tuning knob.
KILL_HISTORY_MAX = 128

DEFAULT_MESSAGE = ("Browsing is off on this PC. Games still work - "
                   "ask a grown-up if you need a web page.")


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    # Seconds between seeing a browser and ending it; see `killdelay` for what the pause
    # is for and why it is deliberately small now that the task fires once a minute.
    delay_min_sec: int = killdelay.DEFAULT_MIN_SEC
    delay_max_sec: int = killdelay.DEFAULT_MAX_SEC
    # Windows usernames this guard may act on. EMPTY MEANS ALL USERS, which is the
    # default. The parent uses this PC too, and a guard that closes their browser as well
    # is a guard they will switch off within a day - but the filter is off by default
    # because a mistyped name fails OPEN, and silently enforcing nothing is worse than
    # occasionally being too broad.
    users: tuple[str, ...] = ()
    # Whether to put a message on screen when a browser is ended. OFF by design, at the
    # parent's instruction: the child is not to be told that something is enforcing this.
    # He still sees the window close, so this buys silence about the CAUSE, not secrecy
    # about the effect - worth knowing when deciding what to say to him directly.
    notify: bool = False
    message: str = DEFAULT_MESSAGE


@dataclass(frozen=True)
class GuardState:
    """What has to outlive a run. Both fields are written; nothing here is decoration.

    There is no countdown any more. The pause happens inside a single run, so nothing
    about it needs persisting - and persisting it would have been wrong anyway: resuming
    a timer that started before a reboot kills a browser the moment the machine comes
    back, which is the "broken PC" reading the pause exists to avoid.
    """

    # Parent escape hatch: enforcement is suspended until this epoch. The Roblox lane's
    # `bonus_sec` is the same idea - a today decision that expires by itself, rather than
    # a config edit someone has to remember to undo.
    unlock_until_ts: int = 0
    # (image name, epoch) of recent kills, pruned to STORM_WINDOW_SEC. A one-shot sees
    # one moment, so storm detection is only possible if this survives between runs.
    kills: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Decision:
    """What this run wants the caller to do.

    Only ever built on a run that already saw a browser - the idle path returns before
    `decide` is reached at all - so anything set here is safe to log.
    """

    kill: tuple[str, ...] = ()
    # Enforcement was suppressed, or the browser went away during the pause, and why.
    stood_down: str | None = None
    notify: bool = False
    # The image name that looks like it is being restarted by Windows rather than by the
    # child. Reported once per storm, not once per kill.
    storm: str | None = None


def _storming(state: GuardState, count: int = STORM_KILLS) -> str | None:
    tally: dict[str, int] = {}
    for name, _ in state.kills:
        tally[name] = tally.get(name, 0) + 1
        if tally[name] >= count:
            return name
    return None


def suppressed(cfg: GuardConfig, state: GuardState, now: int) -> str | None:
    """Why enforcement is standing down right now, or None.

    Checked before the process listing as well as inside `decide`: when the answer is
    "disabled" or "unlocked" there is no reason to spawn `tasklist` at all, and a run
    that spawns nothing is the whole bargain of this task shape.
    """
    if not cfg.enabled:
        return "the guard is disabled in browser-guard.json"
    if now < state.unlock_until_ts:
        return f"a parent unlock is active for another {state.unlock_until_ts - now}s"
    return None


def decide(cfg: GuardConfig, state: GuardState, *, running: tuple[str, ...],
           now: int) -> tuple[GuardState, Decision]:
    """Pure: the observation taken AFTER the pause in, (new state, what to do) out.

    Reconciles rather than latches, like every SafeHouse enforcement decision: the kill
    set is recomputed from what is up RIGHT NOW, so a second browser opened during the
    pause dies with the first, and one closed during the pause is not reported as a kill
    it never was.
    """
    running = tuple(sorted(running))
    kept = tuple((n, ts) for n, ts in state.kills if now - ts <= STORM_WINDOW_SEC)
    state = replace(state, kills=kept[-KILL_HISTORY_MAX:])

    reason = suppressed(cfg, state, now)
    if reason is not None:
        return state, Decision(stood_down=reason)
    if not running:
        return state, Decision(stood_down="closed during the pause")

    # A storm is reported when the tally CROSSES the threshold, not on every kill past
    # it, so one respawn fight is one log line rather than one a minute.
    before = _storming(state)
    history = state.kills + tuple((name, now) for name in running)
    state = replace(state, kills=history[-KILL_HISTORY_MAX:])
    after = _storming(state)
    return state, Decision(kill=running, notify=after is None,
                           storm=after if (after is not None and before is None) else None)


# --- config ------------------------------------------------------------------

def load_config(path: Path) -> tuple[GuardConfig, list[str]]:
    """Read browser-guard.json. Every defect falls back to ENFORCING with the defaults.

    `enabled: false` is honoured, because a parent must be able to stand the lane down -
    but only as a deliberate boolean. A truncated file, a stray comma or a string where a
    number belongs all mean "enforce", never "allow": the failure mode of a config typo
    has to be a browser that closes, not an afternoon of unsupervised web.
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

    raw_users = obj.get("users", [])
    if not isinstance(raw_users, list) or any(not isinstance(u, str) for u in raw_users):
        warnings.append(f"users must be a list of names (got {raw_users!r}), "
                        "acting on every user")
        raw_users = []
    users = tuple(u.strip() for u in raw_users if u.strip())

    notify = obj.get("notify", GuardConfig.notify)
    if not isinstance(notify, bool):
        warnings.append(f"notify must be true or false (got {notify!r}), staying silent")
        notify = False

    message = obj.get("message", DEFAULT_MESSAGE)
    if not isinstance(message, str) or not message.strip():
        warnings.append("message must be a non-empty string, using the default")
        message = DEFAULT_MESSAGE

    return GuardConfig(enabled=enabled, delay_min_sec=low, delay_max_sec=high,
                       users=users, notify=notify, message=message), warnings


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

def _owned_by(csv_text: str, name: str, users: tuple[str, ...]) -> bool:
    """Does any instance of `name` in this `tasklist /V` CSV belong to one of `users`?

    A SYSTEM-context `tasklist /V` reports `N/A` for another session's WINDOW TITLE, but
    it does report the user name, which is the column this reads. Names are compared
    without the domain part, so `DESKTOP-ABC\\child` matches `child`.
    """
    wanted = {u.lower().rpartition("\\")[2] for u in users}
    for row in csv.reader(csv_text.splitlines()):
        if len(row) < 7 or row[0].strip().lower() != name.lower():
            continue
        if row[6].strip().lower().rpartition("\\")[2] in wanted:
            return True
    return False


def running_browsers(runner=_run, users: tuple[str, ...] = ()) -> list[str]:
    """Which browsers are up, from one tasklist CSV dump.

    The match is `"<name>"` against the quoted CSV, the same test the Roblox lane makes,
    and that exactness is the safety property: `"msedge.exe"` cannot match the row for
    `"msedgewebview2.exe"`, so WebView2 - and with it Widgets, Outlook and Teams -
    survives. A substring or startswith test here would break Windows itself.

    ONE unfiltered listing, never one `/FI` call per name. Every `/FI` enumerates the
    whole process table before filtering it, so nine filtered calls do the same work nine
    times and pay nine process spawns for it. This runs on every fire of both lanes'
    idle path, on a machine whose job is running games - it is the one place the cost is
    worth caring about.

    Without `/V` the columns are image name, PID, session, session number and memory:
    nothing that can carry an arbitrary string, so a quoted-name match cannot be spoofed
    by, say, a window title.
    """
    args = ["tasklist", "/FO", "CSV", "/NH"]
    if users:
        args.insert(1, "/V")  # adds the User Name column
    _, out = runner(args)
    if users:
        return [name for name in BROWSER_PROCESSES if _owned_by(out, name, users)]
    return [name for name in BROWSER_PROCESSES if f'"{name}"' in out]


def kill_processes(names: tuple[str, ...], runner=_run) -> list[str]:
    """taskkill /T so helper children die with the browser. Returns what failed.

    Note this is image-wide: with `users` set, the filter decides WHETHER a browser is
    ended, but the kill still ends every instance of that image. If a parent is browsing
    in the same browser at the same moment, in a session of their own, their window goes
    too. Killing by PID would avoid it and is not worth the complexity here - the filter's
    job is to keep the guard off a machine where only the parent is at the keyboard.
    """
    failures = []
    for name in names:
        rc, out = runner(["taskkill", "/F", "/T", "/IM", name])
        if rc != 0 and "not found" not in out.lower():
            failures.append(f"{name}: {out.strip()[:120]}")
    return failures


def notify(message: str, runner=_run) -> None:
    """Put a message on screen, best effort. Only called when `notify` is enabled.

    It ships DISABLED. The argument for telling him was that a window vanishing with no
    explanation is confusing at eight; the parent's decision is that he is not to know a
    control is doing it, and that is theirs to make. The code stays because the switch is
    meant to be flippable, not because anything currently calls it.

    `msg.exe` does not exist on Windows Home, so this is allowed to fail entirely and say
    nothing - same contract as `log`: a message must never take the guard down.
    """
    try:
        runner(["msg.exe", "*", "/TIME:10", message])
    except Exception:  # noqa: BLE001 - deliberately total; see the docstring
        pass


# --- entry point -------------------------------------------------------------

def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, ImportError, OSError):
        return False


def _require_admin(what: str) -> None:
    if not _is_admin():
        raise SystemExit(
            f"{what} needs Administrator rights.\n"
            f"Re-run from an elevated prompt (right-click -> Run as administrator)."
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safehouse-browser-guard", description=__doc__)
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

    config_path = args.root / "browser-guard.json"
    state_path = args.root / "browser-guard.state.json"
    log_path = args.root / "browser-guard.log"

    cfg, warnings = load_config(config_path)
    loaded, state_warn = load_state(state_path)
    now = int(datetime.now().timestamp())

    if args.status:
        left = max(0, loaded.unlock_until_ts - now)
        recent = sum(1 for _, ts in loaded.kills if now - ts <= STORM_WINDOW_SEC)
        print(f"Browser guard - "
              f"{'ENFORCING' if cfg.enabled else 'DISABLED in ' + config_path.name}")
        print(f"  Closes     : {', '.join(BROWSER_PROCESSES)}")
        print(f"  Fires      : every minute (scheduled task), then waits "
              f"{cfg.delay_min_sec}-{cfg.delay_max_sec}s before ending anything")
        print(f"  Users      : {', '.join(cfg.users) if cfg.users else 'all'}")
        print(f"  On screen  : {'shows a message' if cfg.notify else 'nothing - closes silently'}")
        print(f"  Unlock     : {f'{left // 60}m{left % 60:02d}s left' if left else 'none'}")
        print(f"  Recent     : {recent} close(s) in the last "
              f"{STORM_WINDOW_SEC // 60} minutes")
        for w in warnings + ([state_warn] if state_warn else []):
            print(f"  [warn] {w}")
        return 0

    if args.lock or args.unlock is not None:
        minutes = 0 if args.lock else max(0, args.unlock)
        _require_admin("Changing the browser-guard unlock")
        state = replace(loaded, unlock_until_ts=now + minutes * 60 if minutes else 0)
        save_state(state_path, state)
        note = (f"unlocked for {minutes}m" if minutes else "unlock cancelled")
        log(log_path, f"[parent] {note}")
        print(f"Browser guard {note}.")
        return 0

    try:
        # THE IDLE PATH, and the reason this lane may fire every minute at all: when the
        # guard is off, unlocked, or no browser is up, the run must touch NOTHING - no
        # log line, no state write, not even a second subprocess. Everything below this
        # point only happens on a run that has actually seen a browser, which is why it
        # can afford to sleep, log and write.
        if suppressed(cfg, loaded, now) is not None:
            return 0
        up = tuple(running_browsers(users=cfg.users))
        if not up:
            return 0

        prefix = "[dry-run] " if args.dry_run else ""
        # Warnings are held until here for the same reason. A config typo on an idle
        # machine would otherwise write the same line 1,440 times a day.
        for w in warnings + ([state_warn] if state_warn else []):
            log(log_path, f"[warn] {w}")

        _, still = killdelay.pause_and_recheck(
            lambda: running_browsers(users=cfg.users),
            low=cfg.delay_min_sec, high=cfg.delay_max_sec,
            announce=lambda d: log(log_path, f"{prefix}[wait] {', '.join(up)} is open - "
                                             f"ending it in {d}s"),
        )
        now = int(datetime.now().timestamp())
        state, decision = decide(cfg, loaded, running=still, now=now)

        if decision.stood_down:
            log(log_path, f"{prefix}[stand-down] {decision.stood_down}")
        if decision.storm:
            log(log_path,
                f"[error] {decision.storm} has been closed {STORM_KILLS}+ times in "
                f"{STORM_WINDOW_SEC // 60} minutes - Windows is restarting it, not the "
                f"child. Run `python -m safehouse_setup browser` to turn off Edge Startup "
                f"Boost and background mode, then re-run this task.")
        if decision.kill:
            log(log_path, f"{prefix}[kill] ending {', '.join(decision.kill)}")
            if not args.dry_run:
                for failure in kill_processes(decision.kill):
                    log(log_path, f"[error] taskkill failed: {failure}")
                if decision.notify and cfg.notify:
                    notify(cfg.message)

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
