#!/usr/bin/env python3
"""
MAKECODE-mode test against the standalone radio peers.

Topology (only ONE relay board is plugged into this host; the other two
micro:bits are powered but off-host):

  * hello device -- broadcasts MakeCode strings on channel 0, group 10.
  * echo server  -- re-broadcasts whatever it receives, on channel 1, group 10.

This drives the single relay board entirely in the COMMAND plane (the radio is
live there: '>' sends, '<' receives -- no !GO needed) to check three things:

  1. inbound decode : on channel 0 we should see the hello device's strings
                      arrive as '< ...' lines.
  2. outbound encode +
     round-trip      : on channel 1 we send '> <msg>' and the echo server
                      should bounce it back as '< <msg>'.

Usage:
    uv run python3 scripts/makecode_echo_test.py
    uv run python3 scripts/makecode_echo_test.py --listen 4 --pings 5
"""

from __future__ import annotations

import argparse
import sys
import time

from relay_test import BANNER_RE, Relay, discover

HELLO_CH = 0
ECHO_CH = 1
GROUP = 10


def _lines(raw: bytes) -> list[str]:
    return [ln for ln in raw.decode(errors="replace").splitlines() if ln.strip()]


def test_hello_inbound(r: Relay, listen_s: float) -> bool:
    print(f"\n== inbound: listen for hello messages (channel {HELLO_CH}, group {GROUP}) ==")
    r.set_channel(HELLO_CH)             # !C 0 -> channel 0, group 10
    r.ser.reset_input_buffer()
    raw = r.drain(listen_s)
    recv = [ln for ln in _lines(raw) if ln.startswith("<")]
    for ln in _lines(raw):
        print(f"    {ln}")
    ok = len(recv) > 0
    print(f"  -> {len(recv)} hello message(s) received in {listen_s:.0f}s  "
          f"{'OK' if ok else 'FAIL (heard nothing)'}")
    return ok


def test_echo_roundtrip(r: Relay, pings: int) -> bool:
    print(f"\n== round-trip: send + echo (channel {ECHO_CH}, group {GROUP}) ==")
    r.set_channel(ECHO_CH)              # !C 1 -> channel 1, group 10
    hits = 0
    for i in range(pings):
        msg = f"hello-{i}"
        r.ser.reset_input_buffer()
        r.ser.write(f"> {msg}\n".encode())
        r.ser.flush()
        raw = r.drain(0.5)
        got = _lines(raw)
        # echo arrives as a '<'-prefixed line carrying our payload back
        echoed = any(msg in ln and ln.startswith("<") for ln in got)
        hits += echoed
        shown = got if got else ["(nothing)"]
        print(f"  ping {i}: sent {msg!r:11} -> {shown}  {'ECHO OK' if echoed else 'no echo'}")
        time.sleep(0.1)
    ok = hits > 0
    print(f"  -> {hits}/{pings} echoes returned  {'OK' if ok else 'FAIL'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="MAKECODE echo/hello test (single board)")
    ap.add_argument("--listen", type=float, default=4.0,
                    help="seconds to listen for hello broadcasts")
    ap.add_argument("--pings", type=int, default=5,
                    help="number of echo round-trips to attempt")
    args = ap.parse_args()

    boards = discover()
    if not boards:
        print("No relay board found on this host.", file=sys.stderr)
        return 1
    board = boards[0]
    print(f"using board {board.port}  name={board.name} nrf={board.nrf_serial}")

    r = Relay(board)
    results: dict[str, bool] = {}
    try:
        banner = r.reset_to_command()
        print(f"command plane: {banner}")
        # MAKECODE is the boot default, but be explicit so the test is self-contained.
        r.set_mode("MAKECODE")
        print(f"config: {r.query().decode(errors='replace').strip()}")

        results["hello_inbound"] = test_hello_inbound(r, args.listen)
        results["echo_roundtrip"] = test_echo_roundtrip(r, args.pings)
    finally:
        r.close()

    print("\n== summary ==")
    for k, v in results.items():
        print(f"  {k:<16} {'PASS' if v else 'FAIL'}")
    return 0 if all(results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
