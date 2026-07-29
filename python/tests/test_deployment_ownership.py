"""Architecture gates for the production deployment surfaces."""

from pathlib import Path


def test_ansible_stages_only_current_python_windows_tools():
    root = Path(__file__).resolve().parents[2]
    tasks = (
        root / "ansible" / "roles" / "windows_persistence" / "tasks" / "main.yml"
    ).read_text()
    assert "safehouse_setup" in tasks
    assert "safehouse_ytbudget" in tasks
    assert "install.py" in tasks
    assert "*.ps1" not in tasks
    installer = (root / "python" / "install.py").read_text()
    assert "retire_legacy_runtime()" in installer


def test_deployment_owns_nat_with_a_windows_transport_adapter():
    root = Path(__file__).resolve().parents[2]
    persistence = (
        root / "ansible" / "roles" / "windows_persistence" / "tasks" / "main.yml"
    ).read_text()
    production = "\n".join(
        (root / path).read_text()
        for path in (
            "pihole/docker-compose.yml",
            "adguard/docker-compose.yml",
            "scripts/reconcile-dns.sh",
            "ansible/roles/pihole/templates/env.j2",
            "ansible/roles/adguard/templates/env.j2",
        )
    )

    assert "networkingMode=nat" in persistence
    assert "dnsTunneling=false" in persistence
    assert "Remove superseded mirrored-only WSL settings" in persistence
    assert "state: absent" in persistence
    assert production.count("network_mode: host") == 2
    assert "PIHOLE_BIND_IP" not in production
    assert "ADGUARD_BIND_IP" not in production
    assert "ADGUARD_UPSTREAM=127.0.0.1:5353" in production


def test_windows_profile_detection_is_explicit_utf8_and_fails_closed():
    root = Path(__file__).resolve().parents[2]
    persistence = (
        root / "ansible" / "roles" / "windows_persistence" / "tasks" / "main.yml"
    ).read_text()

    assert "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" in persistence
    assert "[Text.UTF8Encoding]::new()" in persistence
    assert "winprofile.stdout | trim == ''" in persistence
    assert 'cmd.exe /c' not in persistence


def test_adguard_http_policy_has_one_implementation():
    root = Path(__file__).resolve().parents[2]
    for script in ("adguard/bootstrap.py", "adguard/apply-filters.py"):
        source = (root / script).read_text()
        assert "from adguard_api import api" in source
        assert "def api(" not in source
