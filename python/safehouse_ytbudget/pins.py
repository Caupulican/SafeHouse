"""The pinned-IP set: which addresses count as "YouTube" right now.

Metering Google's published IP ranges does not work. Much of YouTube's video is served
from Google Global Cache nodes embedded inside the ISP, whose addresses are not in
`goog.json` at all. So instead we pin the exact IPs that YouTube-family hostnames were
seen to resolve to, and meter only those. Gmail, Drive and Search never resolve to them,
so they never count.

The critical property is that a pin is kept alive by **activity, not DNS recency**.
Browsers coalesce QUIC connections onto a warm IP and then stop re-resolving the name --
video streams for an hour with no fresh lookups. Expiring on DNS recency would drop the
pin mid-stream and stop the budget while the child kept watching.

Pure: an IP address is just a string here, and time is passed in.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

from . import atomicio

# pktmon accepts at most 32 active filters, and one pin is one filter.
MAX_PINS = 32


@dataclass(frozen=True)
class Pin:
    ip: str
    first_seen: int
    last_active: int
    # Resolved from a *.googlevideo.com name, i.e. an endpoint that actually carries
    # video. Everything else in the YouTube family -- ytimg, ggpht, the API, the page
    # itself -- is small and chatty. See the eviction rule in `update()`.
    video: bool = False


def normalise_ip(raw: str) -> str | None:
    """Canonicalise an address, or None if it is not one.

    The DNS-Client channel reports IPv4 answers as IPv4-mapped IPv6 (`::ffff:1.2.3.4`)
    alongside genuine IPv6, and the same address must not be pinned twice under two
    spellings -- that would waste two of the 32 filter slots on one endpoint.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None  # a CNAME or other non-address token
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return str(addr)


def update(
    pins: list[Pin],
    resolved: list[str] | list[tuple[str, bool]],
    *,
    now: int,
    active_ips: set[str] | None = None,
    idle_expire_sec: int,
) -> tuple[list[Pin], list[Pin], list[Pin]]:
    """Fold this tick's observations into the pin set.

    `resolved` are addresses seen in DNS answers since the last tick, either bare IPs or
    `(ip, is_video)` pairs; `active_ips` are pins that carried traffic this tick.
    Returns (pins, added, evicted).

    Ordering matters: refresh activity first, then add, then expire, then cap. Expiring
    before refreshing would drop a pin that is streaming right now.
    """
    active_ips = active_ips or set()
    by_ip = {p.ip: p for p in pins}
    # Normalise to (raw, is_video) so callers may pass either shape.
    pairs: list[tuple[str, bool]] = [
        (r, False) if isinstance(r, str) else (r[0], bool(r[1])) for r in resolved
    ]

    # 1) Traffic keeps a pin alive. This is what survives QUIC coalescing.
    for ip in active_ips:
        p = by_ip.get(ip)
        if p is not None:
            by_ip[ip] = Pin(ip=p.ip, first_seen=p.first_seen, last_active=now,
                            video=p.video)

    # 2) New resolutions. A re-resolved existing pin also counts as activity: the name
    #    was looked up, so it is in use. `video` is sticky once set -- the same address
    #    is frequently re-resolved from a non-video name afterwards, and forgetting that
    #    it serves video would put it back at the front of the eviction queue.
    added: list[Pin] = []
    for raw, is_video in pairs:
        ip = normalise_ip(raw)
        if ip is None:
            continue
        existing = by_ip.get(ip)
        if existing is None:
            pin = Pin(ip=ip, first_seen=now, last_active=now, video=is_video)
            by_ip[ip] = pin
            added.append(pin)
        else:
            by_ip[ip] = Pin(ip=ip, first_seen=existing.first_seen, last_active=now,
                            video=existing.video or is_video)

    # 3) Expire the genuinely idle.
    evicted = [p for p in by_ip.values() if now - p.last_active >= idle_expire_sec]
    for p in evicted:
        del by_ip[p.ip]

    # 4) Cap at the filter limit -- VIDEO ENDPOINTS FIRST.
    #
    # This ordering is the whole point. Measured on the live machine: 1.4 Mbps flowing
    # into the box while the 32 metered addresses carried 2.6 kbps, because ytimg,
    # ggpht and the YouTube API resolve constantly and had crowded out the one
    # googlevideo endpoint actually carrying the stream. Once the browser coalesces onto
    # a QUIC connection it stops re-resolving, so the displaced endpoint never returns
    # and the meter is blind to the video entirely.
    #
    # Recency alone cannot fix this: the byte counter is an aggregate, so every pin
    # looks equally active whenever anything flows, and "least recently active" quietly
    # degrades into "least recently resolved" -- which is exactly backwards, since the
    # chatty small endpoints are the ones resolving most often.
    keep = sorted(by_ip.values(), key=lambda p: (not p.video, -p.last_active, p.ip))
    if len(keep) > MAX_PINS:
        evicted.extend(keep[MAX_PINS:])
        keep = keep[:MAX_PINS]

    # Stable order so the meter is not needlessly reconfigured between ticks.
    keep.sort(key=lambda p: p.ip)
    added = [p for p in added if p.ip in {k.ip for k in keep}]
    return keep, added, evicted


def ips(pins: list[Pin]) -> list[str]:
    return [p.ip for p in pins]


# --- persistence -------------------------------------------------------------
# Pins survive a restart so a watcher that is restarted mid-stream keeps metering
# instead of losing the budget until the names happen to be resolved again.


def load(path: Path) -> list[Pin]:
    """Read the pin file. Anything unreadable yields an empty set rather than raising:
    losing pins costs a little accuracy, a crashed watcher enforces nothing."""
    if not path.exists():
        return []
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(obj, list):
        return []

    out: list[Pin] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        ip = normalise_ip(str(item.get("ip", "")))
        if ip is None:
            continue
        try:
            first = int(item.get("first_seen", 0))
            last = int(item.get("last_active", 0))
        except (TypeError, ValueError):
            continue
        out.append(Pin(ip=ip, first_seen=first, last_active=last,
                       video=bool(item.get("video", False))))
    return out


def save(path: Path, pins: list[Pin]) -> None:
    atomicio.write_json(
        path,
        [{"ip": p.ip, "first_seen": p.first_seen, "last_active": p.last_active,
          "video": p.video} for p in pins],
    )
