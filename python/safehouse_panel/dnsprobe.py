"""Live verification: resolve a few names, then ask AdGuard what it did with them.

**Read the query log's `reason`, never the returned IP.** This is the single most
important line in the module and it has been got wrong twice on this machine in one day.
AdGuard Home does not answer a parental-control block with `0.0.0.0`; it answers with
`94.140.14.35`, its own block-page sinkhole. An IP-based check sees a routable address
come back, decides the domain resolved, and reports a *working block as a leak*. The same
trap catches `Rewrite` rules and safe-search redirection, which answer with entirely
ordinary-looking addresses. The query log's `reason` field is the resolver's own account
of the decision, and it is the only honest verdict available.

The query is sent as a raw UDP packet rather than through `socket.getaddrinfo` for two
reasons: `getaddrinfo` is answered from the Windows DNS client cache, so a cached name
never reaches AdGuard and leaves no query-log entry to read; and it hides which resolver
answered, when "which resolver answered" is exactly what a verification probe is for.

Stdlib only: `socket`, `struct`, `random`.
"""

from __future__ import annotations

import random
import socket
import struct
import time

# AdGuard's own reason strings. Anything starting with "Filtered" is a block EXCEPT
# "NotFiltered*", which is why this is an explicit set rather than a prefix test.
BLOCK_REASONS = {
    "FilteredBlackList",      # a filter list or a user rule matched
    "FilteredBlockedService", # blocked_services (the YouTube toggle's other lane)
    "FilteredParental",       # parental control - answers 94.140.14.35, see above
    "FilteredSafeBrowsing",
    "FilteredSafeSearch",
    "FilteredInvalid",
}
ALLOW_REASONS = {
    "NotFilteredNotFound",    # nothing matched: the name resolved normally
    "NotFilteredWhiteList",   # an @@ allow rule matched - allowed on purpose
    "NotFilteredError",
}
REWRITE_REASONS = {"Rewrite", "RewriteEtcHosts", "RewriteRule"}

# The domains the panel checks, and what each one proves. The control domain is not
# decoration: a run where everything comes back "blocked" usually means the resolver is
# refusing everything, and without a name that MUST resolve there is no way to tell that
# from a filter set doing its job.
#
# There is no adult-content domain in this table on purpose. This repo is public and
# carries adult coverage by referencing the HaGeZi NSFW list rather than naming domains
# in its own files (see docs/RUNBOOK.md). The panel reads one from `panel.json`
# (`{"probes": {"adult": "..."}}`) if the parent puts one there, and otherwise reports
# the adult probe as UNKNOWN with that instruction - which is honest, where a silently
# missing probe would leave a hole on the page that looks like a pass.
BUILTIN_PROBES = (
    {"key": "ads", "domain": "doubleclick.net", "expect": "BLOCKED",
     "label": "Ad network", "why": "the ad and tracker lists are loaded and matching"},
    {"key": "youtube", "domain": "www.youtube.com", "expect": "BLOCKED",
     "label": "YouTube", "why": "the standing YouTube block is live at the resolver"},
    {"key": "adult", "domain": None, "expect": "BLOCKED",
     "label": "Adult content", "why": "the NSFW list is loaded and matching"},
    {"key": "control", "domain": "example.com", "expect": "ALLOWED",
     "label": "Control (must resolve)",
     "why": "the resolver is answering at all - without this, everything blocked looks "
            "like everything working"},
)


# --- the wire ----------------------------------------------------------------

def build_query(name: str, *, qtype: int = 1, txid: int | None = None) -> bytes:
    """A minimal DNS query packet: one question, recursion desired."""
    txid = random.randint(0, 0xFFFF) if txid is None else txid
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(
        bytes([len(label)]) + label
        for label in (part.encode("idna") for part in name.rstrip(".").split(".") if part)
    ) + b"\x00"
    return header + qname + struct.pack("!HH", qtype, 1)


def parse_response(packet: bytes) -> dict:
    """Pull the rcode and any A/AAAA answers out of a response.

    Deliberately small: the addresses are shown as *context* on the page, clearly labelled
    as not being the verdict. Nothing here decides whether a domain is blocked.
    """
    if len(packet) < 12:
        return {"rcode": None, "addresses": []}
    _, flags, qdcount, ancount = struct.unpack("!HHHH", packet[:8])
    rcode = flags & 0x0F
    offset = 12
    try:
        for _ in range(qdcount):
            offset = _skip_name(packet, offset) + 4
        addresses = []
        for _ in range(ancount):
            offset = _skip_name(packet, offset)
            rtype, _, _, rdlength = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            rdata = packet[offset:offset + rdlength]
            offset += rdlength
            if rtype == 1 and len(rdata) == 4:
                addresses.append(socket.inet_ntop(socket.AF_INET, rdata))
            elif rtype == 28 and len(rdata) == 16:
                addresses.append(socket.inet_ntop(socket.AF_INET6, rdata))
    except (struct.error, IndexError, OSError, ValueError):
        return {"rcode": rcode, "addresses": []}
    return {"rcode": rcode, "addresses": addresses}


