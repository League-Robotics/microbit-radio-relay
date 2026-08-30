// Radio addressing: a micro:bit's name IS its radio address (protocol §3.7).
//
// Pure functions with no CODAL dependency, so the SAME code runs in the relay
// firmware and in a host build (server/tests/conformance/naming_dump.cpp) that
// dumps the whole name space for comparison with the other implementations --
// mbrelay's naming.py and the robot's MakeCode extension.
//
// Normative spec: docs/radio-addressing.md in pxt-nezha-diffdrive, with the
// machine-readable docs/radio-address-vectors.json (mirrored at
// server/tests/radio-address-vectors.json). If this header and that spec ever
// disagree, the spec wins.
//
//   positions 0,2,4  consonant  z v g p t = 0..4
//   positions 1,3    vowel      u o i e a = 0..4
//   n = base5(name), name[0] most significant              0..3124
//   channel = 25 + 2 * (n % 25)                             25..73, odd
//   group   = 1 + n / 25; if group >= 10: group += 1        1..9, 11..126
//
// The map is a bijection over the 3125 names. Never emitted: channels 3, 4, 7
// and groups 0, 10 -- group 10 is the relay's !C/button space, so a hand-dialled
// relay never lands on a derived link, and `!C` cannot reach a named board at
// all: only `!CG` or `!N` can. Every intermediate is 0..3124, so this is the
// same in MakeCode int32, C++ int and Python: no shifts, no negative modulo.
#pragma once

namespace naming {

constexpr int kNameLen       = 5;
constexpr int kSpace         = 3125;   // 5^5 names
constexpr int kChannelMin    = 25;
constexpr int kChannelMax    = 73;
constexpr int kChannelStep   = 2;
constexpr int kChannels      = 25;
constexpr int kGroupMin      = 1;
constexpr int kGroupMax      = 126;
constexpr int kReservedGroup = 10;

inline const char *alphabet(int position)
{
    return (position % 2 == 0) ? "zvgpt" : "uoiea";
}

// Digit value of `c` at name position `p`, or -1 if `c` is not in that
// position's alphabet.
inline int nameDigit(int p, char c)
{
    const char *a = alphabet(p);
    for (int d = 0; d < 5; ++d)
        if (a[d] == c)
            return d;
    return -1;
}

inline bool isAsciiSpace(char c)
{
    return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' || c == '\v';
}

// Canonical form: trim ASCII whitespace, A-Z -> a-z, then exactly
// [zvgpt][uoiea][zvgpt][uoiea][zvgpt]. `out` must hold kNameLen + 1.
// Returns false (and an empty `out`) for anything else.
inline bool normalizeName(const char *in, char *out)
{
    out[0] = 0;
    while (isAsciiSpace(*in))
        ++in;
    int n = 0;
    for (; *in && !isAsciiSpace(*in); ++in)
    {
        char c = *in;
        if (c >= 'A' && c <= 'Z')
            c = (char)(c + ('a' - 'A'));
        if (n >= kNameLen || nameDigit(n, c) < 0)
        {
            out[0] = 0;                     // too long, or not that position's letter
            return false;
        }
        out[n++] = c;
    }
    for (; *in; ++in)
    {
        if (!isAsciiSpace(*in))
        {
            out[0] = 0;                     // "to vez": a space inside
            return false;
        }
    }
    if (n != kNameLen)
    {
        out[0] = 0;                         // too short
        return false;
    }
    out[n] = 0;
    return true;
}

// Canonical name -> n in 0..kSpace-1. name[0] is the MOST significant digit.
inline int decodeName(const char *name)
{
    int n = 0;
    for (int p = 0; p < kNameLen; ++p)
        n = n * 5 + nameDigit(p, name[p]);
    return n;
}

// n in 0..kSpace-1 -> canonical name. Emits name[4] first -- the LEAST
// significant digit -- which is the endianness trap the spec warns about:
// a reversed encoder still yields 3125 well-formed distinct names.
inline void encodeName(int n, char *out)
{
    for (int p = kNameLen - 1; p >= 0; --p)
    {
        out[p] = alphabet(p)[n % 5];
        n /= 5;
    }
    out[kNameLen] = 0;
}

inline void addressOf(int n, int &channel, int &group)
{
    channel = kChannelMin + kChannelStep * (n % kChannels);
    group   = 1 + n / kChannels;
    if (group >= kReservedGroup)
        group += 1;
}

// Canonical name (normalizeName returned true) -> (channel, group).
inline void nameToRadio(const char *name, int &channel, int &group)
{
    addressOf(decodeName(name), channel, group);
}

// (channel, group) -> the one name that derives it. Returns false when the pair
// is outside the derived space (a !C / !CG link, say); `out` holds kNameLen + 1.
inline bool radioToName(int channel, int group, char *out)
{
    out[0] = 0;
    if (channel % 2 == 0 || channel < kChannelMin || channel > kChannelMax)
        return false;
    if (group == kReservedGroup || group < kGroupMin || group > kGroupMax)
        return false;
    int g = (group > kReservedGroup) ? group - 1 : group;
    encodeName(kChannels * (g - 1) + (channel - kChannelMin) / kChannelStep, out);
    return true;
}

} // namespace naming
