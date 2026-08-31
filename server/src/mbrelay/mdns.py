"""Zero-config discovery: find relay hosts on the LAN, and announce this one.

Two halves that never touch each other's problems:

* a **pure codec** (``encode_query`` / ``decode_message``) with no socket
  anywhere near it, because every mDNS bug worth having is a parsing bug and
  parsing bugs are only cheap to find offline; and
* a **socket layer** (``UdpMulticastSocket`` / ``browse``) that is deliberately
  boring, and whose every failure becomes ``BrowseResult.problem`` text rather
  than an exception.

Why stdlib rather than ``zeroconf``: the server has exactly one dependency
(pyserial), which is what lets a fleet node install with
``apt install python3-serial && pip install --break-system-packages <wheel>``.
A browser is three hundred lines of struct-unpacking; a dependency is forever.

**We never bind UDP 5353.** Not because we cannot -- mDNSResponder and
avahi-daemon both set SO_REUSEPORT, so a second binder that does the same
succeeds, which is how python-zeroconf works -- but because we do not need to.
RFC 6762 s6.7 says a responder MUST answer a query whose *source port is not
5353* by unicast, straight back to the querier's address and port. Binding an
ephemeral port therefore puts us on the MUST path and the replies land on our
own socket. The s5.4 "QU" bit is only a SHOULD with an explicit escape hatch
("if the responder has not multicast that record recently... the responder
SHOULD instead multicast the response"), and that response would go to
224.0.0.251 where we would never see it -- so we set QU because it is free and
depend on the source port. Binding 5353 would additionally drag in
per-interface IP_ADD_MEMBERSHIP and the interface enumeration this design
exists to avoid.

Three consequences of living on the s6.7 path, all handled below:

1. avahi caps a legacy-unicast reply at 512 bytes (``avahi_dns_packet_new_reply``
   in avahi-core/server.c), so a busy node can answer with SRV and no A --
   hence the resolver fallback in ``browse_detailed``.
2. avahi echoes the question verbatim, QU bit included, so the decoder masks
   0x7FFF off *question* classes as well as record classes. Without that the
   echoed question decodes as class 32769.
3. legacy replies carry a short TTL and no cache-flush bit, so cache-flush is
   never used as a signal here. ``ttl == 0`` still means goodbye (s10.1).

IPv6 is out of scope for the first cut: the fleet is IPv4, and ff02::fb roughly
doubles the socket handling for no present benefit. AAAA still *decodes*, so a
mixed reply parses, but a v6 address is not offered to ``connect`` -- a
link-local fe80:: would only make it hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import select
import shutil
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

from .errors import MbrelayError

log = logging.getLogger(__name__)

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
DEFAULT_SERVICE = "_mbrelay._tcp"

TYPE_A = 1
TYPE_PTR = 12
TYPE_TXT = 16
TYPE_AAAA = 28
TYPE_SRV = 33
CLASS_IN = 1

# Top bit of a QCLASS is the s5.4 unicast-response request; top bit of an RRCLASS
# is the s10.2 cache-flush bit. Same mask, two different meanings, and both must
# come off before comparing against CLASS_IN.
CLASS_MASK = 0x7FFF
UNICAST_BIT = 0x8000

FLAG_RESPONSE = 0x8000

_MAX_NAME = 255                 # RFC 1035 s2.3.4
_MAX_LABEL = 63
_HEADER = struct.Struct("!6H")


class MalformedPacket(MbrelayError):
    """A DNS message we could not parse.

    Raised freely by the codec and caught unconditionally by ``browse``: a
    neighbour's broken responder is not our caller's problem.
    """


# ---------------------------------------------------------------------------
# codec -- pure bytes in, dataclasses out. No socket may appear below this line.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Question:
    name: tuple[str, ...]
    qtype: int
    qclass: int
    unicast: bool = False


@dataclass(frozen=True)
class SrvData:
    priority: int
    weight: int
    port: int
    target: tuple[str, ...]


@dataclass(frozen=True)
class Record:
    name: tuple[str, ...]
    rtype: int
    rclass: int
    ttl: int
    value: object            # str for A/AAAA, SrvData, dict for TXT, tuple for PTR


@dataclass(frozen=True)
class Message:
    txid: int
    flags: int
    questions: tuple[Question, ...] = ()
    answers: tuple[Record, ...] = ()
    authority: tuple[Record, ...] = ()
    additional: tuple[Record, ...] = ()

    @property
    def is_response(self) -> bool:
        return bool(self.flags & FLAG_RESPONSE)

    @property
    def records(self) -> tuple[Record, ...]:
        """Every resource record, section-blind.

        RFC 6763 s12 only *suggests* where SRV/TXT/A go, and responders disagree:
        avahi puts them in additional, some put them in answers. Code that reads
        only the answer section finds PTR records and nothing else, which
        presents as "discovery works but every host has no address".
        """
        return self.answers + self.authority + self.additional


def read_name(data: bytes, offset: int) -> tuple[tuple[str, ...], int]:
    """Read a possibly-compressed name.

    Returns (labels, offset just past the name *in the outer packet*). Labels
    are a tuple, NOT joined: a DNS-SD instance label may legitimately contain a
    dot ("lab.bench-1"), and joining loses the boundary between the instance
    name and "_mbrelay._tcp.local".

    Termination is guaranteed twice over, because a malformed or hostile packet
    must not be able to spin here: a pointer must point strictly backwards (so
    every jump lowers the position), and every target visited is recorded.
    """
    labels: list[str] = []
    end: int | None = None
    seen: set[int] = set()
    total = 0
    pos = offset
    while True:
        if pos >= len(data):
            raise MalformedPacket(f"name at {offset} runs past the packet")
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:                    # compression pointer
            if pos + 1 >= len(data):
                raise MalformedPacket("truncated compression pointer")
            target = ((length & 0x3F) << 8) | data[pos + 1]
            # The name ENDS at the FIRST pointer. After a jump `pos` is somewhere
            # else entirely and is useless for advancing the caller.
            if end is None:
                end = pos + 2
            if target >= pos or target in seen:
                raise MalformedPacket(
                    f"compression pointer at {pos} -> {target} is not "
                    f"strictly backwards")
            seen.add(target)
            pos = target
            continue
        if length & 0xC0:                            # 0x40/0x80 are reserved
            raise MalformedPacket(f"reserved label type 0x{length:02x} at {pos}")
        pos += 1
        if pos + length > len(data):
            raise MalformedPacket(f"label at {pos} runs past the packet")
        labels.append(data[pos:pos + length].decode("utf-8", "replace"))
        total += length + 1
        if total > _MAX_NAME:
            raise MalformedPacket("name exceeds 255 bytes")
        pos += length
    return tuple(labels), (pos if end is None else end)


def _read_question(data: bytes, offset: int) -> tuple[Question, int]:
    name, pos = read_name(data, offset)
    if pos + 4 > len(data):
        raise MalformedPacket("question truncated before its type and class")
    qtype, qclass = struct.unpack_from("!HH", data, pos)
    return Question(name, qtype, qclass & CLASS_MASK,
                    bool(qclass & UNICAST_BIT)), pos + 4


def _read_record(data: bytes, offset: int) -> tuple[Record, int]:
    name, pos = read_name(data, offset)
    if pos + 10 > len(data):
        raise MalformedPacket("record truncated before its header")
    rtype, rclass, ttl, rdlength = struct.unpack_from("!HHIH", data, pos)
    start = pos + 10
    if start + rdlength > len(data):
        raise MalformedPacket(
            f"rdata for type {rtype} at {start} runs past the packet")
    value = _decode_rdata(data, rtype, start, rdlength)
    # ALWAYS advance by rdlength, never by where the name parse landed. For a
    # compressed name inside rdata those differ, and getting it wrong shifts
    # every subsequent record -- the classic "the A record decodes as garbage"
    # bug that only shows up against a real responder.
    return Record(name, rtype, rclass & CLASS_MASK, ttl, value), start + rdlength


def _decode_rdata(data: bytes, rtype: int, start: int, rdlength: int):
    blob = data[start:start + rdlength]
    if rtype == TYPE_A:
        if rdlength != 4:
            raise MalformedPacket(f"A record with {rdlength} bytes of rdata")
        return socket.inet_ntoa(blob)
    if rtype == TYPE_AAAA:
        if rdlength != 16:
            raise MalformedPacket(f"AAAA record with {rdlength} bytes of rdata")
        return socket.inet_ntop(socket.AF_INET6, blob)
    if rtype == TYPE_PTR:
        return read_name(data, start)[0]
    if rtype == TYPE_SRV:
        if rdlength < 7:
            raise MalformedPacket(f"SRV record with {rdlength} bytes of rdata")
        priority, weight, port = struct.unpack_from("!HHH", data, start)
        return SrvData(priority, weight, port, read_name(data, start + 6)[0])
    if rtype == TYPE_TXT:
        return decode_txt(blob)
    # Anything else -- NSEC, HINFO, OPT -- is kept as bytes and ignored. An
    # unfamiliar record type is not a reason to throw away the datagram.
    return blob


def decode_txt(blob: bytes) -> dict[str, str]:
    """RFC 6763 s6 TXT strings, as the responders in the wild actually send them.

    A bare ``key`` (present, no value) and ``key=`` (present, empty value) both
    become ``""``; we do not distinguish, and neither does anything that reads
    this. Keys are case-insensitive, the first occurrence wins, a string that
    starts with ``=`` has no key and is dropped, and a single zero-length string
    is the canonical *empty* TXT rather than a malformed one.
    """
    out: dict[str, str] = {}
    pos = 0
    while pos < len(blob):
        length = blob[pos]
        pos += 1
        if pos + length > len(blob):
            raise MalformedPacket("TXT string runs past the record")
        item = blob[pos:pos + length]
        pos += length
        if not item:
            continue
        key, _, value = item.partition(b"=")
        if not key:
            continue
        out.setdefault(key.decode("utf-8", "replace").lower(),
                       value.decode("utf-8", "replace"))
    return out


def decode_message(data: bytes) -> Message:
    """Parse a whole DNS message, or raise MalformedPacket."""
    if len(data) < _HEADER.size:
        raise MalformedPacket(f"packet is {len(data)} bytes, shorter than a header")
    txid, flags, qdcount, ancount, nscount, arcount = _HEADER.unpack_from(data, 0)
    pos = _HEADER.size
    questions = []
    for _ in range(qdcount):
        question, pos = _read_question(data, pos)
        questions.append(question)
    sections: list[list[Record]] = [[], [], []]
    for index, count in enumerate((ancount, nscount, arcount)):
        for _ in range(count):
            record, pos = _read_record(data, pos)
            sections[index].append(record)
    return Message(txid=txid, flags=flags, questions=tuple(questions),
                   answers=tuple(sections[0]), authority=tuple(sections[1]),
                   additional=tuple(sections[2]))


def encode_name(labels: Sequence[str]) -> bytes:
    out = bytearray()
    for label in labels:
        raw = label.encode("utf-8")
        if not raw:
            raise MalformedPacket("empty label in a name")
        if len(raw) > _MAX_LABEL:
            raise MalformedPacket(
                f"label {label!r} is {len(raw)} bytes; the DNS limit is {_MAX_LABEL}")
        out.append(len(raw))
        out += raw
    out.append(0)
    if len(out) > _MAX_NAME:
        raise MalformedPacket("name exceeds 255 bytes")
    return bytes(out)


def service_labels(service: str) -> tuple[str, ...]:
    """"_mbrelay._tcp" -> ("_mbrelay", "_tcp", "local")."""
    parts = tuple(part for part in service.split(".") if part)
    if not parts:
        raise MalformedPacket("empty service type")
    if parts[-1].lower() != "local":
        parts += ("local",)
    return parts


def encode_query(service: str = DEFAULT_SERVICE, txid: int = 0, *,
                 unicast: bool = True, qtype: int = TYPE_PTR) -> bytes:
    """One PTR question for a service type. No compression; nothing to compress."""
    qclass = CLASS_IN | (UNICAST_BIT if unicast else 0)
    return (_HEADER.pack(txid, 0, 1, 0, 0, 0)
            + encode_name(service_labels(service))
            + struct.pack("!HH", qtype, qclass))


# ---------------------------------------------------------------------------
# what a caller gets back
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Service:
    """One advertised relay host, as far as we could resolve it."""
    instance: str                       # the DNS-SD instance label, e.g. "torture"
    hostname: str = ""                  # the SRV target, e.g. "torture.local"
    addresses: tuple[str, ...] = ()
    port: int = 0
    txt: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.txt.get("version", "")

    @property
    def address(self) -> str:
        """The literal address if we have one, else the .local name.

        Falling back to the name is not a cop-out: macOS resolves .local
        natively, and so does any Linux node with nss-mdns. It beats refusing to
        connect because avahi's 512-byte legacy cap dropped the A record.
        """
        return self.addresses[0] if self.addresses else self.hostname

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"


@dataclass(frozen=True)
class BrowseResult:
    services: tuple[Service, ...] = ()
    problem: str = ""       # empty means "nothing went wrong", not "found something"
    source: str = ""        # the local address the query went out from
    elapsed: float = 0.0
    queries: int = 0


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------
def _lower(labels: Sequence[str]) -> tuple[str, ...]:
    return tuple(label.lower() for label in labels)


def _dotted(labels: Sequence[str]) -> str:
    return ".".join(labels)


@dataclass
class _Pending:
    instance: str
    hostname: str = ""
    port: int = 0
    txt: dict[str, str] = field(default_factory=dict)


class _Collector:
    """Accumulates records into services as the datagrams arrive.

    PTR names the instance, SRV gives host and port, TXT the metadata, A the
    address -- and a responder is free to split those across datagrams, so this
    has to be additive rather than per-packet.

    Addresses are keyed on the SRV *target hostname*, globally, not per
    instance: one host may publish several instances, and the A record often
    arrives in a datagram that mentions no instance at all. Every store is a
    ``setdefault`` or an ``in`` check, which is why dedup across interfaces
    costs nothing -- there is no dedup pass because there is nothing to dedup.
    """

    def __init__(self, service: str) -> None:
        self._type = _lower(service_labels(service))
        self._instances: dict[tuple[str, ...], _Pending] = {}
        self._addresses: dict[str, list[str]] = {}

    def add(self, message: Message) -> None:
        withdrawn: set[tuple[str, ...]] = set()
        for record in message.records:
            if record.rtype == TYPE_PTR:
                self._add_ptr(record, withdrawn)
            elif record.rtype == TYPE_SRV:
                self._add_srv(record)
            elif record.rtype == TYPE_TXT:
                self._add_txt(record)
            elif record.rtype == TYPE_A and record.ttl:
                self._add_address(record)
        # Applied last so a goodbye cannot be resurrected by an SRV sitting
        # further down the same datagram.
        for key in withdrawn:
            self._instances.pop(key, None)

    def _add_ptr(self, record: Record, withdrawn: set) -> None:
        if _lower(record.name) != self._type:
            return
        target = record.value
        if not isinstance(target, tuple) or len(target) < 2:
            return
        if _lower(target[1:]) != self._type:
            return                          # a PTR for somebody else's service
        key = _lower(target)
        if record.ttl == 0:                 # goodbye, RFC 6762 s10.1
            withdrawn.add(key)
            return
        self._instances.setdefault(key, _Pending(instance=target[0]))

    def _pending(self, record: Record) -> _Pending | None:
        """The instance a per-instance record belongs to, created if unseen.

        Creating on SRV/TXT rather than waiting for the PTR is deliberate: the
        records may arrive in either order, and an instance we can fully resolve
        is useful whether or not its PTR made it into the same 512 bytes.
        """
        if len(record.name) < 2 or _lower(record.name[1:]) != self._type:
            return None
        return self._instances.setdefault(_lower(record.name),
                                          _Pending(instance=record.name[0]))

    def _add_srv(self, record: Record) -> None:
        pending = self._pending(record)
        if pending is None or not isinstance(record.value, SrvData):
            return
        pending.hostname = _dotted(record.value.target)
        pending.port = record.value.port

    def _add_txt(self, record: Record) -> None:
        pending = self._pending(record)
        if pending is None or not isinstance(record.value, dict):
            return
        for key, value in record.value.items():
            pending.txt.setdefault(key, value)

    def _add_address(self, record: Record) -> None:
        addresses = self._addresses.setdefault(_dotted(_lower(record.name)), [])
        if record.value not in addresses:
            addresses.append(str(record.value))

    def complete(self) -> bool:
        """Every instance we have HEARD OF resolves.

        Not "we have found everything" -- mDNS browsing is open-ended and there
        is no such state. This only licenses an early return.
        """
        return bool(self._instances) and all(
            self._resolved(pending) for pending in self._instances.values())

    def _resolved(self, pending: _Pending) -> bool:
        return bool(pending.port and self._addresses.get(pending.hostname.lower()))

    def unresolved(self) -> list[str]:
        return sorted(p.hostname for p in self._instances.values()
                      if p.hostname and not self._resolved(p))

    def learn_addresses(self, hostname: str, addresses: Sequence[str]) -> None:
        known = self._addresses.setdefault(hostname.lower(), [])
        for address in addresses:
            if address not in known:
                known.append(address)

    def services(self) -> list[Service]:
        out = []
        for _, pending in sorted(self._instances.items()):
            addresses = self._addresses.get(pending.hostname.lower(), ())
            out.append(Service(instance=pending.instance, hostname=pending.hostname,
                               addresses=tuple(addresses), port=pending.port,
                               txt=dict(pending.txt)))
        return out


# ---------------------------------------------------------------------------
# the socket seam
# ---------------------------------------------------------------------------
class MulticastSocket(Protocol):
    """The one thing ``browse`` needs from the network.

    Mirrors transport.PortScanner: a Protocol here, the real implementation
    below, a fake in tests/fake_mdns.py. Nothing in the browse loop knows which
    it has, which is why the whole loop is testable with no network.
    """

    source: str

    def send(self, payload: bytes) -> None: ...

    def receive(self, timeout: float) -> tuple[bytes, str] | None:
        """One datagram as (payload, peer address), or None if `timeout` expired."""

    def close(self) -> None: ...


def query_source(group: str = MDNS_GROUP) -> str:
    """The local address the kernel would use to reach the multicast group.

    A connect() on a UDP socket is a pure route lookup -- it sends nothing -- and
    it is the only interface-selection method measured to be right on this
    multi-homed Mac (en0 and en1 on the same /21, three VM bridges, plus a
    Tailscale utun). ``getaddrinfo(gethostname())`` was measured returning
    ['100.64.0.3'], the Tailscale address: the one interface with no responders
    on it. So: let the kernel pick, and report what it picked, because "we
    queried from 192.168.1.40" is the diagnostic that makes a wrong answer
    obvious.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((group, MDNS_PORT))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


