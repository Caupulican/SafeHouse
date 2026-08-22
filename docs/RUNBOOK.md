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
- **YouTube — on-demand (both lanes):** YouTube is **allowed by default**. To block it on demand,
  run (elevated PowerShell, self-elevates):
  ```powershell
  C:\SafeHouse\python\python.exe -m safehouse_setup toggle youtube --block   # apply
  C:\SafeHouse\python\python.exe -m safehouse_setup toggle youtube --allow   # remove
  C:\SafeHouse\python\python.exe -m safehouse_setup toggle youtube           # status
  ```
  It drives the **hosts file** (Windows applications) *and* the **resolver** (AdGuard Home
  blocked services), and prints both. The resolver lane is the one that covers the Google Play
  Games VM and every phone on the network — the hosts file structurally cannot. Exit code `2`
  means the block applied but the resolver lane is not enforcing, so coverage is incomplete.
  See [../windows/README.md](../windows/README.md) §5.
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
  cd C:\SafeHouse\windows\youtube-budget
  C:\SafeHouse\python\python.exe .\install.py
  ```
  Creates `C:\ProgramData\SafeHouse` (ACL: SYSTEM+Administrators full, **Users read-only**),
  seeds `config.json`, registers the **AtStartup** SYSTEM task, and starts it now. Ships
  **disarmed** — nothing blocks until you run this.
- **Parent CLI** (`python -m safehouse_ytbudget.cli`; mutating verbs need elevation,
  `status` is read-only):
  ```powershell
  cd C:\SafeHouse\windows\youtube-budget
  $py = 'C:\SafeHouse\python\python.exe'
  & $py -m safehouse_ytbudget.cli status         # used / remaining / limit / blocked today
  & $py -m safehouse_ytbudget.cli set-limit 90   # change the daily limit (today + config.json)
  & $py -m safehouse_ytbudget.cli grant 30       # +30 min today (unblocks if it frees budget)
  & $py -m safehouse_ytbudget.cli block          # force-block now
  & $py -m safehouse_ytbudget.cli allow          # override-unblock now
  & $py -m safehouse_ytbudget.cli reset          # zero today's usage + unblock
  ```
- **Tune:** `C:\ProgramData\SafeHouse\config.json` — `limit_min` (180), `sample_sec` (20),
  `window_sec` (240, the sliding "is-active" window that bridges sparse DNS lookups).
  `limit_min`/`bonus` apply live; changing sample/window needs the task restarted.
- **State + log:** `C:\ProgramData\SafeHouse\youtube-budget.json` and `…\youtube-budget.log`
  (`Get-ScheduledTask -TaskName SafeHouse-YouTubeBudget` to verify the task).
- **Caveats:** watch time is *approximate* (DNS activity, not playback); only lookups that go
  through the Windows resolver are seen; blocking YouTube also blocks **YouTube Music**
  (`music.youtube.com` is in the shared block list). See the feature README.
- **Disarm:** `Stop-ScheduledTask -TaskName SafeHouse-YouTubeBudget` (pause) or
  `Unregister-ScheduledTask -TaskName SafeHouse-YouTubeBudget -Confirm:$false` (remove); then
  lift any active block with `python -m safehouse_ytbudget.cli allow`.
- **Migration from the old WSL daemon:** arm the Windows task above, then retire the daemon
  on the WSL host: `sudo systemctl disable --now safehouse-youtube-budget.service`.

## YouTube Shorts (browser layer)

Shorts share every domain with the rest of YouTube — only the URL path and page layout
differ, so no resolver can separate them. Two browser-level lanes, both written by
`python -m safehouse_setup browser` (machine policy, HKLM, out of reach of browser
settings):

- **Navigation is blocked**: `URLBlocklist = youtube.com/shorts` in Edge, Opera GX
  (the unspaced `OperaSoftware` key — see `safehouse_setup/browser.py` for the trap)
  and Chrome. Any `/shorts/` URL lands on the blocked-by-administrator page.
- **Shorts are hidden**: uBlock Origin is force-installed with
  `blocklists/youtube-shorts.txt` pre-subscribed via managed `toOverwrite.filterLists`
  (defaults restated — "overwrite" means overwrite). The list hides the Shorts shelf,
  sidebar entries, thumbnails and channel tabs. It is fetched from this repo's `main`
  branch on GitHub, so **it only serves after the branch carrying it is merged and
  pushed**; until then uBO shows a fetch error for that one list and everything else
  works.
- **Verify:** restart the browser twice (once to load policy, once for the forced
  install), then open `edge://policy` / `opera://policy` and look for `URLBlocklist`;
  navigate to `youtube.com/shorts` → blocked page; open YouTube home → no Shorts
  shelf. Opera's forced install from the Chrome Web Store is best-effort — if uBO does
  not appear there, install it once by hand and import the raw list URL.

