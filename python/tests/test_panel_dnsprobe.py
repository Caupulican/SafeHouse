"""The verification probes, and the mistake they exist to stop being made a third time.

AdGuard Home answers a parental-control block with `94.140.14.35` - its own block-page
sinkhole, a perfectly routable address. Judging "is this domain blocked" by the address
that came back therefore reports a *working block as a leak*. It was done twice on this
machine in one day. The verdict comes from the query log's `reason` field, and the test
below pins that with the real address in it.
"""

from __future__ import annotations

import socket
import struct
import threading

from safehouse_panel import dnsprobe
from safehouse_panel.i18n import render

ADGUARD_BLOCK_PAGE = "94.140.14.35"


def entry(name, reason, *, rule="", rules=None):
    out = {"question": {"name": name, "type": "A"}, "reason": reason,
           "time": "2026-08-21T20:00:00Z"}
    if rule:
        out["rule"] = rule
    if rules is not None:
        out["rules"] = rules
    return out


# --- the wire ----------------------------------------------------------------

def test_a_query_packet_carries_the_name_and_asks_for_recursion():
    packet = dnsprobe.build_query("www.youtube.com", txid=0x1234)
    txid, flags, qdcount = struct.unpack("!HHH", packet[:6])
    assert (txid, flags, qdcount) == (0x1234, 0x0100, 1)
    assert b"\x03www\x07youtube\x03com\x00" in packet


def test_an_answer_is_parsed_back_to_its_addresses():
    """Built by hand from the wire format, so this exercises the real parser."""
    header = struct.pack("!HHHHHH", 1, 0x8180, 1, 1, 0, 0)
    question = b"\x07example\x03com\x00" + struct.pack("!HH", 1, 1)
    answer = (b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([93, 184, 216, 34]))
    parsed = dnsprobe.parse_response(header + question + answer)
    assert parsed == {"rcode": 0, "addresses": ["93.184.216.34"]}


def test_a_truncated_packet_does_not_raise():
    assert dnsprobe.parse_response(b"\x00\x01") == {"rcode": None, "addresses": []}
    assert dnsprobe.parse_response(b"")["addresses"] == []


# --- DNS over TCP ------------------------------------------------------------
#
# The Pi-hole check MUST use TCP: the panel runs on Windows and WSL2 forwards localhost
# TCP only into the VM, so a UDP probe to 127.0.0.1:5353 times out against a Pi-hole
# that is up and answering. That was measured live on 2026-08-22 - container healthy,
# TCP connect fine, UDP probe "no answer (timeout)".

def a_dns_answer(address=(93, 184, 216, 34)) -> bytes:
    header = struct.pack("!HHHHHH", 1, 0x8180, 1, 1, 0, 0)
    question = b"\x07example\x03com\x00" + struct.pack("!HH", 1, 1)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + bytes(address)
    return header + question + answer


def tcp_dns_server(response: bytes | None) -> tuple[int, dict]:
    """One-shot fake resolver speaking RFC 1035 4.2.2 framing. Returns (port, seen)."""
    seen: dict = {}
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def run():
        conn, _ = server.accept()
        try:
            prefix = conn.recv(2)
            length = struct.unpack("!H", prefix)[0]
            query = b""
            while len(query) < length:
                query += conn.recv(length - len(query))
            seen["prefix"], seen["query"] = prefix, query
            if response is not None:
                conn.sendall(struct.pack("!H", len(response)) + response)
        finally:
            conn.close()
            server.close()

    threading.Thread(target=run, daemon=True).start()
    return port, seen


def test_a_tcp_query_is_length_prefixed_and_the_answer_parsed_back():
    port, seen = tcp_dns_server(a_dns_answer())
    result = dnsprobe.ask_tcp("example.com", server="127.0.0.1", port=port, timeout=3.0)
    assert result["answered"] is True and result["error"] is None
    assert result["addresses"] == ["93.184.216.34"] and result["rcode"] == 0
    # The query itself is the same packet `ask` sends, behind a 2-byte length prefix.
    assert struct.unpack("!H", seen["prefix"])[0] == len(seen["query"])
    assert b"\x07example\x03com\x00" in seen["query"]


