"""CLI wiring, exit codes, and the exact mbdeploy invocation `flash` builds."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mbrelay.cli import build_parser, main
from mbrelay.errors import (EXIT_HARDWARE, EXIT_NO_DAEMON, EXIT_NO_DEVICE,
                            EXIT_OK, EXIT_USAGE)
from mbrelay.firmware import FlashError, Flasher


# -- parser -----------------------------------------------------------------
def test_every_documented_subcommand_parses():
    parser = build_parser()
    for argv in (["serve"], ["devices"], ["list"], ["status"], ["sessions"],
                 ["kick", "s-1"], ["reset", "vevov"], ["disable", "x"], ["enable", "x"],
                 ["rescan"], ["events"], ["ping"], ["flash", "--all-relays"],
                 ["connect"], ["config", "show"], ["install-unit"]):
        assert parser.parse_args(argv).func is not None


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
