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
                 ["connect"], ["discover"], ["config", "show"], ["install-unit"]):
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
    assert cli.main(["--socket", str(short_sock), "devices"]) == EXIT_OK
    out = capsys.readouterr()
    assert "/dev/fake" in out.out
    assert "abcd2372" in out.out          # the distinguishing slice, not the tail
    assert "daemon not running" in out.err


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


def test_deploy_argv_is_exactly_what_mbdeploy_needs(flasher, monkeypatch):
    flash, tmp_path = flasher
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    uid = "9906360200052820abcd2372c44f4f67000000006e052820"
    result = flash.deploy(uid, tmp_path / "MICROBIT.hex")

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
