// Radio addressing: a micro:bit's name gives its DEFAULT radio address
// (protocol §3.7).
//
// Pure functions with no CODAL dependency, so the SAME code runs in the relay
// firmware (`!N <name>`, `!N?`) and in a host build
// (server/tests/conformance/naming_dump.cpp) that dumps the whole name space
// for comparison with mbrelay's naming.py and every other implementation.
//
// Normative spec: docs/design/radio-addressing.md in radio-robot-lib (its
// wiki's "Radio addressing" page), transcribed into
// server/tests/radio-address-vectors.json. If this header and the spec ever
// disagree, the spec wins.
//
//   positions 0,2,4  consonant  z v g p t = 0..4
//   positions 1,3    vowel      u o i e a = 0..4
//   n = base5(name), name[0] most significant              0..3124
//   channel = 11 + n % 73                                   11..83
//   group   = 15 + n % 241                                  15..255
//
// 73 and 241 are coprime and 3125 < 73 * 241, so the 3125 names map to 3125
// distinct pairs; about 43 names share each channel. Never emitted: channels
// 0-10 and groups 0-14, so a relay dialled with `!C` (group 10) never lands on
// a derived link. The board cannot see mbrelay's registry, so `!N` is always
// the name's default, never where a moved robot really is. Every intermediate
// is at most 481 * 208 = 100,048, so this is the same in MakeCode int32, C++
// int and Python: no unsigned types, no negative modulo.
#pragma once

namespace naming {

constexpr int kNameLen         = 5;
constexpr int kSpace           = 3125;  // 5^5 names
constexpr int kChannelMin      = 11;
constexpr int kChannelMax      = 83;
constexpr int kChannels        = 73;
constexpr int kGroupMin        = 15;
constexpr int kGroupMax        = 255;
constexpr int kGroups          = 241;
constexpr int kChannelsInverse = 208;   // 73 * 208 = 1 (mod 241)

static_assert((kChannels * kChannelsInverse) % kGroups == 1,
              "kChannelsInverse is not the inverse of kChannels mod kGroups");

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
    channel = kChannelMin + n % kChannels;
    group   = kGroupMin + n % kGroups;
}

// Canonical name (normalizeName returned true) -> (channel, group).
inline void nameToRadio(const char *name, int &channel, int &group)
{
    addressOf(decodeName(name), channel, group);
}

// (channel, group) -> the one name that derives it. Returns false when no name
// does: only 3125 of the 17,593 in-range pairs belong to a name, and no !C link
// does. `out` holds kNameLen + 1.
inline bool radioToName(int channel, int group, char *out)
{
    out[0] = 0;
    if (channel < kChannelMin || channel > kChannelMax)
        return false;
    if (group < kGroupMin || group > kGroupMax)
        return false;
    int c = channel - kChannelMin;          // 0..72
    int g = group - kGroupMin;              // 0..240
    // The n < 73 * 241 with n = c (mod 73) and n = g (mod 241).
    int n = c + kChannels * (((g - c + kGroups) * kChannelsInverse) % kGroups);
    if (n >= kSpace)
        return false;
    encodeName(n, out);
    return true;
}

} // namespace naming
