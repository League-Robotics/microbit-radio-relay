"""Acquire, pipe, release -- the daemon's whole reason for existing."""

from __future__ import annotations

import asyncio

import pytest

from mbrelay.errors import NoFreeDevice
from mbrelay.inventory import TERMINAL, DeviceState, Inventory
from mbrelay.relay import RelayControl
from mbrelay.session import SessionManager

from relay_fixtures import PORT_A, PORT_B, UID_A
from fake_relay import FakeRelayFirmware, StoredConfig


@pytest.fixture
async def manager(cfg, factory, scanner):
    control = RelayControl(cfg)

    async def prober(record):
        return await control.probe(factory, record.port)

    inventory = Inventory(cfg, scanner, prober=prober)
    await inventory.start()
    for _ in range(80):                       # let the probe tasks finish
        if all(r.state in TERMINAL for r in inventory.records.values()):
            break
        await asyncio.sleep(0.02)
    manager = SessionManager(cfg, inventory, factory, control)
    yield manager
    await inventory.stop()


def sink(store: bytearray):
    return lambda data: store.extend(data)


async def test_acquire_binds_a_board_and_replays_the_banner(manager):
    session = await manager.acquire("test:1")
    assert session.record.state is DeviceState.BUSY
    assert session.preamble.startswith(b"DEVICE:RADIOBRIDGE:relay:")
    assert manager.inventory.counts()["busy"] == 1


async def test_acquire_hands_over_a_clean_board(cfg, manager, factory):
    """Normalizing on acquire, not only on release, is what makes 'you get a
    clean board' a guarantee rather than a hope: a SIGKILL or a power cut leaves
    the previous client's channel sitting in flash."""
    factory.boards[PORT_A] = FakeRelayFirmware(
        name="aaaaa", serial="1",
        stored=StoredConfig(channel=31, mode="MAKECODE", echo="ON"))
    factory.boards[PORT_B] = FakeRelayFirmware(
        name="bbbbb", serial="2",
        stored=StoredConfig(channel=29, mode="MAKECODE", echo="ON"))

    session = await manager.acquire("test:1")
    board = factory.boards[session.record.port]
    assert (board.cfg.channel, board.cfg.mode, board.cfg.echo) == (0, "RAW250", "OFF")


async def test_bytes_pass_through_untouched(manager):
    """Transparency is the product. Nulls, high bytes and CRLF must all survive."""
    session = await manager.acquire("test:1")
    got = bytearray()
    session.attach(sink=sink(got), on_gone=lambda exc: None)

    payload = bytes(range(256)) + b"\r\n\x00\xff" + b"A" * 5000
    session.write_to_board(payload)
    await asyncio.sleep(0.05)

    # Check the bytes that actually reached the port, not what the fake firmware
    # made of them: transparency is about the wire, not the parse.
    channel = manager.factory.channels[-1]
    assert bytes(channel.written).endswith(payload)
    assert session.tx_bytes == len(payload)


async def test_data_flows_back_to_the_client(manager):
    session = await manager.acquire("test:1")
    got = bytearray()
    session.attach(sink=sink(got), on_gone=lambda exc: None)

    session.write_to_board(b"?\n")
    await asyncio.sleep(0.05)
    assert b"# channel: 0 group: 10" in bytes(got)


async def test_setup_chatter_is_not_leaked_to_the_client(manager):
    """A direct serial client on a default board would not see the daemon's
    normalize echoes, so neither should a socket client."""
    session = await manager.acquire("test:1")
    got = bytearray()
    session.attach(sink=sink(got), on_gone=lambda exc: None)
    await asyncio.sleep(0.05)
    assert b"# mode: RAW250" not in bytes(got)
    assert b"# echo: OFF" not in bytes(got)


async def test_entering_the_data_plane_is_noticed(manager):
    session = await manager.acquire("test:1")
    session.attach(sink=sink(bytearray()), on_gone=lambda exc: None)
    assert session.plane == "command"
    session.write_to_board(b"!GO\n")
    await asyncio.sleep(0.05)
    assert session.plane == "data"