## The two process-kill lanes (no browsing, and the Roblox budget)

Some things cannot be enforced at the resolver. A joined Roblox session holds a raw UDP
connection to `128.116.0.0/17` and never resolves a name again, so DNS only stops *new*
joins; and "no web browsing" is an infinite list of sites, when the thing being ruled out
is the browser itself. Both are enforced by ending the process instead:

| Task | Module | Config | State + log |
|---|---|---|---|
| `SafeHouse-RobloxBudget` | `safehouse_ytbudget.roblox_guard` | `roblox-budget.json` | `roblox-budget.state.json`, `roblox-budget.log` |
| `SafeHouse-BrowserGuard` | `safehouse_ytbudget.browser_guard` | `browser-guard.json` | `browser-guard.state.json`, `browser-guard.log` |

Everything lives under `C:\ProgramData\SafeHouse` (ACL: SYSTEM + Administrators full,
**Users read-only** — the child may read how much time is left, not edit it).

**Shape — both lanes are the same one-shot.** A SYSTEM task fires at boot and on
registration and repeats every `PT1M`, `ExecutionTimeLimit PT5M`, `IgnoreNew`. Each run
takes **one** `tasklist /FO CSV /NH` dump, acts, and exits. Neither is a resident loop;
the browser lane used to be one and was converted, because:

- **a run that finds nothing running does nothing at all** — no log line, no state write,
  no notification, no second subprocess. On an ordinary day, when nobody browses and
  nobody plays, each lane costs one `tasklist` a minute and nothing else.
- the price is latency, and it is stated plainly below rather than hidden.

**Timing, honestly.** Up to ~60s passes before a launch (or an expiry) is noticed — that
is the finest repetition a scheduled task offers. The run then waits a random **1–5s**,
re-observes, and ends only what is *still* up. That pause is a small part of the total; it
is not what makes the enforcement gentle. Its job is that the exact moment is
unpredictable, that a window closing a beat later reads as a rule rather than a crash, and
that a child who closes the window himself during the pause is not reported as a kill.

**Tune** (both files are seeded by `install.py` and **never overwritten** on re-run):
- `roblox-budget.json` — `limit_min` (**0** = the standing policy, and the strict
  fallback: absent or broken means 0, never unlimited), `delay_min_sec`, `delay_max_sec`.
- `browser-guard.json` — `enabled`, `delay_min_sec`, `delay_max_sec`, `users` (empty =
  every user; a typo here fails *open* to all users rather than silently enforcing
  nothing), `notify`, `message`. Every defect falls back to **enforcing** with the
  defaults. **`notify` is `false`** — the browser closes with nothing on screen, by
  the parent's instruction that the child is not to know a control is doing it. He
  still sees the window go, so this hides the cause, not the effect. Set it to `true`
  to show `message` instead.
- Delays are clamped to 60s: the task's `ExecutionTimeLimit` is PT5M, and a pause the
  scheduler terminates mid-run is a browser that survives.
