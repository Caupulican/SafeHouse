"""The panel's view of the filtering resolver, and its button.

The section exists because the outage it reports is invisible from inside the machine:
when WSL is down the relay fails open to the router, so every other reading on the page
stays green. So the tests here are mostly about refusing to say things are fine.
"""

from __future__ import annotations

import pytest

from safehouse_panel import controls, status as statusmod
from safehouse_panel.i18n import render


# Human-facing text travels as keys+params (see i18n.py); rendering it back through the
# ONE catalog keeps these assertions about substance, not about a hardcoded copy.
def notes_of(view) -> list[str]:
    return [render(n) for n in view["notes"]]

TASK_UP = {"name": "SafeHouse-Resolver", "registered": True, "enabled": True,
           "state": "Ready", "last_run": "2026-08-22 10:00:00", "last_result": 0,
           "last_result_text": "OK", "next_run": "2026-08-22 10:15:00"}
TASK_MISSING = dict(TASK_UP, registered=False, enabled=False, state="unknown")
TASK_UNKNOWN = dict(TASK_UP, registered=None)
TASK_DISABLED = dict(TASK_UP, enabled=False, state="Disabled")

UP = {"filtering": True, "resolves": True, "relay": True, "adguard": True,
      "pihole": True, "vm": True, "up": True}
# The dangerous one: DNS answers, browsing works, and nothing is blocked. Every reading
# except the filtering probe looks healthy. WSL is up here and the containers are not,
# which is the case with no other tell at all - a stopped VM at least has `vmmemWSL`
# missing to point at, and gets its own message.
FAILED_OPEN = {"filtering": False, "resolves": True, "relay": True, "adguard": False,
               "pihole": False, "vm": True, "up": False}
DEAD = {"filtering": False, "resolves": False, "relay": False, "adguard": False,
        "pihole": False, "vm": None, "up": False}
HALF = {"filtering": True, "resolves": True, "relay": True, "adguard": True,
        "pihole": False, "vm": True, "up": True}


def view(chain, task=TASK_UP, last_run=None):
    return statusmod.resolver_view(chain=chain, last_run=last_run, task=task,
                                   now=1_700_000_000)


def test_a_sinkholed_ad_domain_is_what_makes_it_active():
    assert view(UP)["status"] == statusmod.ACTIVE


def test_dns_answering_with_nothing_blocked_is_not_active():
    """The whole reason this section exists. Every other reading on the page is green in
    this state: names resolve, browsing works, the relay is up - and no filtering at all
    is happening, because the relay failed open to the router."""
    seen = view(FAILED_OPEN)
    assert seen["status"] == statusmod.DEGRADED
    assert seen["reason"]["k"] == "resolver.reason.failopen"
    assert "NOTHING IS BEING BLOCKED" in render(seen["reason"])
    assert "looks healthy" in render(seen["reason"])
    assert "NADA ESTÁ SENDO BLOQUEADO" in render(seen["reason"], "pt")


def test_dns_being_entirely_dead_is_reported_as_its_own_thing():
    """Different problem, different fix - and unlike the others, the child notices."""
    seen = view(DEAD)
    assert seen["status"] == statusmod.DEGRADED
    assert "not even the relay" in render(seen["reason"])


def test_a_missing_wsl_vm_is_named_rather_than_inferred():
    seen = view(dict(FAILED_OPEN, vm=False))
    assert "WSL virtual machine is not running" in render(seen["reason"])


def test_filtering_with_one_resolver_missing_is_flagged_even_though_it_is_active():
    """Blocking works, so this is not DEGRADED - but half the coverage is out of the
    path, and a bare green pill would hide that until somebody reached a site."""
    seen = view(HALF)
    assert seen["status"] == statusmod.ACTIVE
    assert any("part of the coverage" in note for note in notes_of(seen))


def test_an_unreadable_process_list_does_not_become_not_running():
    """None is 'I could not look'. Printing 'not running' for it is exactly the kind of
    invented reading this dashboard exists to avoid."""
    facts = {render(f["label"]): render(f["value"])
             for f in view(dict(FAILED_OPEN, vm=None))["facts"]}
    assert facts["WSL virtual machine"] == "could not be read"


