"""The rules that stand between the child and an off-switch, tested without a socket.

`app.Panel` is separable from `http.server` precisely so these can be exhaustive. What is
covered here:

* no session -> no data and no change;
* a cross-site POST is refused even WITH a valid session cookie, because that is the case
  a page the child opens can actually produce;
* a Host that is not this panel is refused, which is what stops DNS rebinding;
* weakening a protection needs the password again, tightening one does not;
* failed passwords are throttled, locked out, and logged with the source address.
"""

from __future__ import annotations

import json

import pytest

from safehouse_panel import controls
from safehouse_panel.app import Panel, Request
from safehouse_panel.auth import (
    SESSION_COOKIE,
    PasswordStore,
    Sessions,
    Throttle,
    hash_password,
    verify_password,
)

PASSWORD = "correct-horse-battery"
HOST = "192.168.1.50:8787"
ORIGIN = "http://192.168.1.50:8787"


class FakeCollector:
    """Stands in for the machine. `apply` records rather than writes."""

    def __init__(self, root):
        self.root = root
        self.applied = []
        self.probed = 0
        self.values = {"roblox_limit_min": 30, "roblox_bonus_min": 0,
                       "browser_enabled": True, "browser_limit_min": None,
                       "browser_unlock_min": 0, "browser_notify": False,
                       "roblox_delay_sec": [1, 5], "browser_delay_sec": [1, 5],
                       "youtube_blocked": "BLOCKED"}

    def snapshot(self, **_):
        return {"lanes": [], "now": {"stamp": "2026-08-21 20:00:00"}}

    def control_state(self, now, client=None):
        return {"values": self.values, "ranges": {}}

    def run_probes(self, **_):
        self.probed += 1
        return {"results": []}

    def apply(self, action, value, *, now=None):
        self.applied.append((action, value))
        if action == "roblox_limit_min" and value == "lots":
            raise controls.ControlError("Roblox daily minutes must be a whole number")
        return controls.Outcome(True, f"{action} set", before=1, after=2)


@pytest.fixture()
def panel(tmp_path):
    store = PasswordStore(tmp_path)
    store.save(PASSWORD)
    clock = {"t": 1_000_000.0}
    slept = []
    made = Panel(collector=FakeCollector(tmp_path), store=store,
                 allowed_hosts={"192.168.1.50", "127.0.0.1", "localhost"},
                 sessions=Sessions(clock=lambda: clock["t"]), throttle=Throttle(),
                 clock=lambda: clock["t"], sleep=slept.append, root=tmp_path)
    made.clock_dict = clock
    made.slept = slept
    return made


def get(panel, path, *, cookie=None, host=HOST, client="192.168.1.60"):
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    return panel.handle(Request.build("GET", path, headers, client=client))


def post(panel, path, payload, *, cookie=None, origin=ORIGIN, host=HOST,
         fetch_site="same-origin", client="192.168.1.60"):
    headers = {"Host": host, "Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    if cookie:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    return panel.handle(Request.build("POST", path, headers,
                                      body=json.dumps(payload).encode(), client=client))


def body(response) -> dict:
    return json.loads(response.body.decode())


def sign_in(panel) -> str:
    response = post(panel, "/api/login", {"password": PASSWORD})
    assert response.status == 200
    cookie = dict(response.headers)["Set-Cookie"]
    return cookie.split(";")[0].split("=", 1)[1]


# --- password storage --------------------------------------------------------

def test_the_password_is_never_stored_in_the_clear(tmp_path):
    store = PasswordStore(tmp_path)
    store.save(PASSWORD)
    raw = store.path.read_text()
    assert PASSWORD not in raw
    assert store.verify(PASSWORD) and not store.verify(PASSWORD + "!")


def test_a_corrupt_auth_file_locks_the_panel_rather_than_opening_it(tmp_path):
    store = PasswordStore(tmp_path)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.configured is False
    assert store.verify("") is False and store.verify(PASSWORD) is False


