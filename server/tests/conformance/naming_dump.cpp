// Host build of the relay firmware's name <-> (channel, group) mapping.
//
// Compiles source/relay/naming.h -- the exact code the firmware runs -- and
// prints the spec's canonical form: for n = 0..3124 in order, one line
// "<name>,<channel>,<group>". Along the way every name is round-tripped
// through normalizeName() and radioToName(), so the reverse direction is
// checked too; any disagreement exits non-zero with the offending name.
//
// Built and run by tools/radio-address-dump (implementation "firmware-cpp");
// compared against the other implementations by
// scripts/radio_address_conformance.py.
//
//   c++ -std=c++11 -I source/relay server/tests/conformance/naming_dump.cpp -o naming_dump

#include "naming.h"

#include <cstdio>
#include <cstring>

int main()
{
    char name[naming::kNameLen + 1];
    char canon[naming::kNameLen + 1];
    char back[naming::kNameLen + 1];
    for (int n = 0; n < naming::kSpace; ++n)
    {
        naming::encodeName(n, name);
        if (!naming::normalizeName(name, canon) || std::strcmp(canon, name) != 0)
        {
            std::fprintf(stderr, "n=%d encodes to '%s' which normalizeName rejects\n", n, name);
            return 2;
        }
        if (naming::decodeName(canon) != n)
        {
            std::fprintf(stderr, "'%s' decodes to %d, not %d\n", canon, naming::decodeName(canon), n);
            return 3;
        }
        int channel = 0, group = 0;
        naming::nameToRadio(canon, channel, group);
        if (!naming::radioToName(channel, group, back) || std::strcmp(back, name) != 0)
        {
            std::fprintf(stderr, "'%s' -> (%d, %d) -> '%s': reverse map disagrees\n",
                         name, channel, group, back);
            return 4;
        }
        std::printf("%s,%d,%d\n", name, channel, group);
    }
    return 0;
}
