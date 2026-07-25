"""Tests for the hosts-file splice.

The load-bearing property is byte preservation: everything outside our markers must come
back byte-identical, because Windows silently ignores a hosts file it considers
malformed, which is indistinguishable from enforcement working.
"""

from __future__ import annotations

import pytest

from safehouse_ytbudget.hosts import (
    END_MARKER,
    START_MARKER,
    insert_block,
    is_blocked,
    detect_newline,
    parse_block_list,
    read,
    remove_block,
    write_atomic,
)

# A realistic Windows hosts file: CRLF header, LF body. Normalising these is the bug.
MIXED = (
    b"# Copyright (c) 1993-2009 Microsoft Corp.\r\n"
    b"#\r\n"
    b"#\t127.0.0.1       localhost\r\n"
    b"\r\n"
    b"0.0.0.0 ads.example.com\n"
    b"0.0.0.0 tracker.example.net\n"
)

HOSTS = ["youtube.com", "www.youtube.com", "m.youtube.com"]


def test_insert_then_remove_is_byte_identical():
    """The whole point of the module."""
    blocked = insert_block(MIXED, HOSTS)
    assert remove_block(blocked) == MIXED


def test_insert_preserves_crlf_header_and_lf_body():
    out = insert_block(MIXED, HOSTS)
    assert out.startswith(MIXED)
    assert b"#\t127.0.0.1       localhost\r\n" in out
    assert b"0.0.0.0 ads.example.com\n0.0.0.0 tracker.example.net\n" in out


def test_inserted_section_contains_every_host():
    out = insert_block(MIXED, HOSTS)
    nl = detect_newline(MIXED)
    for h in HOSTS:
        assert b"0.0.0.0 " + h.encode() + nl in out
    assert START_MARKER in out
    assert END_MARKER in out


def test_is_blocked_detects_our_section():
    assert not is_blocked(MIXED)
    assert is_blocked(insert_block(MIXED, HOSTS))


def test_insert_is_idempotent():
    once = insert_block(MIXED, HOSTS)
    twice = insert_block(once, HOSTS)
    assert once == twice


def test_insert_refuses_an_empty_host_list():
    """An empty list would write markers with no entries -- a block that blocks nothing,
    while `is_blocked` would then report success."""
    with pytest.raises(ValueError):
        insert_block(MIXED, [])


def test_remove_when_absent_is_a_noop():
    assert remove_block(MIXED) == MIXED


def test_repeated_cycles_do_not_accumulate_blank_lines():
    raw = MIXED
    for _ in range(5):
        raw = insert_block(raw, HOSTS)
        raw = remove_block(raw)
    assert raw == MIXED


def test_file_without_trailing_newline_gets_one():
    raw = b"127.0.0.1 localhost"
    nl = detect_newline(raw)  # no newline to learn from -> CRLF, the Windows default
    out = insert_block(raw, HOSTS)
    assert out.startswith(b"127.0.0.1 localhost" + nl)
    assert remove_block(out) == raw + nl


def test_empty_file():
    out = insert_block(b"", HOSTS)
    assert START_MARKER in out
    assert remove_block(out) == b""


# --- manual vs auto scope ----------------------------------------------------

MANUAL = (
    b"# === Parental block: YouTube (manual) ===\n"
    b"0.0.0.0 youtube.com\n"
    b"# === end YouTube block ===\n"
)


def test_auto_scope_leaves_a_manual_block_alone():
    """A parent's deliberate block must survive the watcher's automatic unblock."""
    raw = MIXED + MANUAL
    assert remove_block(raw, "auto") == raw


def test_any_scope_removes_a_manual_block():
    raw = MIXED + MANUAL
    assert remove_block(raw, "any") == MIXED


def test_is_blocked_sees_a_manual_block():
    """So the watcher does not stack its own section on top of one."""
    assert is_blocked(MIXED + MANUAL)


