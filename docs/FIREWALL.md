# Connection-layer ad blocking (Windows Firewall)

This is the second line of defense behind Pi-hole. Pi-hole blocks ads at the DNS layer, which
is cheap and catches most of them. But some in-game ad SDKs never ask Pi-hole: they resolve ad
hostnames over their own encrypted DNS (DoH), over QUIC (UDP 443), or they reuse an IP they
cached earlier. Those connections leave the machine without a DNS query Pi-hole can see, so the
ad still loads. Many of these are cross-promo ads (one game advertising another), and they rotate
IPs constantly, so a hand-written IP list goes stale fast. This layer is built to handle that.

## Why a host firewall can block the VM (and the VM cannot bypass it)

Google Play Games runs the Android games inside a `crosvm` virtual machine, but crosvm uses
**userspace networking**. It terminates the guest's TCP/UDP inside the host `crosvm.exe` process
and re-originates every connection from that host process. You can see this directly:

```powershell
$pids = (Get-Process crosvm).Id
Get-NetTCPConnection -State Established -OwningProcess $pids | Select RemoteAddress, RemotePort
```

Every ad server the games talk to shows up as a connection owned by `crosvm.exe` on the Windows
host. That is the important part: because the traffic egresses through a normal host process, a
**host outbound firewall rule applies to it**, no matter how the guest resolved the address. DoH,
QUIC, and cached IPs all still have to leave through `crosvm.exe`, so the firewall catches them.
The VM has no separate network path it can use to get around the host firewall.

## One deterministic script does the whole loop

[`../windows/safehouse-adblock.ps1`](../windows/safehouse-adblock.ps1) is self-arming and
re-runnable. Each run, in order:

1. **Elevates itself** (UAC) if it is not already admin.
2. **Captures** the current outbound IPs of the `crosvm` processes from the host TCP table.
3. **Classifies** each IP by its network owner (ASN, looked up via Team Cymru over DNS) and its
   reverse DNS, against the watchlist.
4. **Logs** every observation to `logs/traffic.csv`.
5. **Ingests** the confirmed ad IPs into the ruleset file (`blocklists/ad-ip-ranges.txt`), in a
   managed `AUTO` section, deduped.
6. **Rebuilds** the Windows Firewall block group `SafeHouse-AdBlock` from the whole ruleset.

It is deterministic: the same watchlist plus the same set of live connections plus the same
ruleset file always produce the same firewall state. Run it again whenever ads come back to fold
in the new servers.

```powershell
# normal run: capture + classify + log + ingest + build firewall (self-elevates)
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\safehouse-adblock.ps1

# just rebuild the firewall from the file, no capture
... -File C:\SafeHouse\windows\safehouse-adblock.ps1 -RebuildOnly

# also pull in IPs/CIDRs from a file (for example, the SNI hunter's output), then build
... -File C:\SafeHouse\windows\safehouse-adblock.ps1 -IngestFile C:\SafeHouse\new-ips.txt
```

After it runs, **fully quit Google Play Games** (tray icon, Quit) and reopen it so the games drop
the ad SDK's cached IPs and reconnect into the now-blocked ranges.

## Surviving IP rotation: classify by identity, not address

The ad servers move, but who owns them does not. The script decides "is this an ad" from stable
identities held in [`../blocklists/ad-watchlist.txt`](../blocklists/ad-watchlist.txt):

- `asn:<number>` - the owner's AS number (most reliable; an ad network keeps its ASN).
- `owner:<text>` - matches when the ASN owner name contains the text (for example `pubmatic`).
- `host:<text>` - matches when the reverse-DNS or SNI hostname contains the text.

So when PubMatic or AppLovin shifts to a fresh IP, the next run still classifies it as an ad by
its ASN or owner name and blocks the new address. The watchlist is the thing you curate; the IP
list (`ad-ip-ranges.txt`) is largely derived from it and the live traffic.

Shared CDNs (Amazon CloudFront, Fastly, Akamai, Google, Cloudflare) are deliberately kept off the
watchlist, because they also carry legitimate game traffic. Ads fronted by those CDNs are caught
by hostname instead: run the SNI hunter (`tools/adhunt.sh`) to get the exact ad hostname, then add
a `host:` line to the watchlist or a specific IP to `ad-ip-ranges.txt`.

