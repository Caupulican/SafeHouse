"""Close the browser-level bypasses.

Replaces windows/opera-policy.ps1.

A filtering resolver only filters the queries that reach it. Two browser features route
around it entirely:

  * **Opera GX's built-in VPN**, which tunnels all browser traffic through Opera's
    proxies. While it is on, nothing in this project sees any of that traffic.
  * **Browser DNS-over-HTTPS**, which resolves names through the browser's own resolver
    instead of the Windows DNS Client, so the NRPT rule never applies.

Both are closed with machine policy, which lives in HKLM and so is not reachable from the
browser's own settings UI.

**The key name is the trap.** Opera reads
`HKLM\\SOFTWARE\\Policies\\OperaSoftware\\Opera` — *OperaSoftware*, no space. That is NOT
the `Opera Software` used by its install path, its profile directory and its non-policy
registry entries. Writing to the spaced key succeeds, looks correct in regedit, and has no
effect whatsoever. Verified by finding the unspaced string in `opera_browser.dll`, where
the spaced form does not appear at all. Both are written here so either build is covered.
"""

from __future__ import annotations

import json
from pathlib import Path

from .winutil import REG, reg_delete_key, run

OPERA_KEYS = [
    r"HKLM\SOFTWARE\Policies\OperaSoftware\Opera",   # the real one
    r"HKLM\SOFTWARE\Policies\Opera Software\Opera",  # decoy, written for safety
]
CHROMIUM_KEYS = [
    r"HKLM\SOFTWARE\Policies\Google\Chrome",
    r"HKLM\SOFTWARE\Policies\Microsoft\Edge",
]

# All four were confirmed present in opera_browser.dll, so this build implements them
# rather than discarding them as unknown policy names.
STRING_POLICIES = {
    # Send name resolution back through the Windows DNS Client, where the NRPT rule
    # and therefore the filtering resolver apply.
    "DnsOverHttpsMode": "off",
    # Opera's VPN is implemented as a browser proxy, so pinning direct connections
    # removes the mechanism it depends on.
    "ProxyMode": "direct",
}
DWORD_POLICIES = {
    # Use the OS resolver rather than Chromium's built-in async DNS client.
    "BuiltInDnsClientEnabled": 0,
    # Private windows would not bypass DNS filtering, but they do hide history from a
    # parent, which is the other half of supervising a young child.
    "IncognitoModeAvailability": 1,
}


def apply(include_chromium: bool = True) -> dict:
    """Write the policies. Returns {key: ok}."""
    results: dict[str, bool] = {}
    keys = list(OPERA_KEYS) + (CHROMIUM_KEYS if include_chromium else [])
    for key in keys:
        ok = True
        for name, value in STRING_POLICIES.items():
            ok &= run([REG, "add", key, "/v", name, "/t", "REG_SZ",
                       "/d", str(value), "/f"])[0] == 0
        for name, value in DWORD_POLICIES.items():
            ok &= run([REG, "add", key, "/v", name, "/t", "REG_DWORD",
                       "/d", str(value), "/f"])[0] == 0
        results[key] = ok
    return results


def clear() -> dict:
    return {key: reg_delete_key(key) for key in OPERA_KEYS}


def status() -> dict:
    """What is actually set, per key. Use this rather than trusting that apply() worked:
    the whole point of the key-name trap above is that a write can succeed and mean
    nothing."""
    from .winutil import reg_query

    out: dict[str, dict] = {}
    for key in OPERA_KEYS + CHROMIUM_KEYS:
        values = {}
        for name in list(STRING_POLICIES) + list(DWORD_POLICIES):
            got = reg_query(key, name)
            if got is not None:
                values[name] = got
        if values:
            out[key] = values
    return out


def is_opera_installed() -> list[Path]:
    """Opera GX installs per-user, which is why machine policy for Edge and Chrome does
    not cover it. Returns the profile directories found."""
    found = []
    users = Path(r"C:\Users")
    if not users.exists():
        return found
    for user in users.iterdir():
        if not user.is_dir():
            continue
        for candidate in (
            user / "AppData" / "Roaming" / "Opera Software",
            user / "AppData" / "Local" / "Programs" / "Opera GX",
        ):
            if candidate.exists():
                found.append(candidate)
    return found


def forbid_vpn_in_profiles() -> list[str]:
    """Set Opera's own "VPN not permitted" flag in each profile.

    `freedom` is Opera's internal name for the built-in VPN. This is the profile-level
    half; the machine policy above is the durable half, since a profile file is writable
    by the user who owns it.
    """
    touched = []
    for base in is_opera_installed():
        for prefs in base.rglob("Default/Preferences"):
            try:
                data = json.loads(prefs.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            node = data.setdefault("freedom", {}).setdefault("proxy_switcher", {})
            node["forbidden"] = True
            sidebar = data.setdefault("ui", {}).setdefault("browser", {}).setdefault("sidebar", {})
            sidebar["visible_proxy"] = 0
            try:
                prefs.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False),
                                 encoding="utf-8")
                touched.append(str(prefs))
            except OSError:
                continue
    return touched
