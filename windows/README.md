# Windows side (admin / UAC steps)

WSL can't elevate itself, so these two one-time steps are done by **you** in an **elevated
PowerShell** (Right-click Start → *Terminal (Admin)*). The scripts here are staged automatically
by `bootstrap.sh` / the Ansible `windows_persistence` role:
- `safehouse_setup dns` → copied to `%USERPROFILE%\.pihole\safehouse_setup dns`
- `safehouse_setup persist`  → copied to `C:\Temp\safehouse_setup persist`

## 1. Point Windows DNS at Pi-hole (now)
Run the staged persistence script once from an elevated PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.pihole\safehouse_setup dns
```
It auto-detects the active adapter, gateway, and current WSL address. It points the adapter at
Pi-hole and installs a global Windows DNS Client NRPT rule so router-advertised IPv6 DNS cannot
silently bypass Pi-hole. Chrome and Edge secure DNS are disabled by machine policy so those
browsers stay on the enforced Windows resolver. If Pi-hole cannot answer, the script points both
the adapter and NRPT rule at the detected gateway as an internet-preserving failsafe.

## 2. Arm the reboot-proof logon task
```powershell
powershell -ExecutionPolicy Bypass -File C:\Temp\safehouse_setup persist
```
This registers **`PiholeGPG-DNS`** (runs at logon, highest privileges). On each login it wakes WSL,
re-binds Pi-hole to the current WSL IP, and refreshes the adapter DNS plus NRPT enforcement.

Verify / test:
```powershell
Get-ScheduledTask -TaskName PiholeGPG-DNS
Start-ScheduledTask -TaskName PiholeGPG-DNS                       # simulate a reboot
Get-DnsClientNrptRule | Where-Object Comment -like 'SafeHouse:*' # should name the WSL IP
Resolve-DnsName doubleclick.net -Type A                           # should return 0.0.0.0
Resolve-DnsName example.com -Type A                               # should return real addresses
Get-Content $env:USERPROFILE\.pihole\set-dns.log -Tail 3
```
Remove:
```powershell
Unregister-ScheduledTask -TaskName PiholeGPG-DNS -Confirm:$false
Get-DnsClientNrptRule | Where-Object Comment -eq 'SafeHouse: force Windows DNS Client through Pi-hole' |
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

## 5. Toggle the YouTube parental block (on demand)
`safehouse_setup toggle` applies or removes a named parental block in the **live Windows hosts file**
on demand. It is staged to `C:\SafeHouse\python -m safehouse_setup toggle` with its source host list at
`C:\SafeHouse\windows\parental-blocks\youtube.txt`. **YouTube is allowed by default** — the block is
only applied when you run it with `-Block`.
```powershell
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\python -m safehouse_setup toggle -Name youtube -Block   # apply
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\python -m safehouse_setup toggle -Name youtube -Allow   # remove
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\python -m safehouse_setup toggle -Name youtube          # status
```
- **Self-elevates** via UAC for `-Block`/`-Allow` (status needs no elevation).
- `-Block` appends a `# === Parental block: YouTube (toggled via safehouse_setup toggle) ===` … `# === end
  YouTube block ===` section (one `0.0.0.0 <host>` line per host from `parental-blocks\youtube.txt`).
  Idempotent — re-running does not duplicate it. `-Allow` removes that section. Both are byte-preserving:
  the script splices only its own section and leaves every other line (and the file's mixed CRLF/LF
  line endings) untouched.
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
- `safehouse_setup dns` contains no machine-specific interface, username, WSL path, or LAN gateway. It
  discovers the active IPv4 default route at runtime.
- The task runs as the logged-on user with highest privileges (no UAC prompt at logon).
