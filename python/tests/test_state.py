"""Tests for state persistence.

A torn or misread state file either re-blocks a child who had time left, or hands out
an unbounded day. Both directions are tested.
"""

from __future__ import annotations

import json

from safehouse_ytbudget.state import State, from_json, load, save, to_json

TODAY = "2026-07-25"


def test_roundtrip(tmp_path):
    st = State(date=TODAY, seconds_used=1234, bonus_sec=600, blocked_by_budget=True)
    p = tmp_path / "state.json"
    save(p, st)
    again, warnings = load(p, today=TODAY)
    assert again == st
    assert warnings == []


def test_missing_file_starts_a_clean_day(tmp_path):
    st, warnings = load(tmp_path / "nope.json", today=TODAY)
    assert st == State(date=TODAY)
    assert warnings == []


def test_corrupt_state_fails_closed_to_a_fresh_day():
    """Failing closed keeps the watcher running; a dead watcher enforces nothing."""
    st, warnings = from_json("{ not json", today=TODAY)
    assert st.date == TODAY
    assert st.seconds_used == 0
    assert any("not valid JSON" in w for w in warnings)


def test_negative_usage_is_rejected():
    st, warnings = from_json(json.dumps({"date": TODAY, "seconds_used": -50}), today=TODAY)
    assert st.seconds_used == 0
    assert any("seconds_used" in w for w in warnings)


def test_non_bool_flag_is_rejected():
    st, warnings = from_json(
        json.dumps({"date": TODAY, "blocked_by_budget": "yes"}), today=TODAY
    )
    assert st.blocked_by_budget is False
    assert any("blocked_by_budget" in w for w in warnings)


def test_missing_date_starts_fresh():
    st, warnings = from_json(json.dumps({"seconds_used": 100}), today=TODAY)
    assert st.date == TODAY
    assert any("date" in w for w in warnings)


def test_unknown_keys_are_preserved(tmp_path):
    """A rollback to the PowerShell version must not lose the keys it relies on."""
    raw = {"date": TODAY, "seconds_used": 10, "limit_min": 120, "future": "x"}
    st, _ = from_json(json.dumps(raw), today=TODAY)
    assert st.extra == {"limit_min": 120, "future": "x"}
    out = json.loads(to_json(st))
    assert out["limit_min"] == 120
    assert out["future"] == "x"
    assert out["seconds_used"] == 10


def test_extra_does_not_affect_equality():
    a = State(date=TODAY, extra={"limit_min": 120})
    b = State(date=TODAY)
    assert a == b


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "state.json"
    save(p, State(date=TODAY, seconds_used=1))
    save(p, State(date=TODAY, seconds_used=2))
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "state.json"]
    assert leftovers == []
    again, _ = load(p, today=TODAY)
    assert again.seconds_used == 2


def test_save_creates_parent_directory(tmp_path):
    p = tmp_path / "nested" / "deeper" / "state.json"
    save(p, State(date=TODAY))
    assert p.exists()


def test_effective_limit_includes_bonus():
    st = State(date=TODAY, bonus_sec=600)
    assert st.effective_limit_sec(10800) == 11400
