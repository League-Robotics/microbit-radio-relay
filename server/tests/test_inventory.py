"""Discovery, identity caching, hot-plug, and the allow/deny rules."""

from __future__ import annotations

import asyncio

import pytest

from mbrelay.inventory import TERMINAL, DeviceRecord, DeviceState, Inventory
from mbrelay.relay import RelayControl
from mbrelay.transport import PortInfo

from relay_fixtures import PORT_A, PORT_B, UID_A, UID_B
from fake_relay import FakeRelayFirmware


@pytest.fixture
async def inventory(cfg, factory, scanner):
    control = RelayControl(cfg)

    async def prober(record):
        return await control.probe(factory, record.port)

    inv = Inventory(cfg, scanner, prober=prober)
    await inv.start()
    await _settle(inv)
    yield inv
    await inv.stop()


async def _settle(inv, timeout=6.0):
    """Wait until every board has reached a state we will not move it out of."""
    for _ in range(int(timeout / 0.02)):
        if inv.records and all(r.state in TERMINAL for r in inv.records.values()):
            return
        await asyncio.sleep(0.02)


# -- identity ---------------------------------------------------------------
def test_short_uid_distinguishes_boards_sharing_an_interface_chip():
    """Every micro:bit on this bench ends in the same 000000006e052820, so a tail
    slice names them all the same thing. The unique field is in the middle."""
    a = DeviceRecord(uid=UID_A)
    b = DeviceRecord(uid=UID_B)
    assert a.short_uid != b.short_uid
    assert UID_A[-12:] == UID_B[-12:], "fixture no longer models the shared tail"


def test_matches_accepts_every_reasonable_handle():
    record = DeviceRecord(uid=UID_A, device_name="getez", label="relay-a")
    for token in (UID_A, "getez", "GETEZ", "relay-a", record.short_uid):
        assert record.matches(token)
    assert not record.matches("zavaz")
    assert not record.matches("")


# -- discovery --------------------------------------------------------------
async def test_boards_are_discovered_and_classified(inventory):
    assert set(inventory.records) == {UID_A, UID_B}
    assert all(r.state is DeviceState.FREE for r in inventory.records.values())
    assert inventory.records[UID_A].device_name == "aaaaa"
    assert inventory.counts() == {"total": 2, "free": 2, "busy": 0,
                                  "error": 0, "other": 0}


async def test_a_board_without_relay_firmware_is_marked_not_probed_forever(
        cfg, factory, scanner):
    silent = FakeRelayFirmware(drop_first_banners=99)
    silent._handle = lambda line: None
    factory.boards[PORT_A] = silent
    control = RelayControl(cfg)

    inv = Inventory(cfg, scanner, prober=lambda r: control.probe(factory, r.port))
    await inv.start()
    await _settle(inv)
    try:
        assert inv.records[UID_A].state is DeviceState.NO_FIRMWARE
        # Backing off matters: without it a blank board would be rebooted every
        # two seconds forever.
        assert inv.records[UID_A].next_retry_at > 0
        assert inv.records[UID_A] not in inv.free_devices()
    finally:
        await inv.stop()


async def test_a_foreign_role_is_never_offered(cfg, factory, scanner):
    """The old RADIORELAY firmware does not accept !ECHO ON / !MODE, so the
    normalize sequence cannot be verified on it."""
    factory.boards[PORT_A] = FakeRelayFirmware(name="getez", serial="6a5d86c0",
                                               role="RADIORELAY")
    control = RelayControl(cfg)
    inv = Inventory(cfg, scanner, prober=lambda r: control.probe(factory, r.port))
    await inv.start()
    await _settle(inv)
    try:
        assert inv.records[UID_A].state is DeviceState.FOREIGN
        assert inv.records[UID_A] not in inv.free_devices()
    finally:
        await inv.stop()


# -- hot-plug ---------------------------------------------------------------
async def test_replug_on_a_different_port_keeps_the_same_record(inventory, scanner):
    """ttyACM renumbering is the normal case, which is why the uid is the key."""
    record = inventory.records[UID_A]
    assert record.port == PORT_A
    scanner.ports[UID_A] = PortInfo(uid=UID_A, device="/dev/fake-a-renumbered")

    await inventory.scan_once()

    assert inventory.records[UID_A] is record
    assert record.port == "/dev/fake-a-renumbered"
    assert record.device_name == "aaaaa", "identity was lost on replug"


async def test_unplug_marks_the_board_gone(inventory, scanner):
    del scanner.ports[UID_A]
    await inventory.scan_once()
    assert inventory.records[UID_A].state is DeviceState.GONE
    assert inventory.records[UID_A].port is None
    assert inventory.counts()["total"] == 1


async def test_replug_after_unplug_comes_back_free(inventory, scanner):
    saved = scanner.ports.pop(UID_A)
    await inventory.scan_once()
    scanner.ports[UID_A] = saved
    await inventory.scan_once()
    await _settle(inventory)
    assert inventory.records[UID_A].state is DeviceState.FREE


