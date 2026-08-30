// Host build of the relay firmware's name <-> (channel, group) mapping.
//
// Compiles source/relay/naming.h -- the exact code the firmware runs -- and
// prints the spec's canonical form, dump protocol v2: for n = 0..3124 in
// order, one line "<name>,<channel>,<group>,<decode(name)>,<reverse(channel,group)>".
// The last two columns are always n, and that is the point: every line makes
// decodeName() -- what `!N <name>` actually executes -- and radioToName() run
// and puts their output INTO the hashed artifact, so a checker verifies the
// inverse rather than trusting this program's own opinion of it. Its sha256
// is the spec's conformance_sha256 (D2). Pass "1" for the three-column v1 form
// (full_space_sha256, D1), kept as a bisector.
//
// Built and run by tools/radio-address-dump (implementation "firmware-cpp");
// compared against the other implementations by
// scripts/radio_address_conformance.py.
//
//   c++ -std=c++11 -I source/relay server/tests/conformance/naming_dump.cpp -o naming_dump

#include "naming.h"

#include <cstdio>
#include <cstring>

int main(int argc, char **argv)
{
    const int version = (argc > 1 && std::strcmp(argv[1], "1") == 0) ? 1 : 2;
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
        int channel = 0, group = 0;
        naming::nameToRadio(canon, channel, group);
        if (!naming::radioToName(channel, group, back))
        {
            std::fprintf(stderr, "'%s' -> (%d, %d): radioToName says not a derived address\n",
                         name, channel, group);
            return 4;
        }
        if (version == 1)
            std::printf("%s,%d,%d\n", name, channel, group);
        else
            std::printf("%s,%d,%d,%d,%d\n", name, channel, group,
                        naming::decodeName(canon), naming::decodeName(back));
    }
    return 0;
}
