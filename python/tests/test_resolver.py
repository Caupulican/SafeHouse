"""The lane that notices when the filtering resolver is gone.

Worth being thorough about, because its failure mode is invisible. Everything blocked at
the DNS layer lives in two containers inside WSL; when that VM is not running the Windows
relay falls back to the router, so names resolve, browsing works, and every other signal
stays green while nothing at all is filtered. That is the 2026-08-01 outage, fourteen
hours long.

The properties that matter, each with a test:

* **"answered" is not "filtering".** The relay owns UDP 53 on Windows and answers
  perfectly well during the outage - with unfiltered results. So the verdict is a known
  ad domain coming back sinkholed, never the fact that something replied. A check built
  the other way round reports the outage as healthy, which is what this module exists to
  stop.
* **an idle run spawns nothing.** This fires every fifteen minutes on the machine the
  child games on. If a healthy check cost a `wsl.exe` it would not be schedulable.
* **"could not look" is not "not running".** `vm_running()` returns None when the process
  list cannot be read, and None must never collapse into False.
"""

from __future__ import annotations

import json

import pytest

from safehouse_setup import resolver

# Real answer shapes, built the way a resolver builds them: header, question, answer.
QUESTION = b"\x0bdoubleclick\x03net\x00\x00\x01\x00\x01"


def _packet(rcode: int = 0, answers: bytes = b"", count: int = 0,
            txid: bytes = b"\x5a\xfe") -> bytes:
    header = txid + bytes([0x81, 0x80 | rcode]) + b"\x00\x01" + count.to_bytes(2, "big") \
        + b"\x00\x00\x00\x00"
    return header + QUESTION + answers


def _a_record(address: bytes) -> bytes:
    # A compressed name pointer back to the question, then type A, IN, TTL, rdlength.
    return b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + address


SINKHOLED = _packet(answers=_a_record(b"\x00\x00\x00\x00"), count=1)
ROUTABLE = _packet(answers=_a_record(bytes([4, 228, 31, 150])), count=1)
NXDOMAIN = _packet(rcode=3)
EMPTY = _packet()


# --- the verdict -------------------------------------------------------------

def test_the_unspecified_address_is_a_block():
    assert resolver.is_blocked(SINKHOLED) is True


def test_nxdomain_is_also_a_block():
    """AdGuard's blocking mode is configurable; `nxdomain` is a supported setting. Reading
    only 0.0.0.0 would report a correctly-configured resolver as unfiltered."""
    assert resolver.is_blocked(NXDOMAIN) is True


def test_a_real_address_is_not_a_block():
    assert resolver.is_blocked(ROUTABLE) is False


def test_no_answer_at_all_is_not_a_block():
    """It is a failure, and a failure must never be reported as a successful block - that
    would turn a dead resolver into a green pill."""
    assert resolver.is_blocked(None) is False
    assert resolver.is_blocked(EMPTY) is False


def test_resolved_normally_needs_a_real_address():
    assert resolver.resolved_normally(ROUTABLE) is True
    assert resolver.resolved_normally(SINKHOLED) is False
    assert resolver.resolved_normally(NXDOMAIN) is False
    assert resolver.resolved_normally(EMPTY) is False
    assert resolver.resolved_normally(None) is False


# --- the query ---------------------------------------------------------------

class FakeSocket:
    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, _):
        pass

    def sendto(self, packet, address):
        self.sent.append((packet, address))

    def recvfrom(self, _):
        if self.reply is None:
            raise OSError("timed out")
        return self.reply, ("127.0.0.1", 53)


def _sockets(reply, seen):
    def make(*_a, **_k):
        sock = FakeSocket(reply)
        seen.append(sock)
        return sock
    return make


def test_a_timeout_is_no_answer(monkeypatch):
    monkeypatch.setattr(resolver.socket, "socket", _sockets(None, []))
    assert resolver.query("doubleclick.net") is None


def test_a_reply_with_somebody_elses_transaction_id_is_discarded(monkeypatch):
    stray = _packet(answers=_a_record(b"\x00\x00\x00\x00"), count=1, txid=b"\x11\x11")
    monkeypatch.setattr(resolver.socket, "socket", _sockets(stray, []))
    assert resolver.query("doubleclick.net") is None


def test_the_query_goes_to_the_port_it_was_given(monkeypatch):
    seen = []
    monkeypatch.setattr(resolver.socket, "socket", _sockets(ROUTABLE, seen))
    resolver.query("github.com", "127.0.0.1", 5353)
    assert seen[0].sent[0][1] == ("127.0.0.1", 5353)


