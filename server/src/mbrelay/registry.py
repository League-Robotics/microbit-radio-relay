"""Where a robot actually is, as opposed to where its name says it should be.

A micro:bit's five-letter name is its ``DEVICEID[1]`` in base 5, so
``mbrelay.naming`` turns any name into a ``(channel, group)`` with no
coordination at all. That is a wonderful property and it is not enough: the
mapping has 3125 names but only 25 channels, so 125 names share each one, and
when two robots collide there is nothing to be done about it -- the address is
a property of the silicon.

So the derived pair is a **default**, and this is the thing that can override
it. Three layers, highest first::

    [registry.names] in mbrelay.toml   source "config"    pinned; survey output
    <state.dir>/names.json             source "registry"  set through the API
    naming.name_to_radio(name)         source "derived"   computed, then stored

**A lookup always answers.** A well-formed name nobody has ever mentioned is
resolved from the mapping and a record is written, so `GET /names` really is
the list of every robot this relay knows about. Only a *malformed* name is an
error -- the spec is explicit that refusing an unknown-but-well-formed name
breaks the tune-to-whatever-I-name model that makes the whole thing usable.

**Collisions are reported, not refused.** Two names may hold one pair; a survey
is allowed to make a mess halfway through, and an operator moving robots by
hand needs to see the clash rather than be stopped by it. That is the same
posture the daemon already takes for two sessions sharing a channel.

**No security, deliberately.** This is an internal service on a lab LAN; see
``httpapi.py``.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from . import naming
from .errors import RegistryError

log = logging.getLogger(__name__)

#: What `!CG <ch> <group>` accepts (RadioRelay.cpp). A pair outside this is not
#: merely unusual, it is one the board will refuse, so it is rejected up front
#: rather than at tune time on a bench somewhere.
CHANNEL_MIN, CHANNEL_MAX = 0, 83
GROUP_MIN, GROUP_MAX = 0, 255

CONFIG, REGISTRY, DERIVED = "config", "registry", "derived"


@dataclass(frozen=True)
class Entry:
    name: str
    channel: int
    group: int
    source: str                 # CONFIG | REGISTRY | DERIVED
    updated: float = 0.0

    @property
    def derived(self) -> bool:
        """Is this still the address the name itself gives?"""
        return (self.channel, self.group) == naming.name_to_radio(self.name)

    def to_json(self) -> dict:
        return {"name": self.name, "channel": self.channel, "group": self.group,
                "source": self.source, "updated": round(self.updated, 3),
                "derived": self.derived}


def parse_pair(text: str, where: str = "pin") -> tuple[int, int]:
    """``"12/4"`` -> ``(12, 4)``. The one accepted spelling, so a config file
    and an API body cannot disagree about what a pair looks like."""
    channel, sep, group = text.partition("/")
    if not sep:
        raise RegistryError(f"{where}: expected <channel>/<group>, got {text!r}",
                            code="bad_request")
    try:
        return validate_pair(int(channel), int(group), where)
    except ValueError:
        raise RegistryError(f"{where}: expected <channel>/<group>, got {text!r}",
                            code="bad_request") from None


def validate_pair(channel: int, group: int, where: str = "pair") -> tuple[int, int]:
    if not CHANNEL_MIN <= channel <= CHANNEL_MAX:
        raise RegistryError(
            f"{where}: channel {channel} is outside {CHANNEL_MIN}-{CHANNEL_MAX}, "
            "which is what the relay firmware accepts", code="bad_request")
    if not GROUP_MIN <= group <= GROUP_MAX:
        raise RegistryError(
            f"{where}: group {group} is outside {GROUP_MIN}-{GROUP_MAX}, "
            "which is what the relay firmware accepts", code="bad_request")
    return channel, group


def validate_name(name: str) -> str:
    """Normalize and require a real micro:bit name.

    Unknown is fine, malformed is not: `pipip` is a legal address nobody is on,
    while `robot1` has no address at all and would otherwise produce a
    working-looking link on an arbitrary channel.
    """
    try:
        return naming.validate(name)
    except ValueError:
        raise RegistryError(
            f"not a micro:bit name: {name!r} (five letters, e.g. tovez)",
            code="bad_request") from None


class NameRegistry:
    """name -> (channel, group), with the derived pair as the fallback."""

    VERSION = 1

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.path = Path(cfg.state.dir) / "names.json"
        #: name -> (channel, group), from [registry.names]. Never written back.
        self.pins: dict[str, tuple[int, int]] = {}
        #: name -> (channel, group, explicit, updated), from names.json
        self._learned: dict[str, tuple[int, int, bool, float]] = {}
        self._load_pins()

    def _load_pins(self) -> None:
        for name, text in (self.cfg.registry.names or {}).items():
            pinned = validate_name(name)
            self.pins[pinned] = parse_pair(text, where=f"[registry.names] {name}")

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for name, entry in (data.get("names") or {}).items():
            try:
                channel, group = validate_pair(int(entry["channel"]), int(entry["group"]))
                self._learned[validate_name(name)] = (
                    channel, group, bool(entry.get("explicit")),
                    float(entry.get("updated") or 0.0))
            except (KeyError, TypeError, ValueError, RegistryError):
                # A hand-edited or half-written file must not stop the daemon
                # serving boards; the bad row is dropped and re-derived.
                log.warning("registry: ignoring unusable entry name=%r", name)
        log.debug("registry loaded entries=%d path=%s", len(self._learned), self.path)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": self.VERSION, "names": {
                name: {"channel": ch, "group": grp, "explicit": explicit,
                       "updated": round(updated, 3)}
                for name, (ch, grp, explicit, updated) in self._learned.items()}}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("could not write registry path=%s err=%r", self.path, exc)

    # -- lookup ------------------------------------------------------------
    def resolve(self, name: str) -> Entry:
        """Where is this robot? Always answers for a well-formed name, and
        records the answer so `all()` lists every robot ever asked about."""
        name = validate_name(name)
        if name in self.pins:
            channel, group = self.pins[name]
            return Entry(name, channel, group, CONFIG)
        if name in self._learned:
            channel, group, explicit, updated = self._learned[name]
            return Entry(name, channel, group, REGISTRY if explicit else DERIVED, updated)
        channel, group = naming.name_to_radio(name)
        self._learned[name] = (channel, group, False, time.time())
        self.save()
        log.info("registry_derived name=%s channel=%d group=%d", name, channel, group)
        return Entry(name, channel, group, DERIVED, self._learned[name][3])

    def get(self, name: str) -> Entry | None:
        """Like resolve(), but does not create. For callers that want to know
        whether a name is already on record."""
        name = validate_name(name)
        if name in self.pins:
            return Entry(name, *self.pins[name], CONFIG)
        if name in self._learned:
            channel, group, explicit, updated = self._learned[name]
            return Entry(name, channel, group, REGISTRY if explicit else DERIVED, updated)
        return None

    def all(self) -> list[Entry]:
        names = set(self.pins) | set(self._learned)
        return sorted((self.get(n) for n in names), key=lambda e: e.name)

    def name_for(self, channel: int, group: int) -> str | None:
        """Which robot is on this link? The inverse of resolve().

        Explicit assignments win. Failing those the derived bijection answers,
        because a name maps to its own pair for free -- but NOT when that name
        has been moved somewhere else, since the address it vacated no longer
        belongs to it.
        """
        for name, (ch, grp) in self.pins.items():
            if (ch, grp) == (channel, group):
                return name
        for name, (ch, grp, explicit, _) in self._learned.items():
            if explicit and (ch, grp) == (channel, group):
                return name
        try:
            name = naming.radio_to_name(channel, group)
        except ValueError:
            return None
        moved = self.get(name)
        if moved is not None and (moved.channel, moved.group) != (channel, group):
            return None
        return name

    def listing(self) -> dict:
        """The whole registry as the API and the CLI both report it.

        One function rather than two, because the first version annotated
        conflicts only on the HTTP path and `mbrelay names` quietly showed every
        clash as "-".
        """
        conflicts = self.conflicts()
        rows = []
        for entry in self.all():
            row = entry.to_json()
            clash = conflicts.get((entry.channel, entry.group))
            if clash:
                row["conflict"] = [n for n in clash if n != entry.name]
            rows.append(row)
        return {"names": rows,
                "conflicts": [{"channel": ch, "group": grp, "names": names}
                              for (ch, grp), names in sorted(conflicts.items())]}

    def conflicts(self) -> dict[tuple[int, int], list[str]]:
        """Pairs held by more than one name. Reported, never enforced."""
        seen: dict[tuple[int, int], list[str]] = {}
        for entry in self.all():
            seen.setdefault((entry.channel, entry.group), []).append(entry.name)
        return {pair: names for pair, names in seen.items() if len(names) > 1}

    # -- assignment --------------------------------------------------------
    def set(self, name: str, channel: int, group: int) -> Entry:
        name = validate_name(name)
        channel, group = validate_pair(channel, group, where=name)
        if name in self.pins:
            raise RegistryError(
                f"{name} is pinned to {self.pins[name][0]}/{self.pins[name][1]} in "
                "[registry.names]; edit the config file and restart the daemon",
                code="pinned")
        self._learned[name] = (channel, group, True, time.time())
        self.save()
        log.info("registry_set name=%s channel=%d group=%d", name, channel, group)
        return Entry(name, channel, group, REGISTRY, self._learned[name][3])

    def clear(self, name: str) -> Entry:
        """Drop an override; the name goes back to its derived address."""
        name = validate_name(name)
        if name in self.pins:
            raise RegistryError(
                f"{name} is pinned in [registry.names]; edit the config file "
                "and restart the daemon", code="pinned")
        self._learned.pop(name, None)
        self.save()
        log.info("registry_cleared name=%s", name)
        return self.resolve(name)
