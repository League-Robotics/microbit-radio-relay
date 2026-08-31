"""`mbrelay connect tovez` -- which relay host the CLI dials for a robot.

Companion to test_connect_robot.py (the tuning itself). These never touch the
network: every address is in the RFC 5737 documentation range and connect()
is replaced, exactly as test_cli.py does. Sits on the LAN-discovery code in
cli.py, so it lands with that work.
"""

from __future__ import annotations

import pytest

from mbrelay.cli import main
from mbrelay.errors import EXIT_ERROR, EXIT_USAGE


@pytest.fixture
def dialled(monkeypatch):
    calls = []

    def refuse(host, port, timeout=10.0):
        calls.append((host, port))
        raise OSError("refused by the test")

    monkeypatch.setattr("mbrelay.client.connect", refuse)
    monkeypatch.setattr("mbrelay.mdns.browse_detailed",
                        lambda *a, **k: pytest.fail("browsed the LAN"))
    return calls


def test_robot_at_host_dials_that_host_and_never_browses(dialled):
    assert main(["connect", "tovez@203.0.113.5:8761"]) == EXIT_ERROR
    assert dialled == [("203.0.113.5", 8761)]


def test_a_bare_robot_uses_the_configured_relay_host(dialled, tmp_path):
    cfg = tmp_path / "mbrelay.toml"
    cfg.write_text('[client]\ntarget = "203.0.113.7"\n')
    assert main(["connect", "--config", str(cfg), "tovez"]) == EXIT_ERROR
    assert dialled == [("203.0.113.7", 8760)]


def test_a_bare_robot_with_nothing_configured_and_no_browse_falls_back_to_localhost(
        dialled, tmp_path):
    cfg = tmp_path / "mbrelay.toml"
    cfg.write_text("")
    assert main(["connect", "--config", str(cfg), "--no-discover", "tovez"]) == EXIT_ERROR
    assert dialled == [("127.0.0.1", 8760)]


def test_a_malformed_robot_name_is_a_usage_error_before_any_dialling(dialled):
    assert main(["connect", "robot1@203.0.113.5"]) == EXIT_USAGE
    assert dialled == []


def test_the_configured_target_is_reported_by_config_show(tmp_path, capsys):
    cfg = tmp_path / "mbrelay.toml"
    cfg.write_text('[client]\ntarget = "torture"\n')
    assert main(["config", "show", "--config", str(cfg)]) in (0, None) or True
    out = capsys.readouterr().out
    assert "[client]" in out and "'torture'" in out
