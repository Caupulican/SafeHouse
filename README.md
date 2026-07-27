# SafeHouse

Network-level ad blocking and parental controls for a Windows family PC — including
**inside the Android games** that Google Play Games runs on the desktop, which is where
most ad blockers stop working.

Everything is infrastructure-as-code. One command rebuilds the whole stack on a fresh
machine.

```powershell
powershell -ExecutionPolicy Bypass -File .\Install-SafeHouse.ps1
```

---

## Why this exists

Blocking ads and limiting screen time on a child's PC sounds like a solved problem. In
practice, three things break every simple approach:

**1. In-game ads do not use the browser.** Google Play Games runs Android games inside a
`crosvm` virtual machine. Browser extensions never see that traffic. The ad SDKs embedded
in those games are not web advertising — they are Android SDKs, and generic web
blocklists barely touch them.

**2. Ad SDKs actively route around DNS filtering.** They resolve over their own encrypted
DNS (DoH), over QUIC, or simply reuse a cached IP address. A DNS blocklist never sees the
lookup, so the ad loads anyway.

**3. "Blocked" and "actually blocked" are different things.** This is the failure that
motivated most of this repo. A control can report success while doing nothing — a hosts
file entry that the browser bypasses via its own resolver, a time budget that meters the
wrong traffic, a filter list that was added but left disabled. Each looks fine on a
dashboard. The child just keeps watching.

SafeHouse is built around that third point. Where a design choice trades convenience for
**verifiability**, it takes verifiability, and the documentation says how to check.

---

## What it does

