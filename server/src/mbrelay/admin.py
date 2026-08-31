"""The local control surface: a Unix socket speaking newline-delimited JSON.

Never TCP. Authorization is filesystem permissions and nothing else -- the socket
is mode 0660 in a directory systemd owns, and an operator gets access by being in
the right group. Inventing a token scheme here would add a secret to manage
without adding a boundary that the filesystem does not already provide.

JSON Lines rather than JSON-RPC (ceremony with no payoff at this size) or a
bespoke text grammar (the CLI needs machine-readable output for --json anyway).

Handlers are plain functions in a registry dict, so the whole command surface is
unit-testable without opening a socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from . import __version__
from .errors import AdminError, RegistryError
from .inventory import DeviceState

log = logging.getLogger(__name__)

MAX_LINE = 1 << 20          # a request line has no business being bigger

HANDLERS = {}


def handler(name: str):
    def register(fn):
        HANDLERS[name] = fn
        return fn
    return register


# ---------------------------------------------------------------------------
# handlers: (daemon, args) -> result dict
# ---------------------------------------------------------------------------
@handler("ping")
def _ping(daemon, args):
    return {"pong": True, "version": __version__}


@handler("version")
def _version(daemon, args):
    return {"version": __version__}


@handler("status")
def _status(daemon, args):
    return daemon.status()


@handler("list")
def _list(daemon, args):
    show_all = bool(args.get("all"))
    rows = [r.to_json() for r in daemon.inventory.records.values()
            if show_all or r.state is not DeviceState.GONE]
    rows.sort(key=lambda r: (r["state"], r["name"]))
    return {"devices": rows}


@handler("sessions")
def _sessions(daemon, args):
    return {"sessions": daemon.sessions.to_json()}


@handler("kick")
def _kick(daemon, args):
    targets = []
    if args.get("all"):
        targets = list(daemon.sessions.sessions.values())
    elif sid := args.get("session"):
        session = daemon.sessions.sessions.get(sid)
        if session is None:
            raise AdminError(f"no session {sid}", code="not_found")
        targets = [session]
    elif token := args.get("device"):
        record = daemon.inventory.find(token)
        if record is None:
            raise AdminError(f"no device matching {token!r}", code="not_found")
        targets = [s for s in daemon.sessions.sessions.values() if s.record is record]
    else:
        raise AdminError("kick needs session, device or all", code="bad_request")

    reason = args.get("reason") or "operator"
    for session in targets:
        daemon.spawn(daemon.sessions.kick(session, reason), name="mbrelay-kick")
    return {"kicked": [s.id for s in targets]}


@handler("rescan")
def _rescan(daemon, args):
    daemon.spawn(_do_rescan(daemon, bool(args.get("force"))), name="mbrelay-rescan")
    return {"scheduled": True}


async def _do_rescan(daemon, force: bool):
    await daemon.inventory.scan_once()
    daemon.inventory.rescan(force=force)


@handler("disable")
def _disable(daemon, args):
    record = _require_device(daemon, args)
    if record.state in (DeviceState.BUSY, DeviceState.ACQUIRING, DeviceState.RELEASING):
        raise AdminError(f"{record.name} is in use; kick it first", code="busy")
    record.state = DeviceState.DISABLED
    record.disabled_reason = args.get("reason") or "operator"
    return {"device": record.to_json()}


@handler("enable")
def _enable(daemon, args):
    record = _require_device(daemon, args)
    if record.state is not DeviceState.DISABLED:
        return {"device": record.to_json(), "changed": False}
    record.disabled_reason = ""
    record.state = DeviceState.UNKNOWN
    daemon.inventory._classify_or_probe(record)
    return {"device": record.to_json(), "changed": True}


@handler("reset")
def _reset(daemon, args):
    if args.get("all"):
        records = [r for r in daemon.inventory.records.values()
                   if r.port and r.state not in (DeviceState.GONE,)]
    else:
        records = [_require_device(daemon, args)]

    busy = [r.name for r in records
            if r.state in (DeviceState.BUSY, DeviceState.ACQUIRING, DeviceState.RELEASING)]
    if busy and not args.get("force"):
        raise AdminError(f"in use: {', '.join(busy)} (use --force)", code="busy")

    daemon.spawn(_do_reset(daemon, records, bool(args.get("force"))), name="mbrelay-reset")
    return {"scheduled": [r.name for r in records]}


async def _do_reset(daemon, records, force: bool):
    for record in records:
        session = daemon.sessions.sessions.get(record.session_id or "")
        if session is not None and force:
            await daemon.sessions.kick(session, "reset")
            await asyncio.sleep(1.0)
        if record.port is None:
            continue
        try:
            await daemon.control.reset_and_normalize(daemon.factory, record.port)
            record.state = DeviceState.FREE
            record.error_count = 0
            log.info("reset_ok uid=%s name=%s", record.uid, record.name)
        except Exception as exc:
            record.state = DeviceState.ERROR
            record.last_error = repr(exc)
            log.error("reset_failed uid=%s err=%r", record.uid, exc)


@handler("names")
def _names(daemon, args):
    """The registry, over the local socket as well as over HTTP -- an operator
    on the box should not have to curl their own daemon."""
    if name := args.get("name"):
        try:
            return {"name": daemon.registry.resolve(name).to_json()}
        except RegistryError as exc:
            raise AdminError(str(exc), code=exc.code) from None
    return daemon.registry.listing()


@handler("names_set")
def _names_set(daemon, args):
    name = args.get("name")
    if not name:
        raise AdminError("names set needs a name", code="bad_request")
    try:
        channel, group = int(args["channel"]), int(args["group"])
    except (KeyError, TypeError, ValueError):
        raise AdminError("names set needs a channel and a group",
                         code="bad_request") from None
    try:
        return {"name": daemon.registry.set(name, channel, group).to_json()}
    except RegistryError as exc:
        raise AdminError(str(exc), code=exc.code) from None


@handler("names_clear")
def _names_clear(daemon, args):
    name = args.get("name")
    if not name:
        raise AdminError("names clear needs a name", code="bad_request")
    try:
        return {"name": daemon.registry.clear(name).to_json()}
    except RegistryError as exc:
        raise AdminError(str(exc), code=exc.code) from None


@handler("config")
def _config(daemon, args):
    return {"config": daemon.cfg.as_dict(), "sources": daemon.cfg.sources}


@handler("loglevel")
def _loglevel(daemon, args):
    from .logs import set_level
    level = args.get("level")
    if not level:
        raise AdminError("loglevel needs a level", code="bad_request")
    set_level(level)
    return {"level": level}


@handler("shutdown")
def _shutdown(daemon, args):
    if not daemon.cfg.admin.allow_shutdown:
        raise AdminError("shutdown is disabled (admin.allow_shutdown); use systemctl",
                         code="forbidden")
    daemon._stopping.set()
    return {"stopping": True}


def _require_device(daemon, args):
    token = args.get("device") or args.get("name")
    if not token:
        raise AdminError("no device given", code="bad_request")
    record = daemon.inventory.find(token)
    if record is None:
        raise AdminError(f"no device matching {token!r}", code="not_found")
    return record


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------
class AdminServer:
    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.path = Path(daemon.cfg.admin.socket)
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: set[asyncio.Queue] = set()

    # sockaddr_un.sun_path is a fixed-size buffer: 104 bytes on macOS/BSD, 108 on
    # Linux. Exceeding it gives a bare "AF_UNIX path too long", which is a
    # miserable thing to debug from a unit file, so check it here and say so.
    MAX_SOCKET_PATH = 100

    async def start(self) -> None:
        if len(str(self.path).encode()) > self.MAX_SOCKET_PATH:
            raise AdminError(
                f"admin socket path is too long for AF_UNIX "
                f"({len(str(self.path))} bytes, limit ~{self.MAX_SOCKET_PATH}): {self.path}\n"
                f"Set a shorter admin.socket, or $MBRELAY_SOCKET.",
                code="bad_config")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await self._clear_stale_socket()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        self._apply_permissions()
        log.debug("admin socket listening path=%s", self.path)

    async def _clear_stale_socket(self) -> None:
        """Refuse to start if another daemon is live; clean up if it is not."""
        if not self.path.exists():
            return
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.path))
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            self.path.unlink(missing_ok=True)       # stale socket from a crash
            return
        writer.close()
        await writer.wait_closed()
        _ = reader
        raise AdminError(
            f"another mbrelay daemon is already listening at {self.path}",
            code="already_running")

    def _apply_permissions(self) -> None:
        try:
            os.chmod(self.path, int(self.daemon.cfg.admin.socket_mode, 8))
        except OSError as exc:
            log.warning("could not chmod admin socket path=%s err=%r", self.path, exc)
        group = self.daemon.cfg.admin.socket_group
        if not group:
            return
        try:
            import grp
            os.chown(self.path, -1, grp.getgrnam(group).gr_gid)
        except (KeyError, OSError, ImportError) as exc:
            log.warning("could not set admin socket group=%s err=%r", group, exc)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        self.path.unlink(missing_ok=True)

    def publish(self, event: str, data: dict) -> None:
        """Fan an event out to any `mbrelay events --follow` subscribers."""
        import time
        payload = {"event": event, "ts": round(time.time(), 3), "data": data}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass          # a subscriber that cannot keep up misses events

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionResetError, asyncio.LimitOverrunError):
                    return
                if not line:
                    return
                if len(line) > MAX_LINE:
                    await self._send(writer, {"ok": False, "error": {
                        "code": "too_large", "message": "request line too large"}})
                    return
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                except ValueError as exc:
                    await self._send(writer, {"ok": False, "error": {
                        "code": "bad_request", "message": str(exc)}})
                    return
                if not await self._dispatch(request, writer):
                    return
        finally:
            writer.close()

    async def _dispatch(self, request: dict, writer: asyncio.StreamWriter) -> bool:
        req_id = request.get("id")
        cmd = request.get("cmd")
        args = request.get("args") or {}

        if cmd == "events":
            await self._send(writer, {"id": req_id, "ok": True, "result": {"stream": True}})
            await self._stream_events(req_id, writer)
            return False

        fn = HANDLERS.get(cmd)
        if fn is None:
            await self._send(writer, {"id": req_id, "ok": False, "error": {
                "code": "unknown_command", "message": f"unknown command {cmd!r}"}})
            return True
        try:
            result = fn(self.daemon, args)
        except AdminError as exc:
            await self._send(writer, {"id": req_id, "ok": False,
                                      "error": {"code": exc.code, "message": str(exc)}})
            return True
        except Exception as exc:
            log.exception("admin command failed cmd=%s", cmd)
            await self._send(writer, {"id": req_id, "ok": False,
                                      "error": {"code": "error", "message": repr(exc)}})
            return True
        await self._send(writer, {"id": req_id, "ok": True, "result": result})
        return True

    async def _stream_events(self, req_id, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        try:
            while True:
                payload = await queue.get()
                await self._send(writer, {"id": req_id, **payload})
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.discard(queue)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, payload: dict) -> None:
        try:
            writer.write(json.dumps(payload, default=str).encode() + b"\n")
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass


def terminal_width(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns
