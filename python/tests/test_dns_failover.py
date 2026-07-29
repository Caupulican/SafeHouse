"""Failure-path gates for Windows DNS configuration."""

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
    monkeypatch.setattr(
        dns,
        "set_adapter_dns",
        lambda _iface, server: calls.append(("adapter", server)) or server == "192.168.50.1",
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
    assert calls == [
        ("nrpt", None),
        ("adapter", "127.0.0.1"),
        ("adapter", "192.168.50.1"),
    ]


def test_system_resolution_failure_rolls_back_without_crashing(monkeypatch):
    monkeypatch.setattr(dns, "reconcile_wsl_dns_stack", lambda: True)
    monkeypatch.setattr(dns, "default_route", lambda: (5, "192.168.50.1"))
    monkeypatch.setattr(dns, "interface_name", lambda _index: "Ethernet")
    monkeypatch.setattr(dns, "_probe_server", lambda *_args: True)
    monkeypatch.setattr(dns, "set_adapter_dns", lambda *_args: True)
    monkeypatch.setattr(dns, "set_adapter_dns_v6", lambda *_args: True)
    monkeypatch.setattr(dns, "reset_adapter_dns_v6", lambda *_args: True)
    monkeypatch.setattr(dns, "set_nrpt", lambda *_args: True)
    monkeypatch.setattr(dns, "disable_browser_doh", lambda: None)
    monkeypatch.setattr(dns, "flush_dns", lambda: None)
    monkeypatch.setattr(dns, "resolves", lambda: False)
    monkeypatch.setattr(dns.time, "sleep", lambda _seconds: None, raising=False)

    result = dns.apply(wait_for_wsl=1)

    assert result["mode"] == "rolled-back"
    assert result["ok"] is False