- A today-only Roblox grant is `bonus_sec` in the *state* file; it dies with the date
  rollover instead of quietly becoming the new limit.
- Because the installer never overwrites, a `browser-guard.json` written before the lane
  became a one-shot keeps its old values: `poll_sec` is now **ignored** (there is no loop
  to pace) and a `delay_min_sec`/`delay_max_sec` of 5–60 is still honoured. Edit those two
  down to 1–5 by hand and drop `poll_sec` if you want the current defaults.

**Parent CLI** (browser lane; mutating verbs need elevation, `--status` is read-only):
```powershell
cd C:\SafeHouse\windows\youtube-budget
$py = 'C:\SafeHouse\python\python.exe'
& $py -m safehouse_ytbudget.browser_guard --status         # enforcing? unlock left?
& $py -m safehouse_ytbudget.browser_guard --unlock 30      # suspend for 30 min
& $py -m safehouse_ytbudget.browser_guard --lock           # cancel the unlock now
& $py -m safehouse_ytbudget.browser_guard --dry-run        # one run: observe + log only
& $py -m safehouse_ytbudget.roblox_guard  --dry-run        # ditto for Roblox
```

**Arm** (elevated; each flag touches nothing else):
```powershell
cd C:\SafeHouse\windows\youtube-budget
C:\SafeHouse\python\python.exe .\install.py --roblox-guard-only
C:\SafeHouse\python\python.exe .\install.py --browser-guard-only
```
Both are also registered by a full `install.py` run. Neither needs `/Run` — the
`RegistrationTrigger` starts the repetition immediately. Re-running is how you *change
the task's shape*: `schtasks /Create /F` overwrites the registration, so if a task is
being converted from an older shape, `schtasks /End /TN <name>` first to stop whatever is
still running under the old one.

**Prerequisite for the browser lane:** run `python -m safehouse_setup browser` first. Edge
Startup Boost and Chrome/Edge background mode keep a browser process resident with no
window at all; without those policies off, "a browser is running" says nothing about what
the child did, and the guard spends the day fighting Windows. It detects that fight —
12 closes of the same image within 30 minutes logs `[error] … Windows is restarting it,
not the child` — rather than assuming it
away.

**Before arming, add the AV exclusion.** Avast quarantined the earlier watcher on this
machine, script and scheduled task both, and enforcement then dies **silently**. The
installer adds a Microsoft Defender exclusion; third-party AV needs its own, added by hand
in that product, *before* the task is registered.

**Verify** — each lane, both directions:
```powershell
schtasks /Query /TN SafeHouse-RobloxBudget /V /FO LIST   # Ready, last run within a minute
schtasks /Query /TN SafeHouse-BrowserGuard /V /FO LIST
```
- *It enforces:* open Edge → within about a minute plus a few seconds the window closes
  with nothing shown on screen; `browser-guard.log` shows `[wait]` then `[kill]` — the
  log is the only place a close is recorded now. For Roblox, set
  `{"limit_min": 1}`, launch it, and the game must die within about a minute (then remove
  the override).
- *It does not over-reach:* with Steam, Epic, Roblox and Windows Widgets running and no
  browser open, nothing is closed and nothing is logged. `msedgewebview2.exe`,
  `steamwebhelper.exe` and `RobloxPlayerBeta.exe` are Chromium in someone else's clothes
  and must survive every sweep — killing them breaks the games, which is how a guard gets
  turned off for good.
- *It is quiet when idle:* with nothing running, neither log may grow and neither state
  file's timestamp may move, however many times the tasks fire. A log line per minute is a
  regression, not cosmetics.

**Honest limits.** child has local admin by choice, so he can stop or delete either task.
These lanes make that a visible, deliberate act; they do not make it impossible. Also, a
kill is image-wide: if a parent is browsing in the same browser in their own session at
that moment, their window goes too — set `users` if that matters.

