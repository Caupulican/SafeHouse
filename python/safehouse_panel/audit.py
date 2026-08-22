"""panel.log - who changed what, from where, and from what to what.

The guards' logs already record what enforcement *did*. This one records what a person
did to the enforcement, which is a different question and the one that matters when the
answer to "why did Roblox run for two hours yesterday" is "somebody moved a slider".

Format and failure behaviour are borrowed from the guards rather than reinvented:
`roblox_guard.log` appends `YYYY-MM-DD HH:MM:SS <message>` with CRLF endings and swallows
every OSError, because logging must never take the process down. One format means one
thing to grep for across all four files in the directory.
"""

from __future__ import annotations

from pathlib import Path

from safehouse_ytbudget.roblox_guard import log as _append

from . import PANEL_LOG


def audit(root: Path, message: str) -> None:
    """Append one line to `root/panel.log`. Never raises."""
    _append(Path(root) / PANEL_LOG, message)


def format_change(action: str, before: object, after: object, *, who: str,
                  detail: str = "") -> str:
    """One line describing a mutation.

    `before` and `after` are both recorded even when they are equal: a parent pressing
    Apply on a value that was already set is still a decision that was made at a time,
    and "nothing happened" is a useful thing to be able to prove afterwards.
    """
    arrow = f"{before!r} -> {after!r}"
    tail = f" - {detail}" if detail else ""
    return f"[change] {action}: {arrow} (by {who}){tail}"


def format_denied(what: str, *, who: str, why: str) -> str:
    return f"[denied] {what} refused for {who}: {why}"
