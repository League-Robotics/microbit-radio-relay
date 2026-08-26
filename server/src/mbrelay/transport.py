"""The serial byte channel, and the seam that lets tests replace it.

Everything above this module talks to ``ByteChannel``. Production supplies
``SerialChannel`` over a real tty; tests supply an in-process fake. That split is
not decoration -- it is the escape hatch if ``loop.add_reader`` on a tty ever
misbehaves on a platform, because then *only this file* changes.

Why we drive the serial fd with ``add_reader`` rather than pyserial-asyncio or a
reader thread:

* CDC-ACM ttys are selectable on both epoll (Linux) and kqueue (macOS), so the
  wake latency is the same as a blocking read in a thread.
* Killing a thread parked in ``serial.read()`` on a *vanished* tty means closing
  its fd from another thread. ``loop.remove_reader()`` plus an explicit close is
  deterministic; the threaded version hangs on macOS.
* pyserial-asyncio is an extra dependency that is not in Ubuntu apt, and its
  transport hides the device-yanked-mid-read path, which is exactly the one that
  matters here.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import socket
from dataclasses import dataclass
from typing import Callable, Protocol

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

# BBC micro:bit DAPLink interface chip. Both V1 and V2 present this.
MICROBIT_VID = 0x0D28
MICROBIT_PID = 0x0204

OnData = Callable[[bytes], None]
OnError = Callable[[BaseException | None], None]


@dataclass(frozen=True)
class PortInfo:
    """One micro:bit as the OS currently sees it."""
    uid: str            # DAPLink USB serial number, 48 hex chars. The stable key.
    device: str         # /dev/ttyACM0, /dev/cu.usbmodem..., /dev/microbit/<uid>
    description: str = ""


class ByteChannel(Protocol):
    """An async duplex byte channel to one relay board."""

    async def open(self) -> None:
        """Open the port. THIS RESETS THE BOARD (the open toggles DTR)."""

    async def close(self) -> None: ...

    def start_reading(self, on_data: OnData, on_error: OnError) -> None: ...

    def stop_reading(self) -> None: ...

    def write_nowait(self, data: bytes) -> None:
        """Queue bytes. Never blocks; buffers if the tty is not ready."""

    async def drain(self) -> None: ...

    @property
    def pending_bytes(self) -> int: ...

    def set_watermarks(self, on_high: Callable[[], None],
                       on_low: Callable[[], None]) -> None: ...


class ChannelFactory(Protocol):
    async def open(self, port: str) -> ByteChannel: ...


class PortScanner(Protocol):
    def scan(self) -> dict[str, PortInfo]: ...


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------
def _prefer(existing: str | None, candidate: str) -> str:
    """Pick between two device paths for the same board.

    Some pyserial builds surface both /dev/cu.* and /dev/tty.* for one macOS
    device. Always take cu.: opening tty.* blocks waiting for carrier detect.
    """
    if existing is None:
        return candidate
    for path in (existing, candidate):
        if "/cu." in path:
            return path
    return existing


def scan_ports() -> dict[str, PortInfo]:
    """Every attached micro:bit, keyed by DAPLink UID.

    Works identically on Linux and macOS -- pyserial reports the same UID that
    pyOCD and mbdeploy use, so this is the join key across all our tooling.
    """
    found: dict[str, PortInfo] = {}
    for p in list_ports.comports():
        if (p.vid, p.pid) != (MICROBIT_VID, MICROBIT_PID) or not p.serial_number:
            continue
        uid = p.serial_number
        # The udev rule ships /dev/microbit/<uid>, which never renumbers. Prefer
        # it when present; macOS has no equivalent, which is why the inventory is
        # keyed on the uid rather than on the path.
        stable = f"/dev/microbit/{uid}"
        device = stable if os.path.exists(stable) else _prefer(
            found[uid].device if uid in found else None, p.device)
        found[uid] = PortInfo(uid=uid, device=device, description=p.description or "")
    return found


class PySerialScanner:
    def scan(self) -> dict[str, PortInfo]:
        return scan_ports()


# ---------------------------------------------------------------------------
# the real channel
# ---------------------------------------------------------------------------
class SerialChannel:
    """A relay's tty, driven by the event loop's selector."""

    def __init__(self, port: str, baud: int = 115200,
                 high_water: int = 65536, low_water: int = 16384) -> None:
        self.port = port
        self._baud = baud
        self._high = high_water
        self._low = low_water
        self._ser: serial.Serial | None = None
        self._fd: int = -1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wbuf = bytearray()
        self._reading = False
        self._writing = False          # add_writer on an already-armed fd silently
                                       # replaces the callback, so track it
        self._high_fired = False
        self._closed = False
        self._on_data: OnData = lambda _: None
        self._on_error: OnError = lambda _: None
        self._on_high: Callable[[], None] = lambda: None
        self._on_low: Callable[[], None] = lambda: None

    # -- lifecycle ---------------------------------------------------------
    async def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Blocking: opens the tty, toggles DTR (RESETTING THE BOARD), runs
        # tcsetattr. exclusive=True sets TIOCEXCL so a stray screen/minicom
        # cannot steal the port out from under a live session.
        self._ser = await asyncio.to_thread(functools.partial(
            serial.Serial, self.port, self._baud,
            timeout=0, write_timeout=0, exclusive=True))
        self._fd = self._ser.fileno()
        os.set_blocking(self._fd, False)
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # ORDER IS LOAD-BEARING. Deregister before closing: selecting on a closed
        # fd raises EBADF, and if the fd number gets recycled the loop starts
        # reading someone else's file.
        self._disarm_writer()
        self.stop_reading()
        self._wbuf.clear()
        if self._ser is not None and self._ser.is_open:
            await asyncio.to_thread(self._ser.close)
        self._ser = None
        self._fd = -1

    # -- reading -----------------------------------------------------------
    def start_reading(self, on_data: OnData, on_error: OnError) -> None:
        self._on_data, self._on_error = on_data, on_error
        if not self._reading and self._fd >= 0 and self._loop is not None:
            self._loop.add_reader(self._fd, self._do_read)
            self._reading = True

    def stop_reading(self) -> None:
        if self._reading and self._fd >= 0 and self._loop is not None:
            self._loop.remove_reader(self._fd)
        self._reading = False

    def _do_read(self) -> None:
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:          # yanked device: ENXIO / EIO
            self._fail(exc)
            return
        if not data:                    # EOF on a tty means the device is gone
            self._fail(None)
            return
        self._on_data(data)             # forwarded verbatim; no buffering, no timers

    # -- writing -----------------------------------------------------------
    def write_nowait(self, data: bytes) -> None:
        if self._closed:
            return
        self._wbuf += data
        self._flush()
        if len(self._wbuf) >= self._high and not self._high_fired:
            self._high_fired = True
            self._on_high()

    def _flush(self) -> None:
        while self._wbuf:
            try:
                n = os.write(self._fd, self._wbuf)
            except BlockingIOError:
                n = 0
            except OSError as exc:
                self._fail(exc)
                return
            if n == 0:
                self._arm_writer()
                return
            del self._wbuf[:n]
        self._disarm_writer()
        if self._high_fired and len(self._wbuf) <= self._low:
            self._high_fired = False
            self._on_low()

    def _arm_writer(self) -> None:
        if not self._writing and self._fd >= 0 and self._loop is not None:
            self._loop.add_writer(self._fd, self._flush)
            self._writing = True

    def _disarm_writer(self) -> None:
        if self._writing and self._fd >= 0 and self._loop is not None:
            self._loop.remove_writer(self._fd)
        self._writing = False

    async def drain(self, timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._wbuf and not self._closed and loop.time() < deadline:
            await asyncio.sleep(0.005)

    @property
    def pending_bytes(self) -> int:
        return len(self._wbuf)

    def set_watermarks(self, on_high, on_low) -> None:
        self._on_high, self._on_low = on_high, on_low

    def _fail(self, exc: BaseException | None) -> None:
        self.stop_reading()
        self._disarm_writer()
        self._on_error(exc)


class SerialChannelFactory:
    def __init__(self, baud: int = 115200, high_water: int = 65536,
                 low_water: int = 16384) -> None:
        self._baud, self._high, self._low = baud, high_water, low_water

    async def open(self, port: str) -> SerialChannel:
        ch = SerialChannel(port, self._baud, self._high, self._low)
        await ch.open()
        return ch


# ---------------------------------------------------------------------------
# socket helpers
# ---------------------------------------------------------------------------
def enable_keepalive(sock: socket.socket, idle: int, interval: int, count: int) -> None:
    """Turn on TCP keepalive, portably.

    Without this, a client whose laptop goes to sleep holds a relay until the
    process is restarted. Linux spells the idle timer TCP_KEEPIDLE, macOS spells
    it TCP_KEEPALIVE, and other platforms may have neither -- so every option is
    set defensively.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for name, value in (("TCP_KEEPIDLE", idle), ("TCP_KEEPALIVE", idle),
                        ("TCP_KEEPINTVL", interval), ("TCP_KEEPCNT", count)):
        opt = getattr(socket, name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass
