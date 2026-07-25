"""The seam between the budget logic and Windows.

Everything above this line is pure Python that runs and is tested on Linux; everything
below it is Windows-specific. That separation is the reason for the port: in the
PowerShell version the logic and the Windows calls were the same code, so the only way
to test enforcement was to enforce.

Two implementations exist: `windows.WindowsPlatform` (the real one) and `fake.FakePlatform`
(in-memory, for tests). A third mode -- shadow -- is not a separate implementation but a
wrapper, `ReadOnlyPlatform`, which permits every observation and refuses every mutation.
That is what lets the Python watcher run beside the PowerShell one on the live machine,
deciding and logging without touching anything.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Platform(Protocol):
    """What one tick needs from the outside world."""

    # --- clock ---------------------------------------------------------------

    def today(self) -> str:
        """Local calendar date as 'YYYY-MM-DD'. Local, not UTC: the budget resets at the
        child's midnight, not at London's."""
        ...

    def now_epoch(self) -> int:
        """Unix seconds. Used for pin ages, which are durations, so UTC is correct here."""
        ...

    # --- observation ---------------------------------------------------------

    def hosts_blocked(self) -> bool:
        ...

    def resolved_youtube_ips(self, since_epoch: int) -> list[tuple[str, bool]]:
        """(IP, is_video) pairs that YouTube-family hostnames resolved to since
        `since_epoch`. `is_video` marks a googlevideo answer -- the only names that
        carry actual video, and therefore what must survive the 32-filter cap.

        Read from the DNS-Client ETW channel rather than the DNS cache: modern YouTube
        streams over coalesced QUIC connections that stop re-resolving, and much of the
        video comes from ISP-embedded Google Global Cache nodes. The cache misses both.
        """
        ...

    def inbound_bytes(self) -> int | None:
        """Cumulative inbound bytes to the currently metered IPs, or None if the meter
        is not running. None must be treated as "skip this sample" rather than zero --
        counting a failed read as no traffic silently stops the budget."""
        ...

    def firewall_ips(self) -> list[str]:
        ...

    # --- mutation ------------------------------------------------------------

    def set_metered_ips(self, ips: list[str]) -> bool:
        """Point the byte meter at exactly these IPs. At most 32 (a pktmon limit).

        Returns whether the underlying byte COUNTER was restarted. The caller uses this
        to decide whether to drop its baseline: a delta spanning a counter restart is
        meaningless, but dropping the baseline when the counter did *not* restart throws
        away a perfectly good reading and costs a whole sample.
        """
        ...

    def block_hosts(self) -> str:
        """Splice the YouTube block in. Returns a short status for the log."""
        ...

    def unblock_hosts(self, scope: str = "auto") -> str:
        ...

    def set_firewall(self, ips: list[str]) -> str:
        """Block these endpoints. Hosts blocking stops new lookups but cannot terminate
        an already-open QUIC stream; this can."""
        ...

    def clear_firewall(self) -> str:
        ...

    def flush_dns(self) -> None:
        ...

    # --- logging -------------------------------------------------------------

    def log(self, message: str) -> None:
        ...


class ReadOnlyPlatform:
    """Wraps a Platform, permitting observation and refusing every mutation.

    This is shadow mode. The Python watcher runs beside the armed PowerShell one on the
    live machine and records what it *would* have done; the two logs are then diffed
    before cutover. Without this the only way to test the port would be to let it enforce
    on a real child's machine and see what happened.

    Mutations are recorded and logged with a `[shadow]` prefix rather than silently
    dropped, so the diff can compare intended actions, not just outcomes.
    """

    def __init__(self, inner: Platform) -> None:
        self._inner = inner
        self.suppressed: list[tuple[str, object]] = []

    # -- observation: straight through --
    def today(self) -> str: return self._inner.today()
    def now_epoch(self) -> int: return self._inner.now_epoch()
    def hosts_blocked(self) -> bool: return self._inner.hosts_blocked()
    def inbound_bytes(self) -> int | None: return self._inner.inbound_bytes()
    def firewall_ips(self) -> list[str]: return self._inner.firewall_ips()

    def resolved_youtube_ips(self, since_epoch: int) -> list[tuple[str, bool]]:
        return self._inner.resolved_youtube_ips(since_epoch)

    def log(self, message: str) -> None:
        self._inner.log(message)

    # -- mutation: recorded, never performed --
    def _suppress(self, action: str, detail: object = None) -> str:
        self.suppressed.append((action, detail))
        self._inner.log(f"[shadow] would {action}" + (f" {detail}" if detail is not None else ""))
        return "SHADOW"

    def block_hosts(self) -> str:
        return self._suppress("block hosts")

    def unblock_hosts(self, scope: str = "auto") -> str:
        return self._suppress("unblock hosts", f"scope={scope}")

    def set_firewall(self, ips: list[str]) -> str:
        return self._suppress("set firewall", f"{len(ips)} ips")

    def clear_firewall(self) -> str:
        return self._suppress("clear firewall")

    def flush_dns(self) -> None:
        self._suppress("flush dns")

    def set_metered_ips(self, ips: list[str]) -> bool:
        """SUPPRESSED. Metering looks passive but is not: pktmon is a single global
        session for the whole machine, so reconfiguring it tears down the filters the
        PowerShell watcher is using.

        This was not theoretical -- an earlier version let this call through, and one
        shadow tick wiped all 32 of the live watcher's filters, leaving it metering
        unfiltered traffic until the task was restarted. Shadow mode must not be able to
        degrade the thing it is shadowing.

        The consequence is that during the overlap the Python watcher reads the counters
        of whichever session already exists. Both implementations pin by the same rule,
        so the figures stay comparable, and any divergence is exactly what the pin-set
        diff is meant to surface.

        Returns False -- the counter was NOT restarted, because nothing was touched.
        Returning True here meant the meter dropped its baseline every time the pin set
        churned, so `observe()` spent almost every tick re-baselining and reported 0.
        That kept every pin looking idle, so they all aged out and were re-added, which
        churned the set again: a self-reinforcing loop that pinned the shadow watcher at
        `idle` forever while the machine streamed video.
        """
        self._suppress("set metered ips", f"{len(ips)} ips")
        return False
