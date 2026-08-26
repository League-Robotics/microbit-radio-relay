"""Driving one relay board through its command plane.

This is where the firmware's quirks are encoded, and they are load-bearing:

* **Reset means close AND reopen the port.** An in-place DTR toggle does not
  reset the board. Since the data plane has no in-band escape, close/reopen is
  the *only* way back to the command plane -- which is exactly why release works.
* **The boot banner is normally missed**, because it is emitted while the host is
  still opening the port. So we always ask again with ``HELLO``.
* **``!DEFAULTS`` does not change live state.** It clears the stored flash record;
  the compiled defaults only take effect on the *next* reset. Normalizing
  therefore has to send explicit values. Do not "simplify" this away --
  ``test_relay_sequences.py`` pins it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .errors import RelayError
from .inventory import BANNER_RE
from .transport import ByteChannel

log = logging.getLogger(__name__)

# printConfig() output. One line confirms channel, group, mode and power at once.
CONFIG_RE = re.compile(
    rb"#\s*channel:\s*(\d+)\s+group:\s*(\d+)\s+mode:\s*(\S+)\s+power:\s*(\d+)")

#: Restore the compiled-in factory defaults, explicitly, one command at a time.
#:
#: Each entry is (command, expected acknowledgement). ``!C 0`` is last because it
#: forces group 10, so it must not be undone by a later command.
#:
#: Sent one at a time rather than as one burst. Two reasons, both learned on
#: hardware: ``!MODE`` reconfigures the radio (``applyRadioConfig`` disables the
#: peripheral and spins on hardware flags with the radio IRQ masked), and a burst
#: arriving behind that lands in a board that is not reading, which shows up as
#: ``# error: unknown command`` for the commands that got chopped.
NORMALIZE_STEPS: tuple[tuple[bytes, bytes], ...] = (
    (b"!MODE RAW250\n", rb"#\s*mode:\s*RAW250"),
    (b"!FRAG OFF\n", rb"#\s*frag:\s*OFF"),
    (b"!ECHO OFF\n", rb"#\s*echo:\s*OFF"),
    (b"!P 7\n", rb"#\s*channel:"),
    (b"!C 0\n", rb"#\s*channel:\s*0\s+group:\s*10"),
)

#: The whole batch, for callers that only want to look at the bytes (tests do).
NORMALIZE = b"".join(cmd for cmd, _ in NORMALIZE_STEPS)

#: What ``?`` must report once the steps above have been applied.
DEFAULT_CFG = (0, 10, b"RAW250", 7)


@dataclass(frozen=True)
class BannerInfo:
    role: str
    device_name: str
    serial: str
    raw: bytes

    @classmethod
    def parse(cls, data: bytes) -> "BannerInfo | None":
        m = BANNER_RE.search(data)
        if not m:
            return None
        return cls(role=m.group(1).decode(), device_name=m.group(2).decode(errors="replace"),
                   serial=m.group(3).decode(), raw=m.group(0))


class Reader:
    """Accumulates everything a channel emits so sequences can scan for patterns."""

    def __init__(self, channel: ByteChannel) -> None:
        self.buf = bytearray()
        self._event = asyncio.Event()
        self.error: BaseException | None = None
        self.failed = False
        channel.start_reading(self._on_data, self._on_error)

    def _on_data(self, data: bytes) -> None:
        self.buf.extend(data)
        self._event.set()

    def _on_error(self, exc: BaseException | None) -> None:
        self.error, self.failed = exc, True
        self._event.set()

    def clear(self) -> None:
        self.buf.clear()

    def complete_lines(self) -> bytes:
        """The buffer up to and including its last newline.

        Everything the relay says in the command plane is a whole line, and
        matching a pattern against a PARTIAL line is how banners came back
        truncated in the field: BANNER_RE ends in ([0-9A-Fa-f]+), so the moment a
        read delivered ":getez:1" it matched with a one-digit serial and the
        remaining digits were discarded. Only ever match what has terminated.
        """
        end = self.buf.rfind(b"\n")
        return bytes(self.buf[:end + 1]) if end >= 0 else b""

    async def wait_for(self, pattern: re.Pattern[bytes], timeout: float):
        """Scan the completed lines for a pattern until it matches or time runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if m := pattern.search(self.complete_lines()):
                return m
            if self.failed:
                raise RelayError(f"device read failed: {self.error!r}")
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except TimeoutError:
                return pattern.search(self.complete_lines())