async def test_no_free_device_when_every_board_is_bound(manager):
    await manager.acquire("test:1")
    await manager.acquire("test:2")
    with pytest.raises(NoFreeDevice) as exc:
        await manager.acquire("test:3")
    assert exc.value.total == 2 and exc.value.busy == 2
    assert manager.rejected_total == 1


async def test_concurrent_connects_never_double_book(manager):
    """Two boards, ten simultaneous clients: exactly two must win.

    The FREE -> ACQUIRING flip is synchronous and pre-await, so this is meant to
    hold by construction rather than by luck.
    """
    results = await asyncio.gather(*(manager.acquire(f"c:{i}") for i in range(10)),
                                   return_exceptions=True)
    bound = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, NoFreeDevice)]
    assert len(bound) == 2
    assert len(rejected) == 8
    assert len({s.record.uid for s in bound}) == 2, "two clients got the same board"


async def test_release_restores_defaults_and_frees_the_board(manager, factory):
    session = await manager.acquire("test:1")
    record = session.record
    session.attach(sink=sink(bytearray()), on_gone=lambda exc: None)

    session.write_to_board(b"!C 17\n")
    await asyncio.sleep(0.05)
    session.write_to_board(b"!GO\n")
    await asyncio.sleep(0.05)
    board = factory.boards[record.port]
    assert board.cfg.channel == 17 and board.plane == "data"

    await manager.release(session, "client_closed")

    assert record.state is DeviceState.FREE
    assert board.plane == "command", "release must reset the board out of the data plane"
    assert board.cfg.channel == 0 and board.cfg.mode == "RAW250"
    assert record.session_id is None


async def test_release_of_a_vanished_board_does_not_hang(manager, scanner):
    """Unplugging mid-session must not leave release trying to talk to nothing.

    The board is removed from the SCANNER, not by poking Inventory.live_uids:
    the scan loop is running, and it would put a hand-cleared uid straight back
    -- which is what this test originally did, and it passed on a fast machine
    only because release happened to win the race.
    """
    session = await manager.acquire("test:1")
    uid = session.record.uid

    del scanner.ports[uid]
    await manager.inventory.scan_once()
    assert session.record.state is DeviceState.GONE

    await manager.release(session, "device_removed")
    assert session.record.state is DeviceState.GONE
    assert session.record.port is None
    assert session.id not in manager.sessions


async def test_shutdown_returns_every_board(manager, factory):
    a = await manager.acquire("test:1")
    b = await manager.acquire("test:2")
    for session in (a, b):
        session.attach(sink=sink(bytearray()), on_gone=lambda exc: None)
        session.write_to_board(b"!C 9\n")
    await asyncio.sleep(0.05)

    await manager.shutdown(grace=10)

    assert not manager.sessions
    for board in factory.boards.values():
        assert board.cfg.channel == 0, "a board was left on the last client's channel"


async def test_reader_is_detached_before_the_port_closes(manager, factory):
    """Mirrors the SerialChannel rule: selecting on a closed fd raises EBADF, and
    on a recycled fd number it silently reads an unrelated file."""
    session = await manager.acquire("test:1")
    session.attach(sink=sink(bytearray()), on_gone=lambda exc: None)
    channel = session.channel
    await manager.release(session, "client_closed")
    assert channel.reader_removed_before_close is True


async def test_data_plane_marker_split_across_reads(manager):
    """Serial reads land on arbitrary chunk boundaries, so the marker can arrive
    in two pieces. A naive `marker in chunk` misses it -- which it did, on real
    hardware, while the fake happened to deliver it whole."""
    session = await manager.acquire("test:1")
    session.attach(sink=lambda data: None, on_gone=lambda exc: None)
    assert session.plane == "command"

    session._on_board_data(b"# entering data pl")
    assert session.plane == "command"
    session._on_board_data(b"ane\r\n")
    assert session.plane == "data"


async def test_plane_watcher_does_not_alter_the_stream(manager):
    session = await manager.acquire("test:1")
    got = bytearray()
    session.attach(sink=got.extend, on_gone=lambda exc: None)
    for chunk in (b"# entering data pl", b"ane\r\n", b"\x00\xff raw bytes"):
        session._on_board_data(chunk)
    assert bytes(got) == b"# entering data plane\r\n\x00\xff raw bytes"
