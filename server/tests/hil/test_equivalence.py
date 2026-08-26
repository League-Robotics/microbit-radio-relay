"""The requirement, turned into an assertion.

"The experience on the socket should be exactly the experience of being
connected through a serial port." Run one identical script against both and
compare the transcripts.
"""

from __future__ import annotations

import socket
import time

import serial

from hil_support import bind

BAUD = 115200

# Command-plane exchanges with deterministic replies. Anything involving the
# radio is excluded on purpose: over-the-air traffic is not reproducible, and
# this is a test about the transport, not the link.
SCRIPT = [
    b"?\n",
    b"!MODE?\n",
    b"!C 3\n",
    b"!P 4\n",
    b"!ECHO ON\n",
    b"!ECHO OFF\n",
    b"!C 0\n",
]

# Both sides use the same rhythm -- send, wait, read whatever arrived -- rather
# than one side stopping at the first regex match. A pattern match returns the
# instant it is satisfied, so "# mode: RAW250" gets captured as "# mode: RA" and
# the comparison then fails on the reader's timing rather than on the transport.
SETTLE = 0.4


def _normalise(chunks: list[bytes]) -> list[list[str]]:
    """Reduce a transcript to the board's replies to OUR commands.

    Two things are dropped, both deliberately:

    * Chunk boundaries. A serial reader gets arbitrary ones anyway, so requiring
      identical framing would be testing the kernel, not the daemon.
    * Everything that is not a "#" comment. Per the protocol, the relay answers
      every command with a "#"-prefixed line, so keeping only those keeps
      exactly the thing under test.

      Filtering positively matters. The first attempt dropped lines starting
      with "<" (messages the board RECEIVED over the radio, which other boards
      on the bench emit whenever they like, in different numbers each run). But
      a read can split such a line, leaving a bare fragment like "0 0 none"
      with no "<" on it -- which then failed the comparison for reasons that
      had nothing to do with the transport.
    """
    out = []
    for chunk in chunks:
        lines = [line.decode(errors="replace").strip()
                 for line in chunk.replace(b"\r\n", b"\n").split(b"\n")]
        out.append([line for line in lines if line.startswith("#")])
    return out


def _via_serial(port: str) -> list[bytes]:
    ser = serial.Serial(port, BAUD, timeout=0.3, exclusive=True)
    try:
        time.sleep(0.5)
        ser.reset_input_buffer()
        ser.write(b"HELLO\n")
        ser.flush()
        time.sleep(SETTLE)
        ser.read(8192)                       # discard the banner
        replies = []
        for command in SCRIPT:
            ser.write(command)
            ser.flush()
            time.sleep(SETTLE)
            replies.append(ser.read(8192))
        return replies
    finally:
        ser.close()


def _read_for(sock, duration: float) -> bytes:
    """Read everything that arrives within `duration`.

    Mirrors a serial read with a timeout, rather than stopping at the first thing
    that looks right -- which is what makes the two transcripts comparable.
    """
    buf = bytearray()
    end = time.time() + duration
    sock.settimeout(0.1)
    while time.time() < end:
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _via_socket() -> tuple[list[bytes], str]:
    sock, name = bind()                       # banner already consumed by bind()
    try:
        _read_for(sock, SETTLE)               # discard any banner remainder
        replies = []
        for command in SCRIPT:
            sock.sendall(command)
            replies.append(_read_for(sock, SETTLE))
        return replies, name
    finally:
        sock.close()


def test_socket_behaves_like_a_direct_serial_connection(daemon, admin, borrowed):
    from hil_support import wait_until

    raw_socket_replies, name = _via_socket()
    socket_replies = _normalise(raw_socket_replies)

    # Compare against the SAME board, and wait for it specifically -- another
    # board going free says nothing about this one.
    def this_board():
        return next(d for d in admin("list")["devices"] if d["name"] == name)

    assert wait_until(lambda: this_board()["state"] == "free"), \
        f"{name} never came back to the pool"

    port = borrowed(this_board()["port"])
    serial_replies = _normalise(_via_serial(port))

    for command, via_socket, via_serial in zip(SCRIPT, socket_replies,
                                               serial_replies):
        assert via_socket == via_serial, (
            f"{command!r} differs\n  socket: {via_socket}\n  serial: {via_serial}")
