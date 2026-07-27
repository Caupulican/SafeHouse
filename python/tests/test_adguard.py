"""Tests for the AdGuard Home enforcement client.

Runs against a stub HTTP server rather than a live AdGuard, so it exercises the real
urllib path -- auth header, JSON body, method, status handling -- without needing the
container. The stub mirrors the shapes returned by the deployed v0.107.78.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from safehouse_ytbudget.platform.adguard import AdGuardClient, AdGuardConfig


class _State:
    def __init__(self):
        self.ids: list[str] = []
        self.schedule = {"time_zone": "America/Sao_Paulo"}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail_with: int | None = None
        self.auth_seen: str | None = None


@pytest.fixture
def server():
    state = _State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _respond(self, code, obj=None):
            body = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.auth_seen = self.headers.get("Authorization")
            state.requests.append(("GET", self.path, None))
            if state.fail_with:
                return self._respond(state.fail_with, {"message": "nope"})
            if self.path == "/control/blocked_services/get":
                return self._respond(200, {"schedule": state.schedule, "ids": state.ids})
            self._respond(404)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            state.requests.append(("PUT", self.path, payload))
            if state.fail_with:
                return self._respond(state.fail_with, {"message": "nope"})
            if self.path == "/control/blocked_services/update":
                state.ids = list(payload.get("ids", []))
                state.schedule = payload.get("schedule", {})
                return self._respond(200, {})
            self._respond(404)

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    cfg = AdGuardConfig(base_url=f"http://127.0.0.1:{port}", username="admin",
                        password="s3cret", timeout_sec=5)
    yield AdGuardClient(cfg), state
    httpd.shutdown()


# --- reading -----------------------------------------------------------------


def test_reports_not_blocked_initially(server):
    client, _ = server
    assert client.is_blocked() is False


def test_reports_blocked_when_service_present(server):
    client, state = server
    state.ids = ["youtube"]
    assert client.is_blocked() is True


def test_sends_basic_auth(server):
    client, state = server
    client.is_blocked()
    expected = "Basic " + base64.b64encode(b"admin:s3cret").decode()
    assert state.auth_seen == expected


# --- writing -----------------------------------------------------------------


def test_block_adds_the_service(server):
    client, state = server
    assert client.set_blocked(True) == "BLOCKED"
    assert state.ids == ["youtube"]
    assert client.is_blocked() is True


def test_unblock_removes_the_service(server):
    client, state = server
    state.ids = ["youtube"]
    assert client.set_blocked(False) == "UNBLOCKED"
    assert state.ids == []


def test_blocking_twice_is_reported_as_already(server):
    client, state = server
    state.ids = ["youtube"]
    assert client.set_blocked(True) == "ALREADY"
    assert [r for r in state.requests if r[0] == "PUT"] == [], "no needless write"


def test_unblocking_when_absent_is_reported_as_none(server):
    client, state = server
    assert client.set_blocked(False) == "NONE"
    assert [r for r in state.requests if r[0] == "PUT"] == []


def test_uses_put_not_post(server):
    """v0.107 rejects POST on this endpoint."""
    client, state = server
    client.set_blocked(True)
    assert any(m == "PUT" for m, _, _ in state.requests)


# --- preserving what the parent configured -----------------------------------


def test_other_blocked_services_are_preserved(server):
    """Replacing the list wholesale would silently un-block things a parent set by hand."""
    client, state = server
    state.ids = ["tiktok", "roblox"]
    client.set_blocked(True)
    assert set(state.ids) == {"tiktok", "roblox", "youtube"}
    client.set_blocked(False)
    assert set(state.ids) == {"tiktok", "roblox"}


def test_schedule_is_preserved(server):
    """The schedule carries the timezone; dropping it would move any time-of-day rule."""
    client, state = server
    state.schedule = {"time_zone": "America/Sao_Paulo", "mon": {"start": 0, "end": 100}}
    before = dict(state.schedule)
    client.set_blocked(True)
    assert state.schedule == before


# --- failing soft ------------------------------------------------------------


def test_unreachable_server_reports_rather_than_raising():
    """The hosts layer is still in place, so an unreachable resolver must degrade
    enforcement, not crash the watcher."""
    cfg = AdGuardConfig(base_url="http://127.0.0.1:9", username="a", password="b",
                        timeout_sec=1)
    client = AdGuardClient(cfg)
    assert client.is_blocked() is None
    assert client.set_blocked(True) == "UNREACHABLE"


def test_auth_failure_is_reported(server):
    client, state = server
    state.fail_with = 403
    assert client.is_blocked() is None
    assert client.set_blocked(True) == "UNREACHABLE"


def test_write_failure_is_reported(server):
    client, state = server

    calls = {"n": 0}
    original = client._call

    def flaky(path, payload=None, method=None):
        calls["n"] += 1
        if method == "PUT":
            return 500, "boom"
        return original(path, payload, method)

    client._call = flaky
    assert client.set_blocked(True) == "ERROR:500"


# --- config loading ----------------------------------------------------------


def test_config_from_file(tmp_path):
    p = tmp_path / "adguard.json"
    p.write_text(json.dumps({"base_url": "http://host:3000/", "username": "u",
                             "password": "p"}))
    cfg = AdGuardConfig.from_file(p)
    assert cfg is not None
    assert cfg.base_url == "http://host:3000"  # trailing slash stripped
    assert cfg.service_id == "youtube"


def test_config_missing_file_is_none(tmp_path):
    assert AdGuardConfig.from_file(tmp_path / "nope.json") is None


def test_config_incomplete_is_none(tmp_path):
    p = tmp_path / "adguard.json"
    p.write_text(json.dumps({"base_url": "http://host:3000"}))
    assert AdGuardConfig.from_file(p) is None


def test_config_corrupt_is_none(tmp_path):
    p = tmp_path / "adguard.json"
    p.write_text("{ not json")
    assert AdGuardConfig.from_file(p) is None


# --- arming the lane ---------------------------------------------------------
# The defect these cover was not in this module at all: every function above worked, and
# no caller ever built the client. So these assert on the loader that both entry points
# now share, and above all on it SAYING SO when the lane is not armed.


def test_load_client_returns_none_and_says_why_when_unconfigured(tmp_path):
    from safehouse_ytbudget.platform.adguard import load_client

    client, status = load_client(tmp_path)
    assert client is None
    assert "DISARMED" in status
    # The status is the only thing standing between a silent half-block and a parent
    # noticing, so it must name the consequence, not just the missing file.
    assert "adguard.json" in status
    assert "Play Games" in status


def test_load_client_arms_from_a_credentials_file(tmp_path, server):
    from safehouse_ytbudget.platform.adguard import load_client

    client, _ = server
    (tmp_path / "adguard.json").write_text(json.dumps({
        "base_url": client.cfg.base_url,
        "username": "admin",
        "password": "s3cret",
    }))
    loaded, status = load_client(tmp_path)
    assert loaded is not None
    assert "ARMED" in status
    assert loaded.set_blocked(True) == "BLOCKED"


def test_load_client_reports_an_unreachable_resolver_as_armed_but_silent(tmp_path):
    from safehouse_ytbudget.platform.adguard import load_client

    (tmp_path / "adguard.json").write_text(json.dumps({
        "base_url": "http://127.0.0.1:9",  # discard port: nothing listens
        "username": "admin",
        "password": "x",
    }))
    loaded, status = load_client(tmp_path)
    assert loaded is not None, "credentials exist, so the lane is armed and retryable"
    assert "not answering" in status


def test_load_client_treats_a_corrupt_credentials_file_as_disarmed(tmp_path):
    from safehouse_ytbudget.platform.adguard import load_client

    (tmp_path / "adguard.json").write_text("{ not json")
    client, status = load_client(tmp_path)
    assert client is None
    assert "DISARMED" in status
