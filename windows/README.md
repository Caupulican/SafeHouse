# Windows side (admin / UAC steps)

WSL can't elevate itself, so these two one-time steps are done by **you** in an **elevated
PowerShell** (Right-click Start → *Terminal (Admin)*). The scripts here are staged automatically
by `bootstrap.sh` / the Ansible `windows_persistence` role:
- `set-dns.ps1` → copied to `%USERPROFILE%\.pihole\set-dns.ps1`
- `mktask.ps1`  → copied to `C:\Temp\mktask.ps1`

## 1. Point Windows DNS at Pi-hole (now)
Run the staged persistence script once from an elevated PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.pihole\set-dns.ps1
```
It auto-detects the active adapter, gateway, and current WSL address. It points the adapter at
Pi-hole and installs a global Windows DNS Client NRPT rule so router-advertised IPv6 DNS cannot
silently bypass Pi-hole. Chrome and Edge secure DNS are disabled by machine policy so those
browsers stay on the enforced Windows resolver. If Pi-hole cannot answer, the script points both
the adapter and NRPT rule at the detected gateway as an internet-preserving failsafe.

## 2. Arm the reboot-proof logon task
```powershell
powershell -ExecutionPolicy Bypass -File C:\Temp\mktask.ps1
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
`parental-toggle.ps1` applies or removes a named parental block in the **live Windows hosts file**
on demand. It is staged to `C:\SafeHouse\windows\parental-toggle.ps1` with its source host list at
`C:\SafeHouse\windows\parental-blocks\youtube.txt`. **YouTube is allowed by default** — the block is
only applied when you run it with `-Block`.
```powershell
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\parental-toggle.ps1 -Name youtube -Block   # apply
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\parental-toggle.ps1 -Name youtube -Allow   # remove
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\parental-toggle.ps1 -Name youtube          # status
```
- **Self-elevates** via UAC for `-Block`/`-Allow` (status needs no elevation).
- `-Block` appends a `# === Parental block: YouTube (toggled via parental-toggle.ps1) ===` … `# === end
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
# ARM — registers SafeHouse-YouTubeBudget (AtStartup, SYSTEM, Restart=always) and starts it now:
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\youtube-budget\install-task.ps1
```
It creates `C:\ProgramData\SafeHouse` with a locked-down ACL (SYSTEM + Administrators full,
**Users read-only**, so a standard user can read but not reset the budget), seeds `config.json`
(limit 30 min / sample 20 s / window 240 s), and registers the task.
```powershell
$ctl = 'C:\SafeHouse\windows\youtube-budget\youtube-budget-ctl.ps1'
powershell -ExecutionPolicy Bypass -File $ctl status        # used / remaining / limit / blocked today
powershell -ExecutionPolicy Bypass -File $ctl set-limit 90  # change the daily limit
powershell -ExecutionPolicy Bypass -File $ctl grant 30      # +30 min today
powershell -ExecutionPolicy Bypass -File $ctl allow         # lift a block now (override)
Get-ScheduledTask -TaskName SafeHouse-YouTubeBudget         # verify
Unregister-ScheduledTask -TaskName SafeHouse-YouTubeBudget -Confirm:$false   # disarm
```
**Migration** from the old WSL daemon: after arming the task above, retire the daemon on the
WSL host — `sudo systemctl disable --now safehouse-youtube-budget.service`. Full detail:
[youtube-budget/README.md](youtube-budget/README.md).

## Notes
- `set-dns.ps1` contains no machine-specific interface, username, WSL path, or LAN gateway. It
  discovers the active IPv4 default route at runtime.
- The task runs as the logged-on user with highest privileges (no UAC prompt at logon).
