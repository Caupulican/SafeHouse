"""Tests for the microphone lane.

Core Audio only exists on Windows, so the COM interfaces are replaced here by fakes that
answer the same three questions a real endpoint answers - what is your name, are you
muted, at what level - and record what was written to them. That is enough to hold down
everything the lane DECIDES: that every endpoint is covered rather than just the default,
that mute AND zero are both applied, that a defective config still enforces, that a COM
failure is a pause rather than a crash, and that a child clicking unmute twenty times is
twenty re-mutes and one log line.

The safety property with teeth: every way this lane can be confused - a broken config, an
unreadable driver, an endpoint that vanishes mid-pass - must end with the microphone off.
"""

from __future__ import annotations

import json
from pathlib import Path

from safehouse_ytbudget import mic_guard
from safehouse_ytbudget.mic_guard import (
    EPSILON,
    LOG_SUPPRESS_SEC,
    POLL_SEC,
    MicConfig,
    enforce_pass,
    load_config,
    needs_silencing,
)


class FakeEndpoint:
    """A capture endpoint that remembers what was done to it.

    `fail` makes every read raise OSError, which is exactly what ctypes raises when a COM
    call returns a failing HRESULT - a headset yanked out mid-pass.
    """

    def __init__(self, name, muted=False, level=1.0, fail=False, key=None):
        self.name = name
        self.key = key or name
        self.muted = muted
        self.level = level
        self.fail = fail
        self.silenced = 0
        self.closed = 0

    def state(self):
        if self.fail:
            raise OSError("[WinError -2147023728] the device is gone")
        return self.muted, self.level

    def silence(self):
        if self.fail:
            raise OSError("[WinError -2147023728] the device is gone")
        self.silenced += 1
        self.muted = True
        self.level = 0.0

    def close(self):
        self.closed += 1


def read_log(root: Path) -> list[str]:
    p = root / "mic-guard.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


# --- the pure decision --------------------------------------------------------

def test_a_muted_endpoint_at_zero_is_left_alone():
    # The only quiet answer there is. Everything else is a write.
    assert needs_silencing(True, 0.0) is False
    assert needs_silencing(True, EPSILON) is False


def test_anything_audible_is_silenced():
    assert needs_silencing(False, 0.0) is True      # muted off, level irrelevant
    assert needs_silencing(False, 1.0) is True
    assert needs_silencing(True, 0.5) is True       # muted but hot underneath
    assert needs_silencing(True, EPSILON + 0.01) is True


def test_unreadable_answers_fail_closed_towards_silence():
    # None is a driver that refused the read; NaN is the input a naive `level > EPSILON`
    # comparison fails OPEN on, which is the one direction this lane must never fail.
    assert needs_silencing(None, None) is True
    assert needs_silencing(True, None) is True
    assert needs_silencing(True, float("nan")) is True
    assert needs_silencing(True, float("inf")) is True
    assert needs_silencing(True, -0.5) is True
    assert needs_silencing(True, 5.0) is True
    assert needs_silencing(1, 0.0) is True          # truthy is not True: only True is


# --- config: every defect must still mute -------------------------------------

def test_the_config_fallback_is_enforcing(tmp_path):
    assert MicConfig() == MicConfig(enabled=True)
    assert load_config(tmp_path / "nope.json") == (MicConfig(), [])
    p = tmp_path / "mic-guard.json"
    p.write_text("{}")
    assert load_config(p) == (MicConfig(), [])


def test_a_broken_config_still_mutes_everything(tmp_path):
    p = tmp_path / "mic-guard.json"
    for bad in ("{broken", "[]", "null", '"off"', '{"enabled": "no"}',
                '{"enabled": 0}', '{"enabled": null}'):
        p.write_text(bad)
        cfg, warnings = load_config(p)
        assert cfg.enabled, bad
        assert warnings, bad


