"""An in-process stand-in for the multicast socket, and a clock to match.

The seam is the same one transport.py uses: a Protocol (``MulticastSocket``), a
real implementation (``UdpMulticastSocket``), and this fake, injected. Nothing in
the browse loop knows which it has, so the whole loop is exercised with no
network -- which is the point, because CI is stock ubuntu-latest with no mDNS
responder and the bench Mac has four real relays on the LAN. A test that issued
a real query would pass or fail depending on which room the laptop was in.

The fake is a *responder*, not a recording: it stamps the transaction id of the
query it just received onto each canned answer, exactly as RFC 6762 s6.7
requires ("it MUST repeat the query ID and the question"). That is what lets a
test queue an answer without knowing the random id the browser will pick.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


class FakeClock:
    """Monotonic time the test owns.

    Time only moves when the browser asks to wait for a datagram, so a 1.5s
    timeout costs nothing and `elapsed` is exact rather than approximately
    right.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeMdnsSocket:
    """A MulticastSocket whose answers the test supplies."""

    answers: list = field(default_factory=list)     # bytes, or (bytes, peer)
    clock: FakeClock = field(default_factory=FakeClock)
    source: str = "192.168.1.40"
    peer: str = "192.168.1.12"
    echo_txid: bool = True
    fail_send: OSError | None = None
    sent: list[bytes] = field(default_factory=list)
    closed: bool = False

    def send(self, payload: bytes) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append(payload)

    def receive(self, timeout: float):
        if not self.answers or not self.sent:
            self.clock.advance(timeout)      # the wire was quiet for the whole wait
            return None
        answer = self.answers.pop(0)
        data, peer = answer if isinstance(answer, tuple) else (answer, self.peer)
        return self._stamp(data), peer

    def _stamp(self, data: bytes) -> bytes:
        """Put the right (or deliberately wrong) transaction id on an answer."""
        if len(data) < 2 or not self.sent:
            return data
        txid = struct.unpack_from("!H", self.sent[-1])[0]
        if not self.echo_txid:
            txid ^= 0xFFFF          # deterministically somebody else's reply
        return struct.pack("!H", txid) + data[2:]

    def close(self) -> None:
        self.closed = True

    @property
    def txids(self) -> list[int]:
        return [struct.unpack_from("!H", query)[0] for query in self.sent]


def one_socket(*sockets: FakeMdnsSocket):
    """An `open_socket` factory handing out the given fakes in order."""
    queue = list(sockets)

    def factory(interface: str = "") -> FakeMdnsSocket:
        return queue.pop(0) if queue else sockets[-1]
    return factory
