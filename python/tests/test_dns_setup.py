"""Regression tests for the single-owner Windows/WSL DNS setup path."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from safehouse_setup import __main__ as setup_cli
from safehouse_setup import dns
from safehouse_setup.migration import legacy_files, retire_legacy_runtime


def test_nrpt_cleanup_removes_every_safehouse_alias():
    script = dns._remove_rule_script()
    assert "-like 'SafeHouse:*'" in script


@pytest.mark.parametrize(
    ("setter", "family", "server"),
    [
        (dns.set_adapter_dns, "IPv4", "127.0.0.1"),
        (dns.set_adapter_dns_v6, "IPv6", "::1"),
    ],
)
def test_adapter_dns_setters_do_not_rewrite_an_already_correct_adapter(
    monkeypatch, setter, family, server
):
    monkeypatch.setattr(
        dns, "adapter_dns_servers", lambda _iface, seen_family: [server]
        if seen_family == family else []
    )
    monkeypatch.setattr(
        dns,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an idempotent apply must not enter the DNS configuration lock")
        ),
    )

    assert setter("Ethernet", server) is True


def test_relay_captures_dhcp_dns_instead_of_assuming_the_gateway_is_dns(monkeypatch):
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(
        dns,
        "adapter_dns_servers",
        lambda _iface, family: ["10.20.30.40", "10.20.30.1"] if family == "IPv4" else [],
    )
    assert dns.fallback_resolver(5, "10.20.30.1") == "10.20.30.40"


def test_relay_uses_gateway_when_adapter_is_already_on_safehouse(monkeypatch):
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(
        dns, "adapter_dns_servers", lambda _iface, _family: ["127.0.0.1"]
    )
    assert dns.fallback_resolver(5, "192.168.50.1") == "192.168.50.1"


def test_apply_reconciles_stack_before_probing_stable_localhost(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        dns,
        "reconcile_wsl_dns_stack",
        lambda: events.append("reconcile") or True,
        raising=False,
    )
    monkeypatch.setattr(dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(
        dns,
        "_probe_server",
        lambda server, *_args: events.append(f"probe:{server}") or True,
    )
    monkeypatch.setattr(
        dns,
        "_probe_filter",
        lambda server, *_args: events.append(f"filter:{server}") or True,
    )
    monkeypatch.setattr(dns, "set_adapter_dns", lambda *_args: True)
    v6_servers: list[str] = []
    nrpt_servers: list[str | None] = []
    monkeypatch.setattr(
        dns, "set_adapter_dns_v6", lambda _iface, server: v6_servers.append(server) or True
    )
    monkeypatch.setattr(
        dns, "set_nrpt", lambda server: nrpt_servers.append(server) or True
    )
    monkeypatch.setattr(dns, "disable_browser_doh", lambda: None)
    monkeypatch.setattr(dns, "flush_dns", lambda: None)
    monkeypatch.setattr(dns, "resolves", lambda: True)

    result = dns.apply(wait_for_wsl=1)

    assert events[:3] == ["reconcile", "probe:127.0.0.1", "filter:127.0.0.1"]
    assert result["mode"] == "filtering-resolver"
    assert result["dns"] == "127.0.0.1"
    assert result["stack_reconciled"] is True
    assert v6_servers == ["::1"]
    assert nrpt_servers == [None]


def test_supervised_apply_skips_the_recursive_system_probe(monkeypatch):
    monkeypatch.setattr(dns, "reconcile_wsl_dns_stack", lambda: True)
    monkeypatch.setattr(dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(dns, "_probe_server", lambda *_args: True)
    monkeypatch.setattr(dns, "_probe_filter", lambda *_args: True)
    monkeypatch.setattr(dns, "set_adapter_dns", lambda *_args: True)
    monkeypatch.setattr(dns, "set_adapter_dns_v6", lambda *_args: True)
    monkeypatch.setattr(dns, "set_nrpt", lambda *_args: True)
    monkeypatch.setattr(dns, "disable_browser_doh", lambda: None)
    monkeypatch.setattr(dns, "flush_dns", lambda: None)
    monkeypatch.setattr(
        dns, "resolves", lambda: (_ for _ in ()).throw(AssertionError("recursive probe"))
    )

    assert dns.apply(wait_for_wsl=1, verify_system=False)["ok"] is True


def test_filter_probe_requires_a_real_sinkhole_answer(monkeypatch):
    qname = b"\x0bdoubleclick\x03net\x00"
    header = b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
    question = qname + b"\x00\x01\x00\x01"
    answer_prefix = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04"

    monkeypatch.setattr(
        dns, "_query_server", lambda *_args: header + question + answer_prefix + b"\x00" * 4
    )
    assert dns._probe_filter("127.0.0.1") is True

    monkeypatch.setattr(
        dns, "_query_server", lambda *_args: header + question + answer_prefix + b"\x01\x02\x03\x04"
    )
    assert dns._probe_filter("127.0.0.1") is False


def test_logon_relay_never_mutates_windows_dns(monkeypatch):
    events: list[str] = []

    class Router:
        def __init__(self, _primary, fallback):
            assert fallback() == "192.168.50.1"

    class Proxy:
        def __init__(self, _router):
            pass

        def start(self):
            events.append("dns-start")

        def wait(self):
            events.append("wait")

    class Relay(Proxy):
        def start(self):
            events.append("api-start")

    monkeypatch.setattr(setup_cli.dns_proxy, "DnsProxy", Proxy)
    monkeypatch.setattr(setup_cli.dns_proxy, "TcpRelay", Relay)
    monkeypatch.setattr(setup_cli.dns_proxy, "UpstreamRouter", Router)
    monkeypatch.setattr(
        setup_cli.dns,
        "default_route",
        lambda: (_ for _ in ()).throw(
            AssertionError("DNS requests must not query Windows routing")
        ),
    )
    monkeypatch.setattr(
        setup_cli.dns,
        "apply",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the logon relay must not enter the DNS configuration lock")
        ),
    )

    assert setup_cli.cmd_serve(Namespace(fallback="192.168.50.1")) == 0
    assert events == ["dns-start", "api-start", "wait"]


def test_persist_retires_legacy_owner_before_registering_current_task(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(setup_cli, "is_admin", lambda: True)
    monkeypatch.setattr(
        setup_cli,
        "retire_legacy_runtime",
        lambda: calls.append(["retire-legacy"]),
        raising=False,
    )
    monkeypatch.setattr(
        setup_cli,
        "windowless_python",
        lambda: Path("C:/SafeHouse/python/pythonw.exe"),
    )
    monkeypatch.setattr(setup_cli.dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(
        setup_cli.dns, "fallback_resolver", lambda *_args: "192.168.50.1"
    )
    monkeypatch.setattr(setup_cli, "wait_for_relay", lambda: True)
    monkeypatch.setattr(
        setup_cli,
        "powershell",
        lambda script, **_kwargs: (calls.append(script) or (0, "")),
    )

    assert setup_cli.cmd_persist(Namespace(remove=False)) == 0
    assert calls[0] == ["retire-legacy"]
    assert any("SafeHouse-DNS" in str(call) for call in calls[1:])


def test_logon_task_uses_windowless_python_when_available(tmp_path):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.touch()
    pythonw.touch()

    assert setup_cli.windowless_python(python) == pythonw


def test_logon_task_refuses_a_visible_python_fallback(tmp_path):
    python = tmp_path / "python.exe"
    python.touch()

    with pytest.raises(FileNotFoundError):
        setup_cli.windowless_python(python)


def test_persist_registers_a_hidden_windowless_bounded_task(monkeypatch, tmp_path):
    calls: list[str] = []
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    monkeypatch.setattr(setup_cli, "is_admin", lambda: True)
    monkeypatch.setattr(setup_cli, "retire_legacy_runtime", lambda: None)
    monkeypatch.setattr(setup_cli, "windowless_python", lambda: pythonw)
    monkeypatch.setattr(setup_cli.dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(
        setup_cli.dns, "fallback_resolver", lambda *_args: "192.168.50.1"
    )
    monkeypatch.setattr(setup_cli, "wait_for_relay", lambda: True)
    monkeypatch.setattr(
        setup_cli,
        "powershell",
        lambda script, **_kwargs: (calls.append(script) or (0, "")),
        raising=False,
    )

    assert setup_cli.cmd_persist(Namespace(remove=False)) == 0

    script = calls[0]
    assert "New-ScheduledTaskAction" in script
    assert str(pythonw) in script
    assert "-Hidden" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-RestartCount 3" in script
    assert "-m safehouse_setup serve" in script
    assert "--fallback 192.168.50.1" in script


def test_persist_refuses_to_repoint_dns_when_the_relay_did_not_start(monkeypatch, tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    monkeypatch.setattr(setup_cli, "is_admin", lambda: True)
    monkeypatch.setattr(setup_cli, "retire_legacy_runtime", lambda: None)
    monkeypatch.setattr(setup_cli, "windowless_python", lambda: pythonw)
    monkeypatch.setattr(setup_cli.dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(
        setup_cli.dns, "fallback_resolver", lambda *_args: "192.168.50.1"
    )
    monkeypatch.setattr(setup_cli, "powershell", lambda *_args, **_kwargs: (0, ""))
    monkeypatch.setattr(setup_cli, "wait_for_relay", lambda: False)

    assert setup_cli.cmd_persist(Namespace(remove=False)) == 1


def test_legacy_files_are_deleted_idempotently(tmp_path, monkeypatch):
    old_dns = tmp_path / "set-dns.ps1"
    old_task = tmp_path / "mktask.ps1"
    old_dns.write_text("obsolete")
    old_task.write_text("obsolete")
    monkeypatch.setattr("safehouse_setup.migration.run", lambda *_a, **_k: (1, "absent"))

    retire_legacy_runtime((old_dns, old_task))
    retire_legacy_runtime((old_dns, old_task))

    assert not old_dns.exists()
    assert not old_task.exists()


def test_migration_covers_superseded_budget_scripts(monkeypatch, tmp_path):
    monkeypatch.setenv("SystemDrive", str(tmp_path))
    names = {path.name for path in legacy_files()}
    assert {"common.ps1", "youtube-budget.ps1", "youtube-budget-ctl.ps1"} <= names


def test_legacy_runtime_files_are_absent_from_the_repository():
    root = Path(__file__).resolve().parents[2]
    assert not (root / "scripts" / "bootstrap.sh").exists()

    forbidden = (
        "set-dns.ps1",
        "mktask.ps1",
        "parental-toggle.ps1",
        "install-task.ps1",
        "youtube-budget-ctl.ps1",
        "PiholeGPG-DNS",
    )
    allowed = {
        root / "python" / "safehouse_setup" / "migration.py",
        Path(__file__).resolve(),
    }
    offenders: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path in allowed or ".git" in path.parts:
            continue
        if any(part in {".venv-dev", "__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{path.relative_to(root)}: {marker}")
    assert offenders == []


def test_pihole_deploy_retries_transient_database_lock():
    root = Path(__file__).resolve().parents[2]
    tasks = (root / "ansible" / "roles" / "pihole" / "tasks" / "main.yml").read_text()
    blocklist_task = tasks.split("- name: Load blocklists and rebuild gravity", 1)[1]
    blocklist_task = blocklist_task.split("- name:", 1)[0]
    assert "until: _grav.rc == 0" in blocklist_task
    assert "retries:" in blocklist_task
    assert "delay:" in blocklist_task
