# Runbook

## Deploy / rebuild from scratch
```bash
# Ansible (preferred)
cd SafeHouse/ansible && ansible-playbook -i inventory.ini playbook.yml
# or plain bash
cd SafeHouse && ./scripts/bootstrap.sh
```
Then finish the admin steps in [../windows/README.md](../windows/README.md) and **restart Google Play Games**.

## A game still shows ads (DNS layer): hunt and block the leak
```bash
bash tools/adhunt.sh           # 45s capture; APPROVE UAC, trigger ads in-game
```
- Read the **AD/TRACKER** and **UNKNOWN** sections it prints.
- Add confirmed ad hosts to `blocklists/regex-denylist.txt`, e.g. `(^|\.)somenetwork\.com$`
- Apply: `./scripts/load-blocklists.sh`
- **Restart the game** (clears the SDK's cached ad IPs) and re-test.

## Ads still showing after DNS blocks: the firewall layer
Some ad SDKs bypass DNS entirely (DoH, QUIC, or cached IPs), so Pi-hole never sees the lookup.
Block them at the connection layer instead. In an **elevated PowerShell** on Windows, with the
ads showing:
```powershell
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\safehouse-adblock.ps1
```
This captures the crosvm VM's live connections, classifies them by network owner against
`blocklists/ad-watchlist.txt`, logs to `logs/traffic.csv`, ingests confirmed ad IPs into
`blocklists/ad-ip-ranges.txt`, and rebuilds the `SafeHouse-AdBlock` firewall group. Then **restart
Google Play Games**. Re-run any time ads return (the servers rotate IPs; the watchlist keeps
catching them). Full detail in [FIREWALL.md](FIREWALL.md).

## Add a new blocklist URL
Append the URL to `blocklists/adlists.txt`, then `./scripts/load-blocklists.sh`.

## Back up live changes into the repo
```bash
./scripts/export-config.sh
git add blocklists && git commit -m "update blocklists"
```

## Verify it's working
```bash
docker exec pihole dig +short doubleclick.net @127.0.0.1   # -> 0.0.0.0 (blocked)
docker exec pihole dig +short example.com   @127.0.0.1     # -> real IP
# dashboard: http://localhost:8053/admin
```
In the dashboard query log you should see Android domains (`time.android.com`,
`connectivitycheck.gstatic.com`) once the VM is routing through Pi-hole.

## Something legit broke (false positive, likely HaGeZi Pro++)
Allowlist it: Dashboard → **Domains** → add to allowlist, **or**:
```bash
docker exec pihole pihole allow example.com   # exact
docker exec pihole pihole reloadlists
```

## Reboot behavior
1. Log in → the `PiholeGPG-DNS` task wakes WSL, re-binds Pi-hole to the new WSL IP, points DNS at it.
2. **Open Google Play Games *after* login** so the VM picks up Pi-hole.
3. Check it ran: `Get-Content $env:USERPROFILE\.pihole\set-dns.log -Tail 3`

## Troubleshooting
- **No internet after reboot:** the failsafe sets DNS to the router; check the log, ensure WSL/docker
  started, then re-run the task: `Start-ScheduledTask -TaskName PiholeGPG-DNS`.
- **Container won't start:** `cd ~/pihole && docker compose logs --tail=50`. Usually a stale
  `PIHOLE_BIND_IP` in `.env`: fix it to `hostname -I`'s first IP and `docker compose up -d`.
- **Dashboard unreachable from Windows:** TCP localhost forwarding → http://localhost:8053/admin.
