"""Synchronous client for the admin socket, used by every CLI subcommand.

Synchronous on purpose: the CLI does one request and prints one table. Dragging
an event loop into that would buy nothing and would make ``mbrelay status``
harder to read.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Iterator

from .errors import AdminError, DaemonNotRunning


class AdminClient:
    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 1

    def __enter__(self) -> "AdminClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            sock.close()
            raise DaemonNotRunning(self.path) from exc
        except OSError as exc:
            sock.close()
            raise AdminError(f"cannot reach {self.path}: {exc}") from exc
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def call(self, cmd: str, **args: Any) -> dict:
        """One request, one response. Raises AdminError on a refusal."""
        if self._sock is None:
            self.connect()
        req_id, self._next_id = self._next_id, self._next_id + 1
        payload = {"id": req_id, "cmd": cmd}
        if args:
            payload["args"] = {k: v for k, v in args.items() if v is not None}
        self._sock.sendall(json.dumps(payload).encode() + b"\n")   # type: ignore[union-attr]
        response = self._readline()
        if not response.get("ok"):
            error = response.get("error") or {}
            raise AdminError(error.get("message", "admin request failed"),
                             code=error.get("code", "error"))
        return response.get("result") or {}

    def events(self) -> Iterator[dict]:
        """Subscribe to the event stream. Yields until the socket closes."""
        if self._sock is None:
            self.connect()
        self._sock.settimeout(None)                                # type: ignore[union-attr]
        req_id, self._next_id = self._next_id, self._next_id + 1
        self._sock.sendall(                                        # type: ignore[union-attr]
            json.dumps({"id": req_id, "cmd": "events"}).encode() + b"\n")
        self._readline()          # the {"stream": true} acknowledgement
        while True:
            try:
                yield self._readline()
            except AdminError:
                return

    def _readline(self) -> dict:
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(65536)                     # type: ignore[union-attr]
            except socket.timeout as exc:
                raise AdminError("timed out waiting for the daemon") from exc
            if not chunk:
                raise AdminError("daemon closed the connection")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise AdminError(f"bad response from daemon: {line[:120]!r}") from exc
