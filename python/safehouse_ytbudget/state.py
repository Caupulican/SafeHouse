"""Persistent daily state for the YouTube watch-time budget.

Deliberately narrow: this file holds *what happened today* and today-only overrides.
It does NOT hold the daily limit -- that lives in config.json. In the PowerShell version
`limit_min` was stored here too, was written once when the file was first created, and
then never refreshed, so editing the config had no effect on enforcement. Keeping the
limit out of the state makes that class of bug impossible rather than merely fixed.

Writes are atomic via `os.replace`, which is a genuine atomic rename on Windows as well
as POSIX. A torn state file would either re-block a child who had time left or hand out
an unbounded day, so a half-written file must never be observable.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class State:
    # Local calendar date this usage belongs to, "YYYY-MM-DD". The reset is driven by
    # comparing this to today rather than by a midnight timer, so it is idempotent and
    # survives the machine being off across one or several days.
    date: str
    # Seconds of metered watching credited today.
    seconds_used: int = 0
    # Extra seconds granted for today only (`ctl grant`). Cleared by the day reset.
    bonus_sec: int = 0
    # The budget-driven block is currently applied.
    blocked_by_budget: bool = False
    # A parent forced the block on (`ctl block`). Held separately so the per-tick
    # reconcile does not lift a deliberate block just because usage sits under the limit.
    manual_block: bool = False
    # Unrecognised keys from an older or newer version, carried through untouched so a
    # rollback does not lose data written by the other version.
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    def effective_limit_sec(self, limit_sec: int) -> int:
        return limit_sec + self.bonus_sec


_FIELDS = {"date", "seconds_used", "bonus_sec", "blocked_by_budget", "manual_block"}


def from_json(text: str, *, today: str) -> tuple[State, list[str]]:
    """Parse a state file. A corrupt or absent state resets to a clean day.

    Failing closed (fresh state, zero usage) rather than raising keeps the watcher
    running. The cost is at most one day's usage forgotten; the alternative is a dead
    watcher and no enforcement at all, which is strictly worse.
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        return State(date=today), [f"state file is not valid JSON ({exc}); starting a fresh day"]

    if not isinstance(obj, dict):
        return State(date=today), ["state file is not a JSON object; starting a fresh day"]

    warnings: list[str] = []

    def _int(key: str, default: int = 0) -> int:
        raw = obj.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            warnings.append(f"state.{key} invalid ({raw!r}); using {default}")
            return default
        return raw

    def _bool(key: str) -> bool:
        raw = obj.get(key, False)
        if not isinstance(raw, bool):
            warnings.append(f"state.{key} invalid ({raw!r}); using False")
            return False
        return raw

    date = obj.get("date")
    if not isinstance(date, str) or not date:
        warnings.append("state.date missing or invalid; starting a fresh day")
        date = today

    return (
        State(
            date=date,
            seconds_used=_int("seconds_used"),
            bonus_sec=_int("bonus_sec"),
            blocked_by_budget=_bool("blocked_by_budget"),
            manual_block=_bool("manual_block"),
            extra={k: v for k, v in obj.items() if k not in _FIELDS},
        ),
        warnings,
    )


def to_json(state: State) -> str:
    obj = dict(state.extra)
    obj.update({k: v for k, v in asdict(state).items() if k != "extra"})
    return json.dumps(obj, indent=2)


def load(path: Path, *, today: str) -> tuple[State, list[str]]:
    if not path.exists():
        return State(date=today), []
    try:
        return from_json(path.read_text(encoding="utf-8-sig"), today=today)
    except OSError as exc:
        return State(date=today), [f"could not read {path} ({exc}); starting a fresh day"]


def save(path: Path, state: State) -> None:
    """Atomically replace the state file.

    The temp file is created in the destination directory so the rename stays on one
    volume -- `os.replace` across volumes raises rather than falling back to a copy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ybstate-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(to_json(state) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leaving a stale temp file behind would accumulate junk in a directory the
        # child can read; drop it on any failure including KeyboardInterrupt.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
