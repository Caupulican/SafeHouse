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
ROBLOX_TASK = "SafeHouse-RobloxBudget"
BROWSER_TASK = "SafeHouse-BrowserGuard"

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


# One-shot that must repeat forever, unlike the watcher's infinite loop above. Both kill
# lanes (Roblox and the browser) use this shape. A true "on process start" trigger is not
# possible here: scheduled-task event filters speak an XPath subset without substring
# matching, and Roblox's executable path changes on every update (a versioned directory
# under the child's profile), so a 4688 filter cannot name it. The one-minute repetition
# IS the launch detector: a launch while the budget is spent survives at most a minute.
# RegistrationTrigger covers install day, BootTrigger every day after; both repeat
# indefinitely (no Duration).
#
# ExecutionTimeLimit PT5M is the ceiling on a single run. A run that finds nothing takes
# one `tasklist`; a run that finds something sleeps a random 1-5 seconds before killing
# it, so the limit is ~60x the worst case and exists only to reap a wedged run. It is
# also why the guards clamp their configurable pause far below it: a pause the scheduler
# terminates is a browser that survives.
GUARD_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT1M</Interval></Repetition>
    </BootTrigger>
    <RegistrationTrigger>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT1M</Interval></Repetition>
    </RegistrationTrigger>
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
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
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
                  description: str, start: bool, xml_template: str = TASK_XML) -> bool:
    xml = xml_template.format(
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


def register_roblox_guard(*, python: Path, workdir: Path) -> bool:
    """The Roblox kill lane - deliberately independent of the YouTube watcher task.

    It shares nothing at runtime with the watcher (own state, own log, no pktmon, no
    hosts file), so disabling one must not disable the other. RegistrationTrigger
    starts the repetition immediately; there is no /Run to issue.
    """
    return register_task(
        name=ROBLOX_TASK, python=python,
        arguments="-m safehouse_ytbudget.roblox_guard", workdir=workdir,
        description=("SafeHouse Roblox budget - kills the game when the daily hour is "
                     "spent (a joined session never re-resolves DNS, so only ending "
                     "the process enforces mid-session)"),
        start=False, xml_template=GUARD_TASK_XML,
    )


def register_browser_guard(*, python: Path, workdir: Path) -> bool:
    """The no-browsing kill lane - the same repeating one-shot as the Roblox lane.

    It was a resident loop first, so the gap between a browser opening and closing could
    be shorter than a scheduled task's finest repetition. The parent chose the cheap
    shape instead: a run that finds no browser does nothing at all, so a task that fires
    every minute of every day costs one `tasklist` a minute on an idle machine, against a
    process that would otherwise be up all day. The price is up to a minute of latency
    before a launch is noticed. Hence GUARD_TASK_XML, and no /Run - the
    RegistrationTrigger starts the repetition immediately.
    """
    return register_task(
        name=BROWSER_TASK, python=python,
        arguments="-m safehouse_ytbudget.browser_guard", workdir=workdir,
        description=("SafeHouse browser guard - closes any web browser within about a "
                     "minute of it opening (no browsing on this PC; games are untouched)"),
        start=False, xml_template=GUARD_TASK_XML,
    )


def seed_json(path: Path, payload: dict) -> str:
    """Write `payload` only when `path` is absent.

    Never overwrite: these files are the parent's authoring surface, and re-running the
    installer must not silently revert an edit - the same reasoning that makes
    `config.render` layer the live config underneath `.env`.
    """
    if path.exists():
        return f"kept existing {path.name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return f"seeded {path.name} with {json.dumps(payload)}"


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
    from safehouse_setup.migration import retire_legacy_runtime

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
    p.add_argument("--roblox-guard-only", action="store_true",
                   help="register only the Roblox kill-lane task; touches nothing else "
                        "(no watcher, no config render, no task retirement)")
    p.add_argument("--browser-guard-only", action="store_true",
                   help="register only the browser kill-lane task; touches nothing else "
                        "(no watcher, no config render, no task retirement)")
    args = p.parse_args(argv)

    if not is_admin():
        print("Not elevated. Re-run this from an elevated prompt.")
        return 1
    if not args.python.exists():
        print(f"Vendored runtime not found at {args.python}.")
        print("Fetch the embeddable distribution first - see windows/youtube-budget/README.md")
        return 1

    pkg_dir = Path(__file__).resolve().parent

    if args.roblox_guard_only:
        print(f"[*] Registering scheduled task {ROBLOX_TASK}...")
        return 0 if register_roblox_guard(python=args.python, workdir=pkg_dir) else 1

    if args.browser_guard_only:
        print(f"[*] Registering scheduled task {BROWSER_TASK}...")
        return 0 if register_browser_guard(python=args.python, workdir=pkg_dir) else 1

    retire_legacy_runtime()

    print(f"[*] Locking down {args.root}...")
    # The ACL is set on the whole root, so every file the guards own -- including the two
    # seeded below and their logs -- is covered without naming them here.
    lock_down(args.root)

    print("[*] Seeding the guard configs (existing files are never overwritten)...")
    # 0 minutes is the standing policy, not a placeholder: no Roblox unless a parent
    # raises it. Seeding it explicitly means the file exists to be edited, rather than a
    # parent having to know the name of a file that is not there.
    # delay_*_sec is the pause between seeing a program and ending it, on both lanes.
    # Both tasks fire once a minute, so the pause is a small part of the latency; its job
    # is that the exact moment cannot be timed, and that anything closed DURING it counts
    # as the child's own doing rather than a kill.
    print("    " + seed_json(args.root / "roblox-budget.json", {
        "limit_min": 0,
        "delay_min_sec": 1,
        "delay_max_sec": 5,
    }))
    print("    " + seed_json(args.root / "browser-guard.json", {
        "enabled": True,
        "delay_min_sec": 1,
        "delay_max_sec": 5,
        "users": [],
        "message": ("Browsing is off on this PC. Games still work - "
                    "ask a grown-up if you need a web page."),
    }))

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

    print(f"[*] Registering scheduled task {ROBLOX_TASK}...")
    if not register_roblox_guard(python=args.python, workdir=pkg_dir):
        return 1

    print(f"[*] Registering scheduled task {BROWSER_TASK}...")
    if not register_browser_guard(python=args.python, workdir=pkg_dir):
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
