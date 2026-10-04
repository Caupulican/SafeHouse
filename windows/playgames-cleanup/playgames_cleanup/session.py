"""The per-game loop: ask, drive (or guide), verify, record.

Everything here is decision logic with the outside world passed in - the app, the
console (`ask`/`say`), the disk measurement, the guard check, the registry lookup and
the clock - so the whole loop runs under pytest on Linux against a fake app.

Three promises the loop keeps:

1. **Nothing destructive without a "y" for that game.** The prompt is per game, every
   game, and anything but y/s/q is asked again.
2. **A miss becomes an instruction, never a guess.** A step whose control is not found
   prints what to click and waits for Enter.
3. **PASS needs evidence.** The game left the library (or the uninstall registry), or the
   avd shrank. Otherwise the verdict is NOT VERIFIED, with the reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from . import disk
from .steps import STEPS, App

PASS = "PASS"
NOT_VERIFIED = "NOT VERIFIED"
NOT_CHECKED = "NOT CHECKED"


@dataclass(frozen=True)
class Options:
    dry_run: bool = False
    yes_per_step: bool = False
    settle_timeout: float = 180.0   # how long to wait for a game to leave the library
    poll_interval: float = 5.0


@dataclass(frozen=True)
class Result:
    game: str
    action: str                 # uninstalled / assisted / skipped / aborted / dry-run / ...
    verdict: str                # PASS / NOT VERIFIED / NOT CHECKED / "-"
    freed_bytes: int | None     # avd change for this game, positive = smaller
    note: str = ""


Ask = Callable[[str], str]
Say = Callable[[str], None]


def parse_choice(text: str) -> str | None:
    """'y'/'yes'/'s'/'skip'/'q'/'quit' (any case) -> 'y'/'s'/'q'. Anything else: None.

    Deliberately NO default: an empty Enter is not a yes.
    """
    t = text.strip().casefold()
    for full in ("yes", "skip", "quit"):
        if t and full.startswith(t):
            return full[0]
    return None


def still_present(ui: bool | None, reg: bool | None) -> bool | None:
    """Combine the two presence signals. Any source still listing it wins; one source
    saying gone (and none saying present) is gone; no signal at all is unknown."""
    if ui is True or reg is True:
        return True
    if ui is False or reg is False:
        return False
    return None


def judge(gone: bool | None, freed: int | None,
          threshold: int = disk.SHRINK_THRESHOLD) -> tuple[str, str]:
    """(verdict, note) for one attempted uninstall. Honest about the image file."""
    shrank = freed is not None and freed >= threshold
    if gone is True:
        if shrank:
            return PASS, "left the library; avd shrank"
        return PASS, "left the library; freed inside the VM, avd file size unchanged"
    if gone is None:
        if shrank:
            return PASS, "avd shrank (library could not be read)"
        # Includes the LAST game: an empty library reads the same as an unreadable one
        # (a Chromium tree that exposes no tiles), so emptiness is not taken as proof.
        return NOT_VERIFIED, "library empty or unreadable, and avd did not shrink"
    if shrank:
        return NOT_VERIFIED, "avd shrank but the game is still listed"
    return NOT_VERIFIED, "still listed"


def wait_until_gone(app: App, game: str, reg_present: Callable[[], bool | None], opts:
                    Options, *, sleep=time.sleep, clock=time.monotonic) -> bool | None:
    """Poll until the game is gone (True), the timeout passes while it is still listed
    (False), or nothing could be read at all (None)."""
    deadline = clock() + opts.settle_timeout
    last: bool | None = None
    while True:
        try:
            ui = app.is_present(game)
        except Exception:  # noqa: BLE001 - a UI read failing is "unknown", not fatal
            ui = None
        present = still_present(ui, reg_present())
        if present is False:
            return True
        if present is True:
            last = True
        if clock() >= deadline:
            return False if last else None
        sleep(opts.poll_interval)


def drive(app: App, game: str, *, ask: Ask, say: Say, opts: Options) -> str:
    """Run the steps for one game. Returns 'automated', 'assisted' or 'aborted'."""
    assisted = False
    for step in STEPS:
        if opts.yes_per_step:
            ans = ask(f"  Next: {step.describe}. [Enter] go / [m] I'll do it / "
                      f"[a] abort this game: ").strip().casefold()
            if ans.startswith("a"):
                return "aborted"
            if ans.startswith("m"):
                say(f"  {step.manual(game)}")
                if ask("  Press Enter when done (a = abort this game): "
                       ).strip().casefold().startswith("a"):
                    return "aborted"
                assisted = True
                continue
        try:
            ok = step.run(app, game)
        except Exception as exc:  # noqa: BLE001 - any UI failure falls back to the parent
            say(f"  ({step.key}: {type(exc).__name__}: {exc})")
            ok = False
        if ok:
            say(f"  done: {step.describe}")
            continue
        say(f"  Could not {step.describe} automatically. Please do it yourself:")
        say(f"    {step.manual(game)}")
        if ask("  Press Enter when done (a = abort this game): "
               ).strip().casefold().startswith("a"):
            return "aborted"
        assisted = True
    return "assisted" if assisted else "automated"


def run(games: Iterable[str], app: App, *, ask: Ask, say: Say,
        measure: Callable[[], disk.Snapshot],
        guard_ok: Callable[[], tuple[bool, str]],
        reg_names: Callable[[], list[str]] = lambda: [],
        opts: Options = Options(),
        results: list[Result] | None = None,
        sleep=time.sleep, clock=time.monotonic) -> list[Result]:
    """The loop. `results` may be passed in so a Ctrl+C still leaves the caller with
    everything recorded up to that point."""
    results = [] if results is None else results
    for game in games:
        ok, why = guard_ok()
        if not ok:
            say(f"\nStopping before {game!r}: {why}")
            results.append(Result(game, "not attempted", NOT_CHECKED, None, why))
            break

        say(f"\n=== {game} ===")
        choice = None
        while choice is None:
            choice = parse_choice(ask(f"Uninstall {game}? [y]es/[s]kip/[q]uit: "))
        if choice == "q":
            break
        if choice == "s":
            results.append(Result(game, "skipped", "-", None))
            continue
        if opts.dry_run:
            for step in STEPS:
                say(f"  [dry-run] would {step.describe}")
            results.append(Result(game, "dry-run", NOT_CHECKED, None, "nothing clicked"))
            continue

        # The registry only counts as a signal for a game it listed BEFORE the attempt:
        # a name the UI shows differently would otherwise read as "gone" from the start.
        was_registered = game.casefold() in {n.casefold() for n in reg_names()}

        def reg_present(g=game, listed=was_registered) -> bool | None:
            return (g.casefold() in {n.casefold() for n in reg_names()}) if listed else None

        before = measure()
        app.focus()
        outcome = drive(app, game, ask=ask, say=say, opts=opts)
        if outcome == "aborted":
            app.dismiss()
            results.append(Result(game, "aborted", NOT_CHECKED, None,
                                  "stopped by you; check the library"))
            continue

        say(f"  waiting up to {int(opts.settle_timeout)}s for it to leave the library...")
        gone = wait_until_gone(app, game, reg_present, opts, sleep=sleep, clock=clock)
        after = measure()
        freed = disk.freed(before.avd_bytes, after.avd_bytes)
        verdict, note = judge(gone, freed)
        action = "uninstalled" if outcome == "automated" else "uninstalled (assisted)"
        say(f"  {verdict}: {note} (avd {disk.gb(freed, signed=True)})")
        results.append(Result(game, action, verdict, freed, note))
    return results


def report(results: list[Result], start: disk.Snapshot, end: disk.Snapshot) -> list[str]:
    """The final table, as lines. Totals are the honest kind: per-game avd deltas, the
    whole-session avd delta, and the whole-session change in C: free space."""
    rows = [("Game", "Action", "Verdict", "avd freed", "Note")]
    for r in results:
        rows.append((r.game, r.action, r.verdict,
                     disk.gb(r.freed_bytes, signed=True) if r.freed_bytes is not None
                     else "-", r.note))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = []
    for i, row in enumerate(rows):
        lines.append("  ".join(row[c].ljust(widths[c]) for c in range(4)) + "  " + row[4])
        if i == 0:
            lines.append("  ".join("-" * w for w in widths) + "  ----")
    passed = sum(1 for r in results if r.verdict == PASS)
    attempted = sum(1 for r in results if r.verdict in (PASS, NOT_VERIFIED))
    lines.append("")
    lines.append(f"Uninstalls verified : {passed} of {attempted} attempted")
    lines.append(f"avd size            : {disk.gb(start.avd_bytes)} -> "
                 f"{disk.gb(end.avd_bytes)} "
                 f"({disk.gb(disk.freed(start.avd_bytes, end.avd_bytes), signed=True)})")
    lines.append(f"C: free space       : {disk.gb(start.free_bytes)} -> "
                 f"{disk.gb(end.free_bytes)} "
                 f"({disk.gb(disk.freed(end.free_bytes, start.free_bytes), signed=True)})")
    if passed and (disk.freed(start.avd_bytes, end.avd_bytes) or 0) < disk.SHRINK_THRESHOLD:
        lines.append("Note: games were removed inside the Android VM, but its disk image "
                     "did not shrink. That space is free for Play Games to reuse, not "
                     "returned to Windows. See README: 'Getting the space back'.")
    return lines


def choose_games(ui: list[str] | None, reg: list[str], *, ask: Ask,
                 say: Say) -> Iterable[str]:
    """Where the game list comes from, best source first.

    1. UIA read the library: use it. Registry-only names are shown, not merged - the
       same game may carry a different name there, and a duplicate would be a second
       prompt for something already gone.
    2. UIA could not, the uninstall registry could: offer that list.
    3. Neither: the parent types names, either all up front or one at a time.
    """
    if ui:
        extra = [n for n in reg if n.casefold() not in {g.casefold() for g in ui}]
        if extra:
            say("Also registered with Windows but not seen in the library view: "
                + ", ".join(extra))
        return ui
    say("Could not read the game list from the Play Games window.")
    if reg and parse_choice(ask(f"Use the {len(reg)} game(s) Windows lists as installed "
                                f"by Play Games ({', '.join(reg)})? [y]es/[s]kip: ")
                            ) == "y":
        return reg
    say("Type the games yourself. Enter names one per line, blank line to finish;")
    say("or press Enter right away to go one game at a time.")
    typed: list[str] = []
    while True:
        name = ask("  game name: ").strip()
        if not name:
            break
        typed.append(name)
    return typed or ask_names(ask)


def ask_names(ask: Ask) -> Iterator[str]:
    """Manual "next visible game" mode: the parent types each name as they go."""
    while True:
        name = ask("Type the next game's name as shown in the Library (Enter = done): "
                   ).strip()
        if not name:
            return
        yield name
