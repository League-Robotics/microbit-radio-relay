"""An in-process stand-in for a relay board.

Reply strings are copied verbatim from ``source/relay/RadioRelay.cpp`` so a test
passing here means something about the real firmware.

The single most important behaviour to model correctly is the DTR reset: opening
the port reboots the board back into the command plane, but the *stored config*
lives in flash and survives. Almost every interesting bug in the daemon is a
misunderstanding of that one line.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace


@dataclass
class StoredConfig:
    """The 8-byte record the firmware keeps under the "relaycfg" key."""
    channel: int = 0
    group: int = 10
    power: int = 7
    mode: str = "RAW250"
    frag: str = "OFF"
    echo: str = "OFF"


DEFAULTS = StoredConfig()


class FakeRelayFirmware:
    """A byte-level state machine speaking the relay's command grammar."""

    def __init__(self, name: str = "getez", serial: str = "1779042496",
                 role: str = "RADIOBRIDGE", *, drop_first_banners: int = 0,
                 stored: StoredConfig | None = None) -> None:
        self.name, self.serial, self.role = name, serial, role
        # Survives reset(), just like the real board's flash. Tests that want a
        # dirty board hand one in here.
        self.flash: StoredConfig | None = stored
        self.cfg = replace(self.flash) if self.flash else replace(DEFAULTS)
        self.plane = "command"
        self.out = bytearray()
        self._line = bytearray()
        self._drop_banners = drop_first_banners
        self.commands: list[bytes] = []      # every command line, in order
        self.data_plane_rx = bytearray()
        self.boot_count = 0
        self.peer: "FakeRelayFirmware | None" = None   # the other end of the radio

    # -- lifecycle ---------------------------------------------------------
    def reset(self) -> None:
        """What opening the port does: reboot, reload flash, back to command plane."""
        self.boot_count += 1
        self.cfg = replace(self.flash) if self.flash else replace(DEFAULTS)
        self.plane = "command"
        self._line.clear()
        self.out.clear()
        if self._drop_banners > 0:
            # Model the usual case: the boot banner goes out while the host is
            # still opening the port, so nobody sees it.
            self._drop_banners -= 1
        else:
            self._emit_banner()

    def _emit_banner(self) -> None:
        self.out += f"DEVICE:{self.role}:relay:{self.name}:{self.serial}\r\n".encode()

    def _comment(self, text: str) -> None:
        self.out += f"# {text}\r\n".encode()

    def _print_config(self) -> None:
        c = self.cfg
        self._comment(f"channel: {c.channel} group: {c.group} "
                      f"mode: {c.mode} power: {c.power}")

    def _save(self) -> None:
        self.flash = replace(self.cfg)

    # -- input -------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        if self.plane == "data":
            self.data_plane_rx += data
            if self.peer is not None:
                self.peer.out += data           # the "radio"
            return
        for byte in data:
            if byte == 0x0A:
                line, self._line = bytes(self._line), bytearray()
                self._handle(line.rstrip(b"\r"))
            else:
                self._line.append(byte)

    def _handle(self, line: bytes) -> None:
        self.commands.append(line)
        c = self.cfg

        if line == b"HELLO":
            self._emit_banner()
        elif line == b"?":
            self._print_config()
        elif line == b"!MODE?":
            self._comment(f"mode: {c.mode}")
        elif line in (b"!MODE RAW250", b"!MODE RAW251"):
            c.mode = "RAW250"; self._save(); self._comment("mode: RAW250")
        elif line == b"!MODE MAKECODE":
            c.mode = "MAKECODE"; self._save(); self._comment("mode: MAKECODE")
        elif line in (b"!FRAG ON", b"!FRAG OFF"):
            c.frag = line.split()[1].decode(); self._save()
            self._comment(f"frag: {c.frag}")
        elif line in (b"!ECHO", b"!ECHO ON", b"!ECHO OFF"):
            parts = line.split()
            c.echo = parts[1].decode() if len(parts) > 1 else ("OFF" if c.echo == "ON" else "ON")
            self._save(); self._comment(f"echo: {c.echo}")
        elif line == b"!DEFAULTS":
            # Clears the stored record ONLY. Live config is untouched until the
            # next reset -- this is the firmware behaviour the daemon must not
            # "simplify" away.
            self.flash = None
            self._comment("stored config cleared; defaults apply on next reset")
        elif line == b"!GO":
            self._comment("entering data plane")
            self.plane = "data"
        elif line.startswith(b"!C "):
            try:
                channel = int(line[3:])
            except ValueError:
                self._comment("error: usage !C <ch 0-35>"); return
            if not 0 <= channel <= 35:
                self._comment("error: usage !C <ch 0-35>"); return
            c.channel, c.group = channel, 10       # !C forces group 10
            self._save()
            self._print_config()                   # !C calls printConfig()
        elif line.startswith((b"!CG ", b"!RC ")):
            try:
                channel, group = (int(x) for x in line.split()[1:3])
            except ValueError:
                self._comment("error: usage !CG <ch 0-83> <group 0-255>"); return
            c.channel, c.group = channel, group
            self._save(); self._print_config()
        elif line.startswith(b"!P "):
            try:
                power = int(line[3:])
            except ValueError:
                self._comment("error: usage !P <0-7>"); return
            if not 0 <= power <= 7:
                self._comment("error: usage !P <0-7>"); return
            c.power = power
            self._save()
            self._print_config()                   # !P ALSO calls printConfig() --
                                                   # this is what broke the first
                                                   # version of normalize()
        elif line.startswith(b">"):
            payload = line[1:].lstrip()
            if self.peer is not None:
                self.peer.out += b"< " + payload + b"\r\n"
        elif line == b"!HELP":
            self._comment("!C <ch>            set channel")
        else:
            self._comment("error: unknown command (try !HELP)")

    def drain(self) -> bytes:
        data, self.out = bytes(self.out), bytearray()
        return data


