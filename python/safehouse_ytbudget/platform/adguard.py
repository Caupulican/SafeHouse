"""Enforce the budget at the resolver, via the AdGuard Home REST API.

Why this exists, and why it is better than the hosts file:

A hosts file can only block names you can enumerate. YouTube's video bytes come from
`*.googlevideo.com`, whose left-hand label is generated per-session
(`rr3---sn-4g5ednse.googlevideo.com`), and a hosts file has no wildcards. So the hosts
layer blocks the *page* while the CDN stays reachable, and the byte metering has to chase
rotating addresses through a 32-filter cap -- which is exactly how the meter went blind
on this machine: 1.4 Mbps of traffic against 2.6 kbps of metered traffic.

AdGuard Home matches wildcards natively and ships a maintained YouTube ruleset covering
the whole surface. Blocking there needs no IP tracking at all.

Fails soft in every direction. The hosts layer stays in place as a second line, so an
unreachable resolver degrades enforcement rather than removing it.

Stdlib only -- this ships on a vendored embeddable Python.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SERVICE = "youtube"


@dataclass(frozen=True)
class AdGuardConfig:
    base_url: str
    username: str
    password: str
    service_id: str = DEFAULT_SERVICE
    timeout_sec: int = 15

    @classmethod
    def from_file(cls, path: Path) -> "AdGuardConfig | None":
        """Load credentials from JSON, or None if absent/unusable.

        Kept in its own file rather than config.json because config.json is deliberately
        world-readable (a child can see how much time is left) and this is not: the
        AdGuard dashboard can switch off all filtering. install.py restricts it to
        SYSTEM and Administrators.
        """
        if not path.exists():
            return None
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        url = str(obj.get("base_url", "")).rstrip("/")
        user = str(obj.get("username", ""))
        pw = str(obj.get("password", ""))
        if not url or not user or not pw:
            return None
        return cls(
            base_url=url,
            username=user,
            password=pw,
            service_id=str(obj.get("service_id", DEFAULT_SERVICE)),
            timeout_sec=int(obj.get("timeout_sec", 15)),
        )


class AdGuardClient:
    def __init__(self, cfg: AdGuardConfig) -> None:
        self.cfg = cfg
        self._auth = base64.b64encode(
            f"{cfg.username}:{cfg.password}".encode()
        ).decode()

    def _call(self, path: str, payload: object | None = None, method: str | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.cfg.base_url + path,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
        )
        req.add_header("Authorization", "Basic " + self._auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_sec) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()
        except (urllib.error.URLError, OSError, ValueError):
            return None, ""

    def _get_state(self) -> dict | None:
        status, body = self._call("/control/blocked_services/get")
        if status != 200:
            return None
        try:
            obj = json.loads(body)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def is_blocked(self) -> bool | None:
        """True/False, or None if AdGuard Home could not be reached."""
        state = self._get_state()
        if state is None:
            return None
        ids = state.get("ids")
        return self.cfg.service_id in ids if isinstance(ids, list) else False

    def set_blocked(self, blocked: bool) -> str:
        """Add or remove the YouTube service. Returns a short status for the log.

        Reads the current set and writes it back modified, so the schedule and any other
        blocked services a parent configured by hand are preserved. Replacing the whole
        object with a fixed list would silently discard them.
        """
        state = self._get_state()
        if state is None:
            return "UNREACHABLE"

        ids = [str(i) for i in state.get("ids", []) if isinstance(i, (str, int))]
        already = self.cfg.service_id in ids
        if blocked and already:
            return "ALREADY"
        if not blocked and not already:
            return "NONE"

        if blocked:
            ids.append(self.cfg.service_id)
        else:
            ids = [i for i in ids if i != self.cfg.service_id]

        payload = {"schedule": state.get("schedule", {}), "ids": ids}
        status, _ = self._call("/control/blocked_services/update", payload, method="PUT")
        if status != 200:
            return f"ERROR:{status}"
        return "BLOCKED" if blocked else "UNBLOCKED"


# --- which day the block belongs to ------------------------------------------
# The resolver has no memory of *why* a service is blocked, so something has to record
# it. Without this, a lift-on-startup task cannot tell "the block is left over from
# yesterday, clear it" from "the block was applied twenty minutes ago, leave it" -- and
# would hand back YouTube on the first reboot after the budget ran out.


def read_block_date(path: Path) -> str | None:
    """The local date the block was applied, or None if unknown."""
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    value = obj.get("blocked_on")
    return value if isinstance(value, str) and value else None


def write_block_date(path: Path, date: str | None) -> None:
    """Record the date the block was applied, or clear it when lifting."""
    from .. import atomicio

    atomicio.write_json(path, {"blocked_on": date} if date else {})
