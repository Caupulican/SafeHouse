"""A weekly allow-schedule: blocked at all times, except inside windows a parent defines.

Pure module - parsing, validation and clock arithmetic only, with **no YouTube in it**
beyond the docstrings: the same engine is meant to drive other lanes later (the browser
guard, Roblox) by pointing a different consumer at a different file. Reading the file,
talking to AdGuard and the once-a-minute reconciler thread live in `collect.py`, which
is the package's one impure module; `controls.py` owns which file belongs to which lane.

The first consumer is `C:\\ProgramData\\SafeHouse\\youtube-schedule.json`:

    {"mode": "blocked" | "allowed" | "schedule",
     "windows": [{"days": [5, 6]},
                 {"days": [3], "start": "14:00", "end": "15:00"}, ...],
     "override": {"state": "ALLOWED", "until_ts": 1787502000} | null}

* `mode` - "blocked" is the standing state (what the old checkbox meant), "allowed"
  lifts the panel's rules entirely, "schedule" blocks everywhere except inside a window.
  **The two manual modes are absolute**: the reconciler enforces only "schedule".
* **Days are the primary unit.** A window with days and no times allows the whole day -
  `{"days": [5, 6]}` is "weekends are open". Times, when given, must come as a pair and
  refine the window.
* `days` use Python's own numbering: **0 = Monday ... 6 = Sunday**, matching
  `datetime.weekday()` so no translation layer can get it backwards.
* Times are the machine's local clock (America/Sao_Paulo on this box), and a window
  whose end is at or before its start **crosses midnight**: `{"days": [4], "start":
  "22:00", "end": "01:00"}` runs Friday 22:00 to Saturday 01:00.
* `windows` are kept whatever the mode, so switching to "blocked" for a week does not
  throw away the windows the parent typed.
* `override` is a **manual hold on top of a schedule** - "Liberar agora" / "Bloquear
  agora" - written by the panel with a server-computed expiry: the next schedule
  boundary (a window edge or the day change), whichever comes first. While it is
  active the reconciler stands down exactly as it does for a manual mode; when it
  expires the schedule resumes by itself.

Failure directions, chosen once: a **missing** file means "no schedule was ever set" and
nothing is enforced (the standing repo rules own the lane, exactly as before this
feature existed). A file that is **present but unreadable** normalises to always-blocked
- the strict default every guard config takes - for what the page *reports* and for the
password-again baseline; the reconciler enforces nothing it could not read.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .i18n import msg

MODES = ("blocked", "allowed", "schedule")
MAX_WINDOWS = 8
SCHEDULE_CONFIG = "youtube-schedule.json"

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# English day abbreviations for panel.log, which stays grep-able English; the page
# renders `day.N` keys from the catalog instead.
_DAYS_EN = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleError(ValueError):
    """A schedule the panel refuses to store. `payload` is an i18n message."""

    def __init__(self, payload: dict) -> None:
        super().__init__(payload)
        self.payload = payload


# --- validation ---------------------------------------------------------------

def _minutes(text: object, *, err_key: str) -> int:
    match = _TIME.match(str(text or ""))
    if not match:
        raise ScheduleError(msg(err_key, got=repr(text)))
    return int(match.group(1)) * 60 + int(match.group(2))


def validate(value: object) -> dict:
    """A normalised `{"mode": ..., "windows": [...]}` or `ScheduleError`.

    Wrong types refuse rather than guess, like every other control: a mode of `True` or
    a window with no days is a bug or an attack, not a preference to be interpreted.
    """
    if not isinstance(value, dict):
        raise ScheduleError(msg("err.sched.shape"))
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise ScheduleError(msg("err.sched.mode", got=repr(mode)))
    raw_windows = value.get("windows", [])
    if not isinstance(raw_windows, list):
        raise ScheduleError(msg("err.sched.shape"))
    if len(raw_windows) > MAX_WINDOWS:
        raise ScheduleError(msg("err.sched.count", max=MAX_WINDOWS))
    windows = []
    for raw in raw_windows:
        if not isinstance(raw, dict):
            raise ScheduleError(msg("err.sched.win"))
        days = raw.get("days")
        if (not isinstance(days, list) or not days
                or not all(isinstance(d, int) and not isinstance(d, bool)
                           and 0 <= d <= 6 for d in days)):
            raise ScheduleError(msg("err.sched.days"))
        window = {"days": sorted(set(days))}
        raw_start, raw_end = raw.get("start") or None, raw.get("end") or None
        if raw_start is None and raw_end is None:
            pass  # no times: the whole day is the window
        elif raw_start is None or raw_end is None:
            raise ScheduleError(msg("err.sched.halftime"))
        else:
            start = _minutes(raw_start, err_key="err.sched.time")
            end = _minutes(raw_end, err_key="err.sched.time")
            if start == end:
                raise ScheduleError(msg("err.sched.same"))
            window.update({"start": str(raw_start), "end": str(raw_end)})
        windows.append(window)
    out = {"mode": mode, "windows": windows}
    override = value.get("override")
    if override is not None:
        if (not isinstance(override, dict)
                or override.get("state") not in ("ALLOWED", "BLOCKED")
                or not isinstance(override.get("until_ts"), int)
                or isinstance(override.get("until_ts"), bool)):
            raise ScheduleError(msg("err.sched.ovr"))
        out["override"] = {"state": override["state"],
                          "until_ts": override["until_ts"]}
    return out


def normalize(obj: object) -> tuple[dict, dict | None]:
    """(config, defect-message-or-None) from whatever was in the file.

    A hand-edited file that no longer validates is a broken policy, and a broken policy
    falls back to the strict default - always blocked - with the defect reported rather
    than the file silently meaning something else.
    """
    try:
        return validate(obj), None
    except ScheduleError as exc:
        return {"mode": "blocked", "windows": []}, msg("sched.defect",
                                                       why=exc.payload)


# --- clock arithmetic ---------------------------------------------------------

def _week_minute(moment: datetime) -> int:
    return moment.weekday() * MINUTES_PER_DAY + moment.hour * 60 + moment.minute


def _window_minutes(window: dict):
    """Yield every week-minute the window covers. Crossing midnight wraps the week."""
    if "start" not in window:  # no times: the whole day
        start, span = 0, MINUTES_PER_DAY
    else:
        start = _minutes(window["start"], err_key="err.sched.time")
        end = _minutes(window["end"], err_key="err.sched.time")
        span = (end - start) % MINUTES_PER_DAY  # end <= start: crosses midnight
    for day in window["days"]:
        base = day * MINUTES_PER_DAY + start
        for offset in range(span):
            yield (base + offset) % MINUTES_PER_WEEK


def allowed_minutes(config: dict) -> set[int]:
    """Every minute of the week (0 = Monday 00:00) this config allows YouTube in."""
    if config["mode"] == "allowed":
        return set(range(MINUTES_PER_WEEK))
    if config["mode"] == "blocked":
        return set()
    allowed: set[int] = set()
    for window in config["windows"]:
        allowed.update(_window_minutes(window))
    return allowed


def desired_state(config: dict, moment: datetime) -> str:
    """BLOCKED or ALLOWED, per the config, at `moment` (machine local time)."""
    if config["mode"] == "allowed":
        return "ALLOWED"
    if config["mode"] == "blocked" or not config["windows"]:
        return "BLOCKED"
    return "ALLOWED" if _week_minute(moment) in allowed_minutes(config) else "BLOCKED"


def weakens(new: dict, old: dict) -> bool:
    """True when `new` allows YouTube at any minute `old` did not.

    This single rule covers every case in the password-again policy: switching to
    "allowed", adding a window, adding a day to one, widening its hours - each one opens
    minutes that were closed. Removing or narrowing anything opens none.
    """
    return bool(allowed_minutes(new) - allowed_minutes(old))


def next_change(config: dict, moment: datetime) -> tuple[str, datetime] | None:
    """The next (state, when) transition, or None when the state never changes.

    Walked minute by minute across one week, which is 10,080 set lookups - cheap, and
    immune to the fencepost mistakes a cleverer interval walk invites around midnight.
    """
    if config["mode"] != "schedule" or not config["windows"]:
        return None
    allowed = allowed_minutes(config)
    if not allowed or len(allowed) == MINUTES_PER_WEEK:
        return None
    base = moment.replace(second=0, microsecond=0)
    now_state = _week_minute(base) in allowed
    for ahead in range(1, MINUTES_PER_WEEK + 1):
        then = base + timedelta(minutes=ahead)
        if (_week_minute(then) in allowed) != now_state:
            return ("BLOCKED" if now_state else "ALLOWED", then)
    return None


def active_window(config: dict, moment: datetime) -> dict | None:
    """The window covering `moment`, if any - for the transition log line."""
    minute = _week_minute(moment)
    for window in config.get("windows", []):
        if minute in set(_window_minutes(window)):
            return window
    return None


def describe_cause(config: dict, moment: datetime) -> str:
    """English, for panel.log: why the schedule wants the state it wants right now."""
    if config["mode"] != "schedule":
        return f"mode {config['mode']}"
    window = active_window(config, moment)
    if window is None:
        return "outside every window"
    days = ",".join(_DAYS_EN[d] for d in window["days"])
    if "start" not in window:
        return f"window {days} all day"
    return f"window {days} {window['start']}-{window['end']}"


# --- the manual override ------------------------------------------------------
#
# A hold a parent places ON TOP of a running schedule - "allow now", "block now" -
# applied on the spot by the control that sets it and honoured here only as a reason to
# do nothing: while it is active the reconciler stands down, exactly as it does for a
# manual mode. It expires at the next schedule boundary (window edge or day change,
# whichever comes first, computed server-side when it is set), or when the parent
# clears it. An expired override is simply inactive; nothing needs to clean it up for
# the schedule to resume.

def override_active(config: dict, now: int) -> dict | None:
    """The override in force at epoch `now`, or None (absent, malformed, expired)."""
    override = config.get("override")
    if not isinstance(override, dict):
        return None
    if override.get("state") not in ("ALLOWED", "BLOCKED"):
        return None
    until = override.get("until_ts")
    if not isinstance(until, int) or isinstance(until, bool) or now >= until:
        return None
    return {"state": override["state"], "until_ts": until}


def override_until(config: dict, moment: datetime) -> datetime:
    """When an override placed at `moment` expires: the next window edge, or the day
    change, whichever comes first. Never further away than tomorrow 00:00."""
    midnight = (moment.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=1))
    change = next_change(config, moment)
    if change is not None and change[1] < midnight:
        return change[1]
    return midnight


def effective_state(config: dict, moment: datetime) -> str:
    """BLOCKED or ALLOWED right now: an active override wins, then the schedule."""
    active = override_active(config, int(moment.timestamp()))
    if active is not None:
        return active["state"]
    return desired_state(config, moment)


# --- what the page shows ------------------------------------------------------

def view(config: dict | None, defect: dict | None, *, now: int) -> dict:
    """The snapshot's schedule block: mode, windows, effective state now, next change,
    and any manual override in force."""
    if config is None:
        return {"configured": False, "mode": None, "windows": [], "desired": None,
                "next": None, "override": None, "defect": defect}
    moment = datetime.fromtimestamp(now)
    active = override_active(config, now)
    desired = active["state"] if active else desired_state(config, moment)
    upcoming = next_change(config, moment)
    nxt = None
    if upcoming is not None:
        state, when = upcoming
        nxt = msg("sched.next.allow" if state == "ALLOWED" else "sched.next.block",
                  day=msg(f"day.{when.weekday()}"), t=when.strftime("%H:%M"))
    override_view = None
    if active is not None:
        until = datetime.fromtimestamp(active["until_ts"]).strftime("%H:%M")
        override_view = {
            "state": active["state"], "until_ts": active["until_ts"],
            "note": msg("sched.ovr.allowed" if active["state"] == "ALLOWED"
                        else "sched.ovr.blocked", t=until),
        }
    return {"configured": True, "mode": config["mode"], "windows": config["windows"],
            "desired": desired, "next": nxt, "override": override_view,
            "defect": defect}
