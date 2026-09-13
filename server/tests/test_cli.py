"""CLI wiring, exit codes, and the exact mbdeploy invocation `flash` builds."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mbrelay.cli import build_parser, main
from mbrelay.errors import (EXIT_ERROR, EXIT_HARDWARE, EXIT_NO_DAEMON,
                            EXIT_NO_DEVICE, EXIT_OK, EXIT_USAGE)
from mbrelay.firmware import FlashError, Flasher


# -- parser -----------------------------------------------------------------
def test_every_documented_subcommand_parses():
    parser = build_parser()
    for argv in (["serve"], ["devices"], ["list"], ["status"], ["sessions"],
                 ["kick", "s-1"], ["reset", "vevov"], ["disable", "x"], ["enable", "x"],
                 ["rescan"], ["events"], ["ping"], ["flash", "--all-relays"],
                 ["connect"], ["discover"], ["config", "show"], ["install-unit"],
                 ["devices", "--remote"], ["devices", "torture"], ["list", "--remote"]):
        assert parser.parse_args(argv).func is not None


@pytest.mark.parametrize("argv", [
    ["--socket", "/nope.sock", "ping"],      # globals before the subcommand
    ["ping", "--socket", "/nope.sock"],      # and after it
])
def test_global_options_work_on_either_side_of_the_subcommand(argv):
    """The systemd unit runs `mbrelay serve --config ...`, i.e. a global option
    AFTER the subcommand. argparse rejects parent-parser options in that position
    unless every subparser also declares them -- which it did not, so the service
    crash-looped with "unrecognized arguments" on first deploy."""
    assert main(argv) == EXIT_NO_DAEMON


def test_serve_accepts_config_after_the_subcommand(capsys):
    """Reaching the config loader (and failing on a missing file) proves the
    flag was parsed rather than rejected."""
    assert main(["serve", "--config", "/nonexistent/mbrelay.toml"]) == EXIT_USAGE
    assert "not found" in capsys.readouterr().err


def test_a_global_given_after_the_subcommand_wins():
    parser = build_parser()
    args = parser.parse_args(["--socket", "/before.sock", "ping",
                              "--socket", "/after.sock"])
    assert args.socket == "/after.sock"


def test_a_global_given_only_before_the_subcommand_survives():
    """SUPPRESS on the subparser copies is what stops an unspecified option
    clobbering the one the user gave earlier."""
    parser = build_parser()
    args = parser.parse_args(["--socket", "/before.sock", "ping"])
    assert args.socket == "/before.sock"


def test_no_command_is_a_usage_error(capsys):
    assert main([]) == EXIT_USAGE


def test_bad_config_path_is_a_usage_error(capsys):
    assert main(["--config", "/nonexistent/mbrelay.toml", "status"]) == EXIT_USAGE
    assert "not found" in capsys.readouterr().err


def test_ping_without_a_daemon_has_its_own_exit_code(short_sock, capsys):
    """Ansible and the HIL scripts branch on these, so they are an interface."""
    assert main(["--socket", str(short_sock), "ping"]) == EXIT_NO_DAEMON


def test_status_without_a_daemon_has_its_own_exit_code(short_sock):
    assert main(["--socket", str(short_sock), "status"]) == EXIT_NO_DAEMON


def test_devices_falls_back_to_a_local_scan(short_sock, monkeypatch, capsys):
    """`mbrelay devices` must work before the daemon starts -- that is exactly
    when you are trying to work out whether the hardware is visible at all."""
    from mbrelay import cli
    from mbrelay.transport import PortInfo
    uid = "9906360200052820abcd2372c44f4f67000000006e052820"
    monkeypatch.setattr("mbrelay.transport.scan_ports",
                        lambda: {uid: PortInfo(uid=uid, device="/dev/fake")})
    monkeypatch.setattr("mbrelay.cli._known_boards", lambda cfg: {})

    async def silent(self, factory, port):
        return None

    monkeypatch.setattr("mbrelay.relay.RelayControl.probe", silent)
    assert cli.main(["--socket", str(short_sock), "devices"]) == EXIT_OK
    out = capsys.readouterr()
    assert "/dev/fake" in out.out
    assert "abcd2372" in out.out          # the distinguishing slice, not the tail
    assert "no_firmware" in out.out and "silent on USB" in out.out
    assert "daemon not running" in out.err


def test_devices_without_a_daemon_asks_each_board_who_it_is(short_sock, monkeypatch,
                                                            capsys):
    """Not a hex slice and "unknown": the board's own name, role and firmware,
    for a relay and a robot alike."""
    from mbrelay import cli
    from mbrelay.relay import BannerInfo
    from mbrelay.transport import PortInfo
    relay = "99063602000528208939f0a5fd47f738000000006e052820"
    robot = "9906360200052820a8fdb5e413abb276000000006e052820"
    old = "99063602000528202e78ea8f7143163f000000006e052820"
    monkeypatch.setattr("mbrelay.transport.scan_ports", lambda: {
        relay: PortInfo(uid=relay, device="/dev/fake-relay"),
        robot: PortInfo(uid=robot, device="/dev/fake-robot"),
        old: PortInfo(uid=old, device="/dev/fake-old")})
    monkeypatch.setattr("mbrelay.cli._known_boards", lambda cfg: {})
    answers = {
        "/dev/fake-relay": BannerInfo("RADIOBRIDGE", "vitut", "2198604104", b"",
                                      firmware="0.20260913.2"),
        "/dev/fake-robot": BannerInfo("NEZHA2", "tovez", "2314287040", b"",
                                      firmware="1.20260912.8"),
        "/dev/fake-old": BannerInfo("RADIOBRIDGE", "vevav", "1", b"")}

    async def probe(self, factory, port):
        return answers[port]

    monkeypatch.setattr("mbrelay.relay.RelayControl.probe", probe)
    assert cli.main(["--socket", str(short_sock), "devices"]) == EXIT_OK
    lines = {line.split()[0]: line.split() for line in capsys.readouterr().out.splitlines()
             if line.split() and line.split()[0] in ("vitut", "tovez", "vevav")}
    assert lines["vitut"][1:4] == ["free", "RADIOBRIDGE", "0.20260913.2"]
    assert lines["tovez"][1:4] == ["foreign", "NEZHA2", "1.20260912.8"]
    assert lines["vevav"][1:4] == ["free", "RADIOBRIDGE", "<0.20260913.2"], \
        "a relay that answered but has no !VER? is older firmware, not unknown"


def test_devices_names_a_board_another_program_holds_and_says_who(short_sock,
                                                                  monkeypatch, capsys):
    import errno

    from mbrelay import cli
    from mbrelay.transport import PortInfo
    uid = "9906360200052820a8fdb5e413abb276000000006e052820"
    monkeypatch.setattr("mbrelay.transport.scan_ports",
                        lambda: {uid: PortInfo(uid=uid, device="/dev/fake")})
    monkeypatch.setattr("mbrelay.cli._known_boards", lambda cfg: {
        uid: ("tovez", "NEZHA2", "radio-robot-lib/config/robots/devices.json")})
    monkeypatch.setattr("mbrelay.cli._port_holder",
                        lambda port: "node scripts/dev.mjs (pid 56603)")

    async def held(self, factory, port):
        raise OSError(errno.EBUSY, "could not open port /dev/fake: Resource busy")

    monkeypatch.setattr("mbrelay.relay.RelayControl.probe", held)
    assert cli.main(["--socket", str(short_sock), "devices"]) == EXIT_OK
    out = capsys.readouterr().out
    row = next(line.split() for line in out.splitlines() if line.startswith("tovez"))
    assert row[1:3] == ["busy", "NEZHA2"]
    assert "held by node scripts/dev.mjs (pid 56603)" in out
    assert "from radio-robot-lib/config/robots/devices.json" in out


def test_devices_no_probe_leaves_the_boards_alone_and_names_them_from_mbdeploy(
        short_sock, monkeypatch, capsys):
    from mbrelay import cli
    from mbrelay.transport import PortInfo
    uid = "99063602000528208939f0a5fd47f738000000006e052820"
    monkeypatch.setattr("mbrelay.transport.scan_ports",
                        lambda: {uid: PortInfo(uid=uid, device="/dev/fake")})
    monkeypatch.setattr("mbrelay.cli._known_boards",
                        lambda cfg: {uid: ("vitut", "RADIOBRIDGE", "mbdeploy-registry.json")})

    async def never(self, factory, port):
        raise AssertionError("--no-probe opened a port, which resets the board")

    monkeypatch.setattr("mbrelay.relay.RelayControl.probe", never)
    assert cli.main(["--socket", str(short_sock), "devices", "--no-probe"]) == EXIT_OK
    out = capsys.readouterr().out
    assert any(line.split()[:1] == ["vitut"] for line in out.splitlines())
    assert "name and role from mbdeploy-registry.json" in out


def test_config_show_reports_where_each_value_came_from(capsys):
    assert main(["--json", "config", "show"]) == EXIT_OK
    import json
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["server"]["port"] == 8760
    assert "sources" in payload


def test_install_unit_prints_both_artifacts(capsys):
    assert main(["install-unit"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[Unit]" in out and "SUBSYSTEM==" in out
    assert "<type>_mbrelay._tcp</type>" in out           # the avahi service file
    # avahi-utils is not installed by default on Ubuntu Server, so the unit has
    # to say that discovery wants it -- and that nothing breaks without it.
    assert "avahi-utils" in out
    # The unit must outlive the drain, or systemd kills the daemon while it is
    # still handing boards back.
    assert "TimeoutStopSec=30" in out
    assert 'ATTRS{idProduct}=="0204"' in out


# -- flash: the mbdeploy contract -------------------------------------------
@pytest.fixture
def flasher(tmp_path):
    from mbrelay.config import load
    (tmp_path / "pyocd.yaml").write_text("chip_erase: chip\n")
    (tmp_path / "MICROBIT.hex").write_text(":00000001FF\n")
    cfg = load(overrides={
        "state.dir": str(tmp_path),
        "firmware.hex": str(tmp_path / "MICROBIT.hex"),
        "firmware.pyocd_config": str(tmp_path / "pyocd.yaml"),
    }, environ={})
    return Flasher(cfg), tmp_path


class FakePopen:
    """subprocess.Popen for mbdeploy: records the call, replays canned output."""
    lines = ["Erasing chip...", "[====      ] 40%", "[==========] 100%", "Done."]
    returncode = 0

    def __init__(self, cmd, **kwargs):
        FakePopen.cmd, FakePopen.cwd = cmd, kwargs.get("cwd")
        self.stdout = iter(line + "\n" for line in self.lines)

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def test_deploy_argv_is_exactly_what_mbdeploy_needs(flasher, monkeypatch):
    flash, tmp_path = flasher
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    uid = "9906360200052820abcd2372c44f4f67000000006e052820"
    result = flash.deploy(uid, tmp_path / "MICROBIT.hex")
    captured = {"cmd": FakePopen.cmd, "cwd": FakePopen.cwd}

    assert result.ok
    cmd = captured["cmd"]
    assert cmd[0:2] == ["mbdeploy", "deploy"]
    # Target by UID: mbdeploy matches a 40-52 char hex token straight against the
    # uid field, skipping the name lookup that depends on a prior probe.
    assert cmd[2] == uid
    # Without this mbdeploy refuses every relay-role board, which is all of them.
    assert "--force-relay" in cmd
    assert "--target-mcu" in cmd and "nrf52833" in cmd
    # pyocd reads pyocd.yaml from its cwd. Wrong cwd means a silent fallback to
    # sector erase, which fails on the nRF52833 MBR region at 0x0.
    assert captured["cwd"] == tmp_path


def test_missing_pyocd_yaml_is_refused_with_the_reason(tmp_path):
    from mbrelay.config import load
    cfg = load(overrides={"state.dir": str(tmp_path),
                          "firmware.pyocd_config": str(tmp_path)}, environ={})
    with pytest.raises(FlashError, match="chip_erase"):
        Flasher(cfg).pyocd_cwd()


def test_missing_hex_is_refused(flasher):
    flash, tmp_path = flasher
    with pytest.raises(FlashError, match="not found"):
        flash.resolve_hex(str(tmp_path / "absent.hex"))


def test_deploy_hands_over_output_as_it_arrives(flasher, monkeypatch):
    """A board takes a minute or more; captured-until-the-end output made a
    working flash indistinguishable from a hung one."""
    flash, tmp_path = flasher
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    seen = []
    result = flash.deploy("9906360200052820abcd2372c44f4f67000000006e052820",
                          tmp_path / "MICROBIT.hex", on_line=seen.append)
    assert seen == FakePopen.lines
    assert result.ok and result.message == "Done."


@pytest.fixture
def downloads(monkeypatch):
    """Serve canned (body, redirect hops) by URL instead of reaching GitHub."""
    served = {}
    asked = []

    def fetch(url, timeout):
        asked.append(url)
        body, hops = served[url]
        return hops, body

    monkeypatch.setattr("mbrelay.firmware._fetch", fetch)
    return served, asked


def bare_flasher(tmp_path, **overrides):
    from mbrelay.config import load
    return Flasher(load(overrides={"state.dir": str(tmp_path), **overrides}, environ={}))


def test_no_hex_and_no_url_means_the_latest_release(tmp_path, downloads):
    served, asked = downloads
    flash = bare_flasher(tmp_path)
    latest = flash.cfg.firmware.release_url
    assert "League-Robotics/microbit-radio-relay/releases/latest" in latest
    # What GitHub really does: latest -> the tagged asset -> a signed blob URL.
    served[latest] = (b":00000001FF\n", [
        latest.replace("latest/download", "download/v0.20260913.2"),
        "https://release-assets.githubusercontent.com/github-production-release-asset/1?sig=x"])
    said = []
    path = flash.resolve_source(say=said.append)
    assert asked == [latest]
    assert path.read_bytes() == b":00000001FF\n"
    assert "  release: v0.20260913.2" in said, "the release tag is shown"
    assert not any("sig=" in line for line in said), "not the signed storage URL"


def test_a_url_wins_over_the_configured_image(tmp_path, downloads):
    served, asked = downloads
    (tmp_path / "configured.hex").write_text(":00000001FF\n")
    flash = bare_flasher(tmp_path, **{"firmware.hex": str(tmp_path / "configured.hex")})
    served["https://example.com/x.hex"] = (b":00000001FF\n", [])
    flash.resolve_source(url="https://example.com/x.hex", say=lambda s: None)
    assert asked == ["https://example.com/x.hex"]
    # ...and with no flag at all, the configured image is what gets used.
    assert flash.resolve_source(say=lambda s: None) == (tmp_path / "configured.hex").resolve()


def test_a_download_that_is_not_a_hex_image_is_refused(tmp_path, downloads):
    """A wrong GitHub URL answers with an HTML page, not an error status."""
    served, _ = downloads
    served["https://example.com/nope"] = (b"<!DOCTYPE html><html>", [])
    with pytest.raises(FlashError, match="not an Intel HEX"):
        bare_flasher(tmp_path).resolve_source(url="https://example.com/nope",
                                              say=lambda s: None)


def test_hex_and_url_together_is_a_usage_error(capsys):
    assert main(["flash", "--all-relays", "--hex", "a.hex", "--url", "https://x/y.hex"]) \
        == EXIT_USAGE
    assert "not both" in capsys.readouterr().err


def test_missing_mbdeploy_explains_how_to_install_it(flasher, monkeypatch):
    """The daemon has no runtime dependency on mbdeploy, so a host that only
    serves relays will not have it -- say so instead of a traceback."""
    flash, _ = flasher
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(FlashError, match="pipx install"):
        flash.check()


def test_flash_without_a_target_is_a_usage_error(capsys, tmp_path):
    (tmp_path / "pyocd.yaml").write_text("chip_erase: chip\n")
    (tmp_path / "MICROBIT.hex").write_text(":00000001FF\n")
    code = main(["flash", "--hex", str(tmp_path / "MICROBIT.hex")])
    assert code in (EXIT_USAGE, EXIT_HARDWARE)


def test_flash_of_an_absent_board_reports_no_device(monkeypatch, tmp_path, capsys):
    (tmp_path / "pyocd.yaml").write_text("chip_erase: chip\n")
    hexfile = tmp_path / "MICROBIT.hex"
    hexfile.write_text(":00000001FF\n")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/mbdeploy")
    monkeypatch.setattr("mbrelay.transport.scan_ports", lambda: {})
    code = main(["flash", "nosuchboard", "--hex", str(hexfile),
                 "--yes"])
    assert code == EXIT_NO_DEVICE


def test_the_shipped_packaging_files_match_the_strings_in_the_wheel(tmp_path):
    """packaging/ is sdist-only and packaging_assets.py is what a wheel-only host
    gets, so the two are duplicates by construction. Nothing pinned them
    together, which is exactly how they drift."""
    from mbrelay.packaging_assets import SYSTEMD_UNIT, UDEV_RULE

    packaging = Path(__file__).resolve().parent.parent / "packaging"
    if not packaging.is_dir():
        pytest.skip("packaging/ is not shipped in the wheel")
    assert (packaging / "mbrelay.service").read_text() == SYSTEMD_UNIT
    assert (packaging / "99-microbit-relay.rules").read_text() == UDEV_RULE


# -- discover and auto-connect ----------------------------------------------
def found(*services, problem=""):
    """A stand-in for mbrelay.mdns.browse_detailed that answers instantly."""
    from mbrelay.mdns import BrowseResult

    def browse(*args, **kwargs):
        return BrowseResult(services=tuple(services), problem=problem,
                            source="192.168.1.40", elapsed=0.0, queries=3)
    return browse


def host(instance="torture", address="192.0.2.12", port=8760,
         version="0.20260826.9"):
    from mbrelay.mdns import Service

    return Service(instance=instance, hostname=f"{instance}.local",
                   addresses=(address,), port=port,
                   txt={"txtvers": "1", "version": version})


@pytest.fixture
def dialled(monkeypatch):
    """Record what connect() was asked for, without opening a socket.

    Every address in these tests is in the RFC 5737 documentation range, but a
    unit test must not reach the network even to be refused -- the bench has
    real relays on 192.168.1.0/24 and the suite has to describe the code rather
    than the room it runs in.
    """
    calls = []

    def refuse(host, port, timeout=10.0):
        calls.append((host, port))
        raise OSError("refused by the test")

    monkeypatch.setattr("mbrelay.client.connect", refuse)
    return calls


def test_discover_finding_nothing_is_success_and_says_why(monkeypatch, capsys):
    """Naming a host still works and always did, so this is not an error."""
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        found(problem="no _mbrelay._tcp nodes answered on 192.168.1.40"))
    assert main(["discover"]) == EXIT_OK
    assert "no _mbrelay._tcp nodes answered" in capsys.readouterr().err


def test_discover_lists_what_it_found(monkeypatch, capsys):
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        found(host(), host("agony", "192.0.2.19")))
    assert main(["discover"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "NAME" in out and "torture" in out and "192.0.2.19" in out


def test_discover_json_carries_the_source_address_it_queried_from(monkeypatch, capsys):
    """A wrong NIC and a firewall drop look identical on the wire, so the source
    is the first thing to check when the answer is empty."""
    monkeypatch.setattr("mbrelay.mdns.browse_detailed", found(host()))
    assert main(["--json", "discover"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "192.168.1.40"
    assert payload["hosts"][0]["name"] == "torture"
    assert payload["hosts"][0]["port"] == 8760


# -- devices on other hosts -------------------------------------------------
def board(name, state="free", session=None):
    return {"uid": "9906360200052820abababab" + "0" * 24, "name": name, "state": state,
            "role": "RADIOBRIDGE", "session": session, "short_uid": "abababab"}


def test_devices_remote_lists_the_boards_on_every_host_it_found(monkeypatch, capsys):
    from mbrelay.mdns import Service
    vali = Service(instance="vali", hostname="vali.local", addresses=("192.0.2.151",),
                   port=8760, txt={"version": "x", "registry": "9000"})
    monkeypatch.setattr("mbrelay.mdns.browse_detailed", found(host(), vali))
    boards = {"192.0.2.12": [board("gozop", "busy", "s-804"), board("getez")],
              "192.0.2.151": [board("zavaz")]}
    asked = []

    def fetch(address, port, timeout=3.0):
        asked.append((address, port))
        return boards[address]

    monkeypatch.setattr("mbrelay.client.fetch_devices", fetch)
    assert main(["devices", "--remote"]) == EXIT_OK
    out = capsys.readouterr().out
    assert [line.split()[:2] for line in out.splitlines()[2:]] == [
        ["torture", "gozop"], ["torture", "getez"], ["vali", "zavaz"]]
    assert "s-804" in out
    # The HTTP port -- advertised, else the default -- never the pool port:
    # looking must not take a board away from anyone.
    assert sorted(asked) == [("192.0.2.12", 8761), ("192.0.2.151", 9000)]


def test_devices_remote_names_a_host_that_did_not_answer_and_lists_the_rest(
        monkeypatch, capsys):
    from mbrelay.client import DevicesUnavailable
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        found(host(), host("vali", "192.0.2.151")))

    def fetch(address, port, timeout=3.0):
        if address == "192.0.2.151":
            raise DevicesUnavailable("not found -- that daemon predates GET /devices")
        return [board("getez")]

    monkeypatch.setattr("mbrelay.client.fetch_devices", fetch)
    assert main(["devices", "--remote"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "getez" in captured.out and "vali" not in captured.out
    assert "vali (mbrelay 0.20260826.9)" in captured.err
    assert "predates GET /devices" in captured.err


def test_devices_remote_is_an_error_when_no_host_answers(monkeypatch):
    from mbrelay.client import DevicesUnavailable
    monkeypatch.setattr("mbrelay.mdns.browse_detailed", found(host()))

    def fetch(address, port, timeout=3.0):
        raise DevicesUnavailable("connection refused")

    monkeypatch.setattr("mbrelay.client.fetch_devices", fetch)
    assert main(["devices", "--remote"]) == EXIT_ERROR


def test_devices_for_a_named_host_skips_discovery(monkeypatch, capsys):
    def no_browse(*args, **kwargs):
        raise AssertionError("a named host must not browse")

    monkeypatch.setattr("mbrelay.mdns.browse_detailed", no_browse)
    asked = []
    monkeypatch.setattr("mbrelay.client.fetch_devices",
                        lambda address, port, timeout=3.0:
                        asked.append((address, port)) or [board("getez")])
    assert main(["devices", "torture"]) == EXIT_OK
    assert asked == [("torture", 8761)]
    assert "getez" in capsys.readouterr().out


def test_connect_with_no_target_uses_the_one_host_it_found(monkeypatch, dialled,
                                                           capsys):
    monkeypatch.setattr("mbrelay.mdns.browse_detailed", found(host()))
    assert main(["connect"]) == EXIT_ERROR          # the dial itself is refused
    assert dialled == [("192.0.2.12", 8760)]
    assert "# discovered torture at 192.0.2.12:8760" in capsys.readouterr().err


def test_connect_with_a_target_does_not_browse_at_all(monkeypatch, dialled):
    """No discovery, no delay: a typed address is an instruction, not a hint."""
    def refuse_to_browse(*args, **kwargs):
        raise AssertionError("browsed despite being given a target")

    monkeypatch.setattr("mbrelay.mdns.browse_detailed", refuse_to_browse)
    assert main(["connect", "198.51.100.7:9000"]) == EXIT_ERROR
    assert dialled == [("198.51.100.7", 9000)]


def test_connect_falls_back_to_localhost_when_nothing_answers(monkeypatch, dialled,
                                                              capsys):
    """Preserving what `mbrelay connect` did before discovery existed."""
    monkeypatch.setattr("mbrelay.mdns.browse_detailed", found(problem="nobody home."))
    assert main(["connect"]) == EXIT_ERROR
    assert dialled == [("127.0.0.1", 8760)]
    assert "127.0.0.1:8760" in capsys.readouterr().err


def test_no_discover_skips_the_browse_entirely(monkeypatch, dialled):
    def refuse_to_browse(*args, **kwargs):
        raise AssertionError("browsed despite --no-discover")

    monkeypatch.setattr("mbrelay.mdns.browse_detailed", refuse_to_browse)
    assert main(["connect", "--no-discover"]) == EXIT_ERROR
    assert dialled == [("127.0.0.1", 8760)]


def test_connect_names_an_advertised_host_with_discover(monkeypatch, dialled):
    """`--discover NAME` picks by advertised name; without the flag NAME is a
    hostname and goes straight to the resolver, as it always has."""
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        found(host(), host("agony", "192.0.2.19")))
    assert main(["connect", "agony", "--discover"]) == EXIT_ERROR
    assert dialled == [("192.0.2.19", 8760)]


def test_connect_naming_a_host_that_did_not_answer_is_a_usage_error(monkeypatch,
                                                                    dialled, capsys):
    monkeypatch.setattr("mbrelay.mdns.browse_detailed", found(host()))
    assert main(["connect", "nosuchnode", "--discover"]) == EXIT_USAGE
    assert dialled == []
    assert "hosts that did answer: torture" in capsys.readouterr().err


def test_several_hosts_and_no_terminal_is_a_usage_error_not_a_hung_pipe(
        monkeypatch, dialled, capsys):
    """A pipeline must never block on a prompt, and guessing which host a script
    meant would be worse than saying so."""
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        found(host(), host("agony", "192.0.2.19")))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert main(["connect"]) == EXIT_USAGE
    assert dialled == []
    err = capsys.readouterr().err
    assert "several relay hosts found" in err


def test_the_picker_connects_to_the_numbered_choice(monkeypatch, dialled, capsys):
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        found(host(), host("agony", "192.0.2.19")))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: " 2 ")
    assert main(["connect"]) == EXIT_ERROR
    assert dialled == [("192.0.2.19", 8760)]
    assert "2 relay hosts found:" in capsys.readouterr().out


def test_declining_the_picker_is_success_not_an_error(monkeypatch, dialled):
    """Following the flash confirmation: saying no is not a failure."""
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        found(host(), host("agony", "192.0.2.19")))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "q")
    assert main(["connect"]) == EXIT_OK
    assert dialled == []


# -- connect ----------------------------------------------------------------
@pytest.mark.parametrize("target,expected", [
    ("host:1234", ("host", 1234)),
    ("host", ("host", 8760)),
    ("", ("127.0.0.1", 8760)),
    ("[::1]:99", ("::1", 99)),
    ("192.168.1.12:8760", ("192.168.1.12", 8760)),
])
def test_connect_target_parsing(target, expected):
    from mbrelay.client import parse_target
    assert parse_target(target) == expected


def test_flash_result_names_the_board_not_the_interface_chip():
    """Four boards from one batch share the last sixteen UID characters, so a
    tail slice labels every line of flash output identically."""
    from mbrelay.firmware import FlashResult
    a = FlashResult(uid="9906360200052820aaaa2372c44f4f67000000006e052820", name="", ok=True)
    b = FlashResult(uid="9906360200052820bbbb6c3809a44554000000006e052820", name="", ok=True)
    assert a.short_uid != b.short_uid


def test_a_port_already_in_use_is_explained_not_traced(capsys):
    """`journalctl -u mbrelay` should say what is wrong, not print a traceback.

    A leftover daemon holding the port is the single most common way a restart
    fails, and the bare OSError from create_server names neither the port nor
    the likely cause.
    """
    import socket

    held = socket.socket()
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        code = main(["serve", "--bind", "127.0.0.1", "--port", str(port)])
    finally:
        held.close()

    assert code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "cannot listen on" in err and str(port) in err
    assert "Traceback" not in err
