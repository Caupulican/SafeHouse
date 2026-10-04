"""The three UI steps of one uninstall, each with what to tell the parent if it misses.

A step is an automated attempt plus a plain-language instruction. When the attempt
cannot find its control (or will not guess between two), the session prints the
instruction - naming the control in both languages - and waits for Enter. So the worst
case of a UI the strings do not match is a guided manual uninstall, not a wrong click.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from . import ui_strings as S


class App(Protocol):
    """What the session needs from the UI. `app.PlayGamesApp` is the real one; the tests
    pass a fake."""

    def focus(self) -> bool: ...
    def list_games(self) -> list[str] | None: ...
    def is_present(self, game: str) -> bool | None: ...
    def open_game_menu(self, game: str) -> bool: ...
    def click_uninstall(self) -> bool: ...
    def confirm_uninstall(self) -> bool: ...
    def dismiss(self) -> None: ...


def _either(names: tuple[str, ...]) -> str:
    return " / ".join(f'"{n}"' for n in names[:2])


@dataclass(frozen=True)
class Step:
    key: str
    describe: str                          # for --yes-per-step and the log
    run: Callable[[App, str], bool]
    manual: Callable[[str], str]           # instruction, given the game name
    destructive: bool


STEPS: tuple[Step, ...] = (
    Step(
        key="menu",
        describe="open the game's menu",
        run=lambda app, game: app.open_game_menu(game),
        manual=lambda game: (f'In the Library, find "{game}". Open its menu: the '
                             f'three-dot button on the tile ({_either(S.MORE_OPTIONS)}), '
                             f'or right-click the tile.'),
        destructive=False,
    ),
    Step(
        key="uninstall",
        describe="choose Uninstall in the menu",
        run=lambda app, game: app.click_uninstall(),
        manual=lambda game: f"In that menu, click {_either(S.UNINSTALL)}.",
        destructive=True,
    ),
    Step(
        key="confirm",
        describe="confirm the uninstall",
        run=lambda app, game: app.confirm_uninstall(),
        manual=lambda game: (f"If a confirmation box is showing, click "
                             f"{_either(S.UNINSTALL)} in it. If none appeared, there is "
                             f"nothing to click."),
        destructive=True,
    ),
)
