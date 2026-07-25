"""Tests for the throughput meter.

The governing rule: an unusable reading is skipped, never counted as zero. Counting a
failed counter read as "no traffic" silently stops the budget while the child keeps
watching -- exactly the failure this project exists to prevent.
"""

from __future__ import annotations

from safehouse_ytbudget.meter import Meter

SAMPLE = 20
WINDOW = 240
MIN_KBPS = 64


def meter(**kw) -> Meter:
    args = dict(window_sec=WINDOW, sample_sec=SAMPLE, min_throughput_kbps=MIN_KBPS)
    args.update(kw)
    return Meter(**args)


def bytes_for(kbps: float, seconds: int) -> int:
    return int(kbps * 1000 / 8 * seconds)


# --- baseline ----------------------------------------------------------------


def test_first_reading_only_establishes_a_baseline():
    m = meter()
    assert m.observe(1_000_000, SAMPLE) is None
    assert m.is_watching() is False


def test_second_reading_yields_throughput():
    m = meter()
    m.observe(0, SAMPLE)
    kbps = m.observe(bytes_for(1000, SAMPLE), SAMPLE)
    assert kbps is not None
    assert round(kbps) == 1000


# --- the watching decision ---------------------------------------------------


def test_sustained_video_counts_as_watching():
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(12):
        total += bytes_for(2000, SAMPLE)
        m.observe(total, SAMPLE)
    assert m.is_watching() is True


def test_idle_does_not_count():
    m = meter()
    m.observe(0, SAMPLE)
    for _ in range(12):
        m.observe(0, SAMPLE)
    assert m.is_watching() is False


def test_a_trickle_below_the_threshold_does_not_count():
    """A background tab polling must not burn budget."""
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(12):
        total += bytes_for(10, SAMPLE)
        m.observe(total, SAMPLE)
    assert m.is_watching() is False


def test_a_brief_pause_does_not_immediately_stop_the_clock():
    """An ad break or buffer top-up produces zero-byte samples. Stopping instantly
    would under-count badly."""
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(12):
        total += bytes_for(2000, SAMPLE)
        m.observe(total, SAMPLE)
    m.observe(total, SAMPLE)  # one idle sample
    assert m.is_watching() is True


def test_the_clock_does_stop_once_the_window_fills_with_idle():
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(12):
        total += bytes_for(2000, SAMPLE)
        m.observe(total, SAMPLE)
    for _ in range(WINDOW // SAMPLE):
        m.observe(total, SAMPLE)
    assert m.is_watching() is False


def test_threshold_is_inclusive():
    m = meter(window_sec=SAMPLE)  # single-sample window
    m.observe(0, SAMPLE)
    m.observe(bytes_for(MIN_KBPS, SAMPLE), SAMPLE)
    assert m.is_watching() is True


# --- unusable readings -------------------------------------------------------


def test_none_reading_is_skipped_not_counted_as_zero():
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(12):
        total += bytes_for(2000, SAMPLE)
        m.observe(total, SAMPLE)
    before = m.average_kbps()
    assert m.observe(None, SAMPLE) is None
    assert m.average_kbps() == before, "a failed read must not dilute the average"


def test_recovery_after_none_does_not_produce_a_phantom_spike():
    """The counter may have restarted while the meter was down; a delta against the
    stale baseline would credit a huge burst of traffic that never happened."""
    m = meter()
    m.observe(10_000_000, SAMPLE)
    m.observe(None, SAMPLE)
    assert m.observe(50, SAMPLE) is None  # re-baselines instead
    assert m.average_kbps() == 0.0


def test_counter_restart_is_ignored():
    m = meter()
    m.observe(10_000_000, SAMPLE)
    assert m.observe(5, SAMPLE) is None  # went backwards
    assert m.average_kbps() == 0.0


def test_zero_elapsed_is_rejected():
    m = meter()
    m.observe(0, SAMPLE)
    assert m.observe(1_000_000, 0) is None


# --- window management -------------------------------------------------------


def test_window_holds_only_its_capacity():
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(100):
        total += bytes_for(1000, SAMPLE)
        m.observe(total, SAMPLE)
    assert len(m._samples) == WINDOW // SAMPLE


def test_window_shorter_than_a_sample_still_holds_one():
    m = meter(window_sec=5, sample_sec=20)
    m.observe(0, 20)
    m.observe(bytes_for(1000, 20), 20)
    assert len(m._samples) == 1


def test_reset_clears_history():
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(12):
        total += bytes_for(2000, SAMPLE)
        m.observe(total, SAMPLE)
    assert m.is_watching()
    m.reset()
    assert m.is_watching() is False
    assert m.observe(total, SAMPLE) is None  # baseline re-established


# --- high-water mark, not a decaying average ---------------------------------


def test_a_dip_after_real_playback_keeps_the_clock_running():
    """REGRESSION: this port originally used the rolling AVERAGE over the window.

    That decays as idle samples accumulate, so a stream that dips - an ad break, a
    buffer top-up, an already-buffered segment - fell under the threshold and stopped
    the clock while playback continued. Compared against the PowerShell watcher on the
    live machine over 45 minutes: 18 samples where it credited and this did not, zero
    the other way. Entirely one-directional under-counting, i.e. the child was being
    handed extra time.
    """
    m = meter()
    m.observe(0, SAMPLE)
    total = bytes_for(5000, SAMPLE)
    m.observe(total, SAMPLE)          # one solid above-threshold sample
    assert m.is_watching() is True

    # Now a long run of near-zero samples, well inside the window. The average would
    # have collapsed; the high-water mark holds.
    for _ in range(WINDOW // SAMPLE - 2):
        total += bytes_for(2, SAMPLE)
        m.observe(total, SAMPLE)
        assert m.is_watching() is True, "must stay active for the whole window"


def test_the_clock_stops_once_the_window_elapses_without_activity():
    m = meter()
    m.observe(0, SAMPLE)
    total = bytes_for(5000, SAMPLE)
    m.observe(total, SAMPLE)
    for _ in range(WINDOW // SAMPLE):
        total += bytes_for(2, SAMPLE)
        m.observe(total, SAMPLE)
    assert m.is_watching() is False


def test_never_active_without_an_above_threshold_sample():
    """A trickle must never start the clock, however long it runs."""
    m = meter()
    total = 0
    m.observe(total, SAMPLE)
    for _ in range(100):
        total += bytes_for(10, SAMPLE)
        m.observe(total, SAMPLE)
    assert m.is_watching() is False


def test_each_active_sample_re_arms_the_window():
    m = meter()
    m.observe(0, SAMPLE)
    total = 0
    for _ in range(30):
        total += bytes_for(5000, SAMPLE)
        m.observe(total, SAMPLE)
    assert m.is_watching() is True


def test_skipped_samples_do_not_advance_the_idle_timer():
    """An unusable reading is UNKNOWN. Letting it age out the window would stop the
    clock on the strength of data we never had."""
    m = meter()
    m.observe(0, SAMPLE)
    m.observe(bytes_for(5000, SAMPLE), SAMPLE)
    for _ in range(WINDOW // SAMPLE + 5):
        m.observe(None, SAMPLE)
    assert m.is_watching() is True
