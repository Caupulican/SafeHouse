"""Bounded Windows DNS transport between loopback and the current WSL address."""

from __future__ import annotations

import socket
import struct
import threading
import select
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

LISTEN_ADDRESS = "127.0.0.1"
LISTEN_ADDRESS_V6 = "::1"
DNS_PORT = 53
UPSTREAM_TIMEOUT = 2.0
ADGUARD_RELAY_PORT = 3000
REFRESH_COOLDOWN_SEC = 1.0
SIO_UDP_CONNRESET = 0x9800000C


def disable_udp_connreset(sock: socket.socket) -> None:
    """Best-effort Windows UDP reset suppression; unsupported runtimes may reject it."""
    if not hasattr(sock, "ioctl"):
        return
    try:
        sock.ioctl(SIO_UDP_CONNRESET, False)
    except (OSError, ValueError):
        pass


def udp_exchange(server: str, packet: bytes, timeout: float) -> bytes | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, DNS_PORT))
            return sock.recvfrom(65535)[0]
    except OSError:
        return None


def _read_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def tcp_exchange(server: str, packet: bytes, timeout: float) -> bytes | None:
    try:
        with socket.create_connection((server, DNS_PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            header = _read_exact(sock, 2)
            if header is None:
                return None
            return _read_exact(sock, struct.unpack("!H", header)[0])
    except OSError:
        return None


class UpstreamRouter:
    """Refresh a stale WSL address, then fail open to the LAN router."""

    def __init__(self, primary_supplier: Callable[[], str | None],
                 fallback_supplier: Callable[[], str | None],
                 recovery_callback: Callable[[], None] | None = None) -> None:
        self._primary_supplier = primary_supplier
        self._fallback_supplier = fallback_supplier
        self._recovery_callback = recovery_callback
        self._primary: str | None = None
        self._degraded = False
        self._last_refresh = 0.0
        self._refreshing = False
        self._refresh_done = threading.Event()
        self._lock = threading.Lock()

    def _mark_degraded(self) -> None:
        with self._lock:
            self._degraded = True

    def _mark_healthy(self) -> None:
        callback = None
        with self._lock:
            if self._degraded:
                self._degraded = False
                callback = self._recovery_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                pass

    def _get_primary(self) -> str | None:
        with self._lock:
            primary = self._primary
        if primary is None:
            self._schedule_refresh()
        return primary

    def _refresh_primary(self) -> str | None:
        """Invalidate a failed address and refresh it without blocking DNS traffic."""
        with self._lock:
            self._primary = None
        self._schedule_refresh()
        return None

    def _schedule_refresh(self) -> None:
        """Start at most one WSL lookup; request workers must never wait for wsl.exe."""
        with self._lock:
            now = time.monotonic()
            if self._refreshing or now - self._last_refresh < REFRESH_COOLDOWN_SEC:
                return
            self._refreshing = True
            self._last_refresh = now
            self._refresh_done.clear()

        def discover() -> None:
            try:
                primary = self._primary_supplier()
            except Exception:
                primary = None
            with self._lock:
                self._primary = primary
                self._last_refresh = time.monotonic()
                self._refreshing = False
                self._refresh_done.set()

        threading.Thread(
            target=discover,
            name="safehouse-wsl-discovery",
            daemon=True,
        ).start()

    def exchange_udp(self, packet: bytes) -> bytes | None:
        first = self._get_primary()
        if first:
            response = udp_exchange(first, packet, UPSTREAM_TIMEOUT)
            if response is not None:
                self._mark_healthy()
                return response
            refreshed = self._refresh_primary()
            if refreshed and refreshed != first:
                response = udp_exchange(refreshed, packet, UPSTREAM_TIMEOUT)
                if response is not None:
                    self._mark_healthy()
                    return response
        self._mark_degraded()
        fallback = self._fallback_supplier()
        if fallback and fallback != first:
            return udp_exchange(fallback, packet, UPSTREAM_TIMEOUT)
        return None

    def exchange_tcp(self, packet: bytes) -> bytes | None:
        first = self._get_primary()
        if first:
            response = tcp_exchange(first, packet, UPSTREAM_TIMEOUT)
            if response is not None:
                self._mark_healthy()
                return response
            refreshed = self._refresh_primary()
            if refreshed and refreshed != first:
                response = tcp_exchange(refreshed, packet, UPSTREAM_TIMEOUT)
                if response is not None:
                    self._mark_healthy()
                    return response
        self._mark_degraded()
        fallback = self._fallback_supplier()
        if fallback and fallback != first:
            return tcp_exchange(fallback, packet, UPSTREAM_TIMEOUT)
        return None

    def connect_primary(self, port: int, timeout: float = 5.0) -> socket.socket | None:
        started = time.monotonic()
        first = self._get_primary()
        candidates = [first] if first else []
        for server in candidates:
            try:
                connected = socket.create_connection((server, port), timeout=timeout)
                self._mark_healthy()
                return connected
            except OSError:
                pass
        if first:
            self._refresh_primary()
        else:
            # `_get_primary` already started discovery. Do not invalidate a result that
            # may have landed between that return and this line.
            self._schedule_refresh()
        # API calls are not on the DNS request path, so they can wait briefly for the
        # already-asynchronous WSL discovery. Without this, the first YouTube toggle
        # after logon always hit a deliberate empty cache and failed UNREACHABLE even
        # though AdGuard was healthy milliseconds later.
        remaining = max(0.0, timeout - (time.monotonic() - started))
        self._refresh_done.wait(remaining)
        with self._lock:
            refreshed = self._primary
        if refreshed and refreshed not in candidates:
            try:
                remaining = max(0.001, timeout - (time.monotonic() - started))
                connected = socket.create_connection((refreshed, port), timeout=remaining)
                self._mark_healthy()
                return connected
            except OSError:
                pass
        self._mark_degraded()
        return None


class DnsProxy:
    """Loopback-only UDP/TCP DNS relay with bounded worker concurrency."""

    def __init__(self, router: UpstreamRouter, workers: int = 32) -> None:
        self.router = router
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="safehouse-dns")
        self.slots = threading.BoundedSemaphore(workers * 2)
        self.udp_sockets = [
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
            socket.socket(socket.AF_INET6, socket.SOCK_DGRAM),
        ]
        self.tcp_sockets = [
            socket.socket(socket.AF_INET, socket.SOCK_STREAM),
            socket.socket(socket.AF_INET6, socket.SOCK_STREAM),
        ]

    def start(self) -> None:
        # Windows reports an ICMP "port unreachable" from a departed UDP client as
        # WSAECONNRESET on the next recvfrom(), which otherwise kills the listener.
        for sock in self.udp_sockets:
            disable_udp_connreset(sock)
        for sock in self.tcp_sockets:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_sockets[0].bind((LISTEN_ADDRESS, DNS_PORT))
        self.udp_sockets[1].bind((LISTEN_ADDRESS_V6, DNS_PORT))
        self.tcp_sockets[0].bind((LISTEN_ADDRESS, DNS_PORT))
        self.tcp_sockets[1].bind((LISTEN_ADDRESS_V6, DNS_PORT))
        for sock in self.tcp_sockets:
            sock.listen(64)
        for sock in self.udp_sockets:
            threading.Thread(target=self._udp_loop, args=(sock,), daemon=True).start()
        for sock in self.tcp_sockets:
            threading.Thread(target=self._tcp_loop, args=(sock,), daemon=True).start()

    def _udp_loop(self, sock: socket.socket) -> None:
        while True:
            try:
                packet, client = sock.recvfrom(65535)
            except ConnectionResetError:
                continue
            self._submit(self._handle_udp, sock, packet, client)

    def _submit(self, handler, *args) -> bool:
        if not self.slots.acquire(blocking=False):
            return False

        def bounded_handler() -> None:
            try:
                handler(*args)
            finally:
                self.slots.release()

        self.pool.submit(bounded_handler)
        return True

    def _handle_udp(self, sock: socket.socket, packet: bytes, client) -> None:
        response = self.router.exchange_udp(packet)
        if response is not None:
            sock.sendto(response, client)

    def _tcp_loop(self, sock: socket.socket) -> None:
        while True:
            client, _ = sock.accept()
            if not self._submit(self._handle_tcp, client):
                client.close()

    def _handle_tcp(self, client: socket.socket) -> None:
        with client:
            client.settimeout(UPSTREAM_TIMEOUT + 1)
            header = _read_exact(client, 2)
            if header is None:
                return
            packet = _read_exact(client, struct.unpack("!H", header)[0])
            if packet is None:
                return
            response = self.router.exchange_tcp(packet)
            if response is not None:
                client.sendall(struct.pack("!H", len(response)) + response)

    def wait(self) -> None:
        threading.Event().wait()


class TcpRelay:
    """Loopback TCP relay for AdGuard's API across the same dynamic WSL boundary."""

    def __init__(self, router: UpstreamRouter, port: int = ADGUARD_RELAY_PORT,
                 workers: int = 8) -> None:
        self.router = router
        self.port = port
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="safehouse-api")
        self.slots = threading.BoundedSemaphore(workers * 2)

    def start(self) -> None:
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((LISTEN_ADDRESS, self.port))
        self.listener.listen(32)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            client, _ = self.listener.accept()
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            self.pool.submit(self._handle, client)

    def _handle(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            upstream = self.router.connect_primary(self.port)
            if upstream is None:
                return
            with client, upstream:
                while True:
                    readable, _, _ = select.select([client, upstream], [], [], 30)
                    if not readable:
                        continue
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is client else client).sendall(data)
        except OSError:
            return
        finally:
            if upstream is None:
                client.close()
            self.slots.release()
