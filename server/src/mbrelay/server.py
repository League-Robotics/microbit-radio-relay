"""The TCP listener and the daemon that owns everything else.

The pipe is an ``asyncio.Protocol`` rather than a StreamReader/StreamWriter pair
because we need ``pause_reading()``/``resume_reading()`` for real TCP-window
backpressure, and because it keeps zero tasks on the steady-state data path --
every byte moves in a callback, with no scheduling hop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import time

from . import __version__
from .admin import AdminServer
from .errors import MbrelayError, NoFreeDevice
from .inventory import Inventory
from .relay import RelayControl
from .session import SessionManager
from .transport import (PySerialScanner, SerialChannelFactory, enable_keepalive)

log = logging.getLogger(__name__)


class RelayProtocol(asyncio.Protocol):
    """One client connection, bound to one board for its lifetime."""

    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon
        self.cfg = daemon.cfg
        self.transport: asyncio.Transport | None = None
        self.session = None
        self._peer = "?"
        self._bind_task: asyncio.Task | None = None
        self._closing = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport                      # type: ignore[assignment]
        peer = transport.get_extra_info("peername")
        self._peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        sock: socket.socket | None = transport.get_extra_info("socket")
        if sock is not None:
            if self.cfg.server.tcp_nodelay:
                # Without this, Nagle adds ~40ms to every small write and roughly
                # doubles the observed radio round-trip.
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
            if self.cfg.server.keepalive:
                enable_keepalive(sock, self.cfg.server.keepalive_idle,
                                 self.cfg.server.keepalive_interval,
                                 self.cfg.server.keepalive_count)

        # Hold the client's bytes until a board is ready. Without this, a client
        # that writes "?\n" the instant it connects would have those bytes
        # delivered before the reset settles. They wait in the kernel receive
        # buffer; TCP flow control means nothing is lost.
        self.transport.pause_reading()
        self._bind_task = asyncio.create_task(self._bind(), name="mbrelay-bind")

    async def _bind(self) -> None:
        try:
            self.session = await self.daemon.sessions.acquire(self._peer)
        except NoFreeDevice as exc:
            self._reject(exc)
            return
        except Exception:
            log.exception("bind failed peer=%s", self._peer)
            self._abort()
            return

        self.session.attach(sink=self._to_client, on_gone=self._device_gone)
        if self.session.preamble and self.cfg.server.preamble != "none":
            self.transport.write(self.session.preamble)
        if self.transport is not None:
            self.transport.resume_reading()

    def _reject(self, exc: NoFreeDevice) -> None:
        """No board free.

        A plain byte pipe has no error channel, so say it in the relay's own
        comment syntax: a client that already skips '#' lines is unaffected, and
        a human on netcat can read why.
        """
        log.warning("session_reject peer=%s reason=no_free_device total=%d "
                    "busy=%d releasing=%d",
                    self._peer, exc.total, exc.busy, exc.releasing)
        template = self.cfg.server.reject_message
        if template and self.transport is not None:
            message = template.format(total=exc.total, busy=exc.busy,
                                      releasing=exc.releasing)
            self.transport.write(message.encode("utf-8", "replace") + b"\r\n")
            self.transport.close()
        else:
            self._abort()

    def _abort(self) -> None:
        if self.transport is not None:
            self.transport.abort()

    def _to_client(self, data: bytes) -> None:
        if self.transport is not None and not self._closing:
            self.transport.write(data)

    def _device_gone(self, exc: BaseException | None) -> None:
        # The board vanished or errored. Close the socket with a FIN: the client
        # sees a clean EOF, exactly what `cat /dev/ttyACM0` sees on an unplug.
        self._closing = True
        if self.transport is not None:
            self.transport.close()

    def data_received(self, data: bytes) -> None:
        if self.session is not None:
            self.session.write_to_board(data)

    def eof_received(self) -> bool:
        return False                 # client half-closed; close our side too

    def pause_writing(self) -> None:
        # The client is not draining. Nothing to do: the board produces at
        # 115200 baud at worst, and asyncio buffers that happily.
        pass

    def connection_lost(self, exc: Exception | None) -> None:
        if self._bind_task is not None and not self._bind_task.done():
            self._bind_task.cancel()
        if self.session is not None:
            session, self.session = self.session, None
            reason = "client_error" if exc else "client_closed"
            self.daemon.spawn(self.daemon.sessions.release(session, reason),
                              name="mbrelay-release")


class Daemon:
    """Owns the inventory, the sessions, the listener and the admin socket."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.started = time.time()
        self.factory = SerialChannelFactory(
            baud=cfg.serial.baud,
            high_water=cfg.serial.write_high_water,
            low_water=cfg.serial.write_low_water)
        self.control = RelayControl(cfg)
        self.inventory = Inventory(cfg, PySerialScanner(), prober=self._probe)
        self.sessions = SessionManager(cfg, self.inventory, self.factory, self.control)
        self.admin = AdminServer(self)
        self.server: asyncio.AbstractServer | None = None
        self.conns_total = 0
        self._tasks: set[asyncio.Task] = set()
        self._stopping = asyncio.Event()

    async def _probe(self, record):
        return await self.control.probe(self.factory, record.port)

    def spawn(self, coro, name: str | None = None) -> asyncio.Task:
        """Fire-and-forget with a strong reference, so the task is not GC'd."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- lifecycle ---------------------------------------------------------
    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stopping.set)
        try:
            loop.add_signal_handler(signal.SIGHUP, self._reload)
        except (NotImplementedError, AttributeError):
            pass

        await self.admin.start()
        await self.inventory.start()

        def factory() -> RelayProtocol:
            self.conns_total += 1
            return RelayProtocol(self)

        try:
            self.server = await loop.create_server(
                factory, self.cfg.server.bind, self.cfg.server.port,
                backlog=self.cfg.server.backlog, reuse_address=True)
        except OSError as exc:
            # A bare traceback here is a poor way to learn that something else
            # already has the port -- typically a daemon left over from a
            # previous run, or the systemd unit still active.
            raise MbrelayError(
                f"cannot listen on {self.cfg.server.bind}:{self.cfg.server.port}: "
                f"{exc.strerror or exc}. Another mbrelay may already be running "
                f"(systemctl status mbrelay), or choose a different port."
            ) from exc

        counts = self.inventory.counts()
        log.info("daemon_start version=%s pid=%d", __version__, __import__("os").getpid())
        log.info("listen addr=%s:%d devices=%d free=%d admin=%s",
                 self.cfg.server.bind, self.cfg.server.port,
                 counts["total"], counts["free"], self.cfg.admin.socket)

        await self._stopping.wait()
        return await self._shutdown()

    def _reload(self) -> None:
        from .logs import set_level
        log.info("SIGHUP: reloading log level (bind address changes need a restart)")
        set_level(self.cfg.log.level)

    async def _shutdown(self) -> int:
        log.info("shutdown starting sessions=%d", len(self.sessions.sessions))
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        await self.sessions.shutdown(self.cfg.state.shutdown_grace_s)
        await self.inventory.stop()
        await self.admin.stop()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("shutdown complete")
        return 0

    # -- introspection -----------------------------------------------------
    def status(self) -> dict:
        counts = self.inventory.counts()
        return {
            "version": __version__,
            "pid": __import__("os").getpid(),
            "uptime_s": round(time.time() - self.started, 1),
            "listeners": [{
                "addr": f"{self.cfg.server.bind}:{self.cfg.server.port}",
                "conns_total": self.conns_total,
                "accepted": self.sessions.accepted_total,
                "rejected": self.sessions.rejected_total,
            }],
            "devices": counts,
            "sessions": self.sessions.to_json(),
        }
