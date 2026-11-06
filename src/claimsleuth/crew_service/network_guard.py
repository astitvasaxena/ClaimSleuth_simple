"""Network guard for CrewAI runs.

While active, DNS resolution and socket connects to anything other than localhost are refused and
recorded. The MCP server on 127.0.0.1 stays reachable; an accidental call to an LLM provider
(generativelanguage.googleapis.com, openrouter.ai, ...) fails and is listed in `blocked`.

`allow_hosts` names the only extra hosts a run may reach (live Gemini mode: the Gemini API host). Their
addresses are allowed only as returned by a DNS lookup of that host made inside the guard.
It is a safety check, not a sandbox: it patches Python's socket module only.
"""
from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class NetworkBlockedError(OSError):
    pass


@contextmanager
def loopback_only(allow_hosts: tuple[str, ...] = ()):
    blocked: list[str] = []
    allowed_hosts = {h.lower() for h in allow_hosts}
    allowed_addresses: set[str] = set()
    lock = threading.Lock()
    real_getaddrinfo, real_connect, real_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex

    def check(host) -> None:
        host = host.decode() if isinstance(host, bytes) else str(host)
        if host in LOOPBACK or host.startswith("127.") or host.lower() in allowed_hosts:
            return
        with lock:
            if host in allowed_addresses:
                return
            blocked.append(host)
        raise NetworkBlockedError(f"network access to {host!r} is blocked for this CrewAI run")

    def guarded_getaddrinfo(host, *args, **kwargs):
        check(host)
        result = real_getaddrinfo(host, *args, **kwargs)
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name.lower() in allowed_hosts:
            with lock:
                allowed_addresses.update(str(info[4][0]) for info in result)
        return result

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
