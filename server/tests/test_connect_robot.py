"""`mbrelay connect tovez` -- name the robot, not the relay.

The client takes a relay from the pool, tunes it with `!N <name>`, enters the
data plane and hands over a terminal. Tuning is driven here against the fake
firmware over a real socket pair; the CLI routing tests never touch the
network (every address is in the RFC 5737 documentation range and connect()
is replaced, exactly as test_cli.py does).
"""

from __future__ import annotations

import io
import socket
import threading

import pytest

from mbrelay.client import RobotTuneError, parse_connect_target, tune_to_robot

from fake_relay import FakeRelayFirmware


# -- target syntax -----------------------------------------------------------
@pytest.mark.parametrize("target,expected", [
    ("tovez", ("tovez", None, None)),
    ("  Tovez ", ("tovez", None, None)),
    ("tovez@torture", ("tovez", "torture", 8760)),
    ("tovez@torture:9000", ("tovez", "torture", 9000)),
    ("torture", (None, "torture", 8760)),
    ("192.168.1.12:8760", (None, "192.168.1.12", 8760)),
    ("", (None, None, None)),
    (None, (None, None, None)),
])
def test_a_target_is_a_robot_a_host_or_both(target, expected):
    t = parse_connect_target(target)
    assert (t.robot, t.host, t.port) == expected


@pytest.mark.parametrize("bad", ["robot1@torture", "tove@torture", "gauti@torture"])
def test_a_robot_that_is_not_a_micro_bit_name_is_refused_before_dialling(bad):
    with pytest.raises(ValueError):
        parse_connect_target(bad)


# -- tuning, against the fake firmware over a socket -------------------------
def _serve(fw: FakeRelayFirmware, peer: socket.socket, *, pong: bool = True,
           reply_to_n: bytes | None = None) -> threading.Thread:
    """Play daemon + board: banner first, then answer whatever arrives."""
    fw.reset()

    def run():
        peer.sendall(fw.drain())
        peer.settimeout(0.05)
        while True:
            try:
                data = peer.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            if reply_to_n is not None and data.startswith(b"!N "):
                peer.sendall(reply_to_n)
                continue
            was_command = fw.plane == "command"
            fw.feed(data)
            out = fw.drain()
            if out:
                peer.sendall(out)
            if not was_command and pong and b"PING" in data:
                peer.sendall(b"pong 42\r\n")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def link():
    a, b = socket.socketpair()
    yield a, b
    a.close(); b.close()


def test_tuning_puts_the_relay_on_the_robots_link_and_enters_the_data_plane(link):
    client, board = link
    fw = FakeRelayFirmware(name="getez")
    _serve(fw, board)
    log = io.StringIO()

    tuned = tune_to_robot(client, "tovez", settle=0.01, out=log)

    assert (tuned.relay, tuned.channel, tuned.group) == ("getez", 55, 108)
    assert fw.cfg.name == "tovez" and fw.plane == "data"
    assert tuned.answered is True
    assert "relay getez tuned to tovez: channel 55 group 108" in log.getvalue()
    assert "tovez answered PING" in log.getvalue()


def test_a_silent_robot_is_reported_but_the_terminal_is_still_handed_over(link):
    """Today's robots are not yet self-addressing: the link is right, nobody is
    on it. That is a warning, not a failure -- the operator may know why."""
    client, board = link
    _serve(FakeRelayFirmware(), board, pong=False)
    log = io.StringIO()
    tuned = tune_to_robot(client, "vevov", settle=0.01, out=log)
    assert (tuned.channel, tuned.group, tuned.answered) == (37, 43, False)
    assert "no answer from vevov on channel 37 group 43" in log.getvalue()


def test_a_relay_that_predates_named_links_is_named_as_the_problem(link):
    client, board = link
    _serve(FakeRelayFirmware(), board,
           reply_to_n=b"# error: unknown command (try !HELP)\r\n")
    with pytest.raises(RobotTuneError, match="predates !N"):
        tune_to_robot(client, "tovez", settle=0.01, out=io.StringIO())


def test_the_relays_own_refusal_is_passed_through(link):
    client, board = link
    _serve(FakeRelayFirmware(), board)
    with pytest.raises(RobotTuneError, match="refused !N gauti"):
        tune_to_robot(client, "gauti", settle=0.01, out=io.StringIO())