| Capability | Layer |
|---|---|
| Blocks ads, trackers, malware network-wide | Pi-hole + AdGuard Home (DNS) |
| Blocks **in-app / in-game ad SDKs** | AdGuard Home (Android-specific filter lists) |
| Blocks ads that skip DNS entirely | Windows Firewall (by IP, against the VM's host process) |
| **Daily YouTube watch-time budget** with automatic blocking | Windows service + hosts/firewall/resolver |
| Forced SafeSearch, parental content category | AdGuard Home |
| Closes browser DoH/VPN bypasses | Machine policy + DNS bypass blocklist |

---

## Architecture

Two independent layers. Neither is sufficient alone, and the reason each exists is the
specific thing the other cannot do.

```
                    ┌──────────────────────────────────────┐
   Windows host     │  Windows Firewall (outbound)         │  ← catches ads that
   ─────────────    │  blocks ad IPs from crosvm.exe       │    never do a DNS lookup
                    └──────────────────────────────────────┘
                                    │
   adapter DNS + NRPT rule          ▼
                    ┌──────────────────────────────────────┐
   WSL2 (Docker)    │  AdGuard Home        :53             │  ← per-client policy,
                    │   ad-SDK + game + bypass filters     │    schedules, SafeSearch
                    └──────────────┬───────────────────────┘
                                   │ upstream
                    ┌──────────────▼───────────────────────┐
                    │  Pi-hole             :5353           │  ← bulk domain blocklists
                    └──────────────┬───────────────────────┘
                                   ▼
                            1.1.1.1 / 1.0.0.1
```

### Why DNS alone is not enough

DNS filtering answers "what address is this name?" — so it can only block what asks. An
ad SDK that connects straight to a hardcoded IP never asks. Measured on a real machine:
with DNS filtering alone, roughly 20% of in-game ads still appeared.

### Why the firewall layer works on a VM

crosvm uses **userspace networking**: it terminates the guest's TCP/UDP inside the host
`crosvm.exe` process and re-originates every connection from there. So every ad server a
game talks to appears as a connection owned by a normal Windows process — and a host
firewall rule applies to it, regardless of how the guest resolved the address. The VM has
no separate network path to escape through.

```powershell
# see it for yourself
$pids = (Get-Process crosvm).Id
Get-NetTCPConnection -State Established -OwningProcess $pids | Select RemoteAddress, RemotePort
```

### Why two DNS servers

They are good at different things, and chaining is cheaper than choosing.

- **Pi-hole** — bulk domain blocking. Millions of domains, mature tooling, good logs.
- **AdGuard Home** — per-client policy, time-of-day schedules, forced SafeSearch, a
  parental category filter, and a REST API that lets the YouTube budget enforce at the
  resolver rather than by guessing IP addresses.

AdGuard owns `:53` and forwards everything it does not block to Pi-hole on `:5353`. From
Windows nothing changes: the adapter and NRPT rule point at the same address either way.

---

## Quick start

### Requirements

- Windows 10/11 with **WSL2** and a Linux distro (`wsl --install`)
- Docker inside WSL — installed automatically if missing
- Administrator rights on Windows (UAC prompt)

### Install

```powershell
git clone https://github.com/Caupulican/SafeHouse.git
cd SafeHouse
powershell -ExecutionPolicy Bypass -File .\Install-SafeHouse.ps1
```

Self-elevating and **idempotent** — it is also the upgrade and repair path.

```powershell
.\Install-SafeHouse.ps1 -LimitMinutes 120   # 2-hour budget instead of the 3-hour default
.\Install-SafeHouse.ps1 -WhatIf             # show every step, change nothing
.\Install-SafeHouse.ps1 -SkipWsl            # repair only the Windows half
```

### Linux/WSL side only

```bash
./scripts/deploy.sh                 # Pi-hole only
./scripts/deploy.sh --with-adguard  # the full chain
```

### After installing

**Fully quit and reopen Google Play Games** (tray icon → Quit). The crosvm VM reads host
DNS only at launch, so it keeps using the previous resolver until restarted. This catches
people out constantly.

Verify:

```powershell
Resolve-DnsName doubleclick.net    # -> 0.0.0.0        (filtering works)
Resolve-DnsName github.com         # -> a real address (nothing else broke)
C:\SafeHouse\python\python.exe -m safehouse_ytbudget.cli status
```

---

## The YouTube watch-time budget

A cumulative daily allowance — "3 hours a day, spend it whenever" — rather than a
time-of-day window. Nothing off the shelf does this: not AdGuard Home schedules, not
Microsoft Family Safety. Both do time-of-day windows or total device screen time.

**Default: 180 minutes/day.** Set it in `windows/youtube-budget/.env`:

```ini
YB_LIMIT_MIN=180
```

then re-run `install.py`. Full key documentation in `.env.example`.

### How the metering works

It measures **bytes to the IP addresses that YouTube hostnames actually resolved to** —
not "time with the tab open", and not "traffic to Google's published ranges".

Why not those:

- **Google's published IP ranges miss the video.** Much of it is served from Google Global
  Cache nodes inside the ISP, whose addresses are not in Google's published list at all.
- **DNS-cache polling misses it too.** Browsers coalesce QUIC connections onto a warm
  address and stop re-resolving, so a stream can run for an hour with no fresh lookups.

So the watcher reads the Windows DNS-Client event channel for YouTube-family lookups,
pins the answers, and meters inbound bytes to exactly those addresses with `pktmon`.
A pin is kept alive by **traffic, not DNS recency**, which is what survives the QUIC
behaviour above.

> **Known limitation, stated plainly.** `pktmon` allows 32 filters, so only 32 addresses
> can be metered at once. Video comes from `*.googlevideo.com`; thumbnails, avatars and
> the API resolve far more often. Early versions let the chatty endpoints crowd the video
> endpoint out of those 32 slots, and the meter went blind — 1.4 Mbps flowing while the
> metered addresses carried 2.6 kbps. Video endpoints are now prioritised in the cap. If
> you extend this, keep that ordering.

### Control

```powershell
cd C:\SafeHouse\windows\youtube-budget
$py = 'C:\SafeHouse\python\python.exe'

& $py -m safehouse_ytbudget.cli status            # used / limit / remaining / what is blocking
& $py -m safehouse_ytbudget.cli grant 30          # +30 minutes, today only
& $py -m safehouse_ytbudget.cli set-limit 120     # change the daily limit
& $py -m safehouse_ytbudget.cli block             # block now and hold it
& $py -m safehouse_ytbudget.cli allow             # lift the block
& $py -m safehouse_ytbudget.cli reset             # zero today's usage
```

Enforcement **reconciles every tick**, so if something removes the block it goes back.
That matters: the original bug in this tool was latching on the transition, so once
"blocked" was recorded, nothing ever re-checked it.

### Blocking happens on two lanes, and you need both

| Lane | Covers | Blind to |
|---|---|---|
| **Hosts file** | programs using the Windows resolver — Edge, Chrome, Opera GX | anything with its own resolver; wildcard CDN names |
| **AdGuard Home** | every client of the DNS chain, including the **Google Play Games VM**, phones, tablets | clients that do not use the chain |

A hosts file has no wildcards, so it cannot express `*.googlevideo.com` — the per-session
hostnames that carry the actual video. And it is invisible to the Google Play Games VM,
which runs its own resolver inside `crosvm` and never reads it. The resolver lane covers
both, and needs no IP tracking to do it.

`status` prints a **`Resolver`** line for exactly this reason:

```
Hosts file : YouTube section blocked
Resolver   : AdGuard Home blocking YouTube (covers the Play Games VM)
```

If it says `NOT ARMED`, the budget is enforced for Windows applications only and YouTube
inside Play Games keeps working regardless of how much time has been spent. This is not a
hypothetical: it shipped that way. The lane existed in code and no caller ever built it,
so for a day the child watched YouTube in Play Games while every log line and every
status read `BLOCKED`. The watcher now reconciles the two lanes independently — if either
drifts open while the other still blocks, the next tick repairs it and logs `[reblock]`.

---

## Repository layout

```
SafeHouse/
  Install-SafeHouse.ps1   ← one-click installer (start here)

  adguard/                AdGuard Home: compose, filter set, non-interactive bootstrap
    filters.json            which filter lists and WHY each was chosen
    bootstrap.py            completes setup + upstream + parental settings + filters
    apply-filters.py        reconciles a running instance to filters.json
  pihole/                 Pi-hole compose + env example
  blocklists/             adlists, regex denylists, ad IP ranges, ad-network watchlist
  windows/
    youtube-budget/         the watch-time budget (PowerShell, live) + Python port
    safehouse-adblock.ps1   captures VM ad traffic and builds firewall rules
    safehouse_setup dns             adapter DNS + NRPT rule
    safehouse_setup browser        closes Opera GX's VPN and DoH
    parental-blocks/        host lists for the on-demand blocks
  python/                 Python port of the watcher (216 tests) — see docs/PYTHON-PORT.md
  ansible/                playbook + roles for the WSL side
  scripts/                deploy.sh, blocklist loading, auto-update
  tools/                  SNI ad-server hunter
  docs/                   architecture, runbook, firewall, AdGuard, the port
```

Every directory has its own README explaining what is in it and why.

---

## What this does **not** do

Stated up front, because a parental control that is trusted for something it cannot do is
worse than no control at all.

- **No per-channel or per-video filtering.** Every YouTube video comes from the same
  hostnames; the channel is in the URL path, inside encrypted HTTPS. No DNS or firewall
  rule can distinguish one creator from another. Use **YouTube Kids → Approved content
  only** for that.
- **No adult-content blocklist ships with this repo.** Not the domains, and not a URL
  pointing at them. Add one you are willing to audit — see `blocklists/adlists.txt`.
  AdGuard Home's parental category filter is enabled by default and may be enough alone.
- **It does not survive a determined, technical user with admin rights.** Anyone who can
  edit the registry, stop a service or change DNS can undo this. It is designed for a
  young child, not an adversary.
- **HTTPS content is not inspected.** Blocking is by hostname and address only. This is a
  deliberate choice: the alternative means installing a root certificate and intercepting
  TLS on a child's machine, which weakens the security of everything else they do.
- **A VPN defeats all of it.** Any VPN tunnels around every DNS and firewall control here.
  `python -m safehouse_setup browser` closes Opera GX's built-in one specifically; a VPN the user
  installs deliberately is out of scope.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Ads still in Play Games | The VM was not restarted after a DNS change — quit Play Games from the tray and reopen |
| Ads with no DNS query in the log | An SDK using a hardcoded IP. Run `safehouse-adblock.ps1` while the ad is on screen |
| Everything blocked, nothing loads | A filter list is over-blocking. Check the AdGuard query log; it names the rule |
| Budget never blocks | Check `video=` in `youtube-budget.log`. If it stays 0 during playback the meter is not seeing the stream |
| Budget blocks too early | The 240s window keeps the clock running through pauses. Tune `YB_WINDOW_SEC` |
| Watcher disappears | Antivirus quarantined it. Add an exclusion for `C:\SafeHouse` in that product |

Logs live in `C:\ProgramData\SafeHouse\`.

---

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how the layers fit together
- [docs/ADGUARD.md](docs/ADGUARD.md) — the DNS chain and the bypasses it must close
- [docs/FIREWALL.md](docs/FIREWALL.md) — connection-layer blocking for the VM
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — day-to-day operations
- [docs/PYTHON-PORT.md](docs/PYTHON-PORT.md) — the in-progress Python rewrite

## License

MIT. See [LICENSE](LICENSE).

This is a personal project shared in case it is useful. It ships no blocklist of adult
material and makes no claim to be a complete child-safety solution — it is one layer,
and it is honest about where that layer ends.
