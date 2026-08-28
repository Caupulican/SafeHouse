# The control panel

One web page that shows everything SafeHouse is enforcing on the Windows box, and lets the
settings that matter be changed with a slider. It runs **on the child's PC** and is read
**from another device on the house network** — a phone, a tablet, the parent's laptop.

    C:\SafeHouse\python\python.exe -m safehouse_panel

Package: `python/safehouse_panel/`. Standard library only, like everything else that ships
to that machine (it runs on a vendored embeddable Python with nothing installed).

---

## Launching it

It is a **scheduled task**, not something you start by hand: the parent needs it reachable
at any hour, so it comes up at boot and is restarted if it dies.

```powershell
# Register the task + open the firewall port. Elevated.
cd C:\SafeHouse\windows\youtube-budget
C:\SafeHouse\python\python.exe .\install.py --panel-only

# The resolver watchdog, which keeps the filtering chain alive. Elevated, and run
# WHILE SIGNED IN AS THE ACCOUNT WSL IS REGISTERED TO - it runs as that user.
C:\SafeHouse\python\python.exe .\install.py --resolver-only --distro Debian

# Set the password (see below). Elevated, on that machine.
C:\SafeHouse\python\python.exe -m safehouse_panel set-password --generate
```

A full `install.py` run registers both; `--no-panel` / `--no-resolver` skip them, and
`--panel-port N` moves the port.

| | |
|---|---|
| Task | `SafeHouse-Panel`, SYSTEM, hidden, `BootTrigger`, `ExecutionTimeLimit PT0S`, `RestartOnFailure` every minute |
| Listens on | `127.0.0.1:8787` **and** the primary LAN address (`192.168.1.50:8787` today) |
| Firewall | `SafeHouse-Panel`, inbound TCP 8787, `remoteip=LocalSubnet`, private+domain profiles |
| Reads/writes | `C:\ProgramData\SafeHouse\*.json`, `panel.log` |

Other commands — all run from `C:\SafeHouse\windows\youtube-budget`, since that is where
the packages live and `-m` resolves modules from the working directory (the scheduled task
sets it as its `WorkingDirectory`):

```powershell
C:\SafeHouse\python\python.exe -m safehouse_panel self-test     # print one snapshot as JSON, bind nothing
C:\SafeHouse\python\python.exe -m safehouse_panel set-password   # prompt twice (needs a console)
C:\SafeHouse\python\python.exe -m safehouse_panel set-password --stdin   # read it from a pipe
C:\SafeHouse\python\python.exe -m safehouse_panel --loopback-only        # do not listen on the LAN
```

It **refuses to start unelevated.** `C:\ProgramData\SafeHouse` is SYSTEM+Administrators;
as a standard user the panel would show you the settings and then fail to write them, and
a control that silently does nothing is the worst outcome on this page.

### Nothing is ever shown on the guarded machine

No browser is opened, no console window, no toast, no tray icon. The task is `Hidden`,
runs as SYSTEM, and its only output is `panel.log`. Two reasons: the child is not meant to
know the panel exists, and a browser opened on that PC would be closed by the browser
guard within a minute anyway.

