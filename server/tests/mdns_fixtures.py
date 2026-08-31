"""Canned DNS-SD packets, built the way a real responder builds them.

Deliberately NOT in conftest.py, for the reason given in relay_fixtures.py:
there are two conftest modules in this tree and `from conftest import ...`
resolves to whichever pytest inserted into sys.path first.

The packets here are *written*, not pasted as hex, because the interesting part
of a real reply is its name compression -- and a writer that compresses the way
avahi does keeps the pointers correct when a fixture changes. Every name that
repeats becomes a 0xC0 pointer, including names inside rdata, which is exactly
the shape that broke the first decoder.
"""

from __future__ import annotations

import struct

SERVICE = "_mbrelay._tcp"
TYPE_LABELS = ("_mbrelay", "_tcp", "local")
TXID = 0x1234
INSTANCE = "torture"
HOSTNAME = ("torture", "local")
ADDRESS = "192.168.1.12"
PORT = 8760
VERSION = "0.20260826.9"
TXT_STRINGS = ("txtvers=1", f"version={VERSION}", "node=torture")

# QR=1, AA=1. What avahi sets on a legacy-unicast reply.
RESPONSE_FLAGS = 0x8400
# The class avahi echoes back on the question, QU bit and all, and the class it
# puts on a unique record. Both must decode as plain IN.
QU_IN = 0x8001
FLUSH_IN = 0x8001

TYPE_A, TYPE_PTR, TYPE_TXT, TYPE_AAAA, TYPE_SRV = 1, 12, 16, 28, 33


class Packet:
    """A DNS message writer with RFC 1035 name compression."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self._offsets: dict[tuple[str, ...], int] = {}

    # -- names -------------------------------------------------------------
    def name(self, labels) -> None:
        rest = tuple(labels)
        while rest:
            key = tuple(label.lower() for label in rest)
            if key in self._offsets:
                self.buf += struct.pack("!H", 0xC000 | self._offsets[key])
                return
            if len(self.buf) < 0x4000:      # a pointer only has 14 bits of offset
                self._offsets[key] = len(self.buf)
            raw = rest[0].encode("utf-8")
            self.buf += bytes([len(raw)]) + raw
            rest = rest[1:]
        self.buf += b"\x00"

    # -- structure ---------------------------------------------------------
    def header(self, txid: int, flags: int, counts) -> None:
        self.buf += struct.pack("!6H", txid, flags, *counts)

    def question(self, labels, qtype: int = TYPE_PTR, qclass: int = QU_IN) -> None:
        self.name(labels)
        self.buf += struct.pack("!HH", qtype, qclass)

    def record(self, labels, rtype: int, rdata, *, rclass: int = 1,
               ttl: int = 10) -> None:
        """`rdata` is bytes, or a callable that writes into this packet."""
        self.name(labels)
        self.buf += struct.pack("!HHI", rtype, rclass, ttl)
        length_at = len(self.buf)
        self.buf += b"\x00\x00"
        start = len(self.buf)
        if callable(rdata):
            rdata(self)
        else:
            self.buf += rdata
        struct.pack_into("!H", self.buf, length_at, len(self.buf) - start)

    @property
    def bytes(self) -> bytes:
        return bytes(self.buf)


def txt_rdata(strings=TXT_STRINGS) -> bytes:
    if not strings:
        return b"\x00"          # the canonical EMPTY txt: one zero-length string
    return b"".join(bytes([len(s.encode())]) + s.encode() for s in strings)


def srv_rdata(target=HOSTNAME, port: int = PORT, priority: int = 0, weight: int = 0):
    def write(packet: Packet) -> None:
        packet.buf += struct.pack("!HHH", priority, weight, port)
        packet.name(target)     # compressed against the rest of the packet
    return write


def a_rdata(address: str = ADDRESS) -> bytes:
    return bytes(int(octet) for octet in address.split("."))


def reply(txid: int = TXID, *, instance: str = INSTANCE, hostname=HOSTNAME,
          address: str = ADDRESS, port: int = PORT, txt=TXT_STRINGS,
          service=TYPE_LABELS, ttl: int = 10, ptr_ttl: int | None = None,
          sections=("ptr", "srv", "txt", "a"), echo_question: bool = True,
          flags: int = RESPONSE_FLAGS) -> bytes:
    """One responder's answer, with whichever records you want in it.

    `sections` exists because a real responder splits records across datagrams
    however it likes, and avahi's 512-byte cap on a legacy reply can drop the
    trailing A entirely -- both are cases the collector has to survive.
    """
    full = (instance,) + tuple(service)
    packet = Packet()
    answers = 1 if "ptr" in sections else 0
    additional = sum(1 for s in ("srv", "txt", "a") if s in sections)
    packet.header(txid, flags, (1 if echo_question else 0, answers, 0, additional))
    if echo_question:
        packet.question(service)
    if "ptr" in sections:
        packet.record(service, TYPE_PTR, lambda p: p.name(full),
                      ttl=ptr_ttl if ptr_ttl is not None else ttl)
    if "srv" in sections:
        packet.record(full, TYPE_SRV, srv_rdata(hostname, port),
                      rclass=FLUSH_IN, ttl=ttl)
    if "txt" in sections:
        packet.record(full, TYPE_TXT, txt_rdata(txt), rclass=FLUSH_IN, ttl=ttl)
    if "a" in sections:
        packet.record(hostname, TYPE_A, a_rdata(address), rclass=FLUSH_IN, ttl=ttl)
    return packet.bytes
