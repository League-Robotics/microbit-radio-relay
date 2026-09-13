"""A micro:bit's name gives its DEFAULT radio address.

The five-letter CODAL friendly name is a base-5 encoding of the chip's
``NRF_FICR->DEVICEID[1] % 3125``, so a board derives its own
``(channel, group)`` at boot and any tool that knows the name derives the same
pair, with no coordination at all.

That is a default, not an address. The 3125 names spread over 73 channels, so
about 43 names share each one; when two robots collide, one has to move, and
its name then no longer says where it is. ``registry.py`` is what records the
exceptions, and this module is what it calls to compute the default in the
first place. Nothing here knows about overrides -- keeping the mapping pure is
what lets its digest stay a cross-repo contract.

Normative spec: ``docs/design/radio-addressing.md`` in radio-robot-lib (its
wiki's "Radio addressing" page). ``server/tests/radio-address-vectors.json``
transcribes its digests and vectors, and the tests assert the whole 3125-name
space against the published sha256 -- never a copied table.

The relay firmware implements the same map in ``source/relay/naming.h`` for
``!N <name>``. The board cannot see the registry, so ``!N`` always tunes to the
name's DEFAULT; `mbrelay connect <robot>` asks the registry and sends ``!CG``.

The map, verbatim from the spec::

    positions 0, 2, 4   consonant   z v g p t   = 0 1 2 3 4
    positions 1, 3      vowel       u o i e a   = 0 1 2 3 4

    n       = base5(name)          # name[0] is the MOST significant digit
    channel = 11 + (n % 73)        # 11 .. 83
    group   = 15 + (n % 241)       # 15 .. 255

73 and 241 are coprime and 3125 < 73 * 241, so every name gets its own pair
(Chinese remainder theorem); a channel is shared, a pair never is. Every
intermediate is at most 100,048, so MakeCode int32, C++ ``int`` and Python
agree. Never emitted: channels 0-10 (the legacy fleet's 3/4/5 and MakeCode's
default 7) and groups 0-14 (MakeCode's 0 and the relay's ``!C``/button group
10), so a hand-dialled relay can never land on a derived link. A registry
override is under no such constraint: it may use anything ``!CG`` accepts.
"""

from __future__ import annotations

import re

CONSONANTS = "zvgpt"        #: positions 0, 2, 4
VOWELS = "uoiea"            #: positions 1, 3
NAME_LEN = 5
NAME_RE = re.compile(r"^[zvgpt][uoiea][zvgpt][uoiea][zvgpt]$")
SPACE = 5 ** NAME_LEN       #: 3125 names, 3125 distinct pairs

CHANNEL_MIN, CHANNEL_MAX, CHANNELS = 11, 83, 73
GROUP_MIN, GROUP_MAX, GROUPS = 15, 255, 241
CHANNELS_INVERSE = 208      #: 73 * 208 = 1 (mod 241), for the reverse map

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
    ``usage !N <name>``. Unknown is fine, malformed is not: ``pipip`` is a
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
    return CHANNEL_MIN + n % CHANNELS, GROUP_MIN + n % GROUPS


def name_to_radio(name: str) -> tuple[int, int]:
    """The ``(channel, group)`` a name derives -- its default, and what the
    registry records for a name it has not seen before."""
    return address(decode(name))


def radio_to_name(channel: int, group: int) -> str:
    """The one name that derives ``(channel, group)``, or ``ValueError`` when
    no name does. Only 3125 of the 17,593 in-range pairs belong to a name, so
    most pairs -- and every ``!C`` link -- have none."""
    if not CHANNEL_MIN <= channel <= CHANNEL_MAX:
        raise ValueError(f"channel {channel} is not a derived address")
    if not GROUP_MIN <= group <= GROUP_MAX:
        raise ValueError(f"group {group} is not a derived address")
    c, g = channel - CHANNEL_MIN, group - GROUP_MIN
    # The n < 73 * 241 with n = c (mod 73) and n = g (mod 241).
    n = c + CHANNELS * (((g - c + GROUPS) * CHANNELS_INVERSE) % GROUPS)
    if n >= SPACE:
        raise ValueError(f"{channel}/{group} belongs to no name")
    return encode(n)


def canonical_form(version: int = 2) -> str:
    """The spec's canonical full-space form, one line per name for n = 0..3124
    in order. Its sha256 is the cross-repo contract.

    version 2 (the conformance gate, D2):
    ``<name>,<channel>,<group>,<decode(name)>,<reverse(channel,group)>`` --
    the last two columns are always n, which is the point: every line forces
    the decoder (what ``!N`` and a registry lookup actually run) and the
    reverse map to execute and hashes their output. A little-endian decoder
    passes version 1 unchanged; against version 2 it yields the spec's
    published broken-decode digest and fails loudly.

    version 1 (D1): the first three columns only. Kept as a bisector --
    version 2 failing while 1 passes localises the fault to decode/reverse.
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