def test_enabled_false_is_the_only_escape_hatch(tmp_path):
    p = tmp_path / "mic-guard.json"
    p.write_text(json.dumps({"enabled": False}))
    assert load_config(p) == (MicConfig(enabled=False), [])
    # And nothing else in the file can stand the lane down - there is no allow list.
    p.write_text(json.dumps({"enabled": True, "devices": ["Steam"], "max": 100}))
    assert load_config(p) == (MicConfig(enabled=True), [])


def test_a_bom_from_powershell_does_not_break_the_config(tmp_path):
    # Set-Content -Encoding UTF8 under PowerShell 5.1 writes a BOM; a parent's edit from
    # a PowerShell prompt must not read as a defect (same lesson as the Roblox lane).
    p = tmp_path / "mic-guard.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"enabled": False}).encode())
    assert load_config(p) == (MicConfig(enabled=False), [])


# --- one pass: every endpoint, both writes ------------------------------------

def test_every_endpoint_is_covered_not_just_the_first(tmp_path):
    # The whole reason this lane enumerates instead of taking the default: a muted
    # default microphone is worth nothing if switching input is two clicks.
    eps = [FakeEndpoint("Steam Streaming Microphone"),
           FakeEndpoint("BT Headset Hands-Free"),
           FakeEndpoint("Microphone Array")]
    assert enforce_pass(eps, tmp_path / "mic-guard.log", 0.0, {}) == 3
    assert all(e.silenced == 1 for e in eps)


def test_mute_and_zero_are_both_applied(tmp_path):
    ep = FakeEndpoint("Mic", muted=False, level=0.9)
    enforce_pass([ep], tmp_path / "mic-guard.log", 0.0, {})
    assert ep.muted is True
    assert ep.level == 0.0


def test_a_muted_endpoint_is_not_written_to_or_logged(tmp_path):
    # A quiet tick costs reads and nothing else - no COM writes, no log line.
    ep = FakeEndpoint("Mic", muted=True, level=0.0)
    assert enforce_pass([ep], tmp_path / "mic-guard.log", 0.0, {}) == 0
    assert ep.silenced == 0
    assert read_log(tmp_path) == []


def test_the_log_line_names_the_device(tmp_path):
    enforce_pass([FakeEndpoint("BT Headset Hands-Free")],
                 tmp_path / "mic-guard.log", 0.0, {})
    lines = read_log(tmp_path)
    assert len(lines) == 1
    assert "BT Headset Hands-Free" in lines[0]
    assert "[mute]" in lines[0]


def test_log_suppression_is_per_episode_not_per_tick(tmp_path):
    # A child clicking unmute is re-muted every time; the log says so once per episode.
    ep = FakeEndpoint("Mic")
    last: dict = {}
    for tick in range(int(LOG_SUPPRESS_SEC)):
        ep.muted, ep.level = False, 1.0  # unmuted again between passes
        enforce_pass([ep], tmp_path / "mic-guard.log", float(tick), last)
    assert ep.silenced == int(LOG_SUPPRESS_SEC)   # enforced every single time
    assert len(read_log(tmp_path)) == 1           # said once

    ep.muted, ep.level = False, 1.0
    enforce_pass([ep], tmp_path / "mic-guard.log", LOG_SUPPRESS_SEC + 1.0, last)
    assert len(read_log(tmp_path)) == 2           # a new episode is new news


def test_suppression_is_keyed_per_device_so_one_does_not_hide_another(tmp_path):
    a, b = FakeEndpoint("Steam", key="{0.0.1.a}"), FakeEndpoint("Headset",
                                                               key="{0.0.1.b}")
    enforce_pass([a, b], tmp_path / "mic-guard.log", 0.0, {})
    lines = read_log(tmp_path)
    assert len(lines) == 2
    assert any("Steam" in ln for ln in lines) and any("Headset" in ln for ln in lines)


# --- the loop -----------------------------------------------------------------

