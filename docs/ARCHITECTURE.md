# Architecture

## The problem
**Google Play Games on PC** runs Android games inside a **`crosvm`** VM (Hyper-V backed) on
Windows. The games show ads served by mobile ad SDKs. We block them at the **network layer**, so
nothing is installed, rooted, or modified inside the VM.

## Two layers of defense

```
                          layer 1 (DNS)                         layer 2 (connections)
crosvm Android VM --DNS--> Windows host resolver --> Pi-hole    crosvm.exe (host process)
   (games)                 (Ethernet DNS = WSL IP)  (blocklists)    |
      |                                                             v
      +-- ad SDK resolves over DoH / QUIC / cached IP -----> Windows Firewall (SafeHouse-AdBlock)
          (Pi-hole never sees this) ----------------------------> blocked here
```

- **Layer 1, Pi-hole (DNS).** Pi-hole runs in Docker inside WSL2, DNS bound to the WSL `eth0` IP
  (`PIHOLE_BIND_IP`). The Windows Ethernet adapter's DNS points at it, and the crosvm VM NATs
  through the host, so it inherits the host resolver. Ad domains resolve to `0.0.0.0`. This is
  cheap and catches most ads.
- **Layer 2, Windows Firewall (connections).** Some ad SDKs never ask Pi-hole. They resolve ad
  hostnames over their own encrypted DNS (DoH), over QUIC (UDP 443), or reuse a cached IP, so no
  DNS query reaches Pi-hole. This layer blocks those by IP at the host firewall. See
  [FIREWALL.md](FIREWALL.md).

## The gotchas this design solves

### 1. crosvm caches DNS at launch and otherwise uses its own encrypted DNS
Setting the host DNS does nothing to a **running** VM. crosvm reads host DNS only at boot and
otherwise resolves upstream via its own DoH to Google. **Fix:** point host DNS at Pi-hole, then
**restart Google Play Games**. After that, normal guest DNS flows through Pi-hole (verified:
Android domains like `time.android.com` appear in the Pi-hole query log; ad domains return
`0.0.0.0`).

### 2. Public blocklists miss many ad subdomains
Generic lists block `doubleclick` and `googlesyndication` but **not** `logs.ads.vungle.com`,
`sts.applovin.com`, `aax.amazon-adsystem.com`, and so on. We find them with **`adhunt`** and add
them as regex rules.

### 3. Some ad traffic bypasses DNS entirely
When an ad SDK resolves over DoH or QUIC, or connects to a cached IP, Pi-hole has nothing to
block. This is where layer 2 comes in. Because crosvm uses **userspace networking**, every guest
connection is re-originated from the host `crosvm.exe` process, so the host firewall can block the
ad server no matter how the VM resolved it, and the VM has no path around it.

## adhunt: the silent SNI hunter (`tools/`)
Pure visibility, no resident firewall:
```
pktmon (built-in)  -->  *.etl  --pktmon etl2pcap-->  *.pcapng  --parse_cap.py-->  ad hosts
   capture :443+:53        full packets                  Wireshark format        TLS SNI + DNS
```
`parse_cap.py` extracts the **TLS SNI** (the real HTTPS server name) from each ClientHello, so it
reveals ad servers even when the game resolves them via its own DoH or connects by IP. Use the
findings two ways: add the hostname to `blocklists/regex-denylist.txt` for the DNS layer, and add
a `host:` line to `blocklists/ad-watchlist.txt` (or a specific IP to `ad-ip-ranges.txt`) for the
firewall layer.

## The firewall blocker: `windows/safehouse-adblock.ps1`
One deterministic, self-elevating script captures the VM's current connections, classifies them
by ASN owner and reverse DNS against the watchlist, logs everything to `logs/traffic.csv`, ingests
confirmed ad IPs into `ad-ip-ranges.txt`, and rebuilds the firewall group. Re-run it any time ads
return. The watchlist holds stable ad-network identities (ASN, owner, hostname), so it keeps
catching ad servers after they rotate IPs. Full detail in [FIREWALL.md](FIREWALL.md).

## Persistence
WSL2 (NAT mode) hands out a **new eth0 IP** on most reboots, so:
- the compose binds DNS via `${PIHOLE_BIND_IP}` (injected from `.env`), so the container always
  starts;
- a **logon scheduled task** (`windows/`) wakes WSL, re-binds Pi-hole to the current IP, and points
  the Ethernet adapter's DNS at it, with a **router failsafe** so the internet never breaks;
- the firewall rules (layer 2) persist across reboots on their own, no task needed.
