"""`mbrelay names` and the registry lookup `mbrelay connect <robot>` makes.

The lookup is the part that matters: a client that quietly fell back to the
derived address on a fleet where a robot HAS been moved would tune to the wrong
link, which is the exact failure this whole design exists to prevent. So the
fallback has to happen, and it has to say so.
"""

from __future__ import annotations

import io
import json

import pytest

from mbrelay.client import RegistryUnreachable, resolve_robot
from mbrelay.cli import main
from mbrelay.errors import EXIT_ERROR, EXIT_USAGE


# -- resolving against a stubbed registry ------------------------------------
class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_resolve_asks_the_registry_and_believes_it(monkeypatch):
    seen = []

    def urlopen(url, timeout=None):
        seen.append(url)
        return _Response({"name": "tovez", "channel": 12, "group": 4,
                          "source": "registry"})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert resolve_robot("torture", "tovez") == (12, 4, "registry")
    assert seen == ["http://torture:8761/names/tovez"]


def test_a_registry_that_does_not_answer_offers_the_derived_address(monkeypatch):
    """A relay host running an older daemon must still be usable for every
    robot that never moved -- which today is all of them."""
    def urlopen(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(RegistryUnreachable) as caught:
        resolve_robot("torture", "tovez")
    assert (caught.value.channel, caught.value.group) == (55, 108)


def test_a_malformed_name_is_never_papered_over_with_a_fallback(monkeypatch):
    """`robot1` has no derived address to fall back TO, so this has to raise
    rather than invent a link."""
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: pytest.fail("dialled for a bad name"))
    with pytest.raises(ValueError):
        resolve_robot("torture", "robot1")


def test_nonsense_from_the_registry_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Response({"unexpected": True}))
    with pytest.raises(RegistryUnreachable):
        resolve_robot("torture", "tovez")


# -- connect tells the user which answer it got ------------------------------
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


def test_connect_looks_the_robot_up_before_it_dials(dialled, monkeypatch, capsys):
    """The socket is refused here, so the only thing under test is that the
    lookup happens against the relay host the CLI settled on."""
    seen = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=None: seen.append(url) or
                        _Response({"channel": 12, "group": 4, "source": "registry"}))
    assert main(["connect", "tovez@203.0.113.5"]) == EXIT_ERROR
    assert seen == ["http://203.0.113.5:8761/names/tovez"]
    assert "registry puts tovez on 12/4" in capsys.readouterr().err


def test_connect_says_out_loud_when_it_fell_back_to_the_derived_address(
        dialled, monkeypatch, capsys):
    """Silence here is the dangerous case: the user would have no way to tell a
    registry answer from a guess."""
    def urlopen(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert main(["connect", "tovez@203.0.113.5"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no registry on 203.0.113.5:8761" in err
    assert "derived address 55/108" in err


# -- the subcommand ----------------------------------------------------------
def test_names_set_rejects_a_link_that_is_not_channel_slash_group(capsys):
    assert main(["names", "set", "tovez", "12"]) == EXIT_USAGE
    assert "channel>/<group" in capsys.readouterr().err


def test_names_set_without_a_link_says_what_to_type(capsys):
    assert main(["names", "set", "tovez"]) == EXIT_USAGE
    assert "mbrelay names set tovez 12/4" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["get", "set", "clear"])
def test_a_name_is_required_where_one_is_meant(action, capsys):
    assert main(["names", action]) == EXIT_USAGE
    assert f"names {action} needs a name" in capsys.readouterr().err
