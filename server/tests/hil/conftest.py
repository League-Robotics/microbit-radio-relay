"""Fixtures for hardware-in-the-loop tests.

Skipped unless real relays are attached, so `pytest -m 'not hil'` in CI and a
bare `pytest` on a bench machine both do the right thing.

These tests take minutes, not seconds: every acquire and release resets a board,
and a reset costs a second or two of settling. That is the thing being tested.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from mbrelay.transport import scan_ports

from hil_support import PORT


# NOTE: pytest hands this hook EVERY collected item in the session, not just the
# ones under this directory -- so it must filter by path. Marking unconditionally
# tagged the entire hardware-free suite as `hil` and `-m 'not hil'` deselected
# all of it.
_HIL_DIR = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    for item in items:
        if _HIL_DIR in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.hil)


@pytest.fixture(scope="session")
def attached():
    boards = scan_ports()
    if len(boards) < 2:
        pytest.skip(f"needs 2 micro:bit relays attached, found {len(boards)}")
    return boards


@pytest.fixture(scope="session")
def daemon(attached):
    """A real daemon on real hardware, on its own port so it cannot collide with
    one an operator already has running."""
    root = "/tmp" if os.path.isdir("/tmp") else None
    workdir = Path(tempfile.mkdtemp(prefix="mbrhil", dir=root))
    config = workdir / "hil.toml"
    config.write_text(
        f'[server]\nbind = "127.0.0.1"\nport = {PORT}\n'
        f'[state]\ndir = "{workdir}"\n'
        f'[admin]\nsocket = "{workdir}/s.sock"\n'
        f'[log]\nlevel = "debug"\n')
    logfile = open(workdir / "daemon.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "mbrelay.cli",
                             "--config", str(config), "serve"],
                            stdout=logfile, stderr=subprocess.STDOUT)
    def die_if_dead(stage: str) -> None:
        """Surface the daemon's own error instead of a 60-second timeout.

        Without this, a daemon that exits at startup (port in use, bad config)
        looks identical to hardware that never came ready.
        """
        if proc.poll() is None:
            return
        logfile.flush()
        tail = (workdir / "daemon.log").read_text()[-1500:]
        raise RuntimeError(
            f"the daemon exited during {stage} (rc={proc.returncode}):\n{tail}")

    try:
        _wait_for_port(PORT, timeout=45, alive=die_if_dead)
        _wait_for_free(workdir / "s.sock", want=2, timeout=60, alive=die_if_dead)
        yield {"port": PORT, "socket": str(workdir / "s.sock"),
               "log": workdir / "daemon.log", "config": str(config)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
        logfile.close()


def _wait_for_port(port: int, timeout: float, alive=lambda stage: None) -> None:
    end = time.time() + timeout
    while time.time() < end:
        alive("startup")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"daemon never listened on {port}")


def _wait_for_free(sock_path, want: int, timeout: float,
                   alive=lambda stage: None) -> None:
    from mbrelay.adminclient import AdminClient
    end = time.time() + timeout
    while time.time() < end:
        alive("device discovery")
        try:
            client = AdminClient(str(sock_path))
            free = client.call("status")["devices"]["free"]
            client.close()
            if free >= want:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"fewer than {want} relays became free")


@pytest.fixture(autouse=True)
def pool_settled(daemon):
    """Wait for every board to be back in the pool before the test starts.

    Releasing a board is not instant: the daemon has to reset it and re-verify
    factory defaults, which costs a couple of seconds. Without this, each test
    races the previous test's cleanup and intermittently sees "no relay
    available" -- the same surprise a test-script author hits in the wild.
    """
    from mbrelay.adminclient import AdminClient

    def free_count():
        client = AdminClient(daemon["socket"])
        try:
            counts = client.call("status")["devices"]
            return counts["free"], counts["total"]
        finally:
            client.close()

    end = time.time() + 90
    while time.time() < end:
        free, total = free_count()
        if free >= total:
            return
        time.sleep(0.5)

    client = AdminClient(daemon["socket"])
    try:
        stuck = [f"{d['name']}={d['state']}" for d in client.call("list")["devices"]
                 if d["state"] != "free"]
    finally:
        client.close()
    pytest.fail("boards never returned to the pool between tests: " + ", ".join(stuck))


@pytest.fixture
def borrowed(daemon, admin):
    """Take a board out of the daemon's pool so a test can open its tty directly.

    Necessary because the daemon opens ports with TIOCEXCL, and so do we -- and
    because a scan or an acquire landing mid-test would reset the board under us.
    """
    taken = []

    def take(port: str) -> str:
        record = next(d for d in admin("list")["devices"] if d["port"] == port)
        admin("disable", device=record["uid"], reason="hil direct access")
        taken.append(record["uid"])
        return port

    yield take
    for uid in taken:
        try:
            admin("enable", device=uid)
        except Exception:
            pass


@pytest.fixture
def admin(daemon):
    from mbrelay.adminclient import AdminClient

    def call(cmd, **args):
        client = AdminClient(daemon["socket"])
        try:
            return client.call(cmd, **args)
        finally:
            client.close()
    return call
