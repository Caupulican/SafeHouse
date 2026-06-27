# YouTube daily watch-time budget

An **AdAway-style, browser-agnostic** daily watch-time limiter for YouTube, built on
the SafeHouse Pi-hole + hosts infrastructure. It *measures* how long YouTube is actually
being watched on this machine by reading Pi-hole's query log, bills it against a daily
budget, and once the budget runs out it **blocks YouTube by splicing the same hosts
section the manual toggle uses** (`windows/parental-blocks/youtube.txt`) into the live
Windows hosts file. At local midnight the budget resets and the block lifts.

Everything runs on the **Pi-hole / WSL (Debian) side**, where the kid (a Windows user
with no WSL account) cannot reach it: detection, accounting and enforcement all live
together, tamper-resistant by placement.

## How it works

Every `SAMPLE_SEC` the daemon (`youtube-budget.sh`) does three things:

1. **Detect.** It asks Pi-hole's FTL query DB (`/etc/pihole/pihole-FTL.db`, table
   `queries`) for the COUNT of **allowed** lookups (`status IN (2,3)` = forwarded/cached)
   of any YouTube *content* host (see `detect-domains.txt`) in the last `WINDOW_SEC`.
   `> 0` ⇒ "actively watching". This keys off the very hostnames the block would
   `0.0.0.0`, so it works for **any browser or app** resolving through Pi-hole — there is
   no browser extension and nothing inside the crosvm VM.

2. **Account.** It keeps a tiny JSON state file (`STATE_FILE`, root-owned `0600`):
   `{ "date", "seconds_used", "limit_min", "blocked_by_budget", "bonus_sec" }`. If today
   is active and not already blocked, it adds `SAMPLE_SEC` to `seconds_used`. The state
   resets at the local-date rollover.

3. **Enforce.** Effective limit = `limit_min*60 + bonus_sec`. When `seconds_used`
   reaches it, the daemon splices

   ```
   # === Parental block: YouTube (auto: daily budget) ===
   0.0.0.0 youtube.com
   ... (the hosts from windows/parental-blocks/youtube.txt)
   # === end YouTube block ===
   ```

   into the hosts file (byte-preserving: the CRLF header and LF body are left untouched —
   the same algorithm as `windows/parental-toggle.ps1`), then flushes Windows DNS. On the
   daily reset (or a parent `allow`/`reset`) the section is removed again, byte-for-byte.

It shares the **block host list** with the manual toggle and never double-inserts: if a
`# === Parental block: YouTube ... ===` section already exists (manual or auto), it leaves
it alone.

## Caveats (read these)

- **Approximate watch time.** This measures *DNS activity*, not video playback. A long
  video that re-resolves `googlevideo.com` only every couple of minutes still looks
  "active" thanks to the sliding `WINDOW_SEC`; conversely a paused tab that keeps polling
  can read as active. **FTL flush lag:** Pi-hole buffers queries in RAM and flushes to the
  DB only every ~30–60 s, so a multi-minute `WINDOW_SEC` (default 240 s) is required to
  absorb that lag. Treat the number as a good-enough budget, not a stopwatch — calibrate
  `WINDOW_SEC` and the limit after a few days of real use.
- **YouTube Music.** `music.youtube.com` is in the block list (shared with the manual
  toggle), so when the budget blocks "YouTube" it also blocks YouTube Music. If you want
  music to stay available, remove `music.youtube.com` from
  `../windows/parental-blocks/youtube.txt` (and decide whether music should count toward
  the budget — its content also rides `googlevideo.com`, so it *does* count as watch time).
- **DNS-only reach.** Like the rest of SafeHouse, this only sees clients that resolve
  through Pi-hole. An app using its own DoH/DoT, or a cached IP, is invisible to both
  detection and the hosts block.
- **Not the firewall layer.** This is a hosts-file (name resolution) control, separate
  from the Windows Firewall ad layer.

## Arm / disarm

This ships **disarmed** — installing it is what turns on live enforcement.

```bash
# ARM (parent, on the WSL host):
sudo ./youtube-budget/install.sh        # systemd service (cron/nohup fallback)

# DISARM:
sudo systemctl disable --now safehouse-youtube-budget.service \
  && sudo rm /etc/systemd/system/safehouse-youtube-budget.service \
  && sudo systemctl daemon-reload
# (cron fallback: sudo rm /etc/cron.d/safehouse-youtube-budget && sudo pkill -f youtube-budget.sh)
```

After disarming, if a block is currently applied, lift it with `sudo ./youtube-budget/youtube-budget-ctl.sh allow`.

## Parent CLI

```bash
sudo ./youtube-budget/youtube-budget-ctl.sh status            # used / remaining / limit / blocked today
sudo ./youtube-budget/youtube-budget-ctl.sh set-limit 90      # change daily limit (today + config.env)
sudo ./youtube-budget/youtube-budget-ctl.sh grant 30          # +30 min today; unblocks if it frees budget
sudo ./youtube-budget/youtube-budget-ctl.sh block             # force-block now
sudo ./youtube-budget/youtube-budget-ctl.sh allow             # override-unblock now
sudo ./youtube-budget/youtube-budget-ctl.sh reset             # zero today's usage + unblock
```

`allow` is an instantaneous override: it lifts the block and clears the budget flag, but
does **not** forgive accrued time, so the daemon re-arms once fresh watching crosses the
limit again. For a lasting reprieve use `grant <min>` (adds time) or `reset` (zeroes the
day).

## Tuning

All in `config.env` (sourced by the daemon and CLI; env vars override):

| Key | Default | Meaning |
|-----|---------|---------|
| `DAILY_LIMIT_MIN` | `60` | minutes of watching per local day before blocking |
| `SAMPLE_SEC` | `20` | daemon tick / billing granularity |
| `WINDOW_SEC` | `240` | sliding "is-active" window; must exceed FTL flush lag |
| `PIHOLE_CONTAINER` | `pihole` | Docker container running Pi-hole |
| `HOSTS_PATH` | live Windows hosts | enforcement target |
| `STATE_FILE` | `/var/lib/safehouse/youtube-budget.json` | accounting state (root `0600`) |
| `BLOCK_NAME` | `YouTube` | hosts marker name + which `parental-blocks/<name>.txt` to use |
| `TZ_RESET` | `local` | timezone for the midnight reset (`local` or an IANA zone) |

Raise `WINDOW_SEC` if short watching sessions are under-counted; lower `SAMPLE_SEC` for
finer accounting at the cost of more wakeups.

## Files

- `youtube-budget.sh` — the daemon loop (detect → account → enforce).
- `youtube-budget-ctl.sh` — parent CLI.
- `lib.sh` — shared helpers (state, hosts splice, detection) sourced by both.
- `detect-domains.txt` — YouTube content hosts that mean "watching".
- `config.env` — documented configuration sample.
- `install.sh` — arms it as a systemd service (cron/nohup fallback).
- Unit templates: `../automation/safehouse-youtube-budget.{service,cron}`.