def test_a_refused_tcp_connection_is_no_answer_not_an_exception():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there any more
    result = dnsprobe.ask_tcp("example.com", server="127.0.0.1", port=port, timeout=1.0)
    assert result["answered"] is False and result["error"]


def test_a_connection_closed_mid_answer_is_no_answer():
    port, _ = tcp_dns_server(None)  # accepts the query, then hangs up
    result = dnsprobe.ask_tcp("example.com", server="127.0.0.1", port=port, timeout=2.0)
    assert result["answered"] is False
    assert "closed" in result["error"]


def test_resolver_alive_asks_over_tcp_because_wsl2_forwards_tcp_only():
    """THE fix for the false 'no answer': a healthy Pi-hole behind WSL2's TCP-only
    localhost forwarding must read ACTIVE, not UNKNOWN."""
    port, seen = tcp_dns_server(a_dns_answer())
    view = dnsprobe.resolver_alive("127.0.0.1", port)
    assert view["status"] == "ACTIVE"
    assert view["server"] == f"127.0.0.1:{port}"
    assert view["detail"]["k"] == "pihole.v.ms"
    assert "answered in" in render(view["detail"])
    assert seen["query"], "the probe never actually spoke TCP"


def test_resolver_alive_reports_unknown_when_nothing_answers_tcp():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    view = dnsprobe.resolver_alive("127.0.0.1", port, timeout=1.0)
    assert view["status"] == "UNKNOWN"
    assert view["detail"]["k"] == "pihole.v.none"


def test_resolver_alive_keeps_the_result_shape_the_page_reads():
    view = dnsprobe.resolver_alive("127.0.0.1", 1,
                                   asker=lambda name, *, server, port, timeout: {
                                       "answered": True, "error": None, "addresses": [],
                                       "rcode": 0, "elapsed_ms": 7})
    assert set(view) == {"server", "status", "detail"}
    assert view == {"server": "127.0.0.1:1", "status": "ACTIVE",
                    "detail": {"k": "pihole.v.ms", "p": {"ms": 7}}}


# --- verdicts ----------------------------------------------------------------

def test_a_parental_block_is_a_block_even_though_it_answers_a_real_address():
    """THE regression. `94.140.14.35` is AdGuard's block page, not a leak."""
    probe = {"key": "adult", "label": "Adult content", "domain": "blocked.example",
             "expect": "BLOCKED", "why": ""}
    result = dnsprobe.probe_result(
        probe, entry("blocked.example", "FilteredParental"),
        {"addresses": [ADGUARD_BLOCK_PAGE]})
    assert result["verdict"] == "BLOCKED" and result["ok"] is True
    # The address is still shown, as context - but it decided nothing.
    assert result["addresses"] == [ADGUARD_BLOCK_PAGE]


def test_every_filtered_reason_reads_as_blocked():
    for reason in dnsprobe.BLOCK_REASONS:
        assert dnsprobe.classify(reason) == "BLOCKED", reason


def test_not_filtered_reasons_read_as_allowed_despite_starting_with_filtered_lookalikes():
    assert dnsprobe.classify("NotFilteredNotFound") == "ALLOWED"
    assert dnsprobe.classify("NotFilteredWhiteList") == "ALLOWED"


def test_a_rewrite_is_named_rather_than_guessed():
    assert dnsprobe.classify("Rewrite") == "REWRITTEN"


def test_an_unknown_reason_is_unknown_not_allowed():
    assert dnsprobe.classify("SomethingNew") == "UNKNOWN"
    assert dnsprobe.classify(None) == "UNKNOWN"


def test_the_control_domain_must_resolve_or_the_probe_fails():
    probe = {"key": "control", "label": "Control", "domain": "example.com",
             "expect": "ALLOWED", "why": ""}
    good = dnsprobe.probe_result(probe, entry("example.com", "NotFilteredNotFound"), None)
    bad = dnsprobe.probe_result(probe, entry("example.com", "FilteredBlackList"), None)
    assert good["ok"] is True and bad["ok"] is False


