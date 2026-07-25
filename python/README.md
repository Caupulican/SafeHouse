# Python port of the YouTube budget watcher

Work in progress. The **PowerShell version in `windows/youtube-budget/` is the one that
enforces**; this runs beside it in shadow mode until it is proven. See
[../docs/PYTHON-PORT.md](../docs/PYTHON-PORT.md) for the plan and cutover criteria.

## Why port it at all

Not taste. The PowerShell version has no seam between its logic and Windows, so testing
the `.env` parser meant slicing the source between two comment markers and
`Invoke-Expression`-ing the fragment. Every bug in it was found by reading code, never by
a test. There was also no way to compute a decision without performing it, so the only
way to test enforcement was to enforce — on a real child's machine.

The port fixes exactly that: a `Platform` protocol separates every Windows call from the
budget logic, so the logic runs and is tested on Linux against a fake, and shadow mode can
decide-and-log without touching anything.

The tests earned their keep immediately. They caught, among others:

- a day reset that unblocked and then read a stale observation, opening a gap at midnight
- crediting time with zero pinned IPs, so unrelated traffic burned the budget
- an unusable byte reading being counted as idle instead of skipped
- a meter using a decaying average rather than a high-water mark, which **under-counted
  in one direction only** — measured against the live watcher: 18 disagreements, all the
  same way, quietly handing out extra time

## Layout

```
safehouse_ytbudget/
  config.py      .env + config.json -> validated Config
  state.py       today's usage; atomic writes
  decide.py      PURE: (State, Config, Observation) -> actions. No I/O
  meter.py       rolling window -> "is this video playback?"
  pins.py        the 32-address metered set, video endpoints prioritised
  hosts.py       byte-preserving hosts-file splice
  tick.py        observe -> decide -> apply -> persist
  watcher.py     the loop; --shadow runs without enforcing
  cli.py         status / set-limit / grant / block / allow / reset
  platform/
    base.py      the Protocol, and ReadOnlyPlatform (shadow mode)
    windows.py   the real implementation
    fake.py      in-memory, for tests
    adguard.py   enforcement via the AdGuard Home REST API
tools/
  diff_watchers.py   compares the two implementations; the gate for cutover
```

`decide()` is pure and is where every rule lives. If you change behaviour, change it
there and the tests will tell you what you broke.

## Running the tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
cd python && PYTHONPATH=. ../.venv/bin/python -m pytest -q
```

No runtime dependencies, ever. The watcher ships on a vendored embeddable Python and has
to start as SYSTEM at boot with nothing installed, so `safehouse_ytbudget/` imports only
the standard library. `pytest` is for the tests alone.

## Shadow mode

```powershell
C:\SafeHouse\python\python.exe install.py --shadow   # registers an observe-only task
C:\SafeHouse\python\python.exe tools\diff_watchers.py
```

Shadow mode writes to its own state and log files and refuses every mutation. One rule
learned the hard way: it must **not** reconfigure `pktmon`, which is a single global
session for the whole machine — doing so tore down the live watcher's 32 filters and left
it metering unfiltered traffic.
