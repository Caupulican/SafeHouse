"""Tests for the manual parental toggle.

Two properties matter most:

1. A manual block and the watcher's automatic budget block must not interfere. A parent
   saying "no, now" must survive the watcher's reconcile, and neither may stack a second
   section on top of the other.
2. A block must reach BOTH lanes, or be reported as incomplete. The hosts file does not
   cover the Google Play Games VM, so a hosts-only block is one the child walks around by
   opening Play Games -- while the command claims success.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safehouse_setup import toggle  # noqa: E402
from safehouse_ytbudget import hosts as hostsmod  # noqa: E402
from safehouse_ytbudget.platform import adguard as adguardmod  # noqa: E402

BASE = (
    b"# Copyright (c) 1993-2009 Microsoft Corp.\r\n"
    b"#\r\n"
    b"0.0.0.0 ads.example.com\r\n"
)


class FakeResolver:
    """Stands in for AdGuard Home's blocked-services list."""

    def __init__(self, blocked: bool = False) -> None:
        self.blocked = blocked
        self.calls: list[str] = []

    def set_blocked(self, blocked: bool) -> str:
        self.calls.append(f"set_blocked({blocked})")
        if self.blocked == blocked:
            return "ALREADY" if blocked else "NONE"
        self.blocked = blocked
        return "BLOCKED" if blocked else "UNBLOCKED"

    def is_blocked(self) -> bool:
        return self.blocked


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    # Neither blocking nor unblocking should need admin in a test.
    monkeypatch.setattr(toggle, "require_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(toggle, "flush_dns", lambda: None)
    p = tmp_path / "hosts"
    p.write_bytes(BASE)
    return p


@pytest.fixture
def resolver(monkeypatch):
    """An armed resolver lane whose service the budget also owns."""
    fake = FakeResolver()
    monkeypatch.setattr(toggle, "resolver_client", lambda name, root=None: (fake, "OK"))
    monkeypatch.setattr(toggle, "_budget_service", lambda _root: "youtube")
    return fake


@pytest.fixture
def blocklist(tmp_path, monkeypatch):
    lst = tmp_path / "youtube.txt"
    lst.write_bytes(b"# comment\nyoutube.com\nwww.youtube.com\n")
    monkeypatch.setattr(toggle, "find_block_list", lambda name, search_from=None: lst)
    return lst


# --- naming ------------------------------------------------------------------


def test_known_names_are_capitalised_properly():
    assert toggle.display_name("youtube") == "YouTube"
    assert toggle.display_name("tiktok") == "TikTok"


def test_unknown_names_fall_back_to_title_case():
    assert toggle.display_name("some-site") == "Some Site"


# --- block / allow -----------------------------------------------------------


def test_block_then_allow_restores_the_file_exactly(hosts, blocklist):
    assert toggle.status("youtube", hosts) is False
    assert toggle.block("youtube", hosts).hosts.startswith("BLOCKED:")
    assert toggle.status("youtube", hosts) is True
    assert toggle.allow("youtube", hosts).hosts == "UNBLOCKED"
    assert hosts.read_bytes() == BASE


def test_block_inserts_every_host(hosts, blocklist):
    toggle.block("youtube", hosts)
    raw = hosts.read_bytes()
    assert b"0.0.0.0 youtube.com" in raw
    assert b"0.0.0.0 www.youtube.com" in raw


def test_block_is_idempotent(hosts, blocklist):
    toggle.block("youtube", hosts)
    once = hosts.read_bytes()
    assert toggle.block("youtube", hosts).hosts == "ALREADY"
    assert hosts.read_bytes() == once


def test_allow_when_absent_is_a_noop(hosts, blocklist):
    assert toggle.allow("youtube", hosts).hosts == "NONE"
    assert hosts.read_bytes() == BASE


def test_missing_block_list_is_reported(hosts, monkeypatch):
    monkeypatch.setattr(toggle, "find_block_list", lambda name, search_from=None: None)
    result = toggle.block("youtube", hosts)
    assert result.hosts == "NOLIST"
    assert result.hosts_failed is True
    assert hosts.read_bytes() == BASE


def test_empty_block_list_refuses(hosts, tmp_path, monkeypatch):
    """A section with no entries blocks nothing while reporting success."""
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"# only comments\n")
    monkeypatch.setattr(toggle, "find_block_list", lambda name, search_from=None: empty)
    assert toggle.block("youtube", hosts).hosts == "NOLIST"


# --- line-ending fidelity ----------------------------------------------------


def test_section_matches_the_files_line_endings(hosts, blocklist):
    toggle.block("youtube", hosts)
    added = hosts.read_bytes()[len(BASE):]
    assert added.count(b"\r\n") == added.count(b"\n"), "all-CRLF file must get a CRLF section"


# --- coexistence with the watcher's automatic block --------------------------


def test_manual_block_is_detected_alongside_the_auto_one(hosts, blocklist):
    """`--block` must not stack a second section on top of the watcher's."""
    auto = hostsmod.insert_block(hosts.read_bytes(), ["youtube.com"])
    hosts.write_bytes(auto)
    # The watcher's marker says "(auto: daily budget)" and is a YouTube section too.
    assert toggle.status("youtube", hosts) is True
    assert toggle.block("youtube", hosts).hosts == "ALREADY"


