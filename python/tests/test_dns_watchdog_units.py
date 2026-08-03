"""Architecture gates for the DNS stack watchdog.

The filtering chain going down is not hypothetical and it is not loud: the Windows relay
fails open to the LAN router, so name resolution keeps working with nothing filtered
behind it. Docker's `restart: unless-stopped` is set on both containers and recovers the
stack only *sometimes* -- on 2026-08-01 a dockerd restart left AdGuard Home dead with
RestartCount=0 while Pi-hole came back, and a second restart minutes later recovered both.
These gates pin the parts that do not depend on Docker getting it right.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = ROOT / "automation" / "safehouse-dns.service"
TIMER = ROOT / "automation" / "safehouse-dns.timer"
INSTALLER = ROOT / "scripts" / "install-dns-service.sh"
AUTOMATION_ROLE = ROOT / "ansible" / "roles" / "automation" / "tasks" / "main.yml"


def test_service_is_a_oneshot_reconcile_ordered_after_docker():
    unit = SERVICE.read_text()
    assert "Type=oneshot" in unit
    assert "TimeoutStartSec=5min" in unit
    assert "After=docker.service" in unit
    assert "__RECONCILE_SH__" in unit
    # The reconciler finds the compose projects and the AdGuard credentials under $HOME,
    # which a systemd service is not guaranteed to inherit.
    assert "Environment=HOME=__HOME__" in unit
    assert "Environment=PIHOLE_DIR=__HOME__/pihole" in unit
    assert "Environment=ADGUARD_DIR=__HOME__/adguard" in unit


def test_timer_covers_both_boot_and_mid_session_death():
    unit = TIMER.read_text()
    # Boot: WSL restarts and the containers do not come back.
    assert "OnBootSec=" in unit
    # Mid-session: the stack dies while the machine stays up -- the same outage, later.
    assert "OnUnitActiveSec=" in unit
    assert "WantedBy=timers.target" in unit


def test_installer_renders_every_placeholder_the_units_declare():
    installer = INSTALLER.read_text()
    declared = set()
    for unit in (SERVICE.read_text(), TIMER.read_text()):
        for token in unit.split():
            for candidate in ("__USER__", "__REPO__", "__HOME__", "__RECONCILE_SH__"):
                if candidate in token:
                    declared.add(candidate)
    # An unrendered placeholder is a unit that silently never works.
    for placeholder in declared:
        assert f"s#{placeholder}#" in installer, f"{placeholder} is never rendered"


def test_installer_refuses_to_pretend_cron_can_do_this():
    """The weekly auto-update falls back to cron; a 5-minute watchdog must not.

    Quietly degrading to a cadence this cannot promise would recreate the original
    failure: a filter everyone believes is being watched, and is not.
    """
    installer = INSTALLER.read_text()
    assert "/etc/cron.d" not in installer
    assert "systemd is not available" in installer


def test_normal_adguard_deploy_installs_the_watchdog():
    """A recovery mechanism only mentioned in a runbook is not deployed protection."""
    role = AUTOMATION_ROLE.read_text()
    assert "scripts/install-dns-service.sh" in role
    assert "safehouse_enable_adguard" in role
    assert "SAFEHOUSE_RUN_USER" in role
    assert "SAFEHOUSE_RUN_HOME" in role


def test_service_uses_the_stable_installed_reconciler_not_the_checkout_copy():
    installer = INSTALLER.read_text()
    assert ".local/bin/safehouse-reconcile-dns" in installer
    assert 'RECONCILE="$REPO/scripts/reconcile-dns.sh"' not in installer
