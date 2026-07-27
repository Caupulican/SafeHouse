"""In-memory Platform for tests.

Holds the whole outside world as plain attributes a test can set and assert on,
including a clock the test advances by hand. Nothing here touches a filesystem, a
network, or a subprocess, so the full tick can be exercised on Linux in microseconds.
"""

from __future__ import annotations


class FakePlatform:
    def __init__(
        self,
        *,
        today: str = "2026-07-25",
        now: int = 1_800_000_000,
        hosts_blocked: bool = False,
        resolver: bool | None = None,
    ) -> None:
        self._today = today
        self._now = now
        self._hosts_blocked = hosts_blocked
        # None = resolver lane disarmed, which is the default so that tests written
        # before the lane existed keep describing a single-layer machine.
        self._resolver_blocked = resolver

        # Observation knobs a test drives.
        self.resolved: list[str] = []
        self.bytes_counter: int | None = 0
        self._firewall: list[str] = []
        self.metered: list[str] = []
        self.meter_ok = True
        # Armed but not answering, e.g. AdGuard restarting. Reads report None (unknown)
        # and writes fail, without the lane counting as disarmed.
        self.resolver_unreachable = False

        # Recorded calls, for assertions.
        self.calls: list[str] = []
        self.logs: list[str] = []

    # --- clock ---------------------------------------------------------------

    def today(self) -> str:
        return self._today

    def now_epoch(self) -> int:
        return self._now

    def advance(self, seconds: int, *, new_day: str | None = None) -> None:
        """Move the clock. `new_day` also rolls the calendar date, for reset tests."""
        self._now += seconds
        if new_day is not None:
            self._today = new_day

    # --- observation ---------------------------------------------------------

    def hosts_blocked(self) -> bool:
        return self._hosts_blocked

    def resolver_blocked(self) -> bool | None:
        if self._resolver_blocked is None or self.resolver_unreachable:
            return None
        return self._resolver_blocked

    def resolved_youtube_ips(self, since_epoch: int) -> list[tuple[str, bool]]:
        self.calls.append(f"resolved_youtube_ips(since={since_epoch})")
        # Tests may set plain IPs or (ip, is_video) pairs.
        return [(r, False) if isinstance(r, str) else r for r in self.resolved]

    def inbound_bytes(self) -> int | None:
        return self.bytes_counter

    def firewall_ips(self) -> list[str]:
        return list(self._firewall)

    # --- mutation ------------------------------------------------------------

    def set_metered_ips(self, ips: list[str]) -> bool:
        self.calls.append(f"set_metered_ips({sorted(ips)})")
        if self.meter_ok:
            self.metered = sorted(ips)
        return self.meter_ok

    def block_hosts(self) -> str:
        """Drives every armed layer, like the real one -- each independently, so a tick
        that finds one layer already applied still repairs the other."""
        self.calls.append("block_hosts")
        result = "ALREADY" if self._hosts_blocked else "BLOCKED"
        self._hosts_blocked = True
        if self._resolver_blocked is not None and not self.resolver_unreachable:
            self._resolver_blocked = True
        return result

    def unblock_hosts(self, scope: str = "auto") -> str:
        self.calls.append(f"unblock_hosts(scope={scope})")
        result = "UNBLOCKED" if self._hosts_blocked else "NONE"
        self._hosts_blocked = False
        if self._resolver_blocked is not None and not self.resolver_unreachable:
            self._resolver_blocked = False
        return result

    def set_firewall(self, ips: list[str]) -> str:
        self.calls.append(f"set_firewall({sorted(ips)})")
        self._firewall = sorted(ips)
        return f"BLOCKED:{len(ips)}"

    def clear_firewall(self) -> str:
        self.calls.append("clear_firewall")
        if not self._firewall:
            return "NONE"
        self._firewall = []
        return "UNBLOCKED"

    def flush_dns(self) -> None:
        self.calls.append("flush_dns")

    # --- logging -------------------------------------------------------------

    def log(self, message: str) -> None:
        self.logs.append(message)
