"""The admin socket: framing, dispatch, and the commands operators actually use."""

from __future__ import annotations

import asyncio
import json

import pytest

from pathlib import Path

from mbrelay.admin import HANDLERS, AdminServer
from mbrelay.adminclient import AdminClient
from mbrelay.errors import AdminError, DaemonNotRunning
from mbrelay.inventory import DeviceState, Inventory
from mbrelay.registry import NameRegistry
from mbrelay.relay import RelayControl
from mbrelay.session import SessionManager
from mbrelay.transport import PortInfo

from relay_fixtures import UID_A, UID_B


class StubDaemon:
    """Just enough daemon for the handlers, which is the point of keeping them
    plain functions in a registry rather than methods on the server."""

    def __init__(self, cfg, factory, scanner):
        self.cfg = cfg
        self.factory = factory
        self.control = RelayControl(cfg)
        self.inventory = Inventory(cfg, scanner,
                                   prober=lambda r: self.control.probe(factory, r.port))
        self.registry = NameRegistry(cfg)
        self.sessions = SessionManager(cfg, self.inventory, factory, self.control,
                                       registry=self.registry)
        self.conns_total = 0
        self.spawned = []
        self._stopping = asyncio.Event()

    def spawn(self, coro, name=None):
        task = asyncio.create_task(coro, name=name)
        self.spawned.append(task)
        return task

    def status(self):
        return {"version": "test", "pid": 1, "uptime_s": 1.0,
                "listeners": [{"addr": "x:1", "conns_total": 0,
                               "accepted": 0, "rejected": 0}],
                "devices": self.inventory.counts(),
                "sessions": self.sessions.to_json()}


@pytest.fixture
async def daemon(cfg, factory, scanner):
    d = StubDaemon(cfg, factory, scanner)
    await d.inventory.start()
    from mbrelay.inventory import TERMINAL
    for _ in range(200):
        if all(r.state in TERMINAL for r in d.inventory.records.values()):
            break
        await asyncio.sleep(0.02)
    yield d
    await d.inventory.stop()


@pytest.fixture
async def served(daemon):
    server = AdminServer(daemon)
    await server.start()
    yield daemon, server
    await server.stop()


# -- handlers, called directly ---------------------------------------------
def test_every_documented_command_is_registered():
    for name in ("ping", "version", "status", "list", "sessions", "kick", "rescan",
                 "disable", "enable", "reset", "config", "loglevel", "shutdown",
                 "names", "names_set", "names_clear"):
        assert name in HANDLERS


async def test_list_reports_both_boards(daemon):
    rows = HANDLERS["list"](daemon, {})["devices"]
    assert {r["uid"] for r in rows} == {UID_A, UID_B}
    assert all(r["state"] == "free" for r in rows)
    # The distinguishing slice must be in the payload; the CLI prints it.
    assert len({r["short_uid"] for r in rows}) == 2


async def test_disable_then_enable(daemon):
    HANDLERS["disable"](daemon, {"device": "aaaaa", "reason": "maintenance"})
    record = daemon.inventory.find("aaaaa")
    assert record.state is DeviceState.DISABLED
    assert record.disabled_reason == "maintenance"
    assert record not in daemon.inventory.free_devices()

    HANDLERS["enable"](daemon, {"device": "aaaaa"})
    assert record.state is not DeviceState.DISABLED


async def test_disable_refuses_a_bound_board(daemon):
    session = await daemon.sessions.acquire("t:1")
    with pytest.raises(AdminError, match="in use"):
        HANDLERS["disable"](daemon, {"device": session.record.name})


async def test_unknown_device_is_not_found(daemon):
    with pytest.raises(AdminError) as exc:
        HANDLERS["kick"](daemon, {"device": "nosuchboard"})
    assert exc.value.code == "not_found"


async def test_kick_terminates_a_session(daemon):
    session = await daemon.sessions.acquire("t:1")
    gone = []
    session.attach(sink=lambda d: None, on_gone=gone.append)
    result = HANDLERS["kick"](daemon, {"session": session.id})
    await asyncio.sleep(0.05)
    assert result["kicked"] == [session.id]
    assert gone, "the session was not told to go away"


async def test_shutdown_is_refused_unless_enabled(daemon):
    with pytest.raises(AdminError, match="disabled"):
        HANDLERS["shutdown"](daemon, {})


async def test_reset_refuses_a_bound_board_without_force(daemon):
    session = await daemon.sessions.acquire("t:1")
    with pytest.raises(AdminError, match="in use"):
        HANDLERS["reset"](daemon, {"device": session.record.name})


# -- the wire ---------------------------------------------------------------
# AdminClient is deliberately synchronous: in real use it is a separate process
# (the CLI) doing one request and printing one table. Inside an async test it
# would block the very loop the server runs on, so drive it from a thread.
async def _call(client: AdminClient, cmd: str, **args):
    return await asyncio.to_thread(client.call, cmd, **args)