def test_the_loop_enumerates_every_endpoint_and_silences_them(tmp_path):
    eps = [FakeEndpoint("Steam"), FakeEndpoint("Headset")]
    calls = []

    def opener():
        calls.append(1)
        return eps

    assert mic_guard.run(tmp_path, opener=opener, sleep=lambda s: None,
                         clock=lambda: 0.0, passes=1) == 0
    assert calls == [1]
    assert all(e.silenced == 1 and e.muted and e.level == 0.0 for e in eps)


def test_enabled_false_stands_the_lane_down_without_opening_anything(tmp_path):
    (tmp_path / "mic-guard.json").write_text(json.dumps({"enabled": False}))
    opened = []

    def opener():
        opened.append(1)
        raise AssertionError("a stood-down lane must not hold COM interfaces open")

    assert mic_guard.run(tmp_path, opener=opener, sleep=lambda s: None,
                         clock=lambda: 0.0, passes=3) == 0
    assert opened == []
    assert read_log(tmp_path) == []


def test_a_broken_config_leaves_the_loop_enforcing(tmp_path):
    (tmp_path / "mic-guard.json").write_text("{enabled: false")  # a parent's typo
    ep = FakeEndpoint("Mic")
    assert mic_guard.run(tmp_path, opener=lambda: [ep], sleep=lambda s: None,
                         clock=lambda: 0.0, passes=1) == 0
    assert ep.silenced == 1
    # The warning rides along with the pass that acted, once.
    assert any("[warn]" in ln and "mic-guard.json" in ln for ln in read_log(tmp_path))


def test_an_osalert_from_com_is_a_pause_not_a_crash(tmp_path):
    # A dead loop is fifteen minutes of open microphone before the task resurrects it,
    # so every OSError has to land on release-and-re-enumerate.
    broken = FakeEndpoint("Yanked", fail=True)
    good = FakeEndpoint("Mic")
    attempts = []
    slept = []

    def opener():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise OSError("CoCreateInstance failed")
        return [broken] if len(attempts) == 2 else [good]

    ticks = iter([0.0, 10.0, 20.0, 30.0])
    assert mic_guard.run(tmp_path, opener=opener, sleep=slept.append,
                         clock=lambda: next(ticks), passes=3) == 0
    assert len(attempts) == 3            # it kept trying rather than dying
    assert good.silenced == 1            # and enforced once the device came back
    assert broken.closed >= 1            # the failed pass released what it held
    assert mic_guard.RETRY_SEC in slept  # it backed off between attempts

    lines = read_log(tmp_path)
    # One warning per failure EPISODE - and these are two distinct episodes: the second
    # pass DID re-enumerate successfully (so the recovery is announced honestly) and only
    # then lost the device mid-read. A repeated failure inside one episode stays quiet.
    assert sum("[warn] capture endpoints unavailable" in ln for ln in lines) == 2
    assert sum("[ok] capture endpoints re-enumerated" in ln for ln in lines) == 2
    assert sum("[mute] Mic" in ln for ln in lines) == 1


def test_endpoints_are_rebuilt_so_a_freshly_paired_headset_is_caught(tmp_path):
    # The headset does not exist on the first pass and is live on the second; the lane
    # must have re-enumerated by then rather than re-polling yesterday's list.
    steam = FakeEndpoint("Steam")
    headset = FakeEndpoint("BT Headset Hands-Free")
    lists = [[steam], [steam, headset]]
    ticks = iter([0.0, mic_guard.REACQUIRE_EVERY_SEC])
    assert mic_guard.run(tmp_path, opener=lambda: lists.pop(0), sleep=lambda s: None,
                         clock=lambda: next(ticks), passes=2) == 0
    assert headset.silenced == 1


def test_reacquire_is_frequent_enough_to_catch_a_new_headset():
    assert mic_guard.REACQUIRE_EVERY_SEC <= 10