**Disarm:** `Stop-ScheduledTask -TaskName SafeHouse-BrowserGuard` (pause the current run)
or `Unregister-ScheduledTask -TaskName SafeHouse-BrowserGuard -Confirm:$false` (remove),
same for `SafeHouse-RobloxBudget`. A temporary parent override is `--unlock N` instead, so
it expires by itself.

## The control panel (dashboard + settings, from any device in the house)

One web page showing everything that is enforced, with sliders that write the real config.
It runs **on the child's PC** as a SYSTEM scheduled task (`SafeHouse-Panel`, boot-triggered,
restarted if it dies) and is read **from another device on the LAN** — never displayed on
that machine: no browser is opened, no console, no toast. Full detail, including the
security model and its limits, in [PANEL.md](PANEL.md).

```powershell
# Arm it (elevated). Registers the task and opens TCP 8787 to the local subnet only.
cd C:\SafeHouse\windows\youtube-budget
C:\SafeHouse\python\python.exe .\install.py --panel-only

# Set the password. It is written to C:\ProgramData\SafeHouse\panel-password.txt —
# store it, then DELETE that file. Only the hash is kept.
C:\SafeHouse\python\python.exe -m safehouse_panel set-password --generate
```

Then open `http://192.168.1.50:8787` from a phone or laptop on the house network.

- **Binds the primary LAN address plus loopback, never `0.0.0.0`** — the box also has two
  VMware host-only adapters and the WSL adapter, and publishing a control surface for every
  protection in the house on those is not wanted. The LAN address is DHCP: **set a
  reservation on the router**, or the bookmark breaks and it looks like the panel is down.
- **Password + session cookie, and no TLS.** Traffic crosses the LAN in clear, so anyone
  already sniffing this network can read the login. House LAN only, not port-forwarded.
- **Weakening a protection asks for the password again** (more Roblox minutes, a bonus,
  disabling the browser guard, unlocking browsing, allowing YouTube). Tightening does not.
- **Verify the readings without it:** `C:\SafeHouse\python\python.exe -m safehouse_panel
  self-test` prints the whole snapshot as JSON and binds nothing. PANEL.md has a table of
  independent checks for every reading on the page.
- **Is it alive?** `Get-Content C:\ProgramData\SafeHouse\panel.log -Tail 5` — a `[start]`
  line with the bound addresses, an `[alive]` line every hour, and every change as
  `[change] <control>: before -> after (by <address>)`.
- **Do not read it on the guarded PC.** The browser guard closes the browser you would be
  reading it in, within about a minute — that is what the unlock control is for.
- **Changing the password** is done on the page, under *Panel access*. It asks for the
  current one and signs every other device out. Minimum four characters; the panel warns
  about a weak one and then accepts it, because the six-tries-then-15-minutes lockout is
  what is actually holding the line.

### Is the filtering resolver up? (the section that would have caught 2026-08-01)

The top card on the panel, and the one to look at first. Its verdict is a single question:
**does `doubleclick.net` come back sinkholed?** Not "did port 53 answer" — the Windows
relay owns that port and answers happily during an outage, with unfiltered results. That
is what failing open means, and it is why the outage was invisible for fourteen hours.

`SafeHouse-Resolver` is the lane behind it: at logon and every 15 minutes it checks, and
starts WSL and the containers when the chain is not filtering. It is **the only task in
this repo that does not run as SYSTEM** — Windows refuses to run WSL as LocalSystem at all
(`WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`), so it runs in the logged-on user's session, hidden,
under `pythonw.exe`. Consequence worth knowing: **nothing can repair the chain while
nobody is signed in to the PC.**

```powershell
# Arm it. Run it while signed in as the account WSL is registered to.
C:\SafeHouse\python\python.exe .\install.py --resolver-only --distro Debian

# Ask it directly, any time.
C:\SafeHouse\python\python.exe -m safehouse_setup resolver --status
C:\SafeHouse\python\python.exe -m safehouse_setup resolver           # repair if needed
```

