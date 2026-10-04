"""The pure half of app.py: which window, which names are games, which registry entries.

None of this touches pywinauto; importing app.py on Linux is itself part of the test.
"""

from __future__ import annotations

from playgames_cleanup import app
from playgames_cleanup.app import (
    filter_game_candidates,
    games_from_uninstall_entries,
    is_more_options_for,
    name_in,
    pick_main_window,
    under_install_root,
    wait_for,
)

CLIENT = r"C:\Program Files\Google\Play Games\current\client.exe"


def test_name_matching_is_case_and_space_insensitive():
    assert name_in("  desinstalar ", ("Uninstall", "Desinstalar"))
    assert name_in("UNINSTALL", ("Uninstall",))
    assert not name_in("Uninstall now", ("Uninstall",))
    assert not name_in("", ("",))
    assert not name_in(None, ("Uninstall",))


def test_filter_drops_chrome_and_keeps_games_in_order():
    raw = ["Home", "Biblioteca", "Subway Surfers", "", None, "Instalados",
           "subway surfers", "Asphalt 9: Legends", "Mais opções", "4.5", "Jogar",
           "x" * 200, "Mais opções de Subway Surfers", "Clash Royale"]
    assert filter_game_candidates(raw) == ["Subway Surfers", "Asphalt 9: Legends",
                                           "Clash Royale"]


def test_filter_normalises_whitespace():
    assert filter_game_candidates(["  Free   Fire "]) == ["Free Fire"]


def test_more_options_for_a_specific_game():
    assert is_more_options_for("More options for Clash Royale", "Clash Royale")
    assert is_more_options_for("mais opções de clash royale", "Clash Royale")
    assert not is_more_options_for("More options for Clash of Clans", "Clash Royale")
    assert not is_more_options_for("More options", "Clash Royale")


def test_install_root_is_a_directory_boundary():
    assert under_install_root(CLIENT)
    assert under_install_root(CLIENT.upper())
    assert not under_install_root(
        r"C:\Program Files\Google\Play Games Services\GooglePlayGamesServices.exe")
    assert not under_install_root(None)


def test_pick_main_window_needs_title_and_path():
    cands = [
        (1, "Google Play Games", r"C:\Program Files\Opera GX\opera.exe"),  # browser tab
        (2, "", CLIENT),                                                   # tray helper
        (3, "Google Play Games",
         r"C:\Program Files\Google\Play Games Services\x.exe"),            # sibling product
        (4, "Google Play Games", CLIENT),
    ]
    assert pick_main_window(cands) == 4
    assert pick_main_window(cands[:3]) is None
    assert pick_main_window([(9, "google play games beta", CLIENT)]) == 9


def test_registry_entries_filtered_to_play_games_games():
    entries = [
        {"DisplayName": "Google Play Games",
         "UninstallString": r'"C:\Program Files\Google\Play Games\Uninstaller.exe"'},
        {"DisplayName": "Subway Surfers",
         "UninstallString": r'"C:\Program Files\Google\Play Games\Bootstrapper.exe" '
                            r'--uninstall com.kiloo.subwaysurf'},
        {"DisplayName": "Clash Royale", "UninstallString": "",
         "DisplayIcon": r"C:\Program Files\Google\Play Games\icons\cr.ico"},
        {"DisplayName": "7-Zip", "UninstallString": r"C:\Program Files\7-Zip\Uninstall.exe"},
        {"DisplayName": "Achievements helper",
         "UninstallString": r"C:\Program Files\Google\Play Games Services\u.exe"},
        {"UninstallString": r"C:\Program Files\Google\Play Games\x.exe"},
    ]
    assert games_from_uninstall_entries(entries) == ["Subway Surfers", "Clash Royale"]


def test_wait_for_returns_first_truthy():
    calls = iter([None, [], ["hit"]])
    t = [0.0]
    out = wait_for(lambda: next(calls), timeout=10, sleep=lambda s: t.__setitem__(0, t[0] + s),
                   clock=lambda: t[0])
    assert out == ["hit"]


def test_wait_for_times_out():
    t = [0.0]
    out = wait_for(lambda: None, timeout=3, interval=1,
                   sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
    assert out is None and t[0] == 3


def test_windows_only_helpers_are_inert_off_windows():
    assert app.registry_uninstall_entries() == [] or app.sys.platform == "win32"
    assert app.process_exe(1) is None or app.sys.platform == "win32"
