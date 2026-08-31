"""The browse loop and the record collector, against the fake socket.

Every test here runs in microseconds and touches no network. The invariant being
defended throughout: ``browse`` has no ``raise`` on any path. Discovery is a
convenience, and a firewall or one broken neighbour must degrade to a list and a
sentence -- never to a traceback in the middle of `mbrelay connect`.
"""

from __future__ import annotations

import errno

import pytest

from mbrelay.mdns import browse, browse_detailed, decode_message

from fake_mdns import FakeClock, FakeMdnsSocket, one_socket
from mdns_fixtures import ADDRESS, INSTANCE, PORT, VERSION, reply


def run(*answers, expect=0, timeout=1.5, clock=None, sock=None, **kwargs):
    clock = clock or FakeClock()
    sock = sock or FakeMdnsSocket(answers=list(answers), clock=clock)
    result = browse_detailed(timeout=timeout, expect=expect,
                             open_socket=one_socket(sock), clock=clock,
                             resolver=lambda host: (), **kwargs)
    return result, sock


# -- correlation ------------------------------------------------------------
def test_one_packet_with_everything_yields_one_service():
    result, _ = run(reply())
    service, = result.services
    assert service.instance == INSTANCE
    assert service.hostname == "torture.local"
    assert (service.addresses, service.port) == ((ADDRESS,), PORT)
    assert service.version == VERSION


def test_records_split_across_datagrams_are_joined():
    """A responder may split its answer however it likes, so the collector is
    additive rather than per-packet."""
    result, _ = run(reply(sections=("ptr",)), reply(sections=("srv", "txt")),
                    reply(sections=("a",)))
    service, = result.services
    assert (service.hostname, service.port, service.addresses) == (
        "torture.local", PORT, (ADDRESS,))


def test_an_address_in_a_datagram_naming_no_instance_still_joins():
    """Addresses are keyed on the SRV target host, globally.

    One host can publish several instances, and the A record routinely arrives
    in a datagram that mentions no instance at all.
    """
    result, _ = run(reply(sections=("ptr", "srv")), reply(sections=("a",)))
    assert result.services[0].addresses == (ADDRESS,)


def test_two_nodes_both_appear():
    result, _ = run(reply(),
                    reply(instance="agony", hostname=("agony", "local"),
                          address="192.168.1.19"))
    assert [s.instance for s in result.services] == ["agony", "torture"]
    assert [s.address for s in result.services] == ["192.168.1.19", ADDRESS]


def test_the_same_service_heard_on_two_interfaces_is_one_entry_with_both_addresses():
    """Dedup is free: every store is a setdefault or an `in` check, so there is
    no dedup pass because there is nothing left to dedup."""
    clock = FakeClock()
    first = FakeMdnsSocket(answers=[reply()], clock=clock)
    second = FakeMdnsSocket(answers=[reply(address="10.4.0.12")], clock=clock,
                            source="10.4.0.1")
    result = browse_detailed(interfaces=("192.168.1.40", "10.4.0.1"),
                             open_socket=one_socket(first, second), clock=clock,
                             resolver=lambda host: ())
    service, = result.services
    assert service.addresses == (ADDRESS, "10.4.0.12")
    assert result.source == "192.168.1.40, 10.4.0.1"


def test_a_ttl_zero_goodbye_withdraws_a_service():
    """RFC 6762 s10.1. Applied after the whole datagram, so an SRV further down
    the same packet cannot resurrect it."""
    result, _ = run(reply(), reply(sections=("ptr",), ptr_ttl=0))
    assert result.services == ()


def test_a_service_with_no_address_is_filled_in_by_the_resolver():
    """avahi caps a legacy-unicast reply at 512 bytes, so a busy node answers
    SRV with the A silently dropped off the end."""
    clock = FakeClock()
    sock = FakeMdnsSocket(answers=[reply(sections=("ptr", "srv", "txt"))], clock=clock)
    result = browse_detailed(open_socket=one_socket(sock), clock=clock,
                             resolver=lambda host: ("192.168.1.99",))
    assert result.services[0].addresses == ("192.168.1.99",)


def test_an_unresolvable_host_keeps_its_name_rather_than_inventing_an_address():
    result, _ = run(reply(sections=("ptr", "srv")))
    service, = result.services
    assert service.addresses == () and service.address == "torture.local"


# -- what must be ignored ---------------------------------------------------
def test_a_response_with_a_foreign_transaction_id_is_ignored():
    """s6.7 requires the responder to repeat our query id. Anything else is a
    reply to somebody else's query that happened to reach our socket."""
    clock = FakeClock()
    sock = FakeMdnsSocket(answers=[reply()], clock=clock, echo_txid=False)
    result, _ = run(clock=clock, sock=sock)
    assert result.services == ()


