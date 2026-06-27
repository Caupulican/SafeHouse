# YouTube daily watch-time budget (Windows-native)

A **browser-agnostic, fully automatic** daily watch-time limiter for YouTube that runs
entirely on **Windows** — PowerShell + Windows Task Scheduler. It does **not** depend on
WSL, Pi-hole, or Docker being up. It *measures* how long YouTube is actually being watched
on this PC by polling the Windows DNS Client cache, bills it against a daily budget, and
once the budget runs out it **blocks YouTube by splicing the same hosts section the manual
toggle uses** (`..\parental-blocks\youtube.txt`) into the live Windows hosts file. The
budget resets — and any block lifts — automatically on the next day.

It is tamper-resistant by placement: the watcher runs as **SYSTEM**, and its state lives
under `C:\ProgramData\SafeHouse` with an ACL that lets a standard (non-admin) user **read but not
edit or reset** it.

> This replaces the old WSL/systemd bash daemon (`youtube-budget/`). Same detect → account →
> enforce → reset logic, ported to Windows so it survives with WSL off. See **Migration** below.

## How it works

A Scheduled Task (`SafeHouse-YouTubeBudget`, trigger **AtStartup**, runs as SYSTEM with
`Restart=always`) runs `youtube-budget.ps1` as a persistent loop. Every `sample_sec` (~20s)
each tick does, in order:

1. **Date-based idempotent reset.** Read state, compute today's **local** date
   (`yyyy-MM-dd`). If `state.date` != today — for **any** number of days elapsed, including
   the very first tick after boot — zero `seconds_used` + `bonus_sec`, clear
   `blocked_by_budget`, set `date = today`, and **remove any stale auto YouTube block** from
   the hosts file. This is a pure date comparison (not a midnight timer), so it is correct
   whether the PC ran through midnight, was **off** at midnight, or was **off for several
   days**. Running it twice in one day is a no-op.

2. **Detect (pure Windows — no WSL/Pi-hole).** Poll the **Windows DNS Client cache**
   (`Get-DnsClientCache`) for live resolutions of YouTube *content* domains:
   `googlevideo.com` (the video-stream CDN — the strongest signal), `youtube.com`,
   `youtubei.googleapis.com`, `ytimg.com`, `youtu.be`, `yt3.ggpht.com`. This works because
   browser DoH is forced **off** by machine policy, so YouTube name lookups go through the
   Windows resolver and land in this cache regardless of which browser or app is used.
   Domains are matched as **anchored suffixes** (so `nsfwyoutube.com` never counts), and
   hosts-file/blocked entries (`Data = 0.0.0.0`) are ignored.

3. **Account.** A "fresh" signal — a content host that wasn't cached last tick, or whose
   TTL bumped up (a re-resolution) — marks watching and updates a *last-active* timestamp.
   A sliding **`window_sec`** (default 240s) keeps the sample counting through the gaps
   between a long video's sparse DNS lookups, and stops counting ~`window_sec` after
   watching truly ends. If active and not already blocked, the tick adds `sample_sec` to
   `seconds_used`.

4. **Enforce.** Effective limit = `limit_min*60 + bonus_sec` (default limit **60 min**).
   When `seconds_used >= limit` and not yet blocked, splice

   ```
   # === Parental block: YouTube (auto: daily budget) ===
   0.0.0.0 youtube.com
   ... (the hosts from ..\parental-blocks\youtube.txt)
   # === end YouTube block ===
   ```

   into the hosts file — **byte-preserving** (the mixed CRLF header / LF body is left
   untouched; same algorithm as `windows\parental-toggle.ps1`) — set `blocked_by_budget`,
   and flush DNS (`ipconfig /flushdns`). The next day's reset removes it again.

It shares the **block host list** with the manual toggle and never double-inserts: if a
`# === Parental block: YouTube ... ===` section already exists (manual or auto), it leaves
it alone. The automatic daily reset lifts only the **auto** block — a YouTube block you
applied by hand with `parental-toggle.ps1` is left in place.

Each tick is resilient: a transient error is logged and the loop continues; a failed
DNS-cache query skips the sample (no phantom time). Log: `C:\ProgramData\SafeHouse\youtube-budget.log`.

### Detection is approximate (read this)

