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
