"""Production-shaped test for the WSL DNS stack reconciler."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "reconcile-dns.sh"

# A fake `docker` that logs its arguments and succeeds, so the script's command sequence
# is observable without a container runtime.
_LOGGING_STUB = "#!/bin/sh\nprintf '%s\\n' \"{command} $*\" >> '{log}'\n"


def _stage(tmp_path: Path, docker_stub: str | None = None) -> tuple[dict, Path]:
    """Build a deployed-looking stack and return (env, command log path)."""
    pihole = tmp_path / "pihole"
    adguard = tmp_path / "adguard"
    bindir = tmp_path / "bin"
    for directory in (pihole, adguard, bindir):
        directory.mkdir()
    (pihole / ".env").write_text("PIHOLE_DNS_PORT=5353\n")
    (adguard / ".env").write_text("ADGUARD_UPSTREAM=127.0.0.1:5353\n")
    for name in ("docker-compose.yml",):
        (pihole / name).write_text("services: {}\n")
        (adguard / name).write_text("services: {}\n")
    (adguard / "bootstrap.py").write_text("# fixture\n")
    (adguard / ".admin-password").write_text("secret\n")

    log = tmp_path / "commands.log"
    for command in ("docker", "python3"):
        executable = bindir / command
        body = docker_stub if command == "docker" and docker_stub else _LOGGING_STUB
        executable.write_text(body.format(command=command, log=log))
        executable.chmod(0o755)

    env = os.environ | {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PIHOLE_DIR": str(pihole),
        "ADGUARD_DIR": str(adguard),
    }
    return env, log


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )


def test_reconciler_uses_stable_host_network_without_mutating_addresses(tmp_path):
    env, log = _stage(tmp_path)
    pihole = Path(env["PIHOLE_DIR"])
    adguard = Path(env["ADGUARD_DIR"])
    before = [(directory / ".env").read_bytes() for directory in (pihole, adguard)]

    completed = _run(env)

    assert completed.returncode == 0, completed.stderr
    assert [(directory / ".env").read_bytes() for directory in (pihole, adguard)] == before
    commands = log.read_text()
    assert commands.count("docker compose up -d") == 2
    assert "--upstream 127.0.0.1:5353" in commands
    assert "docker exec adguardhome sh -c nslookup doubleclick.net" in commands


def test_reconciler_pins_the_restart_policy_on_the_live_containers(tmp_path):
    """`restart:` in the compose file is applied at CREATE only.

    A container created before that line landed keeps `no` and does not come back after a
    WSL shutdown, which takes the whole filter offline while DNS still appears to work.
    """
    env, log = _stage(tmp_path)

    completed = _run(env)

    assert completed.returncode == 0, completed.stderr
    assert "docker update --restart unless-stopped pihole adguardhome" in log.read_text()


def test_restart_policy_failure_never_fails_the_reconcile(tmp_path):
    """A non-zero exit here would be worse than the drift it reports.

    The caller reads a failed reconcile as "the stack is down" and points Windows at the
    unfiltered router -- causing the outage the policy pin exists to prevent.
    """
    stub = (
        "#!/bin/sh\n"
        "printf '%s\\n' \"{command} $*\" >> '{log}'\n"
        'if [ "$1" = "update" ]; then exit 1; fi\n'
    )
    env, log = _stage(tmp_path, docker_stub=stub)

    completed = _run(env)

    assert completed.returncode == 0, completed.stderr
    assert "WARNING: restart policy not set" in completed.stderr
    # The health checks still ran, so the reconcile did not bail out early.
    assert "nslookup github.com" in log.read_text()


def test_reconciler_has_no_dynamic_wsl_address_input():
    script = SCRIPT.read_text()
    assert "hostname -I" not in script
    assert "SAFEHOUSE_WSL_IP" not in script
    assert "_BIND_IP" not in script
    assert "docker network" not in script
