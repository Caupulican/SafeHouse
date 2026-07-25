# YouTube daily watch-time budget (Windows-native)

A **browser-agnostic, fully automatic** daily watch-time limiter for YouTube that runs
entirely on **Windows** — PowerShell + Windows Task Scheduler. It does **not** depend on
WSL, Pi-hole, or Docker being up. It *measures* how long YouTube is actually being watched
on this PC by **pinning the exact IPs that YouTube-family hostnames resolve to and metering
the streaming bytes to those IPs**, bills it against a daily budget, and once the budget
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

2. **Detect — resolved-IP pinning (pure Windows, no WSL/Pi-hole).**

   - **Harvest resolved IPs.** A real-time **`Microsoft-Windows-DNS-Client/Operational`** ETW
     channel is read each tick for **query-completed** events (Id 3008). For any query name
     matching a YouTube-family term (`googlevideo`, `youtube`, `ytimg`, `ggpht`, `yt3`,
     case-insensitive), the **answer IPs** are parsed out of the result set (the CNAME chain
     is dropped; IPv4 answers mapped as `::ffff:x.x.x.x` are unmapped; both IPv4 and IPv6 are
     kept). Unlike the DNS *cache*, this catches even the sparse, short-TTL lookups that never
     linger in `Get-DnsClientCache`.

   - **Pin the IPs.** Each resolved IP is added to a persisted **pinned-IP set**
     (`yt-pinned-ips.json` = `[{ ip, first_seen, last_active }]`). A pin persists by
     **activity, not DNS recency**: because QUIC connection-coalescing means a long session
     rarely re-resolves, `last_active` is refreshed whenever bytes are flowing to the pinned
     set, and a pin expires only after `pin_idle_expire_min` (default **30**) of zero bytes.
     The set is capped at **32** IPs (pktmon's filter limit); the least-recently-active is
     evicted.

   - **Byte meter.** A lightweight **`pktmon`** *counters-only* session (no packet logging to
     disk) counts inbound bytes to **exactly the pinned `/32` and `/128` IPs**. Each tick
     reads `pktmon counters --json` and takes the **inbound-byte delta** since the last tick →
     throughput in kbps. This survives **DNS caching and QUIC connection-coalescing**, and —
     crucially — it follows the stream **wherever it actually flows**: Google's own IPs (IPv4
     *and* IPv6) *and* the **Google Global Cache (GGC)** nodes embedded in the ISP whose IPs
     are **not in `goog.json` at all**. Metering "Google's published ranges" structurally
     missed the GGC path and much of the IPv4 path; pinning the resolved IPs does not.
     (`pktmon` counts UDP/QUIC too. `goog.json` is still fetched — cached to `goog-ranges.json`
     — but only to **diagnostically flag** which pinned IPs are off-`goog` GGC nodes in the
     log.)

   The pinned IP set is the disambiguator: only IPs a YouTube-family name resolved to are ever
   metered, so generic Google traffic (Gmail, Drive sync, Search, Play-games) **never counts**
   — no separate DNS "gate" is needed.

3. **Account.** A sample is **fresh watching** when inbound throughput to the pinned IPs
   exceeds `min_throughput_kbps` (default **64**, well under any real video bitrate yet above
   keep-alive chatter). A fresh sample updates a *last-active* timestamp; a sliding
   **`window_sec`** (default 240s) keeps the sample counting through brief throughput dips
   between video segments and stops counting ~`window_sec` after streaming truly ends. If
   active and not already blocked, the tick adds `sample_sec` to `seconds_used`; a paused/idle
   tab (no bytes) accrues **+0s**. The per-tick `[sample]` log line records `kbps=… pins=N`
   and `[pin]` lines record each new pinned IP (flagged `goog` or `GGC/off-goog`) so the
   mechanism can be audited from the world-readable log.

4. **Enforce.** Effective limit = `limit_min*60 + bonus_sec` (default limit **180 min**).
   Enforcement is **reconciled on every tick**, not latched on the transition: while
   `seconds_used >= limit`, each tick re-checks that the block is actually present and
   re-applies it if something removed it (logged as `[reblock]`). Conversely, once under
   the limit again, an auto block is lifted. To block, splice

   ```
   # === Parental block: YouTube (auto: daily budget) ===
   0.0.0.0 youtube.com
   ... (the hosts from ..\parental-blocks\youtube.txt)
   # === end YouTube block ===
   ```

   into the hosts file — **byte-preserving** (the mixed CRLF header / LF body is left
   untouched; same algorithm as `python -m safehouse_setup toggle`) — set `blocked_by_budget`,
   and flush DNS (`ipconfig /flushdns`). It also adds SafeHouse-owned inbound and outbound
   Windows Firewall rules for the pinned YouTube endpoints, which stop a pre-existing QUIC
   stream that a hosts/DNS change cannot terminate. The next day's reset removes both blocks again.