def test_allow_leaves_the_watchers_auto_block_alone(hosts, blocklist):
    """Lifting the manual block must not hand back time the budget already spent - and
    the watcher would re-apply it within one sample regardless."""
    hosts.write_bytes(hostsmod.insert_block(hosts.read_bytes(), ["youtube.com"]))
    before = hosts.read_bytes()
    assert toggle.allow("youtube", hosts).hosts == "NONE"
    assert hosts.read_bytes() == before


# --- the resolver lane -------------------------------------------------------


def test_block_drives_the_resolver_and_stamps_it_manual(hosts, blocklist, resolver,
                                                        tmp_path):
    """The resolver is the only lane the Play Games VM can see."""
    result = toggle.block("youtube", hosts, tmp_path)

    assert result.hosts.startswith("BLOCKED:")
    assert result.resolver == "BLOCKED"
    assert result.resolver_covered is True
    assert resolver.is_blocked() is True
    # Stamped manual, or the nightly reset lifts a parent's decision at midnight.
    marker = tmp_path / toggle.BLOCK_MARKER
    assert adguardmod.read_block_owner(marker) == adguardmod.MANUAL_OWNER


def test_allow_lifts_a_manual_resolver_block(hosts, blocklist, resolver, tmp_path):
    toggle.block("youtube", hosts, tmp_path)
    result = toggle.allow("youtube", hosts, tmp_path)

    assert result.resolver == "UNBLOCKED"
    assert resolver.is_blocked() is False
    assert adguardmod.read_block_owner(tmp_path / toggle.BLOCK_MARKER) is None


def test_allow_refuses_to_lift_a_budget_owned_resolver_block(hosts, blocklist, resolver,
                                                            tmp_path):
    """The mirror of the hosts-lane rule, on the lane that actually enforces.

    Lifting here would hand back time the budget already spent.
    """
    resolver.blocked = True
    adguardmod.write_block_date(tmp_path / toggle.BLOCK_MARKER, "2026-07-31",
                               owner=adguardmod.BUDGET_OWNER)

    result = toggle.allow("youtube", hosts, tmp_path)

    assert result.resolver == "BUDGET_OWNED"
    assert resolver.is_blocked() is True
    assert resolver.calls == [], "the budget's block must not even be touched"
    # Still counts as covered: the resolver IS enforcing, just not because of this command.
    assert result.resolver_covered is True


def test_manual_reason_coexists_with_budget_and_allow_releases_only_manual(
        hosts, blocklist, resolver, tmp_path):
    resolver.blocked = True
    marker = tmp_path / toggle.BLOCK_MARKER
    adguardmod.write_block_date(marker, "2026-07-31", owner=adguardmod.BUDGET_OWNER)

    blocked = toggle.block("youtube", hosts, tmp_path)
    assert blocked.resolver == "ALREADY"
    assert adguardmod.read_block_owners(marker) == {
        adguardmod.BUDGET_OWNER,
        adguardmod.MANUAL_OWNER,
    }

    allowed = toggle.allow("youtube", hosts, tmp_path)
    assert allowed.resolver == "BUDGET_OWNED"
    assert resolver.is_blocked() is True
    assert adguardmod.read_block_owners(marker) == {adguardmod.BUDGET_OWNER}


def test_a_disarmed_resolver_is_reported_rather_than_hidden(hosts, blocklist, monkeypatch):
    monkeypatch.setattr(toggle, "resolver_client",
                        lambda name, root=None: (None, "DISARMED"))

    result = toggle.block("youtube", hosts)

    assert result.hosts.startswith("BLOCKED:")
    assert result.resolver == "DISARMED"
    assert result.resolver_covered is False, "a hosts-only block must not read as covered"


def test_an_unknown_service_refuses_instead_of_silently_blocking_nothing(hosts, monkeypatch):
    """AdGuard accepts any id and reports success, so the id has to be validated."""
    monkeypatch.setattr(toggle, "resolver_client",
                        lambda name, root=None: (None, f"NO_SERVICE:{name}"))
    monkeypatch.setattr(toggle, "find_block_list", lambda name, search_from=None: None)

    result = toggle.block("nosuchsite", hosts)

    assert result.resolver == "NO_SERVICE:nosuchsite"
    assert result.resolver_covered is False


def test_the_resolver_is_still_blocked_when_the_host_list_is_missing(hosts, resolver,
                                                                    tmp_path, monkeypatch):
    """A missing text file must not leave the Play Games VM unblocked too."""
    monkeypatch.setattr(toggle, "find_block_list", lambda name, search_from=None: None)

    result = toggle.block("youtube", hosts, tmp_path)

    assert result.hosts == "NOLIST"
    assert result.resolver == "BLOCKED"
    assert resolver.is_blocked() is True


def test_watcher_removal_leaves_a_manual_block_alone(hosts, blocklist):
    """The mirror case: the watcher's auto-unblock must not lift a parent's block."""
    toggle.block("youtube", hosts)
    manual = hosts.read_bytes()
    assert hostsmod.remove_block(manual, "auto") == manual
