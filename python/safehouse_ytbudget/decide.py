"""The decision function: given a snapshot, what should change?

This module is pure. No clock, no filesystem, no subprocess -- `decide()` takes an
observation and returns the next state plus a list of actions for the caller to apply.
That is the whole point of the port: in the PowerShell version this logic was welded to
the Windows calls, so the only way to test enforcement was to enforce, and every bug in
it was found by reading code rather than by a test.

The two rules that matter, both learned from real failures:

1. **Reconcile, never latch.** The original bug fired enforcement only on the
   false->true transition of `blocked_by_budget`. Once latched, nothing re-verified the
   block, so anything that removed it went unnoticed indefinitely -- the tool reported
   "blocked" while YouTube played. Every tick now compares desired state to observed
   state and corrects the difference, in both directions.

2. **A manual block is not a budget block.** `ctl block` must survive a tick where usage
   happens to sit under the limit, so the desired-state calculation ORs the two reasons
   and only the budget half is cleared automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .config import Config
from .state import State


@dataclass(frozen=True)
class Observation:
    """Everything the outside world tells one tick. Assembled by the platform layer."""

    # Local calendar date, "YYYY-MM-DD".
    today: str
    # This sample looked like video playback (sustained throughput from pinned IPs).
    watching: bool
    # The YouTube block is currently present in the hosts file.
    hosts_blocked: bool
    # IPs currently pinned and being metered.
    pinned_ips: tuple[str, ...] = ()
    # IPs currently covered by the endpoint firewall rules.
    firewall_ips: tuple[str, ...] = ()
    # The resolver (AdGuard Home) is currently refusing YouTube, or None when that lane
    # is disarmed or unreachable and therefore cannot be judged. None makes the layer
    # invisible to the reconcile below, which is deliberate: a lane we cannot read is
    # also a lane we cannot fix, and retrying it every tick would only spam the log.
    resolver_blocked: bool | None = None


# --- actions -----------------------------------------------------------------
# Small frozen records rather than an enum + payload, so a test asserts on exactly
# what would have happened, and the applying layer can pattern-match.


@dataclass(frozen=True)
class ResetDay:
    previous: str
    today: str


@dataclass(frozen=True)
class BlockHosts:
    # "enforce" = budget just crossed; "reblock" = it was already meant to be blocked
    # and something removed it; "manual" = a parent forced it on.
    reason: str


@dataclass(frozen=True)
class UnblockHosts:
    reason: str


@dataclass(frozen=True)
class SetFirewall:
    ips: tuple[str, ...]


@dataclass(frozen=True)
class ClearFirewall:
    pass


@dataclass(frozen=True)
class FlushDns:
    pass


@dataclass(frozen=True)
class CreditTime:
    seconds: int


Action = ResetDay | BlockHosts | UnblockHosts | SetFirewall | ClearFirewall | FlushDns | CreditTime


def decide(state: State, cfg: Config, obs: Observation) -> tuple[State, list[Action]]:
    """Return the state after this tick and the actions needed to realise it."""
    actions: list[Action] = []
    new = replace(state, extra=dict(state.extra))

    # 1) Date-based idempotent reset. Comparing dates rather than watching for midnight
    #    means a machine that was off for three days still resets exactly once.
    #    This step touches STATE ONLY. It deliberately emits no hosts/firewall actions:
    #    the single reconcile in step 3 owns those, using the state as it stands after
    #    the reset. Doing it here as well meant unblocking and then reading a
    #    now-stale `obs.hosts_blocked`, so a zero limit unblocked and never re-blocked.
    did_reset = new.date != obs.today
    if did_reset:
        actions.append(ResetDay(previous=new.date, today=obs.today))
        new.date = obs.today
        new.seconds_used = 0
        new.bonus_sec = 0
        new.blocked_by_budget = False
        # A manual block is cleared too: a block a parent applied yesterday should not
        # silently outlive the day.
        new.manual_block = False

    # 2) Credit this sample. Time is credited in whole samples, so the budget is
    #    accurate to +/- one sample_sec.
    if obs.watching:
        new.seconds_used += cfg.sample_sec
        actions.append(CreditTime(seconds=cfg.sample_sec))

    # 3) Reconcile: compute the desired world, then emit only the differences.
    #    Exactly one hosts action per tick, and none at all when nothing needs to change.
    limit = new.effective_limit_sec(cfg.limit_sec)
    over_budget = new.seconds_used >= limit
    want_blocked = over_budget or new.manual_block

    # Enforcement runs on more than one layer, and they drift independently -- the
    # resolver can be restarted, or edited by hand in its dashboard, while the hosts file
    # sits untouched. So the two questions are asked with opposite strictness:
    #
    #   to stay blocked  -> EVERY visible layer must be blocking (all)
    #   to stay allowed  -> NO layer may be blocking          (any)
    #
    # Reading a single OR'd boolean for both, as this did originally, hides exactly the
    # failures that matter. If the resolver was unreachable when the budget ran out, the
    # hosts file alone answered "blocked" and the resolver was never retried for the rest
    # of the day -- which is precisely how the Play Games VM kept streaming while every
    # log line said BLOCKED. Both directions are idempotent per layer, so re-applying
    # costs an "ALREADY" and converges.
    layers = [obs.hosts_blocked]
    if obs.resolver_blocked is not None:
        layers.append(obs.resolver_blocked)
    fully_blocked = all(layers)
    any_blocked = any(layers)

    if want_blocked:
        if not fully_blocked:
            # "reblock" means it was supposed to be there already and something removed
            # it -- worth flagging loudly in the log. After a reset there is no such
            # expectation, so a fresh day crossing the limit is a plain "enforce".
            expected_block = state.blocked_by_budget or state.manual_block
            reason = "reblock" if (expected_block and not did_reset) else "enforce"
            # Note this fires when a layer is *partially* applied too, e.g. hosts is
            # blocking but the resolver came back up clear. That is a reblock, not a
            # fresh enforce, and the log should say so.
            actions.append(BlockHosts(reason=reason))
            actions.append(FlushDns())
        new.blocked_by_budget = over_budget

        # Endpoint firewall: only meaningful with pins to block. Re-apply when it is
        # missing or the pin set has moved on. Skipping the empty case keeps a quiet
        # machine from logging a no-op every single sample.
        if obs.pinned_ips and tuple(sorted(obs.pinned_ips)) != tuple(sorted(obs.firewall_ips)):
            actions.append(SetFirewall(ips=tuple(sorted(obs.pinned_ips))))

    else:
        if any_blocked:
            # Removing only SafeHouse's own auto-block marker is the platform layer's
            # job; a block applied by hand with `safehouse_setup toggle` carries a different
            # marker and is left alone there.
            actions.append(UnblockHosts(reason="day-reset" if did_reset else "under-budget"))
            actions.append(ClearFirewall())
            actions.append(FlushDns())
        elif obs.firewall_ips:
            # Hosts already clear but stale firewall rules linger -- e.g. the block was
            # lifted out of band. Leaving them would keep YouTube broken with nothing
            # in the state to explain why.
            actions.append(ClearFirewall())
        new.blocked_by_budget = False

    return new, actions
