"""Find and drive the Play Games window over UI Automation.

The client is a Chromium (CEF) app. Chromium builds its accessibility tree only when an
assistive client asks, and how much of the page it exposes to UIA varies by build - so
every method here can come back empty-handed, and every caller treats that as normal:
"not found" turns into an instruction for the parent, never into a guess.

Rules this module keeps:

* **Lookup is by accessible name and control type** (strings in `ui_strings`), never by
  screen position.
* **Destructive controls are only ever INVOKED** (UIA InvokePattern - no mouse involved).
  If a destructive control cannot be invoked, or more than one control could be the one
  meant, the step fails and the parent clicks it themselves.
* **pyautogui does only harmless things**: hover a tile that UIA located (some tiles show
  their menu button on hover) and press Escape to close a menu we opened.

The pure parts - name matching, filtering the tree down to game titles, picking the right
top-level window, reading the uninstall registry - sit at the top and are what the tests
cover. pywinauto and pyautogui are imported lazily, so this module imports on Linux.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Callable, Iterable

from . import ui_strings as S

log = logging.getLogger(__name__)


# --- pure helpers ------------------------------------------------------------

def norm(text: str | None) -> str:
    """Case- and whitespace-insensitive form used for every name comparison."""
    return " ".join((text or "").split()).casefold()


def name_in(name: str | None, names: Iterable[str]) -> bool:
    n = norm(name)
    return bool(n) and any(n == norm(x) for x in names)


def is_more_options_for(name: str | None, game: str) -> bool:
    """Is this the overflow button for `game` specifically ("More options for X")?"""
    n = norm(name)
    return any(n == norm(prefix + game) for prefix in S.MORE_OPTIONS_PREFIXES)


def filter_game_candidates(names: Iterable[str | None]) -> list[str]:
    """Accessible names from the library view -> plausible game titles, in order, unique.

    Drops app chrome (`CHROME_NAMES`), overflow-button labels, blanks, sentences, and
    anything that is only digits/punctuation (counters, ratings). Heuristic on purpose:
    the per-game y/s/q prompt is the real filter, this only keeps the list short.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        name = " ".join((raw or "").split())
        key = name.casefold()
        if not name or key in seen:
            continue
        if key in S.CHROME_NAMES or len(name) > S.MAX_GAME_NAME_LEN:
            continue
        if any(key.startswith(p) for p in S.MORE_OPTIONS_PREFIXES):
            continue
        if not any(ch.isalpha() for ch in name):
            continue
        seen.add(key)
        out.append(name)
    return out


def under_install_root(exe_path: str | None) -> bool:
    return bool(exe_path) and exe_path.casefold().startswith(S.INSTALL_ROOT.casefold())


def pick_main_window(candidates: Iterable[tuple[int, str, str | None]]) -> int | None:
    """(handle, title, exe path) of each top-level window -> the Play Games main window.

    Both tests must hold: the title is one of MAIN_WINDOW_TITLES AND the owning process
    runs from under the install root. The title alone would also match a browser tab
    called "Google Play Games"; the path alone would also match a tray or toast window.
    """
    for handle, title, exe in candidates:
        if name_in(title, S.MAIN_WINDOW_TITLES) and under_install_root(exe):
            return handle
    return None


def games_from_uninstall_entries(entries: Iterable[dict]) -> list[str]:
    """Windows "Installed apps" entries -> the ones Play Games put there for its games.

    Each Play Games game is expected to register its own uninstall entry whose command
    runs from the Play Games install directory. The app itself is excluded by name.
    """
    out: list[str] = []
    for entry in entries:
        name = (entry.get("DisplayName") or "").strip()
        cmd = (entry.get("UninstallString") or "") + " " + (entry.get("DisplayIcon") or "")
        if not name or name_in(name, S.MAIN_WINDOW_TITLES):
            continue
        if "\\google\\play games\\" in cmd.casefold() and name not in out:
            out.append(name)
    return out


# --- Windows-only plumbing ---------------------------------------------------

