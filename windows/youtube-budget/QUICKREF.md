# YouTube budget — quick reference

## Change the daily limit

Edit `.env`, then re-run the installer:

```ini
YB_LIMIT_MIN=180      # 3 hours (default)
```

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

`.env` is the authoring surface; the installer renders it into
`C:\ProgramData\SafeHouse\config.json`, which the watcher re-reads every tick. Every key
is documented in `.env.example`.

## Day-to-day

```powershell
$ctl = 'C:\SafeHouse\windows\youtube-budget\youtube-budget-ctl.ps1'

& $ctl status          # used / limit / remaining / what is blocking
& $ctl grant 30        # +30 min, today only, cleared at midnight
& $ctl set-limit 120   # change the daily limit (writes config.json)
& $ctl block           # block now and hold through the reconcile
& $ctl allow           # lift
& $ctl reset           # zero today's usage and bonus
```

`grant` vs `set-limit`: `grant` is a one-off for today and disappears at midnight;
`set-limit` changes the standing policy. For "just tonight", use `grant`.

## Reading the log

`C:\ProgramData\SafeHouse\youtube-budget.log`

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

Other markers: `[enforce]` first block, `[reblock]` something removed the block and it was
re-applied, `[unblock]` lifted, `[reset]` new day, `[pin]` an address entered or left the
metered set.

## Troubleshooting

| Symptom | Check |
|---|---|
| Never blocks | Is `video=` above 0 during playback? If not, the stream is not being metered |
| Blocks too early | The 240s window keeps the clock running through pauses — tune `YB_WINDOW_SEC` |
| Watcher vanished | Antivirus quarantine. Exclude `C:\SafeHouse` in that product |
| Limit change ignored | Re-run `install-task.ps1`; `.env` alone does not apply itself |
