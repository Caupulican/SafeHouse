"""The watcher loop. Entry point for the scheduled task.

    python -m safehouse_ytbudget.watcher            # enforce
    python -m safehouse_ytbudget.watcher --shadow   # decide and log, change nothing

Shadow mode is how this gets validated: it runs beside the armed PowerShell watcher on
the live machine, writing to its own state and log files, and the two are diffed before
cutover. It is the only way to test the port without experimenting on a child's access.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .config import Config, load as load_config
from .platform.adguard import load_client as load_adguard
from .platform.base import ReadOnlyPlatform
from .platform.windows import WindowsPlatform
from .tick import Paths, Ticker

DEFAULT_ROOT = Path(os.environ.get("YB_ROOT", r"C:\ProgramData\SafeHouse"))
DEFAULT_HOSTS = Path(
    os.environ.get("YB_HOSTS", str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                                  / "System32" / "drivers" / "etc" / "hosts"))
)


def find_block_list(script_dir: Path) -> Path:
    """The host list is shared with the PowerShell toggle, and the repo layout and the
    deployed C:\\SafeHouse layout put it in different places."""
    for candidate in (
        script_dir / "parental-blocks" / "youtube.txt",
        script_dir.parent / "parental-blocks" / "youtube.txt",
        script_dir.parent.parent / "windows" / "parental-blocks" / "youtube.txt",
        Path(r"C:\SafeHouse\windows\parental-blocks\youtube.txt"),
    ):
        if candidate.exists():
            return candidate
    return script_dir / "parental-blocks" / "youtube.txt"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safehouse-ytbudget", description=__doc__)
    p.add_argument("--shadow", action="store_true",
                   help="decide and log but never enforce; for running beside the "
                        "PowerShell watcher during the port")
    p.add_argument("--once", action="store_true", help="run a single tick and exit")
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--hosts", type=Path, default=DEFAULT_HOSTS)
    p.add_argument("--block-list", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    suffix = "-py" if args.shadow else ""
    log_path = args.root / f"youtube-budget-python{'-shadow' if args.shadow else ''}.log"
    block_list = args.block_list or find_block_list(Path(__file__).resolve().parent.parent)

    # Arm the resolver lane before the platform is built. Without it the hosts file is
    # the only enforcement, and the hosts file is invisible to anything that does not use
    # the Windows resolver -- which is exactly the Google Play Games VM.
    adguard, adguard_status = load_adguard(args.root)

    real = WindowsPlatform(
        root=args.root,
        hosts_path=args.hosts,
        block_list=block_list,
        log_path=log_path,
        adguard=adguard,
    )
    real.enable_dns_channel()
    if not args.shadow:
        # Clear any session left behind by a previous run before taking the meter over.
        # NEVER in shadow mode: pktmon is one global session for the whole machine, so
        # this would tear down the filters the PowerShell watcher is using -- which it
        # did, once, wiping all 32 of them until the task was restarted.
        real.stop_metering()

    platform = ReadOnlyPlatform(real) if args.shadow else real

    cfg, warnings = load_config(args.root / "config.json")
    for w in warnings:
        platform.log(f"[warn] {w}")

    ticker = Ticker(platform=platform, cfg=cfg, paths=Paths.under(args.root, suffix=suffix))

    mode = "SHADOW (observes only)" if args.shadow else "ENFORCING"
    platform.log(
        f"[start] python watcher {mode} sample={cfg.sample_sec}s window={cfg.window_sec}s "
        f"limit={cfg.limit_min}m min_kbps={cfg.min_throughput_kbps} hosts={args.hosts}"
    )
    # Loud on every start, at warn level when the lane is down. A disarmed resolver is a
    # hole in the coverage, not a configuration nicety, and must never be inferable only
    # from the absence of a line.
    platform.log(("[start] " if adguard is not None else "[warn] ") + adguard_status)

    try:
        while True:
            started = time.monotonic()
            try:
                # Re-read config every tick so the limit is live. Storing it once was the
                # bug that made editing .env silently do nothing in the PowerShell version.
                fresh, cfg_warnings = load_config(args.root / "config.json")
                if fresh != ticker.cfg:
                    for w in cfg_warnings:
                        platform.log(f"[warn] {w}")
                    if fresh.limit_min != ticker.cfg.limit_min:
                        platform.log(
                            f"[config] daily limit {ticker.cfg.limit_min}m -> {fresh.limit_min}m"
                        )
                    ticker.cfg = fresh

                ticker.run()
            except Exception as exc:  # noqa: BLE001 - a tick must never kill the loop
                platform.log(f"[error] tick failed: {type(exc).__name__}: {exc}")

            if args.once:
                return 0

            # Sleep the remainder of the interval, so a slow tick does not compound.
            time.sleep(max(1.0, ticker.cfg.sample_sec - (time.monotonic() - started)))
    except KeyboardInterrupt:
        platform.log("[stop] interrupted")
        return 0
    finally:
        if not args.shadow:
            real.stop_metering()


if __name__ == "__main__":
    sys.exit(main())
