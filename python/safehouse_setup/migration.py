"""Mandatory removal of runtime implementations superseded by current Python owners."""

from __future__ import annotations

import os
from pathlib import Path

from .winutil import SCHTASKS, run

LEGACY_TASKS = (
    "PiholeGPG-DNS",
    "SafeHouse-YouTubeBudget",
    "SafeHouse-YouTubeBudget-Shadow",
)


def legacy_files() -> tuple[Path, ...]:
    profile = Path(os.environ.get("USERPROFILE", Path.home()))
    system_drive = Path(os.environ.get("SystemDrive", "C:") + "/")
    return (
        profile / ".pihole" / "set-dns.ps1",
        system_drive / "Temp" / "mktask.ps1",
        system_drive / "SafeHouse" / "windows" / "youtube-budget" / "common.ps1",
        system_drive / "SafeHouse" / "windows" / "youtube-budget" / "install-task.ps1",
        system_drive / "SafeHouse" / "windows" / "youtube-budget" / "youtube-budget-ctl.ps1",
        system_drive / "SafeHouse" / "windows" / "youtube-budget" / "youtube-budget.ps1",
    )


def retire_legacy_runtime(paths: tuple[Path, ...] | None = None) -> None:
    """Remove every executable legacy owner before current task registration."""
    for task in LEGACY_TASKS:
        run([SCHTASKS, "/End", "/TN", task], timeout=30)
        run([SCHTASKS, "/Delete", "/TN", task, "/F"], timeout=30)
    for path in paths if paths is not None else legacy_files():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Registration still proceeds; stale files are inert once their task is gone.
            pass
