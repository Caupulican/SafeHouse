"""The loop's decisions, against a fake app and a scripted console.

The properties that matter most are the safety ones: no step runs without a "y" for that
game, an empty Enter is never a yes, a dry run touches no step, a missed control turns
into an instruction, and PASS needs evidence.
"""

from __future__ import annotations

from playgames_cleanup import session
from playgames_cleanup.disk import GIB, Snapshot
from playgames_cleanup.session import (
    NOT_CHECKED,
    NOT_VERIFIED,
    PASS,
    Options,
    choose_games,
    judge,
    parse_choice,
    report,
    still_present,
)

FAST = Options(settle_timeout=10, poll_interval=1)


class FakeApp:
    """Library of games; each step can be set to miss. Records every call."""

    def __init__(self, games, *, miss=(), readable=True, removes=True):
        self.games = list(games)
        self.miss = set(miss)
        self.readable = readable
        self.removes = removes
        self.calls: list[tuple[str, str | None]] = []
        self._current: str | None = None

    def focus(self):
        self.calls.append(("focus", None))
        return True

    def list_games(self):
        return list(self.games) if self.readable else None

    def is_present(self, game):
        self.calls.append(("is_present", game))
        if not self.readable or not self.games:
            return None
        return game in self.games

    def open_game_menu(self, game):
        self.calls.append(("menu", game))
        self._current = game
        return "menu" not in self.miss

    def click_uninstall(self):
        self.calls.append(("uninstall", self._current))
        return "uninstall" not in self.miss

    def confirm_uninstall(self):
        self.calls.append(("confirm", self._current))
        if "confirm" in self.miss:
            return False
        if self.removes and self._current in self.games:
            self.games.remove(self._current)
        return True

    def dismiss(self):
        self.calls.append(("dismiss", None))

    def steps_called(self):
        return [c for c in self.calls if c[0] in ("menu", "uninstall", "confirm")]


class Console:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.out: list[str] = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt!r}")
        return self.answers.pop(0)

    def say(self, text):
        self.out.append(text)


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def __call__(self):
        return self.t


class Disk:
    """avd shrinks by `per_call` bytes after each measurement pair, if set."""

    def __init__(self, start=80 * GIB, shrink=0):
        self.avd = start
        self.shrink = shrink
        self.n = 0

    def __call__(self):
        self.n += 1
        if self.n % 2 == 0:            # the "after" measurement
            self.avd -= self.shrink
        return Snapshot(self.avd, 100 * GIB)


def go(games, app, answers, *, opts=FAST, disk_=None, guard=None, reg=lambda: []):
    con = Console(answers)
    clock = Clock()
    results = session.run(games, app, ask=con.ask, say=con.say,
                          measure=disk_ or Disk(), guard_ok=guard or (lambda: (True, "ok")),
                          reg_names=reg, opts=opts, sleep=clock.sleep, clock=clock)
    return results, con


# --- parse_choice -------------------------------------------------------------

def test_parse_choice():
    assert parse_choice("y") == "y" and parse_choice(" YES ") == "y"
    assert parse_choice("s") == "s" and parse_choice("skip") == "s"
    assert parse_choice("q") == "q" and parse_choice("Quit") == "q"


def test_empty_and_junk_are_not_answers():
    for text in ("", "   ", "n", "no", "sim", "yy", "x"):
        assert parse_choice(text) is None, text


# --- judge / presence ---------------------------------------------------------

def test_still_present():
    assert still_present(True, None) is True
    assert still_present(None, True) is True
    assert still_present(False, True) is True        # any source listing it wins
    assert still_present(False, None) is False
    assert still_present(None, False) is False
    assert still_present(None, None) is None


def test_judge_matrix():
    big, none = 2 * GIB, 0
    assert judge(True, big) == (PASS, "left the library; avd shrank")
    v, note = judge(True, none)
    assert v == PASS and "avd file size unchanged" in note
    assert judge(True, None)[0] == PASS
    assert judge(None, big)[0] == PASS
    assert judge(None, none)[0] == NOT_VERIFIED
    assert judge(None, None)[0] == NOT_VERIFIED
    assert judge(False, none) == (NOT_VERIFIED, "still listed")
    assert judge(False, big)[0] == NOT_VERIFIED
    assert judge(True, -GIB)[0] == PASS              # grew a little: still gone


