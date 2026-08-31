"""The registry's network surface: a very small HTTP/1.1 server speaking JSON.

Why HTTP at all, when the daemon's control plane is a Unix socket: the people
who need this are not on this machine. Somebody building a robot's config, or
running a channel survey across the fleet, has to ask *the relay* where a robot
is, and `admin.py` is deliberately unreachable from off-box. So the registry
gets its own TCP port. It is unauthenticated on purpose -- an internal lab
service whose entire content is "which radio channel is tovez on", which is
also readable by anyone with an antenna.

Why hand-written, when every language has a web framework: this package has
exactly one dependency (pyserial), which is what lets a fleet node install with
``apt install python3-serial && pip install --break-system-packages <wheel>``.
That constraint produced the stdlib mDNS codec in ``mdns.py`` and it produces
this. Five routes do not justify a dependency, so the scope is kept honest:

* one request per connection -- ``Connection: close``, always, so there is no
  keep-alive state machine and no pipelining to get wrong;
* every read bounded, because this port is open to the LAN;
* no chunked encoding, no compression, no content negotiation. JSON in, JSON
  out, and a 400 for anything else.

Routing and rendering are plain functions over bytes, so the whole surface is
testable without a socket -- the same reason ``admin.py`` keeps its handlers in
a dict.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import __version__
from .errors import RegistryError
from .registry import validate_pair

log = logging.getLogger(__name__)

MAX_HEAD = 8192             # request line + headers
MAX_BODY = 64 * 1024        # a PUT body is two integers; this is already absurd
MAX_TARGET = 2048

_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 413: "Payload Too Large", 431: "Request Header Fields Too Large",
            500: "Internal Server Error"}

_CODE_STATUS = {"bad_request": 400, "pinned": 409, "not_found": 404}


class BadRequest(Exception):
    """The bytes on the wire were not a request we can act on."""

    def __init__(self, message: str, status: int = 400) -> None:
        self.status = status
        super().__init__(message)


class Request:
    __slots__ = ("method", "path", "headers")

    def __init__(self, method: str, path: str, headers: dict[str, str]) -> None:
        self.method = method
        self.path = path
        self.headers = headers

    @property
    def content_length(self) -> int:
        raw = self.headers.get("content-length", "0")
        try:
            length = int(raw)
        except ValueError:
            raise BadRequest(f"bad Content-Length: {raw!r}") from None
        if length < 0:
            raise BadRequest(f"bad Content-Length: {raw!r}")
        if length > MAX_BODY:
            raise BadRequest("body too large", status=413)
        return length


def parse_head(head: bytes) -> Request:
    """The request line and headers, already split off at the blank line."""
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:
        raise BadRequest("undecodable request") from None
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    parts = lines[0].split()
    if len(parts) != 3:
        raise BadRequest(f"bad request line: {lines[0][:80]!r}")
    method, target, version = parts
    if not version.startswith("HTTP/"):
        raise BadRequest(f"bad request line: {lines[0][:80]!r}")
    if len(target) > MAX_TARGET:
        raise BadRequest("request target too long", status=431)
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return Request(method.upper(), target.split("?", 1)[0].split("#", 1)[0], headers)


def render(status: int, payload: dict) -> bytes:
    body = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
    head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n").encode()
    return head + body


def route(registry, request: Request, body: bytes) -> tuple[int, dict]:
    """The whole API. Returns (status, payload); never raises for a bad
    request, because a 400 with a message is more useful than a stack trace in
    the daemon's log."""
    path = request.path.rstrip("/") or "/"
    try:
        if path == "/status":
            return _require(request, "GET") or (200, _status(registry))
        if path == "/names":
            return _require(request, "GET") or (200, registry.listing())
        if path.startswith("/names/"):
            name = path[len("/names/"):]
            if "/" in name:
                return 404, _error("not_found", f"no such route: {request.path}")
            return _one(registry, request, name, body)
        return 404, _error("not_found", f"no such route: {request.path}")
    except RegistryError as exc:
        return _CODE_STATUS.get(exc.code, 400), _error(exc.code, str(exc))


def _require(request: Request, *allowed: str) -> tuple[int, dict] | None:
    if request.method in allowed:
        return None
    return 405, _error("method_not_allowed",
                       f"{request.method} not allowed here; try {', '.join(allowed)}")


def _one(registry, request: Request, name: str, body: bytes) -> tuple[int, dict]:
    if request.method == "GET":
        # Creates on miss, deliberately: it is what makes "the registry always
        # answers" true, which is the property every caller is built on.
        return 200, registry.resolve(name).to_json()
    if request.method == "PUT":
        channel, group = _pair_from_body(body)
        return 200, registry.set(name, channel, group).to_json()
    if request.method == "DELETE":
        return 200, registry.clear(name).to_json()
    return _require(request, "GET", "PUT", "DELETE")


def _pair_from_body(body: bytes) -> tuple[int, int]:
    try:
        data = json.loads(body or b"{}")
    except ValueError as exc:
        raise RegistryError(f"body is not JSON: {exc}", code="bad_request") from None
    if not isinstance(data, dict):
        raise RegistryError('body must be an object, e.g. {"channel": 12, "group": 4}',
                            code="bad_request")
    if "channel" not in data or "group" not in data:
        raise RegistryError('body needs "channel" and "group", '
                            'e.g. {"channel": 12, "group": 4}', code="bad_request")
    try:
        channel, group = int(data["channel"]), int(data["group"])
    except (TypeError, ValueError):
        raise RegistryError("channel and group must be whole numbers",
                            code="bad_request") from None
    return validate_pair(channel, group)


def _status(registry) -> dict:
    return {"version": __version__, "names": len(registry.all()),
            "conflicts": len(registry.conflicts())}


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


class HttpApi:
    """Serves ``route()`` on a TCP port. Never fatal: a registry that will not
    start must not stop the daemon handing out boards."""

    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.cfg = daemon.cfg.registry
        self._server: asyncio.AbstractServer | None = None
        self.problem = ""

    @property
    def bind(self) -> str:
        return self.cfg.bind or self.daemon.cfg.server.bind

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            return self.cfg.port
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        if not self.cfg.enabled:
            log.debug("registry api disabled")
            return
        try:
            self._server = await asyncio.start_server(
                self._handle, self.bind, self.cfg.port)
        except OSError as exc:
            self.problem = f"{exc.strerror or exc} ({self.bind}:{self.cfg.port})"
            log.warning("registry api could not listen addr=%s:%s err=%r -- "
                        "boards are still served; `mbrelay connect <robot>` will "
                        "fall back to the derived address",
                        self.bind, self.cfg.port, exc)
            return
        log.info("registry api listening addr=%s:%d", self.bind, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def to_json(self) -> dict:
        return {"enabled": bool(self.cfg.enabled), "bind": self.bind,
                "port": self.port, "listening": self._server is not None,
                "problem": self.problem}

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            status, payload = await self._exchange(reader)
            writer.write(render(status, payload))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("registry request failed")
        finally:
            writer.close()

    async def _exchange(self, reader: asyncio.StreamReader) -> tuple[int, dict]:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError:
            return 431, _error("too_large", "request headers too large")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            raise
        if len(head) > MAX_HEAD:
            return 431, _error("too_large", "request headers too large")
        try:
            request = parse_head(head[:-4])
            body = await reader.readexactly(request.content_length)
        except BadRequest as exc:
            return exc.status, _error("bad_request", str(exc))
        except asyncio.IncompleteReadError:
            return 400, _error("bad_request", "body shorter than Content-Length")
        return route(self.daemon.registry, request, body)
