"""Tests for the parent-facing control CLI, driven against the fake platform."""

from __future__ import annotations

from safehouse_ytbudget import config as configmod
from safehouse_ytbudget import state as statemod
from safehouse_ytbudget.cli import format_hms, run
from safehouse_ytbudget.config import Config
from safehouse_ytbudget.platform.fake import FakePlatform
from safehouse_ytbudget.state import State
from safehouse_ytbudget.tick import Paths

TODAY = "2026-07-25"


def setup(tmp_path, *, limit_min=180, **state_kw):
    configmod.write(tmp_path / "config.json", Config(limit_min=limit_min))
    paths = Paths.under(tmp_path)
    if state_kw:
        statemod.save(paths.state, State(date=TODAY, **state_kw))
    return FakePlatform(today=TODAY), paths


def call(cmd, value, plat, tmp_path):
    return run(cmd, value, plat, root=tmp_path)


# --- formatting --------------------------------------------------------------


def test_format_hms():
    assert format_hms(0) == "0m00s"
    assert format_hms(90) == "1m30s"
    assert format_hms(3661) == "1h01m01s"
    assert format_hms(-5) == "0m00s"


# --- status ------------------------------------------------------------------


def test_status_reports_usage_and_remaining(tmp_path):
    plat, _ = setup(tmp_path, seconds_used=3600)
    code, lines = call("status", None, plat, tmp_path)
    text = "\n".join(lines)
    assert code == 0
    assert "1h00m00s" in text          # used
    assert "3h00m00s" in text          # limit
    assert "2h00m00s" in text          # remaining
    assert "ALLOWED" in text


def test_status_shows_blocked(tmp_path):
    plat, _ = setup(tmp_path, seconds_used=10800, blocked_by_budget=True)
    _, lines = call("status", None, plat, tmp_path)
    assert "BLOCKED (budget)" in "\n".join(lines)


def test_status_does_not_mutate(tmp_path):
    plat, paths = setup(tmp_path, seconds_used=1234)
    call("status", None, plat, tmp_path)
    st, _ = statemod.load(paths.state, today=TODAY)
    assert st.seconds_used == 1234
    assert plat.calls == [] or all("block" not in c for c in plat.calls)


def test_stale_state_from_yesterday_is_not_reported(tmp_path):
    """Otherwise `status` shows yesterday's usage until the watcher next ticks."""
    configmod.write(tmp_path / "config.json", Config(limit_min=180))
    paths = Paths.under(tmp_path)
    statemod.save(paths.state, State(date="2026-07-24", seconds_used=9999))
    plat = FakePlatform(today=TODAY)
    _, lines = call("status", None, plat, tmp_path)
    assert "0m00s" in "\n".join(lines)


# --- set-limit ---------------------------------------------------------------


def test_set_limit_writes_config(tmp_path):
    plat, _ = setup(tmp_path)
    code, _ = call("set-limit", 90, plat, tmp_path)
    assert code == 0
    cfg, _ = configmod.load(tmp_path / "config.json")
    assert cfg.limit_min == 90


def test_set_limit_unblocks_when_it_frees_budget(tmp_path):
    plat, paths = setup(tmp_path, limit_min=60, seconds_used=3600, blocked_by_budget=True)
    plat._hosts_blocked = True
    code, lines = call("set-limit", 180, plat, tmp_path)
    assert code == 0
    assert plat.hosts_blocked() is False
    assert "unblocked" in "\n".join(lines).lower()
    st, _ = statemod.load(paths.state, today=TODAY)
    assert st.blocked_by_budget is False


def test_set_limit_does_not_unblock_when_still_over(tmp_path):
    plat, _ = setup(tmp_path, limit_min=180, seconds_used=10800, blocked_by_budget=True)
    plat._hosts_blocked = True
    call("set-limit", 100, plat, tmp_path)
    assert plat.hosts_blocked() is True


def test_set_limit_requires_a_value(tmp_path):
    plat, _ = setup(tmp_path)
    code, lines = call("set-limit", None, plat, tmp_path)
    assert code == 2
    assert "needs a whole number" in "\n".join(lines)


def test_set_limit_rejects_negative(tmp_path):
    plat, _ = setup(tmp_path)
    code, _ = call("set-limit", -10, plat, tmp_path)
    assert code == 2
    cfg, _ = configmod.load(tmp_path / "config.json")
    assert cfg.limit_min == 180  # unchanged


def test_set_limit_zero_is_allowed(tmp_path):
    """A legitimate 'no YouTube today'."""
    plat, _ = setup(tmp_path)
    code, _ = call("set-limit", 0, plat, tmp_path)
    assert code == 0
    cfg, _ = configmod.load(tmp_path / "config.json")
    assert cfg.limit_min == 0


# --- grant -------------------------------------------------------------------


def test_grant_adds_bonus(tmp_path):
    plat, paths = setup(tmp_path, seconds_used=100)
    call("grant", 30, plat, tmp_path)
    st, _ = statemod.load(paths.state, today=TODAY)
    assert st.bonus_sec == 1800