def process_exe(pid: int) -> str | None:
    """Full image path of a pid, via QueryFullProcessImageNameW. None if not queryable."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(handle)


def registry_uninstall_entries() -> list[dict]:
    """Read-only walk of the per-user and machine Uninstall keys. Windows only."""
    if sys.platform != "win32":
        return []
    import winreg

    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    out: list[dict] = []
    for hive, path in keys:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                        entry = {}
                        for field in ("DisplayName", "UninstallString", "DisplayIcon"):
                            try:
                                entry[field] = str(winreg.QueryValueEx(sub, field)[0])
                            except OSError:
                                pass
                        out.append(entry)
                except OSError:
                    continue
    return out


def registry_games() -> list[str]:
    return games_from_uninstall_entries(registry_uninstall_entries())


def wait_for(fn: Callable[[], object], timeout: float, interval: float = 0.5,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic):
    """Call `fn` until it returns something truthy or `timeout` passes; return that."""
    deadline = clock() + timeout
    while True:
        result = fn()
        if result or clock() >= deadline:
            return result
        sleep(interval)


# --- the driver --------------------------------------------------------------

class PlayGamesApp:
    """The live client. Every public method returns a plain value and never raises for
    "control not found" - the session decides what a miss means."""

    def __init__(self, launch_timeout: float = 120.0, step_timeout: float = 8.0):
        self.launch_timeout = launch_timeout
        self.step_timeout = step_timeout
        self._handle: int | None = None

    # ---- window -----------------------------------------------------------

    def _desktop(self):
        from pywinauto import Desktop  # imported first: it sets the COM apartment
        return Desktop(backend="uia")

    def _top_levels(self) -> list:
        """Top-level UIA windows owned by a process under the install root."""
        out = []
        for w in self._desktop().windows():
            try:
                if under_install_root(process_exe(w.element_info.process_id)):
                    out.append(w)
            except Exception:  # noqa: BLE001 - a window that closed mid-walk
                continue
        return out

    def _find_main(self) -> int | None:
        cands = []
        for w in self._desktop().windows():
            try:
                cands.append((w.handle, w.window_text(),
                              process_exe(w.element_info.process_id)))
            except Exception:  # noqa: BLE001
                continue
        return pick_main_window(cands)

    def main_window(self):
        if self._handle is None:
            return None
        try:
            return self._desktop().window(handle=self._handle).wrapper_object()
        except Exception:  # noqa: BLE001 - closed (or killed by the guard)
            self._handle = None
            return None

    def connect_or_launch(self) -> bool:
        """Attach to a running client, else start Bootstrapper.exe and wait for it."""
        self._handle = self._find_main()
        if self._handle is None:
            log.info("launching %s", S.BOOTSTRAPPER)
            try:
                subprocess.Popen([S.BOOTSTRAPPER], close_fds=True)
            except OSError as exc:
                log.error("could not start Play Games: %s", exc)
                return False
            self._handle = wait_for(self._find_main, self.launch_timeout, interval=2.0)
        log.info("main window handle: %s", self._handle)
        return self._handle is not None

    def focus(self) -> bool:
        w = self.main_window()
        if w is None:
            return False
        try:
            if w.is_minimized():
                w.restore()
            w.set_focus()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not focus window: %s", exc)
            return False

    # ---- lookup -----------------------------------------------------------

    def _search(self, match: Callable[[object], bool],
                control_types: Iterable[str] | None = None) -> list:
        """Every visible element in any Play Games top-level window that `match`es.

        All top-levels, not just the main one: Chromium may render a menu or dialog as
        its own popup window.
        """
        found = []
        for root in self._top_levels():
            for ct in (control_types or (None,)):
                try:
                    elems = root.descendants(control_type=ct) if ct else root.descendants()
                except Exception:  # noqa: BLE001
                    continue
                for e in elems:
                    try:
                        if e.is_visible() and match(e):
                            found.append(e)
                    except Exception:  # noqa: BLE001
                        continue
        return found

    def _by_name(self, names: Iterable[str], control_types=None) -> list:
        names = tuple(names)
        return self._search(lambda e: name_in(e.element_info.name, names), control_types)

    @staticmethod
    def _invoke(elem) -> bool:
        """InvokePattern only. No mouse, no coordinates."""
        try:
            elem.invoke()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("invoke failed on %r: %s", elem.element_info.name, exc)
            return False

    def _activate(self, elem) -> bool:
        """For NON-destructive controls: invoke, else click the element UIA located."""
        if self._invoke(elem):
            return True
        try:
            elem.click_input()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("click failed on %r: %s", elem.element_info.name, exc)
            return False

    # ---- navigation (non-destructive) --------------------------------------

    def open_library(self) -> bool:
        self.focus()
        hits = wait_for(lambda: self._by_name(
            S.LIBRARY, ("Button", "Hyperlink", "TabItem", "ListItem", "MenuItem", "Text")),
            self.step_timeout)
        if not hits or not self._activate(hits[0]):
            return False
        time.sleep(1.5)
        chips = self._by_name(S.INSTALLED_FILTER, ("Button", "RadioButton", "TabItem",
                                                   "CheckBox", "ListItem", "Hyperlink"))
        if chips:
            self._activate(chips[0])
            time.sleep(1.0)
        else:
            log.info("no Installed filter found; enumerating the whole library")
        return True

    def list_games(self) -> list[str] | None:
        """Game titles visible in the library, or None if the view could not be read."""
        if not self.open_library():
            return None
        names: list[str] = []
        for e in self._search(lambda e: True, S.TILE_CONTROL_TYPES):
            try:
                names.append(e.element_info.name)
            except Exception:  # noqa: BLE001
                continue
        games = filter_game_candidates(names)
        log.info("UIA candidates: %s", games)
        return games or None

    def is_present(self, game: str) -> bool | None:
        """True/False when the library could be read, None when it could not."""
        games = self.list_games()
        if not games:
            return None
        return any(norm(g) == norm(game) for g in games)

    # ---- the three steps ----------------------------------------------------

    def _menu_visible(self) -> bool:
        return bool(self._by_name(S.UNINSTALL, ("MenuItem", "Button", "ListItem")))

    def open_game_menu(self, game: str) -> bool:
        """Open the game's overflow menu (non-destructive). True once an Uninstall item
        is showing."""
        self.focus()
        # 1. A button that names the game ("More options for X").
        own = self._search(lambda e: is_more_options_for(e.element_info.name, game),
                           ("Button", "MenuItem"))
        if len(own) == 1 and self._activate(own[0]):
            if wait_for(self._menu_visible, self.step_timeout):
                return True
        tiles = self._by_name((game,), S.TILE_CONTROL_TYPES)
        if not tiles:
            log.info("tile for %r not found", game)
            return False
        tile = tiles[0]
        # 2. Hover the tile (reveals hover-only buttons), then a bare "More options"
        #    button inside that tile - only if there is exactly one.
        try:
            import pyautogui
            r = tile.rectangle()
            pyautogui.moveTo((r.left + r.right) // 2, (r.top + r.bottom) // 2, duration=0.2)
            time.sleep(0.5)
            inner = [d for d in tile.descendants(control_type="Button")
                     if name_in(d.element_info.name, S.MORE_OPTIONS)]
            if len(inner) == 1 and self._activate(inner[0]):
                if wait_for(self._menu_visible, self.step_timeout):
                    return True
        except Exception as exc:  # noqa: BLE001
            log.info("hover route failed: %s", exc)
        # 3. Right-click the tile UIA located: opens a context menu, changes nothing.
        try:
            tile.click_input(button="right")
            return bool(wait_for(self._menu_visible, self.step_timeout))
        except Exception as exc:  # noqa: BLE001
            log.info("right-click route failed: %s", exc)
            return False

    def click_uninstall(self) -> bool:
        """Invoke the single Uninstall menu item. Destructive-adjacent: invoke only."""
        hits = self._by_name(S.UNINSTALL, ("MenuItem", "ListItem", "Button"))
        if len(hits) != 1:
            log.info("uninstall item: %d matches, not guessing", len(hits))
            return False
        return self._invoke(hits[0])

    def confirm_uninstall(self) -> bool:
        """Invoke the single confirm button in the dialog. Destructive: invoke only, and
        only when exactly one button matches."""
        hits = wait_for(lambda: self._by_name(S.CONFIRM, ("Button",)), self.step_timeout)
        if not hits or len(hits) != 1:
            log.info("confirm button: %d matches, not guessing", len(hits or []))
            return False
        return self._invoke(hits[0])

    def dismiss(self) -> None:
        """Close whatever menu we may have left open. Escape is harmless here."""
        try:
            import pyautogui
            pyautogui.press("esc")
        except Exception:  # noqa: BLE001
            pass
