"""End-to-end tick tests against the fake platform.

These exercise the real orchestration -- observe, decide, apply, persist -- with only
Windows swapped out, which is the whole point of the platform seam.
"""

from __future__ import annotations

from safehouse_ytbudget.config import Config
from safehouse_ytbudget.platform.base import ReadOnlyPlatform
from safehouse_ytbudget.platform.fake import FakePlatform
from safehouse_ytbudget.state import State
from safehouse_ytbudget.tick import Paths, Ticker
from safehouse_ytbudget import state as statemod

SAMPLE = 20
CFG = Config(limit_min=180, sample_sec=SAMPLE, window_sec=240, min_throughput_kbps=64)


def make(tmp_path, platform=None, cfg=CFG):
    p = platform or FakePlatform()
    return Ticker(platform=p, cfg=cfg, paths=Paths.under(tmp_path)), p


def stream(plat, kbps=2000, seconds=SAMPLE):
    """Advance the fake counter as if `kbps` flowed for `seconds`."""
    plat.bytes_counter += int(kbps * 1000 / 8 * seconds)
    plat.advance(seconds)


# --- metering ----------------------------------------------------------------


def test_idle_machine_never_accrues_time(tmp_path):
    t, plat = make(tmp_path)
    for _ in range(20):
        plat.advance(SAMPLE)
        r = t.run()
    assert r.state.seconds_used == 0
    assert r.watching is False


def test_sustained_streaming_accrues_time(tmp_path):
    t, plat = make(tmp_path)
    plat.resolved = ["142.250.1.1"]
    for _ in range(20):
        stream(plat)
        r = t.run()
    assert r.state.seconds_used > 0
    assert r.watching is True


def test_only_pinned_traffic_is_metered(tmp_path):
    """No YouTube resolution means no pins, so nothing is metered no matter how much
    traffic the machine moves -- Gmail and Drive must never burn the budget."""
    t, plat = make(tmp_path)
    plat.resolved = []
    for _ in range(20):
        stream(plat, kbps=50_000)
        r = t.run()
    assert r.pin_count == 0
    assert r.state.seconds_used == 0


def test_meter_failure_skips_rather_than_zeroing(tmp_path):
    t, plat = make(tmp_path)
    plat.resolved = ["142.250.1.1"]
    for _ in range(20):
        stream(plat)
        t.run()
    used_before = statemod.load(Paths.under(tmp_path).state, today=plat.today())[0].seconds_used

    plat.bytes_counter = None  # meter down
    plat.advance(SAMPLE)
    r = t.run()
    # The sample is skipped, not credited and not counted as idle-with-consequences.
    assert r.state.seconds_used == used_before


# --- enforcement -------------------------------------------------------------


def test_blocks_when_the_budget_runs_out(tmp_path):
    t, plat = make(tmp_path, cfg=Config(limit_min=1, sample_sec=SAMPLE, window_sec=40,
                                        min_throughput_kbps=64))
    plat.resolved = ["142.250.1.1"]
    for _ in range(10):
        stream(plat)
        r = t.run()
    assert r.state.blocked_by_budget is True
    assert plat.hosts_blocked() is True
    assert "flush_dns" in plat.calls


def test_reblocks_when_the_block_is_removed_externally(tmp_path):
    t, plat = make(tmp_path, cfg=Config(limit_min=0, sample_sec=SAMPLE, window_sec=40))
    plat.advance(SAMPLE)
    t.run()
    assert plat.hosts_blocked() is True

    plat._hosts_blocked = False  # something wiped the hosts file
    plat.calls.clear()
    plat.advance(SAMPLE)
    t.run()
    assert plat.hosts_blocked() is True
    assert any("reblock" in m for m in plat.logs)


def test_firewall_follows_the_pin_set(tmp_path):
    t, plat = make(tmp_path, cfg=Config(limit_min=0, sample_sec=SAMPLE, window_sec=40))
    plat.resolved = ["142.250.1.1"]
    stream(plat)
    t.run()
    assert plat.firewall_ips() == ["142.250.1.1"]


