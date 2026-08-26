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
from dataclasses import dataclass
from pathlib import Path

from .errors import MbrelayError

log = logging.getLogger(__name__)


class FlashError(MbrelayError):
    """A flash attempt failed, or the tooling to do it is missing."""


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

    def resolve_hex(self, override: str | None = None) -> Path:
        candidate = Path(override or self.cfg.firmware.hex or "MICROBIT.hex")
        if not candidate.is_file():
            raise FlashError(f"firmware image not found: {candidate}")
        return candidate.resolve()

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

    def deploy(self, uid: str, hex_path: Path) -> FlashResult:
        # Target by UID rather than by name: mbdeploy matches a 40-52 char hex
        # token straight against the uid field and skips the name lookup, which
        # depends on a probe having populated the registry correctly.
        result = self._run([
            "deploy", uid,
            "--hex", str(hex_path),
            "--force-relay",
            "--target-mcu", self.target_mcu,
            "--config", str(self.registry),
        ], timeout=300)
        ok = result.returncode == 0
        message = (result.stderr or result.stdout or "").strip().splitlines()
        return FlashResult(uid=uid, name=uid[16:24] if len(uid) >= 32 else uid[-8:],
                           ok=ok, message=message[-1] if message else "")

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