# --- the process list --------------------------------------------------------
# `tasklist` names the WSL2 utility VM `vmmemWSL` with NO `.exe`, which is why the obvious
# `/FI "IMAGENAME eq vmmemWSL.exe"` filter matches nothing at all and reports a running VM
# as stopped. Verified on the live machine.

TASKLIST = (
    '"System Idle Process","0","Services","0","8 K"\r\n'
    '"wslservice.exe","4448","Services","0","14.084 K"\r\n'
    '"vmmemWSL","14724","Services","0","958.684 K"\r\n'
)


def test_vm_running_matches_the_name_tasklist_actually_prints(monkeypatch):
    monkeypatch.setattr(resolver, "run", lambda *a, **k: (0, TASKLIST))
    assert resolver.vm_running() is True


def test_vm_running_is_false_when_only_the_service_is_there(monkeypatch):
    """`wslservice.exe` runs from boot whether or not any distro is up, so it must never
    be read as evidence that the VM is running."""
    without = TASKLIST.replace('"vmmemWSL","14724","Services","0","958.684 K"\r\n', "")
    monkeypatch.setattr(resolver, "run", lambda *a, **k: (0, without))
    assert resolver.vm_running() is False


def test_vm_running_is_none_when_the_process_list_could_not_be_read(monkeypatch):
    monkeypatch.setattr(resolver, "run", lambda *a, **k: (1, "access denied"))
    assert resolver.vm_running() is None


def test_a_substring_is_not_a_match(monkeypatch):
    """`vmmemWSLsomething.exe` is not the VM. Matching by prefix here is the same mistake
    that would make the browser guard kill `msedgewebview2.exe`."""
    monkeypatch.setattr(resolver, "run",
                        lambda *a, **k: (0, '"vmmemWSLother.exe","1","Services","0","1 K"\r\n'))
    assert resolver.vm_running() is False


# --- the chain ---------------------------------------------------------------

def _chain(monkeypatch, *, filter_answer, control_answer, tcp=True, vm=True):
    replies = {resolver.FILTER_PROBE: filter_answer, resolver.CONTROL_PROBE: control_answer}
    monkeypatch.setattr(resolver, "query",
                        lambda name, *a, **k: replies[name])
    monkeypatch.setattr(resolver, "tcp_open", lambda *a, **k: tcp)
    monkeypatch.setattr(resolver, "vm_running", lambda: vm)
    return resolver.chain_state()


def test_up_means_filtering_and_nothing_else(monkeypatch):
    """The load-bearing assertion of this module. Both containers listening and DNS
    resolving is exactly what the fourteen-hour outage looked like."""
    state = _chain(monkeypatch, filter_answer=ROUTABLE, control_answer=ROUTABLE, tcp=True)
    assert state["resolves"] is True and state["adguard"] is True
    assert state["filtering"] is False
    assert state["up"] is False, "DNS answering is not filtering"


def test_a_sinkholed_probe_is_up_even_if_a_container_port_is_shut(monkeypatch):
    """Blocking is the property. Where it comes from is diagnosis, not verdict."""
    state = _chain(monkeypatch, filter_answer=SINKHOLED, control_answer=ROUTABLE,
                   tcp=False)
    assert state["up"] is True and state["adguard"] is False


def test_no_reply_to_either_probe_says_the_relay_itself_is_gone(monkeypatch):
    state = _chain(monkeypatch, filter_answer=None, control_answer=None)
    assert state["relay"] is False and state["up"] is False


def test_the_relay_is_up_if_either_probe_came_back(monkeypatch):
    state = _chain(monkeypatch, filter_answer=None, control_answer=ROUTABLE)
    assert state["relay"] is True


# --- repairing ---------------------------------------------------------------

def test_a_filtering_chain_spawns_no_process_at_all(tmp_path, monkeypatch):
    """The property that makes a 15-minute schedule acceptable on a gaming PC."""
    monkeypatch.setattr(resolver, "chain_state",
                        lambda **k: {"up": True, "filtering": True, "adguard": True,
                                     "pihole": True, "vm": True, "resolves": True,
                                     "relay": True})
    spawned = []
    monkeypatch.setattr(resolver, "run", lambda *a, **k: spawned.append(a) or (0, ""))
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)
    record = resolver.ensure(tmp_path, clock=lambda: 100.0)
    assert record["action"] == "none" and record["ok"] is True
    assert spawned == []


