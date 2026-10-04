"""The guard read must FAIL CLOSED: every defect reads as "enforcing, no unlock".

A wrong "go ahead" here means the guard kills Play Games half-way through an uninstall,
so the defaults are tested as carefully as the happy path. And the module must never
write: the unlock is a parent's act, not this tool's.
"""

from __future__ import annotations

import json

from playgames_cleanup import guard
from playgames_cleanup.guard import check, read_enabled, read_unlock_until, verdict

NOW = 1_800_000_000


def test_locked_by_default():
    v = verdict(enabled=True, unlock_until_ts=0, now=NOW)
    assert not v.ok and "ENFORCING" in v.reason and v.seconds_left == 0


def test_expired_unlock_is_locked():
    assert not verdict(enabled=True, unlock_until_ts=NOW - 1, now=NOW).ok
    assert not verdict(enabled=True, unlock_until_ts=NOW, now=NOW).ok


def test_active_unlock():
    v = verdict(enabled=True, unlock_until_ts=NOW + 3600, now=NOW)
    assert v.ok and v.seconds_left == 3600 and "60m00s" in v.reason
    assert "short" not in v.reason


def test_short_unlock_warns():
    v = verdict(enabled=True, unlock_until_ts=NOW + 120, now=NOW)
    assert v.ok and "short" in v.reason


def test_disabled_config_is_ok_and_flagged():
    v = verdict(enabled=False, unlock_until_ts=0, now=NOW)
    assert v.ok and v.disabled


def test_config_defects_mean_enabled(tmp_path):
    p = tmp_path / "cfg.json"
    assert read_enabled(p) is True                     # missing
    p.write_text("{not json", encoding="utf-8")
    assert read_enabled(p) is True                     # broken
    p.write_text("[]", encoding="utf-8")
    assert read_enabled(p) is True                     # not an object
    p.write_text(json.dumps({"enabled": "false"}), encoding="utf-8")
    assert read_enabled(p) is True                     # a string is not a boolean
    p.write_text(json.dumps({"enabled": 0}), encoding="utf-8")
    assert read_enabled(p) is True
    p.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert read_enabled(p) is False                    # only a literal false disables


def test_config_with_bom(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"enabled": False}).encode())
    assert read_enabled(p) is False


def test_state_defects_mean_no_unlock(tmp_path):
    p = tmp_path / "state.json"
    assert read_unlock_until(p) == 0
    p.write_text("garbage", encoding="utf-8")
    assert read_unlock_until(p) == 0
    p.write_text(json.dumps({"unlock_until_ts": "soon"}), encoding="utf-8")
    assert read_unlock_until(p) == 0
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert read_unlock_until(p) == 0
    p.write_text(json.dumps({"unlock_until_ts": -5}), encoding="utf-8")
    assert read_unlock_until(p) == 0
    p.write_text(json.dumps({"unlock_until_ts": NOW + 60, "kills": []}), encoding="utf-8")
    assert read_unlock_until(p) == NOW + 60


def test_check_reads_the_guards_real_file_names(tmp_path):
    (tmp_path / "playgames-guard.state.json").write_text(
        json.dumps({"unlock_until_ts": NOW + 600, "kills": [["client.exe", NOW - 5]]}),
        encoding="utf-8")
    assert check(tmp_path, NOW).ok
    (tmp_path / "playgames-guard.json").write_text(json.dumps({"enabled": True}),
                                                    encoding="utf-8")
    assert check(tmp_path, NOW + 601).ok is False


def test_check_writes_nothing(tmp_path):
    check(tmp_path, NOW)
    assert list(tmp_path.iterdir()) == []


def test_commands_target_the_deployed_guard():
    unlock = guard.unlock_command(45)
    assert r"C:\SafeHouse\python\python.exe" in unlock
    assert r"cd C:\SafeHouse\windows\youtube-budget" in unlock
    assert "-m safehouse_ytbudget.playgames_guard --unlock 45" in unlock
    assert guard.lock_command().endswith("-m safehouse_ytbudget.playgames_guard --lock")
