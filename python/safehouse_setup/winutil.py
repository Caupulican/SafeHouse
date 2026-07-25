"""Shared helpers for the Windows host-configuration tools.

These replace the PowerShell scripts this project started with. PowerShell was never
doing anything Python cannot: every one of those scripts was ultimately calling `netsh`,
the registry, or the task scheduler. Going through Python instead means one language, one
set of tests, and one runtime to ship — the embeddable interpreter the watcher already
needs.

Nothing here imports anything outside the standard library.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path

SYSTEM_ROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
SYS32 = SYSTEM_ROOT / "System32"

NETSH = SYS32 / "netsh.exe"
REG = SYS32 / "reg.exe"
SCHTASKS = SYS32 / "schtasks.exe"
IPCONFIG = SYS32 / "ipconfig.exe"
WSL = SYS32 / "wsl.exe"
POWERSHELL = SYS32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def powershell(script: str, timeout: int = 180, check: bool = False) -> tuple[int, str]:
    """Run a PowerShell snippet and return (returncode, output).

    Python owns the logic; PowerShell is used only where it is genuinely the right tool
    -- chiefly the DNS-client, firewall and scheduled-task cmdlets, which expose things
    with no clean equivalent elsewhere. NRPT is the clearest case: `Add-DnsClientNrptRule`
    is a supported, tested API, whereas writing its registry keys by hand is undocumented
    guesswork on the one subsystem that must never be left broken.

    The point of the rule "no PowerShell" was no standalone .ps1 files to maintain, test
    and keep in sync. Calling a cmdlet from Python does not reintroduce that problem: the
    logic, the error handling and the tests all stay here.
    """
    return run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        timeout=timeout,
        check=check,
    )


def run(args: list[str], timeout: int = 120, check: bool = False) -> tuple[int, str]:
    """Run a command, returning (returncode, stdout+stderr). Never raises unless `check`.

    Host configuration touches a lot of moving parts, and one of them failing should
    produce a clear message rather than a traceback halfway through an install that has
    already changed the machine.
    """
    try:
        proc = subprocess.run(
            [str(a) for a in args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        if check and proc.returncode != 0:
            raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {out.strip()[:300]}")
        return proc.returncode, out
    except (OSError, subprocess.SubprocessError) as exc:
        if check:
            raise RuntimeError(f"{args[0]} could not run: {exc}") from exc
        return -1, str(exc)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def require_admin(what: str) -> None:
    if not is_admin():
        raise SystemExit(
            f"{what} needs Administrator rights.\n"
            f"Re-run from an elevated prompt (right-click -> Run as administrator)."
        )


def reg_set(key: str, name: str, value: str | int, kind: str = "REG_SZ") -> bool:
    """Write one registry value. Returns success."""
    rc, _ = run([REG, "add", key, "/v", name, "/t", kind, "/d", str(value), "/f"])
    return rc == 0


def reg_delete_key(key: str) -> bool:
    rc, _ = run([REG, "delete", key, "/f"])
    return rc == 0


def reg_query(key: str, name: str) -> str | None:
    rc, out = run([REG, "query", key, "/v", name])
    if rc != 0:
        return None
    # "    ValueName    REG_SZ    the value"
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].lower() == name.lower():
            return parts[2].strip()
    return None


def wsl_ip(distro: str | None = None) -> str | None:
    """The WSL eth0 address, which is where the resolver listens.

    It changes across reboots, which is why nothing in this repo hardcodes it.
    """
    args = [WSL]
    if distro:
        args += ["-d", distro]
    args += ["bash", "-lc", "hostname -I | cut -d' ' -f1"]
    rc, out = run(args, timeout=60)
    if rc != 0:
        return None
    ip = out.strip().split()[0] if out.strip() else ""
    return ip or None


def flush_dns() -> None:
    run([IPCONFIG, "/flushdns"])
