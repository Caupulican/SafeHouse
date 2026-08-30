"""WHICH Roblox experiences may run - the per-game lane.

The budget lane next door answers "for how long"; this one answers "which". They are
separate tasks with separate config, because they fail differently and a parent must be
able to stand one down without touching the other.

Why it cannot be done the obvious way: every experience on the platform runs as the same
`RobloxPlayerBeta.exe`. The image name, the command line and the window title are
identical for a tycoon and for a horror game, so the process table - which is all the
other kill lanes ever need - cannot tell them apart. What CAN is the client's own log.

Verified against Roblox 0.736.0 on this machine (2026-08-30), a join writes two lines:

    [FLog::Output] ! Joining game '<jobId>' place 88083000467860 at 10.34.1.86
    [FLog::GameJoinLoadTime] Report game_join_loadtime: placeid:88083000467860,
        join_time:0.669, universeid:9681996188, referral_page:, sid:..., userid:...

...and leaving writes `[FLog::SingleSurfaceApp] leaveUGCGameInternal`. The SECOND join
line is the one that matters, because it carries the **universeid**, and matching on the
universe rather than the place is not a refinement - it is the difference between a rule
that works and one that does not. A "game" is a universe; a place is one map inside it,
and a teleport between maps keeps the same process and writes a fresh join line with a
DIFFERENT place. Two of the fourteen games in this machine's own history do exactly that:

    universe 4807308814  "Story Game A"  places 13864661000, 13864667823
    universe 2955322961  "Obby Game B"         places 7606564092, 88604154038333

A place-keyed allowlist would have let the child into the lobby and killed him in the
level. Universe-keyed, both are one entry. The universeid comes straight out of the log,
so the enforcing path needs no network call and no API key; `--history` talks to Roblox's
public games API purely to put NAMES on the ids for a human, and works without it.

**Resident, not a one-shot** - and this lane is the second exception to the parent's
"every guard is a one-shot" rule, for a reason the volume ceiling shares: a task can
repeat no more often than once a minute, and a minute is not a latency here, it is a
loophole. A banned game that gets sixty seconds before the kill can simply be re-joined
for another sixty, forever. Polling the log every few seconds makes the ban a ban.

The idle path stays cheap in the same spirit as the one-shots: a tick reads ONE directory
listing and, when nothing has been appended since the last tick, stops there - no
subprocess, no state write, no log line. `tasklist` is spawned only on a tick that has
already decided to kill.

Fail-closed, with one deliberate exception:

* an **unlisted** universe in `allow` mode is killed - that is the whole point;
* an **unknown** universe (a join line whose universeid could not be read, which a client
  update could cause) is killed in `allow` mode and spared in `deny` mode: in both
  directions the unknown case resolves to "enforce the rule", never to "wave it through";
* a **missing** config leaves the lane asleep. This lane is opt-in and a list only the
  parent can write; installing it must not brick Roblox on a machine nobody configured;
* a **broken** config falls back to the LAST GOOD one this process parsed, and to doing
  nothing if there has never been one. This is the exception, and it is chosen over the
  budget lane's strict fallback because the strict fallback here would be "no game at
  all" - a policy nobody wrote down, enforced in a loop, because of a stray comma in
  Notepad. It shouts in the log instead, every episode, and the panel shows it as a fault.

Honest limits, so nobody oversells this:

* enforcement is a **kill of the whole client**, not of one experience: the child is
  dropped to the desktop, not back to the Roblox home page. Re-opening and joining an
  allowed game is immediate and unimpeded, which is the intended shape of the rule;
* there is a window of a few seconds between the join and the kill. He will see the
  loading screen, sometimes the spawn point. It cannot be closed further without hooking
  the client, which this repo will not do;
* the log is the child's own file. He has local admin by his parent's choice and can make
  it unreadable. This lane treats a Roblox that is running while its log has gone silent
  as exactly that - `[warn] blind`, in the log, once per episode - so tampering shows up
  as a fault rather than as a quiet all-clear;
* the Microsoft Store build writes its logs somewhere else. That path is included on a
  best-effort basis and is UNVERIFIED - no Store install exists on this machine to test
  it against.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import killdelay
from .atomicio import write_json
from .browser_guard import notify
from .roblox_guard import kill_processes, log, running_processes

DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")
PROFILES_DIR = Path(r"C:\Users")

# Fast enough that "you cannot farm a minute of it per relaunch" is true, slow enough
# that the tick is a directory listing and nothing else on an idle machine.
POLL_SEC = 3.0
CONFIG_EVERY_SEC = 5.0
# A log this old is not being written to; whatever it last said about a game is history.
STALE_SEC = 180
# Roblox is up but its newest log has said nothing for this long: either the client
# changed its logging or someone made the file unreadable. Said once per episode.
BLIND_AFTER_SEC = 120
# One respawn-storm line per this window, rather than one per kill.
STORM_WINDOW_SEC = 300
# How many games the ledger remembers. Two days of ordinary play on this machine was
# fourteen; two hundred is a year of them, and bounds a file the panel reads on every
# refresh. The oldest LAST-seen entries are what fall off.
SEEN_MAX = 200

MODE_ALLOW = "allow"
MODE_DENY = "deny"
MODES = (MODE_ALLOW, MODE_DENY)

# Roblox's own per-experience content maturity, weakest first. It is NOT what enforces
# anything here - an allow list already ends everything nobody ticked - and it is
# deliberately never consulted on the kill path, which must keep working with the
# internet down. Its job is to let a parent curate with his eyes open, and to shout when
# a game already on the list is rated above the ceiling he set.
#
# Two things learned measuring it against this machine's own history on 2026-08-30, both
# worth keeping in front of whoever reads this next:
#
#   * SEVEN of the fifteen games played were rated `mild`, above the account's documented
#     "Minimal" setting, and they launched anyway. The account-side filter was not
#     enforcing. A label is evidence, not a control.
#   * "Horror Game F" - the horror one - is rated `minimal`. So a
#     maturity ceiling would not have caught the single game the parent named first.
#
# Hence: a floor worth showing, never a filter worth trusting.
MATURITY_ORDER = ("minimal", "mild", "moderate", "restricted")
DEFAULT_MAX_MATURITY = "minimal"

# `universeid` is only on the loadtime line; `! Joining game` is the fallback that at
# least yields the place, and lands on the strict side of `verdict` for want of more.
JOIN_FULL_RE = re.compile(r"game_join_loadtime:\s*placeid:(\d+),.*?universeid:(\d+)")
JOIN_PLACE_RE = re.compile(r"! Joining game '[^']*' place (\d+) at")
LEAVE_RE = re.compile(r"leaveUGCGameInternal")
STAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")


# --- what the log says -------------------------------------------------------

@dataclass(frozen=True)
class Presence:
    """The experience the client is in right now, as far as the log can tell."""
    place: int
    universe: int | None
    at: str = ""  # the log's own UTC stamp, for --history; empty when tailing live


def parse_line(line: str) -> tuple[str, Presence | None]:
    """Pure: classify one log line as a join, a leave, or nothing.

    The order matters. A join emits BOTH forms, the fuller one second, and taking the
    richer line when it exists is what keeps `universe` populated on the happy path.
    """
    m = JOIN_FULL_RE.search(line)
    if m:
        return "join", Presence(int(m.group(1)), int(m.group(2)), stamp(line))
    m = JOIN_PLACE_RE.search(line)
    if m:
        return "join", Presence(int(m.group(1)), None, stamp(line))
    if LEAVE_RE.search(line):
        return "leave", None
    return "", None


def stamp(line: str) -> str:
    m = STAMP_RE.match(line)
    return m.group(1) if m else ""


def upgrades(current: Presence | None, found: Presence) -> bool:
    """Is this join line a second view of the join already open, or a new one?

    One join writes TWO lines - `! Joining game ... place N` first, the richer
    `game_join_loadtime` line with the universeid a beat later - and reading them as two
    events double-counts every session and, worse, leaves the presence flipping between
    "universe known" and "universe unknown" for the same game. Same place with nothing
    closed in between means one join, seen twice.
    """
    return current is not None and current.place == found.place


def merge(current: Presence, found: Presence) -> Presence:
    """The two views of one join, folded into the richer one. Keeps the EARLIER stamp."""
    return Presence(current.place, current.universe if current.universe is not None
                    else found.universe, current.at or found.at)


def fold_presence(lines, current: Presence | None
                  ) -> tuple[Presence | None, list[Presence]]:
    """Pure: fold log lines onto (the presence they leave behind, the ones they ended).

    The second half of that tuple is not a convenience. A tick reads everything appended
    since the last one, and three seconds of log can easily hold a leave and a re-join -
    or two joins back to back. Returning only the final presence throws the games in
    between away, and they are games that were played: the ledger would silently skip
    them, and on a first sighting of a whole file it would skip nearly all of them.

    Two joins for different places in a row (a teleport inside one universe, or a hop
    straight from one game to another) end the first and start the second; there is no
    leave between them.
    """
    ended: list[Presence] = []
    for line in lines:
        kind, found = parse_line(line)
        if kind == "leave":
            if current is not None:
                ended.append(current)
            current = None
        elif kind == "join":
            if upgrades(current, found):
                current = merge(current, found)
            else:
                if current is not None:
                    ended.append(current)
                current = found
    return current, ended


def reduce_presence(lines, current: Presence | None) -> Presence | None:
    """`fold_presence` for callers that only want where the client ended up."""
    return fold_presence(lines, current)[0]


# --- the log files -----------------------------------------------------------

def log_dirs(profiles: Path = PROFILES_DIR) -> list[Path]:
    """Every Roblox client log directory on the machine, across all profiles.

    All profiles, not just the child's, because this lane runs as SYSTEM and a second
    local account would otherwise be a one-click bypass. The Store path is included
    best-effort and is untested here - see the module docstring.
    """
    found: list[Path] = []
    try:
        profiles_list = sorted(profiles.iterdir())
    except OSError:
        return found
    for home in profiles_list:
        local = home / "AppData" / "Local"
        desktop = local / "Roblox" / "logs"
        if desktop.is_dir():
            found.append(desktop)
        try:
            packages = sorted((local / "Packages").glob("ROBLOXCORPORATION.ROBLOX*"))
        except OSError:
            packages = []
        for package in packages:
            store = package / "LocalState" / "logs"
            if store.is_dir():
                found.append(store)
    return found


def newest_log(dirs: list[Path], now: float | None = None,
               stale_sec: int = STALE_SEC) -> Path | None:
    """The most recently written player log, or None if none is live.

    The staleness gate is the whole idle path: a machine where Roblox has not run today
    answers this with one `iterdir` per profile and goes back to sleep.
    """
    now = time.time() if now is None else now
    best: tuple[float, Path] | None = None
    for directory in dirs:
        try:
            entries = list(directory.glob("*_Player_*.log"))
        except OSError:
            continue
        for path in entries:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > stale_sec:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, path)
    return None if best is None else best[1]


def read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Read whole lines appended since `offset`; never consume a partial one.

    The client is writing this file as we read it, and a tail that swallowed half a line
    would drop the join event that half-line was carrying. The returned offset therefore
    moves only past the last newline actually seen.

    A file that has SHRUNK was rotated or replaced under us, so the offset is meaningless
    and reading restarts from the top. Decoding is lossy on purpose: these logs carry the
    profile path, and `C:\\Users\\Th\u00e9o` is not UTF-8 in them.
    """
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            return [], offset
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError:
        return [], offset
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return [], offset  # not one complete line yet; leave it for the next tick
    text = chunk[:cut].decode("utf-8", errors="replace")
    return text.splitlines(), offset + cut + 1


