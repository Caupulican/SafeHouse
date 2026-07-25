"""Lift the AdGuard Home YouTube block at the start of a new day.

Needed only during the transition. The PowerShell watcher is still the enforcing one and
knows nothing about AdGuard Home, so a block applied at the resolver stays applied
forever unless something clears it -- which would silently turn "no more YouTube today"
into "no more YouTube, ever".

Once the Python watcher takes over enforcement this becomes redundant: it drives both
layers from the same decision and its own day reset lifts them together. Delete the task
at cutover.

    C:\\SafeHouse\\python\\python.exe adguard_daily_reset.py [--dry-run]

Deliberately does NOT touch the hosts file: the PowerShell watcher owns that and its own
date-based reset already handles it. Two things resetting one file is how they end up
fighting.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from safehouse_ytbudget.platform.adguard import AdGuardClient, AdGuardConfig  # noqa: E402

DEFAULT_CREDS = Path(r"C:\ProgramData\SafeHouse\adguard.json")
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
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

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
        return 0

    if args.dry_run:
        log(args.log, "[dry-run] would lift the AdGuard YouTube block")
        return 0

    result = client.set_blocked(False)
    log(args.log, f"[reset] new day - AdGuard YouTube block lifted ({result})")
    return 0 if result in ("UNBLOCKED", "NONE") else 1


if __name__ == "__main__":
    sys.exit(main())