def test_hashes_of_the_same_password_differ_because_the_salt_is_random():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first["hash"] != second["hash"]
    assert verify_password(first, PASSWORD) and verify_password(second, PASSWORD)


# --- sessions ----------------------------------------------------------------

def test_without_a_session_there_is_no_data_and_no_change(panel):
    assert get(panel, "/api/status").status == 401
    assert post(panel, "/api/set", {"action": "browser_enabled", "value": True}).status == 401
    assert panel.collector.applied == []


def test_a_bad_password_does_not_create_a_session(panel):
    response = post(panel, "/api/login", {"password": "guess"})
    assert response.status == 401
    assert not any(name == "Set-Cookie" for name, _ in response.headers)


def test_a_signed_in_session_reads_the_dashboard(panel):
    cookie = sign_in(panel)
    assert get(panel, "/api/status", cookie=cookie).status == 200


def test_signing_out_kills_the_session(panel):
    cookie = sign_in(panel)
    assert post(panel, "/api/logout", {}, cookie=cookie).status == 200
    assert get(panel, "/api/status", cookie=cookie).status == 401


def test_a_session_expires_when_it_is_left_alone(panel):
    cookie = sign_in(panel)
    panel.clock_dict["t"] += 5 * 3600
    assert get(panel, "/api/status", cookie=cookie).status == 401


def test_an_invented_cookie_is_not_a_session(panel):
    assert get(panel, "/api/status", cookie="not-a-real-token").status == 401


# --- cross-site --------------------------------------------------------------

def test_a_cross_site_post_is_refused_even_with_a_valid_session(panel):
    """The case that actually happens: a page the child opens fires a POST at the panel,
    and the browser attaches the cookie because cookies go by destination."""
    cookie = sign_in(panel)
    response = post(panel, "/api/set", {"action": "browser_enabled", "value": False},
                    cookie=cookie, origin="http://evil.example",
                    fetch_site="cross-site")
    assert response.status == 403
    assert panel.collector.applied == []


def test_sec_fetch_site_alone_is_enough_to_refuse(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set", {"action": "browser_enabled", "value": True},
                    cookie=cookie, origin=ORIGIN, fetch_site="same-site")
    assert response.status == 403


def test_a_mutation_without_an_origin_header_is_refused(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set", {"action": "browser_enabled", "value": True},
                    cookie=cookie, origin=None, fetch_site=None)
    assert response.status == 403
    assert "Origin" in body(response)["error"]


def test_login_itself_is_protected_from_cross_site_posts(panel):
    response = post(panel, "/api/login", {"password": PASSWORD},
                    origin="http://evil.example", fetch_site="cross-site")
    assert response.status == 403


def test_a_host_header_that_is_not_this_panel_is_refused(panel):
    """DNS rebinding: a name the attacker controls, pointed at this address."""
    response = get(panel, "/", host="panel.evil.example:8787")
    assert response.status == 421


def test_the_loopback_name_is_still_accepted(panel):
    response = get(panel, "/", host="localhost:8787")
    assert response.status == 200


# --- mutations ---------------------------------------------------------------

def test_tightening_needs_only_the_session(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set", {"action": "roblox_limit_min", "value": 0},
                    cookie=cookie)
    assert response.status == 200 and body(response)["ok"] is True
    assert panel.collector.applied == [("roblox_limit_min", 0)]


def test_weakening_without_the_password_changes_nothing(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set", {"action": "roblox_limit_min", "value": 120},
                    cookie=cookie)
    assert response.status == 403 and body(response)["needs_password"] is True
    assert panel.collector.applied == []


def test_weakening_with_the_password_goes_through(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set",
                    {"action": "roblox_limit_min", "value": 120, "password": PASSWORD},
                    cookie=cookie)
    assert response.status == 200
    assert panel.collector.applied == [("roblox_limit_min", 120)]


