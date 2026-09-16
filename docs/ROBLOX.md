# Roblox policy

Roblox is the most-played thing on this machine, and it is not a game — it is a
platform where anyone can publish content and, by default, anyone can talk to a child.
The threat model for a 9-year-old is, in order: **stranger contact via chat**, exposure
to unmoderated experiences, spending, and only then advertising. DNS can enforce some of
this; the account settings enforce the rest. Neither alone is enough, and — as
everywhere in SafeHouse — each control lists how to *verify* it, because a control that
merely reports success is the failure mode this repo exists to fight.

## Layer 1 — Roblox account (set by the parent, locked with a PIN)

These live in Roblox's own parental controls. Network blocking cannot substitute for
them: chat and content maturity are decided server-side per account.

| Setting | Policy | Why |
|---|---|---|
| Linked parent account + settings PIN | **Required** | The child has local admin on the PC by choice; the PIN is what stops settings drifting back |
| Account birthdate | **Real age (9 as of 2026-08-30)** | Drives ad removal (Roblox does not serve ads to under-13s), chat defaults, content defaults. Still under 13, so nothing about this changed when he turned 9 — but the birthdate on the account must be the real one, not a stale one. |
| Content maturity | **Mild** — raised from Minimal on 2026-08-30, the parent's call at age 9 | Filters moderate/restricted experiences from discovery and join. See Layer 4: this setting was measured **not enforcing** on this account. |
| Chat | **Fully off** — no platform chat, no in-experience chat | The single highest-risk feature for this age; the games all remain playable |
| Who can message / invite / join me | **No one** | Belt-and-suspenders once chat is off |
| Voice chat | **Off** (should be impossible under 13 anyway) | Also hard-blocked at DNS — see Layer 2 |
| Spending | **Purchases blocked** (or hard monthly cap) | Robux prompts are pervasive in-experience |
| Daily screen time (Roblox-native) | **Lowest value Roblox offers** | The standing allowance is 0 min/day (see Layer 3) and Roblox's own control cannot express zero; set it as low as it goes so the account-side lane still helps on devices this repo does not manage |

**Verify:** log into the parent account view and confirm each setting; then, from
the child's session, open a busy experience and confirm the chat window does not exist,
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

Policy: **a daily allowance counted from first use, after which the game is ended until
the daily reset.** The standing allowance is **0 min/day** — that is what `install.py`
seeds and what any broken config falls back to; a parent grants time deliberately by
raising `limit_min` (60 for the hour the account-side lane in Layer 1 is set up for), or
by dropping a today-only `bonus_sec` into the state file. Enforced by
`safehouse_ytbudget/roblox_guard.py`, registered as the `SafeHouse-RobloxBudget` task by
`install.py` (or `install.py --roblox-guard-only` to add just this lane).

The enforcement is a **process kill**, and that choice is load-bearing. For YouTube the
resolver block is enough because streaming re-resolves constantly; a joined Roblox
session holds a raw UDP connection to Roblox's own address space (`128.116.0.0/17`) and
never resolves a name again, so a DNS block only prevents *new* joins. Ending the
process is the only mid-session enforcement that works.