This measures **DNS activity**, not video playback. A long video re-resolves
`googlevideo.com` every so often (and YouTube polls `youtubei.googleapis.com`), which keeps
the sliding window "active"; conversely a paused tab that keeps polling can read as active.
Treat the number as a good-enough budget, not a stopwatch — tune `window_sec` and the limit
after a few days of real use. Because it is hosts/DNS-based, it only sees lookups that go
through the Windows resolver: an app using its own DoH/DoT or a cached IP is invisible to
both detection and the block. (Browser DoH is disabled by machine policy precisely so this
holds.) A future enhancement could swap cache-polling for a real-time
`Microsoft-Windows-DNS-Client` **ETW** trace for tighter timing; cache-polling is the
documented, dependency-free baseline.

## State & config

- **State:** `C:\ProgramData\SafeHouse\youtube-budget.json` =
  `{ date, seconds_used, limit_min, bonus_sec, blocked_by_budget }`, written atomically.
- **Config:** `C:\ProgramData\SafeHouse\config.json` = `{ limit_min, sample_sec, window_sec }`
  (defaults 60 / 20 / 240). `limit_min` and `bonus_sec` take effect live each tick; changing
  `sample_sec` / `window_sec` needs the task restarted (`Stop-ScheduledTask` /
  `Start-ScheduledTask -TaskName SafeHouse-YouTubeBudget`, or a reboot).
- **ACL** (set by `install-task.ps1`): SYSTEM + Administrators = Full, **Users = read-only**.

## Arm / disarm

This ships **disarmed** — installing the task is what turns on live enforcement. Run the
installer in an **elevated** PowerShell (it self-elevates via UAC if you forget):

```powershell
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\youtube-budget\install-task.ps1
```

It creates the ProgramData dir + ACL, seeds `config.json`, registers
`SafeHouse-YouTubeBudget` (AtStartup, SYSTEM, Restart=always), and starts it now.

```powershell
# Verify
Get-ScheduledTask -TaskName SafeHouse-YouTubeBudget
Get-Content C:\ProgramData\SafeHouse\youtube-budget.log -Tail 10

# Pause / disarm
Stop-ScheduledTask       -TaskName SafeHouse-YouTubeBudget         # pause
Unregister-ScheduledTask -TaskName SafeHouse-YouTubeBudget -Confirm:$false   # remove
# then lift any active block:
powershell -ExecutionPolicy Bypass -File C:\SafeHouse\windows\youtube-budget\youtube-budget-ctl.ps1 allow
```

## Parent CLI

`youtube-budget-ctl.ps1` drives the budget by hand, consistently with the watcher. Mutating
verbs self-elevate via UAC; `status` is read-only (a standard user can read it).

```powershell
$ctl = 'C:\SafeHouse\windows\youtube-budget\youtube-budget-ctl.ps1'
powershell -ExecutionPolicy Bypass -File $ctl status         # used / remaining / limit / blocked today
powershell -ExecutionPolicy Bypass -File $ctl set-limit 90   # change the daily limit (today + config.json)
powershell -ExecutionPolicy Bypass -File $ctl grant 30       # +30 min today (unblocks if it frees budget)
powershell -ExecutionPolicy Bypass -File $ctl block          # force-block now
powershell -ExecutionPolicy Bypass -File $ctl allow          # override-unblock now
powershell -ExecutionPolicy Bypass -File $ctl reset          # zero today's usage + unblock
```

`allow` is an instantaneous override: it lifts the block and clears the budget flag but does
**not** forgive accrued time, so the watcher re-arms once fresh watching crosses the limit
again. For a lasting reprieve use `grant <min>` (adds time) or `reset` (zeroes the day).

## Note on YouTube Music

`music.youtube.com` is in the shared block list (`..\parental-blocks\youtube.txt`), so when
the budget blocks "YouTube" it also blocks YouTube Music. Remove that line from the list if
you want music to stay reachable (its content also rides `googlevideo.com`, so it still
counts as watch time toward the budget).

## Migration from the WSL daemon

1. Arm the Windows version (elevated PowerShell):
   `…\install-task.ps1`
2. Retire the old WSL daemon (on the WSL host):
   `sudo systemctl disable --now safehouse-youtube-budget.service`
   (cron fallback: `sudo rm /etc/cron.d/safehouse-youtube-budget && sudo pkill -f youtube-budget.sh`)

## Files

- `youtube-budget.ps1` — the watcher loop (date-reset → detect → account → enforce).
- `youtube-budget-ctl.ps1` — parent CLI (status / set-limit / grant / block / allow / reset).
- `install-task.ps1` — self-elevating installer: ProgramData + ACL + the SYSTEM task.
- `common.ps1` — shared helpers (state, byte-preserving hosts splice, DNS-cache detection).
- `config.json` — documented config sample (limit / sample / window).
- Enforcement host list: `..\parental-blocks\youtube.txt` (shared with `parental-toggle.ps1`).
