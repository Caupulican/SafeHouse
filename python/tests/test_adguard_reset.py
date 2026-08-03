"""Tests for the transitional daily/boot reset of the AdGuard block.

The dangerous case is the boot trigger: a naive lift-on-startup hands YouTube back on
the first reboot after the budget runs out. Date-awareness is what makes it safe, and
that is what these pin down.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adguard_daily_reset as reset  # noqa: E402
from safehouse_ytbudget.platform.adguard import (  # noqa: E402
    BUDGET_OWNER,
    MANUAL_OWNER,
    read_block_date,
    read_block_owner,
    read_block_owners,
    add_block_owner,
    write_block_date,
)

TODAY = "2026-07-25"
YESTERDAY = "2026-07-24"


class FakeClient:
    def __init__(self, blocked: bool | None):
        self.blocked = blocked
        self.calls: list[bool] = []

    def is_blocked(self):
        return self.blocked

    def set_blocked(self, value: bool) -> str:
        self.calls.append(value)
        if self.blocked is None:
            return "UNREACHABLE"
        self.blocked = value
        return "BLOCKED" if value else "UNBLOCKED"


@pytest.fixture
def env(tmp_path, monkeypatch):
    creds = tmp_path / "adguard.json"
    creds.write_text(json.dumps({"base_url": "http://x:3000", "username": "u",
                                 "password": "p"}))
    marker = tmp_path / "marker.json"
    logp = tmp_path / "reset.log"

    holder = {}

    def factory(cfg):
        return holder["client"]

    monkeypatch.setattr(reset, "AdGuardClient", factory)

    def run(*extra, client):
        holder["client"] = client
        return reset.main(["--creds", str(creds), "--marker", str(marker),
                           "--log", str(logp), "--today", TODAY, *extra])

    return run, marker, logp


# --- the boot-trigger hazard -------------------------------------------------


def test_a_block_applied_today_is_left_alone(env):
    """THE reason this is date-aware. Rebooting at 20:00 must not hand back the four
    hours of YouTube the budget just closed."""
    run, marker, logp = env
    write_block_date(marker, TODAY)
    client = FakeClient(blocked=True)
    assert run(client=client) == 0
    assert client.calls == [], "must not have touched the block"
    assert client.blocked is True
    assert "hold" in logp.read_text()


def test_a_block_from_yesterday_is_lifted(env):
    run, marker, logp = env
    write_block_date(marker, YESTERDAY)
    client = FakeClient(blocked=True)
    assert run(client=client) == 0
    assert client.calls == [False]
    assert client.blocked is False
    assert read_block_date(marker) is None, "marker must be cleared"


def test_force_overrides_the_date_check(env):
    run, marker, _ = env
    write_block_date(marker, TODAY)
    client = FakeClient(blocked=True)
    assert run("--force", client=client) == 0
    assert client.blocked is False


# --- unknown provenance ------------------------------------------------------


def test_missing_marker_is_treated_as_stale_and_lifted(env):
    """A block nothing will ever clear is worse than one lifted early: it means YouTube
    is gone permanently with no indication why."""
    run, marker, logp = env
    client = FakeClient(blocked=True)
    assert run(client=client) == 0
    assert client.blocked is False
    assert "no block date recorded" in logp.read_text()


def test_corrupt_marker_is_treated_as_stale(env):
    run, marker, _ = env
    marker.write_text("{ not json")
    client = FakeClient(blocked=True)
    assert run(client=client) == 0
    assert client.blocked is False


# --- no-ops and failures -----------------------------------------------------


def test_already_unblocked_is_a_noop_and_clears_the_marker(env):
    run, marker, logp = env
    write_block_date(marker, YESTERDAY)
    client = FakeClient(blocked=False)
    assert run(client=client) == 0
    assert client.calls == []
    assert read_block_date(marker) is None


def test_unreachable_adguard_fails_loudly_and_leaves_the_block(env):
    """Restrictive direction: nobody gets unsupervised access, but it must be visible."""
    run, marker, logp = env
    write_block_date(marker, YESTERDAY)
    client = FakeClient(blocked=None)
    assert run(client=client) == 1
    assert "unreachable" in logp.read_text().lower()


def test_dry_run_changes_nothing(env):
    run, marker, _ = env
    write_block_date(marker, YESTERDAY)
    client = FakeClient(blocked=True)
    assert run("--dry-run", client=client) == 0
    assert client.calls == []
    assert client.blocked is True


def test_missing_credentials_is_an_error(tmp_path, monkeypatch):
    code = reset.main(["--creds", str(tmp_path / "nope.json"),
                       "--marker", str(tmp_path / "m.json"),
                       "--log", str(tmp_path / "l.log")])
    assert code == 1


# --- a parent's block is not the budget's ------------------------------------


def test_a_manual_block_is_never_lifted(env):
    """"No, now" has no expiry.

    Without the owner stamp this block looks exactly like a stale budget block from
    yesterday, and the nightly reset hands the site back at midnight.
    """
    run, marker, logp = env
    write_block_date(marker, YESTERDAY, owner=MANUAL_OWNER)
    client = FakeClient(blocked=True)

    assert run(client=client) == 0
    assert client.calls == []
    assert client.blocked is True
    assert "applied manually" in logp.read_text()


def test_force_still_lifts_a_manual_block(env):
    """The parent's own escape hatch stays usable."""
    run, marker, _ = env
    write_block_date(marker, YESTERDAY, owner=MANUAL_OWNER)
    client = FakeClient(blocked=True)

    assert run("--force", client=client) == 0
    assert client.blocked is False


def test_stale_budget_reason_is_released_without_lifting_a_manual_block(env):
    run, marker, logp = env
    write_block_date(marker, YESTERDAY, owner=BUDGET_OWNER)
    add_block_owner(marker, TODAY, MANUAL_OWNER)
    client = FakeClient(blocked=True)

    assert run(client=client) == 0
    assert client.calls == []
    assert client.blocked is True
    assert read_block_owners(marker) == {MANUAL_OWNER}
    assert "manual block remains" in logp.read_text()


# --- marker round trip -------------------------------------------------------


def test_marker_round_trip(tmp_path):
    p = tmp_path / "m.json"
    write_block_date(p, TODAY)
    assert read_block_date(p) == TODAY
    write_block_date(p, None)
    assert read_block_date(p) is None


def test_owner_defaults_to_the_budget_for_markers_written_before_owners_existed(tmp_path):
    """Old markers carry a date and no owner, and were always the budget's."""
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"blocked_on": YESTERDAY}))
    assert read_block_owner(p) == BUDGET_OWNER


def test_owner_round_trip(tmp_path):
    p = tmp_path / "m.json"
    write_block_date(p, TODAY, owner=MANUAL_OWNER)
    assert read_block_owner(p) == MANUAL_OWNER
    write_block_date(p, None)
    assert read_block_owner(p) is None


def test_two_owners_keep_independent_dates(tmp_path):
    p = tmp_path / "m.json"
    write_block_date(p, YESTERDAY, owner=BUDGET_OWNER)
    add_block_owner(p, TODAY, MANUAL_OWNER)

    assert read_block_date(p, BUDGET_OWNER) == YESTERDAY
    assert read_block_date(p, MANUAL_OWNER) == TODAY
    assert read_block_owners(p) == {BUDGET_OWNER, MANUAL_OWNER}