def test_no_firewall_churn_when_pins_are_stable(tmp_path):
    t, plat = make(tmp_path, cfg=Config(limit_min=0, sample_sec=SAMPLE, window_sec=40))
    plat.resolved = ["142.250.1.1"]
    stream(plat)
    t.run()
    plat.calls.clear()
    for _ in range(5):
        stream(plat)
        t.run()
    assert not [c for c in plat.calls if c.startswith("set_firewall")]


# --- day reset ---------------------------------------------------------------


def test_new_day_resets_and_unblocks(tmp_path):
    t, plat = make(tmp_path, cfg=Config(limit_min=0, sample_sec=SAMPLE, window_sec=40))
    plat.advance(SAMPLE)
    t.run()
    assert plat.hosts_blocked() is True

    # A fresh day with a real limit: the block must lift.
    t.cfg = Config(limit_min=180, sample_sec=SAMPLE, window_sec=40)
    plat.advance(86400, new_day="2026-07-26")
    r = t.run()
    assert r.state.seconds_used == 0
    assert plat.hosts_blocked() is False
    assert any("[reset]" in m for m in plat.logs)


# --- persistence -------------------------------------------------------------


def test_state_and_pins_survive_a_restart(tmp_path):
    t, plat = make(tmp_path)
    plat.resolved = ["142.250.1.1"]
    for _ in range(20):
        stream(plat)
        t.run()
    used = t.run().state.seconds_used

    # New Ticker, same paths and same platform: a restarted watcher.
    t2 = Ticker(platform=plat, cfg=CFG, paths=Paths.under(tmp_path))
    r = t2.run()
    assert r.state.seconds_used >= used
    assert r.pin_count == 1, "pins must survive so metering resumes immediately"


def test_shadow_paths_do_not_collide_with_the_powershell_watcher(tmp_path):
    """Both watchers run at once during the overlap; they must not share state files."""
    live = Paths.under(tmp_path)
    shadow = Paths.under(tmp_path, suffix="-py")
    assert live.state != shadow.state
    assert live.pins != shadow.pins


# --- shadow mode -------------------------------------------------------------


def test_shadow_mode_decides_but_never_enforces(tmp_path):
    inner = FakePlatform()
    plat = ReadOnlyPlatform(inner)
    t = Ticker(platform=plat, cfg=Config(limit_min=0, sample_sec=SAMPLE, window_sec=40),
               paths=Paths.under(tmp_path, suffix="-py"))
    plat_resolved = ["142.250.1.1"]
    inner.resolved = plat_resolved

    inner.advance(SAMPLE)
    r = t.run()

    # It decided to block...
    assert r.state.blocked_by_budget is True
    assert any("would block hosts" in m for m in inner.logs)
    # ...but the machine is untouched.
    assert inner.hosts_blocked() is False
    assert inner.firewall_ips() == []
    assert "block_hosts" not in inner.calls


def test_shadow_mode_still_accrues_time(tmp_path):
    """Otherwise seconds_used stays zero and the log diff proves nothing.

    It manages this WITHOUT reconfiguring the shared pktmon session: the Ticker tracks
    the filter set it *intended*, and reads whatever counter the existing session
    provides. Both watchers pin by the same rule, so the readings stay comparable.
    """
    inner = FakePlatform()
    plat = ReadOnlyPlatform(inner)
    t = Ticker(platform=plat, cfg=CFG, paths=Paths.under(tmp_path, suffix="-py"))
    inner.resolved = ["142.250.1.1"]
    for _ in range(20):
        stream(inner)
        r = t.run()
    assert r.state.seconds_used > 0
    assert inner.metered == [], "the shared session must be left alone"


