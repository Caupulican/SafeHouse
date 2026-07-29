"""Production-shaped test for the WSL DNS stack reconciler."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_reconciler_uses_stable_host_network_without_mutating_addresses(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "reconcile-dns.sh"
    pihole = tmp_path / "pihole"
    adguard = tmp_path / "adguard"
    bindir = tmp_path / "bin"
    for directory in (pihole, adguard, bindir):
        directory.mkdir()
    (pihole / ".env").write_text("PIHOLE_DNS_PORT=5353\n")
    (adguard / ".env").write_text("ADGUARD_UPSTREAM=127.0.0.1:5353\n")
    before = [(directory / ".env").read_bytes() for directory in (pihole, adguard)]
    for name in ("docker-compose.yml",):
        (pihole / name).write_text("services: {}\n")
        (adguard / name).write_text("services: {}\n")
    (adguard / "bootstrap.py").write_text("# fixture\n")
    (adguard / ".admin-password").write_text("secret\n")
    log = tmp_path / "commands.log"
    for command in ("docker", "python3"):
        executable = bindir / command
        executable.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"{command} $*\" >> '{log}'\n"
        )
        executable.chmod(0o755)

    env = os.environ | {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PIHOLE_DIR": str(pihole),
        "ADGUARD_DIR": str(adguard),
    }
    completed = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert [(directory / ".env").read_bytes() for directory in (pihole, adguard)] == before
    commands = log.read_text()
    assert commands.count("docker compose up -d") == 2
    assert "--upstream 127.0.0.1:5353" in commands


def test_reconciler_has_no_dynamic_wsl_address_input():
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts" / "reconcile-dns.sh").read_text()
    assert "hostname -I" not in script
    assert "SAFEHOUSE_WSL_IP" not in script
    assert "_BIND_IP" not in script
    assert "docker network" not in script
