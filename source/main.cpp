#include "MicroBit.h"
#include "relay/RadioRelay.h"

MicroBit uBit;

int main()
{
    uBit.init();

    // Run the radio relay: command plane -> !GO -> transparent data plane.
    // Never returns; the only exit is a device reset. See RadioRelay.cpp and
    // docs/radio-relay-protocol.md.
    radio_relay_main();

    microbit_panic( 999 );
}
