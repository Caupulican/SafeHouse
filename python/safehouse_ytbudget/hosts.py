"""Byte-preserving splice of the YouTube block into the Windows hosts file.

Rewriting this file wholesale normalises its line endings, and a hosts file Windows
considers malformed is silently ignored -- which looks exactly like enforcement working
while YouTube loads fine. It is also not ours to reformat: on this machine it is 754 KB
of adblock entries (26,775 lines, all CRLF) that other tooling maintains.

So this module works in **bytes**, never `str`, only ever inserts or removes its own
marked section, and writes that section using whichever line ending already dominates the
file. Everything outside the markers comes back byte-identical. That property is the
single most important thing here and is asserted directly in the tests, including against
the real file.

(The PowerShell implementation always writes LF regardless. That is harmless -- Windows
accepts either, and both implementations detect each other's markers -- but it does leave
bare-LF lines in an otherwise-CRLF file.)

Pure functions on bytes (`is_blocked`, `insert_block`, `remove_block`) are separated from
the file I/O (`read`, `write_atomic`) so the splice logic is testable without a Windows
hosts file, or any filesystem at all.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import atomicio

# Must match windows/youtube-budget/common.ps1 exactly -- the PowerShell watcher and this
# one have to recognise, and refuse to double-insert, each other's sections during the
# shadow-mode overlap.
START_MARKER = b"# === Parental block: YouTube (auto: daily budget) ==="
END_MARKER = b"# === end YouTube block ==="

# Any YouTube section, automatic or applied by `safehouse_setup toggle`.
_ANY_START = re.compile(rb"(?m)^# === Parental block: YouTube\b")

# Scope 'auto': only the section this watcher owns. Scope 'any': a parent's manual block
# too.
#
# `insert_block` puts a blank line before the marker, so removal consumes one to avoid
# accumulating blank lines across repeated block/unblock cycles. The `(?<=\n)` lookbehind
# is what makes that safe: it only consumes a newline that is itself preceded by one --
# i.e. a genuinely blank line. Without it, a section written without a leading blank line
# (a hand-made one, or the setup toggle's) would have the *previous line's* terminator
# eaten, joining two hosts entries into one corrupt line.
_LEAD_BLANK = rb"(?:(?<=\n)\r?\n)?"
_TRAIL = rb"[^\r\n]*(?:\r?\n)?"

_AUTO_SECTION = re.compile(
    rb"(?ms)" + _LEAD_BLANK + rb"^" + re.escape(START_MARKER)
    + rb".*?^" + re.escape(END_MARKER) + _TRAIL
)
_ANY_SECTION = re.compile(
    rb"(?ms)" + _LEAD_BLANK + rb"^# === Parental block: YouTube\b[^\r\n]*"
    + rb".*?^" + re.escape(END_MARKER) + _TRAIL
)


def is_blocked(raw: bytes) -> bool:
    """True if ANY YouTube section is present -- auto or manual.

    Deliberately broad: the caller must not double-insert on top of a parent's manual
    block, and must treat a manual block as "already blocked".
    """
    return _ANY_START.search(raw) is not None


def parse_block_list(raw: bytes) -> list[str]:
    """Parse parental-blocks/youtube.txt: one host per line, '#' starts a comment."""
    hosts: list[str] = []
    for line in raw.splitlines():
        host = line.split(b"#", 1)[0].strip()
        if host:
            hosts.append(host.decode("utf-8", errors="replace"))
    return hosts


def detect_newline(raw: bytes) -> bytes:
    """Return the line ending that dominates `raw`, defaulting to CRLF.

    Windows ships this file as CRLF and that is what it stays unless something rewrites
    it, so CRLF is the right default for an empty or newline-free file.
    """
    total = raw.count(b"\n")
    if total == 0:
        return b"\r\n"
    crlf = raw.count(b"\r\n")
    return b"\r\n" if crlf * 2 >= total else b"\n"


def insert_block(raw: bytes, hosts: list[str]) -> bytes:
    """Append the auto section. Returns `raw` unchanged if a section already exists."""
    if not hosts:
        raise ValueError("refusing to insert an empty block section")
    if is_blocked(raw):
        return raw

    nl = detect_newline(raw)
    out = raw
    # Guarantee the existing content ends with a newline before appending, or the
    # marker would land on the tail of whatever the last line was.
    if out and not out.endswith(b"\n"):
        out += nl

    # Blank separator line before the marker, but only when there is something to
    # separate from -- prepending it to an empty file would leave a stray blank line
    # behind after removal, since removal only consumes a newline that follows another.
    section = (nl if out else b"") + START_MARKER + nl
    for host in hosts:
        section += b"0.0.0.0 " + host.encode("utf-8") + nl
    section += END_MARKER + nl
    return out + section


def remove_block(raw: bytes, scope: str = "auto") -> bytes:
    """Remove the YouTube section. `scope` is 'auto' (ours only) or 'any'."""
    if scope not in ("auto", "any"):
        raise ValueError(f"scope must be 'auto' or 'any', got {scope!r}")
    pattern = _AUTO_SECTION if scope == "auto" else _ANY_SECTION
    return pattern.sub(b"", raw)


# --- file I/O ----------------------------------------------------------------


def read(path: Path) -> bytes:
    return path.read_bytes()


def write_atomic(path: Path, data: bytes) -> None:
    """Replace the hosts file atomically.

    A torn hosts file is worse than either outcome it sits between: Windows ignores a
    malformed one entirely, so a partial write silently disables every block in it --
    including any unrelated parental blocks the file already carried.
    """
    atomicio.write_bytes(path, data)
