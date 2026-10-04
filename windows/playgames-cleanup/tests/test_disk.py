"""Size measurement and formatting - the numbers the parent will act on."""

from __future__ import annotations

from playgames_cleanup import disk
from playgames_cleanup.disk import GIB, avd_dirs, freed, gb, snapshot, tree_bytes


def test_gb_formatting():
    assert gb(None) == "n/a"
    assert gb(0) == "0.00 GB"
    assert gb(80 * GIB) == "80.00 GB"
    assert gb(int(1.5 * GIB)) == "1.50 GB"
    assert gb(int(2.25 * GIB), signed=True) == "+2.25 GB"
    assert gb(-GIB, signed=True) == "-1.00 GB"
    assert gb(0, signed=True) == "+0.00 GB"


def test_freed_direction():
    assert freed(10 * GIB, 7 * GIB) == 3 * GIB        # smaller after = freed
    assert freed(7 * GIB, 10 * GIB) == -3 * GIB       # grew
    assert freed(None, 1) is None and freed(1, None) is None


def test_tree_bytes_sums_nested_files(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 50)
    assert tree_bytes(tmp_path, allocated=lambda p, n: n) == 150


def test_tree_bytes_uses_allocated_size(tmp_path):
    (tmp_path / "img").write_bytes(b"x" * 1000)
    assert tree_bytes(tmp_path, allocated=lambda p, n: 10) == 10


def test_tree_bytes_missing_root_is_zero(tmp_path):
    assert tree_bytes(tmp_path / "nope") == 0


def test_avd_dirs_globs_every_userdata(tmp_path):
    base = tmp_path / "Google" / "Play Games"
    (base / "userdata_prod" / "avd").mkdir(parents=True)
    (base / "userdata_beta" / "avd").mkdir(parents=True)
    (base / "userdata_x").mkdir()                      # no avd inside: ignored
    (base / "other" / "avd").mkdir(parents=True)       # not userdata_*: ignored
    found = avd_dirs(str(tmp_path))
    assert [p.parent.name for p in found] == ["userdata_beta", "userdata_prod"]


def test_avd_dirs_without_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert avd_dirs() == []


def test_snapshot_without_dirs(tmp_path):
    s = snapshot([], volume=str(tmp_path))
    assert s.avd_bytes is None and s.free_bytes and s.free_bytes > 0


def test_snapshot_with_dir(tmp_path):
    (tmp_path / "f").write_bytes(b"z" * 4096)
    s = snapshot([tmp_path])
    assert s.avd_bytes == 4096


def test_threshold_is_well_below_a_game():
    # A small game is a few hundred MB; the noise floor must sit under that.
    assert disk.SHRINK_THRESHOLD < 200 * 1024 ** 2