def test_insert_does_not_stack_onto_a_manual_block():
    raw = MIXED + MANUAL
    assert insert_block(raw, HOSTS) == raw


def test_invalid_scope_rejected():
    with pytest.raises(ValueError):
        remove_block(MIXED, "everything")


# --- block list parsing ------------------------------------------------------


def test_parse_block_list_strips_comments_and_blanks():
    raw = b"# NOTE: a comment\n\nyoutube.com\n  m.youtube.com  \nyt.be # trailing\n"
    assert parse_block_list(raw) == ["youtube.com", "m.youtube.com", "yt.be"]


def test_parse_block_list_handles_crlf():
    assert parse_block_list(b"youtube.com\r\nyt.be\r\n") == ["youtube.com", "yt.be"]


def test_parse_block_list_empty():
    assert parse_block_list(b"# only comments\n\n") == []


# --- atomic file I/O ---------------------------------------------------------


def test_write_atomic_roundtrip(tmp_path):
    p = tmp_path / "hosts"
    write_atomic(p, MIXED)
    assert read(p) == MIXED


def test_write_atomic_leaves_no_temp_files(tmp_path):
    p = tmp_path / "hosts"
    write_atomic(p, MIXED)
    write_atomic(p, insert_block(MIXED, HOSTS))
    assert [f.name for f in tmp_path.iterdir()] == ["hosts"]


def test_full_cycle_through_the_filesystem(tmp_path):
    p = tmp_path / "hosts"
    write_atomic(p, MIXED)
    write_atomic(p, insert_block(read(p), HOSTS))
    assert is_blocked(read(p))
    write_atomic(p, remove_block(read(p)))
    assert read(p) == MIXED


def test_removal_does_not_eat_the_previous_lines_newline():
    """REGRESSION: the section pattern consumed an optional leading newline
    unconditionally. When a section was not preceded by a blank line, that newline was
    the *previous line's* terminator, so removing the block joined two hosts entries
    into one corrupt line -- and Windows ignores a malformed hosts file wholesale."""
    raw = b"0.0.0.0 first.example.com\n" + MANUAL + b"0.0.0.0 last.example.com\n"
    out = remove_block(raw, "any")
    assert out == b"0.0.0.0 first.example.com\n0.0.0.0 last.example.com\n"
    assert b"first.example.com\n" in out


def test_removal_still_collapses_the_blank_line_we_inserted():
    nl = detect_newline(MIXED)
    raw = insert_block(MIXED, HOSTS)
    # A blank line precedes the marker: the previous line's terminator (whatever it was
    # -- MIXED's body is LF while the section is CRLF) followed by our separator.
    assert b"\n" + nl + START_MARKER in raw
    assert remove_block(raw) == MIXED


# --- line-ending fidelity ----------------------------------------------------


def test_all_crlf_file_gets_a_crlf_section():
    """The real hosts file on this machine is 26,775 lines, every one CRLF. Writing an
    LF-only section into it would introduce exactly the mixed endings this module exists
    to avoid."""
    raw = b"# header\r\n0.0.0.0 ads.example.com\r\n"
    out = insert_block(raw, HOSTS)
    added = out[len(raw):]
    assert b"\n" in added
    assert added.count(b"\r\n") == added.count(b"\n"), "section must be all-CRLF"
    assert remove_block(out) == raw


def test_all_lf_file_gets_an_lf_section():
    raw = b"# header\n0.0.0.0 ads.example.com\n"
    out = insert_block(raw, HOSTS)
    added = out[len(raw):]
    assert b"\r" not in added
    assert remove_block(out) == raw


def test_empty_file_defaults_to_crlf():
    out = insert_block(b"", HOSTS)
    assert out.count(b"\r\n") == out.count(b"\n")


def test_mixed_file_round_trips_either_way():
    """Whichever ending wins, removal must still restore the original exactly."""
    assert remove_block(insert_block(MIXED, HOSTS)) == MIXED
