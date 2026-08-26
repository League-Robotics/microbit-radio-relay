"""Which boards exist, what they are, and whether they are free.

Keyed on the DAPLink UID, never on the device path: /dev/ttyACM* renumbers on
every replug, and the whole point of the uid is that it does not.

The one rule that shapes this module: **probing is destructive**. Opening a
board's port toggles DTR and reboots it. So a board is probed on first sight and
essentially never again -- identity is cached to disk, a FREE board already in
the cache is not re-probed, and a BUSY board is never probed at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .transport import PortInfo, PortScanner

log = logging.getLogger(__name__)

# Tolerant across both firmware families: RADIOBRIDGE prints the serial in
# decimal, the older RADIORELAY in hex. See docs/announce.md.
BANNER_RE = re.compile(rb"DEVICE:(RADIOBRIDGE|RADIORELAY):relay:([^:]+):([0-9A-Fa-f]+)")


class DeviceState(StrEnum):
    UNKNOWN = "unknown"          # on USB, never probed
    PROBING = "probing"          # a probe is in flight (which reboots the board)
    FREE = "free"                # relay firmware confirmed, idle, offerable
    ACQUIRING = "acquiring"
    BUSY = "busy"
    RELEASING = "releasing"
    NO_FIRMWARE = "no_firmware"  # probed, no banner came back
    FOREIGN = "foreign"          # answered, but not a role we can drive
    DISABLED = "disabled"        # deny-list, or an operator disabled it
    ERROR = "error"              # open/probe/release failed; backoff applies
    GONE = "gone"                # was known, now absent from USB


#: States in which the daemon will hand a board to a client.
OFFERABLE = frozenset({DeviceState.FREE})
#: States in which something already holds the port, so nothing else may open it.
#: PROBING belongs here: a probe has the tty open and is mid-reset.
IN_USE = frozenset({DeviceState.ACQUIRING, DeviceState.BUSY, DeviceState.RELEASING,
                    DeviceState.PROBING})

#: States a board settles into once we know what it is. Anything else is transient.
TERMINAL = frozenset({DeviceState.FREE, DeviceState.NO_FIRMWARE, DeviceState.FOREIGN,
                      DeviceState.DISABLED, DeviceState.ERROR, DeviceState.GONE})


@dataclass
class DeviceRecord:
    uid: str
    port: str | None = None
    state: DeviceState = DeviceState.UNKNOWN
    role: str = ""
    device_name: str = ""        # CODAL friendly name from the banner, e.g. "getez"
    nrf_serial: str = ""
    banner_raw: bytes = b""
    label: str = ""              # operator-assigned name from [devices.labels]
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_probe: float = 0.0
    session_id: str | None = None
    error_count: int = 0
    next_retry_at: float = 0.0
    sessions_total: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    last_error: str = ""
    disabled_reason: str = ""

    @property
    def short_uid(self) -> str:
        """A short but actually distinguishing slice of the DAPLink UID.

        DAPLink UIDs are 48 hex chars laid out as
        ``board(4) family(4) hic(8) unique(16) pad(8) hic(8)``. Both ends are
        shared by every board carrying the same interface chip -- on this bench
        all four micro:bits end in the identical ``000000006e052820`` -- so a
        tail slice names them all the same thing. The unique field is the middle.
        """
        return self.uid[16:24] if len(self.uid) >= 32 else self.uid[-8:]

    @property
    def name(self) -> str:
        """Best human handle: operator label, else CODAL name, else short uid."""
        return self.label or self.device_name or self.short_uid

    def matches(self, token: str) -> bool:
        """Does an operator-supplied token refer to this board?"""
        t = token.strip().lower()
        return t in {self.uid.lower(), (self.label or "").lower(),
                     (self.device_name or "").lower(), self.short_uid.lower()} - {""}

    def to_json(self) -> dict:
        return {
            "uid": self.uid, "port": self.port, "state": str(self.state),
            "name": self.name, "role": self.role, "device_name": self.device_name,
            "nrf_serial": self.nrf_serial, "label": self.label,
            "short_uid": self.short_uid,
            "first_seen": round(self.first_seen, 3), "last_seen": round(self.last_seen, 3),
            "last_probe": round(self.last_probe, 3), "session": self.session_id,
            "error_count": self.error_count, "sessions_total": self.sessions_total,
            "bytes_in": self.bytes_in, "bytes_out": self.bytes_out,
            "last_error": self.last_error,
        }

    # Only identity is cached; volatile state is always rediscovered at startup.
    _CACHED = ("role", "device_name", "nrf_serial", "label", "first_seen",
               "last_probe", "sessions_total")

    def cache_entry(self) -> dict:
        return {k: getattr(self, k) for k in self._CACHED}

    def apply_cache(self, data: dict) -> None:
        for key in self._CACHED:
            if key in data:
                setattr(self, key, data[key])


class Inventory:
    """The device registry.

    Every mutation happens in the event-loop thread, so there are no locks. That
    is deliberate: the FREE -> ACQUIRING flip in ``reserve`` has to be
    un-interleavable, and under asyncio a plain assignment already is.
    """

    def __init__(self, cfg, scanner: PortScanner, prober=None) -> None:
        self.cfg = cfg
        self._scanner = scanner
        self._prober = prober            # async (DeviceRecord) -> BannerInfo | None
        self.records: dict[str, DeviceRecord] = {}
        self.live_uids: set[str] = set()
        self._stop = asyncio.Event()
        self._scan_task: asyncio.Task | None = None
        self._probe_sem = asyncio.Semaphore(cfg.devices.max_concurrent_probes)
        self._probe_tasks: set[asyncio.Task] = set()
        self._cache_path = Path(cfg.state.dir) / "devices.json"
        self.on_device_gone = lambda rec: None   # set by SessionManager

    # -- cache -------------------------------------------------------------
    def load_cache(self) -> None:
        try:
            data = json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return
        for uid, entry in data.get("devices", {}).items():
            rec = self.records.setdefault(uid, DeviceRecord(uid=uid))
            rec.apply_cache(entry)
        log.debug("identity cache loaded entries=%d path=%s", len(data.get("devices", {})),
                  self._cache_path)

    def save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1,
                       "devices": {uid: rec.cache_entry() for uid, rec in self.records.items()
                                   if rec.device_name or rec.role}}
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            tmp.replace(self._cache_path)
        except OSError as exc:
            log.warning("could not write identity cache path=%s err=%r", self._cache_path, exc)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self.load_cache()
        for uid, label in self.cfg.devices.labels.items():
            self.records.setdefault(uid, DeviceRecord(uid=uid)).label = label
        await self.scan_once()
        self._scan_task = asyncio.create_task(self._scan_loop(), name="mbrelay-scan")

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._scan_task, *self._probe_tasks):
            if task is not None:
                task.cancel()
        if self._scan_task is not None:
            await asyncio.gather(self._scan_task, return_exceptions=True)
        self.save_cache()

    async def _scan_loop(self) -> None:
        interval = self.cfg.devices.scan_interval_ms / 1000
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                await self.scan_once()
            except Exception:
                log.exception("device scan failed")

    async def scan_once(self) -> None:
        # comports() hits sysfs/IOKit and takes 1-10ms; off-thread so a slow USB
        # stack cannot stall the pipe.
        live = await asyncio.to_thread(self._scanner.scan)
        self._reconcile(live)

    # -- reconciliation ----------------------------------------------------
    def _reconcile(self, live: dict[str, PortInfo]) -> None:
        now = time.time()
        self.live_uids = set(live)
        for uid, info in live.items():
            rec = self.records.get(uid)
            if rec is None:
                rec = self.records[uid] = DeviceRecord(uid=uid, port=info.device)
                rec.label = self.cfg.devices.labels.get(uid, "")
                log.info("device_added uid=%s port=%s", uid, info.device)
                self._classify_or_probe(rec)
            else:
                if rec.port != info.device:
                    log.info("device_port_changed uid=%s old=%s new=%s",
                             uid, rec.port, info.device)
                    rec.port = info.device      # the uid is the key; the path is data
                if rec.state in (DeviceState.GONE, DeviceState.UNKNOWN):
                    rec.state = DeviceState.UNKNOWN
                    self._classify_or_probe(rec)
                elif rec.state is DeviceState.ERROR and now >= rec.next_retry_at:
                    self._classify_or_probe(rec)
            rec.last_seen = now

        for uid, rec in self.records.items():
            if uid in live or rec.state is DeviceState.GONE:
                continue
            log.warning("device_removed uid=%s port=%s state=%s", uid, rec.port, rec.state)
            if rec.state in IN_USE:
                # The session's own read already errored; this just tidies the record.
                self.on_device_gone(rec)
            rec.state = DeviceState.GONE
            rec.port = None

    def _classify_or_probe(self, rec: DeviceRecord) -> None:
        """Apply allow/deny, then schedule a probe if we still need identity."""
        if self._denied(rec):
            rec.state = DeviceState.DISABLED
            rec.disabled_reason = rec.disabled_reason or "deny list"
            return
        if rec.role and rec.device_name:
            # Cached identity is enough. Do not reboot a board to learn what we
            # already know.
            rec.state = (DeviceState.FREE if rec.role in self.cfg.devices.allow_roles
                         else DeviceState.FOREIGN)
            return
        self._schedule_probe(rec)

    def _denied(self, rec: DeviceRecord) -> bool:
        allow, deny = self.cfg.devices.allow, self.cfg.devices.deny
        if any(rec.matches(tok) for tok in deny):
            return True
        return bool(allow) and not any(rec.matches(tok) for tok in allow)

    def _schedule_probe(self, rec: DeviceRecord, force: bool = False) -> None:
        if self._prober is None or rec.state in IN_USE:
            return                       # never probe a board a client is holding
        if not force and time.time() < rec.next_retry_at:
            return
        # Mark it PROBING *now*, synchronously, before the task even starts.
        #
        # The probe body waits on a concurrency semaphore, so on a host with more
        # boards than probe slots the queued ones would otherwise sit at UNKNOWN
        # and the next scan would schedule a second probe for the same board --
        # and a third, and so on. Every probe opens the port, which reboots the
        # board, so the duplicates reset boards out from under each other and
        # perfectly good relays came back as "no_firmware". Only visible with
        # three or more boards, which is exactly the deployed configuration.
        rec.state = DeviceState.PROBING
        task = asyncio.create_task(self._probe(rec),
                                   name=f"mbrelay-probe-{rec.short_uid}")
        self._probe_tasks.add(task)
        task.add_done_callback(self._probe_tasks.discard)

    async def _probe(self, rec: DeviceRecord) -> None:
        async with self._probe_sem:
            if rec.port is None:
                rec.state = DeviceState.GONE
                return
            try:
                banner = await self._prober(rec)
            except Exception as exc:
                self._probe_failed(rec, repr(exc))
                return
            rec.last_probe = time.time()
            if banner is None:
                rec.state = DeviceState.NO_FIRMWARE
                self.note_error(rec)
                log.info("device_probed uid=%s result=no_firmware port=%s", rec.uid, rec.port)
                return
            rec.role, rec.device_name = banner.role, banner.device_name
            rec.nrf_serial, rec.banner_raw = banner.serial, banner.raw
            rec.error_count = 0
            rec.next_retry_at = 0.0
            if rec.role in self.cfg.devices.allow_roles:
                rec.state = DeviceState.FREE
            else:
                rec.state = DeviceState.FOREIGN
            log.info("device_probed uid=%s name=%s role=%s state=%s port=%s",
                     rec.uid, rec.device_name, rec.role, rec.state, rec.port)
            self.save_cache()

    def _probe_failed(self, rec: DeviceRecord, err: str) -> None:
        rec.state = DeviceState.ERROR
        rec.last_error = err
        self.note_error(rec)
        log.warning("device_error uid=%s port=%s err=%s", rec.uid, rec.port, err)

    def note_error(self, rec: DeviceRecord, error: str = "") -> None:
        """Record that an operation on this board failed, and back off.

        A blank board, or a robot that is not a relay, must not be rebooted every
        scan cycle -- and probing is a reboot -- so the retry delay climbs to
        five minutes and stays there.
        """
        if error:
            rec.last_error = error
        schedule = self.cfg.devices.probe_backoff_ms or (5000,)
        idx = min(rec.error_count, len(schedule) - 1)
        rec.next_retry_at = time.time() + schedule[idx] / 1000
        rec.error_count += 1

    # -- allocation --------------------------------------------------------
    def free_devices(self) -> list[DeviceRecord]:
        return [r for r in self.records.values() if r.state in OFFERABLE]

    def reserve(self, rec: DeviceRecord) -> bool:
        """Claim a board for a session.

        Synchronous and pre-``await`` on purpose: this is the only thing standing
        between two simultaneous connects and a double-booked board.
        """
        if rec.state not in OFFERABLE:
            return False
        rec.state = DeviceState.ACQUIRING
        return True

    def find(self, token: str) -> DeviceRecord | None:
        for rec in self.records.values():
            if rec.matches(token):
                return rec
        return None

    def counts(self) -> dict[str, int]:
        # "busy" and "releasing" are reported separately: a board being handed
        # back is not held by anybody, and lumping the two together made the
        # pool-exhaustion message read as though a colleague had a board when
        # nobody did.
        out = {"total": 0, "free": 0, "busy": 0, "releasing": 0,
               "error": 0, "other": 0}
        for rec in self.records.values():
            if rec.state is DeviceState.GONE:
                continue
            out["total"] += 1
            if rec.state is DeviceState.FREE:
                out["free"] += 1
            elif rec.state in (DeviceState.ACQUIRING, DeviceState.BUSY):
                out["busy"] += 1
            elif rec.state is DeviceState.RELEASING:
                out["releasing"] += 1
            elif rec.state is DeviceState.ERROR:
                out["error"] += 1
            else:
                out["other"] += 1
        return out

    def rescan(self, force: bool = False) -> None:
        for rec in self.records.values():
            if rec.state in IN_USE or rec.port is None:
                continue             # never disturb a board in use
            if force:
                rec.role = rec.device_name = ""
                rec.error_count = 0
                rec.next_retry_at = 0.0
            self._classify_or_probe(rec)
