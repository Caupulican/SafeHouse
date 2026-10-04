"""The entry point's two exits that need no Windows UI: guard locked, and not Windows.

Both must print the lock reminder - it is printed on EVERY exit - and the locked path
must stop before anything is launched (app.py, and with it pywinauto, is never imported).
"""

from __future__ import annotations

import json
import sys

from playgames_cleanup import __main__ as entry


def test_locked_guard_exits_2_with_unlock_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    monkeypatch.setattr(sys, "platform", "win32")
    sys.modules.pop("playgames_cleanup.app", None)
    rc = entry.main(["--guard-root", str(tmp_path), "--unlock-minutes", "30"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "--unlock 30" in out and "ELEVATED" in out
    assert "--lock" in out                                   # the reminder still prints
    assert "playgames_cleanup.app" not in sys.modules        # nothing was launched
    assert list((tmp_path / "lad" / "SafeHouse" / "playgames-cleanup" / "logs").glob("*.log"))
    assert not (tmp_path / "playgames-guard.state.json").exists()   # read-only


def test_disabled_guard_reminder_says_reenable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    entry.lock_reminder(entry.guard.verdict(enabled=False, unlock_until_ts=0, now=0))
    assert '"enabled": true' in capsys.readouterr().out


def test_not_windows_exits_1_with_reminder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "x.json").write_text(json.dumps({}))
    assert entry.main(["--dry-run"]) == 1
    out = capsys.readouterr().out
    assert "run it on Windows" in out and "--lock" in out
