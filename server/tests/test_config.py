import pytest

from mbrelay.config import load
from mbrelay.errors import ConfigError


def test_defaults():
    cfg = load(environ={})
    assert cfg.server.port == 8760
    assert cfg.devices.allow_roles == ("RADIOBRIDGE",)
    # Rejecting with a readable reason is the documented behaviour, so it must
    # be the default rather than something you have to switch on.
    assert cfg.server.reject_message.startswith("# ERROR")


def test_precedence_cli_beats_env():
    cfg = load(overrides={"server.port": 9999}, environ={"MBRELAY_PORT": "1234"})
    assert cfg.server.port == 9999
    assert cfg.sources["server.port"] == "command line"


def test_env_is_coerced_to_the_declared_type():
    cfg = load(environ={"MBRELAY_PORT": "1234"})
    assert cfg.server.port == 1234 and isinstance(cfg.server.port, int)


def test_file_layer(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[server]\nport = 4242\n[devices]\nallow_roles = ["X"]\n')
    cfg = load(path, environ={})
    assert cfg.server.port == 4242
    assert cfg.devices.allow_roles == ("X",)
    assert cfg.sources["server.port"] == str(path)


def test_unknown_key_is_fatal(tmp_path):
    """A typo in an ops config must not fail open."""
    path = tmp_path / "c.toml"
    path.write_text("[server]\nprot = 4242\n")
    with pytest.raises(ConfigError, match="unknown key 'prot'"):
        load(path, environ={})


def test_unknown_section_is_fatal(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[srever]\nport = 1\n")
    with pytest.raises(ConfigError, match="unknown config section"):
        load(path, environ={})


def test_missing_explicit_config_is_fatal(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml", environ={})


def test_derived_paths_are_filled_in():
    cfg = load(environ={})
    assert cfg.state.dir and cfg.admin.socket and cfg.firmware.registry
    assert cfg.firmware.registry.endswith("devices.json")
