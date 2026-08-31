"""The publish side: the argv we build, and the child process we supervise.

No avahi anywhere in here. The publisher is a shell script the test writes, which
is enough to exercise everything that is actually ours -- spawn, restart with
backoff, and reap on shutdown. What it cannot cover is whether avahi-publish
itself is happy under the hardened systemd unit; that is a fleet-node question
and it belongs on torture, not in this file.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mbrelay.config import load as load_config
from mbrelay.mdns import Advertiser, publish_argv


def daemon_for(cfg, port: int | None = None) -> SimpleNamespace:
    """The two attributes Advertiser touches on the daemon."""
    sockets = [SimpleNamespace(getsockname=lambda: ("0.0.0.0", port))] if port else []
    return SimpleNamespace(cfg=cfg, server=SimpleNamespace(sockets=sockets))


def publisher(tmp_path, body: str) -> str:
    script = tmp_path / "publisher"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


# -- argv -------------------------------------------------------------------
def test_avahi_publish_argv_is_what_avahi_utils_expects():
    assert publish_argv("/usr/bin/avahi-publish", "torture", "_mbrelay._tcp", 8760,
                        ["txtvers=1", "version=1.2"]) == [
        "/usr/bin/avahi-publish", "-s", "torture", "_mbrelay._tcp", "8760",
        "txtvers=1", "version=1.2"]


def test_dns_sd_argv_takes_a_domain_where_avahi_does_not():
    """macOS has no avahi-publish, and dns-sd wants the domain spelled out --
    so a dev-laptop daemon advertises too rather than silently not."""
    assert publish_argv("/usr/bin/dns-sd", "gala", "_mbrelay._tcp", 8760, []) == [
        "/usr/bin/dns-sd", "-R", "gala", "_mbrelay._tcp", "local", "8760"]


# -- the paths that must never stop the daemon ------------------------------
async def test_a_missing_publisher_warns_and_keeps_serving(monkeypatch, caplog):
    """avahi-utils is not installed by default on Ubuntu Server, so this is the
    common case on a fresh node -- and it must cost nothing but a log line."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    advertiser = Advertiser(daemon_for(load_config(environ={})))
    await advertiser.start()
    assert not advertiser.active
    assert "not found" in advertiser.state
    assert any("apt install avahi-utils" in r.message for r in caplog.records)
    await advertiser.stop()             # and stopping what never started is fine


async def test_disabling_mdns_spawns_nothing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/avahi-publish")
    cfg = load_config(overrides={"mdns.enabled": False}, environ={})
    advertiser = Advertiser(daemon_for(cfg))
    await advertiser.start()
    assert not advertiser.active and advertiser.state == "disabled"


# -- supervision ------------------------------------------------------------
async def test_the_publisher_runs_until_stop_and_is_reaped(tmp_path):
    """The record lives exactly as long as the child, which is the property we
    want: a stale advertisement is worse than none."""
    cfg = load_config(overrides={
        "mdns.publish_cmd": publisher(tmp_path, "exec sleep 30")}, environ={})
    advertiser = Advertiser(daemon_for(cfg, port=8760))
    await advertiser.start()
    await asyncio.sleep(0.1)
    assert advertiser.active
    child = advertiser._proc
    assert child is not None and child.returncode is None
    assert "_mbrelay._tcp" in advertiser.state

    await advertiser.stop()
    assert child.returncode is not None
    assert not advertiser.active


async def test_a_publisher_that_dies_is_restarted(tmp_path):
    """avahi-daemon being down is the likely cause and it may well come back, so
    the supervisor keeps trying rather than giving up on the first exit."""
    counter = tmp_path / "runs"
    cfg = load_config(overrides={
        "mdns.publish_cmd": publisher(tmp_path, f"echo x >> {counter}\nexit 1")},
        environ={})
    advertiser = Advertiser(daemon_for(cfg, port=8760))
    advertiser.BACKOFF = (0.01,) * 5            # the real one starts at a second
    await advertiser.start()
    # Polled rather than slept: a fork plus an exec costs ~0.2s on a loaded
    # machine, so any fixed sleep is either flaky or slow.
    states = set()
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
        states.add(advertiser.state)
        if counter.exists() and counter.read_text().count("x") >= 2:
            break
    await advertiser.stop()

    assert counter.read_text().count("x") >= 2
    assert any("retrying" in state for state in states)


async def test_stopping_does_not_wait_around_for_a_child_that_ignores_sigterm(tmp_path):
    """Teardown is budgeted against state.shutdown_grace_s (20s) and the unit's
    TimeoutStopSec (30s). An advertiser must not eat into either."""
    cfg = load_config(overrides={
        "mdns.publish_cmd": publisher(tmp_path, "trap '' TERM\nexec sleep 30")},
        environ={})
    advertiser = Advertiser(daemon_for(cfg, port=8760))
    advertiser.STOP_TIMEOUT = 0.1
    await advertiser.start()
    await asyncio.sleep(0.1)
    child = advertiser._proc
    started = asyncio.get_running_loop().time()
    await advertiser.stop()
    assert asyncio.get_running_loop().time() - started < 1.0
    assert child.returncode is not None          # SIGKILLed rather than left behind


# -- what gets announced ----------------------------------------------------
async def test_the_announced_port_is_the_one_actually_bound(tmp_path):
    """With server.port = 0 the config says 0 and the kernel says something
    else. Announcing 0 would be worse than not announcing at all."""
    cfg = load_config(overrides={
        "server.port": 0,
        "mdns.publish_cmd": publisher(tmp_path, "exec sleep 30")}, environ={})
    advertiser = Advertiser(daemon_for(cfg, port=54321))
    await advertiser.start()
    try:
        assert "54321" in advertiser.state
    finally:
        await advertiser.stop()


def test_the_txt_carries_only_static_facts():
    """A live free-board count would force a republish every time a client came
    or went, and it keeps the TXT inside avahi's 512-byte legacy-reply cap.
    Counts are `mbrelay status`'s job.
    """
    from mbrelay import __version__
    txt = Advertiser(daemon_for(load_config(environ={})))._txt()
    assert txt[0] == "txtvers=1"
    assert f"version={__version__}" in txt
    assert not any(key.startswith(("devices=", "free=")) for key in txt)
    assert sum(len(entry) for entry in txt) < 200


def test_the_instance_name_defaults_to_the_short_hostname():
    import socket

    assert load_config(environ={}).mdns.instance == socket.gethostname().split(".")[0]
