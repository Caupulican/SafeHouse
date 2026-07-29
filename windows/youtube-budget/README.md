# YouTube daily watch-time budget (Windows-native)

A **browser-agnostic, fully automatic** daily watch-time limiter for YouTube that runs
entirely on **Windows** — Python + Windows Task Scheduler. Metering does **not** depend on
WSL, Pi-hole, or Docker being up. It *measures* how long YouTube is actually being watched
on this PC by **pinning the exact IPs that YouTube-family hostnames resolve to and metering
the streaming bytes to those IPs**, bills it against a daily budget, and once the budget
runs out it **blocks YouTube on two independent lanes** (below). The budget resets — and
any block lifts — automatically on the next day.

It is tamper-resistant by placement: the watcher runs as **SYSTEM**, and its state lives
under `C:\ProgramData\SafeHouse` with an ACL that lets a standard (non-admin) user **read but not
edit or reset** it.

## The two enforcement lanes — read this first

Blocking is not one action. It is two, covering different machines, and the difference is
the single most important thing to understand about this component:

| Lane | Mechanism | Covers | Blind to |
|---|---|---|---|
| **Hosts file** | splices `..\parental-blocks\youtube.txt` into the live Windows hosts file | every program that uses the Windows resolver — Edge, Chrome, Opera GX | anything with its own resolver; wildcard CDN names |
| **Resolver** | toggles AdGuard Home's `youtube` blocked-service over its REST API | every client of the DNS chain: the **Google Play Games VM**, phones, tablets | clients that do not use the chain |

**Neither is sufficient alone.** The hosts file cannot express `*.googlevideo.com` — the
per-session CDN hostnames that carry the actual video bytes — and it is completely
invisible to the Google Play Games VM, which runs its own resolver inside `crosvm`. The
resolver lane handles both, and needs no IP tracking to do it.

This was learned the hard way. The resolver lane existed in code but was never
constructed by any caller, so for a full day the budget blocked YouTube in the browser
while the child watched it in Play Games — and every log line, and `status`, read
`BLOCKED`. Two guards now exist against a repeat:

- `status` prints a **`Resolver`** line, and says `NOT ARMED` in as many words when the
  lane is missing.
- The watcher logs the lane state at every start, at `[warn]` level when it is down.

If you read one thing after an install, read that line.

## How it works

A Scheduled Task (`SafeHouse-YouTubeBudget-Py`, trigger **AtStartup**, runs as SYSTEM with
`Restart=always`) runs `python -m safehouse_ytbudget.watcher` as a persistent loop. Every
`sample_sec` (~20s) each tick does, in order:

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

4. **Enforce.** Effective limit = `limit_min*60 + bonus_sec` (default limit **60 min**).
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
  (defaults 60 / 20 / 240 / 64 / 30). `limit_min` and `bonus_sec` take effect live each tick;
  changing `sample_sec`, `window_sec`, `min_throughput_kbps`, or `pin_idle_expire_min` needs
  the task restarted (`Stop-ScheduledTask` / `Start-ScheduledTask -TaskName
  SafeHouse-YouTubeBudget-Py`, or a reboot).
- **Resolver credentials:** `C:\ProgramData\SafeHouse\adguard.json` — base URL, admin
  user and generated password for the AdGuard Home lane, restricted to SYSTEM +
  Administrators (that dashboard can switch off every filter in the stack). Written by
  `install.py`, which reads the password `adguard/bootstrap.py` generated inside WSL.
  **Absent = the resolver lane is disarmed**, and coverage drops to Windows apps only.
- **Cache:** `C:\ProgramData\SafeHouse\goog-ranges.json` — Google's published IP prefixes,
  refetched weekly (reused if a refetch fails); used only to flag GGC/off-`goog` pins in the log.
- **ACL** (set by `install.py`): SYSTEM + Administrators = Full, **Users = read-only**.
- **Machine change:** the watcher enables the `Microsoft-Windows-DNS-Client/Operational` log
  channel (off by default) to harvest the IPs that YouTube-family hostnames resolve to.

## Arm / disarm

This ships **disarmed** — installing the task is what turns on live enforcement. Run the
installer from an **elevated** prompt:

```powershell
cd C:\SafeHouse\windows\youtube-budget
C:\SafeHouse\python\python.exe .\install.py
```