If you ever *do* need to read the panel on that PC, unlock browsing first (the "unlock for
N minutes" control) — otherwise the guard closes the window you are reading it in.

### The password

There is no password until you set one. Until then the panel serves only an instructions
page: no dashboard, no controls, no login.

Three ways to set it, none of which put it on that machine's screen:

```powershell
# Pick one yourself, from a pipe. The minimum is 4 characters.
'your-password' | C:\SafeHouse\python\python.exe -m safehouse_panel set-password --stdin

# Or have one invented for you.
C:\SafeHouse\python\python.exe -m safehouse_panel set-password --generate
```

`--generate` writes it to **`C:\ProgramData\SafeHouse\panel-password.txt`**, which
inherits the directory's ACL (SYSTEM + Administrators), because nothing may be displayed on
that machine and the task has no console to print to.

> Copy the password into your password manager and **delete `panel-password.txt`.** The
> panel keeps only a scrypt hash; that file is the only copy of the password itself.

**Changing it afterwards** is done from the panel, under **Acesso ao painel / Panel
access** on the Sistema tab. It asks for the
current password — a session left open on an unlocked phone must not be enough to lock you
out of your own controls — and on success it **signs every other device out**, including
any phone still holding a session. The browser doing the change is issued a fresh cookie in
the same response, so you are not thrown out of the page you are standing in.

#### On short passwords

The floor is **four characters**, not eight, deliberately. A rule that stops a parent
setting the password they actually want just moves that password onto a note beside the
keyboard. What holds the line instead is the **lockout: six wrong tries from one address
and that address waits fifteen minutes**, with a doubling stall before that. A four-digit
PIN is not brute-forceable over a house LAN against that.

The panel says so once, plainly, when you set a weak one — and then accepts it. Worth
knowing what a short password is leaning on here:

* there is **no TLS**, so the login crosses the LAN in clear;
* the child has **local administrator** on the machine the panel runs on, so he can read
  `panel.log` — which records logins and changes, though never the password itself;
* anything that *weakens* a protection asks for the password again, so a guessed password
  is a working off-switch for every control on the page.

Eight characters or more is a real step up and you type it once per device.

### Give it a fixed address

The LAN address is **DHCP**. It is detected at every start from the interface holding the
default route, so a move is picked up on the next restart — but the bookmark on the
parent's phone breaks, and that looks exactly like the panel being down.

**Set a DHCP reservation on the router for `192.168.1.50`.** Alternatively pin it in
`C:\ProgramData\SafeHouse\panel.json`:

```json
{ "port": 8787, "bind": "192.168.1.50", "probes": { "adult": "some-nsfw-domain.example" } }
```

---

### Reading it on a phone

The layout is built at 360px and allowed to grow, because that is where it is actually
read. The page is split into **four tabs** — Início, Regras, DNS, Sistema — switched
client-side with no reload; the active tab is remembered in localStorage and each tab is
deep-linkable (`#inicio`, `#regras`, `#dns`, `#sistema`). Beyond that, three things
carry the layout:

* every grid track is `minmax(min(WIDTH, 100%), 1fr)` — a bare `minmax(280px, 1fr)` does
  not shrink below 280px, so on a narrow phone the cards pushed the page sideways;
* **tables fold into cards under 720px**, each cell carrying its column name in
  `data-label`, so a five-column task table stays readable instead of turning into a
  horizontal scrollbar that hides the column you wanted;
* a slider gets its own full-width row with its value in the card heading, rather than
  three controls fighting over one flex row.

Buttons and fields are 40–42px tall. It follows the phone's light/dark setting.

### Idioma / language

The panel renders in **Brazilian Portuguese by default**, with an **EN/PT toggle** in the
header (also on the login and setup screens). The choice is stored in localStorage per
browser, wrapped in try/catch so private windows still render.

How it works underneath: the server never speaks a language. Everything a parent reads —
lane reasons, notes, fact labels, control result messages, errors — leaves `status.py` /
`controls.py` as a **message key plus params** (`{"k": "ctl.unlock.on", "p": {"n": 30}}`),
and the page renders it from the one catalog in `python/safehouse_panel/i18n.py`, which
carries both languages side by side (a key cannot exist in only one). `panel.log` and the
`self-test` console summary render the same messages back to English, so the audit trail
stays grep-able. Status **codes** (ACTIVE, DEGRADED, BLOCKED…) stay English in the data
and the API — CSS classes and tests key off them — and only their display words are
translated (ATIVO, DEGRADADO, BLOQUEADO…). The one deliberate pass-through is text
quoted from the machine itself: a watchdog step's command output, an unrecognised
HRESULT, a guard config defect — shown verbatim.

---

## The security model, and where it stops

This page can switch off every protection on that machine, and it is reachable from the
same network the child is on. That is the whole risk, stated plainly:

**What protects it**

- **A password**, stored only as a `hashlib.scrypt` hash with a random salt
  (`panel-auth.json`). Never stored, printed or logged in the clear.
- **A session cookie** — `HttpOnly`, `SameSite=Strict`, `Path=/`, 4h idle / 24h absolute.
  Sessions live in memory, so restarting the panel signs everyone out.
- **Lockout with backoff.** Each failed password stalls the response (1s, 2s, 4s… to 8s);
  six failures inside 15 minutes locks that source address out for 15 minutes. Every
  failure is logged **with its source IP** — on this network the person most motivated to
  guess it lives here, and silent brute-forcing is the thing to prevent.
- **Cross-site rejection on every mutation.** `Origin` must match, `Sec-Fetch-Site` must
  not say otherwise, and `Host` must name this panel. This — not the password — is what
  stops a drive-by: a page the child loads can fire a `POST` at this address, and the
  browser attaches the cookie because cookies are attached by destination, not by who
  asked. `SameSite=Strict` is the browser-side half of the same defence; the `Host` check
  is what stops DNS rebinding.
- **Server-side clamping.** Every value is re-validated and clamped here, against the same
  ranges the guards enforce. The slider's `max` is a convenience, not a control.
- **Split privilege.** Actions that *weaken* a protection — more Roblox minutes, a bonus,
  disabling the browser guard, unlocking browsing, allowing YouTube, a longer kill delay —
  ask for the password again. Reading the dashboard, and actions that *tighten* anything,
  need only the session. It costs one predicate and one dialog, and it means a session
  left open on an unlocked phone is not a one-tap way to hand out the afternoon. Making
  parents retype a password to tighten something would just teach them to leave things
  loose.
- **An audit trail.** Every change, refusal and failed login goes to `panel.log` with a
  timestamp, the source address and the before/after values.

**What does not**

- **There is no TLS.** Serving HTTPS needs a certificate, and making one needs tooling
  this vendored Python does not have (there is no openssl on that box). So the login and
  everything after it cross the LAN **in clear**: anyone already able to sniff this network
  can read the password. It is on the house LAN only, the firewall rule is
  `remoteip=LocalSubnet`, and it is not port-forwarded — but this is a real limitation, not
  a footnote.
- **The child has local administrator on that PC.** He can stop the task, edit the JSON,
  or clear the registry policy. Nothing here prevents that. The panel raises the cost,
  makes tampering visible in the logs, and is one more thing that would have to be
  switched off.
- **Obscurity is not one of the defences.** The parent's position is that the child does
  not know the panel exists, which is true today and stops being true the moment he sees a
  bookmark or a tab over a shoulder. The password is what actually stands there.

---

## What the dashboard shows

Everything is read live. Nothing on the page is a constant pretending to be a reading.

Four status words, and **UNKNOWN is not OFF**:

| | |
|---|---|
| `ACTIVE` | enforcing right now |
| `OFF` | deliberately switched off — somebody chose this |
| `DEGRADED` | it should be enforcing and something is wrong, or it is suspended |
| `UNKNOWN` | we could not find out |

A lane whose task could not be queried is UNKNOWN. A lane whose task **does not exist** is
DEGRADED — nothing is enforcing it, which is a fault, not a setting. A lane whose task
exists but is disabled is OFF. Collapsing those three into one word is how a dashboard ends
up reassuring somebody about a protection that has not run in a month.

The page is four tabs. Each piece of information lives in exactly one place (plus the
chips, which are deliberately the only summary):

**Início / Home** — the "is everything OK" screen:

- **Agora** — one chip per lane: Roblox, browsing, the resolver, DNS filtering, YouTube
  and the panel itself, each with its status word and one line of reason. It is the whole
  page in six lines, which is what a phone screen has room for. Under it, in one line:
  UNKNOWN is not OFF.
- **Filtering resolver (WSL)** — see below. The card that would have caught the
  2026-08-01 outage, with its **Iniciar agora / Start it now** button.
- **Known gaps** — shown, not hidden, and labelled as accepted rather than as errors.

**Regras / Rules** — one panel per enforcement lane, each merging that lane's live status
with its own controls (status pill in the heading, reason, facts, the task's state in one
line, then the sliders and switches):

- **Roblox** — budget, used/left today, bonus, closes today; controls: daily minutes,
  extra-today, kill-delay range. A third lane for the Play Games VM is planned; the page
  renders whatever list it is given, so adding it is one more panel.
- **Navegador / Browser** — enforcement, stored budget (marked *not enforced yet*),
  unlock left, notify, closes today; controls: guard on/off, timed unlock, daily minutes
  (stored only), tell-the-child, kill-delay range.
- **YouTube** — the resolver block as **two independent lanes**: the user rules (which
  this panel's mode control drives) and `blocked_services` (which the watch-time budget
  and `toggle youtube` drive). Lifting one does not lift the other, and the panel says
  so. The user-rules lane is driven by **three modes** — always blocked, allowed
  windows, allowed — with a window editor when the schedule mode is chosen (see
  *YouTube: allowed time windows* below).

**DNS** — from AdGuard's own API: reachable, protection, filtering, parental control,
Safe Browsing, safe search per engine, user-rule count, every filter list with rule count
and last-updated (allowlists marked), and whether Pi-hole answers a real DNS query on
5353 — asked **over TCP**, because WSL2 does not forward localhost UDP (see below). Plus **Live
verification**: resolves an ad domain, YouTube, an adult domain and a control that must
resolve, then reads AdGuard's verdict. On demand, not on the 10s refresh.

**Sistema / System**:

- **Scheduled tasks** — every `SafeHouse-*` task, including the ones that are *missing*.
  `SafeHouse-DNS` matters most: when it is not running the relay is not in the path and
  nothing is filtered. (The resolver task also shows on Início; this is the full table.)
- **Browser policy** — per HKLM key: `DnsOverHttpsMode`, `ProxyMode`,
  `BuiltInDnsClientEnabled`, `IncognitoModeAvailability`, `StartupBoostEnabled`,
  `BackgroundModeEnabled`, `URLBlocklist`, the uBO forcelist. Per key, because Opera
  reads the unspaced `OperaSoftware` key and writing the spaced one succeeds and does
  nothing.
- **Acesso ao painel / Panel access** — the change-password form.

Long rationale lives in collapsed *"Por que funciona assim"* details elements; the
always-visible text is one line per control. What never collapsed: unlock-before-reading
on the guarded PC, notify-is-off-on-purpose, the browser budget being stored-not-enforced,
unblocks not surviving `apply-filters.py`, the start button needing somebody signed in,
and UNKNOWN not being OFF.

### The filtering resolver, and why "it answered" proves nothing

Everything this project blocks at the DNS layer lives in two Docker containers inside WSL.
When that VM is not running, **the Windows relay fails open to the router by design**:
names resolve, browsing works, and nothing on the machine looks wrong. That is the
2026-08-01 outage — fourteen hours with no filtering at all and every other signal green.

So the verdict on this card is **one thing only: does `doubleclick.net` come back
sinkholed?** Deliberately not any of the readings that look like they would do:

| Reading | Why it is not the verdict |
|---|---|
| `127.0.0.1:53` answers | The relay owns that port and answers *during* the outage. |
| `github.com` resolves | Same. Resolution working is what failing open means. |
| Both containers listening | They can be up with filtering switched off inside AdGuard. |
| `vmmemWSL` is running | The VM can be up with the containers stopped. |

Those four are all shown, because when the verdict is red they are how you tell *which*
thing broke. They just do not get a vote.

Two Windows-specific traps are baked into how those readings are taken, both found by
measuring rather than assuming:

* **UDP to `127.0.0.1:5353` is not forwarded into WSL.** Only the relay listens on UDP 53;
  Pi-hole answers over WSL2's TCP localhost forwarding but not UDP, so a UDP probe reports
  a perfectly healthy Pi-hole as dead. The resolver card checks Pi-hole and AdGuard with a
  TCP connect, and the DNS tab's Pi-hole probe sends a real **DNS query over TCP**
  (`dnsprobe.ask_tcp`, RFC 1035 length-prefix framing) — a UDP version of that probe
  reported "no answer (timeout)" forever against a healthy container.
* **Windows will not run WSL as LocalSystem at all** — `WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`,
  and it is not a permissions problem: distro registration lives in the user's own registry
  hive, so SYSTEM has no distros. The panel runs as SYSTEM, so it can *read* all of the
  above (sockets and `tasklist`) but cannot start anything.

#### Starting it again

`SafeHouse-Resolver` is the only task in this repo that does **not** run as SYSTEM. It runs
in the logged-on user's session, hidden, under `pythonw.exe` so no console ever appears,
and it:

* fires at logon and every **15 minutes** after;
* sends two UDP packets and **exits without spawning anything** when the chain is
  filtering — which is what makes a 15-minute schedule acceptable on a gaming PC;
* otherwise starts the distro (`wsl.exe -e true`, which boots systemd → dockerd → both
  containers via `restart: unless-stopped`), and only if that was not enough runs
  `safehouse-reconcile-dns`;
* writes what it did to `C:\ProgramData\SafeHouse\resolver.json`, which is how the panel
  learns about a repair it could not perform itself.

The panel's **Start it now** button triggers that task with `schtasks /Run`. It reports
*asked*, not *done* — bringing a cold distro up and reconciling two container stacks takes
tens of seconds, and the page's own 10-second poll is what shows it come back.

> **It only works while somebody is signed in to the PC.** There is no user session to run
> WSL in otherwise. That is also the only time filtering matters, but it does mean pressing
> the button from a phone while the PC sits at the lock screen will do nothing — so the
> card says this whatever its colour, rather than letting you find out at the worst moment.

Run it by hand with:

```powershell
C:\SafeHouse\python\python.exe -m safehouse_setup resolver --status   # report only
C:\SafeHouse\python\python.exe -m safehouse_setup resolver            # repair if needed
C:\SafeHouse\python\python.exe -m safehouse_setup resolver --force    # reconcile regardless
```

### Why the verdict is the query log's `reason`, never the IP

AdGuard Home answers a parental-control block with **`94.140.14.35`** — its own block-page
sinkhole, a perfectly routable address. A check that reads the returned IP therefore
reports a *working block as a leak*. That mistake was made twice on this machine in one
day. The probes send a raw DNS query (so the Windows resolver cache cannot hide it from
AdGuard) and then read `reason` from `/control/querylog`. The addresses are shown as
context and decide nothing.

The **adult** probe ships with no domain: this repo covers NSFW by referencing the HaGeZi
list and does not carry adult domain literals. Put one in `panel.json` under
`probes.adult`. Until you do, that probe reads UNKNOWN — never a silent pass.

---

## The controls

Every control lives on the tab of the thing it controls: the lane controls on **Regras**,
inside their lane's panel; the resolver start button on **Início**; the password form on
**Sistema**.

| Control (tab) | Range | Writes | Weakening? |
|---|---|---|---|
| Roblox daily minutes (Regras) | 0–240 | `roblox-budget.json` → `limit_min` | if raised |
| Roblox extra minutes today (Regras) | 0–240 | `roblox-budget.state.json` → `bonus_sec` | if > 0 |
| Browser guard on/off (Regras) | — | `browser-guard.json` → `enabled` | if off |
| Unlock browsing for N min (Regras) | 0–240 | `browser-guard.state.json` → `unlock_until_ts` | if > 0 |
| Browser daily minutes (Regras) | 0–240 | `browser-guard.json` → `limit_min` | if raised |
| Tell the child when a browser closes (Regras) | — | `browser-guard.json` → `notify` | no |
| Kill delay, per lane (Regras) | 1–60s | `delay_min_sec` / `delay_max_sec` | if raised |
| YouTube mode + windows (Regras) | 3 modes, ≤ 8 windows | `youtube-schedule.json` + AdGuard user rules | if it opens any minute |
| YouTube allow/block now (Regras) | until the next boundary | `youtube-schedule.json` → `override` + AdGuard user rules | if allowed now |
| Start the filtering resolver (Início) | — | triggers `SafeHouse-Resolver` | no — it only restores filtering |
| Change the panel password (Sistema) | ≥4 chars | `panel-auth.json` (hash only) | needs the **current** password |

Every write is atomic (`atomicio.write_json`, the same rename-based write the guards use)
and read-modify-write, so a hand-edited `message` or `users` survives a slider being moved.

Three things worth knowing before you touch them:

- **The Roblox bonus lives in the state file, not the config.** It expires at the date
  rollover instead of quietly becoming the new limit.
- **The browser daily budget is stored, not enforced.** That lane has no metering today —
  it closes every browser it finds. The panel writes the number so the metering work has
  the parent's value to honour, and the page says so rather than showing a slider that
  silently does nothing. With no `limit_min` in the file the lane behaves exactly as it
  does today.
- **`notify` is off at the parent's instruction** — the child sees the window close but is
  not told what closed it. The control is labelled "tell the child when a browser is
  closed" so nobody flips it by accident.
- **YouTube is released as of 2026-08-26, and nothing re-applies the block.** The repo's
  standing rules were removed from `adguard/user-rules.txt` (a comment-only note sits in
  their place) and the Pi-hole regexes are commented out in
  `blocklists/parental-denylist.txt`, so neither `apply-filters.py` nor the weekly
  `auto-update.sh` puts YouTube back. The panel's mode is `allowed`, which the reconciler
  never enforces against. A block set here — by hand or with a schedule — therefore holds
  until a parent changes it. To restore a *standing* block, both repo files have to change
  back as well; the panel alone survives a reconcile now, but not a rebuild from the repo.

### YouTube: allowed time windows

The parent's standing policy is "blocked at all times, except the windows I define". The
YouTube panel on Regras offers **three modes**, stored in
`C:\ProgramData\SafeHouse\youtube-schedule.json` (written atomically, hand-editable,
hand-added keys preserved):

```json
{ "mode": "schedule",
  "windows": [ { "days": [5, 6] },
               { "days": [3], "start": "14:00", "end": "15:00" } ],
  "override": { "state": "ALLOWED", "until_ts": 1787502000 } }
```

| Mode | Meaning |
|---|---|
| `blocked` | always blocked — what the old checkbox meant. **Manual: absolute.** |
| `allowed` | the panel's rules are lifted entirely. **Manual: absolute.** |
| `schedule` | blocked except inside a window — the only mode the reconciler enforces |

**Manual comes first.** The two manual modes are applied synchronously, in the request
that set them, and the reconciler **never** overrides or "corrects" them — a parent who
lifts the block by hand in the AdGuard UI while mode is `blocked` is not fought by a
robot. If AdGuard refused a manual change, *nothing* is saved and the answer says
nothing changed — press Apply again; no silent retry of a manual act.

Facts that keep the schedule honest:

- **Days are the primary unit.** A window with days and no times allows the **whole
  day** — `{"days": [5, 6]}` is "weekends are open", and the editor seeds exactly that
  when schedule mode is first chosen, so the canonical case is a couple of taps. Times,
  when given, must come as a pair and refine the window.
- **Days use Python's numbering: 0 = Monday … 6 = Sunday** (`datetime.weekday()`), on
  both ends, so no translation layer can get them backwards. Times are the machine's
  local clock (America/Sao_Paulo here).
- **A window whose end is at or before its start crosses midnight**: `{"days": [4],
  "start": "22:00", "end": "01:00"}` runs Friday 22:00 → Saturday 01:00.
- **Validation is server-side like every control**: three known modes, at most 8
  windows, `HH:MM` times (both or neither), at least one valid day, start ≠ end.
  Malformed input is refused, never guessed.
- **`override` is a manual hold on top of a running schedule** — the *Liberar agora* /
  *Bloquear agora* buttons. It is applied on the spot, and expires at the **next
  schedule boundary** (window edge or day change, whichever first — computed
  server-side, never accepted from the page) or when the parent presses *Voltar ao
  horário*. While it is active the panel says so plainly ("Liberado manualmente —
  volta ao horário às 15:00") and the reconciler stands down exactly as for a manual
  mode; the tick after expiry puts the schedule back in charge. Overriding to ALLOWED
  asks for the password; to BLOCKED does not. Saving any mode clears the hold.
- **Enforcement is a reconciler thread inside the panel process** (the one SafeHouse
  process that runs 24/7, as SYSTEM under `SafeHouse-Panel`). Once a minute it re-reads
  the file **from disk** — never a cached copy, so a manual change between ticks is
  respected immediately — and, only while mode is `schedule` with no active override
  and the desired state differs from AdGuard's live user-rules state, applies it
  through the same rule filter the checkbox used, with the same guarantees: `@@` allow
  rules are never touched, hand-typed rules survive, the `blocked_services` lane (owned
  by the watch-time budget and `toggle youtube`) is never driven from here. Transitions
  are logged to `panel.log`
  (`[schedule] youtube: BLOCKED -> ALLOWED (window sat,sun all day)`); no-op ticks are
  never logged; an unreachable or refusing AdGuard is logged once per outage and
  retried every minute.
- **Saving a schedule also applies it immediately** — and if AdGuard refused or did not
  answer, the *schedule* is still saved (the reconciler exists to retry it) and the
  message says so. Only manual modes roll back on failure.
- **Failure directions**: no file → no schedule, nothing enforced (the repo's standing
  rules own YouTube, as before this feature). A file that exists but does not parse →
  reported as a defect, shown as always-blocked for the page and the password baseline,
  and the reconciler enforces **nothing** it could not read.
- **Weakening for the password re-ask is one rule**: does the new schedule allow YouTube
  at any minute the old one blocked? Switching to `allowed`, adding a window, adding a
  day, widening hours — all ask for the password. Blocking, removing and narrowing do
  not. With no schedule stored yet, the baseline is the live rules state, and an
  unreadable live state counts as blocked, so the comparison fails towards asking.
- The Início chip and the Regras panel show the effective state plus **"por janela"**
  context and the next change ("Libera sáb 14:00") when the schedule mode is active.
- **The engine is lane-generic on purpose** (`safehouse_panel/schedule.py` is pure and
  never mentions YouTube outside its docstrings): a browser or Roblox schedule later is
  a second config file plus a consumer that applies its lane's own control — the window
  arithmetic, override and weakening rules are already shared.

The removal filter only ever drops SafeHouse's own YouTube lines, matched exactly. It never
touches an `@@` allow rule — `@@||firebaseinstallations.googleapis.com^` is app identity,
and blocking it makes the games fail to launch with nothing connecting that to a YouTube
toggle — and it never touches the Roblox rules that happen to live in the same list.

---

## Verifying each reading independently

The repo's habit: every control states how to check it without trusting the thing being
checked.

| Reading on the page | Check it with |
|---|---|
| Lane task state / last result | `schtasks /Query /TN SafeHouse-RobloxBudget /V /FO LIST` |
| Roblox minutes used today | `type C:\ProgramData\SafeHouse\roblox-budget.state.json` |
| Roblox budget | `type C:\ProgramData\SafeHouse\roblox-budget.json` |
| Browser unlock left | `python -m safehouse_ytbudget.browser_guard --status` |
| Closes today | `Get-Content C:\ProgramData\SafeHouse\browser-guard.log -Tail 20` |
| AdGuard protection / lists | the AdGuard dashboard, `http://127.0.0.1:3000` |
| Filter list rule counts | `curl -u admin:… http://127.0.0.1:3000/control/filtering/status` |
| A probe verdict | AdGuard dashboard → Query log → the domain → the *reason* column |
| Pi-hole on 5353 | in WSL: `dig +short example.com @127.0.0.1 -p 5353`; from Windows add `+tcp` — plain UDP is not forwarded and times out |
| The YouTube schedule | `type C:\ProgramData\SafeHouse\youtube-schedule.json`, and the `[schedule]` lines in `panel.log` |
| Browser policy | `edge://policy` / `opera://policy`, or `reg query "HKLM\SOFTWARE\Policies\OperaSoftware\Opera"` |
| Everything at once | `python -m safehouse_panel self-test` (prints the whole snapshot as JSON, binds nothing) |
| That the panel is up | `Get-Content C:\ProgramData\SafeHouse\panel.log -Tail 5` — `[start]` and hourly `[alive]` lines |
| A change you made | the same log: `[change] <control>: before -> after (by <address>)` |

---

## Known gaps, shown on the page

- **Plain DNS to `8.8.8.8` bypasses everything.** There is no firewall rule forcing port 53
  through the filter. Check: `nslookup doubleclick.net 8.8.8.8` returns a real address,
  while `nslookup doubleclick.net 127.0.0.1` returns `0.0.0.0`.
- **The relay fails open to the router when WSL is down** — resolution keeps working with
  nothing filtered behind it, which is why an outage looks like a healthy machine. This was
  the 2026-08-01 failure: fourteen hours unfiltered with every other signal green.
- **The child has local admin.**
- **The Play Games VM has no kill lane** — only the resolver reaches it.
- **This panel has no TLS.**

A dashboard that only ever shows green is the failure mode this repo exists to fight, so
these are on the page next to the things that are working, labelled as accepted rather than
as errors.

---

## Troubleshooting

**It does not answer from the phone.** Check `panel.log` on the machine:

- `[start] control panel listening on http://127.0.0.1:8787, http://192.168.1.50:8787` —
  it is up on both. The problem is the network or the firewall rule:
  `netsh advfirewall firewall show rule name=SafeHouse-Panel`.
- `[error] the panel is NOT reachable from the house network…` — the LAN address moved
  under DHCP. It is still serving on loopback. Restart the task
  (`schtasks /Run /TN SafeHouse-Panel`) to pick up the new address, and set the DHCP
  reservation.
- Nothing recent at all — the task is not running: `schtasks /Query /TN SafeHouse-Panel /V /FO LIST`.

**It answers but only shows instructions.** No password has been set. Run
`set-password --generate`, elevated, on that machine.

**"Too many failed attempts."** Six wrong passwords from one address inside 15 minutes.
Wait it out, or restart the task to clear the throttle. Check `panel.log` first — repeated
`[auth] FAILED login from …` from an address that is not yours is worth knowing about.

**Everything says UNKNOWN.** The panel could not query Task Scheduler or reach AdGuard.
UNKNOWN is deliberately not OFF: it means the reading failed, not that the protection did.

**A change reported success but nothing happened.** Compare `panel.log`'s before/after
against the file itself. The guards read their config on every run (once a minute), so a
change applies within a minute without restarting anything.

**The resolver card is red and "Start it now" does nothing.** In order:

1. **Is anyone signed in to the PC?** Nothing can start WSL otherwise — Windows will not
   run it as LocalSystem. This is the common answer.
2. Is the task there? `schtasks /Query /TN SafeHouse-Resolver /V /FO LIST`. If not:
   `install.py --resolver-only`, run while signed in as the account WSL belongs to.
3. What did it last do? `type C:\ProgramData\SafeHouse\resolver.json` — it records the
   steps it ran and their output.
4. Ask it directly, elevated on that machine:
   `C:\SafeHouse\python\python.exe -m safehouse_setup resolver --force`
5. Still nothing: in WSL, `docker ps` (both containers Up) and
   `~/.local/bin/safehouse-reconcile-dns`.

**The resolver card is red but browsing works fine.** That is the whole point of the card
— it is the shape the 2026-08-01 outage had. The relay falls back to the router, so DNS
answers and nothing is filtered. Do not read "the internet works" as "this is a false
alarm"; check `doubleclick.net` yourself:

```powershell
nslookup doubleclick.net 127.0.0.1   # must come back 0.0.0.0 / ::
```

---

## Files

| Path | What |
|---|---|
| `python/safehouse_panel/status.py` | pure: readings in, dashboard out (as message keys + params) |
| `python/safehouse_panel/i18n.py` | the ONE translation catalog (en + pt-BR), `msg()` and `render()` |
| `python/safehouse_panel/collect.py` | the only impure module: PowerShell, registry, sockets, AdGuard |
| `python/safehouse_panel/controls.py` | clamping, atomic writes, the YouTube rule filter |
| `python/safehouse_panel/schedule.py` | pure: the YouTube schedule - validation, window arithmetic, the weakening rule |
| `python/safehouse_panel/app.py` | routing, sessions, the cross-site rules — no sockets |
| `python/safehouse_panel/auth.py` | password hashing, sessions, lockout |
| `python/safehouse_panel/dnsprobe.py` | raw DNS queries and the query-log verdict |
| `python/safehouse_panel/page.py` | the page: inline CSS and JS, no external requests |
| `python/safehouse_panel/server.py` | binding, the task entry point, the CLI |
| `python/safehouse_setup/resolver.py` | the filtering probe and the WSL repair (runs as the user, not SYSTEM) |
| `C:\ProgramData\SafeHouse\panel-auth.json` | the password hash and salt |
| `C:\ProgramData\SafeHouse\panel-password.txt` | only after `--generate`; **delete it** |
| `C:\ProgramData\SafeHouse\panel.json` | optional: `port`, `bind`, `probes` |
| `C:\ProgramData\SafeHouse\youtube-schedule.json` | the YouTube mode and its allowed windows |
| `C:\ProgramData\SafeHouse\panel.log` | start, hourly heartbeat, every change, every refusal |
| `C:\ProgramData\SafeHouse\resolver.json` | what the watchdog last did, and what it found |
