# Architecture

## The problem
**Google Play Games on PC** runs Android games inside a **`crosvm`** VM (Hyper-V backed) on
Windows. The games show ads served by mobile ad SDKs. We block them at the **network layer** —
nothing is installed, rooted, or modified inside the VM.

## Data flow
```
crosvm Android VM ──DNS──▶ Windows host resolver ──▶ Pi-hole (Docker, WSL2) ──▶ Cloudflare
   (games)                  (Ethernet adapter DNS    (blocklists + regex)        (upstream)
                             = WSL eth0 IP)
```
- Pi-hole runs in Docker **inside WSL2**, DNS bound to the WSL `eth0` IP (`PIHOLE_BIND_IP`).
- The Windows **Ethernet** adapter's DNS is pointed at that IP.
- The crosvm VM NATs through the Windows host, so it inherits the host resolver → Pi-hole.

## The two gotchas this design solves

### 1. crosvm caches DNS at launch and otherwise uses its own encrypted DNS
Setting the host DNS does nothing to a **running** VM — crosvm reads host DNS only at boot and
otherwise resolves upstream via its own DoH to Google. **Fix:** point host DNS at Pi-hole, then
**restart Google Play Games**. After that, all guest DNS flows through Pi-hole (verified: Android
domains like `time.android.com` appear in the Pi-hole query log; ad domains return `0.0.0.0`).

### 2. Public blocklists miss many ad subdomains
Generic lists block `doubleclick`/`googlesyndication` but **not** `logs.ads.vungle.com`,
`sts.applovin.com`, `aax.amazon-adsystem.com`, etc. We find them with **`adhunt`** and add them
as regex rules.

## adhunt — the silent SNI hunter (`tools/`)
Pure visibility, no resident firewall:
```
pktmon (built-in)  ──▶  *.etl  ──pktmon etl2pcap──▶  *.pcapng  ──parse_cap.py──▶  ad hosts
   capture :443+:53        full packets                  Wireshark format       TLS SNI + DNS
```
`parse_cap.py` extracts the **TLS SNI** (the real HTTPS server name) from each ClientHello, so it
reveals ad servers even when the game resolves them via its own DoH or connects by IP. Found ad
hosts → add to `blocklists/regex-denylist.txt` → `scripts/load-blocklists.sh` → restart the game.

## Why not a host firewall (Portmaster, etc.)?
crosvm's encrypted DNS rides Google's **shared** IPs (same as legit game traffic), so per-IP
blocking would break the games. DNS-layer blocking + SNI-guided regex rules is precise and
"silent" (no always-on app). See git history / RUNBOOK for the reasoning trail.

## Persistence
WSL2 (NAT mode) hands out a **new eth0 IP** on most reboots, so:
- the compose binds DNS via `${PIHOLE_BIND_IP}` (injected from `.env`), so the container always starts;
- a **logon scheduled task** (`windows/`) wakes WSL, re-binds Pi-hole to the current IP, and points
  the Ethernet adapter's DNS at it — with a **router failsafe** so the internet never breaks.
