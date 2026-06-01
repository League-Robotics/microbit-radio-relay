#!/usr/bin/env python3
"""
MAKECODE round-trip test against the standalone echo peers.

Setup (all three boards powered independently; only the host relay is on USB):

  * channel 0 -- OLD TS relay, MakeCode-only firmware, in echo mode. This is the
                 reference peer: the old code is verified to interoperate with a
                 stock micro:bit, so a clean round-trip here proves the new
                 firmware talks to a "regular micro:bit".
  * channel 1 -- NEW C++ firmware, in echo mode. Proves the new firmware's own
                 MAKECODE TX/RX + echo path round-trips peer-to-peer.

The host relay (this board) drives both: for each channel it switches to
MAKECODE, tunes to the channel (group 10), fires N uniquely-tagged messages with
'>' and checks each one bounces back as a '<' line. Each echo proves the full
loop: host TX -> peer RX -> peer echo TX -> host RX.

Usage:
    uv run python3 scripts/echo_roundtrip_test.py
    uv run python3 scripts/echo_roundtrip_test.py --pings 8
"""

from __future__ import annotations

import argparse
import sys
import time

from relay_test import Relay, discover

GROUP = 10
PEERS = [
    (0, "old TS relay (MakeCode-verified -> regular micro:bit compat)"),
    (1, "new C++ firmware (echo mode)"),
]


def roundtrip_channel(r: Relay, ch: int, label: str, pings: int) -> bool:
    print(f"\n== channel {ch}, group {GROUP}: {label} ==")
    r.set_mode("MAKECODE")
    r.set_channel(ch)                       # !C <ch> -> channel ch, group 10
    time.sleep(0.2)
    hits = 0
    for i in range(pings):
        msg = f"rt{ch}-{i}"
        r.ser.reset_input_buffer()
        r.ser.write(f"> {msg}\n".encode())
        r.ser.flush()
        raw = r.drain(0.5)
        got = [ln for ln in raw.decode(errors="replace").splitlines() if ln.strip()]
        echoed = any(msg in ln and ln.startswith("<") for ln in got)
        hits += echoed
        print(f"  ping {i}: sent {msg!r:9} -> {got if got else ['(nothing)']}"
              f"  {'ECHO OK' if echoed else 'NO ECHO'}")
        time.sleep(0.1)
    ok = hits == pings
    print(f"  -> {hits}/{pings} echoes  {'PASS' if ok else 'FAIL' if hits == 0 else 'PARTIAL'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="MAKECODE echo round-trip test")
    ap.add_argument("--pings", type=int, default=5)
    args = ap.parse_args()

    boards = discover()
    if not boards:
        print("No host relay board found on USB.", file=sys.stderr)
        return 1
    r = Relay(boards[0])
    results: dict[str, bool] = {}
    try:
        print(f"host relay: {r.reset_to_command()}")
        for ch, label in PEERS:
            results[f"ch{ch}"] = roundtrip_channel(r, ch, label, args.pings)
    finally:
        r.close()

    print("\n== summary ==")
    for k, v in results.items():
        print(f"  {k:<6} {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