async def test_unplug_of_a_bound_board_notifies_the_session(inventory, scanner):
    record = inventory.records[UID_A]
    record.state = DeviceState.BUSY
    told = []
    inventory.on_device_gone = told.append

    del scanner.ports[UID_A]
    await inventory.scan_once()

    assert told == [record]


# -- allocation and policy --------------------------------------------------
async def test_reserve_is_single_winner(inventory):
    record = inventory.records[UID_A]
    assert inventory.reserve(record) is True
    assert inventory.reserve(record) is False, "a board was reserved twice"
    assert record.state is DeviceState.ACQUIRING


async def test_deny_list_excludes_a_board(cfg, factory, scanner):
    import dataclasses
    cfg = dataclasses.replace(cfg, devices=dataclasses.replace(cfg.devices,
                                                               deny=(UID_A,)))
    control = RelayControl(cfg)
    inv = Inventory(cfg, scanner, prober=lambda r: control.probe(factory, r.port))
    await inv.start()
    await _settle(inv)
    try:
        assert inv.records[UID_A].state is DeviceState.DISABLED
        assert [r.uid for r in inv.free_devices()] == [UID_B]
    finally:
        await inv.stop()


async def test_allow_list_excludes_everything_else(cfg, factory, scanner):
    import dataclasses
    cfg = dataclasses.replace(cfg, devices=dataclasses.replace(cfg.devices,
                                                               allow=(UID_B,)))
    control = RelayControl(cfg)
    inv = Inventory(cfg, scanner, prober=lambda r: control.probe(factory, r.port))
    await inv.start()
    await _settle(inv)
    try:
        assert [r.uid for r in inv.free_devices()] == [UID_B]
    finally:
        await inv.stop()


# -- caching ----------------------------------------------------------------
async def test_cached_identity_avoids_a_second_probe(cfg, factory, scanner):
    """Probing reboots the board, so a board we already know must be left alone."""
    control = RelayControl(cfg)
    inv = Inventory(cfg, scanner, prober=lambda r: control.probe(factory, r.port))
    await inv.start()
    await _settle(inv)
    await inv.stop()
    boots = {port: board.boot_count for port, board in factory.boards.items()}

    inv2 = Inventory(cfg, scanner, prober=lambda r: control.probe(factory, r.port))
    await inv2.start()
    await _settle(inv2)
    try:
        assert all(factory.boards[p].boot_count == n for p, n in boots.items()), \
            "a cached board was re-probed, which reboots it"
        assert inv2.records[UID_A].device_name == "aaaaa"
        assert inv2.records[UID_A].state is DeviceState.FREE
    finally:
        await inv2.stop()


async def test_a_busy_board_is_never_probed(inventory, factory):
    record = inventory.records[UID_A]
    record.state = DeviceState.BUSY
    before = factory.boards[PORT_A].boot_count
    inventory.rescan(force=True)
    await asyncio.sleep(0.1)
    assert factory.boards[PORT_A].boot_count == before


async def test_more_boards_than_probe_slots_probes_each_exactly_once(cfg, factory,
                                                                     scanner):
    """A board queued behind the concurrency semaphore must not be re-scheduled.

    Probing opens the port, which reboots the board. With more boards than probe
    slots the queued ones stayed UNKNOWN, so each scan queued another probe for
    them -- boards reset each other mid-HELLO and healthy relays reported
    no_firmware. Only reproducible with three or more boards, which is what the
    fleet host actually has.
    """
    import dataclasses

    from mbrelay.transport import PortInfo
    from fake_relay import FakeRelayFirmware

    for n in range(4):
        uid = f"99063602000528{n:02d}cafe2372c44f4f67000000006e052820"
        port = f"/dev/fake-{n}"
        scanner.ports[uid] = PortInfo(uid=uid, device=port, description="fake")
        # Slow enough that later boards genuinely queue on the semaphore.
        factory.boards[port] = FakeRelayFirmware(name=f"brd{n}", serial=str(n))
    factory.kwargs["latency"] = 0.05

    cfg = dataclasses.replace(cfg, devices=dataclasses.replace(
        cfg.devices, max_concurrent_probes=1, scan_interval_ms=20))
    control = RelayControl(cfg)
    inv = Inventory(cfg, scanner, prober=lambda r: control.probe(factory, r.port))
    await inv.start()
    await _settle(inv, timeout=15.0)
    try:
        assert all(r.state is DeviceState.FREE for r in inv.records.values()), \
            {r.name: str(r.state) for r in inv.records.values()}
        for port, board in factory.boards.items():
            assert board.boot_count == 1, (
                f"{port} was probed {board.boot_count} times; each probe reboots it")
    finally:
        await inv.stop()
