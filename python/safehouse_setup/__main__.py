"""SafeHouse host configuration — one CLI for the Windows side.

    python -m safehouse_setup dns                    # point Windows at the resolver
    python -m safehouse_setup dns --status
    python -m safehouse_setup dns --clear            # remove the NRPT rule
    python -m safehouse_setup persist                # re-apply DNS at every logon
    python -m safehouse_setup browser                # close Opera's VPN + DoH
    python -m safehouse_setup browser --status
    python -m safehouse_setup toggle youtube --block
    python -m safehouse_setup toggle youtube --allow
    python -m safehouse_setup toggle youtube         # status

Replaces set-dns.ps1, mktask.ps1, opera-policy.ps1 and parental-toggle.ps1 with one
entry point, so there is one thing to learn and one place for the error handling.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import browser, dns, toggle
from .winutil import SCHTASKS, is_admin, run

LOGON_TASK = "SafeHouse-DNS"


def cmd_dns(args) -> int:
    if args.status:
        print(json.dumps({
            "nrpt_servers": dns.nrpt_servers(),
            "default_route": dns.default_route(),
            "resolves": dns.resolves(),
        }, indent=2))
        return 0
    if args.clear:
        dns.clear()
        print("NRPT rule removed (adapter DNS left alone, so the machine can still resolve)")
        return 0
    result = dns.apply(prefer=args.server)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def cmd_persist(args) -> int:
    """Re-apply DNS at logon.

    The WSL address changes across reboots, so a one-time setting goes stale and the
    machine silently falls back to the router — filtering off, and nothing to say so.
    Runs as the logged-on user at highest privileges, which is enough for the DNS
    cmdlets and avoids a SYSTEM task that cannot see the user's WSL distro.
    """
    if not is_admin():
        print("Registering a scheduled task needs Administrator rights.", file=sys.stderr)
        return 1
    if args.remove:
        run([SCHTASKS, "/Delete", "/TN", LOGON_TASK, "/F"])
        print(f"removed scheduled task {LOGON_TASK}")
        return 0

    python = Path(sys.executable).resolve()
    pkg_parent = Path(__file__).resolve().parent.parent
    command = f'"{python}" -m safehouse_setup dns'
    rc, out = run([
        SCHTASKS, "/Create", "/TN", LOGON_TASK, "/TR", command,
        "/SC", "ONLOGON", "/RL", "HIGHEST", "/F",
    ])
    if rc != 0:
        print(f"could not register the task: {out.strip()[:300]}", file=sys.stderr)
        return 1
    print(f"registered {LOGON_TASK} (re-applies DNS at every logon)")
    print(f"  runs: {command}")
    print(f"  cwd must contain the package: {pkg_parent}")
    return 0


def cmd_browser(args) -> int:
    if args.status:
        print(json.dumps(browser.status(), indent=2))
        profiles = browser.is_opera_installed()
        print(f"opera profiles: {[str(p) for p in profiles]}")
        return 0
    results = browser.apply()
    touched = browser.forbid_vpn_in_profiles()
    for key, ok in results.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {key}")
    for path in touched:
        print(f"  profile VPN forbidden: {path}")
    print("\nOpera must be fully closed and reopened for policies to load.")
    print("Verify at opera://policy - they must be listed as applied.")
    return 0 if all(results.values()) else 1


def cmd_toggle(args) -> int:
    if args.block:
        result = toggle.block(args.name)
        print(f"{toggle.display_name(args.name)}: {result}")
        return 0 if result.startswith(("BLOCKED", "ALREADY")) else 1
    if args.allow:
        print(f"{toggle.display_name(args.name)}: {toggle.allow(args.name)}")
        return 0
    blocked = toggle.status(args.name)
    print(f"{toggle.display_name(args.name)}: {'BLOCKED' if blocked else 'allowed'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="safehouse_setup", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_dns = sub.add_parser("dns", help="point Windows at the filtering resolver")
    p_dns.add_argument("--status", action="store_true")
    p_dns.add_argument("--clear", action="store_true")
    p_dns.add_argument("--server", default=None, help="use this resolver instead of autodetecting")
    p_dns.set_defaults(func=cmd_dns)

    p_persist = sub.add_parser("persist", help="re-apply DNS at every logon")
    p_persist.add_argument("--remove", action="store_true")
    p_persist.set_defaults(func=cmd_persist)

    p_browser = sub.add_parser("browser", help="close Opera GX's VPN and DoH")
    p_browser.add_argument("--status", action="store_true")
    p_browser.set_defaults(func=cmd_browser)

    p_toggle = sub.add_parser("toggle", help="manual hosts block by name")
    p_toggle.add_argument("name", nargs="?", default="youtube")
    p_toggle.add_argument("--block", action="store_true")
    p_toggle.add_argument("--allow", action="store_true")
    p_toggle.set_defaults(func=cmd_toggle)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
