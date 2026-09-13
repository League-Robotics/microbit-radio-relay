"""The firmware reports the release it ships in.

One tag builds both the wheel and MICROBIT.hex, so `mbrelay devices` can only
say which build a board runs if the two carry the same string. They live in two
languages, so this is what keeps them together.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mbrelay import __version__

HEADER = Path(__file__).resolve().parents[2] / "source" / "relay" / "version.h"


def test_the_firmware_version_is_the_server_version():
    if not HEADER.is_file():
        pytest.skip("firmware source is not shipped in the wheel")
    match = re.search(r'#define\s+RELAY_FIRMWARE_VERSION\s+"([^"]+)"', HEADER.read_text())
    assert match, f"no RELAY_FIRMWARE_VERSION in {HEADER}"
    assert match.group(1) == __version__, (
        f"source/relay/version.h says {match.group(1)} but mbrelay is {__version__}; "
        "bump them together")
