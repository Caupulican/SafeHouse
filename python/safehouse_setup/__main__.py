"""SafeHouse host configuration — one CLI for the Windows side.

    python -m safehouse_setup dns                    # point Windows at the resolver
    python -m safehouse_setup dns --status
    python -m safehouse_setup dns --clear            # remove the NRPT rule
    python -m safehouse_setup persist                # run the silent relay at every logon
    python -m safehouse_setup browser                # close Opera's VPN + DoH
    python -m safehouse_setup browser --status
    python -m safehouse_setup toggle youtube --block
    python -m safehouse_setup toggle youtube --allow
    python -m safehouse_setup toggle youtube         # status

One entry point owns host configuration, so there is one thing to learn and one place
for error handling.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import browser, dns, dns_proxy, toggle
from .migration import retire_legacy_runtime
from .winutil import is_admin, powershell, wsl_ip

LOGON_TASK = "SafeHouse-DNS"


def windowless_python(executable: Path | None = None) -> Path:
    """Use pythonw for interactive logon tasks so no closable console is created."""
    python = (executable or Path(sys.executable)).resolve()
    candidate = python.with_name("pythonw.exe")
    if not candidate.exists():
        raise FileNotFoundError(f"windowless Python runtime is missing: {candidate}")
    return candidate


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def logon_task_script(python: Path) -> str:
    """Build the sole, silent logon registration for dynamic WSL DNS routing."""
    runtime_dir = Path(__file__).resolve().parent.parent
    return (
        f"$action=New-ScheduledTaskAction -Execute {_ps_literal(python)} "
        f"-Argument '-m safehouse_setup serve' -WorkingDirectory {_ps_literal(runtime_dir)}; "
        "$account=\"$env:USERDOMAIN\\$env:USERNAME\"; "
        "$trigger=New-ScheduledTaskTrigger -AtLogOn -User $account; "
        "$principal=New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive "
        "-RunLevel Highest; "
        "$settings=New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable "
        "-MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName '{LOGON_TASK}' -Action $action "
        "-Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName '{LOGON_TASK}'"
    )


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


def cmd_serve(_args) -> int:
    """Run the silent relay without entering the Windows DNS configuration lock."""
    router = dns_proxy.UpstreamRouter(wsl_ip, lambda: dns.default_route()[1])
    proxy = dns_proxy.DnsProxy(router)
    api_relay = dns_proxy.TcpRelay(router)
    proxy.start()
    api_relay.start()
    proxy.wait()
    return 0


def cmd_persist(args) -> int:
    """Run the stable loopback relay at logon.

    The Windows-native loopback relay tracks WSL's changing NAT address, and the
    containers are reconciled before Windows starts trusting that endpoint.
    Runs as the logged-on user at highest privileges, which is enough for the DNS
    cmdlets and avoids a SYSTEM task that cannot see the user's WSL distro.
    """
    if not is_admin():
        print("Registering a scheduled task needs Administrator rights.", file=sys.stderr)
        return 1
    retire_legacy_runtime()
    if args.remove:
        powershell(
            f"Unregister-ScheduledTask -TaskName '{LOGON_TASK}' -Confirm:$false "
            "-ErrorAction SilentlyContinue"
        )
        print(f"removed scheduled task {LOGON_TASK}")
        return 0

    try:
        python = windowless_python()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    command = f'"{python}" -m safehouse_setup serve'
    rc, out = powershell(logon_task_script(python), timeout=30)
    if rc != 0:
        print(f"could not register the task: {out.strip()[:300]}", file=sys.stderr)
        return 1
    print(f"registered {LOGON_TASK} (silent DNS relay at every logon)")
    print(f"  runs: {command}")
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

    p_serve = sub.add_parser("serve", help=argparse.SUPPRESS)
    p_serve.set_defaults(func=cmd_serve)

    p_persist = sub.add_parser("persist", help="run the silent DNS relay at every logon")
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
