# Windows side (admin / UAC steps)

WSL can't elevate itself, so host changes run from an **elevated PowerShell** (Right-click
Start -> *Terminal (Admin)*). `Install-SafeHouse.ps1` stages one Python package and registers
its current persistence task; there are no standalone DNS or task scripts.

## 1. Arm and verify the reboot-proof localhost relay
Run this first from an elevated PowerShell:
```powershell
cd C:\SafeHouse\windows\youtube-budget
C:\SafeHouse\python\python.exe -m safehouse_setup persist
```
This registers and starts **`SafeHouse-DNS`** at logon (highest privileges), then waits until
the DNS listener on `127.0.0.1` actually answers. The relay tracks WSL's changing NAT address
without doing a blocking `wsl.exe` lookup in a DNS request. If WSL is temporarily unavailable,
it forwards to the captured LAN gateway, preserving ordinary Internet access instead of making
the Ethernet link appear disconnected.

## 2. Point Windows DNS at the verified relay
```powershell
C:\SafeHouse\python\python.exe -m safehouse_setup dns
```
This reconciles the WSL containers, proves that a known blocked name reaches a sinkhole through
the relay, and only then points IPv4 and IPv6 adapter DNS at loopback. If that proof fails, the
adapter stays on the LAN gateway and the command exits `2`: Ethernet/DNS remain usable, but the
banner correctly says that filtering—including YouTube—is not active.

Verify / test:
```powershell
Get-ScheduledTask -TaskName SafeHouse-DNS
Start-ScheduledTask -TaskName SafeHouse-DNS                       # simulate a reboot
Get-DnsClientServerAddress | Where-Object ServerAddresses         # active adapter -> loopback
Resolve-DnsName doubleclick.net -Type A                           # should return 0.0.0.0
Resolve-DnsName example.com -Type A                               # should return real addresses
```
Remove:
```powershell
Unregister-ScheduledTask -TaskName SafeHouse-DNS -Confirm:$false
Get-DnsClientNrptRule | Where-Object Comment -like 'SafeHouse:*' |
  Remove-DnsClientNrptRule -Force
Get-NetIPConfiguration | Where-Object IPv4DefaultGateway | ForEach-Object {
  Set-DnsClientServerAddress -InterfaceIndex $_.InterfaceIndex -ResetServerAddresses
}
```

## 3. Arm the connection-layer ad blocker (firewall)
For ads that bypass DNS (DoH, QUIC, cached IPs), block them at the Windows Firewall. With ads
showing in a game, run:
```powershell
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\safehouse-adblock.ps1
```
It self-elevates, captures the crosvm VM's live connections, classifies them against
`blocklists/ad-watchlist.txt`, logs to `logs/traffic.csv`, ingests confirmed ad IPs into
`blocklists/ad-ip-ranges.txt`, and builds the `SafeHouse-AdBlock` firewall group. Re-run any time
ads return. Detail: [../docs/FIREWALL.md](../docs/FIREWALL.md).

## 4. Restart Google Play Games
The crosvm VM reads DNS only at launch, so fully quit it (tray, Quit) and reopen so it picks up
Pi-hole and drops cached ad IPs.

