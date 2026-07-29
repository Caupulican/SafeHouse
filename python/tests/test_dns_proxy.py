"""Failure-path gates for the Windows-native WSL DNS transport adapter."""

from safehouse_setup import dns_proxy


def test_proxy_rediscovers_wsl_after_a_stale_address(monkeypatch):
    addresses = iter(["172.20.0.2", "172.21.0.2"])
    attempts: list[str] = []
    router = dns_proxy.UpstreamRouter(lambda: next(addresses), lambda: "192.168.50.1")
    monkeypatch.setattr(
        dns_proxy,
        "udp_exchange",
        lambda server, _packet, _timeout: attempts.append(server) or (
            b"answer" if server == "172.21.0.2" else None
        ),
    )

    assert router.exchange_udp(b"query") == b"answer"
    assert attempts == ["172.20.0.2", "172.21.0.2"]


def test_proxy_fails_open_to_router_when_wsl_is_unavailable(monkeypatch):
    attempts: list[str] = []
    router = dns_proxy.UpstreamRouter(lambda: None, lambda: "192.168.50.1")
    monkeypatch.setattr(
        dns_proxy,
        "udp_exchange",
        lambda server, _packet, _timeout: attempts.append(server) or b"router-answer",
    )

    assert router.exchange_udp(b"query") == b"router-answer"
    assert attempts == ["192.168.50.1"]


def test_proxy_flushes_fallback_answers_when_wsl_recovers(monkeypatch):
    primary_up = False
    recovered: list[str] = []
    router = dns_proxy.UpstreamRouter(
        lambda: "172.20.0.2",
        lambda: "192.168.50.1",
        lambda: recovered.append("flush"),
    )

    def exchange(server, _packet, _timeout):
        if server == "172.20.0.2":
            return b"filtered" if primary_up else None
        return b"fallback"

    monkeypatch.setattr(dns_proxy, "udp_exchange", exchange)
    assert router.exchange_udp(b"query") == b"fallback"
    assert recovered == []

    primary_up = True
    assert router.exchange_udp(b"query") == b"filtered"
    assert recovered == ["flush"]


def test_proxy_coalesces_wsl_ip_refreshes_during_an_outage(monkeypatch):
    supplier_calls = 0

    def primary():
        nonlocal supplier_calls
        supplier_calls += 1
        return "172.20.0.2"

    router = dns_proxy.UpstreamRouter(primary, lambda: "192.168.50.1")
    monkeypatch.setattr(
        dns_proxy,
        "udp_exchange",
        lambda server, _packet, _timeout: b"fallback" if server == "192.168.50.1" else None,
    )

    for _ in range(10):
        assert router.exchange_udp(b"query") == b"fallback"

    assert supplier_calls == 2


def test_proxy_coalesces_initial_discovery_when_wsl_has_no_address(monkeypatch):
    supplier_calls = 0

    def primary():
        nonlocal supplier_calls
        supplier_calls += 1
        return None

    router = dns_proxy.UpstreamRouter(primary, lambda: "192.168.50.1")
    monkeypatch.setattr(
        dns_proxy,
        "udp_exchange",
        lambda server, _packet, _timeout: b"fallback" if server == "192.168.50.1" else None,
    )

    for _ in range(10):
        assert router.exchange_udp(b"query") == b"fallback"

    assert supplier_calls == 1


def test_proxy_listener_is_loopback_only():
    assert dns_proxy.LISTEN_ADDRESS == "127.0.0.1"
    assert dns_proxy.LISTEN_ADDRESS_V6 == "::1"
    assert dns_proxy.ADGUARD_RELAY_PORT == 3000
