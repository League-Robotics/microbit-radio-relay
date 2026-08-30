"""The name -> (channel, group) mapping is a contract with the firmware and the
robot's MakeCode extension. These tests pin it; a change here is a change in
all three places."""

import itertools

import pytest

from mbrelay import naming
from mbrelay.naming import VECTORS, name_hash, name_to_radio, normalize, validate

from fake_relay import FakeRelayFirmware


@pytest.mark.parametrize("name,h,channel,group", VECTORS)
def test_the_shared_vectors(name, h, channel, group):
    assert name_hash(name) == h
    assert name_to_radio(name) == (channel, group)


def test_case_and_surrounding_whitespace_do_not_make_a_different_robot():
    assert name_to_radio("TOVEZ") == name_to_radio(" tovez ") == name_to_radio("tovez")
    assert normalize("  ToVeZ\r\n") == "tovez"


@pytest.mark.parametrize("bad", ["", "   ", "to vez", "a" * 16, "t\x00vez", "névé"])
def test_names_the_firmware_would_reject_are_rejected_here_too(bad):
    with pytest.raises(ValueError):
        validate(bad)


def _friendly_names():
    """Every CODAL friendly name: consonant-vowel-consonant-vowel-consonant
    over a 5x5 alphabet -- 3125 names."""
    consonants, vowels = "zvgpt", "aeiou"
    return ["".join(p) for p in itertools.product(consonants, vowels,
                                                   consonants, vowels, consonants)]


def test_every_friendly_name_lands_in_range_and_never_on_the_button_group():
    """Group 10 is what !C and the A/B buttons force. A named link that landed
    there could be joined by accident from a relay's buttons."""
    for name in _friendly_names():
        channel, group = name_to_radio(name)
        assert 0 <= channel <= 83
        assert 0 <= group <= 255
        assert group != naming.RESERVED_GROUP


def test_the_friendly_name_space_spreads_over_the_channels():
    """Not a correctness property, a sanity one: the hash must not funnel the
    real name space onto a handful of channels. Distinct links over the 3125
    friendly names, and no channel starved."""
    names = _friendly_names()
    links = {name_to_radio(n) for n in names}
    assert len(links) > 0.95 * len(names)
    per_channel = [0] * naming.CHANNELS
    for n in names:
        per_channel[name_to_radio(n)[0]] += 1
    assert min(per_channel) > 0


def test_the_fake_firmware_speaks_the_named_link_grammar():
    """fake_relay.py is what the server suite runs against, so it must answer
    `!N` byte-for-byte the way the board does -- name LAST on the config line,
    so parsers anchored on `# channel:` keep working."""
    fw = FakeRelayFirmware()
    fw.feed(b"!N Tovez\n")
    assert fw.drain() == b"# channel: 69 group: 214 mode: RAW250 power: 7 name: tovez\r\n"
    fw.feed(b"!N?\n")
    assert fw.drain() == b"# name: tovez\r\n"
    fw.feed(b"?\n")
    assert fw.drain() == b"# channel: 69 group: 214 mode: RAW250 power: 7 name: tovez\r\n"

    fw.feed(b"!C 3\n")                                 # a number forgets the name
    assert fw.drain() == b"# channel: 3 group: 10 mode: RAW250 power: 7\r\n"
    fw.feed(b"!N?\n")
    assert fw.drain() == b"# name: -\r\n"

    fw.feed(b"!N to vez\n")
    assert fw.drain() == b"# error: usage !N <name>\r\n"


def test_the_name_survives_a_reset_like_the_rest_of_the_config():
    fw = FakeRelayFirmware()
    fw.feed(b"!N vevov\n"); fw.drain()
    fw.reset(); fw.drain()
    fw.feed(b"?\n")
    assert fw.drain().endswith(b"power: 7 name: vevov\r\n")
