#!/usr/bin/env python3
"""
End-to-end test harness for the micro:bit radio relay.

Exercises two relay boards plugged into this host:

  * discovery   -- find both relay serial ports (and join them to the MSD
                   volume / USB serial via ioreg, macOS).
  * reset       -- close+reopen a port returns the board to the COMMAND plane
                   (verified empirically; see docs/radio-relay-protocol.md §2).
  * messaging   -- send small messages one way and round-trip, in both the
                   command plane ('>' / '<') and the transparent data plane.
  * channels    -- confirm same-channel delivery and cross-channel isolation.
  * throughput  -- both boards in the data plane, one host thread echoes, the
                   other blasts data and measures effective round-trip goodput.

Usage:
    uv run python3 scripts/relay_test.py            # run all phases
    uv run python3 scripts/relay_test.py --phase throughput
    uv run python3 scripts/relay_test.py --seconds 5 --mode RAW250
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import serial

BAUD = 115200
BANNER_RE = re.compile(rb"DEVICE:RADIOBRIDGE:relay:([^:]+):(\d+)")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@dataclass
class Board:
    port: str
    usb_serial: str | None = None
    volume: str | None = None
    name: str | None = None      # nRF friendly name from the banner
    nrf_serial: str | None = None


def ioreg_port_serial() -> dict[str, str]:
    """macOS: map each /dev/cu.* serial port to its USB serial number."""
    try:
        out = subprocess.check_output(
            ["ioreg", "-r", "-c", "IOUSBHostDevice", "-l"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    last_serial: str | None = None
    for line in out.splitlines():
        m = re.search(r'"USB Serial Number" = "([^"]+)"', line)
        if m:
            last_serial = m.group(1)
            continue
        m = re.search(r'"IOCalloutDevice" = "(/dev/cu\.[^"]+)"', line)
        if m and last_serial:
            mapping[m.group(1)] = last_serial
    return mapping


def volumes_by_serial() -> dict[str, str]:
    """Map USB serial (DETAILS.TXT Unique ID) -> mounted MICROBIT volume."""
    import glob
    out: dict[str, str] = {}
    for vol in glob.glob("/Volumes/MICROBIT*"):
        try:
            with open(f"{vol}/DETAILS.TXT", "r", errors="ignore") as fh:
                for line in fh:
                    m = re.search(r"Unique ID:\s*([0-9a-fA-F]+)", line)
                    if m:
                        out[m.group(1)] = vol
                        break
        except OSError:
            pass
    return out


def discover() -> list[Board]:
    """Find relay boards: probe every micro:bit-looking port for the banner."""
    import glob
    port_serial = ioreg_port_serial()
    vol_serial = volumes_by_serial()
    boards: list[Board] = []
    for port in sorted(glob.glob("/dev/cu.usbmodem*")):
        try:
            s = serial.Serial(port, BAUD, timeout=0.3)
        except OSError:
            continue
        try:
            time.sleep(0.2)
            s.reset_input_buffer()
            s.write(b"HELLO\n")
            s.flush()
            time.sleep(0.4)
            data = s.read(400)
        finally:
            s.close()
        m = BANNER_RE.search(data)
        if not m:
            continue
        usb = port_serial.get(port)
        boards.append(Board(
            port=port,
            usb_serial=usb,
            volume=vol_serial.get(usb) if usb else None,
            name=m.group(1).decode(errors="replace"),
            nrf_serial=m.group(2).decode(),
        ))
    return boards


# ---------------------------------------------------------------------------
# Relay wrapper
# ---------------------------------------------------------------------------
class Relay:
    def __init__(self, board: Board):
        self.board = board
        self.port = board.port
        self.ser = serial.Serial(self.port, BAUD, timeout=0.2)

    # -- low level -------------------------------------------------------
    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def drain(self, wait: float = 0.3) -> bytes:
        time.sleep(wait)
        return self.ser.read(8192)

    def cmd(self, text: str, wait: float = 0.3) -> bytes:
        self.ser.reset_input_buffer()
        self.ser.write((text + "\n").encode())
        self.ser.flush()
        return self.drain(wait)

    # -- command plane ---------------------------------------------------
    def reset_to_command(self, timeout: float = 6.0) -> str:
        """Close+reopen (resets the board) then poll HELLO until the banner
        comes back, confirming the COMMAND plane."""
        self.close()
        time.sleep(0.8)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.ser = serial.Serial(self.port, BAUD, timeout=0.2)
            except OSError:
                time.sleep(0.3)
                continue
            data = self.cmd("HELLO", 0.4)
            m = BANNER_RE.search(data)
            if m:
                return m.group(0).decode(errors="replace")
            time.sleep(0.3)
        raise RuntimeError(f"{self.port}: no banner after reset")

    def set_channel(self, ch: int) -> bytes:
        return self.cmd(f"!C {ch}")

    def set_channel_group(self, ch: int, grp: int) -> bytes:
        return self.cmd(f"!CG {ch} {grp}")

    def set_mode(self, mode: str) -> bytes:
        return self.cmd(f"!MODE {mode}")

    def query(self) -> bytes:
        return self.cmd("?")

    def send_line(self, msg: str) -> bytes:
        """Command-plane single send ('>')."""
        return self.cmd("> " + msg)

    def go(self) -> bytes:
        return self.cmd("!GO")


# ---------------------------------------------------------------------------
# Test phases
# ---------------------------------------------------------------------------
def phase_discovery(boards: list[Board]) -> None:
    print("\n== discovery ==")
    for b in boards:
        print(f"  {b.port}  name={b.name:<6} nrf={b.nrf_serial:<10} "
              f"vol={b.volume or '-':<16} usb={(b.usb_serial or '-')[-12:]}")


def phase_messaging(a: Relay, b: Relay) -> bool:
    print("\n== messaging (command plane, channel 10) ==")
    a.reset_to_command()
    b.reset_to_command()
    a.set_channel(10)
    b.set_channel(10)
    ok = True
    for direction, src, dst in [("A->B", a, b), ("B->A", b, a)]:
        dst.ser.reset_input_buffer()
        msg = f"ping-{direction}"
        src.send_line(msg)
        got = dst.drain(0.4)
        hit = f"<{msg}".encode() in got or f"< {msg}".encode() in got
        print(f"  {direction}: sent {msg!r:18} -> recv {got!r}  {'OK' if hit else 'FAIL'}")
        ok = ok and hit
    return ok


def phase_channels(a: Relay, b: Relay) -> bool:
    print("\n== channel isolation ==")
    a.reset_to_command()
    b.reset_to_command()

    # Same channel: should deliver.
    a.set_channel(12)
    b.set_channel(12)
    b.ser.reset_input_buffer()
    a.send_line("same-chan")
    same = b.drain(0.4)
    same_ok = b"same-chan" in same
    print(f"  same channel (C):   B recv {same!r}  {'OK (delivered)' if same_ok else 'FAIL'}")

    # Different channels: should NOT deliver.
    a.set_channel(12)
    b.set_channel(20)
    b.ser.reset_input_buffer()
    a.send_line("cross-chan")
    cross = b.drain(0.4)
    cross_ok = b"cross-chan" not in cross
    print(f"  diff channel (C/K): B recv {cross!r}  {'OK (isolated)' if cross_ok else 'FAIL (leaked)'}")
    return same_ok and cross_ok


def phase_dataplane_roundtrip(a: Relay, b: Relay, mode: str) -> bool:
    print(f"\n== data-plane round-trip (mode {mode}) ==")
    a.reset_to_command()
    b.reset_to_command()
    for r in (a, b):
        r.set_channel(10)
        r.set_mode(mode)
        r.go()
    time.sleep(0.3)
    a.ser.reset_input_buffer()
    b.ser.reset_input_buffer()

    # B echoes one message back; A should see it return.
    msg = b"hello-rt"
    a.ser.write(msg + b"\n")
    a.ser.flush()
    time.sleep(0.4)
    at_b = b.ser.read(4096)
    # echo from B's host side
    if at_b:
        b.ser.write(at_b if mode == "RAW250" else at_b.strip() + b"\n")
        b.ser.flush()
    time.sleep(0.4)
    back = a.ser.read(4096)
    ok = b"hello-rt" in at_b and b"hello-rt" in back
    print(f"  A->B saw {at_b!r} ; echoed; A got back {back!r}  {'OK' if ok else 'FAIL'}")
    return ok


def _enter_data_plane(a: Relay, b: Relay, mode: str) -> None:
    a.reset_to_command()
    b.reset_to_command()
    for r in (a, b):
        r.set_channel(10)
        r.set_mode(mode)
        r.go()
    time.sleep(0.3)
    a.ser.reset_input_buffer()
    b.ser.reset_input_buffer()


# Largest payload that fits a single radio frame (no fragmentation), per mode.
# RAW250: 250-byte radio packet - 3-byte frame header = 247.  MAKECODE: <=19-byte
# MakeCode string. One frame per message keeps fire-and-forget reliable -- a
# multi-fragment message fails if any single fragment is lost.
def _frame_payload(mode: str) -> int:
    # MAKECODE: <=19-byte MakeCode string. RAW: radio packet (250) minus the
    # 3-byte frame header = 247.
    return 18 if mode == "MAKECODE" else 247


def _throughput_oneway(a: Relay, b: Relay, mode: str, seconds: float, gap: float) -> None:
    """A sends single-frame messages back-to-back (paced by `gap`); B counts what
    arrives. Measures sustained one-way A->B delivery without overrunning the
    relay's serial buffer."""
    print(f"  -- one-way A->B, single-frame, {gap*1000:.0f}ms gap ({seconds:.0f}s) --")
    _enter_data_plane(a, b, mode)

    stop = threading.Event()
    recv = 0

    def counter():
        nonlocal recv
        while not stop.is_set():
            nb = b.ser.in_waiting
            if nb:
                recv += len(b.ser.read(nb))
            else:
                time.sleep(0.0005)
    ct = threading.Thread(target=counter, daemon=True)

    n = _frame_payload(mode)
    chunk = bytes((48 + (j % 10)) for j in range(n)) + (b"\n" if mode == "MAKECODE" else b"")
    sent = msgs = 0
    ct.start()
    t0 = time.time()
    while time.time() - t0 < seconds:
        a.ser.write(chunk)
        a.ser.flush()
        sent += n
        msgs += 1
        if gap:
            time.sleep(gap)
    elapsed = time.time() - t0
    time.sleep(0.5)                  # let the last frames drain
    stop.set()
    ct.join(timeout=1)
    print(f"     sent     : {msgs} msgs / {sent} B payload ({sent/elapsed:6.0f} B/s offered)")
    print(f"     arrived  : {recv} B at B  (loss {100*(1-min(recv,sent)/max(sent,1)):.0f}%)")
    print(f"     A->B rate: {recv/elapsed:6.0f} B/s ({recv/elapsed*8/1000:.1f} kbit/s)")