It creates the ProgramData dir + ACL, renders `config.json` from `.env`, adds the
Defender exclusions, **provisions `adguard.json` so the resolver lane is armed**,
registers `SafeHouse-YouTubeBudget-Py` (AtStartup, SYSTEM, Restart=always), starts it,
and removes any superseded watcher task.

**Exit code 2 means the task is armed but the resolver lane is not** — enforcement will
cover Windows applications only. That is a real gap, not a warning to skip past: run
`adguard/bootstrap.py` in WSL and re-run the installer.

```powershell
# Verify
Get-ScheduledTask -TaskName SafeHouse-YouTubeBudget-Py
Get-Content C:\ProgramData\SafeHouse\youtube-budget-python.log -Tail 10
# ^ the second [start] line states whether the resolver lane is armed

# Pause / disarm
Stop-ScheduledTask       -TaskName SafeHouse-YouTubeBudget-Py                  # pause
Unregister-ScheduledTask -TaskName SafeHouse-YouTubeBudget-Py -Confirm:$false  # remove
# then lift any active block (this clears BOTH lanes):
C:\SafeHouse\python\python.exe -m safehouse_ytbudget.cli allow
```

## Parent CLI

`safehouse_ytbudget.cli` drives the budget by hand, consistently with the watcher and
across both lanes. `status` is read-only; mutating verbs need an elevated prompt.

```powershell
cd C:\SafeHouse\windows\youtube-budget
$py = 'C:\SafeHouse\python\python.exe'

& $py -m safehouse_ytbudget.cli status         # used / remaining / limit / both lanes
& $py -m safehouse_ytbudget.cli set-limit 90   # change the daily limit (today + config.json)
& $py -m safehouse_ytbudget.cli grant 30       # +30 min today (unblocks if it frees budget)
& $py -m safehouse_ytbudget.cli block          # force-block now, both lanes
& $py -m safehouse_ytbudget.cli allow          # override-unblock now, both lanes
& $py -m safehouse_ytbudget.cli reset          # zero today's usage + unblock
```

`allow` is an instantaneous override: it lifts the block and clears the budget flag but does
**not** forgive accrued time, so the watcher re-arms once fresh watching crosses the limit
again. For a lasting reprieve use `grant <min>` (adds time) or `reset` (zeroes the day).

## Note on YouTube Music

`music.youtube.com` is in the shared block list (`..\parental-blocks\youtube.txt`), so when
the budget blocks "YouTube" it also blocks YouTube Music. Remove that line from the list if
you want music to stay reachable (its content also rides `googlevideo.com`, so it still
counts as watch time toward the budget).

## Files

The implementation is the `safehouse_ytbudget` package (source of truth: `python/` at the
repo root; deployed beside this README). The PowerShell watcher it replaced was removed
at cutover — running two watchers means two state files, two answers to "how much time is
left", and two processes fighting over pktmon, which is a single machine-wide session.

- `safehouse_ytbudget/watcher.py` — the loop and the scheduled-task entry point.
- `safehouse_ytbudget/decide.py` — the pure decision function. No clock, no filesystem, no
  subprocess: it takes an observation and returns the next state plus the actions needed.
  Both enforcement lanes are reconciled here.
- `safehouse_ytbudget/tick.py` — observe → decide → apply → persist, and nothing else.
- `safehouse_ytbudget/meter.py`, `pins.py`, `hosts.py`, `state.py`, `config.py` — metering
  window, pin set (32-slot cap, video-priority eviction), byte-preserving hosts splice,
  state and config.
- `safehouse_ytbudget/platform/windows.py` — everything Windows: DNS-Client ETW harvest,
  pktmon metering, netsh firewall, hosts.
- `safehouse_ytbudget/platform/adguard.py` — the resolver lane (AdGuard Home REST API).
- `safehouse_ytbudget/cli.py` — the parent CLI.
- `install.py` — installer: ProgramData + ACL, config, Defender exclusions, resolver
  credentials, the SYSTEM task.
- `.env.example` — every configuration key, documented.
- Enforcement host list: `..\parental-blocks\youtube.txt` (shared with `safehouse_setup toggle`).

The split exists so the judgement is testable without Windows: `decide()` and everything
above the platform seam run on Linux in milliseconds, and the suite covers the reconcile,
the metering window, the pin eviction and the hosts splice. In the PowerShell version the
logic and the Windows calls were the same code, so the only way to test enforcement was to
enforce it on a child's machine and watch.
