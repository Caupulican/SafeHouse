# YouTube budget — quick reference

Everything below runs the vendored Python at `C:\SafeHouse\python\python.exe`. The
commands assume you are in the package directory, which is where the scheduled task runs
them from:

```powershell
cd C:\SafeHouse\windows\youtube-budget
$py = 'C:\SafeHouse\python\python.exe'
```

## Change the daily limit

Edit `.env`, then re-run the installer:

```ini
YB_LIMIT_MIN=60       # 1 hour (default)
```

```powershell
& $py .\install.py
```

`.env` is the authoring surface; the installer renders it into
`C:\ProgramData\SafeHouse\config.json`, which the watcher re-reads every tick. Every key
is documented in `.env.example`.

## Day-to-day

```powershell
& $py -m safehouse_ytbudget.cli status          # used / limit / remaining / what is blocking
& $py -m safehouse_ytbudget.cli grant 30        # +30 min, today only, cleared at midnight
& $py -m safehouse_ytbudget.cli set-limit 120   # change the daily limit (writes config.json)
& $py -m safehouse_ytbudget.cli block           # block now and hold through the reconcile
& $py -m safehouse_ytbudget.cli allow           # lift
& $py -m safehouse_ytbudget.cli reset           # zero today's usage and bonus
```

`grant` vs `set-limit`: `grant` is a one-off for today and disappears at midnight;
`set-limit` changes the standing policy. For "just tonight", use `grant`.

## Read the `Resolver` line in `status` first

```
Resolver   : AdGuard Home blocking YouTube (covers the Play Games VM)
```

AdGuard Home is the authoritative screen-time enforcement lane:

| Lane | Covers | Blind to |
|---|---|---|
| AdGuard Home | Every client of the DNS chain, including Windows and the Google Play Games VM | Clients that do not use it (see [ADGUARD.md](../../docs/ADGUARD.md)) |

If `status` says **`Resolver : NOT ARMED`**, screen-time blocking is unavailable. Fix by
running `adguard/bootstrap.py` in WSL and re-running `install.py`.

This is not hypothetical: it shipped that way, and the VM streamed unmetered for a full
day while every log line and every `status` read `BLOCKED`.

## Reading the log

`C:\ProgramData\SafeHouse\youtube-budget-python.log`

```
[sample] active +20s used=3600s/10800s blocked=False kbps=15442 pins=32 video=3
```

| Field | Meaning |
|---|---|
| `active` / `idle` | Whether this sample counted toward the budget |
| `used/limit` | Seconds watched today vs the effective limit incl. bonus |
| `kbps` | Throughput to the metered addresses |
| `pins` | Addresses currently metered (max 32) |
| `video` | How many of those are `googlevideo` endpoints |

**`video=0` during playback means the meter is blind** — it is watching thumbnails and
API chatter rather than the stream. That is the single most useful number in the line.

Other markers: `[enforce]` first block, `[reblock]` a lane had drifted open and was
re-applied, `[unblock]` lifted, `[reset]` new day, `[pin]` an address entered or left the
metered set.

The two `[start]` lines are worth reading after any reinstall — the second one states
whether the resolver lane is armed:

```
[start] python watcher ENFORCING sample=20s window=240s limit=60m ...
[start] resolver lane ARMED at http://127.0.0.1:3000 (service=youtube, currently blocking)
```

A `[warn] resolver lane DISARMED` there is a coverage gap, not a cosmetic complaint.

## Troubleshooting

| Symptom | Check |
|---|---|
| Blocked on the PC but YouTube still plays in Play Games | The resolver lane. `status` → `Resolver`. This is the classic one |
| Never blocks | Is `video=` above 0 during playback? If not, the stream is not being metered |
| Blocks too early | The 240s window keeps the clock running through pauses — tune `YB_WINDOW_SEC` |
| Watcher vanished | Antivirus quarantine. Exclude `C:\SafeHouse` in that product |
| Limit change ignored | Re-run `install.py`; `.env` alone does not apply itself |
| `[reblock]` every tick | Something outside SafeHouse is clearing a lane — check for another blocker or a hand-edited AdGuard dashboard |
