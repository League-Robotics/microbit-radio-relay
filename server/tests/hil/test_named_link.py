"""`!N <name>` on a real board (§3.7 of the protocol).

Pins the three things the daemon and the host library rely on: the pair the
firmware computes is the pair naming.py predicts, the name persists across a
reset like the rest of the config, and choosing a link by number forgets it.

A relay running firmware older than §3.7 answers `!N?` with "unknown command";
those boards SKIP rather than fail, so the bench can be upgraded one relay at a
time without turning the suite red.
"""

from __future__ import annotations

import re

import pytest

from mbrelay.naming import name_to_radio

from test_reset_semantics import CONFIG_RE, _restore, _talk, reset_board

NAME_RE = re.compile(rb"#\s*name:\s*(\S+)")


def _first_board(attached, borrowed) -> str:
    port = borrowed(sorted(p.device for p in attached.values())[0])
    if not NAME_RE.search(_talk(port, [b"HELLO\n", b"!N?\n"])):
        _restore(port)
        pytest.skip("this relay's firmware predates !N (protocol §3.7)")
    return port


def _last_pair(data: bytes) -> tuple[int, int]:
    pairs = CONFIG_RE.findall(data)
    assert pairs, f"no config line: {data[-200:]!r}"
    channel, group = pairs[-1]
    return int(channel), int(group)


def test_a_name_selects_the_link_naming_py_predicts(attached, borrowed):
    """The firmware and naming.py must agree byte-for-byte, or `mbrelay status`
    reports a channel the board is not on and a host computing the pair in
    Python talks to the wrong robot."""
    port = _first_board(attached, borrowed)
    try:
        data = _talk(port, [b"!N Tovez\n", b"!N?\n"])
        assert _last_pair(data) == name_to_radio("tovez")
        assert NAME_RE.findall(data)[-1] == b"tovez", "not lower-cased"
        assert b"power:" in data and data.index(b"name: tovez") > data.index(b"power:"), \
            "name must be the LAST field so '# channel:' parsers keep matching"
    finally:
        _restore(port)


def test_the_name_survives_a_reset_and_a_number_forgets_it(attached, borrowed):
    port = _first_board(attached, borrowed)
    try:
        _talk(port, [b"!N vevov\n"])
        reset_board(port)
        data = _talk(port, [b"HELLO\n", b"?\n", b"!N?\n"])
        assert _last_pair(data) == name_to_radio("vevov"), "the link did not survive"
        assert NAME_RE.findall(data)[-1] == b"vevov", "the name did not survive"

        data = _talk(port, [b"!C 0\n", b"!N?\n"])
        assert _last_pair(data) == (0, 10)
        assert NAME_RE.findall(data)[-1] == b"-", "!C must forget the name"
    finally:
        _restore(port)


def test_a_bad_name_is_refused_and_changes_nothing(attached, borrowed):
    port = _first_board(attached, borrowed)
    try:
        data = _talk(port, [b"!C 3\n", b"!N to vez\n", b"?\n"])
        assert b"usage !N" in data
        assert _last_pair(data) == (3, 10)
        assert b"name:" not in data.split(b"usage !N")[-1]
    finally:
        _restore(port)