async def test_round_trip_over_the_socket(served):
    daemon, server = served
    client = AdminClient(str(server.path))
    try:
        assert (await _call(client, "ping"))["pong"] is True
        assert len((await _call(client, "list"))["devices"]) == 2
        assert (await _call(client, "status"))["devices"]["free"] == 2
    finally:
        client.close()


async def test_many_requests_share_one_connection(served):
    daemon, server = served
    client = AdminClient(str(server.path))
    try:
        for _ in range(5):
            assert (await _call(client, "version"))["version"]
    finally:
        client.close()


async def test_unknown_command_is_reported_not_fatal(served):
    daemon, server = served
    client = AdminClient(str(server.path))
    try:
        with pytest.raises(AdminError) as exc:
            await _call(client, "frobnicate")
        assert exc.value.code == "unknown_command"
        # The connection must survive a refusal, not be torn down by it.
        assert (await _call(client, "ping"))["pong"] is True
    finally:
        client.close()


async def test_malformed_line_is_rejected(served):
    daemon, server = served
    reader, writer = await asyncio.open_unix_connection(str(server.path))
    writer.write(b"this is not json\n")
    await writer.drain()
    response = json.loads(await reader.readline())
    assert response["ok"] is False and response["error"]["code"] == "bad_request"
    writer.close()


async def test_response_ids_match_requests(served):
    daemon, server = served
    reader, writer = await asyncio.open_unix_connection(str(server.path))
    for req_id in (7, 8, 9):
        writer.write(json.dumps({"id": req_id, "cmd": "ping"}).encode() + b"\n")
        await writer.drain()
        assert json.loads(await reader.readline())["id"] == req_id
    writer.close()


def test_client_reports_a_missing_daemon_clearly(short_sock):
    client = AdminClient(str(short_sock))
    with pytest.raises(DaemonNotRunning, match="no mbrelay daemon"):
        client.call("ping")


async def test_a_second_daemon_refuses_to_start(served):
    daemon, server = served
    second = AdminServer(daemon)
    with pytest.raises(AdminError, match="already listening"):
        await second.start()


async def test_a_stale_socket_file_is_cleaned_up(daemon):
    stale = Path(daemon.cfg.admin.socket)
    stale.write_bytes(b"")            # a plain file left by a crash
    server = AdminServer(daemon)
    await server.start()              # must not raise
    try:
        client = AdminClient(str(server.path))
        assert (await _call(client, "ping"))["pong"] is True
        client.close()
    finally:
        await server.stop()


async def test_an_over_long_socket_path_is_explained(daemon, tmp_path):
    """AF_UNIX sun_path is a fixed 104/108-byte buffer. Bare OSErrors from a unit
    file are miserable to debug, so this must say what is wrong."""
    import dataclasses
    long_path = tmp_path / ("d" * 120) / "s.sock"
    daemon.cfg = dataclasses.replace(
        daemon.cfg, admin=dataclasses.replace(daemon.cfg.admin, socket=str(long_path)))
    with pytest.raises(AdminError, match="too long for AF_UNIX"):
        await AdminServer(daemon).start()


# -- the name registry over the socket ---------------------------------------
async def test_names_answers_for_a_robot_nobody_has_asked_about(daemon):
    """An operator on the box should not have to curl their own daemon, so the
    registry is reachable here as well as over HTTP."""
    row = HANDLERS["names"](daemon, {"name": "tovez"})["name"]
    assert (row["channel"], row["group"], row["source"]) == (55, 108, "derived")


async def test_names_set_then_clear_moves_a_robot_and_puts_it_back(daemon):
    row = HANDLERS["names_set"](daemon, {"name": "tovez", "channel": 12, "group": 4})
    assert (row["name"]["channel"], row["name"]["source"]) == (12, "registry")
    assert HANDLERS["names"](daemon, {})["names"][0]["channel"] == 12

    row = HANDLERS["names_clear"](daemon, {"name": "tovez"})
    assert (row["name"]["channel"], row["name"]["source"]) == (55, "derived")


async def test_a_registry_refusal_arrives_as_an_admin_error(daemon):
    """RegistryError must not escape as a bare exception: the client renders
    AdminError codes, and an uncaught one becomes a stack trace in the log."""
    with pytest.raises(AdminError) as caught:
        HANDLERS["names"](daemon, {"name": "robot1"})
    assert caught.value.code == "bad_request"

    with pytest.raises(AdminError):
        HANDLERS["names_set"](daemon, {"name": "tovez", "channel": 999, "group": 4})
    with pytest.raises(AdminError):
        HANDLERS["names_set"](daemon, {"name": "tovez"})


async def test_names_reports_a_shared_link(daemon):
    HANDLERS["names_set"](daemon, {"name": "tovez", "channel": 12, "group": 4})
    HANDLERS["names_set"](daemon, {"name": "vevov", "channel": 12, "group": 4})
    assert HANDLERS["names"](daemon, {})["conflicts"] == [
        {"channel": 12, "group": 4, "names": ["tovez", "vevov"]}]
