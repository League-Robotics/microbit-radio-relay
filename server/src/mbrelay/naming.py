"""A micro:bit's name IS its radio address.

The five-letter CODAL friendly name is a base-5 encoding of the chip's
``NRF_FICR->DEVICEID[1]``, so a board derives its own ``(channel, group)`` at
boot, and any tool that knows the name derives the same pair -- no registry, no
allocation. ``!N <name>`` retunes the relay to that pair.

Normative spec: ``docs/radio-addressing.md`` in pxt-nezha-diffdrive, with the
machine-readable contract ``docs/radio-address-vectors.json``. This repo mirrors
that file at ``server/tests/radio-address-vectors.json`` and asserts the whole
3125-name space against its published sha256 -- never a copied table. The
firmware (``nameToRadio`` in ``source/relay/RadioRelay.cpp``) carries the same
steps.

The map, verbatim from the spec::

    positions 0, 2, 4   consonant   z v g p t   = 0 1 2 3 4
    positions 1, 3      vowel       u o i e a   = 0 1 2 3 4

    n       = base5(name)          # name[0] is the MOST significant digit
    channel = 25 + 2 * (n % 25)    # 25, 27, ... 73
    group   = 1 + n // 25          # then skip 10 -> 1..9, 11..126

Every intermediate is 0..3124, so MakeCode int32, C++ ``int`` and Python agree.
Never emitted: channels 3, 4, 7 (the legacy fleet and MakeCode's default) and
groups 0, 10 -- group 10 is the relay's ``!C``/button space, so a hand-dialled
relay can never land on a derived link.
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
    ``# error: usage !N <name>``.
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
    """``(channel, group)`` for a name -- what ``!N <name>`` tunes the relay to."""
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


def canonical_form() -> str:
    """The spec's full-space canonical form: for n = 0..3124 in order, one line
    ``<name>,<channel>,<group>\\n``. Its sha256 is the three-repo contract."""
    return "".join(f"{encode(n)},{address(n)[0]},{address(n)[1]}\n" for n in range(SPACE))
