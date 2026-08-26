"""Socket helpers for the hardware-in-the-loop tests.

Kept out of conftest.py because tests/conftest.py shares that basename, and
`from conftest import ...` then resolves to whichever pytest saw first.
"""

from __future__ import annotations

import os
import re
import socket
import time

BANNER_RE = re.compile(rb"DEVICE:(RADIOBRIDGE|RADIORELAY):relay:([^:]+):([0-9A-Fa-f]+)")
PORT = int(os.environ.get("MBRELAY_HIL_PORT", "8799"))


def connect(port: int = PORT, timeout: float = 20.0) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def read_until(sock: socket.socket, pattern: bytes, timeout: float = 8.0):
    rx = re.compile(pattern)
    buf = bytearray()
    end = time.time() + timeout
    sock.settimeout(0.3)
    while time.time() < end:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
        if match := rx.search(buf):
            return match, bytes(buf)
    return None, bytes(buf)


def read_exactly(sock: socket.socket, n: int, timeout: float = 8.0) -> bytes:
    buf = bytearray()
    end = time.time() + timeout
    sock.settimeout(0.3)
    while len(buf) < n and time.time() < end:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def expect(sock: socket.socket, send: bytes, pattern: bytes, timeout: float = 8.0):
    sock.sendall(send)
    match, buf = read_until(sock, pattern, timeout)
    assert match, f"never saw {pattern!r} after {send!r}; got {buf[-200:]!r}"
    return match


def drain(sock: socket.socket, quiet_for: float = 0.4, limit: float = 3.0) -> bytes:
    """Read until the board goes quiet.

    Needed before measuring a payload: `expect` returns the moment its pattern
    matches, so the rest of that reply line (typically the trailing CRLF) is
    still in flight and would otherwise be counted as the first bytes of the
    next thing you read.
    """
    buf = bytearray()
    sock.settimeout(quiet_for)
    end = time.time() + limit
    while time.time() < end:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def bind(port: int = PORT):
    """Connect and read the replayed banner. Returns (sock, board name)."""
    sock = connect(port)
    match, buf = read_until(sock, BANNER_RE.pattern)
    assert match, f"no banner on connect; got {buf[-200:]!r}"
    return sock, match.group(2).decode()


def wait_until(predicate, timeout: float = 40.0, interval: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False
