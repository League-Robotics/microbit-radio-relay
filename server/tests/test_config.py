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
    assert cfg.firmware.registry.endswith("mbdeploy-registry.json")


def test_mbdeploy_registry_does_not_collide_with_the_identity_cache(tmp_path):
    """Two tools, two formats, two files.

    mbrelay writes {"version": 1, "devices": {...}} to <state.dir>/devices.json;
    mbdeploy expects a flat {uid: {...}} map. Pointing both at one path made
    mbdeploy crash with "argument of type 'int' is not iterable" the first time
    a flash was attempted on real hardware.
    """
    from pathlib import Path

    cfg = load(overrides={"state.dir": str(tmp_path)}, environ={})
    identity_cache = Path(cfg.state.dir) / "devices.json"
    assert Path(cfg.firmware.registry) != identity_cache
    assert Path(cfg.firmware.registry).parent == Path(cfg.state.dir)


# -- [mdns] -----------------------------------------------------------------
def test_mdns_defaults_to_on_with_a_hostname_derived_instance():
    import socket

    cfg = load(environ={})
    assert cfg.mdns.enabled is True
    assert cfg.mdns.service == "_mbrelay._tcp"
    assert cfg.mdns.instance == socket.gethostname().split(".")[0]
    assert cfg.sources["mdns.instance"] == "derived default"


def test_an_unknown_key_in_mdns_is_still_fatal(tmp_path):
    """Registering the section buys the same typo protection as every other one,
    which is the whole reason it goes through _SECTIONS rather than being read
    on the side."""
    path = tmp_path / "c.toml"
    path.write_text('[mdns]\nenable = false\n')
    with pytest.raises(ConfigError, match="unknown key 'enable'"):
        load(path, environ={})


def test_mdns_can_be_turned_off_from_the_environment():
    """So a node whose network forbids multicast can be fixed from the unit file
    without editing a config."""
    assert load(environ={"MBRELAY_MDNS": "off"}).mdns.enabled is False