class FakeChannel:
    """A ByteChannel backed by a FakeRelayFirmware."""

    def __init__(self, firmware: FakeRelayFirmware, latency: float = 0.0,
                 vanish_after_bytes: int | None = None) -> None:
        self.fw = firmware
        self.latency = latency
        self.vanish_after = vanish_after_bytes
        self.written = bytearray()
        self.closed = False
        self.reader_removed_before_close = None
        self._on_data = None
        self._on_error = None
        self._pump: asyncio.Task | None = None
        self._reading = False

    async def open(self) -> None:
        self.fw.reset()                 # opening the port resets the board
        self.closed = False
        self._pump = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self.closed:
                await asyncio.sleep(max(self.latency, 0.001))
                if self._reading and self._on_data is not None:
                    if data := self.fw.drain():
                        self._on_data(data)
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        # Mirrors SerialChannel: reader must come off before the fd goes away.
        self.reader_removed_before_close = not self._reading
        self.closed = True
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None

    def start_reading(self, on_data, on_error) -> None:
        self._on_data, self._on_error = on_data, on_error
        self._reading = True

    def stop_reading(self) -> None:
        self._reading = False

    def write_nowait(self, data: bytes) -> None:
        if self.closed:
            return
        self.written += data
        if self.vanish_after is not None and len(self.written) > self.vanish_after:
            if self._on_error is not None:
                self._on_error(OSError("device vanished"))
            return
        self.fw.feed(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    @property
    def pending_bytes(self) -> int:
        return 0

    def set_watermarks(self, on_high, on_low) -> None:
        self._on_high, self._on_low = on_high, on_low


class FakeChannelFactory:
    def __init__(self, boards: dict[str, FakeRelayFirmware], **kwargs) -> None:
        self.boards = boards                # port -> firmware
        self.kwargs = kwargs
        self.opened: list[str] = []
        self.channels: list[FakeChannel] = []
        self.fail_open_times = 0

    async def open(self, port: str) -> FakeChannel:
        self.opened.append(port)
        if self.fail_open_times > 0:
            self.fail_open_times -= 1
            raise OSError(f"cannot open {port}")
        channel = FakeChannel(self.boards[port], **self.kwargs)
        await channel.open()
        self.channels.append(channel)
        return channel


@dataclass
class FakeScanner:
    """A PortScanner whose answer the test controls."""
    ports: dict = field(default_factory=dict)      # uid -> PortInfo
    scans: int = 0

    def scan(self):
        self.scans += 1
        return dict(self.ports)
