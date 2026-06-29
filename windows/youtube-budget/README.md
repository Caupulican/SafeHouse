# YouTube daily watch-time budget (Windows-native)

A **browser-agnostic, fully automatic** daily watch-time limiter for YouTube that runs
entirely on **Windows** — PowerShell + Windows Task Scheduler. It does **not** depend on
WSL, Pi-hole, or Docker being up. It *measures* how long YouTube is actually being watched
on this PC by **metering the streaming bytes to Google's video infrastructure** (gated on a
real-time YouTube-family DNS signal), bills it against a daily budget, and once the budget
runs out it **blocks YouTube by splicing the same hosts section the manual toggle uses**
(`..\parental-blocks\youtube.txt`) into the live Windows hosts file. The budget resets — and
any block lifts — automatically on the next day.

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

2. **Detect — connection-flow metering (pure Windows, no WSL/Pi-hole).** Two signals must
   agree for a sample to count as watching:

   - **Byte meter (the primary signal).** A lightweight **`pktmon`** *counters-only* session
     (no packet logging to disk) counts inbound bytes to **Google's published serving IP
     ranges** (`https://www.gstatic.com/ipranges/goog.json`, cached to
     `goog-ranges.json`, refreshed weekly; the cache is reused if a refresh fails). `pktmon`
     allows 32 filters but Google publishes ~110 prefixes, so the watcher meters **all of
     Google's IPv6 ranges** (the dual-stack path YouTube actually uses here) **plus the
     largest IPv4 blocks** to fill the remaining slots. Each tick reads
     `pktmon counters --json` and takes the **inbound-byte delta** since the last tick →
     throughput in kbps. This survives **DNS caching and QUIC connection-coalescing**, which
     made the old DNS-cache approach blind: modern Edge reuses warm IPs and streams video
     over already-open Google connections with almost **no fresh `googlevideo.com` lookups**.

   - **DNS gate (keeps Drive/updates/etc. from counting).** A real-time
     **`Microsoft-Windows-DNS-Client/Operational`** ETW channel is read each tick for any
     resolution of a YouTube-family name (`googlevideo.com`, `youtube.com`,
     `youtubei.googleapis.com`, `ytimg.com`, `youtu.be`, `ggpht.com`) — matched as **anchored
     suffixes** (so `nsfwyoutube.com` never counts) on the query-name *property* (so it is
     locale-independent). Unlike the DNS *cache*, this catches even the sparse, short-TTL
     lookups that never linger in `Get-DnsClientCache`. A lookup opens the gate for
     `dns_gate_min` minutes (default **20**); set `dns_gate_min = 0` to disable the gate
     (throughput-only).

3. **Account.** A sample is **fresh watching** when inbound throughput to Google exceeds
   `min_throughput_kbps` (default **64**, well under any real video bitrate yet above
   keep-alive chatter) **and** the DNS gate is open. A fresh sample updates a *last-active*
   timestamp; a sliding **`window_sec`** (default 240s) keeps the sample counting through
   brief throughput dips between video segments and stops counting ~`window_sec` after
   streaming truly ends. If active and not already blocked, the tick adds `sample_sec` to
   `seconds_used`. The per-tick `[sample]` log line records `kbps=… gate=open|closed|off`
   so the two knobs can be tuned from the world-readable log.

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

Each tick is resilient: a transient error is logged and the loop continues; if the `pktmon`
counters can't be read the watcher re-arms the session and skips that sample (no phantom
time). Log: `C:\ProgramData\SafeHouse\youtube-budget.log`.

### Detection is approximate (read this)

This measures **streaming bytes to Google's edge**, gated on YouTube DNS — not the video
element itself, so treat the number as a good-enough budget, not a frame-accurate stopwatch.
Known limitations and the knob that tunes each:

- **False positives — other Google bulk traffic.** Google Drive sync, an Edge/Chrome update,
  or the Play-games VM all ride Google IP ranges. The **DNS gate** (`dns_gate_min`, default
  20 min) suppresses these because they don't resolve YouTube-family names; with the gate
  **off** (`dns_gate_min = 0`) any sustained Google download above `min_throughput_kbps`
  would count. If YouTube is open in another tab while such a download runs, the gate is open
  and the bytes *can* be over-counted — raise `min_throughput_kbps` or keep the gate on.
- **Background YouTube chatter.** A paused/idle YouTube tab still resolves names but streams
  little, so it stays under `min_throughput_kbps` and does **not** accrue — raise the
  threshold if low-bitrate audio-only playback should be ignored, lower it to catch 144p.
- **IPv4 coverage.** Because `pktmon` caps at 32 filters, the IPv4 set is the *largest*
  Google blocks (it covers all IPv6, which is the path this dual-stack box actually uses for
  Google/YouTube). A pure-IPv4 fallback could miss some smaller Google edge ranges; refresh
  `goog-ranges.json` from Google keeps the set current.
- **Self-contained DoH/DoT apps.** An app resolving names itself (own DoH/DoT) bypasses the
  DNS gate; the byte meter still sees the traffic but the gate may not open. Browser DoH is
  disabled by machine policy precisely so the gate holds.

Tune `min_throughput_kbps`, `dns_gate_min`, `window_sec`, and the limit after a few days of
real use — the `[sample]` / `[idle]` log lines print `kbps=…` and `gate=…` to guide it.

## State & config

- **State:** `C:\ProgramData\SafeHouse\youtube-budget.json` =
  `{ date, seconds_used, limit_min, bonus_sec, blocked_by_budget }`, written atomically.
- **Config:** `C:\ProgramData\SafeHouse\config.json` =
  `{ limit_min, sample_sec, window_sec, min_throughput_kbps, dns_gate_min }`
  (defaults 60 / 20 / 240 / 64 / 20). `limit_min` and `bonus_sec` take effect live each tick;
  changing `sample_sec`, `window_sec`, `min_throughput_kbps`, or `dns_gate_min` needs the task
  restarted (`Stop-ScheduledTask` / `Start-ScheduledTask -TaskName SafeHouse-YouTubeBudget`,
  or a reboot).
- **Cache:** `C:\ProgramData\SafeHouse\goog-ranges.json` — Google's published IP prefixes,
  refetched weekly (reused if a refetch fails).
- **ACL** (set by `install-task.ps1`): SYSTEM + Administrators = Full, **Users = read-only**.
- **Machine change:** the watcher enables the `Microsoft-Windows-DNS-Client/Operational` log
  channel (off by default) for the real-time DNS gate.

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
- `common.ps1` — shared helpers (state, byte-preserving hosts splice, pktmon byte metering +
  DNS-Client ETW gate detection).
- `config.json` — documented config sample (limit / sample / window / min-throughput / dns-gate).
- Enforcement host list: `..\parental-blocks\youtube.txt` (shared with `parental-toggle.ps1`).