# --- the loop -------------------------------------------------------------------

def test_happy_path_automated_pass():
    app = FakeApp(["A", "B", "Keep"])
    results, _ = go(["A", "B", "Keep"], app, ["y", "y", "s"])
    assert [(r.game, r.action, r.verdict) for r in results] == [
        ("A", "uninstalled", PASS), ("B", "uninstalled", PASS), ("Keep", "skipped", "-")]
    assert app.games == ["Keep"]


def test_last_game_is_not_verified_by_an_empty_library():
    # Empty and unreadable look the same over UIA, so the last game needs the avd or the
    # registry to PASS. Conservative on purpose.
    app = FakeApp(["A"])
    results, _ = go(["A"], app, ["y"])
    assert results[0].verdict == NOT_VERIFIED
    app = FakeApp(["A"])
    results, _ = go(["A"], app, ["y"], disk_=Disk(shrink=GIB))
    assert results[0].verdict == PASS


def test_nothing_runs_without_y():
    app = FakeApp(["A", "B"])
    results, con = go(["A", "B"], app, ["", "nope", "s", "q"])
    assert app.steps_called() == []
    assert [(r.game, r.action) for r in results] == [("A", "skipped")]
    assert len(con.prompts) == 4                       # asked again on "" and "nope"


def test_quit_stops_without_recording():
    app = FakeApp(["A", "B"])
    results, _ = go(["A", "B"], app, ["q"])
    assert results == [] and app.steps_called() == []


def test_dry_run_touches_no_step():
    app = FakeApp(["A"])
    results, con = go(["A"], app, ["y"], opts=Options(dry_run=True))
    assert app.steps_called() == []
    assert results[0].verdict == NOT_CHECKED and results[0].action == "dry-run"
    assert any("[dry-run] would" in line for line in con.out)


def test_missed_step_prints_instruction_and_waits():
    app = FakeApp(["Clash Royale", "Other"], miss={"menu"})
    results, con = go(["Clash Royale"], app, ["y", ""])
    text = "\n".join(con.out)
    assert "Clash Royale" in text and "right-click" in text
    assert any("Press Enter when done" in p for p in con.prompts)
    assert results[0].action == "uninstalled (assisted)"
    assert results[0].verdict == PASS
    # the remaining steps were still tried automatically after the manual one
    assert [c[0] for c in app.steps_called()] == ["menu", "uninstall", "confirm"]


def test_missed_destructive_step_names_both_languages():
    app = FakeApp(["A"], miss={"uninstall"})
    _, con = go(["A"], app, ["y", ""])
    text = "\n".join(con.out)
    assert '"Uninstall"' in text and '"Desinstalar"' in text


def test_abort_during_manual_step():
    app = FakeApp(["A", "B"], miss={"menu"})
    results, _ = go(["A", "B"], app, ["y", "a", "s"])
    assert results[0].action == "aborted" and results[0].verdict == NOT_CHECKED
    assert ("dismiss", None) in app.calls
    assert ("uninstall", "A") not in app.calls
    assert results[1].action == "skipped"


def test_yes_per_step_asks_before_each_step():
    app = FakeApp(["A", "B"])
    results, con = go(["A"], app, ["y", "", "", ""],
                      opts=Options(yes_per_step=True, settle_timeout=10, poll_interval=1))
    assert sum("Next:" in p for p in con.prompts) == 3
    assert results[0].verdict == PASS


def test_yes_per_step_abort_before_destructive_step():
    app = FakeApp(["A"])
    results, _ = go(["A"], app, ["y", "", "a"],
                    opts=Options(yes_per_step=True, settle_timeout=10, poll_interval=1))
    assert [c[0] for c in app.steps_called()] == ["menu"]
    assert results[0].action == "aborted"


def test_still_listed_is_not_verified():
    app = FakeApp(["A", "B"], removes=False)
    results, _ = go(["A"], app, ["y"])
    assert results[0].verdict == NOT_VERIFIED and results[0].note == "still listed"


