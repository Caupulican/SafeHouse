"""Tests for the reconcile logic.

Most of these are regression tests for defects the PowerShell version actually shipped
with. The name of each says which one.
"""

from __future__ import annotations

import pytest

from safehouse_ytbudget.config import Config
from safehouse_ytbudget.decide import (
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
from safehouse_ytbudget.state import State

TODAY = "2026-07-25"
CFG = Config(limit_min=180, sample_sec=20)  # 3h = 10800s


def obs(**kw) -> Observation:
    base = dict(today=TODAY, watching=False, hosts_blocked=False)
    base.update(kw)
    return Observation(**base)


def kinds(actions) -> list[type]:
    return [type(a) for a in actions]


# --- crediting ---------------------------------------------------------------


def test_watching_credits_one_sample():
    st, acts = decide(State(date=TODAY), CFG, obs(watching=True))
    assert st.seconds_used == 20
    assert CreditTime(seconds=20) in acts


def test_not_watching_credits_nothing():
    st, acts = decide(State(date=TODAY, seconds_used=100), CFG, obs(watching=False))
    assert st.seconds_used == 100
    assert not any(isinstance(a, CreditTime) for a in acts)


# --- the latching bug --------------------------------------------------------


def test_blocks_when_budget_crossed():
    st, acts = decide(State(date=TODAY, seconds_used=10800), CFG, obs())
    assert st.blocked_by_budget is True
    assert BlockHosts(reason="enforce") in acts
    assert FlushDns() in acts


def test_reblocks_when_block_removed_externally():
    """REGRESSION: enforcement used to fire only on the false->true transition.

    Once `blocked_by_budget` was true, nothing re-verified the hosts file, so a block
    removed by anything else stayed removed while the tool still reported "blocked".
    """
    state = State(date=TODAY, seconds_used=10800, blocked_by_budget=True)
    st, acts = decide(state, CFG, obs(hosts_blocked=False))
    assert BlockHosts(reason="reblock") in acts
    assert st.blocked_by_budget is True


def test_no_redundant_block_when_already_blocked():
    state = State(date=TODAY, seconds_used=10800, blocked_by_budget=True)
    _, acts = decide(state, CFG, obs(hosts_blocked=True))
    assert not any(isinstance(a, BlockHosts) for a in acts)
    assert not any(isinstance(a, FlushDns) for a in acts)


def test_unblocks_when_back_under_budget():
    """The other half of the reconcile -- e.g. after the limit is raised."""
    state = State(date=TODAY, seconds_used=3600, blocked_by_budget=True)
    st, acts = decide(state, CFG, obs(hosts_blocked=True))
    assert UnblockHosts(reason="under-budget") in acts
    assert ClearFirewall() in acts
    assert st.blocked_by_budget is False


# --- manual block vs budget block --------------------------------------------


def test_manual_block_survives_being_under_budget():
    """REGRESSION: a declarative reconcile must not lift a deliberate parent block."""
    state = State(date=TODAY, seconds_used=0, manual_block=True)
    st, acts = decide(state, CFG, obs(hosts_blocked=True))
    assert not any(isinstance(a, UnblockHosts) for a in acts)
    assert st.manual_block is True


def test_manual_block_is_reapplied_if_removed():
    state = State(date=TODAY, seconds_used=0, manual_block=True)
    _, acts = decide(state, CFG, obs(hosts_blocked=False))
    assert BlockHosts(reason="reblock") in acts


def test_manual_block_does_not_set_blocked_by_budget():
    """Otherwise `allow` would look like a budget block and get re-applied."""
    state = State(date=TODAY, seconds_used=0, manual_block=True)
    st, _ = decide(state, CFG, obs(hosts_blocked=True))
    assert st.blocked_by_budget is False


# --- day reset ---------------------------------------------------------------


def test_day_reset_zeroes_usage_and_unblocks():
    state = State(
        date="2026-07-24", seconds_used=10800, bonus_sec=600, blocked_by_budget=True
    )
    st, acts = decide(state, CFG, obs(hosts_blocked=True))
    assert ResetDay(previous="2026-07-24", today=TODAY) in acts
    assert UnblockHosts(reason="day-reset") in acts
    assert st.seconds_used == 0
    assert st.bonus_sec == 0
    assert st.blocked_by_budget is False
    assert st.date == TODAY


def test_day_reset_is_idempotent_across_multiple_days_off():
    """Date comparison, not a midnight timer: three days off still resets once."""
    state = State(date="2026-07-22", seconds_used=9999, blocked_by_budget=True)
    st, acts = decide(state, CFG, obs())
    assert len([a for a in acts if isinstance(a, ResetDay)]) == 1
    assert st.date == TODAY
    # A second tick on the same day must not reset again.
    _, acts2 = decide(st, CFG, obs())
    assert not any(isinstance(a, ResetDay) for a in acts2)


def test_day_reset_with_zero_limit_keeps_the_block():
    """A zero limit must not open a gap at midnight.

    The reset zeroes usage, but 0 >= 0 is still over budget, so the existing block is
    simply retained -- no unblock/reblock churn, and no window where YouTube is
    reachable. An earlier version emitted the unblock during the reset and then
    evaluated a stale observation, leaving it unblocked until the next tick.
    """
    state = State(date="2026-07-24", seconds_used=500, blocked_by_budget=True)
    st, acts = decide(state, Config(limit_min=0, sample_sec=20), obs(hosts_blocked=True))
    assert st.seconds_used == 0
    assert st.blocked_by_budget is True
    assert not any(isinstance(a, UnblockHosts) for a in acts)
    assert not any(isinstance(a, BlockHosts) for a in acts)


def test_day_reset_with_zero_limit_blocks_if_hosts_were_clear():
    """Same reset, but the block was absent: it must be applied in this same tick."""
    state = State(date="2026-07-24", seconds_used=500, blocked_by_budget=True)
    st, acts = decide(state, Config(limit_min=0, sample_sec=20), obs(hosts_blocked=False))
    assert st.blocked_by_budget is True
    assert BlockHosts(reason="enforce") in acts


# --- the frozen-limit bug ----------------------------------------------------


def test_limit_comes_from_config_not_state():
    """REGRESSION: `limit_min` used to live in the state file, was written once, and
    never refreshed -- so editing .env/config.json silently changed nothing."""
    state = State(date=TODAY, seconds_used=7200)  # 2h used
    # Under a 3h limit: allowed.
    st, _ = decide(state, Config(limit_min=180, sample_sec=20), obs())
    assert st.blocked_by_budget is False
    # Same state, 1h limit: blocked. Nothing in State had to change.
    st, acts = decide(state, Config(limit_min=60, sample_sec=20), obs())
    assert st.blocked_by_budget is True
    assert BlockHosts(reason="enforce") in acts


def test_bonus_extends_the_limit():
    state = State(date=TODAY, seconds_used=10800, bonus_sec=600)
    st, acts = decide(state, CFG, obs())
    assert st.blocked_by_budget is False
    assert not any(isinstance(a, BlockHosts) for a in acts)


# --- firewall ----------------------------------------------------------------


def test_firewall_set_when_over_budget_with_pins():
    state = State(date=TODAY, seconds_used=10800)
    _, acts = decide(state, CFG, obs(pinned_ips=("2.2.2.2", "1.1.1.1")))
    assert SetFirewall(ips=("1.1.1.1", "2.2.2.2")) in acts  # sorted


def test_no_firewall_action_without_pins():
    """REGRESSION: this logged a NOIPS no-op on every single sample."""
    state = State(date=TODAY, seconds_used=10800)
    _, acts = decide(state, CFG, obs(pinned_ips=()))
    assert not any(isinstance(a, SetFirewall) for a in acts)


def test_firewall_not_rewritten_when_pins_unchanged():
    state = State(date=TODAY, seconds_used=10800, blocked_by_budget=True)
    o = obs(hosts_blocked=True, pinned_ips=("1.1.1.1",), firewall_ips=("1.1.1.1",))
    _, acts = decide(state, CFG, o)
    assert not any(isinstance(a, SetFirewall) for a in acts)


def test_firewall_refreshed_when_pins_move():
    state = State(date=TODAY, seconds_used=10800, blocked_by_budget=True)
    o = obs(hosts_blocked=True, pinned_ips=("1.1.1.1", "3.3.3.3"), firewall_ips=("1.1.1.1",))
    _, acts = decide(state, CFG, o)
    assert SetFirewall(ips=("1.1.1.1", "3.3.3.3")) in acts


def test_stale_firewall_cleared_when_not_blocked():
    state = State(date=TODAY, seconds_used=0)
    _, acts = decide(state, CFG, obs(hosts_blocked=False, firewall_ips=("1.1.1.1",)))
    assert ClearFirewall() in acts


# --- purity ------------------------------------------------------------------


def test_decide_does_not_mutate_input_state():
    state = State(date="2026-07-24", seconds_used=500, blocked_by_budget=True)
    before = (state.date, state.seconds_used, state.blocked_by_budget)
    decide(state, CFG, obs(watching=True))
    assert (state.date, state.seconds_used, state.blocked_by_budget) == before


@pytest.mark.parametrize("used,expected", [(10799, False), (10800, True), (10801, True)])
def test_limit_boundary_is_inclusive(used, expected):
    st, _ = decide(State(date=TODAY, seconds_used=used), CFG, obs())
    assert st.blocked_by_budget is expected