# --- the ledger of what has been played ------------------------------------

"""`roblox-games.seen.json` exists for one reason: nobody can write an allow list for a
platform with sixty million experiences on it, but anyone can tick boxes next to the
fourteen his child opened this week. The guard records every join it observes, allowed or
not, and the panel draws its checkboxes from that file.

It records JOINS, never minutes. Time played is the budget lane's number, kept in
`roblox-budget.state.json`, and a second clock here would sooner or later disagree with
it in front of a parent trying to decide something. Two numbers that mean almost the same
thing is worse than one.

Written only when a join actually changes what is being played - so the idle path, and a
long session inside one game, still touch nothing.
"""


def load_seen(path: Path) -> dict[str, dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}  # a lost ledger costs a parent some checkboxes, never enforcement


def load_ratings(path: Path) -> dict[str, str]:
    """The maturity cache: universe id (as text) -> rating. Its own reader because it is
    a flat map of strings, not the ledger's map of records, and reusing `load_seen` for
    it was a type lie waiting to become a bug."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): str(v) for k, v in obj.items() if isinstance(v, str)}


def record_seen(seen: dict[str, dict], presence: Presence, when: str,
                name: str = "") -> dict[str, dict]:
    """Pure: fold one join into the ledger.

    A join with no universe is keyed on `place:<id>` rather than dropped. It is exactly
    the case a parent most needs to see - a game the guard could not identify, which in
    allow mode it will have ended - and hiding it would make the lane look like it
    misfired for no reason.
    """
    key = str(presence.universe) if presence.universe is not None \
        else f"place:{presence.place}"
    entry = dict(seen.get(key) or {})
    places = sorted({*(entry.get("places") or []), presence.place})
    entry.update({
        "places": places,
        "joins": int(entry.get("joins") or 0) + 1,
        "first": entry.get("first") or when,
        "last": when,
        "name": name or entry.get("name") or "",
    })
    merged = {**seen, key: entry}
    if len(merged) <= SEEN_MAX:
        return merged
    # Drop the least recently played, never the most: the panel's list is a list of
    # what he plays NOW, and a game from four months ago earning a checkbox forever
    # would bury the ones that matter.
    keep = sorted(merged.items(), key=lambda kv: str(kv[1].get("last") or ""),
                  reverse=True)[:SEEN_MAX]
    return dict(keep)


def merge_ledgers(existing: dict[str, dict], scanned: dict[str, dict]
                  ) -> dict[str, dict]:
    """Pure: a re-scan folded onto the ledger without double-counting.

    Idempotent by construction - `joins` takes the larger of the two rather than their
    sum - because `--history` reads the same log files every time it is run, and a parent
    who runs it twice must not be told his child played everything twice.
    """
    merged = dict(existing)
    for key, fresh in scanned.items():
        old = merged.get(key) or {}
        merged[key] = {
            "places": sorted({*(old.get("places") or []), *(fresh.get("places") or [])}),
            "joins": max(int(old.get("joins") or 0), int(fresh.get("joins") or 0)),
            "first": min(x for x in (old.get("first"), fresh.get("first")) if x),
            "last": max(x for x in (old.get("last"), fresh.get("last")) if x),
            "name": fresh.get("name") or old.get("name") or "",
        }
    return merged


def save_seen(path: Path, seen: dict[str, dict]) -> None:
    try:
        write_json(path, seen)
    except OSError:
        pass  # same contract as `log`: bookkeeping must never take the guard down


# --- the policy --------------------------------------------------------------

@dataclass(frozen=True)
class GamePolicy:
    enabled: bool = False
    mode: str = MODE_ALLOW
    universes: frozenset[int] = frozenset()
    names: dict[int, str] = field(default_factory=dict)
    notify: bool = False
    message: str = ""
    max_maturity: str = DEFAULT_MAX_MATURITY
    delay: tuple[int, int] = (killdelay.DEFAULT_MIN_SEC, killdelay.DEFAULT_MAX_SEC)

    def name_of(self, universe: int | None) -> str:
        if universe is None:
            return "unknown game"
        return self.names.get(universe) or f"universe {universe}"


def parse_policy(obj: dict) -> tuple[GamePolicy, list[str]]:
    """Pure: a parsed config object in, a policy and its complaints out.

    Every complaint is a value this refuses to guess at. None of them can widen the rule:
    a malformed entry is DROPPED from the list, which in `allow` mode means one less game
    is permitted, and in `deny` mode means... one less game is banned. That asymmetry is
    real and is why the warnings are surfaced in the panel rather than only logged.
    """
    warnings: list[str] = []
    enabled = obj.get("enabled", False)
    if not isinstance(enabled, bool):
        warnings.append(f"enabled must be true or false (got {enabled!r}), staying off")
        enabled = False

    mode = obj.get("mode", MODE_ALLOW)
    if mode not in MODES:
        warnings.append(f"mode must be one of {MODES} (got {mode!r}), using {MODE_ALLOW}")
        mode = MODE_ALLOW

    universes: set[int] = set()
    names: dict[int, str] = {}
    raw_list = obj.get("universes", [])
    if not isinstance(raw_list, list):
        warnings.append(f"universes must be a list (got {type(raw_list).__name__}), "
                        f"treating it as empty")
        raw_list = []
    for entry in raw_list:
        # Both shapes are accepted: a bare id for a parent editing by hand, and an object
        # with a name for the panel, which needs something human to draw.
        if isinstance(entry, dict):
            raw_id, name = entry.get("id"), entry.get("name")
        else:
            raw_id, name = entry, None
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            warnings.append(f"skipping universe entry {entry!r}: id must be a positive "
                            f"whole number")
            continue
        universes.add(raw_id)
        if isinstance(name, str) and name.strip():
            names[raw_id] = name.strip()

    if enabled and mode == MODE_ALLOW and not universes:
        # Not an error - "nothing is allowed" is a legitimate thing to say, and is what
        # the budget lane's `limit_min: 0` says next door - but it is worth saying out
        # loud, because it is also what an emptied-by-accident list looks like.
        warnings.append("allow mode with an empty list: every experience will be ended")

    notify_on = obj.get("notify", False)
    if not isinstance(notify_on, bool):
        warnings.append(f"notify must be true or false (got {notify_on!r}), staying "
                        f"silent")
        notify_on = False
    message = obj.get("message", "")
    if not isinstance(message, str):
        warnings.append(f"message must be text (got {message!r}), using the default")
        message = ""

    ceiling = obj.get("max_maturity", DEFAULT_MAX_MATURITY)
    if ceiling not in MATURITY_ORDER:
        warnings.append(f"max_maturity must be one of {MATURITY_ORDER} "
                        f"(got {ceiling!r}), using {DEFAULT_MAX_MATURITY}")
        ceiling = DEFAULT_MAX_MATURITY

    return GamePolicy(
        enabled=enabled, mode=mode, universes=frozenset(universes), names=names,
        notify=notify_on, message=message, max_maturity=ceiling,
        delay=killdelay.load_bounds(obj, warnings),
    ), warnings


def above_ceiling(maturity: str | None, ceiling: str) -> bool:
    """Pure: is this rating above the ceiling? An UNKNOWN rating counts as above it.

    Unknown is the important case and it is the one a naive comparison gets backwards.
    A game whose rating could not be read is exactly a game nothing has vouched for, so
    it has to sort with the ones over the line rather than with the safe ones.
    """
    if maturity not in MATURITY_ORDER:
        return True
    return MATURITY_ORDER.index(maturity) > MATURITY_ORDER.index(ceiling)


def maturity_for(universe: int, seen: dict[str, dict],
                 ratings: dict[str, str] | None = None) -> str | None:
    """The rating for one game: the ledger first, the ratings cache second.

    Two sources because they cover different games, and using only the first is a bug
    that reads as the opposite of what it is. The ledger holds what has been PLAYED;
    a curated list's most deliberate entries have never been played at all, and their
    rating lives only in the cache. Looking in the ledger alone reported thirteen
    correctly-rated games as "unknown" - which `above_ceiling` sorts as over the line,
    so the most carefully chosen games were the ones flagged as unvetted.
    """
    key = str(universe)
    from_ledger = (seen.get(key) or {}).get("maturity")
    return from_ledger or (ratings or {}).get(key)


def flag_ceiling(policy: GamePolicy, seen: dict[str, dict],
                 ratings: dict[str, str] | None = None) -> list[str]:
    """Listed games rated above the policy's ceiling, as warnings a human should read.

    Deliberately a REPORT and not a veto. Refusing to enforce a list the parent wrote
    would be this module deciding it knows better than the person who set the rule; the
    honest thing is to keep enforcing exactly what he asked for and say, every time, that
    two of the entries are rated higher than the line he drew.
    """
    flagged = []
    for universe in sorted(policy.universes):
        rating = maturity_for(universe, seen, ratings)
        if above_ceiling(rating, policy.max_maturity):
            flagged.append(f"{policy.name_of(universe)} is rated "
                           f"{rating or 'unknown'}, above the {policy.max_maturity} "
                           f"ceiling - it is allowed because it is on the list")
    return flagged


def load_policy(path: Path, last_good: GamePolicy | None
                ) -> tuple[GamePolicy, list[str], bool]:
    """Read the config file. Returns (policy, warnings, is_fault).

    `is_fault` is the third state the other lanes do not have and this one needs: a file
    that EXISTS and cannot be read is different from one that was never written, and the
    difference has to reach the parent rather than being absorbed into a default.
    """
    if not path.exists():
        return GamePolicy(), [], False
    try:
        # utf-8-sig for the same reason the budget lane needs it: PowerShell 5.1 writes
        # a BOM, and a BOM here once turned a valid config into a silent no-op.
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        held = last_good if last_good is not None else GamePolicy()
        kept = ("holding the last good policy" if last_good is not None
                else "no good policy has been read yet - enforcing nothing")
        return held, [f"bad {path.name} ({type(exc).__name__}: {exc}) - {kept}"], True
    policy, warnings = parse_policy(obj)
    return policy, warnings, False


# --- the decision ------------------------------------------------------------

def verdict(presence: Presence | None, policy: GamePolicy) -> tuple[str, str]:
    """Pure: ("idle" | "allow" | "kill", reason).

    Reconciled from scratch every tick, like every other decision in this repo: there is
    no latch to clear and nothing that "already decided" survives a config edit.
    """
    if not policy.enabled:
        return "idle", "lane disabled"
    if presence is None:
        return "idle", "not in an experience"
    listed = presence.universe is not None and presence.universe in policy.universes
    if policy.mode == MODE_ALLOW:
        if listed:
            return "allow", f"{policy.name_of(presence.universe)} is on the allow list"
        if presence.universe is None:
            return "kill", (f"place {presence.place} joined with no universe in the log "
                            f"- an allow list cannot vouch for it")
        return "kill", f"{policy.name_of(presence.universe)} is not on the allow list"
    if listed:
        return "kill", f"{policy.name_of(presence.universe)} is on the deny list"
    if presence.universe is None:
        return "allow", (f"place {presence.place} joined with no universe in the log "
                         f"- a deny list cannot convict it")
    return "allow", f"{policy.name_of(presence.universe)} is not on the deny list"


def default_message(policy: GamePolicy, presence: Presence | None) -> str:
    if policy.message:
        return policy.message
    return "Este jogo do Roblox nao esta liberado. Escolha outro na lista combinada."


# --- the resident loop -------------------------------------------------------

@dataclass
class Tracker:
    """The only mutable thing in the loop, kept in one place so it can be reasoned about."""
    path: Path | None = None
    offset: int = 0
    presence: Presence | None = None
    quiet_since: float | None = None
    last_storm_log: float = 0.0
    kills_in_window: int = 0


def run(root: Path, *, dry_run: bool = False, dirs: list[Path] | None = None,
        ticks: int | None = None) -> int:
    """The loop. `ticks` bounds it and exists only so the tests can drive it.

    An infinite loop that no test can enter is an infinite loop nobody has checked, and
    the parts worth checking - that a join is noticed, that the verdict reaches taskkill,
    that a second tick does not kill the same join twice - live in here rather than in
    the pure functions above.
    """
    config_path = root / "roblox-games.json"
    log_path = root / "roblox-games.log"
    seen_path = root / "roblox-games.seen.json"
    prefix = "[dry-run] " if dry_run else ""

    def remember(presence: Presence | None) -> None:
        """Fold a FINISHED session into the ledger.

        On the way out, not on the way in, and that timing is the point: one join writes
        two log lines and the universeid is on the second, so a presence recorded the
        instant it appears can be recorded under `place:<id>` a tick before the universe
        arrives - leaving a phantom unidentified game in the parent's list next to the
        real one. By the time a game ENDS, both lines are long past.
        """
        if presence is None or dry_run:
            return
        when = presence.at or f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S}"
        save_seen(seen_path, record_seen(load_seen(seen_path), presence, when,
                                         policy.names.get(presence.universe, "")))

    policy = GamePolicy()
    last_good: GamePolicy | None = None
    said: list[str] = []
    read_ts = -CONFIG_EVERY_SEC
    track = Tracker()
    tick = 0

    while ticks is None or tick < ticks:
        tick += 1
        now = time.monotonic()

        if now - read_ts >= CONFIG_EVERY_SEC:
            policy, warnings, fault = load_policy(config_path, last_good)
            if not fault:
                last_good = policy
            read_ts = now
            # Config complaints are said when they CHANGE, not every five seconds. A
            # broken file on a machine nobody is playing on must not fill the log.
            if warnings != said:
                for warning in warnings:
                    log(log_path, f"[warn] {warning}")
                said = list(warnings)

        if not policy.enabled:
            track = Tracker()
            time.sleep(POLL_SEC)
            continue

        newest = newest_log(dirs if dirs is not None else log_dirs())
        if newest is None:
            track = Tracker()
            time.sleep(POLL_SEC)
            continue

        if newest != track.path:
            # A file seen for the first time is read WHOLE, not tailed from its end: the
            # guard may have just been restarted into a session that is already in a
            # game, and the only record of that join is behind us in this file.
            remember(track.presence)
            track = Tracker(path=newest)

        lines, track.offset = read_new_lines(newest, track.offset)
        if lines:
            track.presence, ended = fold_presence(lines, track.presence)
            for finished in ended:
                remember(finished)
            track.quiet_since = None
        elif track.quiet_since is None:
            track.quiet_since = now

        # Roblox up, log silent: the one shape that could be tampering, and the one this
        # lane must never read as an all-clear.
        if (track.quiet_since is not None and now - track.quiet_since >= BLIND_AFTER_SEC
                and running_processes()):
            log(log_path, f"[warn] blind: Roblox is running but {newest.name} has said "
                          f"nothing for {int(now - track.quiet_since)}s")
            track.quiet_since = now  # rate-limit to one line per BLIND_AFTER_SEC

        decision, reason = verdict(track.presence, policy)
        if decision != "kill":
            time.sleep(POLL_SEC)
            continue

        low, high = policy.delay
        caught = track.presence
        _, still = killdelay.pause_and_recheck(
            running_processes, low=low, high=high,
            announce=lambda d: log(log_path, f"{prefix}[kill] {reason} "
                                             f"(place {caught.place}) - ending Roblox "
                                             f"in {d}s"),
        )
        if not still:
            log(log_path, f"{prefix}[stand-down] closed during the pause - "
                          f"nothing left to end")
        elif not dry_run:
            if policy.notify:
                notify(default_message(policy, caught))
            for failure in kill_processes(list(still)):
                log(log_path, f"[error] taskkill failed: {failure}")

        # Whatever we saw is gone with the process. Forgetting it is what stops the next
        # tick re-killing on the same stale join line - and a game the guard ENDED still
        # belongs in the ledger, so the parent can see what he tried to play.
        remember(caught)
        track.presence = None
        if now - track.last_storm_log >= STORM_WINDOW_SEC:
            track.last_storm_log, track.kills_in_window = now, 0
        track.kills_in_window += 1
        if track.kills_in_window == 4:
            log(log_path, f"[warn] four kills inside {STORM_WINDOW_SEC}s - something is "
                          f"relaunching Roblox behind the guard, or the list is wrong")
        time.sleep(POLL_SEC)
    return 0


# --- one-shot entry points ---------------------------------------------------

def once(root: Path, *, dirs: list[Path] | None = None) -> int:
    """Read the log once and say what the lane WOULD do. Kills nothing."""
    policy, warnings, fault = load_policy(root / "roblox-games.json", None)
    for warning in warnings:
        print(f"[warn] {warning}")
    newest = newest_log(dirs if dirs is not None else log_dirs())
    if newest is None:
        print("no live Roblox log - the client is not running")
        return 0
    lines, _ = read_new_lines(newest, 0)
    presence = reduce_presence(lines, None)
    decision, reason = verdict(presence, policy)
    print(f"log      : {newest}")
    print(f"in game  : {'no' if presence is None else policy.name_of(presence.universe)}"
          + ("" if presence is None
             else f" (place {presence.place}, universe {presence.universe})"))
    print(f"verdict  : {decision.upper()} - {reason}")
    return 2 if fault else 0


def status(root: Path) -> int:
    policy, warnings, fault = load_policy(root / "roblox-games.json", None)
    state = "FAULT" if fault else ("ENFORCING" if policy.enabled else "off")
    print(f"Roblox game filter - {state}")
    print(f"  Mode       : {policy.mode}")
    print(f"  Listed     : {len(policy.universes)} universes")
    for universe in sorted(policy.universes):
        print(f"               {universe}  {policy.names.get(universe, '')}")
    print(f"  Ceiling    : {policy.max_maturity} (a report, not a veto)")
    print(f"  Checks     : every {POLL_SEC:g}s, resident, reading the client log")
    print(f"  Log dirs   : {len(log_dirs())} found")
    for flag in flag_ceiling(policy, load_seen(root / "roblox-games.seen.json"),
                             load_ratings(root / "roblox-games.maturity.json")):
        print(f"  [note] {flag}")
    for warning in warnings:
        print(f"  [warn] {warning}")
    return 2 if fault else 0


# --- history -----------------------------------------------------------------

@dataclass(frozen=True)
class Session:
    start: str
    end: str
    place: int
    universe: int | None


def scan_history(dirs: list[Path]) -> list[Session]:
    """Every join in every log still on disk, closed by its leave or by the log's end.

    This is the lane's onboarding: a parent cannot write an allow list for a platform
    with sixty million experiences on it, but he can look at the fourteen his child
    actually opened this week and tick the ones that stay.
    """
    sessions: list[Session] = []
    for directory in dirs:
        try:
            files = sorted(directory.glob("*_Player_*.log"))
        except OSError:
            continue
        for path in files:
            try:
                text = path.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            open_at: Presence | None = None
            last_stamp = ""
            for line in text.splitlines():
                marker = stamp(line)
                if marker:
                    last_stamp = marker
                kind, found = parse_line(line)
                if kind == "join":
                    if upgrades(open_at, found):
                        open_at = merge(open_at, found)
                        continue
                    if open_at is not None:
                        sessions.append(Session(open_at.at, marker or last_stamp,
                                                open_at.place, open_at.universe))
                    open_at = found
                elif kind == "leave" and open_at is not None:
                    sessions.append(Session(open_at.at, marker or last_stamp,
                                            open_at.place, open_at.universe))
                    open_at = None
            if open_at is not None:
                sessions.append(Session(open_at.at, last_stamp, open_at.place,
                                        open_at.universe))
    return sorted(sessions, key=lambda s: s.start)


def minutes(session: Session) -> int:
    try:
        start = datetime.strptime(session.start, "%Y-%m-%dT%H:%M:%S")
        end = datetime.strptime(session.end, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0
    return max(int((end - start).total_seconds() // 60), 0)


def fetch_names(universes: list[int], cache_path: Path,
                opener=None) -> tuple[dict[int, str], str | None]:
    """Put names on universe ids, from cache first and Roblox's public API second.

    Only `--history` calls this. The enforcing loop never touches the network - the
    universeid it needs is already in the log - and that separation is deliberate: a
    filter that stops working when the internet does is not a filter.
    """
    cache: dict[int, str] = {}
    try:
        cache = {int(k): str(v) for k, v in
                 json.loads(cache_path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    missing = [u for u in universes if u not in cache]
    if not missing:
        return cache, None
    if opener is None:
        import urllib.request
        opener = urllib.request.urlopen
    url = ("https://games.roblox.com/v1/games?universeIds="
           + ",".join(str(u) for u in missing))
    try:
        with opener(url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for game in payload.get("data", []):
            cache[int(game["id"])] = str(game.get("name", ""))
    except Exception as exc:  # noqa: BLE001 - names are a nicety; never fail the report
        return cache, f"could not reach the Roblox API ({type(exc).__name__}: {exc})"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({str(k): v for k, v in sorted(cache.items())},
                                         indent=1), encoding="utf-8")
    except OSError:
        pass
    return cache, None


def fetch_maturity(named: dict[int, str], cache_path: Path,
                   opener=None, pause=None) -> tuple[dict[int, str], str | None]:
    """Roblox's content maturity per universe, from cache first and its search API second.

    The mechanism is ugly and the ugliness is documented rather than hidden: there is no
    public endpoint that takes a universe id and returns a rating. The only public
    surface that carries `contentMaturity` at all is omni-search, so each game is looked
    up BY NAME and the result is accepted only when the universe id matches exactly.
    A rename upstream therefore costs a rating, not a wrong rating.

    Like `fetch_names`, this is curation-time only. The enforcing loop never calls it -
    the kill path has to work with the internet down, and a filter that opens up when DNS
    is broken is not a filter.
    """
    import time as _time
    import urllib.parse
    cache: dict[int, str] = {}
    try:
        cache = {int(k): str(v) for k, v in
                 json.loads(cache_path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    missing = {u: n for u, n in named.items() if u not in cache and n}
    if not missing:
        return cache, None
    if opener is None:
        import urllib.request
        opener = urllib.request.urlopen
    pause = _time.sleep if pause is None else pause

    problem = None
    for universe, name in missing.items():
        url = ("https://apis.roblox.com/search-api/omni-search?searchQuery="
               + urllib.parse.quote(name)
               + "&pageToken=&sessionId=safehouse&pageType=all")
        try:
            with opener(url, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - a rating is a nicety, never a failure
            problem = f"content maturity lookup failed ({type(exc).__name__}: {exc})"
            break
        for group in payload.get("searchResults", []):
            for item in group.get("contents", []):
                if item.get("universeId") == universe and item.get("contentMaturity"):
                    cache[universe] = str(item["contentMaturity"])
        pause(0.5)  # the search API is not ours; do not hammer it

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({str(k): v for k, v in sorted(cache.items())},
                                         indent=1), encoding="utf-8")
    except OSError:
        pass
    return cache, problem


def apply_maturity(seen: dict[str, dict], ratings: dict[int, str]) -> dict[str, dict]:
    """Pure: fold ratings into the ledger the panel reads."""
    out = dict(seen)
    for universe, rating in ratings.items():
        key = str(universe)
        if key in out:
            out[key] = {**out[key], "maturity": rating}
    return out


def history(root: Path, *, dirs: list[Path] | None = None, offline: bool = False,
            tz_hours: float = -3.0) -> int:
    sessions = scan_history(dirs if dirs is not None else log_dirs())
    if not sessions:
        print("no joins found in any Roblox log on this machine")
        return 0
    universes = sorted({s.universe for s in sessions if s.universe is not None})
    names: dict[int, str] = {}
    ratings: dict[int, str] = {}
    if not offline:
        names, problem = fetch_names(universes, root / "roblox-games.cache.json")
        if problem:
            print(f"[warn] {problem} - showing ids only")
        ratings, rating_problem = fetch_maturity(
            {u: names.get(u, "") for u in universes},
            root / "roblox-games.maturity.json")
        if rating_problem:
            print(f"[warn] {rating_problem} - ratings will show as unknown")
    tz = timezone(timedelta(hours=tz_hours))

    print(f"\n{len(sessions)} sessions, {len(universes)} distinct games\n")
    print(f"{'start (local)':17} {'min':>4}  {'universe':>13} {'place':>15}  game")
    print("-" * 96)
    totals: dict[int | None, int] = {}
    places: dict[int | None, set[int]] = {}
    for session in sessions:
        spent = minutes(session)
        totals[session.universe] = totals.get(session.universe, 0) + spent
        places.setdefault(session.universe, set()).add(session.place)
        try:
            when = (datetime.strptime(session.start, "%Y-%m-%dT%H:%M:%S")
                    .replace(tzinfo=timezone.utc).astimezone(tz))
            shown = f"{when:%Y-%m-%d %H:%M}"
        except ValueError:
            shown = session.start or "?"
        print(f"{shown:17} {spent:>4}  {str(session.universe or '?'):>13} "
              f"{session.place:>15}  {names.get(session.universe, '')[:34]}")

    print(f"\n{'total min':>9}  {'universe':>13}  {'places':>6}  {'maturity':9} game")
    print("-" * 96)
    for universe, spent in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"{spent:>9}  {str(universe or '?'):>13}  {len(places[universe]):>6}  "
              f"{ratings.get(universe, 'unknown'):9} {names.get(universe, '')[:40]}")
    over = sorted(u for u in universes
                  if above_ceiling(ratings.get(u), DEFAULT_MAX_MATURITY))
    if over:
        print(f"\n{len(over)} of {len(universes)} played games are rated above "
              f"'{DEFAULT_MAX_MATURITY}':")
        for universe in over:
            print(f"  {ratings.get(universe, 'unknown'):9} {names.get(universe, universe)}")
        print("  Roblox's account-side maturity setting did not stop these from being "
              "joined.")
    # The report is also the ledger's seed: the panel draws its checkboxes from
    # roblox-games.seen.json, and on install day the only history that exists is the one
    # sitting in Roblox's own log directory.
    # Built from EMPTY and then merged, never folded onto what is already there: the
    # logs on disk are the same logs every time, so re-running --history must not add
    # their joins a second time. `merge_ledgers` is idempotent for exactly that reason.
    scanned: dict[str, dict] = {}
    for session in sessions:
        scanned = record_seen(scanned, Presence(session.place, session.universe,
                                                session.start),
                              session.start, names.get(session.universe, ""))
    ledger = apply_maturity(
        merge_ledgers(load_seen(root / "roblox-games.seen.json"), scanned), ratings)
    save_seen(root / "roblox-games.seen.json", ledger)
    print(f"\nrecorded {len(ledger)} games in roblox-games.seen.json "
          f"(the panel's checkbox list)")

    print("\nTo allow these, put their universe ids in roblox-games.json:")
    print(json.dumps({"enabled": True, "mode": MODE_ALLOW, "universes": [
        {"id": u, "name": names.get(u, "")} for u in universes[:3]
    ]}, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safehouse-roblox-games", description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--status", action="store_true",
                   help="print the policy and exit")
    p.add_argument("--once", action="store_true",
                   help="read the log once, say what would happen, kill nothing")
    p.add_argument("--history", action="store_true",
                   help="every experience joined on this machine, from the logs on disk")
    p.add_argument("--offline", action="store_true",
                   help="with --history: do not ask Roblox for names")
    p.add_argument("--dry-run", action="store_true",
                   help="run the loop for real, but log the kills instead of making them")
    return p


def readable_console() -> None:
    """Stop a game's name from taking the report down.

    Roblox experience names are full of emoji - `Builder Game K 💀`, `Build a Stone
    Miner! ⚒️` - and a Windows console on this pt-BR box encodes cp1252, which has no
    code point for any of them. `--history` printed four rows and then died on a skull.
    Found only on the machine itself: every test runs on a UTF-8 stdout, so nothing here
    could have caught it.

    `errors="replace"` rather than a strict re-encode, for the same reason `log` swallows
    OSError: a report that cannot render one character must lose that character, not the
    other fourteen games.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # a stream that cannot be reconfigured is a stream to leave alone


def main(argv: list[str] | None = None) -> int:
    readable_console()
    args = build_parser().parse_args(argv)
    if args.status:
        return status(args.root)
    if args.history:
        return history(args.root, offline=args.offline)
    if args.once:
        return once(args.root)
    return run(args.root, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