class UdpMulticastSocket:
    """An ephemeral-port sender for RFC 6762 s6.7 legacy-unicast queries."""

    def __init__(self, interface: str = "", *, group: str = MDNS_GROUP,
                 port: int = MDNS_PORT) -> None:
        self._peer = (group, port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # RFC 6762 s11 requires 255, and the default is 1. This is a hop
            # limit, NOT a cache TTL, and it does not make the packet routable:
            # 224.0.0.251 is link-scoped whatever we put here.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            # How a daemon on THIS host is discovered. Set explicitly so a
            # refactor cannot silently break local discovery.
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            if interface:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(interface))
            sock.bind(("0.0.0.0", 0))       # ephemeral -> s6.7 -> unicast replies
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self.source = interface or query_source(group)

    @property
    def local_port(self) -> int:
        return self._sock.getsockname()[1]

    def send(self, payload: bytes) -> None:
        self._sock.sendto(payload, self._peer)

    def receive(self, timeout: float) -> tuple[bytes, str] | None:
        readable, _, _ = select.select([self._sock], [], [], max(timeout, 0.0))
        if not readable:
            return None
        # 9000 covers a jumbo frame; a legacy reply is capped far below that.
        data, peer = self._sock.recvfrom(9000)
        return data, peer[0]

    def close(self) -> None:
        self._sock.close()


