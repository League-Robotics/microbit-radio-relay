"""name -> (channel, group) is a contract between the relay firmware, mbrelay
and the robot. The normative spec is docs/design/radio-addressing.md in
radio-robot-lib; radio-address-vectors.json transcribes its digests and
vectors, and these tests assert against that file -- the whole 3125-name space
via its digest -- never against prose."""

import hashlib
import json
import pathlib

import pytest

from mbrelay import naming
from mbrelay.naming import (address, decode, encode, name_to_radio, radio_to_name,
                            validate)


SPEC = json.loads(pathlib.Path(__file__).with_name("radio-address-vectors.json").read_text())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_the_conformance_gate_d2_exercises_the_decoder_the_relay_runs():
    """D2 hashes decode(name) and reverse(channel, group) for every name. It is
    the gate because decode() is what `!N` and a registry lookup execute, and
    D1 never calls it. The spec publishes a little-endian decoder's D2 so that
    fault is nameable."""
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
    exist and must not pretend to."""
    assert name_to_radio("pipip") == (34, 59)
    with pytest.raises(ValueError):
        name_to_radio("robot1")            # malformed: no address exists


@pytest.mark.parametrize("v", SPEC["vectors"], ids=lambda v: v["name"])
def test_the_published_vectors(v):
    assert decode(v["name"]) == v["n"]
    assert encode(v["n"]) == v["name"]
    assert name_to_radio(v["name"]) == (v["channel"], v["group"])
    assert radio_to_name(v["channel"], v["group"]) == v["name"]
    if v.get("evidence") == "silicon":
        # The whole scheme rests on this: the name IS the device id mod 3125.
        assert v["device_id"] % naming.SPACE == v["n"]


def test_the_endianness_probes_are_not_palindromes():
    """zuzuz / tatat / zavaz read the same in either digit order and cannot
    catch a reversed encoder; zuzuv and zotuz can."""
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


def test_every_address_is_in_range_and_clear_of_the_reserved_low_values():
    """Channels 0-10 and groups 0-14 are never emitted: the legacy fleet's
    3/4/5, MakeCode's 7/0, and the relay's !C group 10."""
    ranges = SPEC["ranges"]
    for n in range(naming.SPACE):
        channel, group = address(n)
        assert ranges["channel"]["min"] <= channel <= ranges["channel"]["max"]
        assert ranges["group"]["min"] <= group <= ranges["group"]["max"]


def test_every_name_has_its_own_pair_and_channels_are_shared_evenly():
    """A pair is unique (73 and 241 are coprime, 3125 < 73 * 241); a channel
    is not -- that is what the registry's channel-conflict warning is for."""
    pairs = [address(n) for n in range(naming.SPACE)]
    assert len(set(pairs)) == SPEC["properties"]["distinct_pairs"] == naming.SPACE
    per_channel: dict[int, int] = {}
    per_group: dict[int, int] = {}
    for channel, group in pairs:
        per_channel[channel] = per_channel.get(channel, 0) + 1
        per_group[group] = per_group.get(group, 0) + 1
    assert per_channel == {ch: 43 if ch <= 69 else 42 for ch in range(11, 84)}
    assert per_group == {g: 13 if g <= 247 else 12 for g in range(15, 256)}


def test_no_two_fleet_robots_share_a_channel():
    """The spec's reason for the change: under the old map vevov and togov
    both sat on 37."""
    robots = [v for v in SPEC["vectors"] if v.get("role") == "robot"]
    assert len({v["channel"] for v in robots}) == len(robots)


def test_every_name_round_trips_through_its_address():
    for n in range(naming.SPACE):
        assert radio_to_name(*address(n)) == encode(n)


@pytest.mark.parametrize("channel,group", [
    *map(tuple, SPEC["no_name_pairs"]),
    (0, 10), (3, 10), (7, 0), (10, 15), (84, 15), (11, 14), (11, 256),
    (83, 255),                      # n = 17592: in range, but no name reaches it
])
def test_pairs_no_name_derives_are_refused(channel, group):
    with pytest.raises(ValueError):
        radio_to_name(channel, group)
