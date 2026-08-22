"""Binding, the boot task's entry point, and the command line.

The only module that knows about sockets. Everything it does with a request is to hand it
to `app.Panel` and write back what comes out.

**Where it listens, and why not `0.0.0.0`.** The parent needs the panel from a phone in
another room, so loopback alone is not enough. But this machine also has two VMware
host-only adapters and the WSL adapter, and `0.0.0.0` would publish a control surface for
every protection in the house on all of them. So it binds exactly two addresses: the
loopback, and the primary LAN address - the one on the interface holding the default
route. That address is detected at every start, because it comes from DHCP and will move
if the router is rebooted for long enough; a DHCP reservation for it is the fix, and
docs/PANEL.md says so.

**Nothing is ever drawn on the machine it runs on.** The panel runs on the child's PC
and must never be visible there: no browser is opened, no console window, no tray icon,
no message box. It runs as SYSTEM under a hidden scheduled task and its only output is
`panel.log`. The parent views it from another device on the LAN. (An auto-opened browser
would both reveal the panel to the child and be closed by the browser guard a minute
later - comic, but still a failure.) Anything printed here goes to stdout ONLY when a
person is standing at a console that already exists, which under the task is never.

**A bind failure must be loud.** Under a scheduled task with `RestartOnFailure`, a
process that exits quietly is restarted forever with nobody the wiser - the parent finds
out when they reach for the panel from another room and nothing answers. So a failed LAN
bind is logged in full and the panel keeps running on loopback, where it can still be
read from the machine itself and say what happened.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import DEFAULT_PORT, DEFAULT_ROOT
from .app import SAFE_HEADERS, Panel, Request, Response
from .audit import audit
from .auth import PasswordStore, _restrict_to_admins, generate_password
from .collect import Collector, read_panel_config

MAX_BODY = 64 * 1024
# One line an hour. The guards' rule - an idle process must not grind out a line a minute
# - applies here too, but a service that is supposed to be always alive needs SOMETHING
# in the log to show it was, or "the panel was down all afternoon" is unanswerable.
HEARTBEAT_SEC = 3600
# An address that routes over the default gateway but is never actually contacted:
# `connect` on a UDP socket only fixes the local end, so no packet is sent. TEST-NET-1
# exists for exactly this kind of use.
ROUTE_PROBE = ("192.0.2.1", 9)
# Where `set-password --generate` leaves the password for the parent to collect. It is a
# file rather than console output because nothing may be displayed on this machine and
# the task has no console; the directory ACL (SYSTEM + Administrators) is what protects
# it, and the parent is told to delete it once it is stored.
PASSWORD_HANDOFF = "panel-password.txt"


def is_elevated() -> bool:
    """Administrator on Windows, root elsewhere."""
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def require_elevation(what: str) -> None:
    if is_elevated():
        return
    raise SystemExit(
        f"{what} needs Administrator rights, and is refusing to start without them.\n"
        f"\n"
        f"  C:\\ProgramData\\SafeHouse is restricted to SYSTEM and Administrators. As a\n"
        f"  standard user this panel could show you the settings and then fail to write\n"
        f"  them - a control that silently does nothing is worse than one that refuses\n"
        f"  to start.\n"
        f"\n"
        f"Re-run from an elevated prompt (right-click -> Run as administrator), or let\n"
        f"the SafeHouse-Panel scheduled task run it as SYSTEM."
    )


def primary_lan_address() -> str | None:
    """The address on the interface holding the default route, or None.

    Not `gethostbyname(gethostname())`: on this box that answers with a VMware host-only
    address about as often as not, and binding the panel to VMnet8 would leave the parent
    unable to reach it while looking like it started fine.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(ROUTE_PROBE)
            address = sock.getsockname()[0]
    except OSError:
        return None
    return address if address and not address.startswith("127.") else None


