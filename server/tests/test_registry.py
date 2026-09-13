"""The registry: where a robot actually is, versus where its name says.

The whole point is that the derived address became a DEFAULT. These tests pin
down the three things that follow from that: a lookup always answers, an
override survives, and the reverse map stops trusting the bijection once a name
has been moved.
"""

from __future__ import annotations

import json

import pytest

from mbrelay import naming
from mbrelay.config import load as load_config
from mbrelay.errors import RegistryError
from mbrelay.registry import CONFIG, DERIVED, REGISTRY, NameRegistry, parse_pair


def _cfg(tmp_path, names=None):
    overrides = {"state.dir": str(tmp_path)}
    cfg = load_config(overrides=overrides, environ={})
    if names:
        import dataclasses
        cfg = dataclasses.replace(
            cfg, registry=dataclasses.replace(cfg.registry, names=names))
    return cfg


@pytest.fixture
def registry(tmp_path):
    return NameRegistry(_cfg(tmp_path))


# -- resolving ---------------------------------------------------------------
def test_a_name_nobody_has_mentioned_still_resolves(registry):
    """"Always answers" is the property every caller is built on: a robot that
    has never been talked to has to be reachable by name the first time."""
    entry = registry.resolve("tovez")
    assert (entry.channel, entry.group) == naming.name_to_radio("tovez") == (48, 29)
    assert entry.source == DERIVED and entry.derived


def test_resolving_records_the_robot_so_the_listing_is_useful(registry):
    """`GET /names` is meant to be the list of robots this relay knows about,
    which only works if a plain lookup writes a record."""
    assert registry.all() == []
    registry.resolve("tovez")
    assert [e.name for e in registry.all()] == ["tovez"]


def test_a_malformed_name_is_an_error_but_an_unknown_one_is_not(registry):
    """The spec is explicit: `pipip` is a legal address nobody is on, while
    `robot1` has no address at all. Refusing the first breaks tune-by-name;
    accepting the second invents a link on an arbitrary channel."""
    assert registry.resolve("pipip").channel == 34
    for bad in ("robot1", "gauti", "vevo", "", "aeiou"):
        with pytest.raises(RegistryError):
            registry.resolve(bad)


@pytest.mark.parametrize("name", ["TOVEZ", " tovez ", "Tovez"])
def test_a_name_is_normalized_the_way_the_mapping_normalizes_it(registry, name):
    assert registry.resolve(name).name == "tovez"


# -- overriding --------------------------------------------------------------
def test_an_override_moves_a_robot_off_its_derived_address(registry):
    entry = registry.set("tovez", 12, 4)
    assert (entry.channel, entry.group, entry.source) == (12, 4, REGISTRY)
    assert not entry.derived
    assert registry.resolve("tovez").channel == 12


def test_clearing_an_override_puts_the_robot_back_on_its_default(registry):
    registry.set("tovez", 12, 4)
    entry = registry.clear("tovez")
    assert (entry.channel, entry.group, entry.source) == (48, 29, DERIVED)


@pytest.mark.parametrize("channel,group", [(84, 4), (-1, 4), (12, 256), (12, -1)])
def test_a_pair_the_firmware_would_refuse_is_refused_here(registry, channel, group):
    """Better to fail on the API call than at tune time on a bench: `!CG` takes
    channel 0-83 and group 0-255 and nothing else."""
    with pytest.raises(RegistryError):
        registry.set("tovez", channel, group)


# -- precedence --------------------------------------------------------------
def test_a_config_pin_outranks_a_learned_override(tmp_path):
    """A survey's output goes in the config file. If a stale learned value
    could shadow it, a restart would quietly undo the survey."""
    registry = NameRegistry(_cfg(tmp_path))
    registry.set("tovez", 12, 4)

    pinned = NameRegistry(_cfg(tmp_path, names={"tovez": "20/7"}))
    pinned.load()
    entry = pinned.resolve("tovez")
    assert (entry.channel, entry.group, entry.source) == (20, 7, CONFIG)