## 5. Toggle a parental block (on demand)
`safehouse_setup toggle` applies or removes a named parental block across **both enforcement
lanes**. It is staged to `C:\SafeHouse\python -m safehouse_setup toggle` with its source host list
at `C:\SafeHouse\windows\parental-blocks\youtube.txt`. **YouTube is allowed by default** — the
block is only applied when you run it with `--block`.
```powershell
C:\SafeHouse\python\python.exe -m safehouse_setup toggle youtube --block   # apply
C:\SafeHouse\python\python.exe -m safehouse_setup toggle youtube --allow   # remove
C:\SafeHouse\python\python.exe -m safehouse_setup toggle youtube           # status
```
Every run reports both lanes, because they cover different things and fail independently:
```
YouTube:
  hosts    (Windows applications)   BLOCKED:14
  resolver (Play Games VM, phones)  BLOCKED
```
- **hosts lane** — the live Windows hosts file. Covers Windows applications that use the Windows
  resolver. It appends a `# === Parental block: YouTube (manual) ===` … `# === end YouTube block ===`
  section (one `0.0.0.0 <host>` line per host). Idempotent, and byte-preserving: it splices only
  its own section and leaves every other line (and the file's CRLF/LF line endings) untouched.
- **resolver lane** — AdGuard Home `blocked_services`. This is the one that covers the **Google
  Play Games VM**, which runs its own resolver inside crosvm and cannot see the hosts file at all,
  plus every phone and tablet on the network. A hosts-only block is one the child walks around by
  opening Play Games.
- **Exit codes:** `0` both lanes applied · `1` the host list is missing · `2` applied but the
  resolver lane is not enforcing (disarmed, unreachable, or the service id is unknown to AdGuard).
  A `2` is printed as a loud banner — it means coverage is incomplete, not that nothing happened.
- A resolver block placed here is stamped **manual**, so the nightly reset
  (`adguard_daily_reset.py`) holds it: a parent's "no, now" has no expiry. Conversely `--allow`
  refuses to lift a block the **daily budget** owns, since that would hand back time already spent
  — use `ctl grant <minutes>` for that.
- **Self-elevates** via UAC for `--block`/`--allow` (status needs no elevation).
- After a change it runs `ipconfig /flushdns`; **reload the browser/app** (hard refresh) to see it.
- The markers are a stable, documented convention, so an external undo script can
  find and remove the same sections.

**Pi-hole / network-layer alternative:** to block YouTube for the whole network instead of one machine,
uncomment the `# --- YouTube family ---` regexes in `blocklists/parental-denylist.txt` and run
`./scripts/load-blocklists.sh` (they ship commented-out, so nothing is blocked by default).

## 6. Arm the YouTube daily watch-time budget (auto, Windows-native)
A fully automatic daily watch-time limiter for YouTube that runs as a **SYSTEM Scheduled
Task** (no WSL/Pi-hole dependency). It pins IPs resolved for YouTube-family names and meters
streaming bytes with `pktmon`; once the daily budget is spent it applies the same hosts
block as §5 plus an endpoint firewall block that interrupts existing streams. A date-based reset
lifts both on the next day (correct even across shutdowns / days
powered off). Staged to
`C:\SafeHouse\windows\youtube-budget\`. **YouTube is allowed by default** — nothing blocks
until you arm it. Run in an **elevated PowerShell** (it self-elevates via UAC):
```powershell
# ARM — registers SafeHouse-YouTubeBudget-Py (AtStartup, SYSTEM, Restart=always) and starts it now:
cd C:\SafeHouse\windows\youtube-budget
C:\SafeHouse\python\python.exe .\install.py
```
It creates `C:\ProgramData\SafeHouse` with a locked-down ACL (SYSTEM + Administrators full,
**Users read-only**, so a standard user can read but not reset the budget), seeds `config.json`
(limit 180 min / sample 20 s / window 240 s), provisions the AdGuard credentials so the
resolver lane is armed, and registers the task. **Exit code 2 = armed but resolver-less**,
which leaves the Google Play Games VM uncovered.
```powershell
cd C:\SafeHouse\windows\youtube-budget
$py = 'C:\SafeHouse\python\python.exe'
& $py -m safehouse_ytbudget.cli status        # used / remaining / limit / blocked today
& $py -m safehouse_ytbudget.cli set-limit 90  # change the daily limit
& $py -m safehouse_ytbudget.cli grant 30      # +30 min today
& $py -m safehouse_ytbudget.cli allow         # lift a block now (override)
Get-ScheduledTask -TaskName SafeHouse-YouTubeBudget-Py      # verify
Unregister-ScheduledTask -TaskName SafeHouse-YouTubeBudget-Py -Confirm:$false   # disarm
```
**Migration** from the old WSL daemon: after arming the task above, retire the daemon on the
WSL host — `sudo systemctl disable --now safehouse-youtube-budget.service`. Full detail:
[youtube-budget/README.md](youtube-budget/README.md).

## Notes
- `safehouse_setup dns` contains no machine-specific interface, username, WSL path, or LAN
  resolver. `persist` captures the active adapter's DHCP resolver before SafeHouse changes it.
- The task runs as the logged-on user with highest privileges (no UAC prompt at logon).