A run that finds the chain filtering sends two UDP packets and exits without spawning
anything, which is what makes a 15-minute schedule acceptable on a gaming PC. What it last
did is in `C:\ProgramData\SafeHouse\resolver.json`.

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

Two independent halves, and it matters which does what — believing the Windows side
restarts the containers is what let a fourteen-hour outage go unnoticed on 2026-08-01.

1. **Windows side.** At logon the `SafeHouse-DNS` task starts the loopback relay
   (`safehouse_setup serve`). That is *all* it does: it tracks WSL's changing NAT address
   and forwards `127.0.0.1:53` to it. It does **not** start the containers, and if the
   resolver does not answer it **fails open to the LAN router** — resolution keeps working
   with nothing filtered behind it. Run `python -m safehouse_setup dns` to re-point the
   adapter and reconcile the stack; it now prints a `NO FILTERING` banner when it ends up
   on the router instead of the filter.
2. **WSL side.** The `safehouse-dns.timer` watchdog runs `reconcile-dns.sh` 15s after boot
   and every 5 minutes after that, which is what actually brings Pi-hole and AdGuard Home
   back — **while the VM is running.** It cannot start the VM it lives inside, which is
   the gap `SafeHouse-Resolver` on the Windows side now covers (at logon, then every 15
   minutes, in the logged-on user's session). A normal `deploy.sh --with-adguard` installs it automatically; use
   `./scripts/install-dns-service.sh` directly only to repair its systemd units.

   Do not rely on `restart: unless-stopped` alone. It is set on both containers and it is
   not sufficient: a dockerd restart left AdGuard Home dead with `RestartCount=0` while
   Pi-hole recovered, and an identical restart minutes later recovered both. The
   reconciler is idempotent, so the watchdog covers whichever way it fails.
3. **Open Google Play Games *after* login** so the VM picks up the resolver.
4. Check the Windows resolver is actually filtered — not merely working:
   ```powershell
   Get-ScheduledTaskInfo -TaskName SafeHouse-DNS
   Get-DnsClientNrptRule | Where-Object Comment -like 'SafeHouse:*'
   Resolve-DnsName doubleclick.net -Type A   # -> 0.0.0.0   (if this returns a real
                                             #    address, filtering is OFF)
   Resolve-DnsName example.com -Type A       # -> real addresses
   ```
   And in WSL: `systemctl list-timers safehouse-dns.timer && docker ps`

## Troubleshooting
- **Ads came back everywhere (and YouTube/parental blocks stopped working):** suspect the
  filter is not in the path at all rather than a missing blocklist entry. The relay fails
  open to the router, so this looks exactly like a healthy machine. Check in WSL:
  ```bash
  docker ps                                   # both pihole and adguardhome must be Up
  systemctl list-timers safehouse-dns.timer   # the watchdog must be active
  ~/.local/bin/safehouse-reconcile-dns        # idempotent; brings the chain back
  ```
  Then on Windows confirm `Resolve-DnsName doubleclick.net` returns `0.0.0.0`. This was
  the 2026-08-01 outage: WSL shut down at 01:12, both containers stayed down because they
  carried `RestartPolicy=no`, and the in-game ads in the Play Games VM returned while
  every other signal looked fine.
- **No internet after reboot:** first run `Start-ScheduledTask -TaskName SafeHouse-DNS`.
  If its loopback listener still does not answer, run `safehouse_setup persist` again; it
  refuses to change adapter DNS until the relay is verified. `safehouse_setup dns` resets
  both address families to DHCP whenever that listener is unavailable.
- **Container won't start:** `cd ~/pihole && docker compose logs --tail=50`. Usually a stale
  `PIHOLE_BIND_IP` in `.env`: fix it to `hostname -I`'s first IP and `docker compose up -d`.
- **Dashboard unreachable from Windows:** TCP localhost forwarding → http://localhost:8053/admin.
