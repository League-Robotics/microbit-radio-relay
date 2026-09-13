"""Reflashing boards, by wrapping the mbdeploy CLI.

Deliberately subprocess-only. mbdeploy lives in its own pipx venv on a different
Python, so it is not importable, and its one public entry point for flashing is
the command line anyway.

The daemon has **no runtime dependency** on any of this -- serving relays works
on a host where mbdeploy was never installed. Only ``mbrelay flash`` needs it,
and it fails with an install hint rather than a traceback.

Two things here are easy to get wrong and silently fatal:

* ``--force-relay`` is **required**. mbdeploy refuses to flash a board whose role
  contains RELAY or BRIDGE, which is every board we care about.
* pyocd reads ``pyocd.yaml`` from its **current working directory**. That file
  sets ``chip_erase: chip``; without it pyocd falls back to a sector erase, which
  fails on the nRF52833's MBR region at 0x0. So the subprocess cwd matters.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import MbrelayError

log = logging.getLogger(__name__)


class FlashError(MbrelayError):
    """A flash attempt failed, or the tooling to do it is missing."""


def _fetch(url: str, timeout: float) -> tuple[list[str], bytes]:
    """GET ``url``: (every redirect target, in order; the body)."""
    import urllib.request

    hops: list[str] = []

    class Recorder(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            hops.append(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    with urllib.request.build_opener(Recorder).open(url, timeout=timeout) as response:
        return hops, response.read()


@dataclass
class FlashResult:
    uid: str
    name: str
    ok: bool
    message: str = ""

    @property
    def short_uid(self) -> str:
        """The distinguishing slice of the DAPLink UID -- see DeviceRecord."""
        return self.uid[16:24] if len(self.uid) >= 32 else self.uid[-8:]


class Flasher:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.mbdeploy = cfg.firmware.mbdeploy or "mbdeploy"
        self.registry = Path(cfg.firmware.registry)
        self.target_mcu = cfg.firmware.target_mcu

    # -- preflight ---------------------------------------------------------
    def check(self) -> None:
        if shutil.which(self.mbdeploy) is None:
            raise FlashError(
                f"{self.mbdeploy!r} not found on PATH. Install it with:\n"
                "    pipx install pyocd\n"
                "    pipx install git+https://github.com/Busboombot/mbdeploy")

    def resolve_hex(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_file():
            raise FlashError(f"firmware image not found: {candidate}")
        return candidate.resolve()

    def resolve_source(self, hex_path: str | None = None, url: str | None = None, *,
                       say: Callable[[str], None] = print) -> Path:
        """The image to flash, as a local file.

        ``--hex``, else ``--url``, else ``firmware.hex`` (a path or a URL), else
        the latest GitHub release -- so a bare ``mbrelay flash`` always writes
        the newest published firmware rather than whatever file happens to be
        lying in the current directory.
        """
        if hex_path and url:
            raise FlashError("give --hex or --url, not both")
        if hex_path:
            return self.resolve_hex(hex_path)
        source = url or self.cfg.firmware.hex or self.cfg.firmware.release_url
        if source.startswith(("http://", "https://")):
            return self.download(source, say=say)
        return self.resolve_hex(source)

    def download(self, url: str, *, say: Callable[[str], None] = print,
                 timeout: float = 60.0) -> Path:
        """Fetch an image into a fresh temporary directory and check it is one.

        The check matters because a wrong URL on GitHub answers with an HTML
        page, and pyocd would otherwise be the first thing to notice.
        """
        import re
        import tempfile
        import urllib.error

        say(f"Downloading {url}")
        try:
            hops, data = _fetch(url, timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FlashError(f"could not download {url}: {exc}") from None
        if not data.lstrip().startswith(b":"):
            raise FlashError(f"{url} is not an Intel HEX image "
                             f"(it starts {data[:40]!r})")
        # releases/latest names no release; the redirect through
        # /releases/download/<tag>/ is the only place the answer shows. (The
        # final hop is a signed storage URL, useless to a person.)
        for hop in [url, *hops]:
            if match := re.search(r"/releases/download/([^/]+)/", hop):
                say(f"  release: {match.group(1)}")
                break
        path = Path(tempfile.mkdtemp(prefix="mbrelay-flash-")) / "MICROBIT.hex"
        path.write_bytes(data)
        say(f"  {len(data):,} bytes saved to {path}")
        return path

    def pyocd_cwd(self) -> Path:
        """Where to run pyocd so it finds pyocd.yaml (chip_erase: chip)."""
        configured = self.cfg.firmware.pyocd_config
        if configured:
            path = Path(configured)
            directory = path.parent if path.is_file() else path
            if not (directory / "pyocd.yaml").is_file():
                raise FlashError(
                    f"no pyocd.yaml in {directory}. It must contain 'chip_erase: chip' "
                    "-- without it pyocd sector-erases, which fails on the nRF52833 "
                    "MBR region at 0x0.")
            return directory
        return Path.cwd()

    # -- operations --------------------------------------------------------
    def probe(self) -> list[dict]:
        """Refresh mbdeploy's own registry.

        Mandatory before deploy: mbdeploy resolves targets only against this
        file, so a board it has never probed cannot be deployed to.
        """
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(["probe", "--config", str(self.registry)])
        if result.returncode != 0:
            raise FlashError(f"mbdeploy probe failed:\n{result.stderr.strip()}")
        return []

    def deploy(self, uid: str, hex_path: Path,
               on_line: Callable[[str], None] | None = None) -> FlashResult:
        """Flash one board, handing each line of mbdeploy's output to ``on_line``
        as it arrives -- a board takes a minute or more, and capturing it all
        until the end made a working flash look exactly like a hung one."""
        # Target by UID rather than by name: mbdeploy matches a 40-52 char hex
        # token straight against the uid field and skips the name lookup, which
        # depends on a probe having populated the registry correctly.
        tail: list[str] = []

        def seen(line: str) -> None:
            tail.append(line)
            del tail[:-20]
            if on_line is not None:
                on_line(line)

        code = self._stream([
            "deploy", uid,
            "--hex", str(hex_path),
            "--force-relay",
            "--target-mcu", self.target_mcu,
            "--config", str(self.registry),
        ], seen, timeout=300)
        message = next((line.strip() for line in reversed(tail) if line.strip()), "")
        return FlashResult(uid=uid, name=uid[16:24] if len(uid) >= 32 else uid[-8:],
                           ok=code == 0, message=message)

    def _stream(self, args: list[str], on_line: Callable[[str], None],
                timeout: float) -> int:
        cwd = self.pyocd_cwd()
        cmd = [self.mbdeploy, *args]
        log.debug("running %s (cwd=%s)", " ".join(cmd), cwd)
        try:
            # stderr folded in: pyocd's progress does not stay on one stream.
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as exc:
            raise FlashError(f"could not run {self.mbdeploy}: {exc}") from exc
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, expire)
        timer.start()
        try:
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
            code = proc.wait()
        finally:
            timer.cancel()
        if expired.is_set():
            raise FlashError(f"mbdeploy timed out after {timeout:.0f}s: {' '.join(cmd)}")
        return code

    def _run(self, args: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
        cwd = self.pyocd_cwd()
        cmd = [self.mbdeploy, *args]
        log.debug("running %s (cwd=%s)", " ".join(cmd), cwd)
        try:
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise FlashError(f"mbdeploy timed out after {timeout}s: {' '.join(cmd)}") from exc
        except OSError as exc:
            raise FlashError(f"could not run {self.mbdeploy}: {exc}") from exc
