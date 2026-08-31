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


# -- connecting to a robot by name -------------------------------------------
#
# `mbrelay connect tovez`: name the ROBOT, not the relay. The client asks the
# relay host's name registry where that robot is, takes a relay from the pool,
# tunes it there with `!CG <channel> <group>`, enters the data plane, and hands
# over a terminal in which every line typed goes to the robot. No channel, no
# group, no port.
#
# The pair always comes from the registry, never from the board. A name only
# yields a DEFAULT address (§3.7); the registry is what knows whether a robot
# was moved off it. That is also why this sends `!CG` rather than a tune-by-name
# command -- and, usefully, `!CG` is as old as the protocol, so this works on
# every firmware in the fleet.

import re
from dataclasses import dataclass

from .naming import NAME_RE, name_to_radio, normalize, validate


class RobotTuneError(Exception):
    """The relay could not be put on the robot's link."""


@dataclass(frozen=True)
class ConnectTarget:
    """What `mbrelay connect <target>` asked for.

    ``tovez``            robot tovez; the relay host comes from config or discovery
    ``tovez@torture``    robot tovez through the relay host torture (port 8760)
    ``torture``, ``torture:8760``, ``""``   a relay host (or none), no robot
    """
    robot: str | None
    host: str | None
    port: int | None

    @property
    def endpoint(self) -> str | None:
        return f"{self.host}:{self.port}" if self.host else None


def parse_connect_target(target: str | None, default_port: int = DEFAULT_PORT) -> ConnectTarget:
    text = (target or "").strip()
    robot, at, rest = text.partition("@")
    if at:
        name = normalize(robot)
        if not NAME_RE.match(name):
            raise ValueError(f"{robot!r} is not a micro:bit name (five letters, e.g. tovez)")
        host, port = parse_target(rest, default_port)
        return ConnectTarget(name, host, port)
    name = normalize(text)
    if NAME_RE.match(name):
        return ConnectTarget(name, None, None)
    if not text:
        return ConnectTarget(None, None, None)
    host, port = parse_target(text, default_port)
    return ConnectTarget(None, host, port)


class RobotResolveError(Exception):
    """The registry could not say where a robot is."""


def resolve_robot(host: str, robot: str, port: int = 8761,
                  timeout: float = 3.0) -> tuple[int, int, str]:
    """Ask the relay host's registry where `robot` is: (channel, group, source).

    Falls back to the name's derived address when the registry cannot be
    reached, because a relay host running an older daemon -- or one whose
    registry port is firewalled -- should still be usable for every robot that
    never moved, which today is all of them. The fallback is announced by the
    caller rather than hidden: on a fleet that HAS moved a robot, silently
    tuning to the derived pair is the failure mode this whole design exists to
    prevent, so the user has to be told which answer they got.
    """
    import json
    import urllib.error
    import urllib.request

    name = validate(robot)          # malformed is an error here, not a fallback
    url = f"http://{host}:{port}/names/{name}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read())
        return int(data["channel"]), int(data["group"]), data.get("source", "registry")
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        channel, group = name_to_radio(name)
        raise RegistryUnreachable(channel, group, f"{url}: {exc}") from None


class RegistryUnreachable(RobotResolveError):
    """No registry answered; the derived address is offered instead."""

    def __init__(self, channel: int, group: int, detail: str) -> None:
        self.channel, self.group, self.detail = channel, group, detail
        super().__init__(detail)


_BANNER_RE = re.compile(rb"DEVICE:\w+:relay:([^:\r\n]+):")
_REPLY_RE = re.compile(rb"#\s*(?:channel:|error:)[^\r\n]*\r?\n")
_TUNED_RE = re.compile(rb"#\s*channel:\s*(\d+)\s+group:\s*(\d+)")
_GO_RE = re.compile(rb"#\s*entering data plane[^\r\n]*\r?\n")
_PONG_RE = re.compile(rb"\bpong\b")


def _read_until(sock: socket.socket, pattern: re.Pattern, timeout: float):
    """Read until `pattern` matches or `timeout` elapses; (match, everything read)."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        sock.settimeout(min(0.25, left))
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
        match = pattern.search(buf)
        if match:
            return match, bytes(buf)
    return None, bytes(buf)


@dataclass
class Tuned:
    relay: str          # the relay board's name, from its banner
    channel: int
    group: int
    answered: bool | None   # did the robot answer PING? None = not probed


def tune_to_robot(sock: socket.socket, robot: str, channel: int, group: int, *,
                  timeout: float = 8.0, settle: float = 0.5, probe: bool = True,
                  out=sys.stderr) -> Tuned:
    """Put a freshly acquired relay on `robot`'s link and enter the data plane.

    `channel` and `group` come from the relay host's registry -- resolved by the
    caller, because only the registry knows whether this robot still sits on the
    address its name derives.

    One line, then its reply, then the next: the firmware can drop bytes of
    the following line while it retunes the radio, so this never bursts.
    """
    banner, _ = _read_until(sock, _BANNER_RE, timeout)
    relay = banner.group(1).decode(errors="replace") if banner else "?"
    time.sleep(settle)

    sock.sendall(f"!CG {channel} {group}\n".encode())
    reply, seen = _read_until(sock, _REPLY_RE, timeout)
    if reply is None:
        raise RobotTuneError(
            f"relay {relay} did not answer !CG {channel} {group} within {timeout:.0f}s"
            + (f": {seen[-120:]!r}" if seen else ""))
    line = reply.group(0).strip().decode(errors="replace")
    tuned = _TUNED_RE.search(reply.group(0))
    if tuned is None:
        raise RobotTuneError(f"relay {relay} refused !CG {channel} {group}: {line}")
    # The board echoes what it actually applied; trust that over what we asked
    # for, so a clamped or rejected value shows up in the message below.
    channel, group = int(tuned.group(1)), int(tuned.group(2))
    out.write(f"mbrelay: relay {relay} tuned to {robot}: channel {channel} group {group}\n")

    time.sleep(settle)
    sock.sendall(b"!GO\n")
    if _read_until(sock, _GO_RE, timeout)[0] is None:
        raise RobotTuneError(f"relay {relay} did not enter the data plane")

    answered = None
    if probe:
        # League robot firmware answers PING with "pong <ms>" and needs no
        # sequence id, so this is the cheapest proof that someone is listening.
        time.sleep(settle)
        sock.sendall(b"PING\n")
        answered = _read_until(sock, _PONG_RE, 2.0)[0] is not None
        if answered:
            out.write(f"mbrelay: {robot} answered PING\n")
        else:
            out.write(f"mbrelay: no answer from {robot} on channel {channel} group {group} "
                      "-- is it powered, in range, and running self-addressing firmware?\n")
    return Tuned(relay, channel, group, answered)
