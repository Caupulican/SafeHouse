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

    def known_service_ids(self) -> set[str] | None:
        """Every service id this AdGuard build understands, or None if unreachable.

        Exists to refuse a silent no-op. `set_blocked` will happily add any string to the
        blocked list and report success, so a service id the resolver has never heard of
        blocks nothing while looking exactly like a block that worked -- the same failure
        mode as an unarmed lane, which this project has already been bitten by twice.
        """
        status, body = self._call("/control/blocked_services/all")
        if status != 200:
            return None
        try:
            obj = json.loads(body)
        except ValueError:
            return None
        services = obj.get("blocked_services") if isinstance(obj, dict) else None
        if not isinstance(services, list):
            return None
        return {
            str(service["id"])
            for service in services
            if isinstance(service, dict) and service.get("id")
        }

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


DEFAULT_CREDENTIALS = "adguard.json"


def load_client(root: Path) -> tuple["AdGuardClient | None", str]:
    """Build the resolver-lane client from `root/adguard.json`.

    Returns (client, one-line status for the log). There is exactly one of these because
    the lane has to be armed at *every* entry point: the watcher enforces on a timer, but
    `ctl block` / `ctl allow` enforce too, and a parent typing `block` and getting only
    the hosts file is the same silent half-measure from the other direction.

    The status string is returned rather than logged here so the caller decides the
    prefix -- but callers must log it. The whole failure this exists to prevent was
    silent: the lane was never constructed, nothing said so, and the daily lift task
    cheerfully reported "already lifted; nothing to do" every day for a block that had
    never once been applied.
    """
    cfg = AdGuardConfig.from_file(root / DEFAULT_CREDENTIALS)
    if cfg is None:
        return None, (
            f"resolver lane DISARMED - no usable {root / DEFAULT_CREDENTIALS}. "
            "The hosts file only covers Windows applications; anything with its own "
            "resolver (the Play Games VM above all) can still reach YouTube."
        )
    client = AdGuardClient(cfg)
    reachable = client.is_blocked()
    if reachable is None:
        return client, (
            f"resolver lane ARMED but {cfg.base_url} is not answering - "
            "enforcement will retry every tick until it does"
        )
    return client, (
        f"resolver lane ARMED at {cfg.base_url} (service={cfg.service_id}, "
        f"currently {'blocking' if reachable else 'allowing'})"
    )


# --- which day the block belongs to ------------------------------------------
# The resolver has no memory of *why* a service is blocked, so something has to record
# it. Without this, a lift-on-startup task cannot tell "the block is left over from
# yesterday, clear it" from "the block was applied twenty minutes ago, leave it" -- and
# would hand back YouTube on the first reboot after the budget ran out.


# The marker also records WHO applied the block, because the resolver cannot tell a
# parent's "no, now" from the budget running out -- `blocked_services` is a flat list of
# ids with no provenance. Without this the two lanes fight: the nightly reset lifts a
# parent's deliberate block as though it were yesterday's budget, and `toggle --allow`
# either refuses to undo its own block or hands back time the budget already spent.
BUDGET_OWNER = "budget"
MANUAL_OWNER = "manual"


def _read_marker(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _read_owner_dates(path: Path) -> dict[str, str]:
    """Return every local owner and the date it acquired the resolver block.

    New markers store a date per owner because the budget and a persistent parental
    toggle may both need the same flat AdGuard service entry.  The old one-owner shape is
    still accepted in place so an upgrade never forgets why an existing block is there.
    Markers that predate owner tracking were written only by the budget watcher.
    """
    marker = _read_marker(path)
    stored = marker.get("owners")
    if isinstance(stored, dict):
        return {
            str(owner): date
            for owner, date in stored.items()
            if isinstance(owner, str) and owner
            and isinstance(date, str) and date
        }

    date = marker.get("blocked_on")
    if not isinstance(date, str) or not date:
        return {}
    owner = marker.get("owner")
    if not isinstance(owner, str) or not owner:
        owner = BUDGET_OWNER
    return {owner: date}


def read_block_date(path: Path, owner: str = BUDGET_OWNER) -> str | None:
    """The local date ``owner`` acquired the block, or None if it never did."""
    return _read_owner_dates(path).get(owner)


def read_block_owners(path: Path) -> set[str]:
    """Every local reason currently claiming the flat AdGuard service block."""
    return set(_read_owner_dates(path))


def read_block_owner(path: Path) -> str | None:
    """Compatibility view of the resolver owners.

    Callers making an ownership decision should use :func:`read_block_owners`.  This
    singular view remains for older code and reports ``manual`` when both reasons exist,
    which is the restrictive answer: an automatic reset must not lift a parent's hold.
    """
    owners = read_block_owners(path)
    if MANUAL_OWNER in owners:
        return MANUAL_OWNER
    if BUDGET_OWNER in owners:
        return BUDGET_OWNER
    return sorted(owners)[0] if owners else None


def _write_owner_dates(path: Path, owners: dict[str, str]) -> None:
    from .. import atomicio

    cleaned = {
        owner: date
        for owner, date in sorted(owners.items())
        if isinstance(owner, str) and owner and isinstance(date, str) and date
    }
    atomicio.write_json(path, {"owners": cleaned} if cleaned else {})


def add_block_owner(path: Path, date: str, owner: str) -> set[str]:
    """Add one reason without overwriting another controller's active reason."""
    owners = _read_owner_dates(path)
    owners[owner] = date
    _write_owner_dates(path, owners)
    return set(owners)


def remove_block_owner(path: Path, owner: str) -> set[str]:
    """Release one reason and return the reasons that still require the block."""
    owners = _read_owner_dates(path)
    owners.pop(owner, None)
    _write_owner_dates(path, owners)
    return set(owners)


def write_block_date(path: Path, date: str | None,
                     owner: str = BUDGET_OWNER) -> None:
    """Replace the marker with one owner, or clear every owner.

    This preserves the original public helper's semantics for migrations and explicit
    resets.  Normal enforcement uses :func:`add_block_owner` and
    :func:`remove_block_owner`, since those must not erase the other controller's claim.
    """
    _write_owner_dates(path, {owner: date} if date else {})
