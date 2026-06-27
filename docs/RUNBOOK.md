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

## Parental content layer (DNS) and NSFW coverage
- The **parental content layer** lives in `blocklists/parental-denylist.txt` — Pi-hole regex
  (type 3) loaded with the comment `SafeHouse-Parental`, e.g. the YouTube family. Add more domain
  families there (one regex per line) and apply with `./scripts/load-blocklists.sh`.
- **Adult / NSFW** is covered network-wide by the **HaGeZi NSFW adlist URL** in `adlists.txt` — a
  referenced list, never domain literals in this (public) repo. It downloads with every gravity
  rebuild, so the weekly auto-update keeps it fresh.
- **YouTube — on-demand, per-machine (hosts layer):** YouTube is **allowed by default**. To block it
  on the Windows machine on demand, run (elevated PowerShell, self-elevates):
  ```powershell
  powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\parental-toggle.ps1 -Name youtube -Block   # apply
  powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\parental-toggle.ps1 -Name youtube -Allow   # remove
  powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\parental-toggle.ps1 -Name youtube          # status
  ```
  It edits the live hosts file (idempotent, byte-preserving), flushes DNS, and reads its host list from
  `windows/parental-blocks/youtube.txt`. See [../windows/README.md](../windows/README.md) §5.
  **Network-layer alternative:** uncomment the `# --- YouTube family ---` regexes in
  `blocklists/parental-denylist.txt` and run `./scripts/load-blocklists.sh` (they ship commented-out).
- Verify parental:
  ```bash
  docker exec pihole dig +short youtube.com @127.0.0.1   # -> 0.0.0.0 (blocked)
  ```

## YouTube daily watch-time budget (Windows-native: measure + auto-block)
A browser-agnostic daily limiter that runs **entirely on Windows** (PowerShell + Task
Scheduler) and does **not** depend on WSL/Pi-hole/Docker being up
(`windows/youtube-budget/`). A SYSTEM Scheduled Task (`SafeHouse-YouTubeBudget`,
`Restart=always`) loops every ~20 s: it polls the **Windows DNS Client cache** to *measure*
how long YouTube is actually watched (works because browser DoH is forced off by machine
policy), bills it against a daily budget, and once the budget is spent splices the **same**
YouTube hosts block the manual toggle uses (`windows/parental-blocks/youtube.txt`) into the
live Windows hosts file (byte-preserving, then `ipconfig /flushdns`). A **date-based** reset
(not a midnight timer) zeroes usage and lifts the block on the first tick of any new local
day — correct even if the PC was off at midnight or off for days. Full detail in
[../windows/youtube-budget/README.md](../windows/youtube-budget/README.md).
- **Arm (ENABLES live enforcement, elevated PowerShell — self-elevates):**
  ```powershell
  powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\youtube-budget\install-task.ps1
  ```
  Creates `C:\ProgramData\SafeHouse` (ACL: SYSTEM+Administrators full, **Users read-only**),
  seeds `config.json`, registers the **AtStartup** SYSTEM task, and starts it now. Ships
  **disarmed** — nothing blocks until you run this.
- **Parent CLI** (`youtube-budget-ctl.ps1`; mutating verbs self-elevate via UAC, `status` is
  read-only):
  ```powershell
  $ctl = 'C:\SafeHouse\windows\youtube-budget\youtube-budget-ctl.ps1'
  powershell -ExecutionPolicy Bypass -File $ctl status         # used / remaining / limit / blocked today
  powershell -ExecutionPolicy Bypass -File $ctl set-limit 90   # change the daily limit (today + config.json)
  powershell -ExecutionPolicy Bypass -File $ctl grant 30       # +30 min today (unblocks if it frees budget)
  powershell -ExecutionPolicy Bypass -File $ctl block          # force-block now
  powershell -ExecutionPolicy Bypass -File $ctl allow          # override-unblock now
  powershell -ExecutionPolicy Bypass -File $ctl reset          # zero today's usage + unblock
  ```
- **Tune:** `C:\ProgramData\SafeHouse\config.json` — `limit_min` (60), `sample_sec` (20),
  `window_sec` (240, the sliding "is-active" window that bridges sparse DNS lookups).
  `limit_min`/`bonus` apply live; changing sample/window needs the task restarted.
- **State + log:** `C:\ProgramData\SafeHouse\youtube-budget.json` and `…\youtube-budget.log`
  (`Get-ScheduledTask -TaskName SafeHouse-YouTubeBudget` to verify the task).
- **Caveats:** watch time is *approximate* (DNS activity, not playback); only lookups that go
  through the Windows resolver are seen; blocking YouTube also blocks **YouTube Music**
  (`music.youtube.com` is in the shared block list). See the feature README.
- **Disarm:** `Stop-ScheduledTask -TaskName SafeHouse-YouTubeBudget` (pause) or
  `Unregister-ScheduledTask -TaskName SafeHouse-YouTubeBudget -Confirm:$false` (remove); then
  lift any active block with `… youtube-budget-ctl.ps1 allow`.
- **Migration from the old WSL daemon:** arm the Windows task above, then retire the daemon
  on the WSL host: `sudo systemctl disable --now safehouse-youtube-budget.service`.

## Blocklists auto-update (weekly)
`scripts/auto-update.sh` re-applies the repo blocklists and rebuilds gravity so every adlist's
contents (ads + NSFW) re-download. A weekly schedule (default **Sunday 04:00 local**) is installed
by `scripts/install-autoupdate.sh` (also run by the Ansible `automation` role during provisioning).
- **Run it manually:** `./scripts/auto-update.sh`
- **Log:** `logs/auto-update.log` (timestamped; gitignored). With systemd also
  `journalctl -u safehouse-autoupdate.service`.
- **Optionally `git pull` first:** run with `SAFEHOUSE_GIT_PULL=1` — it only pulls when the working
  tree is clean, otherwise it logs and skips.
- **Verify the schedule:**
  ```bash
  systemctl list-timers safehouse-autoupdate.timer      # systemd
  cat /etc/cron.d/safehouse-autoupdate                  # cron fallback
  ```
- **Run the scheduled unit now (systemd):** `sudo systemctl start safehouse-autoupdate.service`
- **Change the cadence:** edit `OnCalendar=` in `/etc/systemd/system/safehouse-autoupdate.timer`
  then `sudo systemctl daemon-reload` (systemd), or the schedule fields in
  `/etc/cron.d/safehouse-autoupdate` (cron).
- **Uninstall:** `sudo systemctl disable --now safehouse-autoupdate.timer && sudo rm
  /etc/systemd/system/safehouse-autoupdate.{service,timer} && sudo systemctl daemon-reload`
  (systemd), or `sudo rm /etc/cron.d/safehouse-autoupdate` (cron).
- **Troubleshoot:** if a run fails it logs `load-blocklists.sh failed` and exits non-zero — almost
  always Docker/Pi-hole was down. Bring the container up (`cd ~/pihole && docker compose up -d`),
  then re-run `./scripts/auto-update.sh` and check `logs/auto-update.log`.

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