class RelayControl:
    """Command-plane sequences, shared by acquire and release.

    Both paths need the identical "reset the board and put it back to factory
    defaults" dance, so it lives here once.
    """

    def __init__(self, cfg) -> None:
        s = cfg.serial
        self.open_settle = s.open_settle_ms / 1000
        self.hello_timeout = s.hello_timeout_ms / 1000
        self.hello_attempts = max(1, s.hello_attempts)
        self.post_close_settle = s.post_close_settle_ms / 1000
        self.break_duration = s.break_duration_ms / 1000
        self.break_settle = s.break_settle_ms / 1000

    async def hello(self, channel: ByteChannel, reader: Reader,
                    allow_break: bool = True) -> BannerInfo:
        """Confirm we are in the command plane, and learn which board this is.

        The boot banner went out while we were still opening the port, so ask
        again. Several attempts, because a board still running its boot animation
        can miss the first line.

        If nothing answers, send a break before giving up. A silent board is
        almost always one sitting in the data plane, where "HELLO" is radio
        payload rather than a command -- and the data plane has no in-band
        escape, so the only way out is a reboot.

        Reopening the port is supposed to be that reboot, and on macOS it is. On
        Linux it is not: measured on Ubuntu 24.04 against DAPLink v0257, neither
        close/reopen, nor a DTR pulse, nor a 1200-baud touch resets the target,
        and a board parked in the data plane stays deaf indefinitely. A break
        condition rescues it every time. So the break is not paranoia -- without
        it the release guarantee simply does not hold on the platform the fleet
        actually runs.
        """
        if info := await self._ask_hello(channel, reader):
            return info

        if allow_break:
            log.info("no banner; sending a break to reset the board "
                     "(it is probably stuck in the data plane)")
            try:
                await channel.send_break(self.break_duration)
            except Exception as exc:
                log.debug("send_break failed: %r", exc)
            else:
                await asyncio.sleep(self.break_settle)
                reader.clear()
                if info := await self._ask_hello(channel, reader):
                    log.info("break recovered the board")
                    return info

        raise RelayError("no DEVICE banner after "
                         f"{self.hello_attempts} HELLO attempts"
                         f"{' and a break' if allow_break else ''} -- "
                         "board may not be running relay firmware")

    async def _ask_hello(self, channel: ByteChannel,
                         reader: Reader) -> "BannerInfo | None":
        for attempt in range(self.hello_attempts):
            channel.write_nowait(b"HELLO\n")
            match = await reader.wait_for(BANNER_RE, self.hello_timeout)
            if match:
                info = BannerInfo.parse(match.group(0))
                if info is not None:
                    return info
            log.debug("no banner yet attempt=%d/%d", attempt + 1, self.hello_attempts)
        return None

    async def normalize(self, channel: ByteChannel, reader: Reader,
                        step_timeout: float = 1.5, retries: int = 1) -> None:
        """Force the board back to factory defaults, then verify with a fresh query.

        Verification is a separate ``?`` rather than a scan of the replies to the
        setting commands. That is not belt-and-braces, it is necessary: ``!P``
        *also* calls printConfig(), so the batch emits more than one config line
        and the earliest one still shows the channel we are about to change. A
        standalone ``?`` produces exactly one line, describing the state that
        actually ended up on the board.
        """
        for attempt in range(retries + 1):
            for command, ack in NORMALIZE_STEPS:
                reader.clear()
                channel.write_nowait(command)
                # A missed ack is not fatal -- the query below is the authority.
                await reader.wait_for(re.compile(ack), step_timeout)

            config = await self.query(channel, reader, timeout=step_timeout * 2)
            if config is not None and self._is_default(config):
                return
            log.debug("normalize not confirmed attempt=%d config=%r tail=%r",
                      attempt + 1,
                      config.group(0) if config else None,
                      bytes(reader.buf)[-120:])
            # Let the board finish whatever it is still emitting before trying
            # again, so the retry is not written into a board mid-reply.
            await asyncio.sleep(0.4)
        raise RelayError("board did not confirm factory defaults after normalize; "
                         f"last output: {bytes(reader.buf)[-160:]!r}")

    async def query(self, channel: ByteChannel, reader: Reader,
                    timeout: float = 3.0) -> "re.Match[bytes] | None":
        """Ask the board what its live config is. One command, one reply line."""
        reader.clear()
        channel.write_nowait(b"?\n")
        return await reader.wait_for(CONFIG_RE, timeout)

    @staticmethod
    def _is_default(match: re.Match[bytes]) -> bool:
        channel, group, mode, power = match.groups()
        return (int(channel), int(group), mode, int(power)) == DEFAULT_CFG

    async def clear_stored_config(self, channel: ByteChannel, reader: Reader,
                                  timeout: float = 1.0) -> None:
        """Drop the saved flash record so a *cold* boot also lands on defaults.

        Best-effort: normalize() has already fixed the live state, and this only
        matters if the board is power-cycled before its next session.
        """
        reader.clear()
        channel.write_nowait(b"!DEFAULTS\n")
        await reader.wait_for(re.compile(rb"#\s*stored config cleared"), timeout)

    async def reset_and_normalize(self, factory, port: str, clear_stored: bool = True
                                  ) -> BannerInfo:
        """Open the port (which resets the board), verify, and restore defaults.

        Used by acquire (to guarantee a clean board), by release (to leave one),
        and by ``mbrelay reset``.
        """
        channel = await factory.open(port)
        try:
            await asyncio.sleep(self.open_settle)
            reader = Reader(channel)
            info = await self.hello(channel, reader)
            await self.normalize(channel, reader)
            if clear_stored:
                await self.clear_stored_config(channel, reader)
            return info
        finally:
            await channel.close()

    async def probe(self, factory, port: str) -> BannerInfo | None:
        """Identify a board. Returns None if nothing answers -- not an error.

        A blank board, or a robot that is not a relay, simply stays quiet; the
        inventory backs off rather than rebooting it every scan.
        """
        channel = await factory.open(port)
        try:
            await asyncio.sleep(self.open_settle)
            reader = Reader(channel)
            try:
                return await self.hello(channel, reader)
            except RelayError:
                return None
        finally:
            await channel.close()
