"""Turning a cumulative byte counter into "is this child watching video right now?".

The byte meter reports one monotonically increasing number: total inbound bytes to the
pinned IPs since the meter started. This module turns successive readings into a
throughput, averages it over a rolling window, and answers the only question that
matters.

Why a window rather than an instantaneous rate: a paused video, an ad break or a buffer
top-up all produce brief zero-byte samples, and stopping the clock for each would
under-count badly. Conversely a closed tab must stop the clock reasonably promptly. The
window is the compromise, and it is why `window_sec` is several times `sample_sec`.

Pure. Byte readings and elapsed time are passed in.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Meter:
    """Rolling-window throughput from a cumulative counter."""

    window_sec: int
    sample_sec: int
    min_throughput_kbps: int

    _prev: int | None = field(default=None, repr=False)
    _samples: deque[float] = field(default_factory=deque, repr=False)
    # Seconds since the last sample that exceeded the threshold; None = never yet.
    # This, not the rolling average, is what decides "watching" -- see is_watching().
    _since_active: float | None = field(default=None, repr=False)

    @property
    def _capacity(self) -> int:
        # At least one sample, or a window shorter than a sample would never fill.
        return max(1, self.window_sec // max(1, self.sample_sec))

    def reset(self) -> None:
        """Forget history. Called when the metered IP set changes, because the counter
        restarts and a delta across that boundary is meaningless."""
        self._prev = None
        self._samples.clear()
        self._since_active = None

    def observe(self, cumulative: int | None, elapsed_sec: float) -> float | None:
        """Feed one reading. Returns kbit/s for this sample, or None if it is unusable.

        None means "skip", never "zero". A failed counter read counted as zero traffic
        would silently stop the budget while the child kept watching -- the failure mode
        this whole project exists to prevent.
        """
        if cumulative is None:
            # Meter not running. Drop the baseline too: whenever it comes back the
            # counter may have restarted, and a delta against a stale baseline would be
            # a huge phantom spike.
            self._prev = None
            return None

        if elapsed_sec <= 0:
            return None

        prev, self._prev = self._prev, cumulative
        if prev is None:
            return None  # first reading establishes the baseline only

        delta = cumulative - prev
        if delta < 0:
            # The counter restarted (the session was recreated). Re-baseline rather
            # than crediting a negative or absurd figure.
            return None

        kbps = (delta * 8) / 1000.0 / elapsed_sec
        self._samples.append(kbps)
        while len(self._samples) > self._capacity:
            self._samples.popleft()

        # High-water mark, not a decaying average. See is_watching().
        if kbps >= self.min_throughput_kbps:
            self._since_active = 0.0
        elif self._since_active is not None:
            self._since_active += elapsed_sec

        return kbps

    def average_kbps(self) -> float:
        """Mean over the window. Reported in the log for diagnostics; it does NOT decide
        whether the child is watching."""
        if not self._samples:
            return 0.0
        return sum(self._samples) / len(self._samples)

    def is_watching(self) -> bool:
        """True while within `window_sec` of the last above-threshold sample.

        A high-water mark, deliberately, matching the PowerShell watcher. An earlier
        version of this port took the rolling *average* over the window instead, which
        looks equivalent and is not: the average decays as idle samples accumulate, so a
        stream that dips (an ad break, a buffer top-up, an already-buffered segment) fell
        below the threshold and stopped the clock while playback continued.

        The two were compared on the live machine over 45 minutes: 18 samples where
        PowerShell credited and this did not, and zero the other way. Entirely
        one-directional, i.e. the port was under-counting and handing the child extra
        time -- the wrong direction to be wrong in for a parental control.
        """
        if self._since_active is None:
            return False
        return self._since_active < self.window_sec