def _handler_class(panel: Panel, log):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SafeHousePanel"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(min(length, MAX_BODY)) if length > 0 else b""
            request = Request.build(method, self.path, dict(self.headers.items()),
                                    body=body, client=self.client_address[0])
            try:
                response = panel.handle(request)
            except Exception as exc:  # noqa: BLE001 - never take the panel down
                log(f"[error] {method} {self.path} failed: {type(exc).__name__}: {exc}")
                response = Response(500, b'{"error":"internal error"}')
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in list(SAFE_HEADERS) + list(response.headers):
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            """Silent by default. An access line per poll, every 10 seconds, forever,
            would bury the mutation log this directory exists for. Failures are logged
            by `_dispatch` and by the panel itself."""

    return Handler


def _serve_on(address: str, port: int, panel: Panel, log) -> ThreadingHTTPServer | None:
    try:
        httpd = ThreadingHTTPServer((address, port), _handler_class(panel, log))
    except OSError as exc:
        log(f"[error] could not bind {address}:{port} - {type(exc).__name__}: {exc}")
        return None
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name=f"panel-{address}",
                              daemon=True)
    thread.start()
    return httpd


def serve(*, root: Path, port: int, bind: str | None, loopback_only: bool) -> int:
    require_elevation("The SafeHouse control panel")

    config = read_panel_config(root)
    # panel.json wins over the flag, deliberately: the scheduled task always passes
    # `--port`, so a parent moving the port or pinning the address must be able to do it
    # by editing one file rather than re-registering the task.
    port = int(config.get("port", port) or port)
    lan = None if loopback_only else (bind or config.get("bind") or primary_lan_address())

    def log(message: str) -> None:
        """panel.log always; the console only when there already is one.

        Under the scheduled task there is no console and nothing must appear on screen,
        so `panel.log` is the only channel. When a person runs this by hand from an
        elevated prompt, echoing the same lines saves them tailing a file.
        """
        audit(root, message)
        _echo(message)

    store = PasswordStore(root)
    hostname = socket.gethostname().lower()
    allowed = {"127.0.0.1", "localhost", hostname, hostname.split(".")[0]}
    if lan:
        allowed.add(str(lan))
    collector = Collector(root, port=port, bound=[])
    panel = Panel(collector=collector, store=store, allowed_hosts=allowed, root=root)

    servers = []
    bound = []
    loopback = _serve_on("127.0.0.1", port, panel, log)
    if loopback is not None:
        servers.append(loopback)
        bound.append("127.0.0.1")
    if lan:
        lan_server = _serve_on(str(lan), port, panel, log)
        if lan_server is not None:
            servers.append(lan_server)
            bound.append(str(lan))
        else:
            log(f"[error] the panel is NOT reachable from the house network. The LAN "
                f"address {lan} could not be bound - it has probably changed under DHCP. "
                f"Still serving on 127.0.0.1:{port} from this machine only. A DHCP "
                f"reservation on the router prevents this.")
    elif not loopback_only:
        log("[error] no LAN address could be detected (no default route?) - serving on "
            "loopback only, so the panel is not reachable from another room.")

    if not servers:
        log(f"[error] nothing could be bound on port {port}. The panel is not running.")
        return 1

    collector.bound = bound
    log(f"[start] control panel listening on "
        f"{', '.join(f'http://{address}:{port}' for address in bound)}"
        + ("" if store.configured else
           "  -- NO PASSWORD SET: run `python -m safehouse_panel set-password`"))
    if not store.configured:
        _echo("  No password is set. Until one is, the panel serves only the "
              "instructions page.")

    try:
        last = time.monotonic()
        while True:
            time.sleep(1.0)
            if time.monotonic() - last >= HEARTBEAT_SEC:
                last = time.monotonic()
                audit(root, f"[alive] listening on {', '.join(bound)}:{port}, "
                            f"{panel.sessions.count()} session(s)")
    except KeyboardInterrupt:
        log("[stop] control panel stopped from the console")
    finally:
        for httpd in servers:
            httpd.shutdown()
    return 0


def _echo(message: str) -> None:
    """Write to the console only if one is already attached. Never opens anything."""
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            print(message, flush=True)
    except (OSError, ValueError):
        pass


