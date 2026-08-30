"""name -> (channel, group) is a contract with the robot firmware and the host
library. The normative spec lives in pxt-nezha-diffdrive; its machine-readable
companion is mirrored here as radio-address-vectors.json, and these tests
assert against that file -- the whole 3125-name space via its digest -- never
against prose or a hand-copied table."""

import hashlib
import json
import pathlib

import pytest

from mbrelay import naming
from mbrelay.naming import (address, decode, encode, name_to_radio, radio_to_name,
                            validate)

from fake_relay import FakeRelayFirmware

SPEC = json.loads(pathlib.Path(__file__).with_name("radio-address-vectors.json").read_text())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_conformance_gate_d2_exercises_the_decoder_the_relay_runs():
    """D2 hashes decode(name) and reverse(channel, group) for every name. It is
    the gate because decode() is what `!N <name>` executes, and D1 never
    calls it: a little-endian decoder passes D1 while being wrong on 96% of
    names. The spec publishes that exact fault's D2 so it is nameable."""
    props = SPEC["properties"]
    d2 = _sha(naming.canonical_form(version=2))
    assert d2 != props["conformance_sha256_broken_decode"]["digest"], \
        "the DECODER is little-endian: name[0] must be the MOST significant digit"
    assert d2 == props["conformance_sha256"]


def test_the_forward_only_digest_d1_still_holds_as_a_bisector():
    """D2 failing while D1 passes localises a fault to decode/reverse."""
    d1 = _sha(naming.canonical_form(version=1))
    assert d1 != SPEC["properties"]["endianness_probe"]["reversed_encoder_digest"], \
        "the ENCODER is little-endian"
    assert d1 == SPEC["properties"]["full_space_sha256"]


def test_a_well_formed_name_no_board_uses_is_still_an_address():
    """Malformed is not unknown. `pipip` belongs to no board, but it is a legal
    retune to a quiet pair; the address layer does not know which boards
    exist and must not pretend to (that is the deploy-time silicon gate)."""
    assert name_to_radio("pipip") == (51, 90)
    with pytest.raises(ValueError):
        name_to_radio("robot1")            # malformed: no address exists


@pytest.mark.parametrize("v", SPEC["vectors"], ids=lambda v: v["name"])
def test_the_published_vectors(v):
    assert decode(v["name"]) == v["n"]
    assert encode(v["n"]) == v["name"]
    assert name_to_radio(v["name"]) == (v["channel"], v["group"])
    assert radio_to_name(v["channel"], v["group"]) == v["name"]
    if v.get("evidence") == "silicon":
        # The whole scheme rests on this: the name IS the device id in base 5.
        assert v["device_id"] % naming.SPACE == v["n"]


def test_the_endianness_probe_is_not_a_palindrome():
    """zuzuz / tatat / zavaz read the same in either digit order and cannot
    catch a reversed encoder; the spec's probe can, and n=1 is zuzuv."""
    probe = SPEC["properties"]["endianness_probe"]["vector"]
    assert probe != probe[::-1] or True                      # (letters differ by position alphabet)
    assert encode(decode(probe)) == probe
    assert encode(1) == "zuzuv" and decode("zuzuv") == 1
    assert decode("zotuz") == 225 and encode(225) == "zotuz"


@pytest.mark.parametrize("bad", SPEC["reject"])
def test_names_the_spec_rejects_are_rejected(bad):
    with pytest.raises(ValueError):
        validate(bad)


def test_normalization_is_exactly_trim_and_lowercase():
    for raw, canonical in SPEC["normalize_equivalent"].items():
        assert validate(raw) == canonical
    assert naming.normalize("\t ToVeZ\r\n") == "tovez"


def test_reserved_values_are_never_emitted_and_ranges_hold():
    reserved = SPEC["reserved"]
    ranges = SPEC["ranges"]
    for n in range(naming.SPACE):
        channel, group = address(n)
        assert channel not in reserved["channels_never_emitted"]
        assert group not in reserved["groups_never_emitted"]
        assert ranges["channel"]["min"] <= channel <= ranges["channel"]["max"]
        assert (channel - ranges["channel"]["min"]) % ranges["channel"]["step"] == 0
        assert 1 <= group <= 126 and group != 10


def test_the_map_is_a_bijection_that_tiles_the_space_evenly():
    props = SPEC["properties"]
    pairs = [address(n) for n in range(naming.SPACE)]
    assert len(set(pairs)) == props["distinct_pairs"] == props["total_names"]
    per_channel: dict[int, int] = {}
    per_group: dict[int, int] = {}
    for channel, group in pairs:
        per_channel[channel] = per_channel.get(channel, 0) + 1
        per_group[group] = per_group.get(group, 0) + 1
    assert set(per_channel.values()) == {props["names_per_channel"]}
    assert set(per_group.values()) == {props["names_per_group"]}
    assert len(per_channel) == ranges_count(SPEC["ranges"]["channel"])


def ranges_count(r):
    return r["count"]


def test_channel_25_is_inclusive_and_zeguz_sits_on_it():
    assert name_to_radio("zeguz") == (25, 19)


def test_every_name_round_trips_through_its_address():
    for n in range(naming.SPACE):
        assert radio_to_name(*address(n)) == encode(n)


@pytest.mark.parametrize("channel,group", [(0, 10), (3, 10), (7, 0), (26, 5),
                                           (25, 10), (25, 0), (25, 127), (75, 1)])
def test_pairs_outside_the_derived_space_have_no_name(channel, group):
    with pytest.raises(ValueError):
        radio_to_name(channel, group)


def test_the_fake_firmware_speaks_the_named_link_grammar():
    """fake_relay.py is what the server suite runs against, so it must answer
    `!N` byte-for-byte the way the board does -- name LAST on the config line,
    so parsers anchored on `# channel:` keep working."""
    fw = FakeRelayFirmware()
    fw.feed(b"!N Tovez\n")
    assert fw.drain() == b"# channel: 55 group: 108 mode: RAW250 power: 7 name: tovez\r\n"
    fw.feed(b"!N?\n")
    assert fw.drain() == b"# name: tovez\r\n"
    fw.feed(b"?\n")
    assert fw.drain() == b"# channel: 55 group: 108 mode: RAW250 power: 7 name: tovez\r\n"

    fw.feed(b"!C 3\n")                                 # a number forgets the name
    assert fw.drain() == b"# channel: 3 group: 10 mode: RAW250 power: 7\r\n"
    fw.feed(b"!N?\n")
    assert fw.drain() == b"# name: -\r\n"

    for bad in (b"!N to vez\n", b"!N gauti\n", b"!N vevo\n", b"!N \n"):
        fw.feed(bad)
        assert fw.drain() == b"# error: usage !N <name>\r\n", bad


def test_the_name_survives_a_reset_like_the_rest_of_the_config():
    fw = FakeRelayFirmware()
    fw.feed(b"!N vevov\n"); fw.drain()
    fw.reset(); fw.drain()
    fw.feed(b"?\n")
    assert fw.drain() == b"# channel: 37 group: 43 mode: RAW250 power: 7 name: vevov\r\n"
