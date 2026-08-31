"""The DNS-SD codec, against canned bytes. Not one socket in this file.

This is where the value is. Everything above the codec is bookkeeping; the codec
is the part that meets other people's implementations, and a compression pointer
or an off-by-rdlength is the failure that only shows up against a real
responder, three weeks later, on somebody else's LAN.
"""

from __future__ import annotations

import struct
import time

import pytest

from mbrelay.mdns import (CLASS_IN, TYPE_PTR, MalformedPacket, decode_message,
                          decode_txt, encode_query, read_name)

from mdns_fixtures import (ADDRESS, HOSTNAME, INSTANCE, PORT, QU_IN, TXID,
                           TYPE_A, TYPE_AAAA, TYPE_LABELS, TYPE_TXT, VERSION,
                           Packet, a_rdata, reply, txt_rdata)


# -- encoding ---------------------------------------------------------------
def test_a_query_is_exactly_these_thirty_seven_bytes():
    """A golden packet, because a query is small enough to pin completely.

    Twelve bytes of header, "_mbrelay" "_tcp" "local" as length-prefixed labels
    with the root null, then QTYPE 12 (PTR) and QCLASS 0x8001 (IN, unicast bit).
    """
    assert encode_query("_mbrelay._tcp", 0x1234) == bytes.fromhex(
        "1234" "0000" "0001" "0000" "0000" "0000"
        "085f6d6272656c6179" "045f746370" "056c6f63616c" "00"
        "000c" "8001")
    assert len(encode_query("_mbrelay._tcp", 0x1234)) == 37


def test_the_query_asks_for_a_ptr_in_with_the_unicast_bit():
    question = decode_message(encode_query("_mbrelay._tcp", TXID)).questions[0]
    assert question.name == TYPE_LABELS
    assert (question.qtype, question.qclass) == (TYPE_PTR, CLASS_IN)
    assert question.unicast is True


def test_the_unicast_bit_can_be_left_off_for_a_retransmission():
    """RFC 6762 s5.4: SHOULD NOT set it on a retransmission."""
    question = decode_message(
        encode_query("_mbrelay._tcp", TXID, unicast=False)).questions[0]
    assert question.unicast is False


def test_a_service_type_without_a_domain_gets_local_appended():
    assert encode_query("_mbrelay._tcp") == encode_query("_mbrelay._tcp.local")


def test_a_label_longer_than_sixty_three_bytes_is_refused():
    with pytest.raises(MalformedPacket, match="DNS limit"):
        encode_query("_" + "x" * 64 + "._tcp")


# -- a whole reply ----------------------------------------------------------
def test_a_full_response_yields_instance_host_port_txt_and_address():
    records = decode_message(reply()).records
    by_type = {r.rtype: r for r in records}
    assert by_type[TYPE_PTR].value == (INSTANCE,) + TYPE_LABELS
    srv = by_type[33].value
    assert (srv.port, srv.target) == (PORT, HOSTNAME)
    assert by_type[TYPE_TXT].value["version"] == VERSION
    assert by_type[TYPE_A].value == ADDRESS


def test_the_echoed_question_does_not_look_like_class_32769():
    """avahi copies our question into the reply verbatim, QU bit included.

    Without masking 0x7FFF off *question* classes as well as record classes, the
    echoed question decodes as class 32769 and nothing matches IN.
    """
    question = decode_message(reply()).questions[0]
    assert question.qclass == CLASS_IN and question.unicast is True


def test_the_cache_flush_bit_is_masked_off_records():
    """s10.2 puts the same bit on an RRCLASS with an entirely different meaning."""
    assert all(record.rclass == CLASS_IN for record in decode_message(reply()).records)


def test_srv_txt_and_a_are_read_out_of_the_additional_section():
    """RFC 6763 s12 only says SHOULD, and avahi puts them here.

    Reading only the answer section finds the PTR and nothing else, which
    presents as "discovery works but every host has no address".
    """
    message = decode_message(reply())
    assert len(message.answers) == 1 and len(message.additional) == 3
    assert len(message.records) == 4


def test_an_instance_name_containing_a_dot_keeps_its_label_boundary():
    """"lab.bench-1" is one legal DNS-SD label, not two.

    Joining labels into a string would silently turn it into a subdomain of the
    service type, and the instance would never match its own SRV.
    """
    message = decode_message(reply(instance="lab.bench-1"))
    ptr = next(r for r in message.records if r.rtype == TYPE_PTR)
    assert ptr.value == ("lab.bench-1",) + TYPE_LABELS


def test_aaaa_decodes_to_a_v6_string():
    packet = Packet()
    packet.header(TXID, 0x8400, (0, 1, 0, 0))
    packet.record(HOSTNAME, TYPE_AAAA,
                  bytes.fromhex("fe800000000000000000000000000001"))
    assert decode_message(packet.bytes).answers[0].value == "fe80::1"


