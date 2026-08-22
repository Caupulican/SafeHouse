"""A ceiling on the master output volume - the one RESIDENT enforcement lane.

The rule is small: the volume slider may sit anywhere from 0 to 60%, but must never STAY
above 60%. An 8-year-old with headphones will drag it to 100 and leave it there; this
loop reads the master level once a second and, when it is over the ceiling, snaps it
back. Within about a second of the drag, the level is legal again.

Every other lane here is a one-shot fired by the scheduler, and the parent's bargain -
an idle run touches nothing - is why. This lane is deliberately the exception, because
the one-shot shape cannot express it: a task firing every minute leaves up to sixty
seconds of full volume after every drag, which is the entire harm the rule exists to
prevent. Being resident is defensible here where it was not for the kill lanes:

* it runs inside the child's own session AS the child (no SYSTEM process watching him;
  install.py registers it under the interactive user, like the resolver watchdog);
* it kills nothing and reads nothing but one number - the quiet tick is two in-process
  COM calls, ~0% CPU, no subprocess ever;
* C:\\SafeHouse is already excluded in Defender, so a resident Python is not new AV bait.

The Windows side is Core Audio (IAudioEndpointVolume) over RAW ctypes COM - the vendored
runtime is stdlib-only, so no comtypes, no pywin32. Vtable slots are called through a
tiny WINFUNCTYPE cast; ctypes' HRESULT restype raises OSError on any failing call, and
EVERY OSError lands on the same recovery: release what is held, sleep a few seconds,
re-acquire from scratch. A device unplugged, the audio service restarting, or no endpoint
existing yet at logon must degrade to a pause in enforcement, never a crash - the task's
15-minute repetition would eventually resurrect a dead loop, and fifteen minutes at full
volume is the one-shot problem all over again.

The config (`volume-guard.json`) is re-read every few seconds inside the loop: it is one
small file read, and it means the parent can change the ceiling - or stand the lane down
with `enabled: false` - without touching the scheduled task. Both escape hatches are
config values (`enabled: false`, `max_percent: 100`); there is no state file, because
nothing here has to outlive a tick. Any defect in the file means enforcing the DEFAULT
60% ceiling, never "no cap" - same fail-closed discipline as every other lane.

Honest limits, so nobody oversells this: it caps the MASTER endpoint volume only, which
is the one knob the child actually uses. Per-application channel volumes in the mixer are
untouched (they are fractions OF the master, so the ceiling still bounds what reaches the
headphones). And the child has local admin by his parent's own choice - he can end this
process or its task. That is a deliberate act against a named control, which is a
conversation, not a patch.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Borrowed, not copied: same append-only log contract as every other lane.
from .roblox_guard import log

DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")
DEFAULT_MAX_PERCENT = 60

# One read per second is the promise "snaps back within about a second", and two COM
# calls a second rounds to zero CPU.
POLL_SEC = 1.0
# How often the loop re-reads volume-guard.json, so a parent's edit takes effect without
# restarting the task.
CONFIG_EVERY_SEC = 5.0
# How often the default endpoint is re-acquired even while healthy. The default device
# CHANGES when headphones are plugged in - a new endpoint sitting at whatever level it
# last had, often 100% - and a loop polling the old interface would never see it. Five
# seconds bounds how long a fresh device can stay over the ceiling.
REACQUIRE_EVERY_SEC = 5.0
# The back-off after a COM failure, before trying to acquire again.
RETRY_SEC = 3.0

# The scalar is a float32 inside Windows: set 0.60, read back 0.5999-something. Anything
# within this band of the cap reads as AT the cap, so the loop does not re-set the level
# every tick forever. 0.004 is under half a percent - below what the volume UI can even
# express - and far below any real drag.
EPSILON = 0.004

# A slider drag fires dozens of adjustments in a couple of seconds. One clamp episode is
# one log line, not one per tick; further clamps inside this window stay silent.
LOG_SUPPRESS_SEC = 30

# Core Audio identities and vtable geometry, from mmdeviceapi.h / endpointvolume.h.
CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
CLSCTX_ALL = 0x17
E_RENDER = 0        # EDataFlow eRender - output, not microphones
E_MULTIMEDIA = 1    # ERole eMultimedia - the device games and videos play through


@dataclass(frozen=True)
class VolumeConfig:
    enabled: bool = True
    max_percent: int = DEFAULT_MAX_PERCENT


def clamp_target(current: float, cap: float) -> float | None:
    """Pure: what the master level should be SET to, or None to leave it alone.

    `cap` is a 0..1 scalar. A cap of 1.0 disables the clamp entirely - that is the
    parent's `max_percent: 100` escape hatch, and with the ceiling at the top there is
    nothing to enforce, whatever the reading says.

    A reading outside [0, 1] (or NaN/inf) can only come from a broken driver, and a
    broken reading is treated the fail-closed way: snap to the cap, a known-legal level,
    rather than trusting garbage. The NaN case matters - `nan > cap` is False, so a
    naive comparison would quietly fail OPEN on the one input designed to slip past it.
    """
    if cap >= 1.0:
        return None
    if not math.isfinite(current) or not 0.0 <= current <= 1.0:
        return cap
    return cap if current > cap + EPSILON else None


# --- config ------------------------------------------------------------------

def load_config(path: Path) -> tuple[VolumeConfig, list[str]]:
    """Read volume-guard.json. Every defect falls back to ENFORCING the default ceiling.

    `enabled: false` and `max_percent: 100` are the two escape hatches, honoured only as
    the deliberate JSON values. A truncated file, a string where a number belongs or a
    percentage outside 1..100 all mean "enforce 60", never "no cap": the failure mode of
    a config typo has to be a quieter machine, not a louder one.
    """
    if not path.exists():
        return VolumeConfig(), []
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return VolumeConfig(), [
            f"bad {path.name} ({type(exc).__name__}: {exc}) - "
            f"enforcing the {DEFAULT_MAX_PERCENT}% ceiling"
        ]

    warnings: list[str] = []

    enabled = obj.get("enabled", True)
    if not isinstance(enabled, bool):
        warnings.append(f"enabled must be true or false (got {enabled!r}), enforcing")
        enabled = True

    raw = obj.get("max_percent", DEFAULT_MAX_PERCENT)
    # bool is an int subclass in Python, and `"max_percent": true` is a typo, not a 1.
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 100:
        warnings.append(f"max_percent must be a whole number 1..100 (got {raw!r}), "
                        f"enforcing {DEFAULT_MAX_PERCENT}%")
        raw = DEFAULT_MAX_PERCENT

    return VolumeConfig(enabled=enabled, max_percent=raw), warnings


# --- the Windows side: Core Audio over raw ctypes COM ------------------------

def _guid(text: str):
    """Parse "{...}" into the 16 binary bytes ole32 takes by reference. Windows only."""
    import ctypes

    guid = (ctypes.c_ubyte * 16)()
    # oledll checks the HRESULT itself and raises OSError on a malformed string.
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), guid)
    return guid


class MasterVolume:
    """The default render endpoint's IAudioEndpointVolume, held open between polls.

    Construct only on a win32 code path: everything here is ctypes COM, kept inside the
    class so the module imports cleanly on Linux, where the tests run. Any COM failure
    surfaces as OSError - WINFUNCTYPE with an HRESULT restype makes ctypes raise it on
    every failing call, which is the whole error-checking story - and the caller's answer
    to OSError is always `close()` and re-acquire.
    """

    def __init__(self):
        import ctypes

        self._ctypes = ctypes
        self._vol = None
        # Per-thread and refcounted; S_FALSE (already initialised) is a success, so
        # re-acquiring on the same thread is fine.
        ctypes.oledll.ole32.CoInitialize(None)

        enumerator = ctypes.c_void_p()
        ctypes.oledll.ole32.CoCreateInstance(
            _guid(CLSID_MMDEVICE_ENUMERATOR), None, CLSCTX_ALL,
            _guid(IID_IMMDEVICE_ENUMERATOR), ctypes.byref(enumerator))

        device = ctypes.c_void_p()
        try:
            # IMMDeviceEnumerator vtable slot 4: GetDefaultAudioEndpoint(flow, role, **out)
            self._method(enumerator, 4, ctypes.c_int, ctypes.c_int,
                         ctypes.POINTER(ctypes.c_void_p))(
                enumerator, E_RENDER, E_MULTIMEDIA, ctypes.byref(device))
        finally:
            self._release(enumerator)

        vol = ctypes.c_void_p()
        try:
            # IMMDevice vtable slot 3: Activate(REFIID, CLSCTX, PROPVARIANT*, **out)
            self._method(device, 3, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p))(
                device, _guid(IID_IAUDIO_ENDPOINT_VOLUME), CLSCTX_ALL, None,
                ctypes.byref(vol))
        finally:
            self._release(device)
        self._vol = vol

    def _method(self, iface, slot: int, *argtypes):
        """Bind vtable slot `slot` of `iface` as a callable - the COM framework in five
        lines. The first pointer in any COM object is its vtable; the slot is a bare
        function pointer whose first argument is the interface itself."""
        ctypes = self._ctypes
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        vtable = ctypes.cast(
            iface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return proto(vtable[slot])

    def _release(self, iface) -> None:
        """IUnknown vtable slot 2: Release. Its return is a refcount, not an HRESULT, so
        it gets its own prototype - and releasing must never raise, because it runs on
        the recovery path."""
        if not iface:
            return
        try:
            ctypes = self._ctypes
            proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
            vtable = ctypes.cast(
                iface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            proto(vtable[2])(iface)
        except OSError:
            pass

    def get(self) -> float:
        """The master level as a 0..1 scalar."""
        ctypes = self._ctypes
        value = ctypes.c_float()
        # IAudioEndpointVolume vtable slot 9: GetMasterVolumeLevelScalar(float*)
        self._method(self._vol, 9, ctypes.POINTER(ctypes.c_float))(
            self._vol, ctypes.byref(value))
        return float(value.value)

    def set(self, level: float) -> None:
        ctypes = self._ctypes
        # IAudioEndpointVolume vtable slot 7: SetMasterVolumeLevelScalar(float, LPCGUID).
        # The GUID is the event context handed to change listeners; None claims no
        # originator, so the on-screen volume flyout still reflects the snap - the level
        # moving is visible either way, and hiding the cause is not this lane's job.
        self._method(self._vol, 7, ctypes.c_float, ctypes.c_void_p)(
            self._vol, ctypes.c_float(level), None)

    def close(self) -> None:
        self._release(self._vol)
        self._vol = None


# --- the loop ----------------------------------------------------------------

def run(root: Path) -> int:
    """The resident loop. Returns only when launched somewhere it cannot enforce."""
    if sys.platform != "win32":
        print("volume_guard enforces the Windows master volume; nothing to do here.")
        return 1

    config_path = root / "volume-guard.json"
    log_path = root / "volume-guard.log"

    endpoint: MasterVolume | None = None
    acquired = float("-inf")
    cfg, warnings = VolumeConfig(), []
    read_ts = float("-inf")
    last_clamp_log = float("-inf")
    logged_warnings: list[str] | None = None
    failing = False

    while True:
        now = time.monotonic()

        if now - read_ts >= CONFIG_EVERY_SEC:
            cfg, warnings = load_config(config_path)
            read_ts = now

        if not cfg.enabled:
            # The deliberate parent stand-down. Hold nothing open while standing down,
            # so a disabled guard is a sleeping process and not a COM tenant.
            if endpoint is not None:
                endpoint.close()
                endpoint = None
            time.sleep(POLL_SEC)
            continue

        try:
            if endpoint is None or now - acquired >= REACQUIRE_EVERY_SEC:
                if endpoint is not None:
                    endpoint.close()
                endpoint = None
                endpoint = MasterVolume()
                acquired = now
                if failing:
                    failing = False
                    log(log_path, "[ok] audio endpoint re-acquired")

            current = endpoint.get()
            target = clamp_target(current, cfg.max_percent / 100.0)
            if target is not None:
                endpoint.set(target)
                # One line per episode, not one per tick: a drag to 100% is dozens of
                # over-cap readings in a row, and each one is the same news.
                if now - last_clamp_log >= LOG_SUPPRESS_SEC:
                    last_clamp_log = now
                    # Config warnings ride along with a tick that ACTS, like the kill
                    # lanes' - a broken config on a machine that never goes loud says
                    # nothing - and only when they changed since last said.
                    if warnings != logged_warnings:
                        for w in warnings:
                            log(log_path, f"[warn] {w}")
                        logged_warnings = list(warnings)
                    log(log_path, f"[clamp] {current:.0%} -> {target:.0%} "
                                  f"(ceiling {cfg.max_percent}%)")
        except OSError as exc:
            # Device unplugged, audio service restarting, no endpoint yet at logon: all
            # land here. Drop everything, breathe, and rebuild from scratch next pass -
            # the one thing this loop must never do is crash out.
            if endpoint is not None:
                endpoint.close()
                endpoint = None
            if not failing:
                failing = True  # first failure of the episode is said once, then quiet
                log(log_path, f"[warn] audio endpoint unavailable ({exc}) - "
                              f"retrying every {RETRY_SEC:.0f}s")
            time.sleep(RETRY_SEC)
            continue

        time.sleep(POLL_SEC)


# --- one-shot entry points ---------------------------------------------------

def status(root: Path) -> int:
    cfg, warnings = load_config(root / "volume-guard.json")
    print(f"Volume guard - "
          f"{'ENFORCING' if cfg.enabled else 'DISABLED in volume-guard.json'}")
    print(f"  Ceiling    : {cfg.max_percent}%"
          + (" (100% = no clamp)" if cfg.max_percent >= 100 else ""))
    print(f"  Checks     : every {POLL_SEC:g}s, resident in the user's session")
    for w in warnings:
        print(f"  [warn] {w}")
    if sys.platform != "win32":
        print("  Volume     : (reading the live level needs Windows)")
        return 0
    try:
        vol = MasterVolume()
        try:
            print(f"  Volume     : {vol.get():.0%} right now")
        finally:
            vol.close()
    except OSError as exc:
        print(f"  Volume     : unavailable ({exc})")
    return 0


def once(root: Path) -> int:
    """One observe-and-clamp pass - the live-verification entry point."""
    if sys.platform != "win32":
        print("volume_guard --once needs Windows: it talks to Core Audio.")
        return 1
    cfg, warnings = load_config(root / "volume-guard.json")
    for w in warnings:
        print(f"[warn] {w}")
    if not cfg.enabled:
        print("disabled in volume-guard.json - leaving the volume alone")
        return 0
    vol = MasterVolume()
    try:
        current = vol.get()
        target = clamp_target(current, cfg.max_percent / 100.0)
        if target is None:
            print(f"volume {current:.0%} is within the {cfg.max_percent}% ceiling - "
                  f"left alone")
        else:
            vol.set(target)
            log(root / "volume-guard.log",
                f"[clamp] {current:.0%} -> {target:.0%} "
                f"(ceiling {cfg.max_percent}%, --once)")
            print(f"volume {current:.0%} -> clamped to {target:.0%}")
    finally:
        vol.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safehouse-volume-guard", description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--status", action="store_true",
                   help="print the ceiling and the current volume, then exit")
    p.add_argument("--once", action="store_true",
                   help="a single observe-and-clamp pass (for live verification)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.status:
        return status(args.root)
    if args.once:
        return once(args.root)
    return run(args.root)


if __name__ == "__main__":
    sys.exit(main())