Mechanics — a one-shot, not a loop: the task runs at boot and on registration and
repeats every minute. Each run observes whether Roblox is up (`RobloxPlayerBeta.exe`,
its launcher, or the Store app's `Windows10Universal.exe` host) with **one** unfiltered
`tasklist /FO CSV /NH` dump, accrues wall-clock time between sightings (capped, so a
sleeping machine is not charged), recomputes the kill verdict from scratch — reconcile,
never latch — and `taskkill /T`s everything once the allowance is spent. The original
design wanted a task *event-triggered* on process start; that is not implementable —
scheduled-task event filters speak an XPath subset with no substring matching, and
Roblox's executable path changes every update — so the one-minute repetition is the
launch detector, and a launch on a spent budget survives at most a minute.

**A run that finds the game closed does nothing at all** — no log line, no state write,
no second process. That is the condition attached to letting a task fire every minute of
every day: unconditional persistence was ~1,440 atomic write-and-rename cycles a day on
a machine where Roblox may never have been opened, and a log line per minute buries the
runs that matter. One consequence worth knowing: `roblox-budget.state.json` does not
exist until the game is first played.

Timing, honestly: the kill is **not** instant, by design and by shape. Up to ~60s passes
before a launch or an expiry is noticed (the task's finest repetition), and then the run
waits a random **1–5s**, looks again, and ends only what is still up. The pause is a
small part of that total; its job is that the exact moment cannot be timed and raced, and
that a child who quits during it is not logged as a kill that never happened. Bounds live
in `roblox-budget.json` as `delay_min_sec`/`delay_max_sec` (clamped to 60s — the task's
`ExecutionTimeLimit` is PT5M, and a pause the scheduler terminates is a game that
survives).

State: `roblox-budget.state.json` under `C:\ProgramData\SafeHouse` (readable by the
child, like the YouTube state — he may see how much is left; only SYSTEM/admins may
edit). Limit override: `roblox-budget.json`, `{"limit_min": 60}`; **an absent or broken
value falls back to 0, never to unlimited** — a typo takes the game away rather than
handing out an hour nobody granted. A one-off `bonus_sec` in the *state* file grants
extra time for today only and dies with the date rollover. Log: `roblox-budget.log`.

This lane is deliberately independent of the YouTube watcher task — own state, own
log, no pktmon, no shared session — so disabling one cannot disable the other.

**Verify:** `schtasks /Query /TN SafeHouse-RobloxBudget` shows Ready and a last-run
within the minute; `roblox-budget.state.json` appears and `used_sec` climbs during play;
set `{"limit_min": 1}` in `roblox-budget.json`, launch Roblox, and the game must die
within about a minute plus a few seconds (then remove the override). Also verify the
quiet path: with Roblox closed, `roblox-budget.log` must not grow and the state file's
timestamp must not move, however many times the task fires.

**Before arming, add the AV exclusion.** Avast quarantined the earlier watcher on this
machine — script and scheduled task both — and enforcement then dies silently. The
installer adds a Microsoft Defender exclusion; third-party AV needs its own, added by
hand in that product. And note the standing honesty of this setup: the child has local
admin by choice, so he *can* stop the task. The point of these lanes is that doing so is a
visible, deliberate act, not that it is impossible.

## Layer 3a — granting time, and taking it back

The allowance is a **scalar with no day awareness**: `roblox-budget.json` holds one
`limit_min` for every day of the week. There is no Roblox equivalent of the panel's
YouTube schedule engine, so "this weekend only" is not something the config can say. It
has to be granted and then un-granted, and the un-granting is the part that gets
forgotten — a raised `limit_min` stays raised until somebody remembers the file exists.

Two grant shapes, and picking the wrong one is the usual mistake:

| Grant | Where | Ends by itself |
|---|---|---|
| **One day** | `bonus_sec` in `roblox-budget.state.json` | Yes — the date rollover discards it |
| **Several days** | `limit_min` in `roblox-budget.json` | **No** — needs an expiry task |

`bonus_sec` cannot span a weekend: it is scoped to the state file's `date` and dies at
midnight. Reaching for it on a Friday to cover Sunday is the shape of bug that hands a
child a locked game on Saturday morning with no explanation.

### The expiry task

For a multi-day grant, register a one-shot scheduled task that writes `limit_min` back
to the standing value and then deletes itself. Three properties make it safe, and all
three have bitten in practice:

* **Two triggers.** A `TimeTrigger` at the expiry instant, *and* a `BootTrigger` — the
  midnight run never happens if the machine is off, and `StartWhenAvailable` only
  covers a *missed* scheduled run, at an arbitrary later time.
* **Date-aware**, exactly like `adguard_daily_reset.py`. The boot backstop fires every
  boot, including Saturday's, so a run before the expiry date must do nothing at all —
  no write, no log line. Without this the "backstop" ends the grant early.
* **Once-only**, via a marker file next to the state. The task deletes itself after a
  successful revert, but a delete that fails leaves a zombie firing at every future
  boot — and that zombie would silently zero out the *next* grant a parent makes. The
  marker is what makes a lingering task inert instead of harmful.

Register it with `schtasks /Create /XML` and a **UTF-16** file, not `/SC ONCE /SD`:
this machine is pt-BR, so `/SD` expects `dd/MM/yyyy`, while an ISO-8601
`<StartBoundary>` in the XML is locale-independent. Same reason `install.py` registers
every task from XML.

**Verify:** `schtasks /Query /TN <task> /FO CSV /V` shows the expected next run; then
exercise the script against a throwaway root for all four paths — before the expiry
date (does nothing), on it (reverts and self-deletes), after a revert with the marker
present (does nothing, even if the config was re-granted), and with an unreadable
config (leaves it alone; the guard already falls back to 0).

### The panel cannot express a full day

The panel's Roblox "Por dia" slider is `max="240"` (`ROBLOX_LIMIT_RANGE` in
`safehouse_panel/controls.py`), and `set_roblox_limit` clamps to it. A file-level grant
above 240 minutes therefore **cannot be shown or written from the panel** — the slider
pins to 240, and a parent who touches it and presses Aplicar silently cuts the grant to
four hours. Grant and revoke a full-day allowance in the file, or keep the grant inside
240 minutes so the panel stays truthful.

## Layer 4 — which games, not how long (LIVE, ships OFF)

Layer 3 answers *for how long*. This one answers *which*, and the two are independent
tasks with independent config so that standing one down does not touch the other.

### Why the process table cannot do it

Every experience on the platform runs as the same `RobloxPlayerBeta.exe`. Image name,
command line and window title are identical for a tycoon and for a horror game — so the
signal every other kill lane uses cannot tell them apart. The client's own log can.
Verified against Roblox **0.736.0** on this machine on 2026-08-30, a join writes:

```
[FLog::Output] ! Joining game '<jobId>' place 88083000467860 at 10.34.1.86
[FLog::GameJoinLoadTime] Report game_join_loadtime: placeid:88083000467860,
    join_time:0.669, universeid:9681996188, referral_page:, sid:..., userid:...
```

and leaving writes `[FLog::SingleSurfaceApp] leaveUGCGameInternal`. Two consequences:

* the **universeid is in the log**, so the enforcing path needs no network call, no API
  key and no place→universe lookup. It keeps working with the internet down;
* one join writes **two** lines, the plainer one first. Reading them as two events
  double-counts every session and makes the universe flicker in and out — the parser
  folds them.

### Match on the universe, never the place

A *game* is a universe; a *place* is one map inside it, and teleporting between maps
keeps the process and writes a fresh join line with a different place. Two of the
fourteen games in this machine's own history do exactly that:

| Universe | Game | Places seen |
|---|---|---|
| `4807308814` | Story Game A | `13864661000`, `13864667823` |
| `2955322961` | Obby Game B | `7606564092`, `88604154038333` |

A place-keyed list would have allowed the lobby and killed the level.

### Shape

Resident, ~3s poll, SYSTEM — and each of those three differs from the budget lane for a
reason:

| | Layer 3 (budget) | Layer 4 (games) |
|---|---|---|
| Shape | one-shot, every minute | **resident loop** |
| Why | a minute of overtime is a rounding error | a minute of a banned game per launch is a **loophole** — quit, re-join, farm another minute |
| Principal | SYSTEM | SYSTEM (it kills, and it must read *every* profile's log) |
| Input | `tasklist` | `%LOCALAPPDATA%\Roblox\logs\*_Player_*.log`, tailed |

The idle path still touches nothing: a tick is one directory listing, and when nothing
has been appended it stops there. `tasklist` is spawned only on a tick that has already
decided to kill.

### Config, and how it fails

`roblox-games.json`:

```json
{ "enabled": true, "mode": "allow",
  "universes": [{"id": 10342266608, "name": "Tycoon Game H"}],
  "notify": false, "delay_min_sec": 1, "delay_max_sec": 5 }
```

| Situation | What happens |
|---|---|
| Unlisted universe, `allow` mode | **killed** — the point of the lane |
| Universe unreadable from the log | killed under `allow`, spared under `deny` — unknown resolves to *enforce the rule*, never to a guess |
| Config **missing** | lane asleep. It is opt-in, and installing it must not brick Roblox on a machine nobody configured |
| Config **broken** | falls back to the **last good policy this process parsed**; nothing, if there has never been one. Logged every episode and shown in the panel as a fault |
| `allow` mode, empty list | ends *every* experience. Legal, and said out loud in the log and on the card — it is also what an emptied-by-accident list looks like |

The broken-config fallback is the one place this repo does **not** fail to the strictest
setting. The strict fallback here would be "no game at all", enforced in a loop, because
of a stray comma in Notepad — a policy nobody wrote down. It shouts instead.

### Building the list

Nobody can write an allow list for a platform with sixty million experiences on it.
`--history` reads every client log still on disk and prints what was actually played:

```
python -m safehouse_ytbudget.roblox_games --history
```

It also seeds `roblox-games.seen.json`, the ledger the panel draws its checkboxes from.
The guard appends to that ledger as it observes joins — **joins, never minutes**: time
played is Layer 3's number, and a second clock here would eventually disagree with it in
front of a parent trying to decide something.

In the panel, the **Jogos do Roblox** card carries the switch, the mode, and one ticked
row per game seen, newest first. Ticking a game under an allow list is a *weakening*, so
it asks for the password again; under a deny list the direction reverses and the panel
knows it.

### Content maturity — a label, not a control

Roblox rates every experience (`minimal` / `mild` / `moderate` / `restricted`). The only
public surface carrying it is the omni-search API, so `roblox_games.fetch_maturity` looks
each game up **by name** and accepts the answer only when the universe id matches. It is
**curation-time only** — the kill path never touches the network, because a filter that
opens up when DNS breaks is not a filter.

Measured against this machine's own history on **2026-08-30**, two results decide how it
is used:

| Measurement | Consequence |
|---|---|
| **7 of the 15 games played are rated `mild`** — above the account's documented "Minimal" setting — and Roblox started them anyway | Layer 1's content maturity is **not enforcing on this account**. Re-verify it; do not rely on it. |
| **"Horror Game F"** — the horror one — is rated **`minimal`** | A maturity ceiling would not have caught the first game a parent objected to. |

So the rating is **shown, never trusted**: `max_maturity` (default `minimal`) produces a
note on the card and a line in `--status` for any listed game above it, and the game still
runs. The list wins. Refusing to enforce a list the parent wrote would be this code
deciding it knows better than the person who set the rule.

The mild ones, for the record: `Idle Game J`, `Builder Game K`, `Escape Game L`,
`Tycoon Game C`, `Story Game A`, `Obby Game B`, `Shooter Game D`.

### "Block anything the content filter does not cover"

Under `mode: "allow"` this is already true **by construction**: nothing that is not
explicitly ticked ever runs, so every unrated, mild, moderate and restricted experience
is ended whether or not anyone thought about it. The maturity work above exists to make
the *curation* informed, not to add a second gate. Under `mode: "deny"` the property does
not hold — that mode permits everything it has not been told about — which is why `allow`
is the documented default for a young child.

### As deployed, 2026-08-30

`SafeHouse-RobloxGames` armed at 14:30 BRT, `mode: allow`, ceiling `mild`, **26 games**.
Two unattended kills on the first afternoon, both correct:

| Time | Game | Rating |
|---|---|---|
| 14:30:22 | Mining Game I | `minimal` (since added to the list) |
| 18:18:12 | **Horror Game E** — horror chase, 5.0B visits | **`mild`** |

Horror Game E is the one to remember: it is *inside* the `mild` ceiling and was stopped
only because it is not on the list. A maturity setting alone would have let it run. **The
list is the control; the rating is a label.**

The list was curated from what the child actually plays, not from a genre browse. Two
days of his own history, 359 minutes over 17 games:

| Category | min | Share |
|---|---|---|
| Tycoon / manage | 133 | 37% |
| Upgrade & idle loops (incl. the two "mining sandbox" games) | 123 | 34% |
| Survival / combat | 54 | 15% |
| Horror / chase story | 34 | 9% |
| Party minigame | 15 | 4% |
| **Genuine construction sandbox** | **0** | **0%** |

That last row is the point of the list. Sandbox Game M, Sandbox Game N, Sandbox Game O
and Tycoon Game P are all on it and none has been opened yet; the shallow
versions he already likes were **kept, not removed**, because a list that deletes a
child's favourites gets argued with and a list that quietly grows better neighbours does
not. The daily budget in Layer 3 is what limits *how much*.

Beware the genre tag when curating: `Simulation/Sandbox` covers both Sandbox Game M (a
real physics sandbox) and "Mining Game I" / "Mining Game Q" (dig-and-upgrade loops).
Read the description, not the label.

### Honest limits

* the kill ends the **whole client**, not one experience — he lands on the desktop, not
  on the Roblox home page. Re-opening into an allowed game is immediate;
* there is a few-second window between the join and the kill. He sees the loading
  screen, sometimes the spawn point;
* the log is his own file and he has local admin. Roblox running while its log has gone
  silent is logged as `[warn] blind` rather than read as an all-clear;
* the Microsoft Store build logs elsewhere. That path is included **best-effort and
  untested** — no Store install exists here.

**Verify:** `--status` prints the policy; `--once` reads the live log and prints the
verdict without killing anything; then tick a game off in the panel, join it, and Roblox
must close within a few seconds — `roblox-games.log` names the game and the reason.
Also verify the quiet path: with Roblox closed, the log must not grow.

## What is deliberately NOT blocked

In-experience "ads" that are really content — developer-placed billboards and
ad-lookalike props — arrive over the same asset pipeline as everything else in the
game. No network control can remove them without removing the game. The lever that
works is Layer 1: an under-13 account does not get *official* ad units at all.
