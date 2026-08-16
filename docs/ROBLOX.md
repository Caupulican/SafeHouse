# Roblox policy

Roblox is the most-played thing on this machine, and it is not a game — it is a
platform where anyone can publish content and, by default, anyone can talk to a child.
The threat model for an 8-year-old is, in order: **stranger contact via chat**, exposure
to unmoderated experiences, spending, and only then advertising. DNS can enforce some of
this; the account settings enforce the rest. Neither alone is enough, and — as
everywhere in SafeHouse — each control lists how to *verify* it, because a control that
merely reports success is the failure mode this repo exists to fight.

## Layer 1 — Roblox account (set by the parent, locked with a PIN)

These live in Roblox's own parental controls. Network blocking cannot substitute for
them: chat and content maturity are decided server-side per account.

| Setting | Policy | Why |
|---|---|---|
| Linked parent account + settings PIN | **Required** | child has local admin on the PC by choice; the PIN is what stops settings drifting back |
| Account birthdate | **Real age (8)** | Drives ad removal (Roblox does not serve ads to under-13s), chat defaults, content defaults |
| Content maturity | **Minimal** | Filters moderate/restricted experiences from discovery and join |
| Chat | **Fully off** — no platform chat, no in-experience chat | The single highest-risk feature for this age; the games all remain playable |
| Who can message / invite / join me | **No one** | Belt-and-suspenders once chat is off |
| Voice chat | **Off** (should be impossible at age 8 anyway) | Also hard-blocked at DNS — see Layer 2 |
| Spending | **Purchases blocked** (or hard monthly cap) | Robux prompts are pervasive in-experience |
| Daily screen time (Roblox-native) | **60 min/day** | First enforcement lane for the 1 h/day budget; enforced by Roblox account-side on any device |

**Verify:** log into the parent account view and confirm each setting; then, from
child's session, open a busy experience and confirm the chat window does not exist,
and try to send a friend request message. Re-check after any Roblox client update.

## Layer 2 — network (AdGuard Home user rules)

Applied 2026-08-16, live in AdGuard's user rules:

```
||voice.roblox.com^              ! voice signaling — voice cannot connect even if enabled account-side
||adconfiguration.roblox.com^    ! immersive/official ad units fetch their config here
||adserver.roblox.com^
||ads.roblox.com^
```

Game traffic is deliberately untouched: `assetdelivery.roblox.com`, `*.rbxcdn.com` and
the `128.116.0.0/17` game servers are what Roblox *is*. Blocking at that level breaks
the platform rather than filtering it. Telemetry (`ecsv2`, `tracing`, `lms`, `metrics`)
is already covered by the filter lists.

**Verify:** `nslookup voice.roblox.com <dns-ip>` → `0.0.0.0`, and
`nslookup assetdelivery.roblox.com <dns-ip>` → a real address. Both matter — the
second proves the block did not over-reach.

These rules are IaC: `adguard/user-rules.txt` is the source of truth and
`adguard/apply-filters.py` reconciles the running instance to it (add-only — rules a
parent adds by hand in the UI survive). A rebuild restores them; drift is repaired by
re-running the script, which the ansible role does on every play.

## Layer 3 — the time budget (LIVE since 2026-08-16)

Policy: **max 1 hour of Roblox per day, counted from first use, then the game is ended
until the daily reset.** Enforced by `safehouse_ytbudget/roblox_guard.py`, registered
as the `SafeHouse-RobloxBudget` task by `install.py` (or `install.py
--roblox-guard-only` to add just this lane).

The enforcement is a **process kill**, and that choice is load-bearing. For YouTube the
resolver block is enough because streaming re-resolves constantly; a joined Roblox
session holds a raw UDP connection to Roblox's own address space (`128.116.0.0/17`) and
never resolves a name again, so a DNS block only prevents *new* joins. Ending the
process is the only mid-session enforcement that works.

Mechanics — a one-shot, not a loop: the task runs at boot and on registration and
repeats every minute. Each run observes whether Roblox is up (`RobloxPlayerBeta.exe`,
its launcher, or the Store app's `Windows10Universal.exe` host), accrues wall-clock
time between sightings (capped, so a sleeping machine is not charged), recomputes the
kill verdict from scratch — reconcile, never latch — and `taskkill /T`s everything if
the hour is spent. The original design wanted a task *event-triggered* on process
start; that is not implementable — scheduled-task event filters speak an XPath subset
with no substring matching, and Roblox's executable path changes every update — so the
one-minute repetition is the launch detector, and a launch on a spent budget survives
at most a minute.

State: `roblox-budget.state.json` under `C:\ProgramData\SafeHouse` (readable by the
child, like the YouTube state — he may see how much is left; only SYSTEM/admins may
edit). Limit override: `roblox-budget.json`, `{"limit_min": 60}`; a broken value falls
back to 60, never to unlimited. Log: `roblox-budget.log`.

This lane is deliberately independent of the YouTube watcher task — own state, own
log, no pktmon, no shared session — so disabling one cannot disable the other.

**Verify:** `schtasks /Query /TN SafeHouse-RobloxBudget` shows Ready and a last-run
within the minute while Roblox is open; `roblox-budget.state.json` shows `used_sec`
climbing during play; set `{"limit_min": 1}` in `roblox-budget.json`, launch Roblox,
and the game must die within about a minute (then remove the override).

## What is deliberately NOT blocked

In-experience "ads" that are really content — developer-placed billboards and
ad-lookalike props — arrive over the same asset pipeline as everything else in the
game. No network control can remove them without removing the game. The lever that
works is Layer 1: an under-13 account does not get *official* ad units at all.
