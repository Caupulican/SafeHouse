"""Parent-facing control CLI.

    python -m safehouse_ytbudget.cli status
    python -m safehouse_ytbudget.cli set-limit 90
    python -m safehouse_ytbudget.cli grant 30
    python -m safehouse_ytbudget.cli block
    python -m safehouse_ytbudget.cli allow
    python -m safehouse_ytbudget.cli reset

Mirrors `youtube-budget-ctl.ps1`. `set-limit` writes config.json, which is the single
source of truth the watcher re-reads each tick; everything else is today-only state.

The command bodies take a platform and paths rather than reaching for Windows directly,
so the whole surface is driven by the fake in tests.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as configmod
from . import pins as pinmod
from . import state as statemod
from .platform.base import Platform
from .tick import Paths


def format_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _free_budget_if_possible(st, cfg, platform: Platform) -> bool:
    """Lift an automatic block once the budget allows it again.

    Without this the parent would have to wait a full sample interval after raising the
    limit before the child could watch, which reads as the tool being broken.
    """
    if not st.blocked_by_budget:
        return False
    if st.seconds_used >= st.effective_limit_sec(cfg.limit_sec):
        return False
    platform.unblock_hosts("auto")
    platform.clear_firewall()
    platform.flush_dns()
    st.blocked_by_budget = False
    return True


def run(command: str, value: int | None, platform: Platform, *, root: Path,
        paths: Paths | None = None) -> tuple[int, list[str]]:
    """Execute one verb. Returns (exit code, lines to print)."""
    paths = paths or Paths.under(root)
    cfg_path = root / "config.json"
    cfg, _ = configmod.load(cfg_path)
    st, warnings = statemod.load(paths.state, today=platform.today())
    out: list[str] = [f"[warn] {w}" for w in warnings]

    # A stale state file from a previous day would otherwise report yesterday's usage.
    if st.date != platform.today():
        st = statemod.State(date=platform.today())

    if command == "status":
        limit = st.effective_limit_sec(cfg.limit_sec)
        remaining = max(0, limit - st.seconds_used)
        out += [
            f"YouTube watch-time budget - {st.date}",
            f"  Used today : {format_hms(st.seconds_used)}",
            f"  Limit      : {format_hms(limit)}  ({cfg.limit_min}m daily"
            f" + {format_hms(st.bonus_sec)} bonus)",
            f"  Remaining  : {format_hms(remaining)}",
            f"  Budget     : {'BLOCKED (budget)' if st.blocked_by_budget else 'ALLOWED'}",
            f"  Manual hold: {'yes' if st.manual_block else 'no'}",
            f"  Hosts file : YouTube section "
            f"{'blocked' if platform.hosts_blocked() else 'allowed'}",
            f"  Firewall   : {len(platform.firewall_ips())} pinned endpoint(s) blocked",
        ]
        return 0, out

    if command == "set-limit":
        if value is None:
            return 2, out + ["set-limit needs a whole number of minutes"]
        if value < 0:
            return 2, out + ["the limit cannot be negative"]
        # config.json is authoritative and is what the watcher re-reads each tick.
        configmod.write(cfg_path, configmod.merge(cfg, {"limit_min": value}))
        cfg = configmod.merge(cfg, {"limit_min": value})
        freed = _free_budget_if_possible(st, cfg, platform)
        statemod.save(paths.state, st)
        out.append(f"Daily limit set to {value}m (config.json).")
        if freed:
            out.append("Budget freed - YouTube unblocked.")
        return 0, out

    if command == "grant":
        if value is None:
            return 2, out + ["grant needs a whole number of minutes"]
        st.bonus_sec = max(0, st.bonus_sec + value * 60)
        freed = _free_budget_if_possible(st, cfg, platform)
        statemod.save(paths.state, st)
        out.append(f"Granted {value}m of bonus for today (total bonus"
                   f" {format_hms(st.bonus_sec)}).")
        if freed:
            out.append("Budget freed - YouTube unblocked.")
        return 0, out

    if command == "block":
        res = platform.block_hosts()
        if res == "NOLIST":
            return 1, out + ["Cannot block - host list missing (parental-blocks/youtube.txt)."]
        fw = platform.set_firewall(pinmod.ips(pinmod.load(paths.pins)))
        platform.flush_dns()
        # manual_block marks this as deliberate so the watcher's reconcile will not lift
        # it just because today's usage happens to sit under the limit.
        st.manual_block = True
        statemod.save(paths.state, st)
        out.append(f"YouTube BLOCKED now (hosts={res}; endpoint firewall={fw}).")
        return 0, out

    if command == "allow":
        res = platform.unblock_hosts("auto")
        platform.clear_firewall()
        platform.flush_dns()
        st.manual_block = False
        st.blocked_by_budget = False
        statemod.save(paths.state, st)
        out.append(f"YouTube allowed (hosts={res}).")
        # Be honest rather than letting the parent think this stuck: enforcement
        # reconciles every tick, so over budget it will re-block within one sample.
        if st.seconds_used >= st.effective_limit_sec(cfg.limit_sec):
            out.append(
                f"NOTE: today's usage ({format_hms(st.seconds_used)}) is still at or over"
                f" the limit ({format_hms(st.effective_limit_sec(cfg.limit_sec))}),"
                f" so the watcher will re-block within {cfg.sample_sec}s."
            )
            out.append("      Use `grant <minutes>` to actually give more time.")
        return 0, out

    if command == "reset":
        st.seconds_used = 0
        st.bonus_sec = 0
        st.blocked_by_budget = False
        st.manual_block = False
        res = platform.unblock_hosts("auto")
        platform.clear_firewall()
        platform.flush_dns()
        statemod.save(paths.state, st)
        out.append(f"Today's usage and bonus zeroed; block lifted (hosts={res}).")
        return 0, out

    return 2, out + [f"unknown command {command!r}"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safehouse-ytbudget-ctl", description=__doc__)
    p.add_argument(
        "command",
        choices=["status", "set-limit", "grant", "block", "allow", "reset"],
        nargs="?",
        default="status",
    )
    p.add_argument("value", type=int, nargs="?", default=None)
    p.add_argument("--root", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    from .watcher import DEFAULT_HOSTS, DEFAULT_ROOT, find_block_list
    from .platform.windows import WindowsPlatform

    args = build_parser().parse_args(argv)
    root = args.root or DEFAULT_ROOT
    platform = WindowsPlatform(
        root=root,
        hosts_path=DEFAULT_HOSTS,
        block_list=find_block_list(Path(__file__).resolve().parent.parent),
        log_path=root / "youtube-budget-python.log",
    )
    code, lines = run(args.command, args.value, platform, root=root)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