def _skip_name(packet: bytes, offset: int) -> int:
    while True:
        length = packet[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:  # compression pointer: two bytes, no further labels
            return offset + 2
        offset += length + 1


def ask(name: str, *, server: str = "127.0.0.1", port: int = 53,
        timeout: float = 3.0) -> dict:
    """Send one query and return what came back. Never raises."""
    packet = build_query(name)
    started = time.monotonic()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, port))
            data, _ = sock.recvfrom(4096)
    except OSError as exc:
        return {"answered": False, "error": f"{type(exc).__name__}: {exc}",
                "addresses": [], "rcode": None,
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    parsed = parse_response(data)
    parsed.update({"answered": True, "error": None,
                   "elapsed_ms": int((time.monotonic() - started) * 1000)})
    return parsed


def resolver_alive(server: str, port: int, *, name: str = "example.com",
                   timeout: float = 2.0) -> dict:
    """Is something answering DNS at `server:port`? Used for the Pi-hole check on 5353."""
    result = ask(name, server=server, port=port, timeout=timeout)
    return {
        "server": f"{server}:{port}",
        "status": "ACTIVE" if result.get("answered") else "UNKNOWN",
        "detail": ("answered in %dms" % result["elapsed_ms"] if result.get("answered")
                   else f"no answer ({result.get('error') or 'timeout'})"),
    }


# --- the verdict -------------------------------------------------------------

def classify(reason: str | None) -> str:
    if not reason:
        return "UNKNOWN"
    if reason in BLOCK_REASONS:
        return "BLOCKED"
    if reason in ALLOW_REASONS:
        return "ALLOWED"
    if reason in REWRITE_REASONS:
        # A rewrite is an answer the resolver made up. For a sinkhole that is a block;
        # for a local hostname it is not. Reported as its own word rather than guessed.
        return "REWRITTEN"
    return "UNKNOWN"


def _entry_name(entry: dict) -> str:
    question = entry.get("question")
    if not isinstance(question, dict):
        return ""
    # `name` on current builds, `host` on older ones. Both appear in the wild.
    raw = question.get("name") or question.get("host") or ""
    return str(raw).rstrip(".").lower()


def latest_entry(domain: str, entries: list[dict] | None) -> dict | None:
    """The most recent query-log entry for `domain`. AdGuard returns newest first."""
    if not entries:
        return None
    wanted = domain.rstrip(".").lower()
    for entry in entries:
        if _entry_name(entry) == wanted:
            return entry
    return None


def entry_rule(entry: dict) -> str:
    """The rule text AdGuard says matched, across both the old and new shapes."""
    rules = entry.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, dict) and rule.get("text"):
                return str(rule["text"])
    return str(entry.get("rule") or "")


def probe_result(probe: dict, entry: dict | None, dns: dict | None) -> dict:
    """Assemble one probe's row: verdict, whether it matches what was expected, context.

    `ok` is False whenever the verdict is not what was expected AND whenever it could not
    be determined. An unverifiable probe is not a passing probe.
    """
    domain = probe.get("domain")
    if not domain:
        return {**_row(probe), "verdict": "UNKNOWN", "ok": False, "reason": None,
                "rule": "", "addresses": [],
                "detail": "no domain configured for this probe - set "
                          '"probes": {"%s": "<domain>"} in panel.json' % probe["key"]}
    reason = str(entry.get("reason")) if entry else None
    verdict = classify(reason)
    expected = probe.get("expect", "BLOCKED")
    if entry is None:
        detail = ("the query did not reach AdGuard (nothing in its query log). "
                  "Either the resolver is not in the path, or the query log is off.")
    else:
        detail = f"AdGuard reason: {reason}"
    return {
        **_row(probe),
        "verdict": verdict,
        "ok": verdict == expected or (expected == "BLOCKED" and verdict == "REWRITTEN"),
        "reason": reason,
        "rule": entry_rule(entry) if entry else "",
        # Context only. The verdict above comes from `reason`; see the module docstring
        # for why an address like 94.140.14.35 is not evidence of anything.
        "addresses": (dns or {}).get("addresses", []),
        "detail": detail,
    }


def _row(probe: dict) -> dict:
    return {"key": probe["key"], "label": probe["label"], "domain": probe.get("domain"),
            "expect": probe.get("expect", "BLOCKED"), "why": probe.get("why", "")}


def probe_list(configured: dict | None) -> list[dict]:
    """`BUILTIN_PROBES` with any domains the parent configured filled in."""
    overrides = configured if isinstance(configured, dict) else {}
    out = []
    for probe in BUILTIN_PROBES:
        domain = overrides.get(probe["key"]) or probe["domain"]
        out.append({**probe, "domain": str(domain) if domain else None})
    return out


def run_probes(probes: list[dict], *, client, server: str = "127.0.0.1", port: int = 53,
               sleep=time.sleep, asker=ask) -> list[dict]:
    """Resolve each probe, then read AdGuard's verdict for it.

    The small pause is for the query log, which is written asynchronously: without it the
    read can land before the entry does, and a probe that worked reports "the query did
    not reach AdGuard".
    """
    results = []
    answers = {}
    for probe in probes:
        if probe.get("domain"):
            answers[probe["key"]] = asker(probe["domain"], server=server, port=port)
    if answers:
        sleep(0.4)
    for probe in probes:
        entry = None
        if probe.get("domain") and client is not None:
            entries = client.query_log(search=probe["domain"], limit=20)
            entry = latest_entry(probe["domain"], entries)
        results.append(probe_result(probe, entry, answers.get(probe["key"])))
    return results
