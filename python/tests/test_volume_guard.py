"""Tests for the master-volume ceiling.

The COM machinery only exists on Windows, so what is held down here is everything the
lane DECIDES: the pure clamp, the fail-closed config, the CLI's behaviour off-Windows,
and the task shape - a resident loop must not be registered under a template whose
execution limit would terminate it a quarter-hour after logon.

The one safety property with teeth: a broken config file or a broken driver reading must
land on the DEFAULT ceiling, never on "no cap". A typo must make the machine quieter,
not louder.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from safehouse_ytbudget import volume_guard
from safehouse_ytbudget.volume_guard import (
    DEFAULT_MAX_PERCENT,
    EPSILON,
    LOG_SUPPRESS_SEC,
    POLL_SEC,
    VolumeConfig,
    clamp_target,
    load_config,
)

CAP = 0.60


# --- the pure clamp -----------------------------------------------------------

def test_a_legal_level_is_left_alone():
    # Anywhere in 0..cap is the child's to choose; touching it would be a different
    # policy than the one written down.
    assert clamp_target(0.0, CAP) is None
    assert clamp_target(0.30, CAP) is None
    assert clamp_target(0.60, CAP) is None


def test_a_level_over_the_cap_snaps_to_the_cap():
    assert clamp_target(0.61, CAP) == CAP
    assert clamp_target(0.82, CAP) == CAP
    assert clamp_target(1.0, CAP) == CAP


def test_the_epsilon_edge_absorbs_float32_readback_jitter():
    # Windows stores the scalar as a float32: set 0.60, read back 0.5999-something or a
    # hair over. Without the band the loop would re-set the level every tick forever.
    assert clamp_target(CAP + EPSILON, CAP) is None
    assert clamp_target(CAP + EPSILON + 0.001, CAP) == CAP
    assert EPSILON < 0.01  # under what the volume UI can even express


def test_a_cap_of_one_disables_the_clamp():
    # max_percent: 100 is the parent's escape hatch: with the ceiling at the top there
    # is nothing to enforce, whatever the reading says.
    assert clamp_target(1.0, 1.0) is None
    assert clamp_target(0.5, 1.0) is None
    assert clamp_target(5.0, 1.0) is None  # disabled means disabled, even for garbage


def test_broken_driver_readings_land_on_the_cap_not_past_it():
    # A reading outside [0,1] can only be a broken driver, and NaN is the one input a
    # naive `current > cap` comparison quietly fails OPEN on. All of them snap to the
    # cap: a known-legal level, rather than trust in garbage.
    assert clamp_target(1.5, CAP) == CAP
    assert clamp_target(-0.2, CAP) == CAP
    assert clamp_target(float("nan"), CAP) == CAP
    assert clamp_target(float("inf"), CAP) == CAP
    assert clamp_target(float("-inf"), CAP) == CAP


# --- config: every defect must still enforce the default ceiling --------------

def test_the_config_fallbacks_are_the_dataclass_defaults(tmp_path):
    assert VolumeConfig() == VolumeConfig(enabled=True, max_percent=60)
    assert load_config(tmp_path / "nope.json") == (VolumeConfig(), [])
    p = tmp_path / "volume-guard.json"
    p.write_text("{}")
    assert load_config(p) == (VolumeConfig(), [])


def test_corrupt_config_still_enforces_sixty(tmp_path):
    p = tmp_path / "volume-guard.json"
    for bad in ("{broken", "[]", "null", '{"enabled": "no"}', '{"max_percent": "100"}'):
        p.write_text(bad)
        cfg, warnings = load_config(p)
        assert cfg.enabled, bad
        assert cfg.max_percent == DEFAULT_MAX_PERCENT, bad
        assert warnings, bad


def test_a_bad_max_percent_means_sixty_never_no_cap(tmp_path):
    p = tmp_path / "volume-guard.json"
    # Zero, out-of-range, floats, and the bool-is-an-int trap: none of these may be
    # obeyed, and none may fall through to "no cap".
    for bad in (0, -10, 101, 1000, 59.5, True, None, "60"):
        p.write_text(json.dumps({"max_percent": bad}))
        cfg, warnings = load_config(p)
        assert cfg.max_percent == DEFAULT_MAX_PERCENT, repr(bad)
        assert any("max_percent" in w for w in warnings), repr(bad)


def test_a_valid_config_is_honoured(tmp_path):
    p = tmp_path / "volume-guard.json"
    p.write_text(json.dumps({"enabled": False, "max_percent": 30}))
    assert load_config(p) == (VolumeConfig(enabled=False, max_percent=30), [])
    # The whole legal range, including both edges.
    for ok in (1, 60, 100):
        p.write_text(json.dumps({"max_percent": ok}))
        cfg, warnings = load_config(p)
        assert (cfg.max_percent, warnings) == (ok, [])


def test_max_percent_100_is_the_no_clamp_escape_hatch(tmp_path):
    p = tmp_path / "volume-guard.json"
    p.write_text(json.dumps({"max_percent": 100}))
    cfg, _ = load_config(p)
    assert clamp_target(1.0, cfg.max_percent / 100.0) is None


def test_a_bom_from_powershell_does_not_break_the_config(tmp_path):
    # Set-Content -Encoding UTF8 under PowerShell 5.1 writes a BOM; a parent's edit from
    # a PowerShell prompt must not read as a defect (same lesson as the Roblox lane).
    p = tmp_path / "volume-guard.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"max_percent": 40}).encode())
    assert load_config(p) == (VolumeConfig(max_percent=40), [])


# --- the CLI off-Windows ------------------------------------------------------

def test_status_works_anywhere_and_says_the_readout_needs_windows(tmp_path, capsys):
    (tmp_path / "volume-guard.json").write_text(json.dumps({"max_percent": 45}))
    assert volume_guard.main(["--root", str(tmp_path), "--status"]) == 0
    out = capsys.readouterr().out
    assert "ENFORCING" in out
    assert "45%" in out
    assert "Windows" in out  # the live readout is honest about where it can run


def test_once_refuses_off_windows_rather_than_pretending(tmp_path, capsys):
    # --once is the live-verification entry point; succeeding without having verified
    # anything would be worse than refusing.
    assert volume_guard.main(["--root", str(tmp_path), "--once"]) == 1
    assert "Windows" in capsys.readouterr().out


def test_the_resident_loop_refuses_off_windows_rather_than_spinning(tmp_path, capsys):
    assert volume_guard.main(["--root", str(tmp_path)]) == 1
    assert "Windows" in capsys.readouterr().out


# --- the promise in the numbers -----------------------------------------------

def test_the_poll_keeps_the_within_a_second_promise():
    assert POLL_SEC <= 1.0


def test_clamp_logging_is_per_episode_not_per_tick():
    # A slider drag is dozens of over-cap ticks; the suppression window has to cover an
    # entire drag comfortably, or the log becomes one line per tick of a fidgety hand.
    assert LOG_SUPPRESS_SEC >= 10 * POLL_SEC


def test_reacquire_is_frequent_enough_to_catch_new_headphones():
    # Plugging in headphones creates a NEW default endpoint at whatever level it last
    # had. The re-acquire cadence is the bound on how long that stays over the ceiling.
    assert volume_guard.REACQUIRE_EVERY_SEC <= 10


# --- the task shape -----------------------------------------------------------

def test_the_volume_task_is_the_resident_user_loop_without_a_time_limit():
    installer = (Path(__file__).resolve().parents[1] / "install.py").read_text()
    body = installer.split("def register_volume_guard")[1].split("\ndef ")[0]
    assert "xml_template=USER_LOOP_TASK_XML" in body
    # USER_TASK_XML's PT15M execution ceiling reaps a wedged resolver run; on a resident
    # loop it would terminate enforcement a quarter-hour after every logon.
    assert "xml_template=USER_TASK_XML" not in body
    assert "windowless" in body               # no console window on the child's screen
    assert "-m safehouse_ytbudget.volume_guard" in body

    template = installer.split('USER_LOOP_TASK_XML = """')[1].split('"""')[0]
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in template
    # The 15-minute repetition survives ONLY as a resurrection watchdog, which is safe
    # exactly because a fire while the loop is alive is ignored.
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in template
    assert "<Repetition><Interval>PT15M</Interval></Repetition>" in template
    assert "<LogonTrigger>" in template       # a user-session loop needs a user session
    assert "S-1-5-18" not in template         # never LocalSystem


def test_the_volume_task_runs_as_the_interactive_user_not_system(tmp_path, monkeypatch):
    import install as installmod
    import xml.etree.ElementTree as ET

    captured = {}

    def fake_run(args):
        if args[:2] == ["schtasks", "/Create"]:
            captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(
                encoding="utf-16")
        return 0, ""

    monkeypatch.setattr(installmod, "run", fake_run)
    assert installmod.register_volume_guard(
        python=tmp_path / "python.exe", workdir=tmp_path, user="DESKTOP-ABC\\child")

    doc = captured["xml"]
    assert "S-1-5-18" not in doc, "the volume guard must not run as LocalSystem"
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
    assert volume_guard.MasterVolume is not None
    assert math.isclose(volume_guard.CLSCTX_ALL, 0x17)
    assert volume_guard.CLSID_MMDEVICE_ENUMERATOR.startswith("{")
