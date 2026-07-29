import os

import pytest

from safehouse_ytbudget import atomicio


def test_write_bytes_retries_a_transient_windows_replace_denial(tmp_path, monkeypatch):
    path = tmp_path / "hosts"
    path.write_bytes(b"before")
    real_replace = os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporarily locked")
        real_replace(source, destination)

    monkeypatch.setattr(atomicio.os, "replace", flaky_replace)
    monkeypatch.setattr(atomicio.time, "sleep", lambda _seconds: None)

    atomicio.write_bytes(path, b"after")

    assert path.read_bytes() == b"after"
    assert attempts == 3


def test_write_bytes_cleans_up_after_a_persistent_replace_denial(tmp_path, monkeypatch):
    path = tmp_path / "hosts"
    path.write_bytes(b"before")
    monkeypatch.setattr(
        atomicio.os, "replace", lambda *_args: (_ for _ in ()).throw(PermissionError())
    )
    monkeypatch.setattr(atomicio.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        atomicio.write_bytes(path, b"after")

    assert path.read_bytes() == b"before"
    assert list(tmp_path.glob(".safehouse-*.tmp")) == []
