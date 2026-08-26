"""The headline test: two relays, through the server, over the radio.

One run exercises discovery, acquire (reset + banner + verified normalize), byte
transparency in both directions, the radio itself, release normalization, and
free-pool accounting. If this passes on a machine, the product works there.
"""

from __future__ import annotations

import socket
import time

import pytest

import contextlib

from hil_support import (BANNER_RE, bind, connect, drain, expect, read_exactly,
                      read_until, wait_until)


@contextlib.contextmanager
def bound(count: int = 1):
    """Bind `count` relays and always hand them back.

    Without the finally, one failing assertion leaves boards bound and every
    later test fails with "no relay available" instead of its own reason.
    """
    socks = []
    try:
        for _ in range(count):
            socks.append(bind())
        yield socks
    finally:
        for sock, _ in socks:
            with contextlib.suppress(OSError):
                sock.close()


def _into_data_plane(sock, channel: int = 17) -> None:
    expect(sock, f"!C {channel}\n".encode(), rb"# channel: %d group: 10" % channel)
    expect(sock, b"!MODE RAW250\n", rb"# mode: RAW250")
    expect(sock, b"!GO\n", rb"# entering data plane")


def test_radio_roundtrip_through_the_server(daemon, admin):
    with bound(2) as socks:
        (a, name_a), (b, name_b) = socks
        assert name_a != name_b, "the pool handed out the same board twice"

        for sock in (a, b):
            _into_data_plane(sock)

        # `expect` returns as soon as its pattern matches, so the tail of the
        # "# entering data plane" line is still arriving. Clear it, or those
        # bytes become the start of what we read back as payload.
        for sock in (a, b):
            drain(sock)

        # One RAW250 frame (<=247 bytes), so fire-and-forget cannot
        # fragment-fail. Includes 0x0a and 0x0d, which a line-oriented bug
        # would mangle.
        payload = bytes(range(1, 240))

        a.sendall(payload)
        assert read_exactly(b, len(payload)) == payload, "A -> radio -> B corrupted"

        b.sendall(payload)
        assert read_exactly(a, len(payload)) == payload, "B -> radio -> A corrupted"

    assert wait_until(lambda: admin("status")["devices"]["free"] >= 2), \
        "boards were not returned to the pool"


def test_release_restores_factory_defaults(daemon, admin):
    """The promise: nobody inherits the previous user's channel."""
    with bound(1) as socks:
        (sock, name), = socks
        expect(sock, b"!C 23\n", rb"# channel: 23 group: 10")
        expect(sock, b"!ECHO ON\n", rb"# echo: ON")

    def this_board():
        return next(d for d in admin("list")["devices"] if d["name"] == name)

    assert wait_until(lambda: this_board()["state"] == "free"), \
        f"{name} never came back to the pool"

    # Bind until we get the same board back, so the assertion is about the board
    # we actually dirtied rather than whichever one the pool offers first.
    for _ in range(4):
        with bound(1) as socks:
            (fresh, fresh_name), = socks
            if fresh_name == name:
                expect(fresh, b"?\n",
                       rb"# channel: 0 group: 10 mode: RAW250 power: 7")
                return
        wait_until(lambda: this_board()["state"] == "free")
    pytest.fail(f"never got {name} back from the pool")


def test_rejection_is_readable(daemon, admin):
    """A byte pipe has no error channel, so the reason goes out as a '#' comment
    -- which any client that already skips '#' lines will ignore."""
    total = admin("status")["devices"]["total"]
    held = [connect() for _ in range(total)]
    try:
        for sock in held:
            read_until(sock, BANNER_RE.pattern)

        extra = connect()
        try:
            match, data = read_until(extra, rb"# ERROR: no relay available")
            assert match, f"expected a readable refusal, got {data!r}"
            extra.settimeout(5)
            assert extra.recv(1) == b"", "the server must close, not hang"
        finally:
            extra.close()
    finally:
        for sock in held:
            with contextlib.suppress(OSError):
                sock.close()
        wait_until(lambda: admin("status")["devices"]["free"] >= total)


def test_echo_transponder_path(daemon, admin):
    """B echoes without a host in the loop, proving the radio path independently
    of whether our own pipe is looping bytes back."""
    with bound(2) as socks:
        (a, _), (b, _) = socks
        expect(b, b"!C 19\n", rb"# channel: 19 group: 10")
        expect(b, b"!ECHO ON\n", rb"# echo: ON")

        expect(a, b"!C 19\n", rb"# channel: 19 group: 10")
        drain(a)
        a.sendall(b"> ping-over-the-air\n")
        match, buf = read_until(a, rb"< ping-over-the-air", timeout=8)
        assert match, f"no echo came back: {buf[-200:]!r}"
    wait_until(lambda: admin("status")["devices"]["free"] >= 2)


def test_status_tracks_a_session(daemon, admin):
    before = admin("status")
    with bound(1) as socks:
        (sock, name), = socks
        assert wait_until(lambda: admin("status")["devices"]["busy"] >= 1)
        sessions = admin("sessions")["sessions"]
        assert any(s["device_name"] == name for s in sessions)

        sock.sendall(b"!GO\n")
        assert wait_until(
            lambda: any(s["plane"] == "data" for s in admin("sessions")["sessions"]),
            timeout=10), "the daemon did not notice the data-plane transition"
    assert wait_until(lambda: admin("status")["devices"]["busy"] == 0)
    assert admin("status")["listeners"][0]["accepted"] > \
        before["listeners"][0]["accepted"]


def test_kick_frees_a_board(daemon, admin):
    with bound(1) as socks:
        (sock, _), = socks
        session_id = admin("sessions")["sessions"][0]["id"]
        admin("kick", session=session_id, reason="test")
        sock.settimeout(15)
        assert sock.recv(65536) == b"", "kick must close the client socket"
    assert wait_until(lambda: admin("status")["devices"]["free"] >= 1)
