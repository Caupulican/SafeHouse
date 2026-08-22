"""Request handling, with no socket anywhere in it.

`Panel.handle` takes a `Request` and returns a `Response`. `server.py` is the only thing
that knows about `http.server`, and it does nothing but translate between the two. That
split exists so the security rules below can be tested exhaustively without binding a
port - a test that has to start a real server is a test nobody writes enough of, and
these are the rules that stand between the child and an off-switch.

## What stops a drive-by

The panel listens on the house LAN and it mutates state. That means **any page any
browser on this network loads** can try to talk to it: a page the child opens can fire
`fetch('http://192.168.1.50:8787/api/set', {method:'POST', ...})` at it, and the
browser will attach the panel's cookies to that request because cookies are attached by
destination, not by who asked. A password alone does not stop that; a session cookie
alone actively helps it.

Three things do, and they are layered on purpose:

1. `SameSite=Strict` on the session cookie - the browser does not attach it to a request
   that started on another site. This is the primary defence, and it is enforced by the
   browser, not by us.
2. The `Origin` / `Sec-Fetch-Site` check on every mutation, here. This is the one that
   does not depend on the browser's cookie policy being what we think it is, and it is
   what catches a request forged from a page on this same LAN. **It, not the session, is
   what makes a cross-site POST fail.**
3. `Host` validation, so a DNS-rebinding trick - a name the attacker controls, pointed at
   this machine's address - cannot make the browser treat their page as same-origin.

And, separately from all of that: an action that *weakens* a protection asks for the
password again (`controls.requires_password`), so a session left open is not a one-tap
way to hand out the afternoon.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import audit as auditmod, controls, page
from .auth import SESSION_COOKIE, PasswordStore, Sessions, Throttle

SAFE_HEADERS = (
    ("Cache-Control", "no-store, no-cache, must-revalidate"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    # Everything is inline and nothing is fetched from anywhere: the page must work with
    # the network filtered, blocked or down, which is exactly when it is most needed.
    ("Content-Security-Policy",
     "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
     "img-src data:; connect-src 'self'; form-action 'self'; base-uri 'none'; "
     "frame-ancestors 'none'"),
)


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: dict
    body: bytes = b""
    client: str = "?"
    query: dict = field(default_factory=dict)

    @staticmethod
    def build(method: str, target: str, headers: dict | None = None, body: bytes = b"",
              client: str = "?") -> "Request":
        split = urlsplit(target)
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        return Request(method=method.upper(), path=split.path, headers=lowered,
                       body=body, client=client,
                       query={k: v[0] for k, v in parse_qs(split.query).items()})

    def header(self, name: str, default: str = "") -> str:
        return str(self.headers.get(name.lower(), default) or default)

    def json(self) -> dict:
        try:
            obj = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}

    def cookie(self, name: str) -> str | None:
        raw = self.header("cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie is "no cookie"
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: list = field(default_factory=list)


def json_response(status: int, payload: dict, headers: list | None = None) -> Response:
    return Response(status, json.dumps(payload).encode("utf-8"),
                    headers=list(headers or []))


class Panel:
    """Routing, authentication and the cross-site rules. Everything else is delegated."""

    def __init__(self, *, collector, store: PasswordStore, allowed_hosts: set[str],
                 sessions: Sessions | None = None, throttle: Throttle | None = None,
                 clock=time.time, sleep=time.sleep, root: Path | None = None) -> None:
        self.collector = collector
        self.store = store
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        self.sessions = sessions or Sessions()
        self.throttle = throttle or Throttle()
        self.clock = clock
        self.sleep = sleep
        self.root = Path(root or getattr(collector, "root", "."))

    # --- helpers ---------------------------------------------------------
    def _audit(self, message: str) -> None:
        auditmod.audit(self.root, message)

    def _host_ok(self, req: Request) -> bool:
        """The Host header must name this panel.

        Without this a DNS-rebinding attack works: an attacker publishes
        `panel.example.com -> 192.168.1.50`, the child's browser loads their page, and
        every request it makes is same-origin as far as the browser is concerned.
        """
        host = req.header("host").lower().strip()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in self.allowed_hosts or name in self.allowed_hosts

    def _cross_site(self, req: Request) -> str | None:
        """Why this mutation looks cross-site, or None if it is fine.

        Belt and braces, and deliberately strict: a missing `Origin` on a state-changing
        request is refused rather than trusted. Every browser sends it on a POST; the only
        callers it inconveniences are scripts, which can set it.
        """
        fetch_site = req.header("sec-fetch-site").lower()
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return f"Sec-Fetch-Site: {fetch_site}"
        origin = req.header("origin").strip()
        if not origin:
            return "no Origin header on a state-changing request"
        host = req.header("host").strip()
        if origin.lower() not in (f"http://{host.lower()}", f"https://{host.lower()}"):
            return f"Origin {origin} is not this panel"
        return None

    def _session(self, req: Request) -> str | None:
        token = req.cookie(SESSION_COOKIE)
        return token if self.sessions.validate(token) else None

    # --- routing ---------------------------------------------------------
    def handle(self, req: Request) -> Response:
        if not self._host_ok(req):
            return json_response(421, {"error": "This is not a name this panel answers to."})

        route = (req.method, req.path.rstrip("/") or "/")
        if route == ("GET", "/"):
            return self._page(req)
        if route == ("POST", "/api/login"):
            return self._login(req)
        if route == ("POST", "/api/logout"):
            return self._logout(req)
        if route == ("GET", "/api/status"):
            return self._status(req)
        if route == ("POST", "/api/probe"):
            return self._probe(req)
        if route == ("POST", "/api/set"):
            return self._set(req)
        if req.method in ("GET", "POST"):
            return json_response(404, {"error": "no such page"})
        return json_response(405, {"error": "method not allowed"})

    # --- pages -----------------------------------------------------------
    def _page(self, req: Request) -> Response:
        if not self.store.configured:
            return Response(200, page.render_setup().encode("utf-8"),
                            content_type="text/html; charset=utf-8")
        if self._session(req) is None:
            return Response(200, page.render_login().encode("utf-8"),
                            content_type="text/html; charset=utf-8")
        return Response(200, page.render_panel().encode("utf-8"),
                        content_type="text/html; charset=utf-8")

    # --- auth ------------------------------------------------------------
    def _login(self, req: Request) -> Response:
        cross = self._cross_site(req)
        if cross is not None:
            self._audit(auditmod.format_denied("login", who=req.client, why=cross))
            return json_response(403, {"error": "This request did not come from the "
                                                "panel's own page."})
        if not self.store.configured:
            return json_response(503, {"error": "No password is set on this panel yet. "
                                                "Run `python -m safehouse_panel "
                                                "set-password` on the machine."})
        now = self.clock()
        locked = self.throttle.locked_for(req.client, now)
        if locked:
            self._audit(auditmod.format_denied(
                "login", who=req.client, why=f"locked out for another {locked}s"))
            return json_response(429, {"error": f"Too many failed attempts. Try again in "
                                                f"{locked // 60 + 1} minute(s)."})
        password = str(req.json().get("password", ""))
        if not self.store.verify(password):
            delay = self.throttle.record_failure(req.client, now)
            # Logged with the source address: on this network the person most motivated
            # to guess it lives here, and a quiet failed attempt is the signal.
            self._audit(f"[auth] FAILED login from {req.client} "
                        f"(stalling {delay}s before answering)")
            self.sleep(delay)
            return json_response(401, {"error": "Wrong password."})
        self.throttle.record_success(req.client)
        token = self.sessions.create(req.client)
        self._audit(f"[auth] login from {req.client}")
        return json_response(200, {"ok": True}, headers=[("Set-Cookie", _cookie(token))])

    def _logout(self, req: Request) -> Response:
        self.sessions.revoke(req.cookie(SESSION_COOKIE))
        return json_response(200, {"ok": True},
                             headers=[("Set-Cookie", _cookie("", max_age=0))])

    def _require_session(self, req: Request) -> Response | None:
        if self._session(req) is None:
            return json_response(401, {"error": "Not signed in.", "login": True})
        return None

    # --- data ------------------------------------------------------------
    def _status(self, req: Request) -> Response:
        denied = self._require_session(req)
        if denied is not None:
            return denied
        return json_response(200, self.collector.snapshot())

    def _probe(self, req: Request) -> Response:
        denied = self._require_session(req)
        if denied is not None:
            return denied
        cross = self._cross_site(req)
        if cross is not None:
            return json_response(403, {"error": f"Cross-site request refused ({cross})."})
        return json_response(200, {"probes": self.collector.run_probes()})

    # --- mutations -------------------------------------------------------
    def _set(self, req: Request) -> Response:
        denied = self._require_session(req)
        if denied is not None:
            return denied
        cross = self._cross_site(req)
        if cross is not None:
            self._audit(auditmod.format_denied("a settings change", who=req.client,
                                               why=cross))
            return json_response(403, {"error": f"Cross-site request refused ({cross}). "
                                                f"Nothing was changed."})
        payload = req.json()
        action = str(payload.get("action", ""))
        value = payload.get("value")
        if action not in controls.ACTIONS:
            return json_response(400, {"error": f"Unknown control {action!r}."})

        now = int(self.clock())
        current = self.collector.control_state(now)["values"]
        needs = controls.requires_password(action, value, current)
        if needs:
            locked = self.throttle.locked_for(req.client, self.clock())
            if locked:
                return json_response(429, {"error": f"Too many failed attempts. Try "
                                                    f"again in {locked // 60 + 1} "
                                                    f"minute(s)."})
            supplied = str(payload.get("password", ""))
            if not supplied:
                return json_response(403, {
                    "needs_password": True,
                    "error": "This change makes a protection weaker, so it needs the "
                             "panel password again."})
            if not self.store.verify(supplied):
                delay = self.throttle.record_failure(req.client, self.clock())
                self._audit(f"[auth] FAILED re-authorisation for {action} "
                            f"from {req.client}")
                self.sleep(delay)
                return json_response(401, {"needs_password": True,
                                           "error": "Wrong password. Nothing was changed."})
            self.throttle.record_success(req.client)

        try:
            outcome = self.collector.apply(action, value, now=now)
        except controls.ControlError as exc:
            self._audit(auditmod.format_denied(action, who=req.client, why=str(exc)))
            return json_response(400, {"error": str(exc)})
        except OSError as exc:
            # Writing ProgramData as a standard user fails here rather than silently, and
            # the parent is told, because a control that quietly does nothing is worse
            # than one that says it could not.
            self._audit(f"[error] {action} could not be written: "
                        f"{type(exc).__name__}: {exc}")
            return json_response(500, {"error": f"Could not write the change: {exc}"})

        self._audit(auditmod.format_change(
            action, outcome.before, outcome.after,
            who=req.client + (" (re-authorised)" if needs else ""),
            detail=outcome.message if not outcome.ok else ""))
        return json_response(200 if outcome.ok else 502,
                             {"ok": outcome.ok, "message": outcome.message,
                              "before": outcome.before, "after": outcome.after})


def _cookie(token: str, *, max_age: int | None = None) -> str:
    """Session cookie. No `Secure`: there is no TLS here, and marking it Secure would
    stop it being sent at all over http, which is the only scheme this serves."""
    from .auth import SESSION_MAX_SEC

    age = SESSION_MAX_SEC if max_age is None else max_age
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={age}")