## Seed ruleset

The committed seed of [`../blocklists/ad-ip-ranges.txt`](../blocklists/ad-ip-ranges.txt), found
leaking live from the games:

| Range | Owner | Why it is blocked |
|---|---|---|
| `104.36.113.0/24` | PubMatic (AS62713) | Programmatic ad exchange. No legitimate game use. |
| `213.180.193.0/24` | Yandex AppMetrica | Mobile ad attribution and analytics (`report.appmetrica.yandex.net`). |
| `115.227.15.0/24` | CT-HangZhou IDC (AS58461, CN) | In-game ad SDK backend reached directly over 443. |
| `183.134.100.0/24` | CT-HangZhou IDC (AS58461, CN) | In-game ad SDK backend reached directly over 443. |

## The firewall rule

The script builds a single outbound block rule in the group `SafeHouse-AdBlock`:

- **Outbound, Action Block, Profile Any.** Blocks the host from reaching those ranges.
- **Protocol Any.** Blocks TCP and UDP, so the QUIC (UDP 443) escape route is closed as well.
- **Grouped and idempotent.** The whole group is removed and rebuilt each run, so edits and
  removals take effect cleanly.
- **Persistent.** Firewall rules survive reboots; no scheduled task is needed for this layer.

## Forcing ads onto blockable paths (QUIC and DoH)

The hardest ads do not just rotate IPs, they hide. The ad SDKs reach their servers over **QUIC
(UDP 443)** and resolve names over **their own encrypted DNS (DoH/DoT)**. That defeats all three
of the earlier ideas at once: Pi-hole never sees the DNS query, the SNI hunter cannot read an
encrypted QUIC handshake, and the servers sit on shared CDNs (Google, CloudFront, Cloudflare,
Akamai) that cannot be IP-blocked without breaking the games.

So `safehouse-adblock.ps1` also adds three rules scoped to `crosvm.exe` only:

- block **UDP 443 (QUIC)**, so HTTP/3 falls back to HTTP/2 over TCP;
- block **TCP 853 (DoT)**;
- block **TCP 443 to the well-known public DoH resolvers** (Google, Cloudflare, Quad9, AdGuard,
  OpenDNS).

With those bypass channels closed, the games fall back to **plaintext DNS through Pi-hole** (where
the blocklist catches the ad domains) and to **TCP TLS** (where the SNI hunter can read hostnames).
Legit Google traffic falls back to TCP on its own, so nothing breaks. The rules are scoped to
crosvm, so the Windows host's own browsing is untouched. Pass `-NoForceVisibility` to skip them.

## Verify

```powershell
Get-NetFirewallRule -Group 'SafeHouse-AdBlock' | Select DisplayName, Enabled, Action, Direction

# after reopening the games, the blocked ranges should not reappear as live connections:
$pids = (Get-Process crosvm).Id
Get-NetTCPConnection -State Established -OwningProcess $pids |
  Where RemoteAddress -match '^(104\.36\.113\.|213\.180\.193\.|115\.227\.15\.|183\.134\.100\.)'
# (no output = blocked)
```

## Analyze the log and refresh

```powershell
# which owners are contacted most
Import-Csv C:\SafeHouse\logs\traffic.csv | Group-Object owner | Sort-Object Count -Descending

# distinct ad IPs seen
Import-Csv C:\SafeHouse\logs\traffic.csv | Where verdict -eq 'AD' | Select -Expand ip -Unique
```

To refresh the rules, just run `safehouse-adblock.ps1` again while ads are showing. Then back the
updated ruleset into the repo: `./scripts/export-config.sh && git add blocklists && git commit`.

## Remove

```powershell
Get-NetFirewallRule -Group 'SafeHouse-AdBlock' | Remove-NetFirewallRule
```

## False positives

The two Chinese IDC ranges are ad SDK backends in the games observed here. If a game you care
about is published by a developer who legitimately hosts on that network and something breaks,
remove that one line from `ad-ip-ranges.txt` (or that `asn:` line from the watchlist) and re-run
with `-RebuildOnly`. PubMatic and Yandex AppMetrica are pure ad and telemetry and are safe to keep
blocked.