def test_the_poll_keeps_the_within_a_second_promise():
    assert POLL_SEC <= 1.0
    assert LOG_SUPPRESS_SEC >= 10 * POLL_SEC


# --- the CLI off-Windows ------------------------------------------------------

def test_status_works_anywhere_and_says_the_readout_needs_windows(tmp_path, capsys):
    assert mic_guard.main(["--root", str(tmp_path), "--status"]) == 0
    out = capsys.readouterr().out
    assert "ENFORCING" in out
    assert "Windows" in out


def test_status_says_so_when_the_parent_stood_the_lane_down(tmp_path, capsys):
    (tmp_path / "mic-guard.json").write_text(json.dumps({"enabled": False}))
    assert mic_guard.main(["--root", str(tmp_path), "--status"]) == 0
    assert "DISABLED" in capsys.readouterr().out


def test_once_refuses_off_windows_rather_than_pretending(tmp_path, capsys):
    assert mic_guard.main(["--root", str(tmp_path), "--once"]) == 1
    assert "Windows" in capsys.readouterr().out


def test_the_resident_loop_refuses_off_windows_rather_than_spinning(tmp_path, capsys):
    assert mic_guard.main(["--root", str(tmp_path)]) == 1
    assert "Windows" in capsys.readouterr().out


# --- the task shape -----------------------------------------------------------

def test_the_mic_task_is_the_resident_user_loop_without_a_time_limit():
    installer = (Path(__file__).resolve().parents[1] / "install.py").read_text()
    body = installer.split("def register_mic_guard")[1].split("\ndef ")[0]
    assert "xml_template=USER_LOOP_TASK_XML" in body
    # USER_TASK_XML's PT15M execution ceiling reaps a wedged resolver run; on a resident
    # loop it would hand back an open microphone a quarter-hour after every logon.
    assert "xml_template=USER_TASK_XML" not in body
    assert "windowless" in body               # no console window on the child's screen
    assert "-m safehouse_ytbudget.mic_guard" in body
    assert 'MIC_TASK = "SafeHouse-MicGuard"' in installer
    # The installer seeds the config, or the parent has no file to edit.
    assert 'seed_json(args.root / "mic-guard.json"' in installer
    assert "register_mic_guard(python=args.python" in installer


def test_the_mic_task_runs_as_the_interactive_user_not_system(tmp_path, monkeypatch):
    import install as installmod
    import xml.etree.ElementTree as ET

    captured = {}

    def fake_run(args):
        if args[:2] == ["schtasks", "/Create"]:
            captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(
                encoding="utf-16")
        return 0, ""

    monkeypatch.setattr(installmod, "run", fake_run)
    assert installmod.register_mic_guard(
        python=tmp_path / "python.exe", workdir=tmp_path, user="DESKTOP-ABC\\child")

    doc = captured["xml"]
    assert "S-1-5-18" not in doc, "the mic guard must not run as LocalSystem"
    assert "InteractiveToken" in doc
    tree = ET.fromstring(doc)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    principal = tree.find(".//t:Principals/t:Principal", ns)
    assert principal.find("t:UserId", ns).text == "DESKTOP-ABC\\child"
    assert tree.find(".//t:Settings/t:Hidden", ns).text == "true"
    limit = tree.find(".//t:Settings/t:ExecutionTimeLimit", ns)
    assert limit.text == "PT0S"  # a resident loop must never be reaped by the scheduler


# --- the module itself --------------------------------------------------------

def test_the_module_and_its_pure_logic_run_off_windows():
    # The COM class must exist without being constructed: importing the module and
    # exercising every decision above on Linux is itself the property under test.
    assert mic_guard.CaptureEndpoint is not None
    assert mic_guard.open_capture_endpoints is not None
    assert mic_guard.E_CAPTURE == 1              # eCapture, not eRender
    assert mic_guard.DEVICE_STATE_ACTIVE == 1
    assert mic_guard.CLSID_MMDEVICE_ENUMERATOR.startswith("{")
