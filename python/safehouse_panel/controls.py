"""The writing half of the panel: clamp a value, write it atomically, report what happened.

Three rules hold this module together.

**The browser is not trusted.** Every value arriving from the page is re-validated here,
against the same ranges the guards themselves enforce. The slider's `max` attribute is a
convenience for the parent, not a control: a `POST` with `roblox_limit_min=1440` is one
line of JavaScript away, and the panel now listens on the house LAN.

**Out of range clamps, wrong type refuses.** `240` when the maximum is 240 is a parent
dragging to the end of the slider; `"lots"` is a bug or an attack, and guessing what it
meant is how a control ends up doing something nobody asked for. Clamping is reported
back in the message so the page never shows a number the file does not contain.

**Every write is atomic and preserves what it did not touch.** `atomicio.write_json` is
the same rename-based write the guards use - a torn config file is read as "broken" and
falls back to the strict default, which for Roblox means the game stops working. And the
writes are read-modify-write: a parent who hand-edited `message` or `users` in
`browser-guard.json` must not lose it because a slider moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from safehouse_ytbudget import browser_guard, roblox_guard
from safehouse_ytbudget.atomicio import write_json

# --- ranges ------------------------------------------------------------------
# The upper bounds are the panel's, not the guards': `roblox_guard.load_limit_sec`
# accepts up to 24h, but a slider that can hand out a whole day is a slider that will
# eventually be nudged there by accident. 240 minutes is four hours, well past anything
# this house has ever granted, and the config file is still there for a deliberate
# exception.
ROBLOX_LIMIT_RANGE = (0, 240)
ROBLOX_BONUS_RANGE = (0, 240)
BROWSER_LIMIT_RANGE = (0, 240)
BROWSER_UNLOCK_RANGE = (0, 240)
# The kill pause. `killdelay.MAX_DELAY_SEC` is 60 because the task's ExecutionTimeLimit
# is PT5M and a pause the scheduler cuts off is a browser that survives. The floor is 1
# rather than 0: a window that vanishes the instant it opens reads as a broken PC.
DELAY_RANGE = (1, 60)

ROBLOX_CONFIG = "roblox-budget.json"
ROBLOX_STATE = "roblox-budget.state.json"
BROWSER_CONFIG = "browser-guard.json"
BROWSER_STATE = "browser-guard.state.json"


class ControlError(ValueError):
    """A value the panel refuses to act on. The message is shown to the parent."""


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    before: object = None
    after: object = None
    detail: str = ""


# --- coercion ----------------------------------------------------------------

def coerce_int(value: object, *, field: str) -> int:
    """An integer, or `ControlError`. Booleans are not integers here.

    `True` is `1` to Python, so an unchecked `int()` turns a checkbox posted into a
    minutes field into "one minute" - a value nobody chose, silently accepted.
    """
    if isinstance(value, bool) or value is None:
        raise ControlError(f"{field} must be a whole number, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != int(value):
            raise ControlError(f"{field} must be a whole number, got {value!r}")
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 10)
        except ValueError:
            raise ControlError(f"{field} must be a whole number, got {value!r}") from None
    raise ControlError(f"{field} must be a whole number, got {value!r}")


def coerce_bool(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0",
                                                            "on", "off", "yes", "no"}:
        return value.strip().lower() in {"true", "1", "on", "yes"}
    raise ControlError(f"{field} must be true or false, got {value!r}")


def clamp(value: int, bounds: tuple[int, int], *, field: str) -> tuple[int, str]:
    """Return (value inside `bounds`, note describing any clamping)."""
    low, high = bounds
    if value < low:
        return low, f"{field} {value} is below the minimum, applied {low}"
    if value > high:
        return high, f"{field} {value} is above the maximum, applied {high}"
    return value, ""


def coerce_minutes(value: object, bounds: tuple[int, int], *, field: str) -> tuple[int, str]:
    return clamp(coerce_int(value, field=field), bounds, field=field)


# --- JSON files --------------------------------------------------------------

def read_config(path: Path) -> dict:
    """Read a guard config, or `{}` if it is absent or unreadable.

    utf-8-sig for the same reason `roblox_guard` uses it: PowerShell 5.1 writes a BOM,
    and a BOM once made a hand-edited budget file unparseable - the parent had granted
    an hour and the guard silently read zero.
    """
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def update_config(path: Path, changes: dict) -> tuple[dict, dict]:
    """Merge `changes` into the JSON at `path` atomically. Returns (before, after)."""
    before = read_config(path)
    after = dict(before)
    after.update(changes)
    write_json(Path(path), after)
    return before, after


# --- the actions -------------------------------------------------------------

def set_roblox_limit(root: Path, value: object, *, now: int) -> Outcome:
    minutes, note = coerce_minutes(value, ROBLOX_LIMIT_RANGE, field="Roblox daily minutes")
    path = Path(root) / ROBLOX_CONFIG
    before, _ = update_config(path, {"limit_min": minutes})
    was = before.get("limit_min", "unset")
    message = f"Roblox daily budget is now {minutes} min (was {was})."
    if minutes == 0:
        message += " Zero means the game is closed within a minute of launching."
    return Outcome(True, (message + (" " + note if note else "")).strip(),
                   before=was, after=minutes)


def set_roblox_bonus(root: Path, value: object, *, now: int) -> Outcome:
    """Extra minutes for TODAY only, written into the state file.

    Not the config, and this is the difference that matters: the state carries a date and
    the guard throws the whole state away at the rollover, so a grant expires by itself.
    A parent who granted an hour in the config file at 19:00 has quietly changed the
    standing policy instead.
    """
    minutes, note = coerce_minutes(value, ROBLOX_BONUS_RANGE, field="Roblox bonus minutes")
    path = Path(root) / ROBLOX_STATE
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    state, _ = roblox_guard.load_state(path, today)
    if state.date != today:
        state = roblox_guard.GuardState.fresh(today)
    was = state.bonus_sec // 60
    roblox_guard.save_state(path, replace(state, bonus_sec=minutes * 60))
    message = (f"Roblox bonus for today is now {minutes} min (was {was})."
               if minutes else f"Today's Roblox bonus of {was} min has been taken back.")
    return Outcome(True, (message + " It expires at midnight."
                          + (" " + note if note else "")).strip(),
                   before=was, after=minutes)


def set_browser_enabled(root: Path, value: object, *, now: int) -> Outcome:
    enabled = coerce_bool(value, field="Browser guard enabled")
    before, _ = update_config(Path(root) / BROWSER_CONFIG, {"enabled": enabled})
    was = before.get("enabled", True)
    return Outcome(True,
                   ("Browser guard is ENFORCING - any browser is closed within about a "
                    "minute." if enabled else
                    "Browser guard is OFF. Nothing closes a browser now, and this does "
                    "not expire by itself - use the unlock button for a timed pass."),
                   before=was, after=enabled)


def set_browser_limit(root: Path, value: object, *, now: int) -> Outcome:
    """Write `limit_min` into browser-guard.json.

    **The browser guard does not read this yet.** That lane is always-kill today: it ends
    a browser whenever one is up, with no metering at all. The field is written so the
    panel and the config agree about what the parent asked for, and so the metering work
    (a separate task) has a value to honour; until then the number changes nothing, and
    the page says so in as many words rather than showing a slider that silently does
    nothing.
    """
    minutes, note = coerce_minutes(value, BROWSER_LIMIT_RANGE,
                                   field="Browser daily minutes")
    before, _ = update_config(Path(root) / BROWSER_CONFIG, {"limit_min": minutes})
    was = before.get("limit_min", "unset")
    return Outcome(True,
                   (f"Stored a browser budget of {minutes} min/day (was {was}). "
                    "NOT ENFORCED YET - the browser lane still closes every browser; "
                    "metering is separate work." + (" " + note if note else "")).strip(),
                   before=was, after=minutes)


def set_browser_unlock(root: Path, value: object, *, now: int) -> Outcome:
    """Suspend the browser lane for N minutes (0 cancels).

    This is also the answer to "the panel is viewed in a browser and the browser guard
    closes browsers". Viewing the panel from the guarded machine means the guard will
    close the window it is being read in, within about a minute - so unlock first, or
    read it from a phone.
    """
    minutes, note = coerce_minutes(value, BROWSER_UNLOCK_RANGE, field="Unlock minutes")
    path = Path(root) / BROWSER_STATE
    state, _ = browser_guard.load_state(path)
    was = max(0, (state.unlock_until_ts - now + 59) // 60)
    browser_guard.save_state(path, replace(state,
                                           unlock_until_ts=now + minutes * 60 if minutes
                                           else 0))
    message = (f"Browsing is unlocked for {minutes} min." if minutes
               else "Unlock cancelled - the browser guard is enforcing again.")
    return Outcome(True, (message + (" " + note if note else "")).strip(),
                   before=was, after=minutes)


def set_browser_notify(root: Path, value: object, *, now: int) -> Outcome:
    notify = coerce_bool(value, field="Notify")
    before, _ = update_config(Path(root) / BROWSER_CONFIG, {"notify": notify})
    was = before.get("notify", False)
    return Outcome(True,
                   ("The child will now see a message on screen when a browser is closed."
                    if notify else
                    "Browsers close silently - the child is not told why."),
                   before=was, after=notify)


def _set_delay(path: Path, payload: object, *, label: str) -> Outcome:
    if not isinstance(payload, dict):
        raise ControlError(f"{label} needs a min and a max")
    low, note_low = coerce_minutes(payload.get("min_sec"), DELAY_RANGE,
                                   field=f"{label} minimum")
    high, note_high = coerce_minutes(payload.get("max_sec"), DELAY_RANGE,
                                     field=f"{label} maximum")
    notes = [n for n in (note_low, note_high) if n]
    if high < low:
        high = low
        notes.append("the maximum was below the minimum, so both are now the minimum")
    before, _ = update_config(path, {"delay_min_sec": low, "delay_max_sec": high})
    was = (before.get("delay_min_sec", "unset"), before.get("delay_max_sec", "unset"))
    return Outcome(True,
                   (f"{label} is now {low}-{high}s (was {was[0]}-{was[1]}s)."
                    + (" " + "; ".join(notes) if notes else "")),
                   before=list(was), after=[low, high])


def set_roblox_delay(root: Path, value: object, *, now: int) -> Outcome:
    return _set_delay(Path(root) / ROBLOX_CONFIG, value, label="Roblox kill delay")


def set_browser_delay(root: Path, value: object, *, now: int) -> Outcome:
    return _set_delay(Path(root) / BROWSER_CONFIG, value, label="Browser kill delay")


# --- YouTube, at the resolver ------------------------------------------------
#
# The source of truth for these lines is adguard/user-rules.txt in this repo, which
# adguard/apply-filters.py reconciles into the running instance. They are restated here
# because the panel runs on the Windows box, where the repo may not be checked out at
# all - and a test asserts the two lists have not drifted apart.

YOUTUBE_RULES = (
    "||youtube.com^",
    "||youtu.be^",
    "||youtubekids.com^",
    "||youtube-nocookie.com^",
    "||googlevideo.com^",
    "||ytimg.com^",
    "||youtubei.googleapis.com^",
    "||youtube.googleapis.com^",
)

# Written with the rules so the AdGuard UI stays self-explaining, and removed with them
# so a rule-less header does not sit there implying a block that is gone.
YOUTUBE_COMMENTS = (
    "! SafeHouse: YouTube blocked (control panel)",
    "! Blocked at the resolver because it is the only lane that also covers the Play "
    "Games VM, a phone, a tablet and a TV. This takes YouTube Music with it.",
)

_YOUTUBE_OWNED = {line.strip() for line in YOUTUBE_RULES + YOUTUBE_COMMENTS}


def youtube_state(rules: list[str] | None) -> str:
    """BLOCKED / ALLOWED / PARTIAL / UNKNOWN, from the live user-rules list."""
    if rules is None:
        return "UNKNOWN"
    present = {line.strip() for line in rules}
    hits = sum(1 for rule in YOUTUBE_RULES if rule in present)
    if hits == len(YOUTUBE_RULES):
        return "BLOCKED"
    return "ALLOWED" if hits == 0 else "PARTIAL"


def add_youtube_rules(rules: list[str]) -> list[str]:
    """Append whatever is missing, in order, changing nothing else."""
    present = {line.strip() for line in rules}
    missing = [line for line in YOUTUBE_COMMENTS + YOUTUBE_RULES
               if line.strip() not in present]
    if not missing:
        return list(rules)
    return list(rules) + ([""] if rules and rules[-1].strip() else []) + missing


def remove_youtube_rules(rules: list[str]) -> list[str]:
    """Drop only SafeHouse's own YouTube lines. Everything else survives, deliberately.

    The obvious implementation - "drop every line mentioning youtube" - breaks this
    machine in two ways that are invisible until a child complains:

    * `@@||firebaseinstallations.googleapis.com^` is an ALLOW rule. Firebase Installations
      is app identity, not advertising; a blocklist catches it and the games then fail to
      launch or sign in. Losing it would look like "the games broke today", with nothing
      connecting it to a YouTube toggle. So allow rules are never touched here, by an
      explicit test at the top of the loop rather than by the accident of not matching.
    * The Roblox child-safety rules (`voice.roblox.com`, the ad units) are a separate
      policy that happens to live in the same list. A YouTube toggle must not carry them
      away.

    Matching is by exact line against this module's own set, so a rule a parent typed by
    hand in the AdGuard UI - even one about YouTube - is left exactly where it is.
    """
    kept: list[str] = []
    for line in rules:
        stripped = line.strip()
        if stripped.startswith("@@"):  # allow rules: never ours to remove
            kept.append(line)
            continue
        if stripped in _YOUTUBE_OWNED:
            continue
        kept.append(line)
    # Do not leave the blank separator that used to precede our block dangling at the end.
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def set_youtube_blocked(root: Path, value: object, *, now: int, client=None) -> Outcome:
    """Drive YouTube through AdGuard's user rules.

    Note this is one of TWO resolver lanes for YouTube. The other is `blocked_services`,
    which the watch-time budget and `safehouse_setup toggle youtube` own. The panel shows
    both and drives only this one, so lifting a block here cannot silently undo a block
    the budget applied for a reason of its own.
    """
    blocked = coerce_bool(value, field="YouTube blocked")
    if client is None:
        return Outcome(False, "AdGuard Home is not configured on this machine "
                              "(no usable adguard.json), so YouTube cannot be changed "
                              "at the resolver.")
    live = client.user_rules()
    if live is None:
        return Outcome(False, "AdGuard Home did not answer, so nothing was changed. "
                              "The resolver may be down - check WSL.")
    was = youtube_state(live)
    wanted = add_youtube_rules(live) if blocked else remove_youtube_rules(live)
    if wanted == live:
        return Outcome(True, f"YouTube was already {was.lower()} at the resolver; "
                             f"nothing to change.", before=was, after=was)
    result = client.set_user_rules(wanted)
    if result != "OK":
        return Outcome(False, f"AdGuard Home refused the change ({result}). "
                              f"YouTube is still {was.lower()}.", before=was, after=was)
    now_state = "BLOCKED" if blocked else "ALLOWED"
    note = ("" if blocked else
            " Note: adguard/apply-filters.py re-adds these rules when it next runs, so "
            "an unblock that must survive a reconcile needs the repo changed too.")
    return Outcome(True, f"YouTube is now {now_state.lower()} at the resolver." + note,
                   before=was, after=now_state)


# --- the filtering resolver --------------------------------------------------

def start_resolver(root: Path, value: object, *, now: int, collector=None) -> Outcome:
    """Ask the resolver watchdog to run now.

    The panel cannot do this itself. It runs as LocalSystem and Windows refuses to run
    WSL there at all (`WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED`) - not a permissions problem, but
    a missing distro registration, since distros live in the user's own registry hive. So
    the work belongs to `SafeHouse-Resolver`, which runs in the logged-on user's session,
    and this only presses its button.

    Which means the honest answer here is "asked", not "done": `schtasks /Run` returns as
    soon as the scheduler accepts, while starting a cold distro and reconciling two
    container stacks takes tens of seconds. The page's own 10-second poll shows when the
    resolvers actually start answering, and the message says so rather than reporting a
    success nobody has verified.
    """
    if collector is None:
        return Outcome(False, "The resolver watchdog cannot be reached from here.")
    result = collector.start_resolver()
    if not result.get("ok"):
        detail = result.get("detail") or "the scheduler refused the request"
        return Outcome(False,
                       f"Could not start {RESOLVER_TASK}: {detail} "
                       f"Two things cause this: the task is not registered "
                       f"(install.py --resolver-only), or nobody is signed in to the PC - "
                       f"WSL cannot be started without a user session.")
    return Outcome(True,
                   "Asked the resolver watchdog to run. Starting WSL and both containers "
                   "takes up to a minute; this page will show them answering when they "
                   "do. If it still says down after a minute, nobody is signed in to the "
                   "PC or WSL itself is refusing to start.",
                   before="asked", after="asked")


RESOLVER_TASK = "SafeHouse-Resolver"


ACTIONS = {
    "roblox_limit_min": set_roblox_limit,
    "roblox_bonus_min": set_roblox_bonus,
    "browser_enabled": set_browser_enabled,
    "browser_limit_min": set_browser_limit,
    "browser_unlock_min": set_browser_unlock,
    "browser_notify": set_browser_notify,
    "roblox_delay_sec": set_roblox_delay,
    "browser_delay_sec": set_browser_delay,
    "youtube_blocked": set_youtube_blocked,
    "resolver_start": start_resolver,
}


# --- what a control currently reads ------------------------------------------

def current_values(root: Path, *, now: int, user_rules: list[str] | None = None) -> dict:
    """Today's value of every control, for the before/after log and the weakening test."""
    root = Path(root)
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    roblox_cfg = read_config(root / ROBLOX_CONFIG)
    browser_cfg = read_config(root / BROWSER_CONFIG)
    roblox_state, _ = roblox_guard.load_state(root / ROBLOX_STATE, today)
    browser_state, _ = browser_guard.load_state(root / BROWSER_STATE)
    raw_limit = roblox_cfg.get("limit_min")
    raw_browser_limit = browser_cfg.get("limit_min")
    return {
        # A non-integer here is what the guard treats as a defect, and its fallback is
        # zero - so the panel must report zero too, not the broken value.
        "roblox_limit_min": (raw_limit if isinstance(raw_limit, int)
                             and not isinstance(raw_limit, bool)
                             else roblox_guard.DEFAULT_LIMIT_MIN),
        "roblox_bonus_min": (roblox_state.bonus_sec // 60
                             if roblox_state.date == today else 0),
        "browser_enabled": bool(browser_cfg.get("enabled", True)),
        # None, not 0: "no budget written" and "a budget of nothing" are different
        # answers, and the page says which one it is looking at.
        "browser_limit_min": (raw_browser_limit
                              if isinstance(raw_browser_limit, int)
                              and not isinstance(raw_browser_limit, bool) else None),
        "browser_unlock_min": max(0, (browser_state.unlock_until_ts - now + 59) // 60),
        "browser_notify": bool(browser_cfg.get("notify", False)),
        "roblox_delay_sec": [roblox_cfg.get("delay_min_sec", 1),
                             roblox_cfg.get("delay_max_sec", 5)],
        "browser_delay_sec": [browser_cfg.get("delay_min_sec", 1),
                              browser_cfg.get("delay_max_sec", 5)],
        "youtube_blocked": youtube_state(user_rules),
    }


# --- which changes need the password again -----------------------------------
#
# The panel is on the LAN and the person most interested in it lives here. A session
# cookie on an unlocked phone should not be a one-tap way to hand out four hours of
# Roblox - but making the parent retype a password to TIGHTEN something would just teach
# them to leave things loose. So the split is by direction, not by control:
#
#   weakening (more time, less enforcement, YouTube back on) -> password again
#   tightening (less time, more enforcement, block it now)   -> the session is enough
#
# It is cheap: one predicate, one extra field in the form. It does not pretend to stop
# someone who has the password, and nothing else in the design leans on it.

def _weakens(action: str, new: object, current: dict) -> bool:
    if action == "roblox_limit_min":
        return coerce_int(new, field="minutes") > current.get("roblox_limit_min", 0)
    if action == "roblox_bonus_min":
        return coerce_int(new, field="minutes") > current.get("roblox_bonus_min", 0)
    if action == "browser_limit_min":
        base = current.get("browser_limit_min")
        return coerce_int(new, field="minutes") > (0 if base is None else base)
    if action == "browser_unlock_min":
        return coerce_int(new, field="minutes") > 0
    if action == "browser_enabled":
        return not coerce_bool(new, field="enabled")
    if action == "youtube_blocked":
        return not coerce_bool(new, field="blocked")
    if action in ("roblox_delay_sec", "browser_delay_sec"):
        if not isinstance(new, dict):
            return True
        base = current.get(action) or [1, 5]
        try:
            # The current value comes out of a config file a human may have edited, so
            # it is not necessarily a number. An unreadable current value means the
            # comparison cannot be made, and an unanswerable question here is answered
            # by asking for the password.
            was = int(base[1])
        except (TypeError, ValueError, IndexError):
            return True
        return coerce_int(new.get("max_sec"), field="maximum") > was
    # browser_notify changes what the CHILD is told, not what is enforced, and
    # resolver_start only ever turns filtering back ON.
    return False


def requires_password(action: str, value: object, current: dict) -> bool:
    """True when this change weakens a protection and must be re-authorised.

    Anything unrecognised is treated as weakening: a control added later without being
    classified must fail towards asking, not towards a free pass.
    """
    if action not in ACTIONS:
        return True
    try:
        return _weakens(action, value, current)
    except ControlError:
        # A value that will be refused anyway. Ask, rather than letting a malformed
        # payload skip the check on its way to the error.
        return True