def resolve_host(hostname: str) -> tuple[str, ...]:
    """Last-resort A lookup for a host whose record avahi truncated away.

    Not a second multicast round: .local resolves natively on macOS and on any
    Linux node with nss-mdns, and if it does not, an empty tuple is the honest
    answer -- the CLI prints "-" rather than inventing an address.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return ()
    return tuple(dict.fromkeys(info[4][0] for info in infos))


# ---------------------------------------------------------------------------
# browsing
# ---------------------------------------------------------------------------
# Legacy-unicast replies are generated immediately -- they are NOT subject to the
# 20-120ms randomized aggregation delay of s6.3 -- so on a quiet LAN everything
# is back in 5-50ms. Three transmissions cover a dropped datagram; 1.5s is
# generous rather than tight.
RETRIES = (0.0, 0.25, 0.75)
SETTLE = 0.20                   # quiet period before an early return


def browse(service: str = DEFAULT_SERVICE, timeout: float = 1.5,
           **kwargs) -> list[Service]:
    """Every relay host that answers, or an empty list. Never raises."""
    return list(browse_detailed(service, timeout, **kwargs).services)


def browse_detailed(service: str = DEFAULT_SERVICE, timeout: float = 1.5, *,
                    interfaces: Sequence[str] = (), expect: int = 0,
                    open_socket: Callable[[str], MulticastSocket] | None = None,
                    clock: Callable[[], float] | None = None,
                    resolver: Callable[[str], Sequence[str]] | None = None,
                    ) -> BrowseResult:
    """Browse for `service`, reporting why we found nothing when we find nothing.

    **This function has no ``raise`` on any path.** Discovery is a convenience;
    a firewall, a container with no multicast route, or one neighbour with a
    broken responder must all degrade to a list and a sentence, never to a
    traceback in the middle of ``mbrelay connect``.

    `expect` licenses an early return: with ``expect=1`` (what ``connect``
    wants, since one answer is enough) the browse ends once that many services
    resolve and the wire has been quiet for SETTLE. With ``expect=0``, which is
    what ``discover`` wants, it runs the full budget -- a truncated fleet
    listing is far more annoying than a 1.5 second pause.
    """
    open_socket = open_socket or UdpMulticastSocket
    clock = clock or time.monotonic
    resolver = resolver if resolver is not None else resolve_host

    start = clock()
    try:
        # Validate the service type here, once, so the send path further down
        # cannot raise: a typo in [mdns] service is a config mistake, and it
        # should read like one rather than like a crash.
        encode_query(service)
        collector = _Collector(service)
    except MalformedPacket as exc:
        return BrowseResult(problem=str(exc), elapsed=clock() - start)

    sockets: list[MulticastSocket] = []
    problem = ""
    sources: list[str] = []
    sent = 0

    try:
        for interface in (tuple(interfaces) or ("",)):
            sockets.append(open_socket(interface))
        sources = [s.source for s in sockets if getattr(s, "source", "")]
    except OSError as exc:
        problem = _explain(exc, service)

    if sockets and not problem:
        problem, sent = _browse_loop(collector, sockets, service, timeout,
                                     expect, clock, start)

    for sock in sockets:
        with contextlib.suppress(OSError):
            sock.close()

    # avahi's 512-byte cap on a legacy reply can drop the A record off the end of
    # an otherwise complete answer. Fill those in rather than reporting a host
    # with no address.
    for hostname in collector.unresolved():
        try:
            collector.learn_addresses(hostname, resolver(hostname))
        except Exception:                       # a resolver is caller-supplied
            log.debug("mdns: resolver failed for %s", hostname, exc_info=True)

    services = tuple(collector.services())
    source = ", ".join(dict.fromkeys(sources))
    if not services and not problem:
        problem = (f"no {service} nodes answered on {source or 'this host'} "
                   f"in {timeout:g}s. If a relay host is up, check that "
                   f"avahi-daemon is running there and that the local firewall "
                   f"allows UDP 5353.")
    return BrowseResult(services=services, problem=problem, source=source,
                        elapsed=clock() - start, queries=sent)


def _browse_loop(collector: _Collector, sockets: Sequence[MulticastSocket],
                 service: str, timeout: float, expect: int,
                 clock: Callable[[], float], start: float) -> tuple[str, int]:
    deadline = start + timeout
    ids: set[int] = set()
    sent = 0
    settle_at: float | None = None

    while True:
        now = clock()
        if now >= deadline or (settle_at is not None and now >= settle_at):
            return "", sent
        while sent < len(RETRIES) and now >= start + RETRIES[sent]:
            # Per s5.4 the unicast bit SHOULD NOT be set on a retransmission. We
            # do not depend on it either way -- the ephemeral source port is what
            # earns the unicast reply -- so honour the SHOULD and move on.
            txid = random.randint(1, 0xFFFF)
            ids.add(txid)
            payload = encode_query(service, txid, unicast=(sent == 0))
            sent += 1
            for sock in sockets:
                try:
                    sock.send(payload)
                except OSError as exc:
                    return _explain(exc, service), sent

        wait = deadline - now
        if sent < len(RETRIES):
            wait = min(wait, start + RETRIES[sent] - now)
        if settle_at is not None:
            wait = min(wait, settle_at - now)

        try:
            packet = _receive(sockets, max(wait, 0.0))
        except OSError as exc:
            return _explain(exc, service), sent
        if packet is None:
            continue

        data, peer = packet
        try:
            message = decode_message(data)
        except MalformedPacket as exc:
            # One neighbour with a broken responder must not end the browse. -vv
            # turns this into "mdns: ignoring 47 bytes from 192.168.1.9:
            # reserved label type 0x80 at 31", which is enough to name the host.
            log.debug("mdns: ignoring %d bytes from %s: %s", len(data), peer, exc)
            continue
        if not message.is_response:
            continue                            # our own query, looped back
        if message.txid not in ids:
            log.debug("mdns: dropping response txid=%#06x from %s (not ours)",
                      message.txid, peer)
            continue
        collector.add(message)
        if expect and collector.complete() and len(collector.services()) >= expect:
            settle_at = clock() + SETTLE
        else:
            settle_at = None


def _receive(sockets: Sequence[MulticastSocket],
             wait: float) -> tuple[bytes, str] | None:
    if len(sockets) == 1:
        return sockets[0].receive(wait)
    # The multi-interface case is the opt-in escape hatch, not the default, so a
    # coarse round-robin poll is the right trade: no fileno() on the Protocol,
    # and therefore nothing in the seam that a fake cannot implement.
    slice_s = min(wait, 0.02)
    for sock in sockets:
        packet = sock.receive(slice_s)
        if packet is not None:
            return packet
    return None


def _explain(exc: OSError, service: str) -> str:
    """Turn an errno into the sentence that tells the user what to do next."""
    import errno
    if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
        return (f"no route to {MDNS_GROUP}: a container without host networking "
                f"cannot use mDNS. Give an address instead, e.g. "
                f"'mbrelay connect 10.0.0.5:8760'.")
    if exc.errno in (errno.EPERM, errno.EACCES):
        return (f"the local firewall refused the multicast query "
                f"({errno.errorcode.get(exc.errno, exc.errno)}).")
    return f"could not query {service} over multicast: {exc.strerror or exc}"


def probe(service: Service, timeout: float = 0.3) -> bool:
    """Is anything actually listening where the advertisement says?

    An advertised-but-wedged node -- avahi-publish alive, daemon crash-looping --
    looks perfect in a browse. One TCP connect distinguishes them.
    """
    try:
        socket.create_connection((service.address, service.port), timeout).close()
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# the publish side
# ---------------------------------------------------------------------------
# Ordered by preference, and resolved with shutil.which exactly the way
# cmd_flash resolves mbdeploy. avahi-publish is the fleet answer; dns-sd is what
# a dev Mac has, so a laptop daemon advertises too instead of silently not.
PUBLISH_TOOLS = ("avahi-publish", "dns-sd")


def publish_argv(tool: str, instance: str, service: str, port: int,
                 txt: Sequence[str]) -> list[str]:
    if os.path.basename(tool).startswith("dns-sd"):
        return [tool, "-R", instance, service, "local", str(port), *txt]
    return [tool, "-s", instance, service, str(port), *txt]


class Advertiser:
    """Announces the daemon on the LAN by supervising a publisher child process.

    Modelled on AdminServer: constructed with the daemon, start() and stop()
    driven from Daemon.run(). A child process rather than a D-Bus client because
    the only D-Bus binding worth using is dbus-python, and this package has one
    dependency on purpose.

    The record lives exactly as long as the child does, which is the property we
    want: if the daemon is SIGKILLed the publisher dies with it and the record
    goes away, and a stale advertisement is worse than none.

    **This is a convenience and it must never stop boards being served.** Every
    failure path here logs and returns.
    """

    BACKOFF = (1.0, 2.0, 5.0, 15.0, 60.0)
    STOP_TIMEOUT = 1.0          # teardown must not eat into state.shutdown_grace_s
    HEALTHY_S = 30.0            # ran this long => the next failure retries fast

    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.cfg = daemon.cfg
        self.state = "disabled"
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        mdns = self.cfg.mdns
        if not mdns.enabled:
            log.debug("mdns advertisement disabled by config")
            return
        tool = self._find_tool()
        if tool is None:
            # The precedent is cmd_flash's missing-mbdeploy path: say how to fix
            # it, and carry on. avahi-utils is not installed by default on Ubuntu
            # Server, so this is the common case on a fresh node.
            self.state = f"unavailable ({mdns.publish_cmd} not found)"
            log.warning("mdns: %s not found, so this host will not advertise "
                        "itself. 'apt install avahi-utils' to enable it, or set "
                        "[mdns] enabled = false to silence this.", mdns.publish_cmd)
            return
        instance = mdns.instance or socket.gethostname().split(".")[0]
        port = self._port()
        argv = publish_argv(tool, instance, mdns.service, port, self._txt())
        self._stopping = False
        # Set before the task runs, so `mbrelay status` immediately after start
        # reports what is being advertised rather than the constructor's default.
        self.state = f"{instance}.{mdns.service} port {port}"
        self._task = asyncio.create_task(self._supervise(argv), name="mbrelay-mdns")
        log.info("mdns advertising %s as %s port %d via %s",
                 mdns.service, instance, port, tool)

    def _find_tool(self) -> str | None:
        candidates = [self.cfg.mdns.publish_cmd]
        candidates += [t for t in PUBLISH_TOOLS if t != self.cfg.mdns.publish_cmd]
        for candidate in candidates:
            if found := shutil.which(candidate):
                return found
        return None

    def _port(self) -> int:
        """The port actually being served.

        With server.port = 0 the config says 0 and the kernel says something
        else; announcing 0 would be worse than not announcing.
        """
        server = getattr(self.daemon, "server", None)
        if server is not None and getattr(server, "sockets", None):
            with contextlib.suppress(OSError, IndexError):
                return server.sockets[0].getsockname()[1]
        return self.cfg.server.port

    def _txt(self) -> list[str]:
        """Static facts only.

        A live free-board count would force a republish every time a client came
        or went, and the picker does not need it -- counts are `mbrelay status`'s
        job. Keeping the whole TXT under ~200 bytes also keeps SRV + TXT + A
        inside avahi's 512-byte legacy-reply cap.
        """
        from . import __version__
        txt = ["txtvers=1", f"version={__version__}",
               f"node={socket.gethostname().split('.')[0]}"]
        # Where to ask where a robot is. A client that found this host still
        # needs the registry, and one short key is well inside the cap.
        api = getattr(self.daemon, "httpapi", None)
        if api is not None and api.cfg.enabled:
            txt.append(f"registry={api.port}")
        return txt

    async def _supervise(self, argv: list[str]) -> None:
        attempt = 0
        while not self._stopping:
            started = time.monotonic()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE)
            except OSError as exc:
                self.state = f"failed ({exc.strerror or exc})"
                log.warning("mdns: cannot run %s: %s", argv[0], exc)
                return
            self.state = f"{argv[2]}.{self.cfg.mdns.service} port {self._port()}"
            # communicate() rather than wait(): avahi-publish says why it failed
            # on stderr ("Failed to create client object: Daemon not running"),
            # and an unread pipe is a way to deadlock on a chatty child.
            _, stderr = await self._proc.communicate()
            if self._stopping:
                return
            lived = time.monotonic() - started
            reason = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            detail = reason[-1] if reason else f"exit {self._proc.returncode}"
            # The overwhelmingly likely cause is avahi-daemon being down, and it
            # may well come back, so keep retrying -- but say so once at warning
            # and the rest at debug, or a stopped avahi floods the journal.
            level = log.warning if attempt == 0 else log.debug
            level("mdns: %s exited after %.1fs: %s", os.path.basename(argv[0]),
                  lived, detail)
            attempt = 0 if lived >= self.HEALTHY_S else min(attempt + 1,
                                                            len(self.BACKOFF) - 1)
            self.state = f"retrying ({detail})"
            await asyncio.sleep(self.BACKOFF[attempt])

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        proc, self._proc = self._proc, None
        self.state = "stopped"
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), self.STOP_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(proc.wait(), self.STOP_TIMEOUT)
