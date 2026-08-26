"""``mbrelay connect`` -- a terminal against a served relay.

Exists so you can check a deployment without writing a script, and so the HIL
tests have a scriptable client (``--send``/``--expect``) that goes through the
real socket path rather than around it.
"""

from __future__ import annotations

import os
import re
import select
import socket
import sys
import termios
import time
import tty

DEFAULT_PORT = 8760


def parse_target(target: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    if not target:
        return "127.0.0.1", default_port
    if target.startswith("["):                       # [::1]:8760
        host, _, rest = target[1:].partition("]")
        return host, int(rest.lstrip(":") or default_port)
    host, sep, port = target.rpartition(":")
    if not sep:
        return target, default_port
    return (host or "127.0.0.1"), int(port)


def connect(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    # Same reason the server sets it: without TCP_NODELAY, Nagle adds ~40ms per
    # small write, which roughly doubles the observed radio round-trip.
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def run_script(sock: socket.socket, sends: list[str], expect: str | None,
               timeout: float, out=sys.stdout) -> int:
    """Non-interactive mode: send lines, optionally wait for a pattern.

    Returns a process exit code, so a shell script or a test can branch on it.
    """
    pattern = re.compile(expect.encode(), re.MULTILINE) if expect else None
    buf = bytearray()
    deadline = time.monotonic() + timeout
    sock.settimeout(0.2)

    for line in sends:
        payload = line.encode().decode("unicode_escape").encode("latin-1")
        if not payload.endswith(b"\n"):
            payload += b"\n"
        sock.sendall(payload)

    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            if pattern is None and not sends:
                continue
            if pattern is None:
                break
            continue
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
        if pattern is not None and pattern.search(buf):
            out.write(buf.decode("utf-8", "replace"))
            out.flush()
            return 0

    out.write(buf.decode("utf-8", "replace"))
    out.flush()
    if pattern is not None:
        print(f"\nmbrelay: never saw {expect!r} within {timeout}s", file=sys.stderr)
        return 1
    return 0


def interactive(sock: socket.socket, escape: str = "]", raw: bool = True,
                log_path: str | None = None) -> int:
    """A minimal terminal. Ctrl-<escape> quits.

    Raw mode matters: the relay's data plane is a transparent byte stream, so
    line discipline, echo and ^C handling would all corrupt it.
    """
    escape_byte = bytes([ord(escape.upper()) - 64])
    logfile = open(log_path, "ab") if log_path else None
    stdin_fd = sys.stdin.fileno()
    saved = None
    is_tty = os.isatty(stdin_fd)

    print(f"mbrelay: connected. Ctrl-{escape.upper()} to quit.", file=sys.stderr)
    try:
        if is_tty and raw:
            saved = termios.tcgetattr(stdin_fd)
            tty.setraw(stdin_fd)
        sock.settimeout(None)
        while True:
            readable, _, _ = select.select([sock, sys.stdin], [], [], 0.5)
            if sock in readable:
                data = sock.recv(65536)
                if not data:
                    print("\r\nmbrelay: server closed the connection", file=sys.stderr)
                    return 0
                os.write(sys.stdout.fileno(), data)
                if logfile:
                    logfile.write(data)
                    logfile.flush()
            if sys.stdin in readable:
                data = os.read(stdin_fd, 4096)
                if not data:
                    return 0
                if escape_byte in data:
                    sock.sendall(data.split(escape_byte)[0])
                    print("\r\nmbrelay: closed", file=sys.stderr)
                    return 0
                sock.sendall(data)
    except (KeyboardInterrupt, BrokenPipeError, ConnectionResetError):
        return 0
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        if logfile:
            logfile.close()
        sock.close()