def test_a_chain_that_answers_but_does_not_filter_is_still_repaired(tmp_path, monkeypatch):
    """The whole point: this state looks perfect from every angle except the one that
    matters, so it must trigger the repair rather than be left alone."""
    failed_open = {"up": False, "filtering": False, "resolves": True, "relay": True,
                   "adguard": False, "pihole": False, "vm": False}
    fixed = dict(failed_open, up=True, filtering=True, adguard=True, pihole=True, vm=True)
    states = iter([failed_open, fixed, fixed])
    monkeypatch.setattr(resolver, "chain_state", lambda **k: next(states))
    calls = []
    monkeypatch.setattr(resolver, "run", lambda args, **k: calls.append(args) or (0, ""))
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)
    record = resolver.ensure(tmp_path, distro="Debian", clock=lambda: 100.0)
    assert record["ok"] is True and record["action"] == "repaired"
    assert "blocking again" in record["detail"]
    # Starting the distro was enough, so the expensive reconcile was NOT run.
    assert len(calls) == 1 and calls[0][-1] == "true"
    assert calls[0][1:3] == ["-d", "Debian"]


def test_the_reconcile_only_runs_when_starting_was_not_enough(tmp_path, monkeypatch):
    down = {"up": False, "filtering": False, "resolves": True, "relay": True,
            "adguard": False, "pihole": False, "vm": False}
    monkeypatch.setattr(resolver, "chain_state", lambda **k: dict(down))
    calls = []
    monkeypatch.setattr(resolver, "run", lambda args, **k: calls.append(args) or (0, ""))
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)
    record = resolver.ensure(tmp_path, clock=lambda: 100.0)
    assert len(calls) == 2 and resolver.RECONCILE in calls[1][-1]
    assert record["ok"] is False
    assert "STILL DOWN" in record["detail"]
    assert "falls back to the router" in record["detail"]


def test_both_containers_up_but_nothing_blocked_names_that_case(tmp_path, monkeypatch):
    stuck = {"up": False, "filtering": False, "resolves": True, "relay": True,
             "adguard": True, "pihole": True, "vm": True}
    monkeypatch.setattr(resolver, "chain_state", lambda **k: dict(stuck))
    monkeypatch.setattr(resolver, "run", lambda *a, **k: (0, ""))
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)
    detail = resolver.ensure(tmp_path, clock=lambda: 100.0)["detail"]
    assert "filtering is off" in detail


def test_force_reconciles_even_when_the_chain_is_filtering(tmp_path, monkeypatch):
    """The panel's button. A button that answers 'no need' teaches the parent it lies."""
    good = {"up": True, "filtering": True, "resolves": True, "relay": True,
            "adguard": True, "pihole": True, "vm": True}
    monkeypatch.setattr(resolver, "chain_state", lambda **k: dict(good))
    calls = []
    monkeypatch.setattr(resolver, "run", lambda args, **k: calls.append(args) or (0, ""))
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)
    record = resolver.ensure(tmp_path, force=True, clock=lambda: 100.0)
    assert record["action"] == "repaired"
    assert len(calls) == 2


# --- the record the panel reads ----------------------------------------------

def test_the_record_is_written_where_the_panel_reads_it(tmp_path, monkeypatch):
    """The panel runs as LocalSystem and cannot call WSL at all, so this file is the only
    way it learns what the last repair did."""
    monkeypatch.setattr(resolver, "chain_state",
                        lambda **k: {"up": True, "filtering": True, "resolves": True,
                                     "relay": True, "adguard": True, "pihole": True,
                                     "vm": True})
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)
    resolver.ensure(tmp_path, trigger="manual", clock=lambda: 100.0)
    written = json.loads((tmp_path / resolver.STATUS_FILE).read_text())
    assert written["trigger"] == "manual" and written["action"] == "none"
    assert resolver.read_status(tmp_path) == written


def test_an_unwritable_record_does_not_fail_the_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "chain_state",
                        lambda **k: {"up": True, "filtering": True, "resolves": True,
                                     "relay": True, "adguard": True, "pihole": True,
                                     "vm": True})
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)

    import safehouse_ytbudget.atomicio as atomicio

    def explode(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(atomicio, "write_json", explode)
    assert resolver.ensure(tmp_path, clock=lambda: 100.0)["ok"] is True


def test_a_missing_record_reads_as_none(tmp_path):
    assert resolver.read_status(tmp_path) is None
    (tmp_path / resolver.STATUS_FILE).write_text("{not json", encoding="utf-8")
    assert resolver.read_status(tmp_path) is None


def test_the_panel_and_the_watchdog_name_the_same_probe_domains():
    """The page prints these; the watchdog queries them. Two copies drift, and the copy
    that drifts is the one on the screen."""
    from safehouse_panel import status as statusmod

    assert statusmod.RESOLVER_PROBE == resolver.FILTER_PROBE
    assert statusmod.CONTROL_PROBE == resolver.CONTROL_PROBE
