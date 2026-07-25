"""Parser tests for the Windows platform, run against REAL captured output.

`tests/fixtures/` holds genuine output from the target machine: `wevtutil qe` over the
DNS-Client channel, and `pktmon counters --json`. Parsing hand-written samples would
have missed every quirk that actually matters here -- the `type: 5 ` CNAME prefix, the
IPv4-mapped answers, and the localised pktmon field *values*.

The parsers are pure, so all of this runs on Linux.
"""

from __future__ import annotations

import re
from pathlib import Path

from safehouse_ytbudget.platform.windows import (
    YT_NAME,
    parse_dns_events,
    parse_pktmon_counters,
)

FIXTURES = Path(__file__).parent / "fixtures"
DNS_XML = (FIXTURES / "dns-3008.xml").read_text(encoding="utf-8", errors="replace")
PKTMON_JSON = (FIXTURES / "pktmon-counters.json").read_text(encoding="utf-8", errors="replace")


# --- DNS events --------------------------------------------------------------


def test_extracts_ips_from_real_capture():
    found = parse_dns_events(DNS_XML)
    assert found, "the fixture contains YouTube lookups, so this must not be empty"
    assert all(":" in ip or ip.count(".") == 3 for ip, _ in found)


def test_ignores_non_youtube_lookups():
    """The same capture contains Steam, Epic, Blizzard and Microsoft telemetry. None of
    it may be pinned, or unrelated traffic would burn the budget."""
    only_steam = {ip for ip, _ in parse_dns_events(DNS_XML, re.compile(r"steamserver", re.IGNORECASE))}
    youtube = {ip for ip, _ in parse_dns_events(DNS_XML)}
    assert only_steam.isdisjoint(youtube)


def test_drops_cname_hops():
    """Real answers interleave `type: 5 <name>` CNAME tokens with addresses."""
    assert "type: 5 restrictmoderate.youtube.com" in DNS_XML
    ips = [ip for ip, _ in parse_dns_events(DNS_XML)]
    assert not any("youtube.com" in ip for ip in ips)
    assert not any(ip.startswith("type") for ip in ips)


def test_unmaps_ipv4_mapped_answers():
    xml = (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
        "<System><EventID>3008</EventID></System><EventData>"
        "<Data Name='QueryName'>www.youtube.com</Data>"
        "<Data Name='QueryResults'>::ffff:142.250.1.1;</Data>"
        "</EventData></Event>"
    )
    assert parse_dns_events(xml) == [("142.250.1.1", False)]


def test_deduplicates_across_events():
    ips = [ip for ip, _ in parse_dns_events(DNS_XML)]
    assert len(ips) == len(set(ips))


def test_handles_empty_and_malformed_input():
    """An idle channel returns nothing; a truncated read must not crash the watcher."""
    assert parse_dns_events("") == []
    assert parse_dns_events("   ") == []
    assert parse_dns_events("<Event><broken") == []


def test_empty_query_results_is_skipped():
    """Failed lookups (QueryStatus != 0) carry an empty QueryResults."""
    xml = (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
        "<System><EventID>3008</EventID></System><EventData>"
        "<Data Name='QueryName'>www.youtube.com</Data>"
        "<Data Name='QueryResults'></Data>"
        "</EventData></Event>"
    )
    assert parse_dns_events(xml) == []


def test_name_pattern_covers_the_youtube_family():
    for name in ("www.youtube.com", "i.ytimg.com", "yt3.ggpht.com",
                 "rr1---sn-abc.googlevideo.com"):
        assert YT_NAME.search(name), name
    for name in ("mail.google.com", "drive.google.com", "www.google.com"):
        assert not YT_NAME.search(name), name


# --- pktmon counters ---------------------------------------------------------


def test_parses_real_counters():
    value = parse_pktmon_counters(PKTMON_JSON)
    assert isinstance(value, int)
    assert value > 0


def test_takes_the_max_not_the_sum():
    """pktmon counts the same packet at every layer, so summing multi-counts badly."""
    raw = """[
      {"Group":"nic","Components":[
        {"Name":"a","Counters":[{"Inbound":{"Bytes":100},"Outbound":{"Bytes":9}}]},
        {"Name":"b","Counters":[{"Inbound":{"Bytes":250},"Outbound":{"Bytes":9}}]},
        {"Name":"c","Counters":[{"Inbound":{"Bytes":180},"Outbound":{"Bytes":9}}]}
      ]}
    ]"""
    assert parse_pktmon_counters(raw) == 250


def test_ignores_outbound():
    raw = '[{"Components":[{"Counters":[{"Inbound":{"Bytes":10},"Outbound":{"Bytes":99999}}]}]}]'
    assert parse_pktmon_counters(raw) == 10


def test_session_not_running_returns_none_not_zero():
    """Zero would be indistinguishable from an idle machine and would silently stop the
    budget while the child kept watching."""
    assert parse_pktmon_counters("There is no active session.") is None
    assert parse_pktmon_counters("") is None


def test_malformed_json_returns_none():
    assert parse_pktmon_counters("[{not json") is None


def test_no_counters_present_returns_none():
    assert parse_pktmon_counters("[]") is None
    assert parse_pktmon_counters('[{"Group":"nic","Components":[]}]') is None


def test_zero_bytes_is_a_valid_reading():
    """A running session with no traffic is genuinely zero, and must be distinguishable
    from a missing session."""
    raw = '[{"Components":[{"Counters":[{"Inbound":{"Bytes":0},"Outbound":{"Bytes":0}}]}]}]'
    assert parse_pktmon_counters(raw) == 0


def test_booleans_are_not_accepted_as_byte_counts():
    raw = '[{"Components":[{"Counters":[{"Inbound":{"Bytes":true}}]}]}]'
    assert parse_pktmon_counters(raw) is None


def test_survives_localised_values():
    """The target machine is pt-BR and pktmon localises its values ("Fluxos"), though
    not its keys."""
    assert '"Type":"Fluxos"' in PKTMON_JSON
    assert parse_pktmon_counters(PKTMON_JSON) is not None


# --- video classification ----------------------------------------------------


def _event(name: str, results: str) -> str:
    return (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
        "<System><EventID>3008</EventID></System><EventData>"
        f"<Data Name='QueryName'>{name}</Data>"
        f"<Data Name='QueryResults'>{results}</Data>"
        "</EventData></Event>"
    )


def test_googlevideo_answers_are_flagged_as_video():
    """Only googlevideo carries the actual stream; it must survive the 32-filter cap."""
    out = parse_dns_events(_event("rr3---sn-abc.googlevideo.com", "142.250.1.1;"))
    assert out == [("142.250.1.1", True)]


def test_thumbnail_and_api_answers_are_not_video():
    for name in ("i.ytimg.com", "yt3.ggpht.com", "www.youtube.com",
                 "youtubei.googleapis.com"):
        out = parse_dns_events(_event(name, "142.250.1.1;"))
        assert out == [("142.250.1.1", False)], name


def test_video_flag_wins_when_one_address_serves_both():
    """Losing the flag would push a live video endpoint back to the front of the
    eviction queue the moment a thumbnail resolved to the same address."""
    xml = _event("i.ytimg.com", "142.250.1.1;") + _event("x.googlevideo.com", "142.250.1.1;")
    assert parse_dns_events(xml) == [("142.250.1.1", True)]
    xml_rev = _event("x.googlevideo.com", "142.250.1.1;") + _event("i.ytimg.com", "142.250.1.1;")
    assert parse_dns_events(xml_rev) == [("142.250.1.1", True)]