def test_the_api_refuses_to_shadow_a_pin_rather_than_pretending(tmp_path):
    registry = NameRegistry(_cfg(tmp_path, names={"tovez": "20/7"}))
    with pytest.raises(RegistryError) as caught:
        registry.set("tovez", 12, 4)
    assert caught.value.code == "pinned"
    with pytest.raises(RegistryError):
        registry.clear("tovez")


def test_an_unparseable_pin_is_a_startup_error(tmp_path):
    for bad in ({"tovez": "12"}, {"tovez": "x/4"}, {"tovez": "999/4"},
                {"robot1": "12/4"}):
        with pytest.raises(RegistryError):
            NameRegistry(_cfg(tmp_path, names=bad))


@pytest.mark.parametrize("text,expected", [("12/4", (12, 4)), ("0/0", (0, 0)),
                                           ("83/255", (83, 255))])
def test_a_pin_is_channel_slash_group(text, expected):
    assert parse_pair(text) == expected


# -- persistence -------------------------------------------------------------
def test_an_override_survives_a_restart_but_a_derived_entry_re_derives(tmp_path):
    first = NameRegistry(_cfg(tmp_path))
    first.set("tovez", 12, 4)
    first.resolve("vevov")

    second = NameRegistry(_cfg(tmp_path))
    second.load()
    assert second.resolve("tovez").channel == 12
    assert second.resolve("tovez").source == REGISTRY
    assert second.resolve("vevov").source == DERIVED


def test_the_file_is_written_atomically_and_is_plain_json(tmp_path):
    registry = NameRegistry(_cfg(tmp_path))
    registry.set("tovez", 12, 4)
    data = json.loads((tmp_path / "names.json").read_text())
    assert data["version"] == 1
    assert data["names"]["tovez"] == {"channel": 12, "group": 4, "explicit": True,
                                      "updated": data["names"]["tovez"]["updated"]}
    assert not list(tmp_path.glob("*.tmp"))


def test_an_unusable_row_is_dropped_rather_than_stopping_the_daemon(tmp_path):
    """A half-written or hand-edited file must never keep boards from being
    served; the bad row is simply re-derived."""
    (tmp_path / "names.json").write_text(json.dumps({"version": 1, "names": {
        "tovez": {"channel": 12, "group": 4, "explicit": True},
        "vevov": {"channel": 999, "group": 4},
        "robot1": {"channel": 1, "group": 1},
        "getez": "not even an object",
    }}))
    registry = NameRegistry(_cfg(tmp_path))
    registry.load()
    assert registry.resolve("tovez").channel == 12
    assert registry.resolve("vevov").source == DERIVED


def test_a_missing_file_is_simply_an_empty_registry(tmp_path):
    registry = NameRegistry(_cfg(tmp_path / "nothing here"))
    registry.load()
    assert registry.all() == []


# -- the reverse map ---------------------------------------------------------
def test_reverse_lookup_reads_the_bijection_for_free(registry):
    """A name maps to its own pair with no record needed, which is what lets
    `mbrelay status` label a session for a robot nobody has registered."""
    assert registry.name_for(48, 29) == "tovez"
    assert registry.name_for(0, 10) is None          # the !C space is not derived


def test_reverse_lookup_follows_a_robot_that_moved(registry):
    """The bijection stops being the truth the moment an override exists: 12/4
    is tovez now, and 55/108 is nobody."""
    registry.set("tovez", 12, 4)
    assert registry.name_for(12, 4) == "tovez"
    assert registry.name_for(48, 29) is None


def test_a_pin_wins_the_reverse_lookup_too(tmp_path):
    registry = NameRegistry(_cfg(tmp_path, names={"tovez": "20/7"}))
    assert registry.name_for(20, 7) == "tovez"
    assert registry.name_for(48, 29) is None


