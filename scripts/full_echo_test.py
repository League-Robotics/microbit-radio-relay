#!/usr/bin/env python3
"""
End-to-end MAKECODE round-trip test against echo peers, all boards on USB.

Unlike a standalone setup, every board is attached to this host, so we put the
echo peers into echo mode over serial and HOLD THEIR PORTS OPEN -- closing a
port toggles DTR and resets the board (clearing echo mode), so the ports must
stay open for the duration of the test.

Boards are auto-classified by banner:
  * OLD  -- "DEVICE:RADIORELAY:relay:..."  (the TS MakeCode-only relay; verified
            to interoperate with a stock micro:bit). Boots on channel 0.
            Echo via the bare "!ECHO" command.
  * NEW  -- "DEVICE:RADIOBRIDGE:relay:..." (this C++ firmware). Boots channel 10.
            Echo via "!ECHO ON".

Roles: OLD -> channel-0 echo peer (regular-micro:bit compatibility check).
       one NEW -> channel-1 echo peer.   the other NEW -> host driver.

For each channel the host switches to MAKECODE, tunes there (group 10), and
fires uniquely-tagged messages with '>'; each must bounce back as a '<' line,
proving host TX -> peer RX -> peer echo TX -> host RX.

Usage:
    uv run python3 scripts/full_echo_test.py
    uv run python3 scripts/full_echo_test.py --pings 8
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
import time

import serial

from relay_test import Board, Relay

BAUD = 115200
NEW_RE = re.compile(rb"DEVICE:RADIOBRIDGE:relay:([^:]+):(\w+)")
OLD_RE = re.compile(rb"DEVICE:RADIORELAY:relay:([^:]+):(\w+)")
GROUP = 10


def classify_ports() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (old_boards, new_boards) as (port, name) lists, by banner."""
    olds: list[tuple[str, str]] = []
    news: list[tuple[str, str]] = []
    for port in sorted(glob.glob("/dev/cu.usbmodem*")):
        try:
            s = serial.Serial(port, BAUD, timeout=0.3)
        except OSError:
            continue
        try:
            time.sleep(0.3)
            s.reset_input_buffer()
            s.write(b"HELLO\n")
            s.flush()
            time.sleep(0.4)
            data = s.read(400)
        finally:
            s.close()
        m = NEW_RE.search(data)
        if m:
            news.append((port, m.group(1).decode(errors="replace")))
            continue
        m = OLD_RE.search(data)
        if m:
            olds.append((port, m.group(1).decode(errors="replace")))
    return olds, news


def open_fresh(port: str) -> Relay:
    """Open a port (which resets the board) and drain the boot banner."""
    r = Relay(Board(port=port))
    time.sleep(1.0)
    r.drain(0.3)
    return r


def roundtrip(host: Relay, ch: int, label: str, pings: int, mode: str = "MAKECODE") -> bool:
    print(f"\n== channel {ch}, group {GROUP}, mode {mode}: {label} ==")
    host.set_mode(mode)
    host.set_channel(ch)
    time.sleep(0.2)
    hits = 0
    for i in range(pings):
        msg = f"rt{ch}-{i}"
        host.ser.reset_input_buffer()
        host.ser.write(f"> {msg}\n".encode())
        host.ser.flush()
        got = [ln for ln in host.drain(0.5).decode(errors="replace").splitlines() if ln.strip()]
        echoed = any(msg in ln and ln.startswith("<") for ln in got)
        hits += echoed
        print(f"  ping {i}: sent {msg!r:9} -> {got if got else ['(nothing)']}"
              f"  {'ECHO OK' if echoed else 'NO ECHO'}")
        time.sleep(0.1)
    ok = hits == pings
    print(f"  -> {hits}/{pings}  {'PASS' if ok else 'FAIL' if hits == 0 else 'PARTIAL'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="MAKECODE echo round-trip across all boards")
    ap.add_argument("--pings", type=int, default=5)
    args = ap.parse_args()

    olds, news = classify_ports()
    print(f"OLD (RADIORELAY): {[n for _, n in olds] or '-'}")
    print(f"NEW (RADIOBRIDGE): {[n for _, n in news] or '-'}")
    if not news:
        print("Need at least one NEW board to drive the test.", file=sys.stderr)
        return 1

    # Roles.
    host_port, host_name = news[0]
    ch1_peer = news[1] if len(news) > 1 else None
    ch0_peer = olds[0] if olds else None

    held: list[Relay] = []   # echo peers whose ports we keep open
    peer_ch1: Relay | None = None
    host: Relay | None = None
    results: dict[str, bool] = {}
    try:
        # --- set up echo peers and HOLD their ports open ---
        if ch0_peer:
            port, name = ch0_peer
            p = open_fresh(port)            # OLD relay boots on channel 0
            print(f"\nch0 echo peer = OLD {name}: {p.cmd('!ECHO', 0.4).decode(errors='replace').strip()}")
            held.append(p)
        if ch1_peer:
            port, name = ch1_peer
            peer_ch1 = open_fresh(port)
            peer_ch1.cmd("!C 1", 0.4)       # tune NEW peer to channel 1
            print(f"ch1 echo peer = NEW {name}: {peer_ch1.cmd('!ECHO ON', 0.4).decode(errors='replace').strip()}")
            held.append(peer_ch1)

        # --- host driver ---
        host = open_fresh(host_port)
        print(f"host driver  = NEW {host_name}: {host.reset_to_command()}")

        # MAKECODE round-trips against both peers.
        if ch0_peer:
            results["ch0 MAKECODE (OLD relay / regular micro:bit compat)"] = \
                roundtrip(host, 0, f"OLD {ch0_peer[1]} echo", args.pings)
        if ch1_peer:
            results["ch1 MAKECODE (NEW firmware echo)"] = \
                roundtrip(host, 1, f"NEW {ch1_peer[1]} echo", args.pings)

        # RAW250 round-trip: both NEW boards switch to RAW250 (on-air MAXLEN 250),
        # channel 1. The OLD relay can't do RAW250, so this is NEW <-> NEW only.
        if ch1_peer and peer_ch1:
            print(f"\nswitching ch1 peer NEW {ch1_peer[1]} to RAW250: "
                  f"{peer_ch1.cmd('!MODE RAW250', 0.4).decode(errors='replace').strip()}")
            results["ch1 RAW250 (NEW firmware echo)"] = \
                roundtrip(host, 1, f"NEW {ch1_peer[1]} echo", args.pings, mode="RAW250")
    finally:
        if host:
            host.close()
        for p in held:
            p.close()

    print("\n== summary ==")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return 0 if results and all(results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
