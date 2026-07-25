# Porting the YouTube budget watcher to Python

**Rule for this work: the PowerShell version stays armed and working until the Python one
is proven. No half-migrated state — for a parental control, half-migrated means
enforcement is silently off and the kid has unlimited access.**

Baseline verified live on 2026-07-25: task `SafeHouse-YouTubeBudget` Running as SYSTEM,
`limit_min=180` flowing from `.env` → `config.json` → enforcement, block applies at limit
(`youtube.com A = [0.0.0.0]`) and lifts when the limit is raised.

## Why Python is worth the port

Not aesthetics — three concrete defects of the PowerShell version:

1. **It is effectively untestable.** To test the `.env` parser I had to slice the source
   file between two comment markers and `Invoke-Expression` the fragment. There is no
   seam between logic and Windows. Every regression so far (the latching enforcement bug,
   the frozen `limit_min`, the `NOIPS` log spam) was found by reading code, not by a test.
2. **Text munging where structured data exists.** `pktmon counters` emits JSON; the PS
   version parses it as text. The hosts file splice hand-rolls byte preservation across
   mixed CRLF/LF because PowerShell's file cmdlets rewrite line endings and BOMs.
3. **No safe way to dry-run.** There is no mode that computes the decision without
   touching the live hosts file, so the only way to test enforcement is to enforce.

Python fixes all three: `pytest`, `json.loads`, binary-mode file I/O, `os.replace` (a
genuinely atomic rename on Windows), and a platform seam that can be faked.

## What must be preserved exactly

The port is not a rewrite of behaviour. These are load-bearing and were each learned the
hard way:

| Behaviour | Why it exists |
|---|---|
| Enforcement **reconciles every tick**, never latches on a transition | The original bug: once `blocked_by_budget` was set, nothing re-verified the block, so anything that removed it went unnoticed forever |
| `manual_block` distinct from `blocked_by_budget` | So the declarative reconcile does not lift a deliberate `ctl block` when usage happens to sit under the limit |
| Date-based idempotent reset, not a midnight timer | Survives shutdowns and multi-day-off periods |
| Byte-preserving hosts splice | The file has a CRLF header and an LF body; rewriting it wholesale corrupts it and Windows silently ignores a malformed hosts file |
| Pins expire on **inactivity**, not DNS recency | A coalesced QUIC session stops re-resolving but keeps streaming |
| 32-pin cap | pktmon filter limit |
| Inbound bytes = **max across components**, not sum | pktmon counts the same packet at several layers |
| Bad config values are rejected, never applied | A typo must not hand out unlimited YouTube |

## Target layout

```
python/
  safehouse_ytbudget/
    __init__.py
    config.py        # .env + config.json -> Config (validated, frozen)
    state.py         # State dataclass; atomic load/save via os.replace
    decide.py        # PURE: (State, Config, Observation) -> Decision.  No I/O.
    pins.py          # pin set: 32 cap, activity-based expiry
    meter.py         # rolling window -> kbps -> "is watching?"
    tick.py          # orchestration: observe -> decide -> apply -> persist
    watcher.py       # the loop; entrypoint for the scheduled task
    cli.py           # ctl verbs: status/set-limit/grant/block/allow/reset
    platform/
      base.py        # Protocol: hosts, firewall, pktmon, dns, flush, clock
      windows.py     # real implementation
      fake.py        # in-memory, for tests
  tests/
  install.py         # ACL (icacls), AV exclusion, task XML, schtasks /Create
  pyproject.toml
```

The seam is `platform/base.py`. Everything above it is pure Python that runs and is
tested on Linux; everything Windows-specific lives below it.

## Runtime: vendored embeddable Python

The task runs as SYSTEM at boot, before any user logs on. It must not depend on a Python
in the kid's user profile or on `PATH`.

Ship the **embeddable Python distribution** unzipped into `C:\SafeHouse\python\`. It is
self-contained, needs no installer, no registry entry, no admin, and pins the exact
interpreter version. The scheduled task calls
`C:\SafeHouse\python\python.exe -m safehouse_ytbudget.watcher`.

Dependencies: **none**. Everything needed is in the stdlib plus `ctypes` for the two
Windows APIs that matter. Avoiding `pywin32` keeps the embeddable dist trivial to vendor.

## Windows integration: how each piece is done without pywin32

| Concern | PowerShell today | Python |
|---|---|---|
| Resolved-IP harvest | `Get-WinEvent` on `Microsoft-Windows-DNS-Client/Operational` id 3008 | `ctypes` against `wevtapi.dll` (`EvtQuery`/`EvtNext`/`EvtRender`); fall back to `wevtutil qe /f:xml` and parse |
| Byte metering | `pktmon counters` text parse | `pktmon counters --json` + `json.loads` |
| Hosts splice | hand-rolled byte preservation | `open(..., 'rb')`, splice bytes, `os.replace` |
| Firewall rules | `New-NetFirewallRule` | `netsh advfirewall firewall` (auditable, no COM) |
| DNS flush | `ipconfig /flushdns` | same, `subprocess` |
| State write | `Move-Item -Force` | `os.replace` — atomic by contract |
| ProgramData ACL | .NET `Get-Acl`/`Set-Acl` | `icacls` in `install.py` |
| Task registration | `Register-ScheduledTask` | task XML + `schtasks /Create /XML` |

Two calls are worth the `ctypes` effort (the event log query); the rest are subprocess
calls to tools that are already the de-facto API.

## Migration — how we actually cut over

**Phase 1 — build.** Write the package and its tests. Nothing is installed on Windows.
Pure logic (`decide`, `pins`, `meter`, `config`, `state`) is tested on Linux against
`platform/fake.py`, including a fake clock. Definition of done: the behaviour table above
is covered by tests, including the regressions — latching, frozen `limit_min`, bad `.env`
values.

**Phase 2 — shadow mode.** Install the Python watcher alongside the PowerShell one with
`--shadow`: it observes, decides and logs to `youtube-budget-python.log`, but **never**
touches hosts, firewall or DNS. PowerShell stays the only thing enforcing. Run both for
several days.

**Phase 3 — diff.** Compare the two logs tick by tick. The Python watcher must agree with
PowerShell on `seconds_used` and on every block/unblock decision. Disagreements are bugs
in the port, and this is the only way to find them without experimenting on a child's
access.

**Phase 4 — cutover.** Only once the logs agree: stop and unregister
`SafeHouse-YouTubeBudget`, register `SafeHouse-YouTubeBudget-Py` without `--shadow`,
verify a forced block/unblock cycle live (the same test run on 2026-07-25).

**Phase 5 — retire.** Keep the PowerShell scripts in the repo for one release as a
documented rollback, then delete.

## Improvements to make during the port (not straight transliteration)

- **One source of truth for the limit.** `config.json` is authoritative; the state file
  holds usage and today-only overrides (`bonus_sec`, `manual_block`) and nothing else.
  The PS version stores `limit_min` in both and had to be patched to re-sync them.
- **Pure decision function.** `decide()` takes a snapshot and returns an action. No
  clock, no files. This is what makes the regressions testable.
- **Structured log.** JSONL beside the human-readable log, so `status` and any future
  reporting are computed rather than scraped.
- **`--dry-run` on every mutating path**, which is what makes shadow mode possible.