# -- conflicts ---------------------------------------------------------------
def test_two_robots_on_one_link_are_reported_not_refused(registry):
    """A survey is expected to pass through a clash halfway, and an operator
    moving robots by hand needs to see it rather than be stopped by it -- the
    same posture the daemon takes for two sessions sharing a channel."""
    registry.set("tovez", 12, 4)
    registry.set("vevov", 12, 4)
    assert registry.conflicts() == {(12, 4): ["tovez", "vevov"]}


def test_a_registry_with_no_clashes_reports_none(registry):
    registry.set("tovez", 12, 4)
    registry.resolve("vevov")
    assert registry.conflicts() == {}


def test_the_listing_annotates_every_row_that_shares_a_link(registry):
    """One listing serves both the HTTP API and `mbrelay names`. The first
    version built it twice and the CLI copy forgot the annotation, so every
    clash showed as "-" in the table an operator actually reads."""
    registry.set("tovez", 12, 4)
    registry.set("vevov", 12, 4)
    registry.resolve("getez")
    listing = registry.listing()
    assert {r["name"]: r.get("conflict") for r in listing["names"]} == {
        "getez": None, "tovez": ["vevov"], "vevov": ["tovez"]}
    assert listing["conflicts"] == [{"channel": 12, "group": 4,
                                     "names": ["tovez", "vevov"]}]


def test_robots_on_one_channel_in_different_groups_are_a_channel_conflict(registry):
    """Their groups filter each other's packets, but a group is only an address
    byte: both still transmit on one frequency. tovez (48/29) and zuvig (48/52)
    both derive channel 48."""
    registry.resolve("tovez")
    registry.resolve("zuvig")
    assert registry.conflicts() == {}
    assert registry.channel_conflicts() == {48: ["tovez", "zuvig"]}
    assert registry.listing()["channel_conflicts"] == [
        {"channel": 48, "names": ["tovez", "zuvig"]}]


def test_a_derived_row_saved_under_an_old_mapping_is_re_derived_on_load(tmp_path):
    """names.json caches derived pairs. When the mapping changed (25 + 2*(n%25)
    became 11 + n%73), a daemon that trusted the file kept every robot on its
    OLD default. An explicit move is a decision and survives untouched."""
    (tmp_path / "names.json").write_text(json.dumps({"version": 1, "names": {
        "tovez": {"channel": 55, "group": 108, "explicit": False, "updated": 1.0},
        "vevov": {"channel": 12, "group": 4, "explicit": True, "updated": 2.0}}}))
    registry = NameRegistry(_cfg(tmp_path))
    registry.load()
    tovez, vevov = registry.get("tovez"), registry.get("vevov")
    assert (tovez.channel, tovez.group, tovez.source) == (48, 29, DERIVED)
    assert (vevov.channel, vevov.group, vevov.source) == (12, 4, REGISTRY)
    saved = json.loads((tmp_path / "names.json").read_text())["names"]
    assert (saved["tovez"]["channel"], saved["tovez"]["group"]) == (48, 29)


def test_one_clash_is_not_reported_at_both_severities(registry):
    registry.set("tovez", 12, 4)
    registry.set("vevov", 12, 4)
    assert registry.channel_conflicts() == {}
    assert "channel_conflict" not in registry.annotate(registry.get("tovez"))


def test_a_shared_link_with_a_neighbour_on_its_channel_reports_both(registry):
    registry.set("tovez", 12, 4)
    registry.set("vevov", 12, 4)
    registry.set("gopiv", 12, 5)
    assert registry.conflicts() == {(12, 4): ["tovez", "vevov"]}
    assert registry.channel_conflicts() == {12: ["gopiv", "tovez", "vevov"]}
    rows = {r["name"]: r for r in registry.listing()["names"]}
    assert rows["tovez"]["conflict"] == ["vevov"]
    assert rows["tovez"]["channel_conflict"] == ["gopiv"]
    assert "conflict" not in rows["gopiv"]
    assert rows["gopiv"]["channel_conflict"] == ["tovez", "vevov"]
    assert registry.annotate(registry.get("tovez")) == rows["tovez"]