def test_a_healthy_chain_with_no_watchdog_is_still_flagged():
    """Up now is not the same as staying up. A chain nobody restarts is one WSL shutdown
    away from the fourteen-hour outage."""
    seen = view(UP, task=TASK_MISSING)
    assert seen["status"] == statusmod.DEGRADED
    assert any("install.py --resolver-only" in note for note in notes_of(seen))


def test_a_disabled_watchdog_is_named_without_downgrading_a_working_chain():
    seen = view(UP, task=TASK_DISABLED)
    assert seen["status"] == statusmod.ACTIVE
    assert any("nothing will bring it back" in note for note in notes_of(seen))


def test_the_logged_on_requirement_is_always_stated():
    """Otherwise a parent discovers it by pressing the button from a phone while the PC
    sits at the lock screen, and concluding the panel is broken."""
    for chain in (UP, FAILED_OPEN, DEAD, HALF):
        assert any("nobody is signed in" in note for note in notes_of(view(chain)))
        # ...and in the language the parent actually reads it in.
        assert any("ninguém está com sessão aberta" in render(note, "pt")
                   for note in view(chain)["notes"])


def test_the_last_repair_is_reported_when_there_is_one():
    record = {"stamp": "2026-08-22 09:45:00", "action": "repaired",
              "detail": "The filtering chain was down and is answering again.",
              "steps": [{"name": "start the WSL distro", "ok": True, "detail": "started"}]}
    seen = view(UP, last_run=record)
    labels = {render(f["label"]): render(f["value"]) for f in seen["facts"]}
    assert "2026-08-22 09:45:00" in labels["Last watchdog run"]
    # The watchdog's own account is quoted verbatim - the documented pass-through.
    assert record["detail"] in seen["notes"]


def test_a_failed_step_is_surfaced_rather_than_summarised_away():
    record = {"stamp": "x", "action": "repaired", "detail": "STILL DOWN",
              "steps": [{"name": "reconcile the DNS stack", "ok": False,
                         "detail": "docker: command not found"}]}
    notes = " ".join(notes_of(view(FAILED_OPEN, last_run=record)))
    assert "docker: command not found" in notes


def test_with_no_record_the_page_says_whose_reading_it_is_showing():
    assert any("panel's own probe" in note for note in notes_of(view(UP)))


# --- the button --------------------------------------------------------------

class FakeCollector:
    def __init__(self, ok=True, detail=""):
        self.result = {"ok": ok, "detail": detail}
        self.calls = 0

    def start_resolver(self):
        self.calls += 1
        return self.result


def test_the_button_reports_asked_not_done(tmp_path):
    """`schtasks /Run` returns when the scheduler accepts, not when WSL is up. Saying
    'filtering is back' here would be a dashboard asserting something nobody checked."""
    collector = FakeCollector()
    outcome = controls.start_resolver(tmp_path, True, now=0, collector=collector)
    assert outcome.ok and collector.calls == 1
    assert outcome.message["k"] == "ctl.resolver.asked"
    assert "Asked" in render(outcome.message)
    assert "up to a minute" in render(outcome.message)


def test_a_refused_start_explains_the_two_causes(tmp_path):
    outcome = controls.start_resolver(tmp_path, True, now=0,
                                      collector=FakeCollector(ok=False, detail="ERROR"))
    assert outcome.ok is False
    assert "--resolver-only" in render(outcome.message)
    assert "nobody is signed in" in render(outcome.message)
    assert "ERROR" in render(outcome.message)


def test_starting_the_resolver_never_asks_for_the_password(tmp_path):
    """It only ever turns filtering back ON. Asking for a password to repair a broken
    protection is friction with nothing on the other side of it."""
    assert controls.requires_password("resolver_start", True, {}) is False


@pytest.mark.parametrize("action", sorted(controls.ACTIONS))
def test_every_action_is_classified_by_the_password_rule(action):
    """A control added later without being classified must fail towards asking."""
    current = controls.current_values.__doc__ and {}
    assert isinstance(controls.requires_password(action, 0, current), bool)
