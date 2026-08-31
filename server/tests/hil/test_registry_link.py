"""Tuning a real board to where the registry says a robot is.

`!N <name>` used to live in the firmware and is gone: a name only yields a
DEFAULT address, and the board cannot see the registry, so a tune-by-name
command would mistune exactly the robots that were moved off their default
because of a conflict. Everything now goes through `!CG <channel> <group>`.

What that buys, and what these tests pin down: `!CG` is as old as the protocol,
so this works on EVERY firmware in the fleet -- including boards that were
never reflashed for the named-link work. There is no version skip here any
more, and a board answering `!N?` at all is now a stale-firmware finding.
"""

from __future__ import annotations

import pytest

from mbrelay.naming import name_to_radio
from mbrelay.registry import NameRegistry

from test_reset_semantics import CONFIG_RE, _restore, _talk, reset_board


def _first_board(attached, borrowed) -> str:
    return borrowed(sorted(p.device for p in attached.values())[0])


def _last_pair(data: bytes) -> tuple[int, int]:
    pairs = CONFIG_RE.findall(data)
    assert pairs, f"no config line: {data[-200:]!r}"
    channel, group = pairs[-1]
    return int(channel), int(group)


def test_a_board_lands_on_the_pair_the_registry_gives(attached, borrowed, tmp_path):
    """The registry's answer and the board's actual radio config must agree, or
    `mbrelay connect <robot>` tunes somewhere the robot is not."""
    from mbrelay.config import load as load_config

    registry = NameRegistry(load_config(overrides={"state.dir": str(tmp_path)},
                                        environ={}))
    entry = registry.resolve("tovez")
    assert (entry.channel, entry.group) == name_to_radio("tovez")

    port = _first_board(attached, borrowed)
    try:
        data = _talk(port, [f"!CG {entry.channel} {entry.group}\n".encode(), b"?\n"])
        assert _last_pair(data) == (entry.channel, entry.group)
    finally:
        _restore(port)


def test_an_overridden_pair_reaches_the_board_unchanged(attached, borrowed, tmp_path):
    """The whole point of the registry: a robot moved off its derived address
    is reachable at the new one. 12/4 is nowhere in the derived space."""
    from mbrelay.config import load as load_config

    registry = NameRegistry(load_config(overrides={"state.dir": str(tmp_path)},
                                        environ={}))
    entry = registry.set("tovez", 12, 4)
    assert not entry.derived

    port = _first_board(attached, borrowed)
    try:
        data = _talk(port, [f"!CG {entry.channel} {entry.group}\n".encode(), b"?\n"])
        assert _last_pair(data) == (12, 4)
        reset_board(port)
        # Like the rest of the config, the link is in flash and survives.
        assert _last_pair(_talk(port, [b"HELLO\n", b"?\n"])) == (12, 4)
    finally:
        _restore(port)


def test_the_firmware_no_longer_carries_a_tune_by_name_command(attached, borrowed):
    """A board that still answers `!N` is running firmware from before the
    registry. It would still WORK -- the client only sends `!CG` -- but it can
    tune itself somewhere the registry disagrees with, so flag it."""
    port = _first_board(attached, borrowed)
    try:
        data = _talk(port, [b"!N?\n"])
        assert b"unknown command" in data, \
            "this relay predates the registry; reflash it (it is not broken, " \
            "but its !N can put it somewhere the registry does not expect)"
    finally:
        _restore(port)


def test_a_pair_outside_the_firmwares_range_is_refused_by_the_board(attached, borrowed):
    """The registry range-checks against exactly this, so the two must agree."""
    port = _first_board(attached, borrowed)
    try:
        data = _talk(port, [b"!C 3\n", b"!CG 200 4\n", b"?\n"])
        assert b"usage !CG" in data
        assert _last_pair(data) == (3, 10), "a refused pair must change nothing"
    finally:
        _restore(port)