def _throughput_roundtrip(a: Relay, b: Relay, mode: str, seconds: float) -> None:
    """Host-paced ping-pong: A sends one single-frame message, B's host echoes
    it, A waits for it to return before sending the next. This is the honest
    'as fast as the half-duplex link allows' throughput + latency."""
    print(f"  -- paced round-trip A->B->A ({seconds:.0f}s, B echoes) --")
    _enter_data_plane(a, b, mode)

    stop = threading.Event()

    def echo_b():
        while not stop.is_set():
            nb = b.ser.in_waiting
            if nb:
                b.ser.write(b.ser.read(nb))
                b.ser.flush()
            else:
                time.sleep(0.0005)
    et = threading.Thread(target=echo_b, daemon=True)
    et.start()

    n = _frame_payload(mode)
    payload = bytes((65 + (j % 26)) for j in range(n))
    tx = payload + (b"\n" if mode == "MAKECODE" else b"")
    trips = lost = 0
    rtts: list[float] = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        a.ser.reset_input_buffer()
        ts = time.time()
        a.ser.write(tx)
        a.ser.flush()
        got = b""
        deadline = time.time() + 0.3
        while time.time() < deadline and len(got) < n:
            if a.ser.in_waiting:
                got += a.ser.read(a.ser.in_waiting)
            else:
                time.sleep(0.0005)
        if len(got) >= n:
            trips += 1
            rtts.append((time.time() - ts) * 1000)
        else:
            lost += 1
    elapsed = time.time() - t0
    stop.set()
    et.join(timeout=1)

    if trips:
        avg = sum(rtts) / len(rtts)
        rt_payload = trips * n
        print(f"     round-trips : {trips} ok, {lost} lost "
              f"({100*trips/max(trips+lost,1):.0f}% success)")
        print(f"     RTT         : avg {avg:.1f} ms (min {min(rtts):.0f}, max {max(rtts):.0f})")
        print(f"     goodput     : {rt_payload/elapsed:6.0f} B/s payload each way "
              f"({rt_payload/elapsed*8/1000:.1f} kbit/s), {2*rt_payload/elapsed:.0f} B/s on the wire")
    else:
        print(f"     no round-trips completed ({lost} attempts lost)")