def test_weakening_with_the_wrong_password_changes_nothing_and_is_logged(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set",
                    {"action": "roblox_limit_min", "value": 120, "password": "nope"},
                    cookie=cookie)
    assert response.status == 401 and panel.collector.applied == []
    assert "FAILED re-authorisation" in (panel.root / "panel.log").read_text()


def test_an_unknown_control_is_refused(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set", {"action": "sudo_make_me_a_sandwich", "value": 1},
                    cookie=cookie)
    assert response.status == 400


def test_a_refused_value_is_reported_rather_than_swallowed(panel):
    cookie = sign_in(panel)
    response = post(panel, "/api/set",
                    {"action": "roblox_limit_min", "value": "lots",
                     "password": PASSWORD}, cookie=cookie)
    assert response.status == 400 and "whole number" in body(response)["error"]


def test_every_change_is_written_to_the_panel_log_with_the_source(panel):
    cookie = sign_in(panel)
    post(panel, "/api/set", {"action": "roblox_limit_min", "value": 0}, cookie=cookie)
    log = (panel.root / "panel.log").read_text()
    assert "[change] roblox_limit_min" in log
    assert "192.168.1.60" in log


def test_probes_need_a_session_and_a_same_origin_request(panel):
    cookie = sign_in(panel)
    assert post(panel, "/api/probe", {}).status == 401
    assert post(panel, "/api/probe", {}, cookie=cookie,
                origin="http://evil.example", fetch_site="cross-site").status == 403
    assert post(panel, "/api/probe", {}, cookie=cookie).status == 200
    assert panel.collector.probed == 1


# --- brute force -------------------------------------------------------------

def test_failed_logins_are_logged_with_the_address_and_stalled(panel):
    for _ in range(3):
        post(panel, "/api/login", {"password": "guess"})
    log = (panel.root / "panel.log").read_text()
    assert log.count("FAILED login from 192.168.1.60") == 3
    assert panel.slept == [1, 2, 4]  # the stall doubles per attempt


def test_repeated_failures_lock_the_source_out(panel):
    for _ in range(6):
        post(panel, "/api/login", {"password": "guess"})
    blocked = post(panel, "/api/login", {"password": PASSWORD})
    assert blocked.status == 429
    # ...and a different device on the LAN is not punished for it.
    assert post(panel, "/api/login", {"password": PASSWORD},
                client="192.168.1.99").status == 200


def test_a_lockout_expires_by_itself(panel):
    for _ in range(6):
        post(panel, "/api/login", {"password": "guess"})
    panel.clock_dict["t"] += 1000
    assert post(panel, "/api/login", {"password": PASSWORD}).status == 200


def test_a_successful_login_clears_the_failure_count(panel):
    for _ in range(3):
        post(panel, "/api/login", {"password": "guess"})
    sign_in(panel)
    assert panel.throttle.locked_for("192.168.1.60", panel.clock()) == 0


# --- pages -------------------------------------------------------------------

def test_the_page_loads_nothing_from_the_network(panel):
    """It must render with the network filtered, blocked or down - which is exactly when
    a parent reaches for it. One external stylesheet would make it a blank page during
    the outage it exists to report."""
    import re

    cookie = sign_in(panel)
    html = get(panel, "/", cookie=cookie).body.decode()
    assert "<script src" not in html and "@import" not in html and "url(http" not in html
    for target in re.findall(r'(?:src|href)="([^"]*)"', html):
        assert target.startswith(("data:", "/", "#")), target


def test_an_unconfigured_panel_serves_instructions_not_a_login(tmp_path):
    store = PasswordStore(tmp_path)
    made = Panel(collector=FakeCollector(tmp_path), store=store,
                 allowed_hosts={"127.0.0.1"}, root=tmp_path)
    html = made.handle(Request.build("GET", "/", {"Host": "127.0.0.1:8787"})).body.decode()
    assert "set-password" in html
    assert made.handle(Request.build(
        "POST", "/api/login", {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"},
        body=b'{"password":"x"}')).status == 503
