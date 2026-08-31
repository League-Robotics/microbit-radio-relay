"""A micro:bit's name gives its DEFAULT radio address.

The five-letter CODAL friendly name is a base-5 encoding of the chip's
``NRF_FICR->DEVICEID[1]``, so a board derives its own ``(channel, group)`` at
boot and any tool that knows the name derives the same pair, with no
coordination at all.

That is a default, not an address. 3125 names share 25 channels, so 125 names
land on each one; when two robots collide, one has to move, and its name then
no longer says where it is. ``registry.py`` is what records the exceptions, and
this module is what it calls to compute the default in the first place. Nothing
here knows about overrides -- keeping the mapping pure is what lets its digest
stay a cross-repo contract.

Normative spec: ``docs/radio-addressing.md`` in pxt-nezha-diffdrive, with the
machine-readable contract ``docs/radio-address-vectors.json``. This repo mirrors
that file at ``server/tests/radio-address-vectors.json`` and asserts the whole
3125-name space against its published sha256 -- never a copied table.

The relay firmware does NOT implement the mapping. It used to, for a ``!N
<name>`` command that was removed along with it: the board cannot see the
registry, so tuning by name on the board would mistune exactly the robots that
were moved off their default. The robot's own firmware still implements it, to
self-address at boot.

The map, verbatim from the spec::

    positions 0, 2, 4   consonant   z v g p t   = 0 1 2 3 4
    positions 1, 3      vowel       u o i e a   = 0 1 2 3 4

    n       = base5(name)          # name[0] is the MOST significant digit
    channel = 25 + 2 * (n % 25)    # 25, 27, ... 73
    group   = 1 + n // 25          # then skip 10 -> 1..9, 11..126

Every intermediate is 0..3124, so MakeCode int32, C++ ``int`` and Python agree.
Never emitted: channels 3, 4, 7 (the legacy fleet and MakeCode's default) and
groups 0, 10 -- group 10 is the relay's ``!C``/button space, so a hand-dialled
relay can never land on a derived link. A registry override is under no such
constraint: it may use anything ``!CG`` accepts.
"""

from __future__ import annotations

import re

CONSONANTS = "zvgpt"        #: positions 0, 2, 4
VOWELS = "uoiea"            #: positions 1, 3
NAME_LEN = 5
NAME_RE = re.compile(r"^[zvgpt][uoiea][zvgpt][uoiea][zvgpt]$")
SPACE = 5 ** NAME_LEN       #: 3125 names, 3125 distinct pairs

CHANNEL_MIN, CHANNEL_MAX, CHANNEL_STEP, CHANNELS = 25, 73, 2, 25
GROUP_MIN, GROUP_MAX = 1, 126
RESERVED_GROUP = 10         #: the !C / button-A/B group; skipped, never emitted

_ASCII_WS = " \t\r\n\f\v"


def alphabet(position: int) -> str:
    return CONSONANTS if position % 2 == 0 else VOWELS


def normalize(name: str) -> str:
    """Trim ASCII whitespace and map A-Z to a-z -- nothing more, exactly as the
    firmware does. ``VEVOV`` and `` vevov `` are vevov."""
    stripped = name.strip(_ASCII_WS)
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in stripped)


def validate(name: str) -> str:
    """Normalize, then require a well-formed micro:bit name.

    Raises ``ValueError`` for anything the firmware answers with
    ``not a micro:bit name``. Unknown is fine, malformed is not: ``pipip`` is a
    legal address nobody is on, while ``robot1`` has none at all.
    """
    n = normalize(name)
    if not NAME_RE.match(n):
        raise ValueError(f"not a micro:bit name: {name!r}")
    return n


def decode(name: str) -> int:
    """name -> n in 0..3124, ``name[0]`` most significant."""
    n = 0
    for p, ch in enumerate(validate(name)):
        n = n * 5 + alphabet(p).index(ch)
    return n


def encode(n: int) -> str:
    """n in 0..3124 -> name. Emits ``name[4]`` first -- the least significant
    digit -- which is the endianness trap the spec warns about."""
    if not 0 <= n < SPACE:
        raise ValueError(f"n out of range 0..{SPACE - 1}: {n}")
    out = [""] * NAME_LEN
    for p in range(NAME_LEN - 1, -1, -1):
        out[p] = alphabet(p)[n % 5]
        n //= 5
    return "".join(out)


def address(n: int) -> tuple[int, int]:
    """n -> (channel, group)."""
    channel = CHANNEL_MIN + CHANNEL_STEP * (n % CHANNELS)
    group = 1 + n // CHANNELS
    if group >= RESERVED_GROUP:
        group += 1
    return channel, group


def name_to_radio(name: str) -> tuple[int, int]:
    """The ``(channel, group)`` a name derives -- its default, and what the
    registry records for a name it has not seen before."""
    return address(decode(name))


def radio_to_name(channel: int, group: int) -> str:
    """The one name that derives ``(channel, group)``, or ``ValueError`` when
    the pair is outside the derived space (a ``!C``/``!CG`` link, say)."""
    if channel % 2 == 0 or not CHANNEL_MIN <= channel <= CHANNEL_MAX:
        raise ValueError(f"channel {channel} is not a derived address")
    if group == RESERVED_GROUP or not GROUP_MIN <= group <= GROUP_MAX:
        raise ValueError(f"group {group} is not a derived address")
    g = group - 1 if group > RESERVED_GROUP else group
    return encode(CHANNELS * (g - 1) + (channel - CHANNEL_MIN) // CHANNEL_STEP)


def canonical_form(version: int = 2) -> str:
    """The spec's canonical full-space form, one line per name for n = 0..3124
    in order. Its sha256 is the three-repo contract.

    version 2 (the conformance gate, ``$.properties.conformance_sha256``):
    ``<name>,<channel>,<group>,<decode(name)>,<reverse(channel,group)>`` --
    the last two columns are always n, which is the point: every line forces
    the decoder (what a registry lookup actually runs) and the reverse map to
    execute and hashes their output. A little-endian decoder is wrong on 96%
    of names and passes version 1 unchanged; against version 2 it yields the
    spec's published broken-decode digest and fails loudly.

    version 1 (``$.properties.full_space_sha256``): the first three columns
    only. Kept as a bisector -- version 2 failing while 1 passes localises the
    fault to decode/reverse.
    """
    if version not in (1, 2):
        raise ValueError(f"unknown canonical form version {version}")
    lines = []
    for n in range(SPACE):
        name = encode(n)
        channel, group = address(n)
        if version == 1:
            lines.append(f"{name},{channel},{group}\n")
        else:
            back = decode(radio_to_name(channel, group))
            lines.append(f"{name},{channel},{group},{decode(name)},{back}\n")
    return "".join(lines)
