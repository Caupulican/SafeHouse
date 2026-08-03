"""One tick: observe the world, decide, apply, persist.

This is the only module that touches all the others, and it is deliberately thin. All
the judgement lives in `decide()`, which is pure; all the Windows lives behind
`Platform`. What remains here is sequencing, and the sequencing has one rule worth
stating: **observe everything before deciding, apply everything after.** Reading the
world halfway through applying changes to it is what produced the midnight gap bug in
the first version of `decide()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import pins as pinmod
from . import state as statemod
from .config import Config
from .decide import (
    Action,
    BlockHosts,
    ClearFirewall,
    CreditTime,
    FlushDns,
    Observation,
    ResetDay,
    SetFirewall,
    UnblockHosts,
    decide,
)
from .meter import Meter
from .platform.base import Platform
from .state import State


@dataclass
class Paths:
    state: Path
    pins: Path

    @classmethod
    def under(cls, root: Path, *, suffix: str = "") -> "Paths":
        """Standard layout under ProgramData. `suffix` keeps a shadow run's files from
        colliding with the PowerShell watcher's -- both must be able to run at once."""
        return cls(
            state=root / f"youtube-budget{suffix}.json",
            pins=root / f"yt-pinned-ips{suffix}.json",
        )


@dataclass
class TickResult:
    state: State
    actions: list[Action]
    kbps: float
    watching: bool
    pin_count: int


