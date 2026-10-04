# Play Games cleanup (assisted, one game at a time)

A parent's disk-space tool. It walks the Android games installed inside **Google Play
Games for PC** and uninstalls them **one by one**, asking `y / s / q` for each, driving
the Play Games window over UI Automation where it can and telling you exactly what to
click where it cannot.

It is **not** part of the enforcement stack. It reads the Play Games guard's state and
never changes it.

## Read this first: the faster way

Play Games is banned on this machine. If no game in it is worth keeping, the
one-game-at-a-time route is the slow way to free the space. The fast way:

1. Unlock the guard (below) - the guard would otherwise also kill the Play Games
   **uninstaller**, which lives in the same directory it guards.
2. *Settings -> Apps -> Installed apps -> Google Play Games -> Uninstall*
   (or run `C:\Program Files\Google\Play Games\Uninstaller.exe`).
3. Delete `%LOCALAPPDATA%\Google\Play Games\` (the folder that holds `userdata_*\avd`).
4. Re-lock the guard. It keeps running afterwards and has nothing to do.

That returns **all** the space at once and certainly. This tool exists for the case where
some games should stay.

## Why the space may not come back per game

The Android VM's disk is one file, `%LOCALAPPDATA%\Google\Play Games\userdata_*\avd\userdata.img`.
On this machine it is an **80 GB sparse file with about 62 GB actually allocated**
(measured 2026-10-04). Uninstalling a game frees blocks **inside** that image. The file on
Windows only shrinks if the VM passes those frees down (discard/TRIM) - which may happen
later, or never.

So the tool measures the **allocated** size (not Explorer's "Size", which stays 80 GB
forever) and says, per game, either `avd shrank` or
`freed inside the VM, avd file size unchanged`. The second means: Play Games can reuse that
space for other games, Windows does not get it back. The only certain way to return it is
the faster way above.

## The guard: unlock before, lock after

`safehouse_ytbudget.playgames_guard` runs every minute and ends anything under
`C:\Program Files\Google\Play Games\`. This tool **does not** unlock it. On start it reads
`C:\ProgramData\SafeHouse\playgames-guard.json` and `playgames-guard.state.json`
(read-only, failing closed like the guard does), and if the guard is enforcing it prints
the command and exits with code **2** before launching anything.

From an **elevated** PowerShell (right-click Start -> *Terminal (Admin)*):

```powershell
# before: 90 minutes is plenty for a dozen games
cd C:\SafeHouse\windows\youtube-budget; & 'C:\SafeHouse\python\python.exe' -m safehouse_ytbudget.playgames_guard --unlock 90

# after (the tool prints this on every exit - normal, Ctrl+C or error)
cd C:\SafeHouse\windows\youtube-budget; & 'C:\SafeHouse\python\python.exe' -m safehouse_ytbudget.playgames_guard --lock

# check either way
cd C:\SafeHouse\windows\youtube-budget; & 'C:\SafeHouse\python\python.exe' -m safehouse_ytbudget.playgames_guard --status
```

The unlock expires by itself. The tool re-reads the guard before **every** game and stops
cleanly if it has re-locked, instead of starting an uninstall that the guard will kill
half-way.

## How to run

From a **normal (not elevated)** PowerShell, as the user whose Play Games it is:

```powershell
cd C:\SafeHouse\windows\playgames-cleanup      # staged by Install-SafeHouse.ps1
powershell -ExecutionPolicy Bypass -File .\Run-PlayGamesCleanup.ps1 --list-only   # look first
powershell -ExecutionPolicy Bypass -File .\Run-PlayGamesCleanup.ps1 --dry-run     # prompts, clicks nothing destructive
powershell -ExecutionPolicy Bypass -File .\Run-PlayGamesCleanup.ps1               # the real run
```

`Run-PlayGamesCleanup.ps1` finds a real Python 3.11+ (the Microsoft Store stub and the
vendored `C:\SafeHouse\python` runtime do not count). If there is none it offers
`winget install Python.Python.3.13` (per-user) behind a `y/N` prompt. It then builds
`.venv\` next to itself, installs `requirements.txt` (re-installs only when that file
changes) and runs `python -m playgames_cleanup` with your arguments.

| Option | Effect |
|---|---|
| `--list-only` | print the games found and the avd size, exit |
| `--dry-run` | everything except the uninstall clicks; each game is `NOT CHECKED` |
| `--yes-per-step` | also ask before each automated UI step (off by default) |
| `--unlock-minutes N` | the minutes put in the printed unlock command (default 90) |
| `--settle-timeout S` | how long to wait for a game to leave the library (default 180 s) |

Exit codes: `0` done · `1` error (Play Games would not open, not Windows) · `2` guard
locked, nothing launched · `130` Ctrl+C.

## What a run does

```
guard check (read-only) --locked--> print unlock command, exit 2
      | unlocked
