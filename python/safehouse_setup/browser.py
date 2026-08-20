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

# The DoH, proxy, built-in-resolver and incognito policies were all confirmed present in
# opera_browser.dll, so this build implements them rather than discarding them as unknown
# policy names. A browser that does not implement one of the others ignores it.
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
    # These two are what make "msedge.exe is running" mean "the child opened a browser".
    # Edge's Startup Boost launches it at logon and keeps it resident with no window, and
    # background mode keeps Edge and Chrome alive after the last window closes so
    # extensions can run. With either on, the browser guard (safehouse_ytbudget/
    # browser_guard.py) sees a browser that nobody opened, ends it, and Windows starts it
    # again - a permanent kill/respawn fight, a log line a minute, and a message on screen
    # telling the child browsing is off when he touched nothing.
    "StartupBoostEnabled": 0,
    "BackgroundModeEnabled": 0,
}

# List policies are registry SUBKEYS with numbered values, not flat values. URLBlocklist
# is the Shorts lane a resolver cannot provide: Shorts share every domain with the rest
# of YouTube and differ only in URL path, which is exactly what this policy matches.
# The bare pattern covers every scheme and subdomain (www., m.).
LIST_POLICIES = {
    "URLBlocklist": ["youtube.com/shorts"],
}

# The other Shorts lane: uBlock Origin with the repo's hiding list, so Shorts are not
# just dead links but absent from the page. Edge force-installs from its own store by
# bare id; Opera and Chrome need the Chrome Web Store id with an explicit update URL
# (best-effort on Opera - the policy strings are present in opera_browser.dll, like the
# DoH ones were, but a forced store fetch is less certain than a registry-read policy).
UBO_EDGE_ID = "odfafepnkmbhccpbejgmiehpchacaeak"
UBO_CWS_ID = "cjpalhdlnbpafiamejdnhcphjbkeiagm"
UBO_CWS_UPDATE = f"{UBO_CWS_ID};https://clients2.google.com/service/update2/crx"
SHORTS_LIST_URL = ("https://raw.githubusercontent.com/Caupulican/SafeHouse/"
                   "main/blocklists/youtube-shorts.txt")
# uBO reads `toOverwrite.filterLists` from managed storage; entries may be built-in
# list ids or external URLs. The defaults are restated because "overwrite" means
# exactly that - omitting them would turn the ad blocker into a Shorts-only blocker.
UBO_FILTER_LISTS = [
    "user-filters", "ublock-filters", "ublock-badware", "ublock-privacy",
    "ublock-quick-fixes", "ublock-unbreak", "easylist", "easyprivacy",
    "urlhaus-1", "plowe-0", SHORTS_LIST_URL,
]


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
        for name, entries in LIST_POLICIES.items():
            for i, entry in enumerate(entries, start=1):
                ok &= run([REG, "add", rf"{key}\{name}", "/v", str(i), "/t", "REG_SZ",
                           "/d", entry, "/f"])[0] == 0
        ok &= _apply_ubo(key)
        results[key] = ok
    return results


def _apply_ubo(key: str) -> bool:
    """Force-install uBlock Origin and pre-subscribe the Shorts hiding list.

    Both halves matter: the forcelist alone installs a stock uBO that hides no Shorts;
    the managed `toOverwrite.filterLists` alone configures an extension that is not
    there. Managed storage lives under `3rdparty\\extensions\\<id>\\policy`, and the
    extension id differs per store, so the pair is written per browser key.
    """
    edge = "Microsoft\\Edge" in key
    ext_id = UBO_EDGE_ID if edge else UBO_CWS_ID
    forced = ext_id if edge else UBO_CWS_UPDATE
    ok = run([REG, "add", rf"{key}\ExtensionInstallForcelist", "/v", "1", "/t",
              "REG_SZ", "/d", forced, "/f"])[0] == 0
    ok &= run([REG, "add", rf"{key}\3rdparty\extensions\{ext_id}\policy", "/v",
               "toOverwrite", "/t", "REG_SZ",
               "/d", json.dumps({"filterLists": UBO_FILTER_LISTS}), "/f"])[0] == 0
    return ok


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
        for name, entries in LIST_POLICIES.items():
            for i, _ in enumerate(entries, start=1):
                got = reg_query(rf"{key}\{name}", str(i))
                if got is not None:
                    values[rf"{name}\{i}"] = got
        got = reg_query(rf"{key}\ExtensionInstallForcelist", "1")
        if got is not None:
            values[r"ExtensionInstallForcelist\1"] = got
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
