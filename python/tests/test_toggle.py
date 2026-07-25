"""Tests for the manual hosts-block toggle.

The property that matters most: a manual block and the watcher's automatic budget block
must not interfere. A parent saying "no, now" must survive the watcher's reconcile, and
neither may stack a second section on top of the other.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from safehouse_setup import toggle  # noqa: E402
from safehouse_ytbudget import hosts as hostsmod  # noqa: E402

BASE = (
    b"# Copyright (c) 1993-2009 Microsoft Corp.\r\n"
    b"#\r\n"
    b"0.0.0.0 ads.example.com\r\n"
)


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    # Neither blocking nor unblocking should need admin in a test.
    monkeypatch.setattr(toggle, "require_admin", lambda *_a, **_k: None)
    monkeypatch.setattr(toggle, "flush_dns", lambda: None)
    p = tmp_path / "hosts"
    p.write_bytes(BASE)
    return p


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
    assert toggle.block("youtube", hosts).startswith("BLOCKED:")
    assert toggle.status("youtube", hosts) is True
    assert toggle.allow("youtube", hosts) == "UNBLOCKED"
    assert hosts.read_bytes() == BASE


def test_block_inserts_every_host(hosts, blocklist):
    toggle.block("youtube", hosts)
    raw = hosts.read_bytes()
    assert b"0.0.0.0 youtube.com" in raw
    assert b"0.0.0.0 www.youtube.com" in raw


def test_block_is_idempotent(hosts, blocklist):
    toggle.block("youtube", hosts)
    once = hosts.read_bytes()
    assert toggle.block("youtube", hosts) == "ALREADY"
    assert hosts.read_bytes() == once


def test_allow_when_absent_is_a_noop(hosts, blocklist):
    assert toggle.allow("youtube", hosts) == "NONE"
    assert hosts.read_bytes() == BASE


def test_missing_block_list_is_reported(hosts, monkeypatch):
    monkeypatch.setattr(toggle, "find_block_list", lambda name, search_from=None: None)
    assert toggle.block("youtube", hosts) == "NOLIST"
    assert hosts.read_bytes() == BASE


def test_empty_block_list_refuses(hosts, tmp_path, monkeypatch):
    """A section with no entries blocks nothing while reporting success."""
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"# only comments\n")
    monkeypatch.setattr(toggle, "find_block_list", lambda name, search_from=None: empty)
    assert toggle.block("youtube", hosts) == "NOLIST"


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
    assert toggle.block("youtube", hosts) == "ALREADY"


def test_allow_leaves_the_watchers_auto_block_alone(hosts, blocklist):
    """Lifting the manual block must not hand back time the budget already spent - and
    the watcher would re-apply it within one sample regardless."""
    hosts.write_bytes(hostsmod.insert_block(hosts.read_bytes(), ["youtube.com"]))
    before = hosts.read_bytes()
    assert toggle.allow("youtube", hosts) == "NONE"
    assert hosts.read_bytes() == before


def test_watcher_removal_leaves_a_manual_block_alone(hosts, blocklist):
    """The mirror case: the watcher's auto-unblock must not lift a parent's block."""
    toggle.block("youtube", hosts)
    manual = hosts.read_bytes()
    assert hostsmod.remove_block(manual, "auto") == manual
