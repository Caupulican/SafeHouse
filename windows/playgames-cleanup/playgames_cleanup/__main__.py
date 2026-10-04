"""Entry point: `python -m playgames_cleanup [--dry-run] [--list-only] [--yes-per-step]`.

Order of business, and why it is this order:

1. **Guard check (read-only).** Play Games is banned here; the guard closes it within a
   minute. If it is enforcing, print the parent's elevated unlock command and exit 2 -
   before anything is launched.
2. **Measure** the Android VM disk, so every later number has a baseline.
3. **Launch / attach** and read the library.
4. **The loop** (session.run): one game at a time, a y/s/q for each.
5. **Report**, then - always, however we exit - the `--lock` reminder.

Exit codes: 0 done, 1 error (Play Games would not start, not Windows), 2 guard locked,
130 interrupted.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from . import disk, guard, session

log = logging.getLogger("playgames_cleanup")


def log_dir() -> Path:
    """%LOCALAPPDATA%\\SafeHouse\\playgames-cleanup\\logs, else ./logs next to the tool.
    Neither is in git: the first is outside the repo, the second is `*.log`-ignored."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "SafeHouse" / "playgames-cleanup" / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def setup_logging() -> Path:
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"cleanup-{datetime.now():%Y%m%d-%H%M%S}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path


def say(text: str) -> None:
    print(text, flush=True)
    for line in text.splitlines() or [""]:
        if line.strip():
            log.info("> %s", line)


def ask(prompt: str) -> str:
    answer = input(prompt)
    log.info("? %s %r", prompt.strip(), answer)
    return answer


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="playgames_cleanup",
        description="Uninstall Google Play Games (PC) Android games one at a time, "
                    "with a yes/skip/quit prompt for each.")
    p.add_argument("--dry-run", action="store_true",
                   help="launch, list and prompt, but click nothing destructive")
    p.add_argument("--list-only", action="store_true",
                   help="print the installed games and the avd size, then exit")
    p.add_argument("--yes-per-step", action="store_true",
                   help="also ask before each automated UI step (off by default)")
    p.add_argument("--guard-root", type=Path, default=guard.DEFAULT_ROOT,
                   help="where the guard keeps playgames-guard*.json (read-only)")
    p.add_argument("--unlock-minutes", type=int, default=guard.DEFAULT_UNLOCK_MINUTES,
                   help="minutes to put in the printed unlock command")
    p.add_argument("--settle-timeout", type=float, default=180.0,
                   help="seconds to wait for each game to leave the library")
    return p


def guard_now(root: Path) -> guard.GuardVerdict:
    return guard.check(root, int(datetime.now().timestamp()))


def lock_reminder(start: guard.GuardVerdict | None) -> None:
    say("")
    say("-" * 72)
    if start is not None and start.disabled:
        say("REMINDER: the Play Games guard is DISABLED in playgames-guard.json. "
            "Set \"enabled\": true there to ban Play Games again.")
    else:
        say("REMINDER: re-lock the Play Games guard now, from an ELEVATED PowerShell:")
        say(f"  {guard.lock_command()}")
        say("(It also re-locks by itself when the unlock runs out.)")
    say("-" * 72)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logfile = setup_logging()
    say(f"Play Games cleanup - log: {logfile}")
    if args.dry_run:
        say("DRY RUN: nothing will be uninstalled.")

    start_guard: guard.GuardVerdict | None = None
    results: list[session.Result] = []
    start = end = None
    dirs: list[Path] = []
    try:
        if sys.platform != "win32":
            say("This tool drives the Windows Play Games app; run it on Windows.")
            return 1

        # 1. guard
        start_guard = guard_now(args.guard_root)
        say(f"Guard: {start_guard.reason}")
        if not start_guard.ok:
            say("")
            say("Play Games cannot be opened while the guard enforces. A parent unlocks it")
            say("from an ELEVATED PowerShell (right-click Start -> Terminal (Admin)):")
            say(f"  {guard.unlock_command(args.unlock_minutes)}")
            say("then runs this tool again. Nothing was launched.")
            return 2

        # 2. measure
        dirs = disk.avd_dirs()
        if not dirs:
            say("No %LOCALAPPDATA%\\Google\\Play Games\\userdata_*\\avd folder found - "
                "nothing to measure (continuing anyway).")
        start = disk.snapshot(dirs)
        say(f"Android VM disk (avd): {disk.gb(start.avd_bytes)}   "
            f"C: free: {disk.gb(start.free_bytes)}")

        # 3. launch / attach
        from .app import PlayGamesApp, registry_games  # pywinauto loads here, not before
        app = PlayGamesApp()
        say("Starting / attaching to Google Play Games (this can take a minute)...")
        if not app.connect_or_launch():
            say("Could not find the Play Games window. If it did not open at all, the "
                "GooglePlayGamesServices service may be disabled - see the README, "
                "'If Play Games will not start'.")
            return 1
        ui_games = app.list_games()
        reg_games = registry_games()

        if args.list_only:
            say("\nInstalled games seen in the library (UIA):")
            for g in ui_games or []:
                say(f"  - {g}")
            if not ui_games:
                say("  (could not read the library view)")
            say("Games Windows lists as installed by Play Games (registry):")
            for g in reg_games:
                say(f"  - {g}")
            if not reg_games:
                say("  (none found)")
            return 0

        # 4. the loop
        games = session.choose_games(ui_games, reg_games, ask=ask, say=say)

        def guard_ok() -> tuple[bool, str]:
            v = guard_now(args.guard_root)
            return v.ok, v.reason

        session.run(games, app, ask=ask, say=say,
                    measure=lambda: disk.snapshot(dirs), guard_ok=guard_ok,
                    reg_names=registry_games,
                    opts=session.Options(dry_run=args.dry_run,
                                         yes_per_step=args.yes_per_step,
                                         settle_timeout=args.settle_timeout),
                    results=results)
        return 0
    except KeyboardInterrupt:
        say("\nInterrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001 - shown, logged, and the reminder still prints
        log.exception("run failed")
        say(f"\nERROR: {type(exc).__name__}: {exc} (details in {logfile})")
        return 1
    finally:
        if start is not None and (results or not args.list_only):
            end = disk.snapshot(dirs)
            say("")
            for line in session.report(results, start, end):
                say(line)
        lock_reminder(start_guard)


if __name__ == "__main__":
    sys.exit(main())