measure avd (allocated) + C: free
      |
launch Bootstrapper.exe / attach to the window --> Library -> Installed
      |
game list:  UIA library  >  Windows uninstall registry  >  you type names
      |
for each game:  Uninstall <name>? [y]es/[s]kip/[q]uit     (Enter alone is not a yes)
      |  y
      +-- open the game's menu     (UIA; hover/right-click the located tile)
      +-- Uninstall                (UIA Invoke only)
      +-- confirm                  (UIA Invoke only, and only if exactly ONE button matches)
      |      any step not found -> prints what to click, waits for Enter
      +-- verify: gone from library / registry, avd shrank -> PASS or NOT VERIFIED
      |
final table: game, action, verdict, avd freed; totals; --lock reminder
```

- **Nothing destructive happens without a `y` for that game.**
- Destructive controls are only ever **invoked through UIA**, never clicked by
  coordinates. If more than one control could be the one meant, the tool refuses to
  pick and asks you to click it.
- `pyautogui` only hovers a tile UIA already located (some tiles show their `⋮` button on
  hover) and presses Escape to close a menu when you abort a game.
- **PASS needs evidence**: the game left the library or the uninstall registry, or the
  avd shrank. The **last** game is a special case: an empty library looks the same as an
  unreadable one, so it only PASSes on the registry or the avd.
- Plain console text only - no colours, no animation, no flashing.
- Log: `%LOCALAPPDATA%\SafeHouse\playgames-cleanup\logs\cleanup-<timestamp>.log`
  (every prompt and answer, every lookup that missed).

## First run: what to check (never verified against the live app)

Nothing here has been run against the real Play Games window. The UI strings are in one
file, `playgames_cleanup/ui_strings.py` (English + pt-BR); on the first run, with
`--dry-run` and then the real run, check:

| Assumption | If wrong |
|---|---|
| The main window title is `Google Play Games` (or `... beta`) | "Could not find the Play Games window" - add the real title to `MAIN_WINDOW_TITLES` |
| Chromium exposes the library to UIA at all | game list empty -> registry or typed names; every step becomes manual (still works, just guided) |
| The nav item is `Library` / `Biblioteca`, filter `Installed` / `Instalados` | whole library listed, or manual mode |
| Each game tile's accessible name is the game title | names look odd in the list -> `[s]kip` the junk; tune `CHROME_NAMES` |
| A per-tile `More options` / `Mais opções` button, or right-click, opens a menu | menu step becomes manual |
| The menu item is `Uninstall` / `Desinstalar` | uninstall step becomes manual |
| A confirm dialog with an `Uninstall` / `Desinstalar` button follows | confirm step becomes manual; if there is no dialog, just press Enter |
| Each game has an entry in Windows *Installed apps* pointing into `...\Google\Play Games\` | registry list empty; only the UIA signal verifies |

`--yes-per-step` is the careful way to watch each automated step happen the first time.

## If Play Games will not start

The `GooglePlayGamesServices` service was stopped and **disabled** on 2026-08-22 (see the
guard's docstring). If the client depends on it, it will not open even while the guard is
unlocked. Check with `sc qc GooglePlayGamesServices`. Re-enabling it for the session
(`sc config GooglePlayGamesServices start= demand`, elevated) and disabling it again
afterwards is a parent's decision - this tool does not touch services.

## Files

| Path | What |
|---|---|
| `Run-PlayGamesCleanup.ps1` | bootstrap: Python, `.venv`, requirements, run |
| `requirements.txt` | `pywinauto`, `pyautogui` (pinned) - this tool only; `python/` stays stdlib-only |
| `playgames_cleanup/guard.py` | read-only guard verdict + the exact unlock/lock commands |
| `playgames_cleanup/disk.py` | allocated avd size, C: free space, GB formatting |
| `playgames_cleanup/ui_strings.py` | every UI name the automation looks for (EN + pt-BR) |
| `playgames_cleanup/app.py` | pywinauto UIA driver; pure name/window/registry filters at the top |
| `playgames_cleanup/steps.py` | the three UI steps and what to tell you when one misses |
| `playgames_cleanup/session.py` | the per-game loop, verification, final report |
| `tests/` | pytest for the pure parts, runs on Linux |

## Tests

From the repo root, in WSL:

```bash
.venv-dev/bin/python -m pytest windows/playgames-cleanup/tests -q
```

They cover the guard verdict (and that it fails closed and writes nothing), size
measurement and formatting, window/name/registry filtering, and the loop's decisions
against a fake app: no step without a `y`, dry run touches nothing, misses become
instructions, guard re-lock stops the loop, PASS needs evidence. They do **not** cover
pywinauto against the real window - that only happens on the first live run.
