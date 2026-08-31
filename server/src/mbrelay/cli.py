"""``mbrelay`` -- run the daemon, inspect it, and flash boards.

One binary with subcommands, mirroring the sibling ``mbdeploy``, so config and
socket resolution are written once instead of drifting between two entry points.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from . import __version__
from .adminclient import AdminClient
from .config import load as load_config
from .errors import (EXIT_ERROR, EXIT_HARDWARE, EXIT_NO_DAEMON, EXIT_NO_DEVICE,
                     EXIT_OK, EXIT_USAGE, AdminError, ConfigError, DaemonNotRunning,
                     MbrelayError)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def _add_global_options(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """The options that mean the same thing for every subcommand.

    Added twice: once to the top-level parser, and once (via `parents=`) to every
    subparser. argparse will not accept a parent-parser option that appears AFTER
    the subcommand, so without the second copy `mbrelay serve --config X` fails
    with "unrecognized arguments" -- which is exactly how the systemd unit
    invokes it, and how anyone would naturally type it.

    The subparser copies use SUPPRESS so that an option the user did not give
    after the subcommand does not overwrite one they gave before it.
    """
    def default(value):
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("--config", metavar="PATH", default=default(None),
                        help="config file (skips the search path)")
    parser.add_argument("--socket", metavar="PATH", default=default(None),
                        help="admin socket path")
    parser.add_argument("--json", action="store_true", default=default(False),
                        help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="count", default=default(0),
                        help="more logging; repeat for debug")
    parser.add_argument("-q", "--quiet", action="store_true", default=default(False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mbrelay",
        description="Serve USB-attached micro:bit radio relays over TCP.")
    _add_global_options(p, suppress=False)
    p.add_argument("--version", action="version", version=f"mbrelay {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, suppress=True)

    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=lambda **kw:
                           argparse.ArgumentParser(parents=[common], **kw))

    s = sub.add_parser("serve", help="run the daemon in the foreground")
    s.add_argument("--bind", metavar="ADDR")
    s.add_argument("--port", type=int)
    s.add_argument("--log-level", choices=["debug", "info", "warning", "error"])
    s.add_argument("--log-format", choices=["text", "json"])
    s.set_defaults(func=cmd_serve)

    for name, help_text in (("devices", "list attached relays"), ("list", None)):
        d = sub.add_parser(name, help=help_text)
        d.add_argument("--refresh", action="store_true",
                       help="re-probe idle boards (never touches a busy one)")
        d.add_argument("--all", action="store_true", help="include departed boards")
        d.set_defaults(func=cmd_devices)

    st = sub.add_parser("status", help="daemon status and live sessions")
    st.add_argument("--watch", nargs="?", type=float, const=2.0, metavar="SEC")
    st.set_defaults(func=cmd_status)

    sub.add_parser("sessions", help="list live sessions").set_defaults(func=cmd_sessions)

    k = sub.add_parser("kick", help="terminate a session")
    k.add_argument("session", nargs="?")
    k.add_argument("--device", metavar="NAME")
    k.add_argument("--all", action="store_true")
    k.add_argument("--reason", default="operator")
    k.set_defaults(func=cmd_kick)

    r = sub.add_parser("reset", help="reset a board to factory defaults")
    r.add_argument("device", nargs="?")
    r.add_argument("--all", action="store_true")
    r.add_argument("--force", action="store_true", help="kick a session holding it")
    r.set_defaults(func=cmd_reset)

    for name, fn in (("disable", cmd_disable), ("enable", cmd_enable)):
        e = sub.add_parser(name, help=f"{name} a board")
        e.add_argument("device")
        e.add_argument("--reason", default="operator")
        e.set_defaults(func=fn)

    rs = sub.add_parser("rescan", help="re-scan USB now")
    rs.add_argument("--force", action="store_true", help="discard cached identity")
    rs.set_defaults(func=cmd_rescan)

    ev = sub.add_parser("events", help="stream daemon events")
    ev.add_argument("--follow", action="store_true", default=True)
    ev.set_defaults(func=cmd_events)

    sub.add_parser("ping", help="is the daemon up?").set_defaults(func=cmd_ping)

    f = sub.add_parser("flash", help="reflash relay firmware via mbdeploy")
    f.add_argument("device", nargs="?", help="board name or UID")
    f.add_argument("--all-relays", action="store_true", help="every attached board")
    f.add_argument("--hex", metavar="PATH")
    f.add_argument("--yes", "-y", action="store_true", help="skip the confirmation")
    f.set_defaults(func=cmd_flash)

    dc = sub.add_parser("discover", help="find relay hosts on the LAN")
    dc.add_argument("--timeout", type=float, default=1.5, metavar="SECONDS",
                    help="how long to listen for answers (default 1.5)")
    dc.add_argument("--probe", action="store_true",
                    help="also TCP-connect to each host and report whether it answers")
    dc.add_argument("--interface", action="append", default=[], metavar="ADDR",
                    help="query from this local address instead of letting the "
                         "kernel choose; repeatable")
    dc.add_argument("--service", metavar="TYPE", help="DNS-SD service type")
    dc.set_defaults(func=cmd_discover)

    c = sub.add_parser("connect", help="open a terminal on a served relay")
    # default=None, not the literal fallback: "omitted" has to be
    # distinguishable from "typed 127.0.0.1:8760", or discovery can never run.
    c.add_argument("target", nargs="?", default=None,
                   help="HOST[:PORT]; or a ROBOT name such as tovez, or ROBOT@HOST, "
                        "to be tuned to that robot with !N and dropped into the data "
                        "plane; omit it to find a relay host on the LAN")
    c.add_argument("--no-probe", action="store_true",
                   help="after tuning to a robot, do not PING it")
    c.add_argument("--send", action="append", default=[], metavar="LINE")
    c.add_argument("--expect", metavar="REGEX")
    c.add_argument("--timeout", type=float, default=10.0)
    c.add_argument("--raw", action="store_true", default=True)
    c.add_argument("--escape", default="]", metavar="CHAR")
    c.add_argument("--log", metavar="FILE")
    # --timeout is already the run_script budget, so the browse budget needs its
    # own name rather than a second meaning for one flag.
    c.add_argument("--discover", dest="discover", action="store_true", default=None,
                   help="find the host on the LAN; with a TARGET, treat it as the "
                        "advertised name to pick")
    c.add_argument("--no-discover", dest="discover", action="store_false",
                   help="never browse; fall straight back to 127.0.0.1:8760")
    c.add_argument("--discover-timeout", type=float, default=1.5, metavar="SECONDS")
    c.set_defaults(func=cmd_connect)

    cf = sub.add_parser("config", help="inspect the merged configuration")
    cf.add_argument("action", nargs="?", default="show", choices=["show"])
    cf.set_defaults(func=cmd_config)

    iu = sub.add_parser("install-unit", help="print a systemd unit and udev rule")
    iu.add_argument("--print", dest="do_print", action="store_true", default=True)
    iu.set_defaults(func=cmd_install_unit)

    return p


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _config(args):
    overrides = {}
    verbose = getattr(args, "verbose", 0) or 0
    for flag, dotted in (("bind", "server.bind"), ("port", "server.port"),
                         ("log_level", "log.level"), ("log_format", "log.format"),
                         ("socket", "admin.socket")):
        if (value := getattr(args, flag, None)) is not None:
            overrides[dotted] = value
    if verbose >= 2:
        overrides["log.level"] = "debug"
    elif verbose == 1:
        overrides.setdefault("log.level", "info")
    if getattr(args, "quiet", False):
        overrides["log.level"] = "error"
    return load_config(getattr(args, "config", None), overrides=overrides)


def _client(args) -> AdminClient:
    cfg = _config(args)
    path = (getattr(args, "socket", None) or os.environ.get("MBRELAY_SOCKET")
            or cfg.admin.socket)
    return AdminClient(path)


def _emit(args, payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))
    _ = args


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    out = [line, "  ".join("-" * widths[i] for i in range(len(headers)))]
    for row in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(out)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_serve(args) -> int:
    from .logs import setup as setup_logging
    from .server import Daemon

    cfg = _config(args)
    setup_logging(cfg.log.level, cfg.log.format)
    try:
        return asyncio.run(Daemon(cfg).run())
    except KeyboardInterrupt:
        return EXIT_OK
    except MbrelayError as exc:
        # Startup problems (port in use, socket path too long, another daemon
        # already running) are operator errors, not crashes. One line, not a
        # traceback -- this is what shows up in `journalctl -u mbrelay`.
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_ERROR


def cmd_devices(args) -> int:
    cfg = _config(args)
    try:
        with _client(args) as client:
            if args.refresh:
                client.call("rescan", force=False)
                time.sleep(1.5)
            result = client.call("list", all=args.all)
        rows = result["devices"]
    except DaemonNotRunning:
        # Work without the daemon, so you can see the hardware before starting it.
        rows = _local_scan(cfg)
        if not args.json:
            print("(daemon not running -- showing a direct USB scan)", file=sys.stderr)
    except AdminError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        _emit(args, {"devices": rows})
        return EXIT_OK
    if not rows:
        print("no micro:bits found")
        return EXIT_OK
    print(_table(
        [[r.get("name", "?"), r.get("state", "?"), r.get("role") or "-",
          r.get("port") or "-", r.get("session") or "-",
          r.get("short_uid") or r["uid"][16:24]] for r in rows],
        ["NAME", "STATE", "ROLE", "PORT", "SESSION", "UID"]))
    return EXIT_OK


def _local_scan(cfg) -> list[dict]:
    from .transport import scan_ports
    labels = cfg.devices.labels
    return [{"uid": uid, "port": info.device, "state": "unknown",
             "short_uid": uid[16:24],
             "name": labels.get(uid) or uid[16:24], "role": "", "session": None}
            for uid, info in sorted(scan_ports().items())]


def cmd_status(args) -> int:
    def once() -> dict:
        with _client(args) as client:
            return client.call("status")

    try:
        while True:
            status = once()
            if args.json:
                _emit(args, status)
                return EXIT_OK
            d = status["devices"]
            listener = status["listeners"][0]
            if args.watch:
                print("\033[2J\033[H", end="")
            print(f"mbrelay {status['version']}  pid {status['pid']}  "
                  f"up {status['uptime_s']:.0f}s")
            print(f"listening {listener['addr']}  "
                  f"accepted {listener['accepted']}  rejected {listener['rejected']}")
            if listener.get("advertised"):
                print(f"advertising {listener['advertised']}")
            print(f"devices: {d['total']} total, {d['free']} free, "
                  f"{d['busy']} busy, {d['error']} error")
            if status["sessions"]:
                print()
                print(_table(
                    [[s["id"], s["device_name"], s["peer"], s["plane"],
                      f"{s['age_s']:.0f}s", s["rx_bytes"], s["tx_bytes"]]
                     for s in status["sessions"]],
                    ["SESSION", "DEVICE", "PEER", "PLANE", "AGE", "RX", "TX"]))
            else:
                print("no active sessions")
            if not args.watch:
                return EXIT_OK
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return EXIT_OK
    except DaemonNotRunning as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_NO_DAEMON
    except AdminError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_ERROR


def cmd_sessions(args) -> int:
    return _simple(args, "sessions", lambda r: (
        _table([[s["id"], s["device_name"], s["peer"], s["plane"],
                 f"{s['age_s']:.0f}s", s["rx_bytes"], s["tx_bytes"]]
                for s in r["sessions"]],
               ["SESSION", "DEVICE", "PEER", "PLANE", "AGE", "RX", "TX"])
        if r["sessions"] else "no active sessions"))


def cmd_kick(args) -> int:
    return _simple(args, "kick",
                   lambda r: f"kicked: {', '.join(r['kicked']) or '(nothing)'}",
                   session=args.session, device=args.device,
                   all=args.all or None, reason=args.reason)


def cmd_reset(args) -> int:
    if not args.device and not args.all:
        print("mbrelay: reset needs a device name or --all", file=sys.stderr)
        return EXIT_USAGE
    return _simple(args, "reset",
                   lambda r: f"resetting: {', '.join(r['scheduled'])}",
                   device=args.device, all=args.all or None, force=args.force or None)


def cmd_disable(args) -> int:
    return _simple(args, "disable", lambda r: f"disabled {r['device']['name']}",
                   device=args.device, reason=args.reason)


def cmd_enable(args) -> int:
    return _simple(args, "enable", lambda r: f"enabled {r['device']['name']}",
                   device=args.device)


def cmd_rescan(args) -> int:
    return _simple(args, "rescan", lambda r: "rescan scheduled",
                   force=args.force or None)


def cmd_ping(args) -> int:
    try:
        with _client(args) as client:
            result = client.call("ping")
    except DaemonNotRunning:
        if not args.json:
            print("mbrelay: not running", file=sys.stderr)
        return EXIT_NO_DAEMON
    if args.json:
        _emit(args, result)
    else:
        print(f"mbrelay {result['version']}: running")
    return EXIT_OK


def cmd_events(args) -> int:
    try:
        with _client(args) as client:
            for event in client.events():
                if args.json:
                    print(json.dumps(event, default=str), flush=True)
                else:
                    ts = time.strftime("%H:%M:%S", time.localtime(event.get("ts", 0)))
                    print(f"{ts} {event.get('event')} {event.get('data')}", flush=True)
    except KeyboardInterrupt:
        return EXIT_OK
    except DaemonNotRunning as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_NO_DAEMON
    return EXIT_OK


def _simple(args, cmd: str, render, **kwargs) -> int:
    try:
        with _client(args) as client:
            result = client.call(cmd, **kwargs)
    except DaemonNotRunning as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_NO_DAEMON
    except AdminError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_NO_DEVICE if exc.code == "not_found" else EXIT_ERROR
    if args.json:
        _emit(args, result)
    else:
        print(render(result))
    return EXIT_OK


def cmd_flash(args) -> int:
    from .firmware import FlashError, Flasher
    from .transport import scan_ports

    cfg = _config(args)
    flasher = Flasher(cfg)
    try:
        flasher.check()
        hex_path = flasher.resolve_hex(args.hex)
        flasher.pyocd_cwd()
    except FlashError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_HARDWARE

    attached = scan_ports()
    if args.all_relays:
        targets = sorted(attached)
    elif args.device:
        targets = [uid for uid in attached
                   if args.device.lower() in (uid.lower(), uid[16:24].lower())]
        if not targets:
            targets = _resolve_by_name(args, attached)
        if not targets:
            print(f"mbrelay: no attached board matching {args.device!r}", file=sys.stderr)
            return EXIT_NO_DEVICE
    else:
        print("mbrelay: flash needs a device name/UID or --all-relays", file=sys.stderr)
        return EXIT_USAGE

    if not targets:
        print("mbrelay: no micro:bits attached", file=sys.stderr)
        return EXIT_NO_DEVICE

    print(f"Flashing {len(targets)} board(s) with {hex_path}")
    if not args.yes and sys.stdin.isatty():
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            return EXIT_OK

    # Take the boards out of rotation first, so the daemon does not open a port
    # mid-flash. Best-effort: flashing from a host with no daemon is normal.
    disabled = _quiesce(args, targets)
    try:
        flasher.probe()
        results = [flasher.deploy(uid, hex_path) for uid in targets]
    except FlashError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_HARDWARE
    finally:
        _unquiesce(args, disabled)

    for result in results:
        print(f"  {'ok  ' if result.ok else 'FAIL'} {result.short_uid} {result.message}")
    failed = [r for r in results if not r.ok]
    if args.json:
        _emit(args, {"results": [r.__dict__ for r in results]})
    return EXIT_HARDWARE if failed else EXIT_OK


def _resolve_by_name(args, attached: dict) -> list[str]:
    try:
        with _client(args) as client:
            rows = client.call("list", all=True)["devices"]
    except MbrelayError:
        return []
    return [r["uid"] for r in rows
            if r["uid"] in attached and args.device.lower() in
            {r.get("name", "").lower(), r.get("device_name", "").lower()}]


def _quiesce(args, uids: list[str]) -> list[str]:
    done = []
    try:
        with _client(args) as client:
            for uid in uids:
                try:
                    client.call("kick", device=uid, reason="flash")
                    client.call("disable", device=uid, reason="flashing")
                    done.append(uid)
                except AdminError:
                    pass
    except MbrelayError:
        pass
    return done


def _unquiesce(args, uids: list[str]) -> None:
    if not uids:
        return
    try:
        with _client(args) as client:
            for uid in uids:
                try:
                    client.call("enable", device=uid)
                except AdminError:
                    pass
            client.call("rescan", force=True)
    except MbrelayError:
        pass


DEFAULT_TARGET = "127.0.0.1:8760"


def cmd_discover(args) -> int:
    # Deferred like every other import that is not needed on every invocation --
    # and it is also what makes monkeypatching the browser in tests bite.
    from .mdns import browse_detailed, probe

    cfg = _config(args)
    # The only inspection command that turns logging on for itself. "Found
    # nothing" is the common outcome here and the reason for it -- a neighbour's
    # malformed datagram, a reply with somebody else's transaction id -- is only
    # visible at debug, so `mbrelay -vv discover` has to actually show it.
    if getattr(args, "verbose", 0):
        from .logs import setup as setup_logging
        setup_logging(cfg.log.level, cfg.log.format)

    result = browse_detailed(args.service or cfg.mdns.service, args.timeout,
                             interfaces=tuple(args.interface))
    live = {s.instance: probe(s) for s in result.services} if args.probe else {}

    if args.json:
        _emit(args, {
            "source": result.source,
            "elapsed_s": round(result.elapsed, 3),
            "problem": result.problem,
            "hosts": [dict({"name": s.instance, "host": s.hostname,
                            "addresses": list(s.addresses), "port": s.port,
                            "version": s.version, "txt": s.txt},
                           **({"live": live[s.instance]} if args.probe else {}))
                      for s in result.services],
        })
        return EXIT_OK

    if not result.services:
        # Finding nothing is not a failure -- naming a host still works, and
        # always did. Say why, the way `devices` explains its USB fallback.
        print(f"mbrelay: {result.problem}", file=sys.stderr)
        return EXIT_OK

    headers = ["NAME", "HOST", "ADDRESS", "PORT", "VERSION"]
    rows = [[s.instance, s.hostname or "-", s.addresses[0] if s.addresses else "-",
             s.port, s.version or "-"] for s in result.services]
    if args.probe:
        headers.append("LIVE")
        for row, service in zip(rows, result.services):
            row.append("yes" if live[service.instance] else "no")
    print(_table(rows, headers))
    return EXIT_OK


def _resolve_by_discovery(args, cfg, wanted: str | None = None):
    """Pick a relay host by mDNS. Returns "host:port", or an exit code.

    `wanted` is the advertised host name to insist on; by default the command
    line target. `mbrelay connect tovez` passes the robot's relay host (or
    None for "any"), since its target names a robot, not a host.
    """
    from .mdns import browse_detailed

    wanted = ((args.target if wanted is None else wanted) or "").strip().lower()
    # One answer is enough when any host will do, so stop as soon as it resolves.
    # When a particular name was asked for, sit out the full budget: returning
    # early would mean returning whichever host happened to answer first.
    result = browse_detailed(cfg.mdns.service, args.discover_timeout,
                             expect=0 if wanted else 1)
    hosts = [s for s in result.services
             if not wanted or wanted in (s.instance.lower(), s.hostname.lower())]

    if not hosts:
        if wanted:
            others = ", ".join(service.instance for service in result.services)
            detail = result.problem or (f"hosts that did answer: {others}"
                                        if others else "")
            print(f"mbrelay: no relay host named {args.target!r} answered."
                  f"{' ' + detail if detail else ''}", file=sys.stderr)
            return EXIT_USAGE
        print(f"mbrelay: {result.problem} Trying {DEFAULT_TARGET}.", file=sys.stderr)
        return DEFAULT_TARGET
    if len(hosts) == 1:
        # "#" is the relay's own comment convention, so this line is inert to
        # anything already reading the stream.
        print(f"# discovered {hosts[0].instance} at {hosts[0].endpoint}",
              file=sys.stderr)
        return hosts[0].endpoint
    return _pick_host(hosts)


def _pick_host(hosts):
    """The numbered picker, following the flash confirmation exactly.

    Summary first with plain print, prompt gated on isatty so a pipeline or CI
    can never block on it, and declining is success rather than an error.
    """
    print(f"{len(hosts)} relay hosts found:")
    for index, service in enumerate(hosts, 1):
        print(f"  {index}) {service.instance:<12} {service.endpoint:<24} "
              f"{service.version or '-'}")
    if not sys.stdin.isatty():
        print(f"mbrelay: several relay hosts found; name one, e.g. "
              f"'mbrelay connect {hosts[0].endpoint}'", file=sys.stderr)
        return EXIT_USAGE
    answer = input(f"Which? [1-{len(hosts)}, or q] ").strip()
    if not answer or answer[0].lower() == "q":
        return EXIT_OK
    if not answer.isdigit() or not 1 <= int(answer) <= len(hosts):
        print(f"mbrelay: {answer!r} is not one of 1-{len(hosts)}", file=sys.stderr)
        return EXIT_USAGE
    return hosts[int(answer) - 1].endpoint


def cmd_connect(args) -> int:
    from .client import (RobotTuneError, connect, interactive, parse_connect_target,
                         parse_target, run_script, tune_to_robot)

    try:
        want = parse_connect_target(args.target)
    except ValueError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # Which relay host: named on the command line (HOST or ROBOT@HOST), else
    # [client] target from the config, else browse the LAN, else localhost.
    cfg = _config(args)
    target = want.endpoint
    configured = cfg.client.target.strip()
    if args.discover or (args.discover is None and target is None and not configured):
        resolved = _resolve_by_discovery(args, cfg, wanted=want.host or "")
        if isinstance(resolved, int):       # an exit code, not a host
            return resolved
        target = resolved

    host, port = parse_target(target or configured or DEFAULT_TARGET)
    try:
        sock = connect(host, port)
    except OSError as exc:
        print(f"mbrelay: cannot connect to {host}:{port}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if want.robot:
        try:
            tune_to_robot(sock, want.robot, probe=not args.no_probe)
        except RobotTuneError as exc:
            sock.close()
            print(f"mbrelay: {exc}", file=sys.stderr)
            return EXIT_HARDWARE
    if args.send or args.expect:
        try:
            return run_script(sock, args.send, args.expect, args.timeout)
        finally:
            sock.close()
    return interactive(sock, escape=args.escape, raw=args.raw, log_path=args.log)


def cmd_config(args) -> int:
    cfg = _config(args)
    if args.json:
        _emit(args, {"config": cfg.as_dict(), "sources": cfg.sources})
        return EXIT_OK
    for section, values in cfg.as_dict().items():
        print(f"[{section}]")
        for key, value in values.items():
            source = cfg.sources.get(f"{section}.{key}", "default")
            print(f"  {key:<22} {value!r:<40} # {source}")
        print()
    return EXIT_OK


def cmd_install_unit(args) -> int:
    from .packaging_assets import AVAHI_SERVICE, SYSTEMD_UNIT, UDEV_RULE
    print("# ---- /etc/systemd/system/mbrelay.service ----")
    print(SYSTEMD_UNIT)
    print("# ---- /etc/udev/rules.d/99-microbit-relay.rules ----")
    print(UDEV_RULE)
    print("# ---- /etc/avahi/services/mbrelay.service ----")
    print("# Optional, and only if you set [mdns] enabled = false: the daemon")
    print("# already publishes this itself through avahi-publish.")
    print(AVAHI_SERVICE)
    return EXIT_OK


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, fallback in (("json", False), ("verbose", 0), ("quiet", False),
                           ("config", None), ("socket", None)):
        if not hasattr(args, name):
            setattr(args, name, fallback)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_OK
    except MbrelayError as exc:
        print(f"mbrelay: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