def test_grant_unblocks_when_it_frees_budget(tmp_path):
    plat, _ = setup(tmp_path, limit_min=60, seconds_used=3600, blocked_by_budget=True)
    plat._hosts_blocked = True
    _, lines = call("grant", 30, plat, tmp_path)
    assert plat.hosts_blocked() is False
    assert "unblocked" in "\n".join(lines).lower()


def test_grant_accumulates(tmp_path):
    plat, paths = setup(tmp_path)
    call("grant", 10, plat, tmp_path)
    call("grant", 20, plat, tmp_path)
    st, _ = statemod.load(paths.state, today=TODAY)
    assert st.bonus_sec == 1800


def test_grant_cannot_drive_bonus_negative(tmp_path):
    plat, paths = setup(tmp_path)
    call("grant", -99, plat, tmp_path)
    st, _ = statemod.load(paths.state, today=TODAY)
    assert st.bonus_sec == 0


# --- block / allow -----------------------------------------------------------


def test_block_applies_and_marks_manual(tmp_path):
    plat, paths = setup(tmp_path)
    code, lines = call("block", None, plat, tmp_path)
    assert code == 0
    assert plat.hosts_blocked() is True
    st, _ = statemod.load(paths.state, today=TODAY)
    assert st.manual_block is True, "so the watcher's reconcile will not lift it"


def test_allow_clears_manual_hold(tmp_path):
    plat, paths = setup(tmp_path, manual_block=True)
    plat._hosts_blocked = True
    code, _ = call("allow", None, plat, tmp_path)
    assert code == 0
    assert plat.hosts_blocked() is False
    st, _ = statemod.load(paths.state, today=TODAY)
    assert st.manual_block is False


def test_allow_warns_when_the_watcher_will_immediately_reblock(tmp_path):
    """Enforcement reconciles every tick, so over budget `allow` does not stick. Saying
    so beats letting the parent believe it worked."""
    plat, _ = setup(tmp_path, limit_min=60, seconds_used=7200, blocked_by_budget=True)
    plat._hosts_blocked = True
    _, lines = call("allow", None, plat, tmp_path)
    text = "\n".join(lines)
    assert "re-block" in text
    assert "grant" in text


def test_allow_does_not_warn_when_under_budget(tmp_path):
    plat, _ = setup(tmp_path, seconds_used=60, manual_block=True)
    plat._hosts_blocked = True
    _, lines = call("allow", None, plat, tmp_path)
    assert "re-block" not in "\n".join(lines)


# --- reset -------------------------------------------------------------------


def test_reset_zeroes_everything_and_unblocks(tmp_path):
    plat, paths = setup(
        tmp_path, seconds_used=9999, bonus_sec=600, blocked_by_budget=True, manual_block=True
    )
    plat._hosts_blocked = True
    code, _ = call("reset", None, plat, tmp_path)
    assert code == 0
    st, _ = statemod.load(paths.state, today=TODAY)
    assert (st.seconds_used, st.bonus_sec) == (0, 0)
    assert st.blocked_by_budget is False and st.manual_block is False
    assert plat.hosts_blocked() is False


# --- misc --------------------------------------------------------------------


def test_unknown_command(tmp_path):
    plat, _ = setup(tmp_path)
    code, _ = call("frobnicate", None, plat, tmp_path)
    assert code == 2


def test_commands_survive_a_missing_state_file(tmp_path):
    configmod.write(tmp_path / "config.json", Config())
    plat = FakePlatform(today=TODAY)
    for cmd in ("status", "grant", "block", "allow", "reset"):
        code, _ = call(cmd, 5 if cmd == "grant" else None, plat, tmp_path)
        assert code == 0, cmd


# --- the resolver lane is visible in status ----------------------------------
# A parent reading "BLOCKED" while the Play Games VM streamed is the whole reason this
# component exists. Status has to name the lane, especially when it is missing.


def test_status_reports_a_blocking_resolver(tmp_path):
    plat = FakePlatform(hosts_blocked=True, resolver=True)
    _, lines = run("status", None, plat, root=tmp_path)
    assert any("AdGuard Home blocking YouTube" in ln for ln in lines)


def test_status_reports_an_armed_resolver_that_is_currently_allowing(tmp_path):
    plat = FakePlatform(hosts_blocked=True, resolver=False)
    _, lines = run("status", None, plat, root=tmp_path)
    assert any("ARMED and allowing YouTube" in ln for ln in lines)
    assert not any("coverage is incomplete" in ln for ln in lines)


def test_status_says_loudly_when_the_lane_was_never_armed(tmp_path):
    plat = FakePlatform(hosts_blocked=True)          # resolver=None -> disarmed
    _, lines = run("status", None, plat, root=tmp_path)
    assert any("NOT ARMED" in ln for ln in lines)
    assert any("Play Games" in ln for ln in lines)


def test_manual_block_drives_the_resolver_too(tmp_path):
    """`ctl block` must not be a hosts-only half measure."""
    plat = FakePlatform(hosts_blocked=False, resolver=False)
    run("block", None, plat, root=tmp_path)
    assert plat.resolver_blocked() is True
