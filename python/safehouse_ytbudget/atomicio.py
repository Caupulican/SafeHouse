"""Atomic file replacement.

Every file this watcher owns -- state, pins, and above all the Windows hosts file -- is
one a torn write would quietly break. A half-written state file either re-blocks a child
who had time left or hands out an unbounded day; a half-written hosts file is ignored by
Windows entirely, silently disabling every block in it.

`os.replace` is a genuine atomic rename on Windows as well as POSIX, which is why the
port uses it directly instead of the `Move-Item -Force` dance the PowerShell version
needed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

REPLACE_ATTEMPTS = 10
REPLACE_RETRY_SEC = 0.05


def write_bytes(path: Path, data: bytes) -> None:
    """Replace `path` with `data`, atomically.

    The temp file is created in the destination directory so the rename stays on one
    volume -- `os.replace` raises across volumes rather than silently falling back to a
    non-atomic copy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".safehouse-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                # Antivirus and the DNS Client briefly open the hosts file without
                # delete sharing. Keep the update atomic, but tolerate that short lock.
                time.sleep(REPLACE_RETRY_SEC)
    except BaseException:
        # A stale temp file would accumulate in a directory the child can read. Clean up
        # on any failure, including KeyboardInterrupt.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, obj: object) -> None:
    """Atomically write `obj` as pretty JSON with CRLF endings.

    CRLF because these files are read by humans in Notepad on a Windows box, and by the
    PowerShell implementation during the shadow-mode overlap.
    """
    text = json.dumps(obj, indent=2) + "\n"
    write_bytes(path, text.replace("\n", "\r\n").encode("utf-8"))