@dataclass
class Ticker:
    platform: Platform
    cfg: Config
    paths: Paths

    _meter: Meter | None = field(default=None, repr=False)
    _pins: list[pinmod.Pin] = field(default_factory=list, repr=False)
    _metered: tuple[str, ...] = field(default=(), repr=False)
    _last_dns_epoch: int = field(default=0, repr=False)
    _last_tick_epoch: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self._meter is None:
            self._meter = Meter(
                window_sec=self.cfg.window_sec,
                sample_sec=self.cfg.sample_sec,
                min_throughput_kbps=self.cfg.min_throughput_kbps,
            )
        self._pins = pinmod.load(self.paths.pins)

    # --- observation ---------------------------------------------------------

    def _observe(self) -> tuple[Observation, float]:
        now = self.platform.now_epoch()

        # Elapsed since the last tick, not the nominal sample interval: a tick that ran
        # late (machine busy, disk stall) would otherwise be scored as if it covered
        # only sample_sec, inflating the apparent throughput.
        elapsed = (now - self._last_tick_epoch) if self._last_tick_epoch else self.cfg.sample_sec
        self._last_tick_epoch = now

        # Harvest resolutions since the last look, with a small overlap so an event
        # landing between two queries is not missed.
        #
        # On the FIRST tick reach back a full pin-expiry window instead. A watcher that
        # has just started (or restarted) otherwise sees only the last few seconds of
        # DNS and is effectively blind until the browser happens to re-resolve -- which,
        # with QUIC coalescing, may not happen for an hour of continuous watching.
        # Reaching further back than the expiry window would be pointless: those pins
        # would be dropped as idle in the same tick.
        first_look = self._last_dns_epoch == 0
        since = (
            now - self.cfg.pin_idle_expire_min * 60
            if first_look
            else self._last_dns_epoch
        )
        resolved = self.platform.resolved_youtube_ips(since)
        self._last_dns_epoch = now

        # Read bytes BEFORE reconfiguring the meter: the counter belongs to the current
        # filter set, and changing filters restarts it.
        #
        # With nothing metered there is no YouTube traffic by definition, so the counter
        # is meaningless and must not be read as though it were zero-and-valid. Making
        # that explicit here rather than trusting the meter to report nothing keeps a
        # platform quirk from crediting unrelated traffic to the budget.
        raw = self.platform.inbound_bytes() if self._metered else None
        kbps = self._meter.observe(raw, elapsed)

        # A None reading is UNKNOWN, not idle. Crediting the sample from the rolling
        # window's stale contents would be phantom accounting; skipping it is what the
        # PowerShell implementation does and is the safe direction -- at worst one
        # sample of watching goes uncounted. (A genuine pause is a *valid* zero-byte
        # reading and is still absorbed by the window, which is a different case.)
        sample_usable = kbps is not None

        # Any traffic at all means the whole pinned set is active. The meter reports one
        # aggregate counter, not per-IP figures, so this is the honest granularity --
        # and it is the right one: a coalesced QUIC stream is carried by whichever pin
        # the browser settled on, and we cannot tell which.
        active = set(pinmod.ips(self._pins)) if (kbps or 0) > 0 else set()

        self._pins, added, evicted = pinmod.update(
            self._pins,
            resolved,
            now=now,
            active_ips=active,
            idle_expire_sec=self.cfg.pin_idle_expire_min * 60,
        )
        for p in added:
            kind = "video" if p.video else "asset"
            self.platform.log(f"[pin] +{p.ip} ({kind}) pins={len(self._pins)}")
        for p in evicted:
            self.platform.log(f"[pin] -{p.ip} idle (pins={len(self._pins)})")

        # Reconfigure the meter only when the set actually changed.
        #
        # Drop the baseline ONLY if the platform reports that it really restarted the
        # counter. In shadow mode it does not -- pktmon belongs to the other watcher and
        # is left alone -- so the counter runs on and the baseline stays valid. Resetting
        # regardless meant re-baselining on almost every tick, reporting 0 kbps, marking
        # every pin idle, ageing them all out and re-adding them, which churned the set
        # again. A self-reinforcing loop that reported `idle` forever while video played.
        current = tuple(pinmod.ips(self._pins))
        if current != self._metered:
            counter_restarted = self.platform.set_metered_ips(list(current))
            self._metered = current
            if counter_restarted:
                self._meter.reset()

        return (
            Observation(
                today=self.platform.today(),
                watching=sample_usable and self._meter.is_watching(),
                hosts_blocked=self.platform.hosts_blocked(),
                resolver_blocked=self.platform.resolver_blocked(),
                persistent_blocked=self.platform.persistent_blocked(),
                pinned_ips=current,
                firewall_ips=tuple(self.platform.firewall_ips()),
            ),
            self._meter.average_kbps(),
        )

    # --- application ---------------------------------------------------------

    def _apply(self, actions: list[Action], st: State, limit_sec: int,
               *, persistent_blocked: bool = False) -> None:
        for action in actions:
            match action:
                case ResetDay(previous=prev, today=today):
                    self.platform.log(f"[reset] new day {today} (was {prev}) - usage zeroed")
                case CreditTime():
                    pass  # already folded into the state; logged once per tick below
                case BlockHosts(reason=reason):
                    # The AdGuard service list is flat, so the local marker carries the
                    # reasons independently.  A drift repair for a persistent parental
                    # hold must not invent a budget owner that later prevents --allow.
                    owner = (
                        "budget"
                        if st.blocked_by_budget or st.manual_block
                        else "manual" if persistent_blocked else "budget"
                    )
                    res = self.platform.block_hosts(owner=owner)
                    if reason == "reblock":
                        # Deliberately does not name a layer: this fires when EITHER the
                        # hosts file or the resolver has drifted open, and the status in
                        # `res` already says which was repaired and which was ALREADY
                        # fine. Saying "hosts block had been removed", as this once did,
                        # sent a reader hunting through the hosts file when it was the
                        # resolver that had been cleared.
                        self.platform.log(
                            f"[reblock] an enforcement layer had drifted open - re-applied ({res})")
                    else:
                        self.platform.log(
                            f"[enforce] budget reached ({st.seconds_used}s >= {limit_sec}s)"
                            f" - YouTube BLOCKED ({res})"
                        )
                case UnblockHosts(reason=reason):
                    res = self.platform.unblock_hosts("auto")
                    self.platform.log(f"[unblock] {reason} - auto block lifted ({res})")
                case SetFirewall(ips=ips_):
                    res = self.platform.set_firewall(list(ips_))
                    self.platform.log(f"[enforce] endpoint firewall refreshed ({res})")
                case ClearFirewall():
                    self.platform.clear_firewall()
                case FlushDns():
                    self.platform.flush_dns()

    # --- one tick ------------------------------------------------------------

    def run(self) -> TickResult:
        st, warnings = statemod.load(self.paths.state, today=self.platform.today())
        for w in warnings:
            self.platform.log(f"[warn] {w}")

        obs, kbps = self._observe()
        new_state, actions = decide(st, self.cfg, obs)
        limit_sec = new_state.effective_limit_sec(self.cfg.limit_sec)

        self._apply(
            actions,
            new_state,
            limit_sec,
            persistent_blocked=obs.persistent_blocked,
        )

        statemod.save(self.paths.state, new_state)
        pinmod.save(self.paths.pins, self._pins)

        self.platform.log(
            f"[sample] {'active' if obs.watching else 'idle'} "
            f"used={new_state.seconds_used}s/{limit_sec}s "
            f"blocked={new_state.blocked_by_budget} "
            f"kbps={kbps:.0f} pins={len(self._pins)}"
            f" video={sum(1 for p in self._pins if p.video)}"
        )
        return TickResult(
            state=new_state,
            actions=actions,
            kbps=kbps,
            watching=obs.watching,
            pin_count=len(self._pins),
        )
