"""Loopback-only network guard for offline (test-double) runs.

While active, DNS resolution and socket connects to anything other than localhost are refused and
recorded. The MCP server on 127.0.0.1 stays reachable; an accidental call to an LLM provider
(generativelanguage.googleapis.com, openrouter.ai, ...) fails and is listed in `blocked`.
It is a safety check for offline runs, not a sandbox: it patches Python's socket module only.
"""
from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class NetworkBlockedError(OSError):
    pass


@contextmanager
def loopback_only():
    blocked: list[str] = []
    lock = threading.Lock()
    real_getaddrinfo, real_connect, real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex

    def check(host) -> None:
        host = host.decode() if isinstance(host, bytes) else str(host)
        if host not in LOOPBACK and not host.startswith("127."):
            with lock:
                blocked.append(host)
            raise NetworkBlockedError(f"offline test-double run: network access to {host!r} is blocked")

    def guarded_getaddrinfo(host, *args, **kwargs):
        check(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        if isinstance(address, tuple):
            check(address[0])
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if isinstance(address, tuple):
            check(address[0])
        return real_connect_ex(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    try:
        yield blocked
    finally:
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = (
            real_getaddrinfo, real_connect, real_connect_ex)
