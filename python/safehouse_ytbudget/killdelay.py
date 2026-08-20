"""The pause between seeing a program and ending it - shared by both kill lanes.

Roblox and the browser are enforced the same way: a one-shot scheduled task fires every
minute, looks at what is running, and ends what is not allowed. Neither ends it the
instant it is seen. Both wait a random 1-5 seconds and then look AGAIN, and only what is
still up gets killed.

That pause does three things, and none of them is politeness:

* A window that vanishes the instant it opens reads as a broken PC, and a broken PC gets
  debugged - by a child who has local admin on this machine. A window that closes a beat
  later, with a message saying why, reads as a rule.
* The exact moment is unpredictable, so it cannot be timed and raced.
* Anything that disappears DURING the pause was closed by the child, not by the guard.
  Re-observing afterwards is what keeps that out of the log as a kill that never was.

Be honest about the scale of it. With a one-minute firing interval the pause is a small
part of the total latency: up to ~60 seconds to notice a launch, then 1-5 seconds to act.
It is not what makes the enforcement gentle. Its remaining job is that the exact moment
cannot be predicted, and that a window closing a beat later reads as a rule rather than a
crash.

Sleeping inside a one-shot is affordable only because it happens exclusively on a run
that has already found something to end. The idle path - which is nearly every run, on
nearly every day - never reaches this module.
"""

from __future__ import annotations

import random
import time

DEFAULT_MIN_SEC = 1
DEFAULT_MAX_SEC = 5

# Both guards run under a scheduled task carrying ExecutionTimeLimit PT5M. A pause
# anywhere near that would be cut off by the scheduler mid-run - a browser that survives
# because the guard was terminated while waiting - so the configurable ceiling stays far
# below it. 60s is already ten times the default and leaves 4 minutes of headroom.
MAX_DELAY_SEC = 60


def pick_delay(low: int, high: int, pick=random.randint) -> int:
    """Draw a delay, tolerating bounds handed over in the wrong order."""
    lo, hi = (low, high) if low <= high else (high, low)
    return max(0, int(pick(lo, hi)))


def pause_and_recheck(observe, *, low: int, high: int, announce=None,
                      sleep=time.sleep, pick=random.randint) -> tuple[int, tuple[str, ...]]:
    """Wait a random `low`..`high` seconds, then ask `observe` what is still up.

    Returns `(seconds waited, still running)`. `announce` is called with the drawn delay
    BEFORE the sleep: both lanes log their intent first, so a run that the scheduler cuts
    off mid-pause still leaves the record that the guard saw the program.
    """
    delay = pick_delay(low, high, pick)
    if announce is not None:
        announce(delay)
    if delay:
        sleep(delay)
    return delay, tuple(observe())


def _whole_number(obj: dict, key: str, default: int, lo: int, hi: int,
                  warnings: list[str]) -> int:
    raw = obj.get(key, default)
    # bool is an int subclass in Python, and `"delay_max_sec": true` is a typo, not a 1.
    if isinstance(raw, bool) or not isinstance(raw, int):
        warnings.append(f"{key} must be a whole number (got {raw!r}), using {default}")
        return default
    if not lo <= raw <= hi:
        clamped = min(max(raw, lo), hi)
        warnings.append(f"{key}={raw} is outside {lo}..{hi}, using {clamped}")
        return clamped
    return raw


def load_bounds(obj: dict, warnings: list[str]) -> tuple[int, int]:
    """Read `delay_min_sec`/`delay_max_sec` out of an already-parsed config object.

    Both lanes keep these keys in their own config file next to their own settings, but
    they mean the same thing and are validated once, here. Every failure mode lands on a
    SHORT pause - there is no value of these keys that grants the child anything, so
    unlike a budget limit they need no strict-fallback gymnastics, just a warning.
    """
    low = _whole_number(obj, "delay_min_sec", DEFAULT_MIN_SEC, 0, MAX_DELAY_SEC, warnings)
    high = _whole_number(obj, "delay_max_sec", DEFAULT_MAX_SEC, 0, MAX_DELAY_SEC,
                         warnings)
    if high < low:
        warnings.append(f"delay_max_sec={high} is below delay_min_sec={low}, using {low}")
        high = low
    return low, high
