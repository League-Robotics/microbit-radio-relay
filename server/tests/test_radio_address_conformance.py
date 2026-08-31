"""Do the implementations of name <-> (channel, group) agree with EACH OTHER?

test_naming.py proves mbrelay.naming matches the spec's digest. This file
proves that when sibling repositories with a tools/radio-address-dump are
checked out next to this one (pxt-nezha-diffdrive's MakeCode extension,
radio-robot-lib), theirs produce the identical name space. The dump protocol
is documented in tools/radio-address-dump; the checker is
scripts/radio_address_conformance.py.

This repo contributes one implementation, `python`. It used to contribute a
second, `firmware-cpp`, compiled from the header the relay firmware ran for
its `!N <name>` command -- removed with that command, because a name is now
only a DEFAULT and the registry, which the board cannot see, is what says
where a robot actually is. A host-only C++ copy would have proved nothing:
the value of that axis was comparing the code a board really runs.
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
    the real mbrelay.naming and not a table that drifted away from it. A
    disagreement means a name resolves to a different default address here
    than on the robot."""
    assert _dump("python") == canonical_form()
    # v2 dumps (five columns) digest to D2, the conformance gate.
    assert hashlib.sha256(_dump("python").encode()).hexdigest() == \
        SPEC["properties"]["conformance_sha256"]


def test_the_dump_protocol_lists_this_repos_one_implementation():
    """`firmware-cpp` went with `!N`; a stray entry here would make the
    checker report a missing implementation as a failure."""
    r = subprocess.run([sys.executable, str(DUMP), "--list"], capture_output=True, text=True)
    assert r.returncode == 0 and set(r.stdout.split()) == {"python"}


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
