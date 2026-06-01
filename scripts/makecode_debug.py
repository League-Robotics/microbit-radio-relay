#!/usr/bin/env python3
"""
MAKECODE-mode diagnostic with the firmware's '# DBG' logging turned on.

Drives the single relay board in the command plane, enables radio TX/RX debug
logging (!DEBUG ON), then:
  1. listens on channel 0 / group 10 for the CHATTER peer's "hello" broadcasts,
  2. sends a few messages on channel 1 / group 10 and waits for the ECHO peer
     to bounce them back.

Every serial line is printed verbatim, so the '# DBG onRadioFrame ...' lines
reveal whether datagram events fire at all (receive side) and the
'# DBG TX makecode ...' lines confirm we actually hand bytes to the radio.

Usage:
    uv run python3 scripts/makecode_debug.py
"""

from __future__ import annotations

import sys
import time

from relay_test import Relay, discover


def show(raw: bytes, indent: str = "    ") -> list[str]:
    lines = [ln for ln in raw.decode(errors="replace").splitlines() if ln.strip()]
    for ln in lines:
        print(f"{indent}{ln}")
    return lines


def main() -> int:
    boards = discover()
    if not boards:
        print("No relay board found.", file=sys.stderr)
        return 1
    r = Relay(boards[0])
    try:
        print("banner:", r.reset_to_command())
        print("mode  :", r.set_mode("MAKECODE").decode(errors="replace").strip())
        # Verify the new firmware took: !DEBUG must be recognised (old fw replies
        # "unknown command").
        print("debug :", r.cmd("!DEBUG ON").decode(errors="replace").strip())
        print("query :", r.query().decode(errors="replace").strip())

        print(f"\n== listen for CHATTER 'hello' on channel 0, group 10 (6s) ==")
        r.set_channel(0)
        r.ser.reset_input_buffer()
        show(r.drain(6.0))

        print(f"\n== echo round-trip on channel 1, group 10 ==")
        r.set_channel(1)
        for i in range(5):
            msg = f"ping{i}"
            r.ser.reset_input_buffer()
            r.ser.write(f"> {msg}\n".encode())
            r.ser.flush()
            print(f"  --- sent '> {msg}' ---")
            show(r.drain(0.6))
            time.sleep(0.1)
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