def set_password(root: Path, *, generate: bool, from_stdin: bool) -> int:
    """Set the panel password. Three ways in, none of them showing it on a screen.

        --generate     make one up and write it to `panel-password.txt`
        --stdin        read it from a pipe (headless setup)
        (neither)      prompt, twice, when a console is actually attached

    The generated password goes to a FILE rather than to the console, and that is the
    constraint driving it: this runs on the child's PC, where nothing may be displayed,
    and the scheduled task has no console for a parent to read anyway. The file lands in
    the SafeHouse root, whose ACL is SYSTEM+Administrators - the same protection the
    AdGuard credentials get - and docs/PANEL.md tells the parent to copy the password
    into their password manager and then delete it. Only the hash is kept for checking.
    """
    require_elevation("Setting the panel password")
    store = PasswordStore(root)
    handout = None
    if generate:
        password = generate_password()
        handout = Path(root) / PASSWORD_HANDOFF
        try:
            handout.parent.mkdir(parents=True, exist_ok=True)
            handout.write_text(
                "SafeHouse control panel password\n"
                "================================\n\n"
                f"    {password}\n\n"
                "Store this somewhere safe and DELETE THIS FILE. The panel keeps only a\n"
                "hash of it; this file is the only copy of the password itself.\n",
                encoding="utf-8")
            _restrict_to_admins(handout)
        except OSError as exc:
            print(f"Could not write {handout}: {exc}", file=sys.stderr)
            return 1
    elif from_stdin:
        password = sys.stdin.readline().strip()
        if len(password) < 8:
            print("Too short: use at least 8 characters. The password was not changed.",
                  file=sys.stderr)
            return 1
    else:
        import getpass

        try:
            password = getpass.getpass("New panel password: ")
            again = getpass.getpass("Again: ")
        except (EOFError, KeyboardInterrupt, OSError):
            print("\nNo console to prompt on. Use --generate or --stdin.",
                  file=sys.stderr)
            return 1
        if password != again:
            print("Those do not match. The password was not changed.", file=sys.stderr)
            return 1
        if len(password) < 8:
            print("Too short: use at least 8 characters. The password was not changed.",
                  file=sys.stderr)
            return 1
    store.save(password)
    # The password itself is never logged. Where to find it is.
    audit(root, "[auth] the panel password was set" + (
        f"; the new password was written to {PASSWORD_HANDOFF} - "
        f"store it and delete that file" if handout else ""))
    _echo(f"Saved (as a hash only) in {store.path}.")
    if handout:
        _echo(f"The password itself is in {handout}. Store it, then delete that file.")
    _echo("Sessions already signed in stay signed in until the panel restarts.")
    return 0


def self_test(root: Path, *, port: int) -> int:
    """Assemble one snapshot and print it, without binding anything.

    The quickest honest answer to "is the panel reading this machine correctly", and it
    works from a normal prompt: everything it touches is a read.
    """
    import json

    collector = Collector(root, port=port, bound=["(self-test, nothing bound)"])
    snapshot = collector.snapshot()
    print(json.dumps(snapshot, indent=2, default=str))
    for lane in snapshot["lanes"]:
        print(f"  {lane['label']:<14} {lane['status']:<9} {lane['reason']}",
              file=sys.stderr)
    print(f"  {'DNS':<14} {snapshot['dns']['status']:<9} {snapshot['dns']['reason']}",
          file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="safehouse_panel",
        description="SafeHouse control panel - the dashboard and the settings UI.")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "set-password", "self-test", "once"],
                        help="serve (default), set-password, or self-test/once "
                             "(print one snapshot and exit)")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=None,
                        help="LAN address to bind in addition to loopback "
                             "(default: the address holding the default route)")
    parser.add_argument("--loopback-only", action="store_true",
                        help="do not listen on the LAN at all")
    parser.add_argument("--generate", action="store_true",
                        help="set-password: generate one and write it to "
                             f"{PASSWORD_HANDOFF} in the SafeHouse root")
    parser.add_argument("--stdin", action="store_true",
                        help="set-password: read the new password from stdin (headless)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "set-password":
        return set_password(args.root, generate=args.generate,
                            from_stdin=args.stdin)
    if args.command in ("self-test", "once"):
        return self_test(args.root, port=args.port)
    return serve(root=args.root, port=args.port, bind=args.bind,
                 loopback_only=args.loopback_only)
