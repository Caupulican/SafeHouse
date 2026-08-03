"""Failure-path gates for the Windows-native WSL DNS transport adapter."""

import threading
import time

from safehouse_setup import dns_proxy


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_blocked_wsl_discovery_does_not_block_dns_fallback(monkeypatch):
    discovery_started = threading.Event()
    release_discovery = threading.Event()
    completed = threading.Event()
    answers: list[bytes | None] = []

    def blocked_primary():
        discovery_started.set()
        release_discovery.wait(2)
        return "172.20.0.2"

    router = dns_proxy.UpstreamRouter(blocked_primary, lambda: "192.168.50.1")
    monkeypatch.setattr(
        dns_proxy,
        "udp_exchange",
        lambda server, _packet, _timeout: b"router-answer"
        if server == "192.168.50.1" else None,
    )

    def query():
        answers.append(router.exchange_udp(b"query"))
        completed.set()

    worker = threading.Thread(target=query, daemon=True)
    worker.start()
    assert discovery_started.wait(0.5)
    fell_back_without_waiting = completed.wait(0.1)
    release_discovery.set()
    worker.join(1)

    assert fell_back_without_waiting
    assert answers == [b"router-answer"]


def test_udp_listener_survives_a_windows_connection_reset():
    submitted: list[tuple[bytes, tuple[str, int]]] = []

    class StopLoop(BaseException):
        pass

    class Socket:
        calls = 0

        def recvfrom(self, _size):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionResetError("simulated WSAECONNRESET")
            if self.calls == 2:
                return b"next-query", ("127.0.0.1", 12345)
            raise StopLoop

    proxy = object.__new__(dns_proxy.DnsProxy)
    proxy._submit = lambda _handler, _sock, packet, client: (
        submitted.append((packet, client)) or True
    )

    try:
        proxy._udp_loop(Socket())
    except StopLoop:
        pass

    assert submitted == [(b"next-query", ("127.0.0.1", 12345))]


def test_unsupported_udp_reset_ioctl_does_not_abort_relay_startup():
    class Socket:
        def ioctl(self, _command, _value):
            raise ValueError("unsupported ioctl")

    dns_proxy.disable_udp_connreset(Socket())


def test_first_api_connection_waits_for_async_wsl_discovery(monkeypatch):
    connected = object()
    attempts: list[tuple[tuple[str, int], float]] = []
    router = dns_proxy.UpstreamRouter(lambda: "172.20.0.2", lambda: "192.168.50.1")
    monkeypatch.setattr(
        dns_proxy.socket,
        "create_connection",
        lambda address, timeout: attempts.append((address, timeout)) or connected,
    )

    assert router.connect_primary(3000, timeout=1.0) is connected
    assert attempts[0][0] == ("172.20.0.2", 3000)


def test_proxy_rediscovers_wsl_after_a_stale_address(monkeypatch):
    addresses = iter(["172.20.0.2", "172.21.0.2"])
    attempts: list[str] = []
    router = dns_proxy.UpstreamRouter(lambda: next(addresses), lambda: "192.168.50.1")
    monkeypatch.setattr(dns_proxy, "REFRESH_COOLDOWN_SEC", 0)
    monkeypatch.setattr(
        dns_proxy,
        "udp_exchange",
        lambda server, _packet, _timeout: attempts.append(server) or (
            b"answer" if server == "172.21.0.2" else
            b"fallback" if server == "192.168.50.1" else None
        ),
    )

    assert router.exchange_udp(b"query") == b"fallback"
    answers = []

    def recovered():
        answers.append(router.exchange_udp(b"query"))
        return answers[-1] == b"answer"

    assert _wait_until(recovered)
    assert "172.20.0.2" in attempts
    assert attempts[-1] == "172.21.0.2"


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

    assert _wait_until(lambda: supplier_calls == 1)


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
