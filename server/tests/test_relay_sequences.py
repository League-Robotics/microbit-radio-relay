"""The command-plane sequences, against the fake firmware.

These pin the firmware behaviours the whole design rests on. If one of them
starts failing, the daemon is about to hand somebody a dirty board.
"""

from __future__ import annotations

import pytest

from mbrelay.errors import RelayError
from mbrelay.relay import DEFAULT_CFG, NORMALIZE_STEPS, Reader, RelayControl

from relay_fixtures import PORT_A
from fake_relay import FakeRelayFirmware, StoredConfig


@pytest.fixture
def control(cfg):
    return RelayControl(cfg)


async def _open(factory, control, port=PORT_A):
    channel = await factory.open(port)
    reader = Reader(channel)
    return channel, reader


async def test_hello_returns_identity(cfg, factory, control):
    channel, reader = await _open(factory, control)
    info = await control.hello(channel, reader)
    assert info.role == "RADIOBRIDGE"
    assert info.device_name == "aaaaa"
    assert info.serial == "1111111111"


async def test_hello_retries_when_the_boot_banner_is_missed(cfg, factory, control):
    """The usual case on real hardware: the banner goes out while we are still
    opening the port, so nobody ever sees it and HELLO has to ask again."""
    factory.boards[PORT_A] = FakeRelayFirmware(name="aaaaa", serial="1",
                                               drop_first_banners=3)
    channel, reader = await _open(factory, control)
    info = await control.hello(channel, reader)
    assert info.device_name == "aaaaa"


async def test_hello_gives_up_on_a_silent_board(cfg, factory, control):
    board = FakeRelayFirmware(drop_first_banners=99)
    board._handle = lambda line: None            # answers nothing at all
    factory.boards[PORT_A] = board
    channel, reader = await _open(factory, control)
    with pytest.raises(RelayError, match="no DEVICE banner"):
        await control.hello(channel, reader)


async def test_normalize_cleans_a_dirty_board(cfg, factory, control):
    """The case that matters: the last client left it on channel 20 with echo on."""
    factory.boards[PORT_A] = FakeRelayFirmware(
        name="aaaaa", serial="1",
        stored=StoredConfig(channel=20, group=10, power=3, mode="MAKECODE",
                            frag="ON", echo="ON"))
    channel, reader = await _open(factory, control)
    await control.hello(channel, reader)
    await control.normalize(channel, reader)

    board = factory.boards[PORT_A]
    assert (board.cfg.channel, board.cfg.group, board.cfg.mode, board.cfg.power) == \
        (0, 10, "RAW250", 7)
    assert board.cfg.echo == "OFF" and board.cfg.frag == "OFF"


async def test_channel_command_is_sent_last(cfg, factory, control):
    """!C forces group 10, so anything after it could undo that."""
    channel, reader = await _open(factory, control)
    await control.hello(channel, reader)
    await control.normalize(channel, reader)

    issued = [c for c in factory.boards[PORT_A].commands if c.startswith(b"!")]
    assert issued[-1] == b"!C 0"


async def test_verification_survives_the_extra_printconfig_from_P(cfg, factory, control):
    """!P also calls printConfig(), so the batch emits more than one config line
    and the earlier one still shows the OLD channel. Verifying by scanning those
    replies matched the stale line and wrongly reported failure -- which then
    retried into a board mid-reply and cascaded. Verification uses a standalone
    '?' precisely so this cannot happen."""
    board = FakeRelayFirmware(name="aaaaa", serial="1",
                              stored=StoredConfig(channel=17))
    factory.boards[PORT_A] = board
    channel, reader = await _open(factory, control)
    await control.hello(channel, reader)
    await control.normalize(channel, reader)          # must not raise

    config_lines = [c for c in board.commands if c == b"?"]
    assert config_lines, "normalize must verify with a standalone '?'"
    assert board.cfg.channel == 0


async def test_defaults_alone_does_not_change_live_state(cfg, factory, control):
    """Pins the firmware semantics documented in the protocol spec §2.1.

    !DEFAULTS clears the stored flash record; the live config is untouched until
    the next reset. Anyone tempted to "simplify" normalize() down to a single
    !DEFAULTS should fail here, loudly.
    """
    board = FakeRelayFirmware(name="aaaaa", serial="1",
                              stored=StoredConfig(channel=20, echo="ON"))
    factory.boards[PORT_A] = board
    channel, reader = await _open(factory, control)
    await control.hello(channel, reader)
    await control.clear_stored_config(channel, reader)

    assert board.flash is None            # the stored record is gone ...
    assert board.cfg.channel == 20        # ... but the board is still on ch 20
    assert board.cfg.echo == "ON"


async def test_reset_and_normalize_reopens_the_port(cfg, factory, control):
    """Release must reset the board, and reset means close AND reopen -- there is
    no other way out of the data plane."""
    board = factory.boards[PORT_A]
    board.plane = "data"
    before = board.boot_count

    await control.reset_and_normalize(factory, PORT_A)

    assert board.boot_count == before + 1, "the port was not reopened"
    assert board.plane == "command"
    assert (board.cfg.channel, board.cfg.mode) == (0, "RAW250")


async def test_probe_returns_none_for_a_board_with_no_firmware(cfg, factory, control):
    """A blank board or a robot is not an error -- it just is not a relay."""
    board = FakeRelayFirmware(drop_first_banners=99)
    board._handle = lambda line: None
    factory.boards[PORT_A] = board
    assert await control.probe(factory, PORT_A) is None


def test_normalize_steps_cover_every_persisted_setting():
    """The firmware persists channel, group, power, mode, frag and echo. Miss one
    and it leaks from one client to the next."""
    sent = b"".join(cmd for cmd, _ in NORMALIZE_STEPS)
    for expected in (b"!MODE", b"!FRAG", b"!ECHO", b"!P ", b"!C "):
        assert expected in sent
    assert DEFAULT_CFG == (0, 10, b"RAW250", 7)