def test_shadow_mode_records_what_it_would_have_done(tmp_path):
    inner = FakePlatform()
    plat = ReadOnlyPlatform(inner)
    t = Ticker(platform=plat, cfg=Config(limit_min=0, sample_sec=SAMPLE, window_sec=40),
               paths=Paths.under(tmp_path, suffix="-py"))
    inner.advance(SAMPLE)
    t.run()
    assert [a for a, _ in plat.suppressed if a == "block hosts"]


def test_zero_pins_never_credits_even_under_heavy_traffic(tmp_path):
    """REGRESSION: with no pins the counter was still read and credited, so unrelated
    traffic could burn the YouTube budget. No pinned YouTube IP means, by definition,
    no YouTube."""
    t, plat = make(tmp_path)
    plat.resolved = []
    for _ in range(30):
        stream(plat, kbps=100_000)
        r = t.run()
    assert r.pin_count == 0
    assert r.state.seconds_used == 0
    assert r.watching is False


def test_unusable_reading_skips_the_sample(tmp_path):
    """REGRESSION: is_watching() answered from the rolling window's stale contents, so
    a failed counter read still credited time it could not account for."""
    t, plat = make(tmp_path)
    plat.resolved = ["142.250.1.1"]
    for _ in range(20):
        stream(plat)
        t.run()
    used = t.run().state.seconds_used

    plat.bytes_counter = None
    plat.advance(SAMPLE)
    r = t.run()
    assert r.state.seconds_used == used
    assert r.watching is False


def test_a_genuine_pause_is_still_absorbed_by_the_window(tmp_path):
    """The other side of the coin: a valid zero-byte reading is a pause, not a failure,
    and must not stop the clock immediately."""
    t, plat = make(tmp_path)
    plat.resolved = ["142.250.1.1"]
    for _ in range(20):
        stream(plat)
        t.run()
    plat.advance(SAMPLE)          # counter unchanged: a real, valid zero-byte sample
    r = t.run()
    assert r.watching is True


def test_shadow_mode_never_reconfigures_the_shared_meter(tmp_path):
    """REGRESSION: pktmon is ONE global session for the whole machine. Letting shadow
    mode reconfigure it wiped all 32 of the live PowerShell watcher's filters on the
    real machine, leaving it metering unfiltered traffic until the task was restarted.
    Shadow mode must not be able to degrade the thing it is shadowing."""
    inner = FakePlatform()
    plat = ReadOnlyPlatform(inner)
    t = Ticker(platform=plat, cfg=CFG, paths=Paths.under(tmp_path, suffix="-py"))
    inner.resolved = ["142.250.1.1"]
    for _ in range(5):
        stream(inner)
        t.run()

    assert inner.metered == [], "shadow mode must not touch the shared pktmon session"
    assert not [c for c in inner.calls if c.startswith("set_metered_ips")]
    assert [a for a, _ in plat.suppressed if a == "set metered ips"]


def test_first_tick_warms_up_from_the_pin_expiry_window(tmp_path):
    """A restarted watcher must not be blind. With QUIC coalescing a browser may not
    re-resolve for an hour of continuous watching, so a few seconds of DNS lookback
    leaves it metering nothing."""
    plat = FakePlatform()
    cfg = Config(limit_min=180, sample_sec=20, pin_idle_expire_min=30)
    t = Ticker(platform=plat, cfg=cfg, paths=Paths.under(tmp_path))
    t.run()
    first = [c for c in plat.calls if c.startswith("resolved_youtube_ips")][0]
    since = int(first.split("since=")[1].rstrip(")"))
    assert plat.now_epoch() - since == 30 * 60


def test_later_ticks_only_look_back_to_the_previous_tick(tmp_path):
    plat = FakePlatform()
    t = Ticker(platform=plat, cfg=CFG, paths=Paths.under(tmp_path))
    t.run()
    plat.advance(SAMPLE)
    plat.calls.clear()
    t.run()
    call = [c for c in plat.calls if c.startswith("resolved_youtube_ips")][0]
    since = int(call.split("since=")[1].rstrip(")"))
    assert plat.now_epoch() - since == SAMPLE
