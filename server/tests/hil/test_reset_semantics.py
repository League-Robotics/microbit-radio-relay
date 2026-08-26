"""Pins the firmware facts the daemon's whole design rests on.

These talk to a board directly, not through the server. If one of them starts
failing, the firmware changed underneath us and the daemon's release path is no
longer sound -- which would be invisible from the server tests alone.
"""

from __future__ import annotations

import re
import time

import serial

CONFIG_RE = re.compile(rb"#\s*channel:\s*(\d+)\s+group:\s*(\d+)")
BAUD = 115200


def _talk(port: str, commands: list[bytes], settle: float = 0.4) -> bytes:
    """Open (which RESETS the board), send, read, close."""
    ser = serial.Serial(port, BAUD, timeout=0.3, exclusive=True)
    try:
        time.sleep(settle)
        ser.reset_input_buffer()
        out = bytearray()
        for command in commands:
            ser.write(command)
            ser.flush()
            time.sleep(0.35)
            out += ser.read(4096)
        time.sleep(0.2)
        out += ser.read(4096)
        return bytes(out)
    finally:
        ser.close()


def _channel(port: str) -> int:
    data = _talk(port, [b"HELLO\n", b"?\n"])
    match = CONFIG_RE.search(data)
    assert match, f"no config line from {port}: {data[-200:]!r}"
    return int(match.group(1))


def test_config_persists_across_a_reset(attached, borrowed):
    """Flash persistence is why release has to actively restore defaults --
    a reset alone would bring the old channel straight back."""
    port = borrowed(sorted(p.device for p in attached.values())[0])
    try:
        _talk(port, [b"HELLO\n", b"!C 7\n"])
        assert _channel(port) == 7, "channel did not survive close/reopen"
    finally:
        _talk(port, [b"HELLO\n", b"!C 0\n", b"!DEFAULTS\n"])


def test_defaults_only_takes_effect_on_the_next_reset(attached, borrowed):
    """!DEFAULTS clears the stored record; the LIVE config is untouched until the
    board reboots. This is the trap that makes a one-command release wrong."""
    port = borrowed(sorted(p.device for p in attached.values())[0])
    try:
        data = _talk(port, [b"HELLO\n", b"!C 5\n", b"!DEFAULTS\n", b"?\n"])
        match = CONFIG_RE.search(data.split(b"stored config cleared")[-1])
        assert match, f"no config line after !DEFAULTS: {data[-200:]!r}"
        assert int(match.group(1)) == 5, "!DEFAULTS wrongly changed the live channel"

        assert _channel(port) == 0, "defaults did not apply on the next reset"
    finally:
        _talk(port, [b"HELLO\n", b"!C 0\n", b"!DEFAULTS\n"])


def test_reopening_the_port_returns_to_the_command_plane(attached, borrowed):
    """The data plane has no in-band escape, so this close/reopen is the only way
    back -- and therefore the only way release can work."""
    port = borrowed(sorted(p.device for p in attached.values())[0])
    data = _talk(port, [b"HELLO\n", b"!GO\n"])
    assert b"entering data plane" in data

    again = _talk(port, [b"HELLO\n"])
    assert b"DEVICE:RADIOBRIDGE:relay:" in again, \
        "the board did not come back to the command plane after reopen"