It shares the **block host list** with the manual toggle and never double-inserts: if a
`# === Parental block: YouTube ... ===` section already exists (manual or auto), it leaves
it alone. The automatic daily reset lifts only the **auto** block — a YouTube block you
applied by hand with `safehouse_setup toggle` is left in place.

Each tick is resilient: a transient error is logged and the loop continues; if the `pktmon`
counters can't be read the watcher re-arms the session and skips that sample (no phantom
time). Log: `C:\ProgramData\SafeHouse\youtube-budget.log`.

### Detection is approximate (read this)

This measures **streaming bytes to the IPs YouTube-family hostnames resolved to** — not the
video element itself, so treat the number as a good-enough budget, not a frame-accurate
stopwatch. Known limitations and the knob that tunes each:

- **False positives — other Google bulk traffic.** Google Drive sync, an Edge/Chrome update,
  or the Play-games VM ride Google IPs, but those IPs are **never pinned** (only YouTube-family
  resolutions pin an IP), so they do **not** count. The rare overlap is if the *same* IP is
  handed out for both YouTube and another Google service inside `pin_idle_expire_min`; raise
  `min_throughput_kbps` if that ever shows up.
- **Background YouTube chatter.** A paused/idle YouTube tab streams little, so it stays under
  `min_throughput_kbps` and does **not** accrue (`+0s`) — raise the threshold if low-bitrate
  audio-only playback should be ignored, lower it to catch 144p.
- **32-IP cap under heavy channel-surfing.** `pktmon` caps at 32 filters, so at most the 32
  most-recently-active pinned IPs are metered. Continuous streaming keeps the active IP alive
  (activity refresh), but a session that resolves **more than 32 IPs faster than they idle
  out** could momentarily evict an active one; `window_sec` smoothing bridges the brief gap
  and the next resolution re-pins it.
- **Self-contained DoH/DoT apps.** An app resolving names itself (own DoH/DoT) never touches
  the DNS-Client channel, so its YouTube IPs are not harvested and not pinned. Browser DoH is
  disabled by machine policy precisely so resolutions stay visible.

Tune `min_throughput_kbps`, `pin_idle_expire_min`, `window_sec`, and the limit after a few
days of real use — the `[sample]` / `[idle]` / `[pin]` log lines print `kbps=…` and `pins=…`
to guide it.

## State & config

- **State:** `C:\ProgramData\SafeHouse\youtube-budget.json` =
  `{ date, seconds_used, limit_min, bonus_sec, blocked_by_budget }`, written atomically.
- **Pins:** `C:\ProgramData\SafeHouse\yt-pinned-ips.json` =
  `[{ ip, first_seen, last_active }]` — the resolved YouTube IPs currently metered (≤ 32),
  written atomically each tick.
- **Config:** `C:\ProgramData\SafeHouse\config.json` =
  `{ limit_min, sample_sec, window_sec, min_throughput_kbps, pin_idle_expire_min }`
  (defaults 180 / 20 / 240 / 64 / 30). `limit_min` and `bonus_sec` take effect live each tick;
  changing `sample_sec`, `window_sec`, `min_throughput_kbps`, or `pin_idle_expire_min` needs
  the task restarted (`Stop-ScheduledTask` / `Start-ScheduledTask -TaskName
  SafeHouse-YouTubeBudget`, or a reboot).
- **Cache:** `C:\ProgramData\SafeHouse\goog-ranges.json` — Google's published IP prefixes,
  refetched weekly (reused if a refetch fails); used only to flag GGC/off-`goog` pins in the log.
- **ACL** (set by `install-task.ps1`): SYSTEM + Administrators = Full, **Users = read-only**.
- **Machine change:** the watcher enables the `Microsoft-Windows-DNS-Client/Operational` log
  channel (off by default) to harvest the IPs that YouTube-family hostnames resolve to.

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
- `common.ps1` — shared helpers (state, pinned-IP state, byte-preserving hosts splice,
  DNS-Client ETW IP harvest + pktmon per-IP byte metering).
- `config.json` — documented config sample (limit / sample / window / min-throughput / pin-idle-expire).
- Enforcement host list: `..\parental-blocks\youtube.txt` (shared with `safehouse_setup toggle`).
