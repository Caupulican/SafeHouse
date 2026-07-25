"""Tests for the pinned-IP set."""

from __future__ import annotations

from safehouse_ytbudget.pins import MAX_PINS, Pin, ips, normalise_ip, update

NOW = 1_800_000_000
IDLE = 1800  # 30 min


def upd(pins, resolved=(), *, now=NOW, active=None, idle=IDLE):
    return update(pins, list(resolved), now=now, active_ips=active, idle_expire_sec=idle)


# --- address normalisation ---------------------------------------------------


def test_unmaps_ipv4_mapped_ipv6():
    """The DNS channel reports IPv4 answers both ways; pinning one address twice would
    waste two of the 32 filter slots on a single endpoint."""
    assert normalise_ip("::ffff:142.250.1.1") == "142.250.1.1"


def test_rejects_non_addresses():
    assert normalise_ip("www-youtube-ui.l.google.com") is None  # a CNAME
    assert normalise_ip("") is None
    assert normalise_ip("   ") is None


def test_accepts_ipv6():
    assert normalise_ip("2800:3f0:4001:807::2016") == "2800:3f0:4001:807::2016"


def test_mapped_and_bare_form_collapse_to_one_pin():
    pins, added, _ = upd([], ["142.250.1.1", "::ffff:142.250.1.1"])
    assert ips(pins) == ["142.250.1.1"]
    assert len(added) == 1


# --- adding ------------------------------------------------------------------


def test_adds_new_resolutions():
    pins, added, evicted = upd([], ["1.1.1.1", "2.2.2.2"])
    assert ips(pins) == ["1.1.1.1", "2.2.2.2"]
    assert len(added) == 2
    assert evicted == []


def test_cnames_are_skipped():
    pins, added, _ = upd([], ["some.cname.google.com", "1.1.1.1"])
    assert ips(pins) == ["1.1.1.1"]


def test_re_resolving_does_not_duplicate_and_refreshes_activity():
    pins, _, _ = upd([], ["1.1.1.1"])
    later = NOW + 600
    pins, added, _ = upd(pins, ["1.1.1.1"], now=later)
    assert len(pins) == 1
    assert added == []
    assert pins[0].last_active == later
    assert pins[0].first_seen == NOW  # first_seen is not disturbed


# --- activity keeps a pin alive ---------------------------------------------


def test_traffic_keeps_a_pin_alive_without_any_dns():
    """The QUIC-coalescing case: a stream runs for an hour with no fresh lookups.
    Expiring on DNS recency would stop the budget mid-video."""
    pins, _, _ = upd([], ["1.1.1.1"])
    now = NOW
    for _ in range(10):  # 10 * 20 min, far beyond the 30 min idle expiry
        now += 1200
        pins, _, evicted = upd(pins, [], now=now, active={"1.1.1.1"})
        assert evicted == []
    assert ips(pins) == ["1.1.1.1"]


def test_idle_pin_expires():
    pins, _, _ = upd([], ["1.1.1.1"])
    pins, _, evicted = upd(pins, [], now=NOW + IDLE)
    assert pins == []
    assert [p.ip for p in evicted] == ["1.1.1.1"]


def test_expiry_is_exactly_at_the_threshold():
    pins, _, _ = upd([], ["1.1.1.1"])
    survived, _, _ = upd(pins, [], now=NOW + IDLE - 1)
    assert len(survived) == 1
    gone, _, _ = upd(pins, [], now=NOW + IDLE)
    assert gone == []


def test_active_pin_survives_while_an_idle_one_expires():
    pins, _, _ = upd([], ["1.1.1.1", "2.2.2.2"])
    pins, _, evicted = upd(pins, [], now=NOW + IDLE, active={"1.1.1.1"})
    assert ips(pins) == ["1.1.1.1"]
    assert [p.ip for p in evicted] == ["2.2.2.2"]


# --- the 32-filter cap -------------------------------------------------------


def test_cap_is_enforced():
    pins, _, _ = upd([], [f"10.0.0.{i}" for i in range(1, 50)])
    assert len(pins) == MAX_PINS


def test_cap_evicts_least_recently_active_first():
    """A live stream must never be displaced by an address that merely resolved."""
    old = [Pin(ip=f"10.0.0.{i}", first_seen=NOW, last_active=NOW) for i in range(MAX_PINS)]
    streaming = "10.0.0.0"
    later = NOW + 60
    pins, _, evicted = upd(old, ["9.9.9.9"], now=later, active={streaming})
    assert len(pins) == MAX_PINS
    assert streaming in ips(pins)
    assert "9.9.9.9" in ips(pins)
    assert len(evicted) == 1
    assert evicted[0].ip != streaming


def test_added_excludes_pins_immediately_evicted_by_the_cap():
    """Otherwise the log would announce a pin that was never actually metered."""
    old = [Pin(ip=f"10.0.0.{i}", first_seen=NOW, last_active=NOW + 100) for i in range(MAX_PINS)]
    pins, added, _ = upd(old, ["9.9.9.9"], now=NOW + 100)
    assert len(pins) == MAX_PINS
    assert "9.9.9.9" not in ips(pins)
    assert added == []


# --- stability ---------------------------------------------------------------


def test_output_is_sorted_for_stable_meter_configuration():
    pins, _, _ = upd([], ["3.3.3.3", "1.1.1.1", "2.2.2.2"])
    assert ips(pins) == sorted(ips(pins))


def test_quiet_tick_changes_nothing():
    pins, _, _ = upd([], ["1.1.1.1"])
    again, added, evicted = upd(pins, [], now=NOW + 1, active={"1.1.1.1"})
    assert ips(again) == ips(pins)
    assert added == [] and evicted == []
