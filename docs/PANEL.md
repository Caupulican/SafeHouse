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

# Set the password (see below). Elevated, on that machine.
C:\SafeHouse\python\python.exe -m safehouse_panel set-password --generate
```

A full `install.py` run registers it too; `--no-panel` skips it, and `--panel-port N`
moves it.

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

`set-password --generate` invents one and writes it to
**`C:\ProgramData\SafeHouse\panel-password.txt`**, which inherits the directory's ACL
(SYSTEM + Administrators). It is written to a file rather than printed because nothing may
be displayed on that machine and the task has no console to print to.

> Copy the password into your password manager and **delete `panel-password.txt`.** The
> panel keeps only a scrypt hash; that file is the only copy of the password itself.

Re-running `set-password` replaces it. Sessions already signed in stay signed in until the
panel restarts.

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

1. **Enforcement lanes** — Roblox and web browsing: task state and last result, the budget,
   minutes used today, any bonus or unlock in force, how many closes happened today, and
   when the lane last acted. A third lane for the Play Games VM is planned; the page renders
   whatever list it is given, so adding it is one entry.
2. **Scheduled tasks** — every `SafeHouse-*` task, including the ones that are *missing*.
   `SafeHouse-DNS` matters most: when it is not running the relay is not in the path and
   nothing is filtered.
3. **DNS filtering** — from AdGuard's own API: reachable, protection, filtering, every
   filter list with rule count and last-updated, the allowlists, user-rule count, parental
   control, Safe Browsing, safe search per engine, and whether Pi-hole answers on 5353.
   YouTube is shown as **two independent lanes** — the user rules (which this panel drives)
   and `blocked_services` (which the watch-time budget and `toggle youtube` drive).
4. **Live verification** — resolves an ad domain, YouTube, an adult domain and a control
   that must resolve, then reads AdGuard's verdict. On demand, not on the 10s refresh.
5. **Browser policy** — per HKLM key: `DnsOverHttpsMode`, `ProxyMode`,
   `BuiltInDnsClientEnabled`, `IncognitoModeAvailability`, `StartupBoostEnabled`,
   `BackgroundModeEnabled`, `URLBlocklist`, the uBO forcelist. Per key, because Opera reads
   the unspaced `OperaSoftware` key and writing the spaced one succeeds and does nothing.
6. **Known gaps** — shown, not hidden, and labelled as accepted rather than as errors.

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

| Control | Range | Writes | Weakening? |
|---|---|---|---|
| Roblox daily minutes | 0–240 | `roblox-budget.json` → `limit_min` | if raised |
| Roblox extra minutes today | 0–240 | `roblox-budget.state.json` → `bonus_sec` | if > 0 |
| Browser guard on/off | — | `browser-guard.json` → `enabled` | if off |
| Unlock browsing for N min | 0–240 | `browser-guard.state.json` → `unlock_until_ts` | if > 0 |
| Browser daily minutes | 0–240 | `browser-guard.json` → `limit_min` | if raised |
| Tell the child when a browser closes | — | `browser-guard.json` → `notify` | no |
| Kill delay, per lane | 1–60s | `delay_min_sec` / `delay_max_sec` | if raised |
| YouTube at the resolver | — | AdGuard user rules | if allowed |

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
- **Unblocking YouTube here does not survive `adguard/apply-filters.py`.** That reconciler
  re-adds every rule in `adguard/user-rules.txt`, so an unblock that must last needs the
  repo changed too. The panel says this in the result message.

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
| Pi-hole on 5353 | in WSL: `dig +short example.com @127.0.0.1 -p 5353` |
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

---

## Files

| Path | What |
|---|---|
| `python/safehouse_panel/status.py` | pure: readings in, dashboard out |
| `python/safehouse_panel/collect.py` | the only impure module: PowerShell, registry, sockets, AdGuard |
| `python/safehouse_panel/controls.py` | clamping, atomic writes, the YouTube rule filter |
| `python/safehouse_panel/app.py` | routing, sessions, the cross-site rules — no sockets |
| `python/safehouse_panel/auth.py` | password hashing, sessions, lockout |
| `python/safehouse_panel/dnsprobe.py` | raw DNS queries and the query-log verdict |
| `python/safehouse_panel/page.py` | the page: inline CSS and JS, no external requests |
| `python/safehouse_panel/server.py` | binding, the task entry point, the CLI |
| `C:\ProgramData\SafeHouse\panel-auth.json` | the password hash and salt |
| `C:\ProgramData\SafeHouse\panel-password.txt` | only after `--generate`; **delete it** |
| `C:\ProgramData\SafeHouse\panel.json` | optional: `port`, `bind`, `probes` |
| `C:\ProgramData\SafeHouse\panel.log` | start, hourly heartbeat, every change, every refusal |
