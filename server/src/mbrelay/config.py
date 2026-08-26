"""Configuration: layered TOML, env, and CLI flags, merged into a frozen dataclass.

TOML because ``tomllib`` is stdlib on 3.11+, so it costs no dependency, and
because ops files need comments.

Precedence, lowest to highest::

    DEFAULTS
    /etc/mbrelay/mbrelay.toml
    /etc/mbrelay/conf.d/*.toml      (sorted; lets Ansible drop fragments)
    $XDG_CONFIG_HOME/mbrelay/mbrelay.toml
    ./mbrelay.toml                  (dev convenience)
    MBRELAY_* environment
    CLI flags

Every layer merges key-by-key, and ``sources()`` reports which layer won each
key -- that is what ``mbrelay config show`` prints.

Unknown keys are a hard error. A typo in an ops config file is the most common
way a service comes up subtly wrong, and it must not fail open.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

SYSTEM_CONFIG = Path("/etc/mbrelay/mbrelay.toml")
SYSTEM_CONF_D = Path("/etc/mbrelay/conf.d")
LOCAL_CONFIG = Path("mbrelay.toml")


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "0.0.0.0"
    port: int = 8760
    backlog: int = 16
    tcp_nodelay: bool = True
    keepalive: bool = True
    keepalive_idle: int = 15
    keepalive_interval: int = 5
    keepalive_count: int = 3

    # Sent verbatim (plus CRLF) when no relay is free, then the socket is closed.
    # The "#" prefix is the relay's own comment convention, so a client that
    # already skips "#" lines is unaffected. "{total}" and "{busy}" are filled in.
    # Set to "" to send zero bytes and abort() instead.
    reject_message: str = "# ERROR: no relay available ({total} devices, {busy} busy)"

    # 0 honours "reject immediately if none free" literally. Test harnesses that
    # reconnect straight after disconnecting want ~8000 -- see the release window
    # note in docs/relay-server.md.
    acquire_wait_ms: int = 0
    acquire_retries: int = 2

    # banner: replay the board's announcement, as a direct serial open would show
    # none:   send nothing; the client sends HELLO itself
    # raw:    forward every setup byte, normalize echoes included (debug only)
    preamble: str = "banner"


@dataclass(frozen=True)
class SerialConfig:
    baud: int = 115200
    open_settle_ms: int = 300
    hello_timeout_ms: int = 2000
    hello_attempts: int = 4
    post_close_settle_ms: int = 500
    acquire_budget_ms: int = 12000
    release_budget_ms: int = 15000
    write_high_water: int = 65536
    write_low_water: int = 16384


@dataclass(frozen=True)
class DevicesConfig:
    allow: tuple[str, ...] = ()          # UIDs or names; empty means "all relays"
    deny: tuple[str, ...] = ()
    # RADIORELAY (the old MakeCode firmware) is excluded by default: it does not
    # accept "!ECHO ON"/"!MODE", so the normalize sequence cannot be verified on it.
    allow_roles: tuple[str, ...] = ("RADIOBRIDGE",)
    scan_interval_ms: int = 2000
    max_concurrent_probes: int = 2
    probe_backoff_ms: tuple[int, ...] = (5000, 15000, 60000, 300000)
    labels: dict[str, str] = field(default_factory=dict)   # uid -> friendly label


@dataclass(frozen=True)
class SessionConfig:
    # All off by default. A receive-only client listening for radio telemetry
    # legitimately sends nothing forever, so a send-based timer is wrong here.
    # TCP keepalive plus "mbrelay kick" are the real backstops.
    bind_idle_timeout_s: int = 0
    idle_timeout_s: int = 0
    max_seconds: int = 0


@dataclass(frozen=True)
class AdminConfig:
    socket: str = ""            # "" means "work it out from the environment"
    socket_mode: str = "0660"
    socket_group: str = ""
    allow_shutdown: bool = False


@dataclass(frozen=True)
class FirmwareConfig:
    hex: str = ""
    mbdeploy: str = "mbdeploy"
    # mbdeploy's OWN device registry. Deliberately NOT <state.dir>/devices.json:
    # that is mbrelay's identity cache, and the two formats are different --
    # mbrelay writes {"version": 1, "devices": {...}} while mbdeploy expects a
    # flat {uid: {...}} map. Sharing the path makes mbdeploy crash on the
    # "version" key with "argument of type 'int' is not iterable".
    registry: str = ""          # defaults to <state.dir>/mbdeploy-registry.json
    # Directory containing pyocd.yaml (chip_erase: chip). mbdeploy shells out to
    # pyocd, which reads that file from its cwd -- get it wrong and pyocd
    # silently falls back to sector erase, which fails on the nRF52833 MBR at 0x0.
    pyocd_config: str = ""
    target_mcu: str = "nrf52833"


@dataclass(frozen=True)
class LogConfig:
    level: str = "info"
    format: str = "text"        # text | json
    # Data-plane bytes are user payload. Tracing them is loud and a privacy
    # question, so it is off and separate from the log level.
    serial_trace: bool = False
    trace_bytes: int = 0


@dataclass(frozen=True)
class StateConfig:
    dir: str = ""               # identity cache lives here as devices.json
    shutdown_grace_s: int = 20  # systemd TimeoutStopSec must exceed this


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    devices: DevicesConfig = field(default_factory=DevicesConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    firmware: FirmwareConfig = field(default_factory=FirmwareConfig)
    log: LogConfig = field(default_factory=LogConfig)
    state: StateConfig = field(default_factory=StateConfig)

    # Populated by load(): "server.port" -> "/etc/mbrelay/mbrelay.toml"
    sources: dict[str, str] = field(default_factory=dict, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {f.name: _section_dict(getattr(self, f.name))
                for f in fields(self) if is_dataclass(getattr(self, f.name))}


def _section_dict(section) -> dict[str, Any]:
    out = {}
    for f in fields(section):
        value = getattr(section, f.name)
        out[f.name] = list(value) if isinstance(value, tuple) else value
    return out


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
_SECTIONS = {
    "server": ServerConfig, "serial": SerialConfig, "devices": DevicesConfig,
    "session": SessionConfig, "admin": AdminConfig, "firmware": FirmwareConfig,
    "log": LogConfig, "state": StateConfig,
}

# Env var name -> (section, key). Only the knobs an operator plausibly overrides
# from a unit file; everything else belongs in the config file.
_ENV = {
    "MBRELAY_BIND": ("server", "bind"),
    "MBRELAY_PORT": ("server", "port"),
    "MBRELAY_LOG_LEVEL": ("log", "level"),
    "MBRELAY_LOG_FORMAT": ("log", "format"),
    "MBRELAY_SOCKET": ("admin", "socket"),
    "MBRELAY_STATE_DIR": ("state", "dir"),
    "MBRELAY_HEX": ("firmware", "hex"),
}


def candidate_paths(explicit: str | os.PathLike | None = None) -> list[Path]:
    """The config files that would be read, in precedence order."""
    if explicit:
        return [Path(explicit)]
    paths = [SYSTEM_CONFIG]
    if SYSTEM_CONF_D.is_dir():
        paths.extend(sorted(SYSTEM_CONF_D.glob("*.toml")))
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    paths.append(base / "mbrelay" / "mbrelay.toml")
    paths.append(LOCAL_CONFIG)
    return paths


def load(explicit: str | os.PathLike | None = None,
         overrides: dict[str, Any] | None = None,
         environ: dict[str, str] | None = None) -> Config:
    """Merge every layer and build the frozen Config.

    ``overrides`` is the CLI layer, flattened as {"server.port": 9000}. Values
    that are None are ignored, so an unset argparse flag does not clobber a file.
    """
    environ = os.environ if environ is None else environ
    merged: dict[str, dict[str, Any]] = {name: {} for name in _SECTIONS}
    sources: dict[str, str] = {}

    if explicit and not Path(explicit).is_file():
        raise ConfigError(f"config file not found: {explicit}")

    for path in candidate_paths(explicit):
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        _merge(merged, sources, data, str(path), origin=path)

    env_layer: dict[str, dict[str, Any]] = {}
    for var, (section, key) in _ENV.items():
        if var in environ:
            env_layer.setdefault(section, {})[key] = environ[var]
    if env_layer:
        _merge(merged, sources, env_layer, "environment")

    if overrides:
        flat: dict[str, dict[str, Any]] = {}
        for dotted, value in overrides.items():
            if value is None:
                continue
            section, _, key = dotted.partition(".")
            if section not in _SECTIONS:
                raise ConfigError(f"unknown config section in override: {dotted}")
            flat.setdefault(section, {})[key] = value
        if flat:
            _merge(merged, sources, flat, "command line")

    built = {name: _build(cls, merged[name], name) for name, cls in _SECTIONS.items()}
    cfg = Config(**built, sources=sources)
    return _apply_derived_defaults(cfg, sources)


def _merge(merged, sources, data: dict, label: str, origin: Path | None = None) -> None:
    for section, values in data.items():
        if section not in _SECTIONS:
            raise ConfigError(
                f"{label}: unknown config section [{section}] "
                f"(known: {', '.join(sorted(_SECTIONS))})")
        if not isinstance(values, dict):
            raise ConfigError(f"{label}: [{section}] must be a table")
        valid = {f.name for f in fields(_SECTIONS[section])}
        for key, value in values.items():
            if key not in valid:
                raise ConfigError(
                    f"{label}: unknown key '{key}' in [{section}] "
                    f"(known: {', '.join(sorted(valid))})")
            merged[section][key] = value
            sources[f"{section}.{key}"] = label
    _ = origin


def _build(cls, values: dict[str, Any], section: str):
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in values:
            continue
        kwargs[f.name] = _coerce(f.type, values[f.name], f"{section}.{f.name}")
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"[{section}]: {exc}") from exc


def _coerce(declared, value, where: str):
    """Coerce a TOML/env value to the declared field type.

    Env vars arrive as strings, so ints and bools need parsing; TOML already has
    real types and passes straight through.
    """
    name = declared if isinstance(declared, str) else getattr(declared, "__name__", "")
    if name.startswith("tuple"):
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{where}: expected a list, got {type(value).__name__}")
        return tuple(value)
    if name.startswith("dict"):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a table, got {type(value).__name__}")
        return dict(value)
    if name == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() in ("1", "true", "yes", "on"):
                return True
            if value.lower() in ("0", "false", "no", "off"):
                return False
        raise ConfigError(f"{where}: expected a boolean, got {value!r}")
    if name == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: expected an integer, got {value!r}") from exc
    if name == "str":
        return str(value)
    return value


def _apply_derived_defaults(cfg: Config, sources: dict[str, str]) -> Config:
    """Fill in the paths that depend on the environment rather than on a constant."""
    import dataclasses

    state_dir = cfg.state.dir or _default_state_dir()
    admin_sock = cfg.admin.socket or _default_socket_path()
    registry = cfg.firmware.registry or str(Path(state_dir) / "mbdeploy-registry.json")

    for dotted, value in (("state.dir", state_dir), ("admin.socket", admin_sock),
                          ("firmware.registry", registry)):
        sources.setdefault(dotted, "derived default")
        _ = value

    return dataclasses.replace(
        cfg,
        state=dataclasses.replace(cfg.state, dir=state_dir),
        admin=dataclasses.replace(cfg.admin, socket=admin_sock),
        firmware=dataclasses.replace(cfg.firmware, registry=registry),
    )


def _default_state_dir() -> str:
    # systemd StateDirectory=mbrelay sets this, and it is the right answer on a
    # fleet node. Everywhere else, fall back to the XDG state dir.
    if sd := os.environ.get("STATE_DIRECTORY"):
        return sd.split(":")[0]
    if os.access("/var/lib", os.W_OK):
        return "/var/lib/mbrelay"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return str(Path(base) / "mbrelay")


def _default_socket_path() -> str:
    # RuntimeDirectory=mbrelay under systemd; XDG_RUNTIME_DIR for a logged-in
    # user; otherwise the state dir, which always exists somewhere writable.
    if rd := os.environ.get("RUNTIME_DIRECTORY"):
        return str(Path(rd.split(":")[0]) / "mbrelay.sock")
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return str(Path(xdg) / "mbrelay" / "mbrelay.sock")
    if os.path.isdir("/run") and os.access("/run", os.W_OK):
        return "/run/mbrelay/mbrelay.sock"
    return str(Path(_default_state_dir()) / "mbrelay.sock")
