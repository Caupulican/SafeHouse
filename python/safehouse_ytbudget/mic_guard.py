"""Every microphone on the machine, muted - the second RESIDENT enforcement lane.

The rule has no dial: a capture endpoint that is ACTIVE is muted and its level is zero,
always. Not the default microphone - EVERY one of them. A guard that minded only the
default endpoint would be worth nothing here, because switching input is a two-click
affair and this machine has at least two: the `Steam Streaming Microphone` (a virtual
device that exists whether or not a headset is plugged in) and the Bluetooth
`BT Headset Hands-Free` capture endpoint, which appears the moment the
headset pairs. Voice chat stops working. That is the intended outcome, chosen by the
parent, not a side effect to be apologised for.

Shape, and why it is resident rather than a one-shot like the kill lanes: the same
argument that made the volume ceiling resident, only sharper. A task firing every minute
leaves up to sixty seconds of live microphone after every unmute, and sixty seconds of
open mic to strangers is the entire harm the lane exists to prevent. As with the volume
ceiling, being resident is defensible because of what the tick costs and who owns it:

* it runs inside the child's own session AS the child (install.py registers it under the
  interactive user, like the volume guard and the resolver watchdog);
* it kills nothing and spawns nothing - a quiet tick is a handful of in-process COM
  reads and no writes at all, because an already-muted endpoint is left alone;
* C:\\SafeHouse is already excluded in Defender, so a resident Python is not new AV bait.

The Windows side is Core Audio over RAW ctypes COM - the vendored runtime is stdlib-only,
so no comtypes, no pywin32. `IMMDeviceEnumerator::EnumAudioEndpoints(eCapture,
DEVICE_STATE_ACTIVE)` is the whole discovery story, and the collection is rebuilt from
scratch every few seconds (REACQUIRE_EVERY_SEC) rather than held: a headset paired at
10:04 is a capture endpoint that did not exist at 10:03, and a loop holding yesterday's
list would never see it. That bounds how long a freshly appeared microphone can be live.

Enforcement is two calls per endpoint, deliberately: `SetMute(TRUE)` **and**
`SetMasterVolumeLevelScalar(0.0)`. Mute alone is one click in the tray to undo and the
level underneath it is whatever it was; zero alone can be dragged back up. Together, a
child who finds one of them still has a silent microphone, and this loop puts both back
within about a second either way.

The config (`mic-guard.json`) is re-read every few seconds inside the loop, so the parent
can stand the lane down - `{"enabled": false}` - without touching the scheduled task.
That is the ONLY escape hatch: there is no per-device allow list, because the parent's
instruction was everything, always, and a list of exceptions is a list of ways to be
wrong about which device is which. Every defect in the file - missing, truncated, a
string where a bool belongs - means ENFORCE, the same fail-closed discipline as every
other lane: a config typo must leave the microphone off, never on.

Honest limits, so nobody oversells this. It mutes ACTIVE capture ENDPOINTS: what Windows
itself exposes as a microphone. An application that talks to hardware below the endpoint
layer (its own kernel driver, a USB capture device read directly) is not covered, and
neither is a phone in the same room. Per-application capture streams in the mixer are
untouched - they are fractions of an endpoint that is muted at zero, so nothing reaches
them, but the mixer will still show them. And the child has local admin by his parent's
own choice: he can end this process or its task. That is a deliberate act against a named
control, which is a conversation, not a patch.
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

# One pass per second is the promise "muted again within about a second". A pass over two
# or three endpoints that are already muted is a few in-process COM reads: no writes, no
# subprocess, ~0% CPU.
POLL_SEC = 1.0
# How often the loop re-reads mic-guard.json, so a parent's edit takes effect without
# restarting the task.
CONFIG_EVERY_SEC = 5.0
# How often the endpoint COLLECTION is rebuilt from scratch, even while healthy. A
# Bluetooth headset pairing creates a capture endpoint that did not exist a moment ago,
# usually unmuted; five seconds bounds how long it can stay live.
REACQUIRE_EVERY_SEC = 5.0
# The back-off after a COM failure, before enumerating again.
RETRY_SEC = 3.0

# The scalar is a float32 inside Windows: set 0.0, read back 0.0 or a hair above on some
# drivers. Anything within this band of zero reads as AT zero, so the loop does not
# re-write the level every tick forever. Far below anything a level meter can show.
EPSILON = 0.004

# Unmuting is a click, and a child testing the lane will click it repeatedly. One
# silencing episode per device is one log line; further silencings of the same device
# inside this window stay silent.
LOG_SUPPRESS_SEC = 30

# Core Audio identities and vtable geometry, from mmdeviceapi.h / endpointvolume.h,
# re-verified against the live machine before this lane shipped.
CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
# PKEY_Device_FriendlyName - the name the sound control panel shows, which is the only
# name a parent reading the log will recognise.
FMTID_DEVICE_FRIENDLY_NAME = "{A45C254E-DF1C-4EFD-8020-67D146A850E0}"
PID_DEVICE_FRIENDLY_NAME = 14

CLSCTX_ALL = 0x17
E_CAPTURE = 1            # EDataFlow eCapture - microphones, not speakers
DEVICE_STATE_ACTIVE = 1  # plugged in and available; unplugged/disabled ones cannot record
STGM_READ = 0
VT_LPWSTR = 31


@dataclass(frozen=True)
class MicConfig:
    enabled: bool = True


def needs_silencing(muted, level) -> bool:
    """Pure: must this endpoint be written to, given what was read back from it?

    False - leave it alone - only for the one unambiguous case: it says it is muted AND
    its level is at zero. Everything else is a write, including every unreadable answer.

    The fail-closed cases are the point. `None` (the driver refused the read), NaN and
    inf (a broken driver), a level outside 0..1: all mean silence it. NaN matters
    specifically - `nan > EPSILON` is False, so a naive comparison would fail OPEN on a
    live microphone, which is the one direction this lane must never fail.
    """
    if muted is not True:
        return True
    if level is None:
        return True
    if not math.isfinite(level) or not 0.0 <= level <= 1.0:
        return True
    return level > EPSILON


# --- config ------------------------------------------------------------------

def load_config(path: Path) -> tuple[MicConfig, list[str]]:
    """Read mic-guard.json. Every defect falls back to ENFORCING.

    `{"enabled": false}` is the single escape hatch, honoured only as that deliberate
    JSON value. An absent file, a truncated one, a list where an object belongs, or a
    string where the bool belongs all mean "mute everything": the failure mode of a
    config typo has to be a quiet microphone, not a hot one.
    """
    if not path.exists():
        return MicConfig(), []
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return MicConfig(), [
            f"bad {path.name} ({type(exc).__name__}: {exc}) - muting every microphone"
        ]

    warnings: list[str] = []
    enabled = obj.get("enabled", True)
    if not isinstance(enabled, bool):
        warnings.append(f"enabled must be true or false (got {enabled!r}), enforcing")
        enabled = True
    return MicConfig(enabled=enabled), warnings


# --- the Windows side: Core Audio over raw ctypes COM ------------------------

def _guid(text: str):
    """Parse "{...}" into the 16 binary bytes ole32 takes by reference. Windows only."""
    import ctypes

    guid = (ctypes.c_ubyte * 16)()
    # oledll checks the HRESULT itself and raises OSError on a malformed string.
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), guid)
    return guid


def _method(iface, slot: int, *argtypes):
    """Bind vtable slot `slot` of `iface` as a callable - the COM framework in five
    lines. The first pointer in any COM object is its vtable; the slot is a bare function
    pointer whose first argument is the interface itself. The HRESULT restype is the
    entire error-checking story: ctypes raises OSError on any failing call."""
    import ctypes

    proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    vtable = ctypes.cast(
        iface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return proto(vtable[slot])


def _release(iface) -> None:
    """IUnknown vtable slot 2: Release. Its return is a refcount, not an HRESULT, so it
    gets its own prototype - and releasing must never raise, because it runs on the
    recovery path."""
    if not iface:
        return
    try:
        import ctypes

        proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        vtable = ctypes.cast(
            iface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        proto(vtable[2])(iface)
    except OSError:
        pass


def _friendly_name(device, fallback: str) -> str:
    """PKEY_Device_FriendlyName off the device's property store, or `fallback`.

    A missing name is cosmetic - it only changes what the log line calls the device - so
    every failure here is swallowed rather than allowed to abort a mute.
    """
    import ctypes

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", ctypes.c_ulong)]

    class PROPVARIANT(ctypes.Structure):
        # vt + three reserved words, then the union. 24 bytes on x64, which is what
        # IPropertyStore::GetValue writes into.
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                    ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                    ("data", ctypes.c_void_p), ("data2", ctypes.c_void_p)]

    store = ctypes.c_void_p()
    try:
        # IMMDevice vtable slot 4: OpenPropertyStore(STGM, IPropertyStore**)
        _method(device, 4, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(
            device, STGM_READ, ctypes.byref(store))
    except OSError:
        return fallback

    key = PROPERTYKEY()
    key.fmtid = _guid(FMTID_DEVICE_FRIENDLY_NAME)
    key.pid = PID_DEVICE_FRIENDLY_NAME
    value = PROPVARIANT()
    try:
        # IPropertyStore vtable slot 5: GetValue(REFPROPERTYKEY, PROPVARIANT*)
        _method(store, 5, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
            store, ctypes.byref(key), ctypes.byref(value))
        if value.vt != VT_LPWSTR or not value.data:
            return fallback
        return ctypes.cast(value.data, ctypes.c_wchar_p).value or fallback
    except OSError:
        return fallback
    finally:
        try:
            ctypes.oledll.ole32.PropVariantClear(ctypes.byref(value))
        except OSError:
            pass
        _release(store)


class CaptureEndpoint:
    """One ACTIVE capture endpoint's IAudioEndpointVolume, held open for one pass.

    Construct only via `open_capture_endpoints`, and only on a win32 code path:
    everything here is ctypes COM, kept out of module import so the module loads cleanly
    on Linux, where the tests run. Any COM failure surfaces as OSError, and the caller's
    answer to OSError is always to close everything and re-enumerate.
    """

    def __init__(self, vol, key: str, name: str):
        self._vol = vol
        self.key = key
        self.name = name

    def state(self) -> tuple[bool | None, float | None]:
        """(is it muted?, its level as a 0..1 scalar) - what the decision is made on."""
        import ctypes

        muted = ctypes.c_int()
        # IAudioEndpointVolume vtable slot 15: GetMute(BOOL*)
        _method(self._vol, 15, ctypes.POINTER(ctypes.c_int))(
            self._vol, ctypes.byref(muted))
        level = ctypes.c_float()
        # IAudioEndpointVolume vtable slot 9: GetMasterVolumeLevelScalar(float*)
        _method(self._vol, 9, ctypes.POINTER(ctypes.c_float))(
            self._vol, ctypes.byref(level))
        return bool(muted.value), float(level.value)

    def silence(self) -> None:
        """Mute it AND drop it to zero - belt and braces, on purpose.

        Mute alone is one click in the tray to undo, with whatever level was underneath
        it still there; zero alone can be dragged back up. Doing both means finding one
        of them is not enough to be heard.
        """
        import ctypes

        # IAudioEndpointVolume vtable slot 14: SetMute(BOOL, LPCGUID). The GUID is the
        # event context handed to change listeners; None claims no originator, so the
        # sound panel reflects the change - hiding the cause is not this lane's job.
        _method(self._vol, 14, ctypes.c_int, ctypes.c_void_p)(self._vol, 1, None)
        # IAudioEndpointVolume vtable slot 7: SetMasterVolumeLevelScalar(float, LPCGUID)
        _method(self._vol, 7, ctypes.c_float, ctypes.c_void_p)(
            self._vol, ctypes.c_float(0.0), None)

    def close(self) -> None:
        _release(self._vol)
        self._vol = None


def open_capture_endpoints() -> list[CaptureEndpoint]:
    """Every ACTIVE capture endpoint on the machine, as live interfaces.

    Built from scratch on every call rather than cached: that is how a headset paired
    thirty seconds ago gets muted. A failure part-way through releases what was already
    opened before the OSError leaves here, so a half-built list is never handed back.
    """
    import ctypes

    # Per-thread and refcounted; S_FALSE (already initialised) is a success, so calling
    # this on every enumeration from the same thread is fine.
    ctypes.oledll.ole32.CoInitialize(None)

    enumerator = ctypes.c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(
        _guid(CLSID_MMDEVICE_ENUMERATOR), None, CLSCTX_ALL,
        _guid(IID_IMMDEVICE_ENUMERATOR), ctypes.byref(enumerator))

    endpoints: list[CaptureEndpoint] = []
    collection = ctypes.c_void_p()
    try:
        # IMMDeviceEnumerator vtable slot 3:
        #   EnumAudioEndpoints(EDataFlow, DWORD stateMask, IMMDeviceCollection**)
        _method(enumerator, 3, ctypes.c_int, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p))(
            enumerator, E_CAPTURE, DEVICE_STATE_ACTIVE, ctypes.byref(collection))
        count = ctypes.c_uint()
        # IMMDeviceCollection vtable slot 3: GetCount(UINT*)
        _method(collection, 3, ctypes.POINTER(ctypes.c_uint))(
            collection, ctypes.byref(count))

        for index in range(int(count.value)):
            device = ctypes.c_void_p()
            # IMMDeviceCollection vtable slot 4: Item(UINT, IMMDevice**)
            _method(collection, 4, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                collection, index, ctypes.byref(device))
            try:
                key = f"#{index}"
                raw_id = ctypes.c_wchar_p()
                try:
                    # IMMDevice vtable slot 5: GetId(LPWSTR*) - the stable identity the
                    # log-suppression window is keyed on, so two devices unmuted at once
                    # are two episodes rather than one shouting over the other.
                    _method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))(
                        device, ctypes.byref(raw_id))
                    if raw_id.value:
                        key = raw_id.value
                    ctypes.windll.ole32.CoTaskMemFree(raw_id)
                except OSError:
                    pass
                name = _friendly_name(device, key)

                vol = ctypes.c_void_p()
                # IMMDevice vtable slot 3: Activate(REFIID, CLSCTX, PROPVARIANT*, **out)
                _method(device, 3, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p))(
                    device, _guid(IID_IAUDIO_ENDPOINT_VOLUME), CLSCTX_ALL, None,
                    ctypes.byref(vol))
            finally:
                _release(device)
            endpoints.append(CaptureEndpoint(vol, key, name))
    except OSError:
        close_endpoints(endpoints)
        raise
    finally:
        _release(collection)
        _release(enumerator)
    return endpoints


def close_endpoints(endpoints) -> None:
    """Release a whole list, tolerating anything - it runs on the recovery path."""
    for endpoint in endpoints:
        try:
            endpoint.close()
        except OSError:
            pass


# --- one enforcement pass -----------------------------------------------------

def enforce_pass(endpoints, log_path: Path, now: float, last_log: dict) -> int:
    """Silence every endpoint that is not already silent. Returns how many were written.

    `last_log` is the per-device suppression state, mutated in place: a device silenced
    repeatedly inside LOG_SUPPRESS_SEC is enforced every time and logged once. Reading is
    what a quiet tick costs; an endpoint that is already muted at zero is not written to
    at all, which is why a machine nobody touches sees no COM writes and no log lines.
    """
    written = 0
    for endpoint in endpoints:
        muted, level = endpoint.state()
        if not needs_silencing(muted, level):
            continue
        endpoint.silence()
        written += 1
        if now - last_log.get(endpoint.key, float("-inf")) >= LOG_SUPPRESS_SEC:
            last_log[endpoint.key] = now
            was = "unmuted" if muted is not True else f"muted at {level:.0%}"
            log(log_path, f"[mute] {endpoint.name} was {was} - muted and set to 0%")
    return written


# --- the loop ----------------------------------------------------------------

def run(root: Path, *, opener=None, sleep=time.sleep, clock=time.monotonic,
        passes: int | None = None) -> int:
    """The resident loop. Returns only when launched somewhere it cannot enforce.

    `opener`, `sleep`, `clock` and `passes` are the test seams and nothing else: in
    production the loop opens real endpoints, sleeps real seconds and never stops.
    """
    if opener is None:
        if sys.platform != "win32":
            print("mic_guard mutes Windows capture endpoints; nothing to do here.")
            return 1
        opener = open_capture_endpoints

    config_path = root / "mic-guard.json"
    log_path = root / "mic-guard.log"

    endpoints: list = []
    acquired = float("-inf")
    cfg, warnings = MicConfig(), []
    read_ts = float("-inf")
    last_log: dict[str, float] = {}
    logged_warnings: list[str] | None = None
    failing = False
    done = 0

    while passes is None or done < passes:
        done += 1
        now = clock()

        if now - read_ts >= CONFIG_EVERY_SEC:
            cfg, warnings = load_config(config_path)
            read_ts = now

        if not cfg.enabled:
            # The deliberate parent stand-down. Hold nothing open while standing down, so
            # a disabled guard is a sleeping process and not a COM tenant. Nothing is
            # un-muted on the way out: standing the lane down means this loop stops
            # enforcing, not that it hands the microphone back.
            if endpoints:
                close_endpoints(endpoints)
                endpoints = []
                acquired = float("-inf")
            sleep(POLL_SEC)
            continue

        try:
            if not endpoints or now - acquired >= REACQUIRE_EVERY_SEC:
                close_endpoints(endpoints)
                endpoints = []
                endpoints = opener()
                acquired = now
                if failing:
                    failing = False
                    log(log_path, "[ok] capture endpoints re-enumerated")

            # Config warnings ride along with a pass that ACTS - a broken config on a
            # machine whose microphones are already muted says nothing - and only when
            # they changed since last said.
            if enforce_pass(endpoints, log_path, now, last_log) and \
                    warnings != logged_warnings:
                for w in warnings:
                    log(log_path, f"[warn] {w}")
                logged_warnings = list(warnings)
        except OSError as exc:
            # A headset disconnecting mid-pass, the audio service restarting, no endpoint
            # yet at logon: all land here. Drop everything, breathe, and enumerate from
            # scratch next pass - the one thing this loop must never do is crash out,
            # because a dead loop is fifteen minutes of open microphone before the task's
            # repetition resurrects it.
            close_endpoints(endpoints)
            endpoints = []
            acquired = float("-inf")
            if not failing:
                failing = True  # first failure of the episode is said once, then quiet
                log(log_path, f"[warn] capture endpoints unavailable ({exc}) - "
                              f"retrying every {RETRY_SEC:.0f}s")
            sleep(RETRY_SEC)
            continue

        sleep(POLL_SEC)
    close_endpoints(endpoints)
    return 0


# --- one-shot entry points ---------------------------------------------------

def status(root: Path) -> int:
    cfg, warnings = load_config(root / "mic-guard.json")
    print(f"Microphone guard - "
          f"{'ENFORCING' if cfg.enabled else 'DISABLED in mic-guard.json'}")
    print("  Scope      : every ACTIVE capture endpoint, muted and set to 0%")
    print(f"  Checks     : every {POLL_SEC:g}s, resident in the user's session")
    for w in warnings:
        print(f"  [warn] {w}")
    if sys.platform != "win32":
        print("  Microphones: (listing the live endpoints needs Windows)")
        return 0
    try:
        endpoints = open_capture_endpoints()
    except OSError as exc:
        print(f"  Microphones: unavailable ({exc})")
        return 0
    try:
        if not endpoints:
            print("  Microphones: none active")
        for endpoint in endpoints:
            try:
                muted, level = endpoint.state()
                print(f"  Microphone : {endpoint.name} - "
                      f"{'muted' if muted else 'LIVE'} at {level:.0%}")
            except OSError as exc:
                print(f"  Microphone : {endpoint.name} - unreadable ({exc})")
    finally:
        close_endpoints(endpoints)
    return 0


def once(root: Path) -> int:
    """One enumerate-and-silence pass - the live-verification entry point."""
    if sys.platform != "win32":
        print("mic_guard --once needs Windows: it talks to Core Audio.")
        return 1
    cfg, warnings = load_config(root / "mic-guard.json")
    for w in warnings:
        print(f"[warn] {w}")
    if not cfg.enabled:
        print("disabled in mic-guard.json - leaving the microphones alone")
        return 0
    endpoints = open_capture_endpoints()
    try:
        silenced = enforce_pass(endpoints, root / "mic-guard.log", time.monotonic(), {})
        print(f"{len(endpoints)} active capture endpoint(s); "
              f"{silenced} silenced, {len(endpoints) - silenced} already silent")
    finally:
        close_endpoints(endpoints)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safehouse-mic-guard", description=__doc__)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--status", action="store_true",
                   help="print the lane's state and every live capture endpoint")
    p.add_argument("--once", action="store_true",
                   help="a single enumerate-and-silence pass (for live verification)")
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
