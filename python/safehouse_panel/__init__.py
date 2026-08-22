"""SafeHouse control panel - one page that shows what is enforced, and changes it.

    C:\\SafeHouse\\python\\python.exe -m safehouse_panel

Everything this project enforces lives in a different place: two scheduled tasks, four
JSON files under `C:\\ProgramData\\SafeHouse`, machine policy in HKLM, and a filtering
resolver inside WSL. A parent who wants to know whether Roblox is allowed today has to
read three of those. This package assembles all of it into one page and lets the settings
that matter be changed with a slider.

It is a **dashboard first**. Every number on it is read from the live system - the task
state from Task Scheduler, the budget from the config file, the minutes used from the
state file, the filter lists from AdGuard's own API, the browser policy from the
registry. Nothing on the page is a constant pretending to be a reading, and the things
that are NOT enforced (see `status.KNOWN_GAPS`) are on the page next to the things that
are. A dashboard that only ever shows green is the failure this repo exists to fight:
this machine has twice looked completely healthy while filtering was off.

Security, stated honestly, because this page can switch off every protection here:

* It listens on the **house LAN**, on the primary address plus loopback and nothing else
  (never `0.0.0.0`, which would also publish it on the VMware host-only adapters and the
  WSL adapter). That is a deliberate widening of an earlier loopback-only design, because
  the parent needs to allow and block things from a phone in another room.
* A **password** is required. It is stored only as a scrypt hash with a random salt
  (`auth.py`), never in plaintext, never in a log.
* **There is no TLS.** Serving HTTPS needs a certificate, and generating one needs
  something outside the standard library, which this vendored embeddable Python does not
  have. So the login crosses the LAN in clear: anyone already able to sniff this network
  can read the password. That is a real limitation and it is stated in docs/PANEL.md
  rather than buried here.
* Cross-site requests are rejected on every mutation (`app.py`). A server on a LAN
  address that changes state is reachable by *any page a browser on this network loads*.
* Actions that **weaken** a protection ask for the password again; reading the dashboard
  and actions that **tighten** one do not (`controls.requires_password`). The child is
  not supposed to know this panel exists, but obscurity stops working the moment he sees
  a bookmark - and a session left open on an unlocked phone should not be a one-tap
  off-switch for the day's limits.
* Every mutation is logged to `panel.log` with a timestamp, the source address and the
  before/after values.
* It refuses to start unelevated. It writes files whose ACL is SYSTEM+Administrators;
  writing them as a standard user does not fail loudly, it fails *silently*, and a
  control that silently does nothing is the worst outcome on this page.

And the honest limit: **the child has local admin on this machine.** He can stop the
task, edit the JSON, or clear the registry policy. Nothing here prevents that. What it
does is raise the cost, make tampering visible in the logs, and give the parent one place
to see that something was turned off.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["DEFAULT_ROOT", "DEFAULT_PORT", "PANEL_LOG"]

# Same root as the guards: their config, their state, their logs, and now panel.log.
DEFAULT_ROOT = Path(r"C:\ProgramData\SafeHouse")

# 8080 is already taken on this machine; 8443 would imply a TLS service we do not serve.
DEFAULT_PORT = 8787

PANEL_LOG = "panel.log"
