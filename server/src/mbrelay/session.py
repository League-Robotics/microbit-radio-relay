"""Binding a client to a board, piping bytes, and giving the board back clean.

The contract this module implements:

* A client that gets bound receives a board at **verified factory defaults**.
* When the client goes away the board is **reset and normalized** before anyone
  else can have it, so nobody inherits the previous channel or echo setting.
* Between those two points the daemon is a dumb pipe and adds nothing.

Normalizing happens on acquire *as well as* release. Release alone would be a
hope, not a guarantee: a SIGKILL or a power cut leaves the last client's channel
sitting in the board's flash. Doing it on acquire too is cheap, because the
firmware's saveConfig() skips writes that would not change anything -- so a board
that released cleanly costs zero flash erases on the way back out.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Callable

from .errors import AcquireFailed, NoFreeDevice
from .inventory import DeviceRecord, DeviceState, Inventory
from .logs import session_logger
from .relay import Reader, RelayControl
from .transport import ByteChannel

log = logging.getLogger(__name__)
_ids = itertools.count(1)

# Emitted by the firmware immediately before it leaves the command plane. Watched
# read-only so "mbrelay status" can say which plane a session is in.
_DATA_PLANE_MARK = b"# entering data plane"


class Session:
    """One client socket bound to one board."""

    def __init__(self, record: DeviceRecord, channel: ByteChannel,
                 reader: Reader, peer: str, preamble: bytes) -> None:
        self.id = f"s-{next(_ids)}"
        self.record = record
        self.channel = channel
        self.reader = reader
        self.peer = peer
        self.preamble = preamble
        self.started = time.time()
        self.rx_bytes = 0            # board -> client
        self.tx_bytes = 0            # client -> board
        self.last_activity = self.started
        self.plane = "command"
        # Serial reads land on arbitrary chunk boundaries, so the marker below
        # can straddle two of them. Keep the tail of the previous chunk and
        # search the join.
        self._plane_tail = b""
        self._sink: Callable[[bytes], None] | None = None
        self._on_gone: Callable[[BaseException | None], None] = lambda _: None
        self.log = session_logger(log, session=self.id, uid=record.uid,
                                  dev=record.name, peer=peer)

    @property
    def age(self) -> float:
        return time.time() - self.started

    @property
    def idle(self) -> float:
        return time.time() - self.last_activity

    def attach(self, sink: Callable[[bytes], None],
               on_gone: Callable[[BaseException | None], None]) -> None:
        """Start forwarding board output to the client."""
        self._sink = sink
        self._on_gone = on_gone
        # Anything the board said between the banner and now is setup noise the
        # client must not see; a direct serial client on a default board would
        # not have seen it either.
        self.reader.clear()
        self.channel.start_reading(self._on_board_data, self._on_board_error)

    def detach(self) -> None:
        self._sink = None
        self.channel.stop_reading()

    def _on_board_data(self, data: bytes) -> None:
        self.rx_bytes += len(data)
        self.last_activity = time.time()
        if self.plane == "command":
            self._watch_for_data_plane(data)
        if self._sink is not None:
            self._sink(data)          # verbatim, immediately: no coalescing

    def _watch_for_data_plane(self, data: bytes) -> None:
        """Notice the board leaving the command plane.

        Telemetry only -- it is what `mbrelay status` shows in the PLANE column.
        A read-only tee: it never alters or delays the stream, and it disarms
        itself once it has fired.
        """
        window = self._plane_tail + data
        if _DATA_PLANE_MARK in window:
            self.plane = "data"
            self._plane_tail = b""
            return
        self._plane_tail = window[-(len(_DATA_PLANE_MARK) - 1):]

    def _on_board_error(self, exc: BaseException | None) -> None:
        self.log.warning("device read failed mid-session err=%r", exc)
        self._on_gone(exc)

    def write_to_board(self, data: bytes) -> None:
        self.tx_bytes += len(data)
        self.last_activity = time.time()
        self.channel.write_nowait(data)

    def to_json(self) -> dict:
        return {
            "id": self.id, "uid": self.record.uid, "device_name": self.record.name,
            "port": self.record.port, "peer": self.peer,
            "started": round(self.started, 3), "age_s": round(self.age, 1),
            "plane": self.plane, "rx_bytes": self.rx_bytes, "tx_bytes": self.tx_bytes,
            "idle_s": round(self.idle, 1),
        }


class SessionManager:
    """Allocates boards to clients and cleans up after them."""

    def __init__(self, cfg, inventory: Inventory, factory, control: RelayControl) -> None:
        self.cfg = cfg
        self.inventory = inventory
        self.factory = factory
        self.control = control
        self.sessions: dict[str, Session] = {}
        self.rejected_total = 0
        self.accepted_total = 0
        self._retry_tasks: set[asyncio.Task] = set()
        inventory.on_device_gone = self._device_gone

    # -- acquire -----------------------------------------------------------
    async def acquire(self, peer: str) -> Session:
        """Bind a free board, or raise NoFreeDevice.

        Honours ``server.acquire_wait_ms``: 0 means reject immediately, which is
        the documented default. A non-zero wait is for test harnesses that
        reconnect straight after disconnecting, before release has finished.
        """
        deadline = time.monotonic() + self.cfg.server.acquire_wait_ms / 1000
        while True:
            session = await self._try_acquire(peer)
            if session is not None:
                return session
            if time.monotonic() >= deadline:
                counts = self.inventory.counts()
                self.rejected_total += 1
                raise NoFreeDevice(total=counts["total"], busy=counts["busy"])
            await asyncio.sleep(0.25)

    async def _try_acquire(self, peer: str) -> Session | None:
        for _ in range(max(1, self.cfg.server.acquire_retries)):
            candidates = self.inventory.free_devices()
            if not candidates:
                return None
            # Least recently used, so boards wear evenly and a flaky one does not
            # get handed out repeatedly.
            record = min(candidates, key=lambda r: (r.sessions_total, r.uid))
            if not self.inventory.reserve(record):
                continue              # someone else won the race; try the next one
            try:
                return await self._bring_up(record, peer)
            except Exception as exc:
                record.state = DeviceState.ERROR
                self.inventory.note_error(record, repr(exc))
                log.error("acquire failed uid=%s port=%s err=%r",
                          record.uid, record.port, exc)
        return None

    async def _bring_up(self, record: DeviceRecord, peer: str) -> Session:
        """Open, verify, normalize, and hand back a ready Session."""
        if record.port is None:
            raise AcquireFailed(f"{record.uid} has no port")
        budget = self.cfg.serial.acquire_budget_ms / 1000
        async with asyncio.timeout(budget):
            channel = await self.factory.open(record.port)
            try:
                await asyncio.sleep(self.control.open_settle)
                reader = Reader(channel)
                info = await self.control.hello(channel, reader)
                if record.device_name and info.device_name != record.device_name:
                    # A different board answered on this path -- the port moved
                    # between the scan and the open. Abort; the caller retries.
                    raise AcquireFailed(
                        f"expected {record.device_name}, got {info.device_name}")
                record.role, record.device_name = info.role, info.device_name
                record.nrf_serial, record.banner_raw = info.serial, info.raw
                await self.control.normalize(channel, reader)
            except BaseException:
                await channel.close()
                raise

        preamble = b"" if self.cfg.server.preamble == "none" else info.raw + b"\r\n"
        session = Session(record, channel, reader, peer, preamble)
        record.state = DeviceState.BUSY
        record.session_id = session.id
        record.sessions_total += 1
        self.sessions[session.id] = session
        self.accepted_total += 1
        session.log.info("session_open port=%s", record.port)
        return session

    # -- release -----------------------------------------------------------
    async def release(self, session: Session, reason: str) -> None:
        """Give the board back, restored to factory defaults.

        The board is almost certainly in the data plane, where ``!C 0`` would go
        out over the radio as payload rather than being read as a command. So the
        reset comes first, and it is a close/reopen because that is the only
        thing that resets the board.
        """
        record = session.record
        self.sessions.pop(session.id, None)
        record.session_id = None
        record.bytes_in += session.rx_bytes
        record.bytes_out += session.tx_bytes
        if record.state not in (DeviceState.GONE,):
            record.state = DeviceState.RELEASING

        session.detach()
        await session.channel.close()
        await asyncio.sleep(self.control.post_close_settle)

        session.log.info("session_close reason=%s dur=%.1fs rx=%d tx=%d",
                         reason, session.age, session.rx_bytes, session.tx_bytes)

        if record.state is DeviceState.GONE or record.uid not in self.inventory.live_uids:
            record.state = DeviceState.GONE
            record.port = None
            return

        await self._restore(record, reason)

    async def _restore(self, record: DeviceRecord, reason: str) -> None:
        try:
            async with asyncio.timeout(self.cfg.serial.release_budget_ms / 1000):
                await self.control.reset_and_normalize(self.factory, record.port)
        except Exception as exc:
            record.state = DeviceState.ERROR
            self.inventory.note_error(record, repr(exc))
            log.error("release_failed uid=%s reason=%s err=%r", record.uid, reason, exc)
            self._schedule_retry(record)
            return
        record.state = DeviceState.FREE
        record.error_count = 0
        record.next_retry_at = 0.0
        log.info("release_ok uid=%s name=%s", record.uid, record.name)

    def _schedule_retry(self, record: DeviceRecord) -> None:
        """Retry the restore later. The retry *is* the same sequence."""
        async def retry() -> None:
            await asyncio.sleep(max(0.0, record.next_retry_at - time.time()))
            if record.state is not DeviceState.ERROR or record.port is None:
                return
            await self._restore(record, "retry")

        task = asyncio.create_task(retry(), name=f"mbrelay-restore-{record.uid[-8:]}")
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    # -- external events ---------------------------------------------------
    def _device_gone(self, record: DeviceRecord) -> None:
        """The scan loop noticed a board vanish while a session held it."""
        session = self.sessions.get(record.session_id or "")
        if session is not None:
            session.log.warning("device removed while bound")
            session._on_gone(None)

    async def kick(self, session: Session, reason: str = "operator") -> None:
        session.log.info("kicked reason=%s", reason)
        session._on_gone(None)

    async def shutdown(self, grace: float) -> None:
        """Return every board before exiting.

        A board left dirty here is not lost -- the next acquire normalizes it --
        but leaving one on channel 17 means the next student's radio is silently
        on the wrong channel, so it is worth the wait.
        """
        if not self.sessions:
            return
        log.info("shutdown draining sessions=%d grace=%.0fs", len(self.sessions), grace)
        tasks = [asyncio.create_task(self.release(s, "shutdown"))
                 for s in list(self.sessions.values())]
        done, pending = await asyncio.wait(tasks, timeout=grace)
        for task in pending:
            task.cancel()
        if pending:
            dirty = [s.record.uid for s in self.sessions.values()]
            log.error("shutdown grace expired; boards left un-normalized: %s",
                      ", ".join(dirty) or "(none)")

    def to_json(self) -> list[dict]:
        return [s.to_json() for s in self.sessions.values()]
