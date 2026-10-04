"""How big the Android VM disk is, and how much free space C: has.

Two numbers per snapshot, because neither is the truth alone:

* **avd bytes** - the space the `userdata_*\\avd` folders take ON DISK (allocated, not
  logical, where Windows can tell us: a sparse or compressed image's logical size never
  moves when the VM frees blocks, its allocation can).
  On this machine (checked 2026-10-04) `userdata.img` is an 80 GB sparse file with
  about 62 GB actually allocated - so the logical size, which is what Explorer's
  "Size" column and the "~80 GB" figure show, is fixed and tells us nothing.
* **volume free bytes** - what the parent actually cares about. Noisy (anything else on
  the machine moves it), so it is reported as a session total, not per game.

The honest catch, said out loud in the final report: uninstalling inside the VM frees
blocks INSIDE the image. Whether the image file on Windows shrinks depends on the VM
discarding them, which may happen later or never. A game that left the library while the
avd stayed the same size is reported as exactly that.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

GIB = 1024 ** 3
# Below this, a change in avd size is noise (logs, caches the VM writes while running).
SHRINK_THRESHOLD = 50 * 1024 ** 2


@dataclass(frozen=True)
class Snapshot:
    avd_bytes: int | None     # None: no avd folder found
    free_bytes: int | None    # None: volume not measurable


def avd_dirs(local_appdata: str | None = None) -> list[Path]:
    """Every `%LOCALAPPDATA%\\Google\\Play Games\\userdata_*\\avd` that exists."""
    base = local_appdata or os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    root = Path(base) / "Google" / "Play Games"
    return sorted(p for p in root.glob("userdata_*/avd") if p.is_dir())


def _allocated(path: Path, logical: int) -> int:
    """Bytes the file occupies on disk. Windows: GetCompressedFileSizeW; else logical."""
    if sys.platform != "win32":
        return logical
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    k32.GetCompressedFileSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
    high = wintypes.DWORD(0)
    # 0xFFFFFFFF is also a legal low word, so failure is only failure with an error set.
    ctypes.set_last_error(0)
    low = k32.GetCompressedFileSizeW(str(path), ctypes.byref(high))
    if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
        return logical
    return (high.value << 32) + low


def tree_bytes(root: Path, allocated=_allocated) -> int:
    """Sum of on-disk sizes under `root`. Unreadable entries are skipped, not fatal:
    the VM holds some files open, and a partial number beats no number."""
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += allocated(Path(entry.path), entry.stat().st_size)
            except OSError:
                continue
    return total


def snapshot(dirs: list[Path], volume: str | None = None) -> Snapshot:
    avd = sum(tree_bytes(d) for d in dirs) if dirs else None
    try:
        target = volume or (dirs[0].anchor if dirs else os.path.abspath(os.sep))
        free = shutil.disk_usage(target).free
    except OSError:
        free = None
    return Snapshot(avd_bytes=avd, free_bytes=free)


def freed(before: int | None, after: int | None) -> int | None:
    """Bytes released between two measurements (positive = space came back)."""
    if before is None or after is None:
        return None
    return before - after


def gb(n: int | None, *, signed: bool = False) -> str:
    """Bytes as GB the way Explorer counts them (1024-based), two decimals."""
    if n is None:
        return "n/a"
    value = n / GIB
    return f"{value:+.2f} GB" if signed else f"{value:.2f} GB"
