"""Configuration for the YouTube watch-time budget.

Two files, one direction:

    .env  --(install)-->  config.json  --(read every tick)-->  the watcher

`.env` is the human authoring surface. `config.json` under ProgramData is what the
watcher actually reads, and it is the SINGLE source of truth for the daily limit --
unlike the PowerShell version, where `limit_min` was mirrored into the state file and
promptly went stale, so editing the config silently changed nothing.

Nothing here does I/O beyond reading those two files, and `parse_env` / `merge` are pure
so they can be tested without a filesystem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Validated tunables. Frozen: a tick must not be able to mutate its own config."""

    # Daily cumulative budget in minutes. 0 is legitimate and means "never allowed"
    # (`ctl set-limit 0` relies on it), which is why this one permits zero.
    limit_min: int = 180
    # Seconds between samples; also the granularity budget is credited in.
    sample_sec: int = 20
    # Rolling window the throughput average is taken over.
    window_sec: int = 240
    # Sustained inbound kbps from pinned IPs that counts as watching.
    min_throughput_kbps: int = 64
    # Drop a pinned IP after this long with no traffic.
    pin_idle_expire_min: int = 30

    @property
    def limit_sec(self) -> int:
        return self.limit_min * 60


# .env key -> Config field. Also the allow-list: an unknown key is a typo, not an
# extension point, and is reported rather than silently ignored.
ENV_MAP: dict[str, str] = {
    "YB_LIMIT_MIN": "limit_min",
    "YB_SAMPLE_SEC": "sample_sec",
    "YB_WINDOW_SEC": "window_sec",
    "YB_MIN_THROUGHPUT_KBPS": "min_throughput_kbps",
    "YB_PIN_IDLE_EXPIRE_MIN": "pin_idle_expire_min",
}

# Minimum accepted value per field. Everything is an integer count, so a negative is
# always a mistake. Only `limit_min` may be zero -- for the others zero is degenerate
# (a 0s sample interval would spin the loop; a 0-minute pin expiry would drop every pin
# before it could be metered).
MINIMUM: dict[str, int] = {
    "limit_min": 0,
    "sample_sec": 1,
    "window_sec": 1,
    "min_throughput_kbps": 0,
    "pin_idle_expire_min": 1,
}


def parse_env(text: str) -> tuple[dict[str, int], list[str]]:
    """Parse `.env` text into {config field: value} plus a list of human warnings.

    Deliberately strict and non-fatal: a bad line is reported and dropped, never
    applied and never raised. A typo in this file must not be able to hand out
    unlimited YouTube, and must not stop the watcher from starting either.
    """
    values: dict[str, int] = {}
    warnings: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        key, sep, val = line.partition("=")
        if not sep:
            warnings.append(f"line {lineno}: not KEY=VALUE, ignored: {line}")
            continue

        key = key.strip()
        val = val.strip().strip("\"'")

        field = ENV_MAP.get(key)
        if field is None:
            warnings.append(f"line {lineno}: unknown key {key!r}, ignored")
            continue

        try:
            num = int(val)
        except ValueError:
            warnings.append(f"line {lineno}: {key} must be an integer (got {val!r}), ignored")
            continue

        low = MINIMUM[field]
        if num < low:
            warnings.append(f"line {lineno}: {key} must be >= {low} (got {num}), ignored")
            continue

        values[field] = num

    return values, warnings


def merge(base: Config, values: dict[str, int]) -> Config:
    """Overlay validated values onto `base`. Unknown fields are already filtered out."""
    return replace(base, **values)


def from_json(text: str) -> tuple[dict[str, int], list[str]]:
    """Parse a config.json body, applying the same validation as `.env`.

    Unknown keys are preserved by the installer but ignored here; a corrupt file
    yields a warning and no values, so the watcher falls back to defaults rather
    than refusing to run. Enforcement staying up matters more than config fidelity.
    """
    warnings: list[str] = []
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        return {}, [f"config.json is not valid JSON ({exc}); using defaults"]

    if not isinstance(obj, dict):
        return {}, ["config.json is not a JSON object; using defaults"]

    values: dict[str, int] = {}
    for field in Config.__dataclass_fields__:
        if field not in obj:
            continue
        raw = obj[field]
        # bool is an int subclass in Python; accepting True as 1 would be nonsense here.
        if isinstance(raw, bool) or not isinstance(raw, int):
            warnings.append(f"config.json: {field} must be an integer (got {raw!r}), ignored")
            continue
        if raw < MINIMUM[field]:
            warnings.append(f"config.json: {field} must be >= {MINIMUM[field]} (got {raw}), ignored")
            continue
        values[field] = raw

    return values, warnings


def load(config_path: Path) -> tuple[Config, list[str]]:
    """Read config.json (the live source of truth). Missing file -> defaults."""
    if not config_path.exists():
        return Config(), [f"{config_path} not found; using defaults"]
    try:
        text = config_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return Config(), [f"could not read {config_path} ({exc}); using defaults"]

    values, warnings = from_json(text)
    return merge(Config(), values), warnings


def render(config_path: Path, env_path: Path | None) -> tuple[Config, list[str]]:
    """Installer path: defaults <- existing config.json <- .env, last wins.

    Layering the existing config underneath `.env` is what lets a key absent from
    `.env` keep its current live value, so re-running the installer cannot silently
    revert something changed through the control CLI.
    """
    cfg, warnings = load(config_path)

    if env_path is None or not env_path.exists():
        if env_path is not None:
            warnings.append(f"{env_path} not found; using defaults/live config")
        return cfg, warnings

    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        warnings.append(f"could not read {env_path} ({exc}); using defaults/live config")
        return cfg, warnings

    values, env_warnings = parse_env(text)
    return merge(cfg, values), warnings + [f"{env_path}: {w}" for w in env_warnings]


def to_json(cfg: Config) -> str:
    """Serialise for config.json. Stable key order so diffs stay readable."""
    return json.dumps(
        {f: getattr(cfg, f) for f in Config.__dataclass_fields__},
        indent=2,
    )