def test_a_probe_with_no_query_log_entry_is_not_a_pass():
    """No entry means the query never reached AdGuard, which is a finding in itself -
    the resolver may not be in the path at all."""
    probe = {"key": "ads", "label": "Ads", "domain": "doubleclick.net",
             "expect": "BLOCKED", "why": ""}
    result = dnsprobe.probe_result(probe, None, {"addresses": []})
    assert result["verdict"] == "UNKNOWN" and result["ok"] is False
    assert result["detail"] == {"k": "probe.detail.nolog"}
    assert "did not reach AdGuard" in render(result["detail"])


def test_the_newest_entry_for_the_domain_wins_and_others_are_ignored():
    entries = [entry("other.example", "FilteredBlackList"),
               entry("doubleclick.net.", "FilteredBlackList", rule="||doubleclick.net^"),
               entry("doubleclick.net", "NotFilteredNotFound")]
    found = dnsprobe.latest_entry("doubleclick.net", entries)
    assert found["reason"] == "FilteredBlackList"
    assert dnsprobe.entry_rule(found) == "||doubleclick.net^"


def test_the_older_query_log_shape_is_still_understood():
    entries = [{"question": {"host": "youtube.com"}, "reason": "FilteredBlackList"}]
    assert dnsprobe.latest_entry("youtube.com", entries) is entries[0]


def test_the_rules_list_is_read_when_the_build_uses_it():
    found = entry("x.example", "FilteredBlackList",
                  rules=[{"text": "||x.example^", "filter_list_id": 3}])
    assert dnsprobe.entry_rule(found) == "||x.example^"


# --- the probe table ---------------------------------------------------------

def test_the_adult_probe_ships_without_a_domain_and_says_how_to_set_one():
    """This repo does not carry adult domain literals; the parent supplies one in
    panel.json. An unconfigured probe reports UNKNOWN, never a silent pass."""
    probes = {probe["key"]: probe for probe in dnsprobe.probe_list(None)}
    assert probes["adult"]["domain"] is None
    result = dnsprobe.probe_result(probes["adult"], None, None)
    assert result["verdict"] == "UNKNOWN" and result["ok"] is False
    assert "panel.json" in render(result["detail"])
    # the probe's own key is substituted into the instruction
    assert '"adult"' in render(result["detail"])


def test_a_configured_adult_domain_is_used():
    probes = {p["key"]: p for p in dnsprobe.probe_list({"adult": "example.invalid"})}
    assert probes["adult"]["domain"] == "example.invalid"


def test_the_table_covers_ads_youtube_adult_and_a_control():
    assert {p["key"] for p in dnsprobe.probe_list(None)} == {"ads", "youtube", "adult",
                                                             "control"}
    control = next(p for p in dnsprobe.probe_list(None) if p["key"] == "control")
    assert control["expect"] == "ALLOWED"


# --- the run -----------------------------------------------------------------

class FakeClient:
    def __init__(self, table):
        self.table = table
        self.searched = []

    def query_log(self, *, search, limit=20):
        self.searched.append(search)
        return self.table.get(search, [])


def test_a_run_resolves_first_then_reads_the_log_for_each_domain():
    asked = []

    def asker(name, *, server, port):
        asked.append((name, server, port))
        return {"answered": True, "addresses": [ADGUARD_BLOCK_PAGE], "rcode": 0}

    client = FakeClient({
        "doubleclick.net": [entry("doubleclick.net", "FilteredBlackList")],
        "www.youtube.com": [entry("www.youtube.com", "NotFilteredNotFound")],
        "example.com": [entry("example.com", "NotFilteredNotFound")],
    })
    results = dnsprobe.run_probes(dnsprobe.probe_list(None), client=client,
                                  server="127.0.0.1", port=53, sleep=lambda _: None,
                                  asker=asker)
    verdicts = {r["key"]: (r["verdict"], r["ok"]) for r in results}
    assert verdicts["ads"] == ("BLOCKED", True)
    # expect=ALLOWED since the block was lifted: resolving normally is the pass
    assert verdicts["youtube"] == ("ALLOWED", True)
    assert verdicts["control"] == ("ALLOWED", True)
    assert verdicts["adult"] == ("UNKNOWN", False)
    # The unconfigured probe is never resolved and never searched for.
    assert [name for name, _, _ in asked] == ["doubleclick.net", "www.youtube.com",
                                              "example.com"]
    assert "None" not in client.searched
