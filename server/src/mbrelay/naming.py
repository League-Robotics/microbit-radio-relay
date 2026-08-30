"""Name -> (channel, group): the radio link a micro:bit named ``<name>`` uses.

A robot derives its link from its own name; a host that wants to talk to it
says ``!N <name>`` to the relay, and both ends land on the same channel and
group without anyone looking numbers up. The mapping is therefore a wire-format
contract shared by three codebases -- this module, the relay firmware
(``nameToRadio`` in ``source/relay/RadioRelay.cpp``) and the robot's MakeCode
extension (pxt-nezha-diffdrive) -- and :data:`VECTORS` is what all three
assert against. Change one, change all.

Why this particular hash: every intermediate value stays below 2**21, so
MakeCode's static TypeScript (IEEE doubles, no ``Math.imul``) computes it
exactly; the firmware uses ``uint32_t``; here it is plain ints. Group 10 is
never produced: that is the relay's ``!C``/button space, so a hand-dialled
relay can never silently share a named robot's link.
"""

from __future__ import annotations

HASH_MOD = 65521        #: largest prime below 2**16; keeps h*31 < 2**21
CHANNELS = 84           #: CODAL setFrequencyBand() accepts 0..83
GROUPS = 254            #: 0..253 before the reserved-group skip below
RESERVED_GROUP = 10     #: the !C / button-A/B group; never produced
MAX_NAME_LEN = 15       #: what the firmware persists; CODAL names are 5


def normalize(name: str) -> str:
    """The canonical form of a name: trimmed and lower-cased.

    ``TOVEZ``, `` tovez `` and ``tovez`` are one robot.
    """
    return name.strip().lower()


def validate(name: str) -> str:
    """Normalize, then reject anything the firmware would reject.

    Raises ``ValueError`` for an empty name, one longer than
    :data:`MAX_NAME_LEN`, or one containing whitespace or non-printable /
    non-ASCII characters (the command line is ``!N <name>``, so a space would
    end the name early and a stray control byte would corrupt the line).
    """
    n = normalize(name)
    if not n:
        raise ValueError("name is empty")
    if len(n) > MAX_NAME_LEN:
        raise ValueError(f"name longer than {MAX_NAME_LEN} characters: {n!r}")
    if any(not (0x21 <= ord(c) <= 0x7E) for c in n):
        raise ValueError(f"name must be printable ASCII with no spaces: {n!r}")
    return n


def name_hash(name: str) -> int:
    """The 16-bit-ish hash the mapping is built on (exposed for the vectors)."""
    h = 0
    for b in validate(name).encode("ascii"):
        h = (h * 31 + b) % HASH_MOD
    return h


def name_to_radio(name: str) -> tuple[int, int]:
    """``(channel, group)`` for a name. Same result in all three codebases."""
    h = name_hash(name)
    channel = h % CHANNELS
    group = (h // CHANNELS) % GROUPS
    if group >= RESERVED_GROUP:
        group += 1
    return channel, group


#: (name, hash, channel, group). The firmware and the MakeCode extension carry
#: the same table; the bench boards are in it so a hardware test can check a
#: real relay against a real robot by name.
VECTORS: tuple[tuple[str, int, int, int], ...] = (
    ("getez",    30304, 64, 107),   # relay bench board
    ("zavaz",    31251,  3, 119),   # relay bench board
    ("tovez",    17961, 69, 214),   # robot
    ("vevov",    60416, 20, 212),   # robot
    ("gopiv",    62406, 78, 235),   # robot
    ("zeguz",     5578, 34,  67),
    ("zetuv",    18067,  7, 216),
    ("togov",     3852, 72,  46),
    ("aaaaa",    51933, 21, 111),
    ("zzzzz",    59914, 22, 206),
    ("a",           97, 13,   1),
    ("microbit", 16405, 25, 196),
)
