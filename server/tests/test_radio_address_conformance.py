"""Do the implementations of name <-> (channel, group) agree with EACH OTHER?

test_naming.py proves mbrelay.naming matches the spec's digest. This file
proves the code the relay firmware actually runs for `!N` --
source/relay/naming.h, compiled for the host -- produces the identical name
space, and, when sibling repositories with a tools/radio-address-dump are
checked out next to this one, that theirs do too. The dump protocol is
documented in tools/radio-address-dump; the checker is
scripts/radio_address_conformance.py.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mbrelay.naming import canonical_form

ROOT = Path(__file__).resolve().parents[2]
DUMP = ROOT / "tools" / "radio-address-dump"
CHECKER = ROOT / "scripts" / "radio_address_conformance.py"
SPEC = json.loads((Path(__file__).parent / "radio-address-vectors.json").read_text())
UNAVAILABLE = 3


def _dump(impl: str) -> str:
    r = subprocess.run([sys.executable, str(DUMP), impl], capture_output=True, text=True)
    if r.returncode == UNAVAILABLE:
        pytest.skip(f"{impl}: {r.stderr.strip()}")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_the_dump_this_repo_publishes_is_the_mapping_the_registry_uses():
    """The dumper is what every other repo is compared against, so it must be
    the real mbrelay.naming and not a table that drifted away from it."""
    assert _dump("python") == canonical_form()
    assert hashlib.sha256(_dump("python").encode()).hexdigest() == \
        SPEC["properties"]["conformance_sha256"]


def test_the_firmware_cpp_agrees_with_python_on_every_name():
    """The firmware's mapping is compiled from the same header the board runs.
    A disagreement here means `!N tovez` tunes the relay somewhere the
    registry (and the robot) do not expect."""
    firmware = _dump("firmware-cpp")
    python = canonical_form()
    for i, (a, b) in enumerate(zip(firmware.splitlines(), python.splitlines())):
        assert a == b, f"first disagreement at n={i}: firmware {a!r}, python {b!r}"
    assert firmware == python
    assert hashlib.sha256(firmware.encode()).hexdigest() == \
        SPEC["properties"]["conformance_sha256"]


def test_the_dump_protocol_lists_both_implementations():
    r = subprocess.run([sys.executable, str(DUMP), "--list"], capture_output=True, text=True)
    assert r.returncode == 0 and set(r.stdout.split()) == {"python", "firmware-cpp"}


def test_every_sibling_repository_agrees():
    """Cross-repo: skipped when no sibling with a dumper is checked out next
    to this repository (CI), run for real on a developer machine."""
    siblings = [p for p in ROOT.parent.iterdir()
                if p.is_dir() and p != ROOT and (p / "tools" / "radio-address-dump").exists()]
    if not siblings:
        pytest.skip("no sibling repository with tools/radio-address-dump next to this checkout")
    r = subprocess.run([sys.executable, str(CHECKER), "auto"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all implementations agree" in r.stdout
