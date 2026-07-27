"""Install the Python watcher as a scheduled task.

Run with the vendored runtime, elevated:

    C:\\SafeHouse\\python\\python.exe C:\\SafeHouse\\windows\\youtube-budget\\install.py --shadow

`--shadow` registers a SEPARATE task that observes and logs but never enforces, so it can
run beside the armed PowerShell watcher during the port. Without it, the task enforces
and is the cutover.

Does not vendor the interpreter -- there is no Python on a stock Windows box to run this
in the first place, so fetching the embeddable distribution is the PowerShell
bootstrapper's job (see windows/youtube-budget/README.md).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from safehouse_ytbudget import config as configmod  # noqa: E402

LIVE_TASK = "SafeHouse-YouTubeBudget-Py"
SHADOW_TASK = "SafeHouse-YouTubeBudget-PyShadow"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Hidden>true</Hidden>
    <!-- It is an infinite loop, so no execution time limit, and it must come back if
         it dies. -->
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)


def lock_down(root: Path) -> None:
    """SYSTEM + Administrators full, Users read-only.

    A standard user must be able to READ the state and log -- so a curious child can see
    how much time is left -- but not edit or reset them.
    """
    root.mkdir(parents=True, exist_ok=True)
    run(["icacls", str(root), "/inheritance:r"])
    run(["icacls", str(root), "/grant:r", "*S-1-5-18:(OI)(CI)F"])
    run(["icacls", str(root), "/grant:r", "*S-1-5-32-544:(OI)(CI)F"])
    run(["icacls", str(root), "/grant:r", "*S-1-5-32-545:(OI)(CI)RX"])
    print(f"    ACL set on {root}: SYSTEM + Administrators full, Users read-only")


def add_av_exclusions(paths: list[Path]) -> None:
    """A SYSTEM-level infinite loop that rewrites the hosts file, creates firewall rules
    and drives pktmon reads as malware to heuristic engines. Avast deleted the PowerShell
    watcher outright on this machine; without an exclusion enforcement dies silently."""
    for path in paths:
        rc, out = run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Add-MpPreference -ExclusionPath '{path}'"]
        )
        print(f"    {'excluded' if rc == 0 else 'FAILED to exclude'} {path}"
              + ("" if rc == 0 else f" ({out.strip()[:120]})"))
    print("    NOTE: third-party AV needs its own exclusion, added in that product.")


def register_task(*, name: str, python: Path, arguments: str, workdir: Path,
                  description: str, start: bool) -> bool:
    xml = TASK_XML.format(
        name=name, description=description, python=python,
        arguments=arguments, workdir=workdir,
    )
    # schtasks /XML insists on a UTF-16 file with a BOM.
    tmp = workdir / f".{name}.xml"
    tmp.write_text(xml, encoding="utf-16")
    try:
        rc, out = run(["schtasks", "/Create", "/TN", name, "/XML", str(tmp), "/F"])
        if rc != 0:
            print(f"    FAILED to register {name}: {out.strip()[:300]}")
            return False
        print(f"    registered {name}")
        if start:
            rc, out = run(["schtasks", "/Run", "/TN", name])
            print(f"    {'started' if rc == 0 else 'FAILED to start'} {name}")
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


POWERSHELL_TASK = "SafeHouse-YouTubeBudget"


def provision_adguard(root: Path, *, distro: str | None = None) -> str:
    """Write `root/adguard.json` so the resolver lane is armed on a fresh machine.

    This is the difference between "YouTube is blocked" and "YouTube is blocked for
    programs that use the Windows resolver". The hosts file cannot reach the Google Play
    Games VM, which runs its own resolver inside crosvm, and it cannot wildcard
    `*.googlevideo.com` at all. AdGuard Home does both. Left unprovisioned, the watcher
    enforces a budget the child can walk around by opening the Play Games app -- which is
    exactly what happened on the first machine this shipped to, undetected for a day,
    because nothing anywhere said the lane was missing.

    The credentials are generated by `adguard/bootstrap.py` inside WSL, so read them from
    there rather than inventing a second source of truth.
    """
    from safehouse_ytbudget.platform.adguard import AdGuardClient, AdGuardConfig

    target = root / "adguard.json"
    if target.exists() and AdGuardConfig.from_file(target) is not None:
        return f"already provisioned: {target}"

    cmd = ["wsl.exe"] + (["-d", distro] if distro else []) + [
        "bash", "-lc", "cat ~/adguard/.admin-password"
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"NOT provisioned - could not reach WSL ({type(exc).__name__}: {exc})"
    password = (proc.stdout or "").strip()
    if proc.returncode != 0 or not password:
        return ("NOT provisioned - no ~/adguard/.admin-password in WSL. "
                "Run adguard/bootstrap.py there first.")

    payload = {
        "_comment": ("AdGuard Home credentials for the YouTube budget watcher. "
                     "Restricted to SYSTEM+Administrators: this dashboard can disable "
                     "all filtering."),
        "base_url": "http://127.0.0.1:3000",
        "username": "admin",
        "password": password,
        "service_id": "youtube",
    }
    root.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    cfg = AdGuardConfig.from_file(target)
    if cfg is None:
        return f"NOT provisioned - wrote {target} but it did not load back"
    # Prove the credentials work now, rather than discovering at 3h of screen time that
    # the lane cannot authenticate.
    if AdGuardClient(cfg).is_blocked() is None:
        return (f"wrote {target}, but {cfg.base_url} did not answer - "
                "verify AdGuard Home is running")
    return f"armed and verified against {cfg.base_url}"


def retire_task(name: str) -> str:
    """Stop and remove a superseded watcher task.

    Two watchers enforcing the same budget from two state files is worse than either
    alone: they disagree about how much time is left, and each undoes the other's
    reconcile. Worse, both drive pktmon, which is a single machine-wide session -- so
    whichever reconfigures last silently steals the other's byte counters.
    """
    rc, _ = run(["schtasks", "/Query", "/TN", name])
    if rc != 0:
        return "not registered"
    run(["schtasks", "/End", "/TN", name])
    rc, out = run(["schtasks", "/Delete", "/TN", name, "/F"])
    return "removed" if rc == 0 else f"could NOT remove: {out.strip()[:160]}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--shadow", action="store_true",
                   help="register the observe-only task that runs beside the PowerShell "
                        "watcher (does NOT enforce)")
    p.add_argument("--root", type=Path, default=Path(r"C:\ProgramData\SafeHouse"))
    p.add_argument("--python", type=Path, default=Path(r"C:\SafeHouse\python\python.exe"))
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--no-av-exclusions", action="store_true")
    p.add_argument("--distro", default=None,
                   help="WSL distro holding the AdGuard credentials (default: the "
                        "default distro)")
    p.add_argument("--keep-powershell-watcher", action="store_true",
                   help="do not remove the superseded PowerShell task; only useful if "
                        "you are deliberately running the old watcher")
    args = p.parse_args(argv)

    if not is_admin():
        print("Not elevated. Re-run this from an elevated prompt.")
        return 1
    if not args.python.exists():
        print(f"Vendored runtime not found at {args.python}.")
        print("Fetch the embeddable distribution first - see windows/youtube-budget/README.md")
        return 1

    pkg_dir = Path(__file__).resolve().parent

    print(f"[*] Locking down {args.root}...")
    lock_down(args.root)

    print("[*] Rendering config.json from .env...")
    cfg, warnings = configmod.render(args.root / "config.json", pkg_dir / ".env")
    for w in warnings:
        print(f"    warning: {w}")
    configmod.write(args.root / "config.json", cfg)
    print(f"    limit_min={cfg.limit_min}")

    if args.no_av_exclusions:
        print("[*] --no-av-exclusions: skipping.")
    else:
        print("[*] Adding Microsoft Defender exclusions...")
        add_av_exclusions([pkg_dir, args.root, args.python.parent])

    print("[*] Arming the resolver lane (AdGuard Home)...")
    adguard_status = provision_adguard(args.root, distro=args.distro)
    print(f"    {adguard_status}")
    lane_armed = not adguard_status.startswith("NOT provisioned")

    name = SHADOW_TASK if args.shadow else LIVE_TASK
    arguments = "-m safehouse_ytbudget.watcher" + (" --shadow" if args.shadow else "")
    description = (
        "SafeHouse YouTube budget - Python port, SHADOW MODE (observes and logs, does "
        "not enforce; runs beside the PowerShell watcher during the port)"
        if args.shadow else
        "SafeHouse YouTube budget - Python watcher (enforcing)"
    )

    print(f"[*] Registering scheduled task {name}...")
    if not register_task(name=name, python=args.python, arguments=arguments,
                         workdir=pkg_dir, description=description,
                         start=not args.no_start):
        return 1

    print()
    if args.shadow:
        print("SHADOW MODE ARMED. It enforces nothing; the PowerShell watcher is still")
        print("the only thing blocking. Compare them with:")
        print(rf"  Get-Content {args.root}\youtube-budget.log -Tail 20")
        print(rf"  Get-Content {args.root}\youtube-budget-python-shadow.log -Tail 20")
        return 0

    # Live install. Only one watcher may own the budget.
    if args.keep_powershell_watcher:
        print("[*] --keep-powershell-watcher: leaving the old task in place.")
        print("    WARNING: two watchers enforce from two state files and will disagree.")
    else:
        for old in (POWERSHELL_TASK, SHADOW_TASK):
            print(f"[*] Retiring superseded task {old}...")
            print(f"    {retire_task(old)}")

    print()
    print("ENFORCING.")
    if lane_armed:
        print("  Coverage: Windows applications (hosts file) + every client of the")
        print("            resolver, including the Google Play Games VM (AdGuard Home).")
    else:
        print("  " + "!" * 68)
        print("  !! RESOLVER LANE DISARMED - COVERAGE IS INCOMPLETE.")
        print("  !! The budget is enforced only for programs that use the Windows")
        print("  !! resolver. The Google Play Games VM runs its own resolver inside")
        print("  !! crosvm and will reach YouTube regardless of the budget, as will any")
        print("  !! phone or tablet on this network.")
        print(f"  !! Fix: run adguard/bootstrap.py in WSL, then re-run this installer.")
        print("  " + "!" * 68)
    return 0 if lane_armed else 2


if __name__ == "__main__":
    sys.exit(main())
