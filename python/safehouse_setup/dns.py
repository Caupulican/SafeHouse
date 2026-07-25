"""Point the Windows resolver at the WSL DNS stack, and keep it there.

Replaces windows/set-dns.ps1. Two settings, because one is not enough:

  * **Adapter DNS** — the obvious one, set with `netsh`.
  * **An NRPT rule** — the Name Resolution Policy Table. Without it the adapter setting
    is quietly ignored whenever the router advertises an IPv6 resolver, which most do.
    The machine then resolves around the filter and everything downstream looks broken
    for no visible reason.

NRPT is driven through `Add-DnsClientNrptRule`, invoked from Python. That cmdlet is the
supported API; hand-writing its registry keys is undocumented guesswork on the one
subsystem that must never be left broken. Python owns the logic, the verification and the
rollback — PowerShell is just the tool it calls.

**Safety property:** DNS is the one thing on this machine that must not be left broken.
Every path here verifies resolution afterwards and rolls the NRPT rule back if the
machine cannot resolve — a filtered resolver is the goal, an unreachable one is a
catastrophe.
"""

from __future__ import annotations

import re
import socket
import time

from .winutil import NETSH, REG, flush_dns, powershell, run, wsl_ip

# The rule carries this comment, which is how it is found again and removed.
NRPT_COMMENT = "SafeHouse: force the Windows DNS Client through the filtering resolver"

# Keep host browsers on the Windows DNS Client, where the NRPT rule can reach them.
# A browser with its own DoH resolver bypasses every layer in this project.
BROWSER_POLICY_KEYS = [
    r"HKLM\SOFTWARE\Policies\Google\Chrome",
    r"HKLM\SOFTWARE\Policies\Microsoft\Edge",
    # "OperaSoftware" with no space is genuinely the key Opera reads; the spaced form
    # matches its install path and is never consulted. Both are written to be safe.
    r"HKLM\SOFTWARE\Policies\OperaSoftware\Opera",
    r"HKLM\SOFTWARE\Policies\Opera Software\Opera",
]


def default_route() -> tuple[int | None, str | None]:
    """(interface index, gateway) for the active IPv4 default route.

    The gateway doubles as the failsafe resolver: if the filtering stack cannot answer,
    pointing at the router is far better than pointing at nothing.
    """
    rc, out = run([NETSH, "interface", "ipv4", "show", "route"])
    if rc != 0:
        return None, None
    best = None
    for line in out.splitlines():
        parts = line.split()
        # Prefix  Idx  Metric  Gateway/Interface ...
        if len(parts) < 6 or "0.0.0.0/0" not in parts:
            continue
        try:
            idx_pos = parts.index("0.0.0.0/0")
            metric = int(parts[idx_pos - 1])
            iface = int(parts[idx_pos + 1])
            gateway = parts[idx_pos + 2]
        except (ValueError, IndexError):
            continue
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", gateway) or gateway == "0.0.0.0":
            continue
        if best is None or metric < best[0]:
            best = (metric, iface, gateway)
    if best is None:
        return None, None
    return best[1], best[2]


def interface_name(index: int) -> str | None:
    rc, out = run([NETSH, "interface", "ipv4", "show", "interfaces"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0] == str(index):
            return parts[4].strip()
    return None


def set_adapter_dns(iface: str, server: str) -> bool:
    ok, _ = run([NETSH, "interface", "ipv4", "set", "dnsservers",
                 f"name={iface}", "static", server, "primary", "validate=no"])
    return ok == 0


def _remove_rule_script() -> str:
    return (
        "Get-DnsClientNrptRule -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Comment -eq '{NRPT_COMMENT}' }} | "
        "Remove-DnsClientNrptRule -Force -ErrorAction SilentlyContinue"
    )


def set_nrpt(server: str | None) -> bool:
    """Install (or with None, remove) the catch-all NRPT rule.

    Namespace "." matches every name, which is the point: no query may escape the
    filter, including when the router advertises an IPv6 resolver and the adapter
    setting alone is silently ignored.
    """
    powershell(_remove_rule_script())
    if server is None:
        flush_dns()
        return True
    rc, _ = powershell(
        f"{_remove_rule_script()}; "
        f"Add-DnsClientNrptRule -Namespace '.' -NameServers '{server}' "
        f"-Comment '{NRPT_COMMENT}' | Out-Null"
    )
    flush_dns()
    return rc == 0


def nrpt_servers() -> list[str]:
    """The servers our rule currently points at. Empty if the rule is absent.

    Used to verify, rather than assume, that the rule took effect.
    """
    rc, out = powershell(
        "Get-DnsClientNrptRule -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Comment -eq '{NRPT_COMMENT}' }} | "
        "Select-Object -ExpandProperty NameServers"
    )
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def disable_browser_doh() -> None:
    for key in BROWSER_POLICY_KEYS:
        run([REG, "add", key, "/v", "DnsOverHttpsMode", "/t", "REG_SZ", "/d", "off", "/f"])


def resolves(name: str = "example.com", server: str | None = None,
             timeout: float = 3.0) -> bool:
    """Can this machine resolve at all?

    Uses the OS resolver rather than querying `server` directly, because the question
    that matters is whether the *machine* works, not whether a particular server does.
    """
    if server is not None:
        # Probe the server itself with a minimal DNS query over UDP.
        return _probe_server(server, name, timeout)
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(name, None)
        return True
    except (OSError, socket.gaierror):
        return False


def _probe_server(server: str, name: str, timeout: float) -> bool:
    """Minimal DNS/A query, so no third-party resolver library is needed."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    packet = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, 53))
            data, _ = sock.recvfrom(512)
            return len(data) > 12 and data[0:2] == b"\x12\x34"
    except OSError:
        return False


def apply(prefer: str | None = None, wait_for_wsl: int = 8) -> dict:
    """Point Windows at the filtering resolver, falling back to the router.

    Returns a summary dict. Never leaves the machine unable to resolve: if the chosen
    server does not work, the NRPT rule is removed and the adapter falls back.
    """
    iface_index, gateway = default_route()
    if iface_index is None:
        raise SystemExit("No active IPv4 default route - cannot configure DNS.")
    iface = interface_name(iface_index) or str(iface_index)

    target, mode = gateway, "router-failsafe"
    ip = prefer
    if ip is None:
        for _ in range(wait_for_wsl):
            ip = wsl_ip()
            if ip:
                break
            time.sleep(3)

    if ip and _probe_server(ip, "example.com", 3.0):
        target, mode = ip, "filtering-resolver"

    set_adapter_dns(iface, target)
    set_nrpt(target)
    disable_browser_doh()
    flush_dns()

    # Verify, and undo the NRPT rule rather than leave a machine that cannot resolve.
    if not resolves():
        time.sleep(2)
        if not resolves():
            set_nrpt(None)
            if gateway:
                set_adapter_dns(iface, gateway)
            flush_dns()
            return {"mode": "rolled-back", "interface": iface, "wsl_ip": ip,
                    "dns": gateway, "ok": False}

    return {"mode": mode, "interface": iface, "wsl_ip": ip, "dns": target, "ok": True}


def clear() -> None:
    """Remove the NRPT rule. Adapter DNS is left alone deliberately - dropping it too
    would leave the machine with no resolver at all."""
    set_nrpt(None)
