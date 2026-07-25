# AdGuard Home (chained in front of Pi-hole)

Pi-hole is excellent at *what* to block (1.3M domains + the regex denylists this repo
curates). It is weak at *who* and *when* — per-client policy and time-of-day schedules.
AdGuard Home is the opposite. So we chain them instead of choosing.

```
Windows adapter DNS + NRPT ──► <WSL_IP>:53    AdGuard Home
    (UNCHANGED - still the WSL IP)                 │  per-client rules
                                                   │  Blocked Services (YouTube toggle)
                                                   │  schedules, forced SafeSearch
                                                   ▼  upstream
                                             <WSL_IP>:5353   Pi-hole
                                                   │  adlists + regex-denylist.txt
                                                   │  parental-denylist.txt
                                                   ▼
                                             1.1.1.1 / 1.0.0.1
```

The important property: **the Windows side does not change.** The adapter DNS and the
global NRPT rule already point at the WSL `eth0` IP. AdGuard Home simply takes over `:53`
on that IP and Pi-hole moves down to `:5353`. No `set-dns.ps1` re-run, no NRPT edit.

## Setup

1. **Move Pi-hole off :53.** In `~/pihole/.env` add:
   ```
   PIHOLE_DNS_PORT=5353
   ```
   then `cd ~/pihole && docker compose up -d` (recreates with the new published port).
   Verify: `dig @<WSL_IP> -p 5353 example.com` still answers.

2. **Start AdGuard Home.**
   ```bash
   mkdir -p ~/adguard && cp adguard/docker-compose.yml ~/adguard/
   cp adguard/.env.example ~/adguard/.env
   # set ADGUARD_BIND_IP to `hostname -I | cut -d' ' -f1`
   cd ~/adguard && docker compose up -d
   ```

3. **Run the wizard** at `http://localhost:3000` from Windows. When it asks for the
   listen interface choose the WSL IP; admin interface stays on 3000.

4. **Point it at Pi-hole.** Settings → DNS settings → *Upstream DNS servers*:
   ```
   <WSL_IP>:5353
   ```
   Set *Load-balancing* to *Parallel requests: off* so every query deterministically
   traverses Pi-hole. Leave *Bootstrap DNS* as `1.1.1.1`.

5. **Verify the chain.** Query something on a Pi-hole regex rule and confirm it is still
   blocked *through* AdGuard Home, and that the query appears in the Pi-hole log:
   ```bash
   dig @<WSL_IP> doubleclick.net +short     # -> 0.0.0.0
   ```
   If it resolves normally, AdGuard Home is not using Pi-hole as upstream.

## What AdGuard Home adds for parental control

- **Blocked Services → YouTube.** A maintained, one-click ruleset that covers the whole
  YouTube surface (including `googlevideo` CDN patterns a hosts file cannot wildcard).
  This is strictly better than `windows/parental-blocks/youtube.txt`.
- **Per-client rules.** Identify the son's PC by IP/MAC and apply policy to that client
  only, so the parent's machine is unaffected.
- **Schedules.** Time-of-day windows (e.g. YouTube blocked 20:00–08:00).
- **Forced SafeSearch** and adult-content categories.

## What it does NOT do — read before assuming it replaces the budget tool

AdGuard Home schedules are **time-of-day windows, not cumulative budgets.** There is no
"120 minutes per day, whenever he chooses to spend them" in AdGuard Home. Neither has
Microsoft Family Safety (see below). That cumulative budget is exactly what
`windows/youtube-budget/` exists to provide, and it remains necessary.

The intended end-state is therefore: the watcher keeps *metering* and owning the budget,
but *enforces* by toggling AdGuard Home's YouTube rule for that client over the AdGuard
Home REST API instead of splicing the Windows hosts file. That removes the two things
that break it today — the hosts-file write (which is why Avast quarantines the script)
and the hosts-file read path (which Avast's DoH bypasses anyway).

## Hard prerequisite: close the DoH bypasses

**A filtering resolver only filters the queries that reach it.** On this machine several
things resolve DNS on their own and never touch Pi-hole *or* AdGuard Home:

| Bypass | Status | Fix |
|---|---|---|
| **Avast Real Site / Secure DNS** | **RESOLVED 2026-07-25** — Avast uninstalled; Defender is now the only AV | Was serving 20k+ queries through its own `[dnsdoh]` resolver, bypassing every filter here |
| **Opera GX** | **RESOLVED 2026-07-25** — VPN forbidden in-profile and pinned off by machine policy | `windows/opera-policy.ps1` (see below) |
| Edge / Chrome secure DNS | Already disabled by machine policy (`DnsOverHttpsMode=off`) | — |
| crosvm (Play Games VM) | Uses its own DoH; covered by the firewall layer | See [FIREWALL.md](FIREWALL.md) |

### Opera GX specifically

Opera GX is a **per-user** install inside the child's profile, so the machine policies
set for Edge and Chrome do not cover it. It ships two independent bypasses: its own DoH
resolver, and a built-in free VPN that tunnels all browser traffic through Opera's
proxies — while that VPN is on, nothing in this repo sees the traffic at all.

`windows/opera-policy.ps1` (run elevated) closes both under
`HKLM\SOFTWARE\Policies\OperaSoftware\Opera`, which is out of reach of the browser's own
settings UI.

**Mind the key name.** It is `OperaSoftware` with **no space** — *not* the `Opera
Software` that Opera uses for its install path, its profile directory and its own
registry entries. Writing to the spaced key succeeds, looks correct in `regedit`, and has
no effect whatsoever. Confirmed by finding the literal wide string
`SOFTWARE\Policies\OperaSoftware\Opera` in `opera_browser.dll`, where the spaced form
does not appear at all. The script writes both, so the correct one is always covered.

All four policy names below were also confirmed present in that binary, so this build
implements them rather than discarding them as unknown:

| Policy | Effect |
|---|---|
| `DnsOverHttpsMode = off` | lookups go through the Windows resolver, hence AdGuard Home |
| `ProxyMode = direct` | removes the proxy mechanism the VPN is built on |
| `BuiltInDnsClientEnabled = 0` | use the OS resolver, not Chromium's async DNS client |
| `IncognitoModeAvailability = 1` | private windows off, so history stays reviewable |

Plus `freedom.proxy_switcher.forbidden = true` in the profile's `Preferences`
(`freedom` is Opera's internal name for the VPN), and the sidebar VPN entry hidden.

Verify by opening `opera://policy` — the policies must be listed as applied. Opera has
to be fully closed and reopened for them to load.

### Remaining hardening (not yet done)

Block outbound UDP/TCP 53 to anything except the WSL IP at the router, so a hardcoded
`8.8.8.8` cannot be used, and enable AdGuard Home's DoH-provider blocklists to kill
known DoH endpoints.