def test_a_query_is_ignored_rather_than_parsed_as_an_answer():
    """IP_MULTICAST_LOOP is on so a daemon on this host is discoverable, which
    means our own query comes straight back at us."""
    result, _ = run(reply(flags=0x0000))
    assert result.services == ()


def test_a_ptr_for_a_different_service_type_is_ignored():
    result, _ = run(reply(service=("_http", "_tcp", "local")))
    assert result.services == ()


def test_one_malformed_datagram_does_not_abort_the_browse():
    """One neighbour with a broken responder must not cost us the whole browse."""
    result, _ = run(b"\x00\x01\x02", reply())
    assert len(result.services) == 1


# -- the query schedule -----------------------------------------------------
def test_three_queries_go_out_with_three_distinct_ids():
    result, sock = run()
    assert len(sock.sent) == 3 == result.queries
    assert len(set(sock.txids)) == 3


def test_the_unicast_bit_is_set_only_on_the_first_query():
    """s5.4 says SHOULD NOT on a retransmission. We do not depend on it either
    way -- the ephemeral source port is what earns the unicast reply."""
    _, sock = run()
    unicast = [decode_message(q).questions[0].unicast for q in sock.sent]
    assert unicast == [True, False, False]


def test_nothing_answering_spends_exactly_the_timeout():
    clock = FakeClock()
    result, _ = run(clock=clock, timeout=1.5)
    assert result.services == () and result.elapsed == 1.5


def test_a_fully_resolved_browse_returns_before_the_timeout():
    """What `connect` wants: one answer is enough, so do not sit out the budget."""
    result, _ = run(reply(), expect=1, timeout=1.5)
    assert len(result.services) == 1
    assert result.elapsed < 1.5


def test_discover_runs_the_full_budget_even_after_an_answer():
    """A truncated fleet listing is far more annoying than a 1.5s pause, so
    expect=0 never returns early."""
    result, _ = run(reply(), expect=0, timeout=1.5)
    assert result.elapsed == 1.5


def test_the_socket_is_closed_even_when_nothing_answers():
    _, sock = run()
    assert sock.closed


# -- failure reporting ------------------------------------------------------
def test_a_socket_that_cannot_be_opened_reports_a_problem_not_a_traceback():
    def refuse(interface: str = ""):
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    result = browse_detailed(open_socket=refuse, clock=FakeClock())
    assert result.services == () and "no route to 224.0.0.251" in result.problem
    assert "mbrelay connect 10.0.0.5:8760" in result.problem
    assert browse(open_socket=refuse, clock=FakeClock()) == []


def test_a_firewall_rejecting_the_query_says_so():
    clock = FakeClock()
    sock = FakeMdnsSocket(clock=clock, fail_send=OSError(errno.EPERM, "denied"))
    result = browse_detailed(open_socket=one_socket(sock), clock=clock)
    assert "firewall" in result.problem and "EPERM" in result.problem


def test_finding_nothing_names_the_address_we_queried_from():
    """A wrong NIC and a firewall drop are indistinguishable on the wire, so the
    message has to name both possibilities and the source it used."""
    result, _ = run()
    assert "192.168.1.40" in result.problem
    assert "avahi-daemon" in result.problem and "firewall" in result.problem
    assert result.source == "192.168.1.40"


def test_a_bad_service_type_is_a_problem_rather_than_an_exception():
    result = browse_detailed("_" + "x" * 64 + "._tcp", clock=FakeClock())
    assert result.services == () and "DNS limit" in result.problem


def test_a_resolver_that_explodes_does_not_escape_browse():
    clock = FakeClock()
    sock = FakeMdnsSocket(answers=[reply(sections=("ptr", "srv"))], clock=clock)

    def boom(host):
        raise RuntimeError("no such thing")

    result = browse_detailed(open_socket=one_socket(sock), clock=clock, resolver=boom)
    assert result.services[0].addresses == ()


# -- the real socket, opened but never used ---------------------------------
def test_opening_a_real_socket_binds_an_ephemeral_port_and_sends_nothing():
    """The whole design rests on this: an ephemeral source port is what puts us
    on RFC 6762 s6.7's MUST-answer-by-unicast path. Binding 5353 would not.

    Constructing the socket emits no traffic, so this is safe in CI -- but a
    sandbox with no network at all is allowed to refuse, hence the skip.
    """
    from mbrelay.mdns import UdpMulticastSocket
    try:
        sock = UdpMulticastSocket()
    except OSError as exc:
        pytest.skip(f"no multicast-capable socket here: {exc}")
    try:
        assert sock.local_port not in (0, 5353)
    finally:
        sock.close()
