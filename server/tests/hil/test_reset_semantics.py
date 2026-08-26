"""Pins the firmware facts the daemon's whole design rests on.

These talk to a board directly, not through the server. If one starts failing,
the firmware or the platform changed underneath us and the daemon's release path
is no longer sound -- which would be invisible from the server tests alone.

Note what "reset" means here. The data plane has no in-band escape, so the only
way back to the command plane is a reboot, and how you obtain that reboot is
platform-specific: closing and reopening the port does it on macOS, and does
nothing at all on Linux (measured on Ubuntu 24.04 / DAPLink v0257 -- neither a
DTR pulse, nor a 1200-baud touch, nor an RTS pulse works either). A break
condition works everywhere. So these tests use the same layered primitive the
daemon does rather than assuming one mechanism.
"""

from __future__ import annotations

import re
import time

import serial

CONFIG_RE = re.compile(rb"#\s*channel:\s*(\d+)\s+group:\s*(\d+)")
BANNER_RE = re.compile(rb"DEVICE:RADIOBRIDGE:relay:([^:]+):(\d+)")
BAUD = 115200


def _talk(port: str, commands: list[bytes], settle: float = 0.5) -> bytes:
    """Open the port, send each command, and collect everything that comes back."""
    ser = serial.Serial(port, BAUD, timeout=0.3, exclusive=True)
    try:
        time.sleep(settle)
        ser.reset_input_buffer()
        out = bytearray()
        for command in commands:
            ser.write(command)
            ser.flush()
            time.sleep(0.4)
            out += ser.read(8192)
        time.sleep(0.2)
        out += ser.read(8192)
        return bytes(out)
    finally:
        ser.close()


def _answers(port: str) -> bool:
    return bool(BANNER_RE.search(_talk(port, [b"HELLO\n"])))


def reset_board(port: str) -> None:
    """Actually reboot the board, on either platform.

    Unconditional, and that matters. The obvious version -- close/reopen, then
    fall back to a break only if the board stops answering -- looks right but
    reboots nothing on Linux when the board is in the COMMAND plane: it answers
    fine, so the fallback never fires, and the flash-reload that a reboot would
    trigger never happens. Tests about what survives a reboot then silently test
    nothing.

    The daemon's hello() is deliberately different: it only breaks when a board
    has gone quiet, because there the goal is "reachable", not "rebooted", and
    it reaches factory defaults by writing them explicitly rather than by
    rebooting.
    """
    ser = serial.Serial(port, BAUD, timeout=0.3, exclusive=True)
    time.sleep(0.3)
    ser.close()
    time.sleep(0.5)

    ser = serial.Serial(port, BAUD, timeout=0.3, exclusive=True)
    try:
        ser.send_break(duration=0.4)
    finally:
        ser.close()
    time.sleep(1.5)


def _channel(port: str) -> int:
    data = _talk(port, [b"HELLO\n", b"?\n"])
    match = CONFIG_RE.search(data)
    assert match, f"no config line from {port}: {data[-200:]!r}"
    return int(match.group(1))


def _restore(port: str) -> None:
    """Leave the board as we found it, whatever the test did to it."""
    reset_board(port)
    _talk(port, [b"HELLO\n", b"!C 0\n", b"!MODE RAW250\n", b"!ECHO OFF\n",
                 b"!DEFAULTS\n"])


def test_config_persists_across_a_reset(attached, borrowed):
    """Flash persistence is why release has to actively restore defaults -- a
    reset alone would bring the previous user's channel straight back."""
    port = borrowed(sorted(p.device for p in attached.values())[0])
    try:
        _talk(port, [b"HELLO\n", b"!C 7\n"])
        reset_board(port)
        assert _channel(port) == 7, "the channel did not survive a reset"
    finally:
        _restore(port)


def test_defaults_only_takes_effect_on_the_next_reset(attached, borrowed):
    """!DEFAULTS clears the stored record; the LIVE config is untouched until the
    board reboots. This is the trap that makes a one-command release wrong, and
    there is a unit test pinning the same thing against the fake."""
    port = borrowed(sorted(p.device for p in attached.values())[0])
    try:
        data = _talk(port, [b"HELLO\n", b"!C 5\n", b"!DEFAULTS\n", b"?\n"])
        after_clear = data.split(b"stored config cleared")[-1]
        match = CONFIG_RE.search(after_clear)
        assert match, f"no config line after !DEFAULTS: {data[-200:]!r}"
        assert int(match.group(1)) == 5, "!DEFAULTS wrongly changed the live channel"

        reset_board(port)
        assert _channel(port) == 0, "the defaults did not apply on the next reset"
    finally:
        _restore(port)


def test_the_data_plane_is_escapable_only_by_a_reset(attached, borrowed):
    """The invariant the release path depends on.

    After !GO the board stops interpreting serial input -- HELLO becomes radio
    payload -- and the only way back is a reboot.
    """
    port = borrowed(sorted(p.device for p in attached.values())[0])
    try:
        assert b"entering data plane" in _talk(port, [b"HELLO\n", b"!GO\n"])
        assert not _answers(port), "the board still answered from the data plane"

        reset_board(port)
        assert _answers(port), "no reset mechanism could recover the board"
    finally:
        _restore(port)


def test_reopening_the_port_does_not_reset_on_this_platform(attached, borrowed):
    """Records which reset mechanism this platform actually provides.

    Not an assertion about which one works -- that differs between macOS and
    Linux, and the point is to make the difference visible rather than to bake
    one platform's behaviour into a passing test. What IS asserted: after the
    layered reset, the board is reachable.
    """
    port = borrowed(sorted(p.device for p in attached.values())[0])
    try:
        assert b"entering data plane" in _talk(port, [b"HELLO\n", b"!GO\n"])

        ser = serial.Serial(port, BAUD, timeout=0.3, exclusive=True)
        time.sleep(0.3)
        ser.close()
        time.sleep(0.6)
        reopen_worked = _answers(port)

        if not reopen_worked:
            ser = serial.Serial(port, BAUD, timeout=0.3, exclusive=True)
            try:
                ser.send_break(duration=0.4)
            finally:
                ser.close()
            time.sleep(1.2)
            assert _answers(port), (
                "neither close/reopen nor a break could reset this board -- the "
                "daemon has no way to reclaim it")

        print(f"\n  close/reopen resets this board: {reopen_worked}"
              f"  (break needed: {not reopen_worked})")
    finally:
        _restore(port)
