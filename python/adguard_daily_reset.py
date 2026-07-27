"""Lift the AdGuard Home YouTube block once the day it belongs to has passed.

A **failsafe, independent of the watcher**. The watcher lifts both lanes itself on the
first tick of a new day, so in normal operation this finds nothing to do. It exists for
the case where the watcher is not running at all -- quarantined by antivirus, crash-looping,
task unregistered -- because the failure mode it prevents is not "the budget is wrong",
it is a resolver block applied last night that nothing ever clears, silently turning
"no more YouTube today" into **"no more YouTube, ever"**.

That asymmetry is the whole argument for keeping it: a watcher that dies while a block is
applied fails *closed*, permanently, and a child has no way to report it that sounds
different from ordinary enforcement.

Runs from TWO triggers, and is safe on both because it is date-aware:

  * daily at 00:05 -- the ordinary case;
  * at every boot -- because the 00:05 run never happens if the machine is off, and
    `StartWhenAvailable` only covers a *missed* scheduled run, with an arbitrary delay.

Date-awareness is what makes the boot trigger safe. A naive lift-on-startup would hand
YouTube back on the first reboot after the budget ran out: boot at 20:00 and the block
would vanish four hours early. So the block records the local date it was applied, and
this only lifts it once that date is in the past.

    C:\\SafeHouse\\python\\python.exe adguard_daily_reset.py [--dry-run] [--force]

Deliberately does NOT touch the hosts file. The watcher owns that, and a hosts block left
behind is self-correcting anyway: the watcher's own date reset lifts it, and unlike the
resolver lane a stale hosts block is visible in a file a parent can read and edit. Two
things resetting one file is how they end up fighting.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from safehouse_ytbudget.platform.adguard import (  # noqa: E402
    AdGuardClient,
    AdGuardConfig,
    read_block_date,
    write_block_date,
)

DEFAULT_CREDS = Path(r"C:\ProgramData\SafeHouse\adguard.json")
DEFAULT_MARKER = Path(r"C:\ProgramData\SafeHouse\adguard-block-state.json")
DEFAULT_LOG = Path(r"C:\ProgramData\SafeHouse\adguard-daily-reset.log")


def log(path: Path, message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\r\n"
    print(message)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--creds", type=Path, default=DEFAULT_CREDS)
    p.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="lift regardless of which day the block belongs to")
    p.add_argument("--today", default=None, help="override today's date (testing)")
    args = p.parse_args(argv)

    today = args.today or datetime.now().strftime("%Y-%m-%d")

    cfg = AdGuardConfig.from_file(args.creds)
    if cfg is None:
        log(args.log, f"[error] no usable AdGuard credentials at {args.creds}")
        return 1

    client = AdGuardClient(cfg)
    state = client.is_blocked()
    if state is None:
        # Failing loudly matters more than failing safe here: leaving the block on is
        # the *restrictive* direction, so nobody gets unsupervised access, but the child
        # gets no YouTube at all until someone notices.
        log(args.log, "[error] AdGuard Home unreachable - YouTube stays blocked")
        return 1

    if not state:
        log(args.log, "[ok] AdGuard YouTube block already lifted; nothing to do")
        write_block_date(args.marker, None)
        return 0

    blocked_on = read_block_date(args.marker)
    if not args.force:
        if blocked_on == today:
            log(args.log,
                f"[hold] block belongs to today ({today}) - leaving it in place")
            return 0
        if blocked_on is None:
            # Unknown provenance. Lifting is the right call: the alternative is a block
            # that no longer has anything to clear it, i.e. YouTube gone permanently.
            log(args.log, "[warn] no block date recorded - treating as stale and lifting")

    if args.dry_run:
        log(args.log, f"[dry-run] would lift the block (applied {blocked_on}, today {today})")
        return 0

    result = client.set_blocked(False)
    log(args.log,
        f"[reset] block from {blocked_on} lifted on {today} ({result})")
    if result in ("UNBLOCKED", "NONE"):
        write_block_date(args.marker, None)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
