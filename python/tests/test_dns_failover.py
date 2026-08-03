"""Failure-path gates for Windows DNS configuration."""

import argparse

from safehouse_setup import __main__ as cli
from safehouse_setup import dns


def test_system_resolution_probe_is_bounded(monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        dns,
        "powershell",
        lambda script, timeout: (calls.append((script, timeout)) or (0, "")),
    )

    assert dns.resolves("github.com", timeout=3) is True
    assert calls == [
        (
            "Resolve-DnsName -Name 'github.com' -Type A -DnsOnly -QuickTimeout "
            "-ErrorAction Stop | Out-Null",
            5,
        )
    ]


def test_adapter_write_failure_rolls_back_without_installing_nrpt(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(dns, "reconcile_wsl_dns_stack", lambda: True)
    monkeypatch.setattr(dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(dns, "_probe_server", lambda *_args: True)
    monkeypatch.setattr(dns, "_probe_filter", lambda *_args: True)
    monkeypatch.setattr(
        dns,
        "set_adapter_dns",
        lambda _iface, server: calls.append(("adapter", server)) or False,
    )
    monkeypatch.setattr(
        dns,
        "reset_adapter_dns",
        lambda _iface: calls.append(("adapter", "dhcp")) or True,
    )
    monkeypatch.setattr(
        dns,
        "set_nrpt",
        lambda server: calls.append(("nrpt", server)) or True,
    )
    monkeypatch.setattr(dns, "flush_dns", lambda: None)
    monkeypatch.setattr(dns, "reset_adapter_dns_v6", lambda _iface: True)

    result = dns.apply(wait_for_wsl=1)

    assert result["mode"] == "rolled-back"
    assert result["ok"] is False
    assert result["filtering"] is False
    assert calls == [
        ("nrpt", None),
        ("adapter", "127.0.0.1"),
        ("adapter", "dhcp"),
    ]


def test_system_resolution_failure_rolls_back_without_crashing(monkeypatch):
    monkeypatch.setattr(dns, "reconcile_wsl_dns_stack", lambda: True)
    monkeypatch.setattr(dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(dns, "_probe_server", lambda *_args: True)
    monkeypatch.setattr(dns, "_probe_filter", lambda *_args: True)
    monkeypatch.setattr(dns, "set_adapter_dns", lambda *_args: True)
    monkeypatch.setattr(dns, "set_adapter_dns_v6", lambda *_args: True)
    monkeypatch.setattr(dns, "reset_adapter_dns", lambda *_args: True)
    monkeypatch.setattr(dns, "reset_adapter_dns_v6", lambda *_args: True)
    monkeypatch.setattr(dns, "set_nrpt", lambda *_args: True)
    monkeypatch.setattr(dns, "disable_browser_doh", lambda: None)
    monkeypatch.setattr(dns, "flush_dns", lambda: None)
    monkeypatch.setattr(dns, "resolves", lambda: False)
    monkeypatch.setattr(dns.time, "sleep", lambda _seconds: None, raising=False)

    result = dns.apply(wait_for_wsl=1)

    assert result["mode"] == "rolled-back"
    assert result["ok"] is False
    assert result["filtering"] is False


def test_router_failsafe_resolves_but_reports_that_nothing_is_filtered(monkeypatch):
    """The WSL stack is down: DNS works via the router, and nothing is blocked.

    This is the state the machine sat in for fourteen hours on 2026-08-01 while the
    Play Games ads came back. `ok` is True by design -- the fallback worked -- so
    `filtering` is what has to carry the bad news.
    """
    monkeypatch.setattr(dns, "reconcile_wsl_dns_stack", lambda: False)
    monkeypatch.setattr(dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(dns, "_probe_server", lambda *_args: False)
    resets: list[str] = []
    monkeypatch.setattr(dns, "reset_adapter_dns",
                        lambda *_args: resets.append("IPv4") or True)
    monkeypatch.setattr(dns, "reset_adapter_dns_v6",
                        lambda *_args: resets.append("IPv6") or True)
    monkeypatch.setattr(dns, "set_nrpt", lambda *_args: True)
    monkeypatch.setattr(dns, "disable_browser_doh", lambda: None)
    monkeypatch.setattr(dns, "flush_dns", lambda: None)
    monkeypatch.setattr(dns, "resolves", lambda: True)

    result = dns.apply(wait_for_wsl=1)

    assert result["mode"] == "router-failsafe"
    assert result["dns"] == "dhcp"
    assert result["ok"] is True
    assert result["filtering"] is False
    assert result["stack_reconciled"] is False
    assert resets == ["IPv4", "IPv6"]


def test_live_relay_fails_open_without_rewriting_the_adapter(monkeypatch):
    monkeypatch.setattr(dns, "reconcile_wsl_dns_stack", lambda: False)
    monkeypatch.setattr(dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(dns, "_probe_server", lambda *_args: True)
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(dns, "set_adapter_dns",
                        lambda _iface, server: writes.append(("IPv4", server)) or True)
    monkeypatch.setattr(dns, "set_adapter_dns_v6",
                        lambda _iface, server: writes.append(("IPv6", server)) or True)
    monkeypatch.setattr(dns, "set_nrpt", lambda *_args: True)
    monkeypatch.setattr(dns, "disable_browser_doh", lambda: None)
    monkeypatch.setattr(dns, "flush_dns", lambda: None)
    monkeypatch.setattr(dns, "resolves", lambda: True)

    result = dns.apply(wait_for_wsl=1)

    assert result["mode"] == "relay-failsafe"
    assert result["dns"] == "127.0.0.1"
    assert result["ok"] is True
    assert result["filtering"] is False
    assert writes == [("IPv4", "127.0.0.1"), ("IPv6", "::1")]


def test_cli_shouts_when_dns_works_but_filters_nothing(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.dns,
        "apply",
        lambda prefer=None: {"mode": "router-failsafe", "ok": True, "filtering": False},
    )

    rc = cli.cmd_dns(argparse.Namespace(status=False, clear=False, server=None))

    captured = capsys.readouterr()
    assert rc == 2
    assert "NO FILTERING" in captured.err
    assert "safehouse-reconcile-dns" in captured.err


def test_cli_stays_quiet_when_the_filter_is_actually_in_the_path(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.dns,
        "apply",
        lambda prefer=None: {"mode": "filtering-resolver", "ok": True, "filtering": True},
    )

    rc = cli.cmd_dns(argparse.Namespace(status=False, clear=False, server=None))

    assert rc == 0
    assert capsys.readouterr().err == ""