def phase_throughput(a: Relay, b: Relay, mode: str, seconds: float) -> None:
    print(f"\n== throughput (mode {mode}) ==")
    _throughput_oneway(a, b, mode, seconds, gap=0.01)
    _throughput_roundtrip(a, b, mode, seconds)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="micro:bit radio relay test harness")
    ap.add_argument("--phase", default="all",
                    choices=["all", "discovery", "messaging", "channels",
                             "roundtrip", "throughput"])
    ap.add_argument("--mode", default="RAW250", choices=["MAKECODE", "RAW250"])
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()

    boards = discover()
    if len(boards) < 2:
        print(f"Need 2 relay boards, found {len(boards)}: "
              f"{[b.port for b in boards]}", file=sys.stderr)
        return 1
    boards = boards[:2]
    phase_discovery(boards)

    a = Relay(boards[0])
    b = Relay(boards[1])
    results: dict[str, bool] = {}
    try:
        if args.phase in ("all", "messaging"):
            results["messaging"] = phase_messaging(a, b)
        if args.phase in ("all", "channels"):
            results["channels"] = phase_channels(a, b)
        if args.phase in ("all", "roundtrip"):
            results["roundtrip"] = phase_dataplane_roundtrip(a, b, args.mode)
        if args.phase in ("all", "throughput"):
            phase_throughput(a, b, args.mode, args.seconds)
    finally:
        a.close()
        b.close()

    if results:
        print("\n== summary ==")
        for k, v in results.items():
            print(f"  {k:<12} {'PASS' if v else 'FAIL'}")
        return 0 if all(results.values()) else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