def test_an_unknown_record_type_is_skipped_rather_than_fatal():
    """A neighbour advertising HINFO or OPT must not cost us the datagram."""
    packet = Packet()
    packet.header(TXID, 0x8400, (0, 2, 0, 0))
    packet.record(HOSTNAME, 99, b"\x01\x02\x03")
    packet.record(HOSTNAME, TYPE_A, a_rdata())
    records = decode_message(packet.bytes).answers
    assert records[0].value == b"\x01\x02\x03"
    assert records[1].value == ADDRESS


# -- name compression, the number one bug source ----------------------------
def test_a_compression_pointer_into_the_question_resolves():
    """The shape every responder emits: the answer's name is two bytes."""
    raw = reply()
    # Offset 12 is the question name; the PTR answer points straight at it.
    assert b"\xc0\x0c" in raw
    assert decode_message(raw).answers[0].name == TYPE_LABELS


def test_a_self_referential_pointer_raises_instead_of_hanging():
    packet = bytearray(struct.pack("!6H", TXID, 0x8400, 1, 0, 0, 0))
    packet += b"\xc0\x0c"          # the name at offset 12 points to offset 12
    started = time.monotonic()
    with pytest.raises(MalformedPacket, match="strictly backwards"):
        decode_message(bytes(packet))
    assert time.monotonic() - started < 0.1


def test_a_forward_pointer_is_rejected():
    """Requiring target < pos is what makes termination provable."""
    packet = bytearray(struct.pack("!6H", TXID, 0x8400, 1, 0, 0, 0))
    packet += b"\xc0\x20"          # offset 12 points forward to offset 32
    with pytest.raises(MalformedPacket, match="strictly backwards"):
        decode_message(bytes(packet))


def test_a_pointer_chain_that_revisits_a_target_is_rejected():
    """Belt and braces: strictly-backwards alone does not catch this one.

    The name at 30 reads a label, lands at 32, and points back to 30 -- which is
    a legal backwards jump every time round, so only the visited set stops it.
    """
    data = bytearray(b"\x00" * 30)
    data[0:12] = struct.pack("!6H", TXID, 0x8400, 0, 0, 0, 0)
    data += b"\x01a" + b"\xc0\x1e"          # label "a" at 30, pointer at 32 -> 30
    with pytest.raises(MalformedPacket):
        read_name(bytes(data), 30)


def test_rdlength_wins_over_where_the_name_parse_ended():
    """An NSEC's rdata starts with a name and continues with a type bitmap.

    Advancing by the name parse rather than by rdlength shifts every following
    record, and the A after it decodes as garbage -- the failure that only ever
    appears against a real responder.
    """
    packet = Packet()
    packet.header(TXID, 0x8400, (0, 2, 0, 0))
    packet.record(HOSTNAME, 47,          # NSEC: next-domain-name + bitmap
                  lambda p: (p.name(HOSTNAME),
                             p.buf.extend(b"\x00\x04\x40\x00\x00\x08")))
    packet.record(HOSTNAME, TYPE_A, a_rdata())
    records = decode_message(packet.bytes).answers
    assert records[1].rtype == TYPE_A and records[1].value == ADDRESS


def test_every_truncation_either_decodes_or_raises_malformed_packet():
    """Five lines that catch more than the other tests put together.

    A datagram can be short for any reason -- a 512-byte cap, a lost fragment, a
    hostile neighbour -- and the one outcome that is not allowed is an exception
    the browse loop does not catch.
    """
    full = reply()
    for length in range(len(full) + 1):
        try:
            decode_message(full[:length])
        except MalformedPacket:
            pass


# -- TXT, per RFC 6763 s6 ---------------------------------------------------
def test_txt_handles_key_value_bare_key_and_empty_value():
    assert decode_txt(txt_rdata(("a=1", "flag", "empty="))) == {
        "a": "1", "flag": "", "empty": ""}


def test_txt_keys_are_lowercased_and_the_first_occurrence_wins():
    assert decode_txt(txt_rdata(("Version=1", "VERSION=2"))) == {"version": "1"}


def test_txt_discards_a_string_with_no_key():
    assert decode_txt(txt_rdata(("=orphan", "node=torture"))) == {"node": "torture"}


def test_an_empty_txt_record_is_an_empty_dict():
    """One zero-length string is the canonical empty TXT, not a malformed one."""
    assert decode_txt(txt_rdata(())) == {}
    assert decode_txt(b"") == {}


def test_a_txt_string_running_past_its_record_is_malformed():
    with pytest.raises(MalformedPacket, match="runs past"):
        decode_txt(b"\x09short")


# -- header sanity ----------------------------------------------------------
def test_a_packet_shorter_than_a_header_is_malformed():
    with pytest.raises(MalformedPacket, match="shorter than a header"):
        decode_message(b"\x00" * 11)


def test_a_reserved_label_type_is_refused():
    packet = bytearray(struct.pack("!6H", TXID, 0x8400, 1, 0, 0, 0))
    packet += b"\x80\x00"          # 0x40 and 0x80 are reserved label types
    with pytest.raises(MalformedPacket, match="reserved label type"):
        decode_message(bytes(packet))


def test_the_question_class_reported_by_a_reply_is_still_in():
    assert decode_message(reply()).questions[0].qclass == QU_IN & 0x7FFF