def test_unreadable_library_falls_back_to_avd():
    app = FakeApp(["A"], readable=False)
    results, _ = go(["A"], app, ["y"], disk_=Disk(shrink=3 * GIB))
    assert results[0].verdict == PASS and results[0].freed_bytes == 3 * GIB
    app2 = FakeApp(["A"], readable=False)
    results2, _ = go(["A"], app2, ["y"])
    assert results2[0].verdict == NOT_VERIFIED


def test_registry_signal_used_when_ui_unreadable():
    reg = ["A"]
    app = FakeApp(["A"], readable=False)
    orig = app.confirm_uninstall

    def confirm():
        reg.clear()
        return orig()

    app.confirm_uninstall = confirm
    results, _ = go(["A"], app, ["y"], reg=lambda: list(reg))
    assert results[0].verdict == PASS
    assert "avd file size unchanged" in results[0].note


def test_registry_ignored_for_games_it_never_listed():
    app = FakeApp(["A"], readable=False)
    results, _ = go(["A"], app, ["y"], reg=lambda: ["Something else"])
    assert results[0].verdict == NOT_VERIFIED


def test_guard_relock_stops_the_loop():
    states = iter([(True, "ok"), (False, "guard is ENFORCING")])
    app = FakeApp(["A", "B"])
    results, con = go(["A", "B"], app, ["y"], guard=lambda: next(states))
    assert [(r.game, r.verdict) for r in results] == [("A", PASS), ("B", NOT_CHECKED)]
    assert ("menu", "B") not in app.calls


def test_step_exception_becomes_manual():
    app = FakeApp(["A"])

    def boom(game):
        raise RuntimeError("COM error")

    app.open_game_menu = boom
    results, con = go(["A"], app, ["y", ""])
    assert any("COM error" in line for line in con.out)
    assert results[0].action == "uninstalled (assisted)"


def test_results_list_survives_interrupt():
    app = FakeApp(["A", "B"])
    con = Console(["y"])
    clock = Clock()
    out: list = []

    def ask(prompt):
        if not con.answers:
            raise KeyboardInterrupt
        return con.ask(prompt)

    try:
        session.run(["A", "B"], app, ask=ask, say=con.say, measure=Disk(),
                    guard_ok=lambda: (True, ""), opts=FAST, results=out,
                    sleep=clock.sleep, clock=clock)
    except KeyboardInterrupt:
        pass
    assert [r.game for r in out] == ["A"]


# --- choose_games / report --------------------------------------------------------

def test_choose_prefers_ui_and_shows_registry_extras():
    con = Console([])
    games = choose_games(["A", "B"], ["b", "C"], ask=con.ask, say=con.say)
    assert games == ["A", "B"]
    assert any("C" in line and "not seen" in line for line in con.out)


def test_choose_falls_back_to_registry_then_typing():
    con = Console(["y"])
    assert choose_games(None, ["R1"], ask=con.ask, say=con.say) == ["R1"]
    con = Console(["s", "X", "Y", ""])
    assert choose_games(None, ["R1"], ask=con.ask, say=con.say) == ["X", "Y"]


def test_choose_one_at_a_time_mode():
    con = Console(["", "First", "Second", ""])
    games = choose_games(None, [], ask=con.ask, say=con.say)
    assert list(games) == ["First", "Second"]


def test_report_totals_and_honest_note():
    results = [
        session.Result("A", "uninstalled", PASS, 0, "left the library; ..."),
        session.Result("B", "skipped", "-", None),
        session.Result("C", "uninstalled", NOT_VERIFIED, 0, "still listed"),
    ]
    lines = report(results, Snapshot(80 * GIB, 10 * GIB), Snapshot(80 * GIB, 10 * GIB))
    text = "\n".join(lines)
    assert "1 of 2 attempted" in text
    assert "80.00 GB -> 80.00 GB (+0.00 GB)" in text
    assert "did not shrink" in text


def test_report_when_space_came_back():
    results = [session.Result("A", "uninstalled", PASS, 5 * GIB, "left the library; avd shrank")]
    lines = report(results, Snapshot(80 * GIB, 10 * GIB), Snapshot(75 * GIB, 15 * GIB))
    text = "\n".join(lines)
    assert "(+5.00 GB)" in text and "did not shrink" not in text
    assert "10.00 GB -> 15.00 GB (+5.00 GB)" in text
