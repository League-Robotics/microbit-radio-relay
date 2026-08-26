"""Shared fixtures. Everything here runs without hardware."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from mbrelay.config import load as load_config
from mbrelay.transport import PortInfo

from fake_relay import FakeChannelFactory, FakeRelayFirmware, FakeScanner
from relay_fixtures import PORT_A, PORT_B, UID_A, UID_B


@pytest.fixture
def short_sock():
    """A socket path short enough for AF_UNIX.

    sun_path is a fixed 104-byte buffer on macOS, and pytest's tmp_path
    (/private/var/folders/...) already spends most of it, so sockets need their
    own shallow directory.
    """
    root = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None
    directory = Path(tempfile.mkdtemp(prefix="mbr", dir=root))
    try:
        yield directory / "s.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def cfg(tmp_path, short_sock):
    """A config that keeps every side effect inside tmp_path."""
    return load_config(overrides={
        "state.dir": str(tmp_path),
        "admin.socket": str(short_sock),
        "server.port": 0,
        "devices.scan_interval_ms": 50,
        # Hardware timings scaled down: the fake answers instantly, and a real
        # 300ms settle per open would make the suite crawl.
        "serial.open_settle_ms": 1,
        "serial.post_close_settle_ms": 1,
        "serial.hello_timeout_ms": 300,
    }, environ={})


@pytest.fixture
def boards():
    a = FakeRelayFirmware(name="aaaaa", serial="1111111111")
    b = FakeRelayFirmware(name="bbbbb", serial="2222222222")
    a.peer, b.peer = b, a          # wire the two "radios" together
    return {PORT_A: a, PORT_B: b}


@pytest.fixture
def factory(boards):
    return FakeChannelFactory(boards)


@pytest.fixture
def scanner():
    return FakeScanner(ports={
        UID_A: PortInfo(uid=UID_A, device=PORT_A, description="fake"),
        UID_B: PortInfo(uid=UID_B, device=PORT_B, description="fake"),
    })
