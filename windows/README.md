# Windows side (admin / UAC steps)

WSL can't elevate itself, so these two one-time steps are done by **you** in an **elevated
PowerShell** (Right-click Start → *Terminal (Admin)*). The scripts here are staged automatically
by `bootstrap.sh` / the Ansible `windows_persistence` role:
- `set-dns.ps1` → copied to `%USERPROFILE%\.pihole\set-dns.ps1`
- `mktask.ps1`  → copied to `C:\Temp\mktask.ps1`

## 1. Point Windows DNS at Pi-hole (now)
```powershell
# <WSL-IP> = output of:  wsl hostname -I   (first address)
Set-DnsClientServerAddress -InterfaceAlias 'Ethernet' -ServerAddresses <WSL-IP>
ipconfig /flushdns
```
Undo anytime: `Set-DnsClientServerAddress -InterfaceAlias 'Ethernet' -ResetServerAddresses`

## 2. Arm the reboot-proof logon task
```powershell
powershell -ExecutionPolicy Bypass -File C:\Temp\mktask.ps1
```
This registers **`PiholeGPG-DNS`** (runs at logon, highest privileges). On each login it wakes WSL,
re-binds Pi-hole to the current WSL IP, and points the Ethernet DNS at it (router failsafe if
Pi-hole is down).

Verify / test:
```powershell
Get-ScheduledTask -TaskName PiholeGPG-DNS
Start-ScheduledTask -TaskName PiholeGPG-DNS                       # simulate a reboot
Get-DnsClientServerAddress -InterfaceAlias 'Ethernet'            # should show the WSL IP
Get-Content $env:USERPROFILE\.pihole\set-dns.log -Tail 3
```
Remove:
```powershell
Unregister-ScheduledTask -TaskName PiholeGPG-DNS -Confirm:$false
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
- The markers are compatible with `C:\Users\Public\ParentalControls\parental-undo.ps1`.

**Pi-hole / network-layer alternative:** to block YouTube for the whole network instead of one machine,
uncomment the `# --- YouTube family ---` regexes in `blocklists/parental-denylist.txt` and run
`./scripts/load-blocklists.sh` (they ship commented-out, so nothing is blocked by default).

## Notes
- `set-dns.ps1` assumes the adapter is named **`Ethernet`** and a router failsafe of **`192.168.1.1`**.
  Edit the two variables at the top if your machine differs.
- The task runs as the logged-on user with highest privileges (no UAC prompt at logon).
