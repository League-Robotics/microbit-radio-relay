# mbrelay — micro:bit relay server

Serves USB-attached [micro:bit radio relays](../docs/relay-server.md) over TCP.

A client connects to the pool port, the daemon binds it to a free relay, and from
then on the socket is a **transparent byte pipe** to that board's serial port —
the same experience as opening `/dev/ttyACM0` directly, with no client library.
When the client disconnects, the daemon resets the board and restores factory
defaults, so nobody inherits the previous user's channel.

```bash
pip install microbit-relayd
mbrelay serve --port 8760          # on the machine with the boards
nc some-host 8760                  # from anywhere
curl some-host:8761/names          # where each robot is (the name registry)
```

## Quick tour

```bash
mbrelay devices                    # what is attached, and what state it is in
mbrelay status                     # daemon health and live sessions
mbrelay discover                   # relay hosts advertising themselves on the LAN
mbrelay connect                    # a terminal on a relay, host found by itself
mbrelay connect host:8760          # or name it, which skips discovery entirely
mbrelay connect tovez              # a terminal on the ROBOT tovez, wherever it is
mbrelay names                      # the name registry: where each robot is
mbrelay names set tovez 12/4       # move a robot off its derived channel
mbrelay flash --all-relays         # reflash every board (needs mbdeploy)
mbrelay kick s-3                   # boot a session off a board
mbrelay reset vevov                # force one board back to defaults
```

## What the socket guarantees

* You are bound to a board that has just been **reset and verified at factory
  defaults**: channel 0, group 10, RAW250, power 7, echo off, frag off.
* The board's announcement banner is replayed to you as the first thing you read,
  matching what a direct serial open would show.
* After that, every byte is passed through untouched in both directions.

One thing a socket cannot carry: **BREAK/DTR**. Resetting a relay means closing
and reopening its port, which for you means disconnecting and reconnecting — and
the daemon guarantees the reconnect lands on a freshly reset board. That matters
because the relay's data plane has no in-band escape: once you send `!GO`, a
reset is the only way back to the command plane.

Note that release takes two to three seconds (the daemon has to reset and
re-verify the board). A client that reconnects instantly may be rejected because
the board is still being cleaned up. Set `server.acquire_wait_ms` or use
`mbrelay connect --wait` if your test harness does that.

## Development

```bash
uv venv .venv && uv pip install --python .venv -e '.[dev]'
.venv/bin/pytest -m 'not hil'      # no hardware needed
.venv/bin/pytest -m hil            # needs two relays attached
```

The hardware-free tests run against `tests/fake_relay.py`, a state machine whose
replies are copied verbatim from `source/relay/RadioRelay.cpp`.
