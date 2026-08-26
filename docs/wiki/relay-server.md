---
title: Relay Server
blurb: Serve USB-attached relays over TCP, so a radio bridge is a socket away instead of a walk across the room.
order: 40
slug: relay-server
tags: [microbit, radio, relay, server, network, tcp]
---

# Relay Server

A relay is only useful to whoever is sitting at the machine it is plugged into.
The **relay server** (`mbrelay`) fixes that: it runs on the host with the boards
and hands them out over TCP. A client connects to the **pool port**, the daemon
binds it to a free relay, and from then on the socket is a transparent byte pipe
to that board's serial port.

The design target is a **drop-in replacement for opening the serial port
directly**. No client library, no framing, no handshake — `nc`, a serial terminal
pointed at a TCP port, or a few lines of `socket` code all work unchanged.

```
   client                    mbrelay (on the host with the boards)         radio
  ─────────►   TCP port  ►  ┌──────────────────────────────────┐  ►  other relays,
   nc / socket / terminal   │  pool: pick a free relay          │     robots,
  ◄─────────             ◄  │  reset it, verify factory default │  ◄  MakeCode
                            │  then get out of the way          │     micro:bits
                            └──────────────────────────────────┘
```

## What you are promised

When you are bound, you have a board that has **just been reset and verified at
factory defaults**: channel 0, group 10, RAW250, power 7, echo off, frag off.
The first thing you read is the board's announcement banner, exactly as a direct
serial open would give you.

When you disconnect, the server resets the board and restores those defaults
before anyone else can have it.

That last part is why this is a service rather than a `socat` one-liner. The
relay firmware **persists its configuration in flash** across resets and
power-cycles (see the [Protocol Reference](protocol)), so a board someone left on
channel 23 with echo on would come back that way for the next person — silently,
on the wrong frequency, with no error anywhere. The server normalizes on both
release *and* acquire, so a crash or a power cut cannot leave a dirty board in
the pool.

## Connecting

```bash
# Anything that speaks bytes works.
nc <host> <port>

# Or the bundled terminal, which sets TCP_NODELAY for you.
mbrelay connect <host>:<port>
```

From Python, with no dependency on `mbrelay` at all:

```python
import socket

s = socket.create_connection((HOST, PORT))
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

print(s.recv(200))            # DEVICE:RADIOBRIDGE:relay:<name>:<serial>
s.sendall(b"!C 5\n")          # channel 5, same grammar as over serial
s.sendall(b"!GO\n")           # from here every byte is radio payload
s.sendall(b"hello over the radio")
```

Set `TCP_NODELAY`. Without it, Nagle adds about 40 ms to every small write, which
roughly doubles the observed radio round-trip.

Ask your fleet administrator for the host and port. They are recorded on the
internal wiki, not here.

## Three things that will catch you out

**You cannot reset the board mid-session.** BREAK and DTR have no representation
in a TCP stream. This matters because the relay's data plane has no in-band
escape: once you send `!GO`, a reset is the *only* way back to the command plane.
The substitute is to **disconnect and reconnect** — and since the server resets
and re-verifies on every bind, the reconnect lands on a freshly clean board.

**Reconnecting instantly may be refused.** Releasing a board takes two to three
seconds: the server has to close the port, reopen it (which is what resets the
board), confirm the command plane, restore the settings and verify them. If your
script disconnects and immediately reconnects, its old board is still being
cleaned up. Wait a moment, or ask for the server's `acquire_wait_ms` to be raised
so the connect blocks instead of failing.

**There is no flow control.** Writing flat out at 115200 will overrun the board's
USB receive buffer, because the radio is much slower than the serial link. That
is exactly what happens on a direct serial connection too, so the server
reproduces it faithfully rather than papering over it. Pace your writes — a 10 ms
gap between frames is the usual remedy.

## When nothing is free

A byte pipe has no error channel, so the server says why in the relay's own
comment syntax and then closes the connection:

```
$ nc <host> <port>
# ERROR: no relay available (4 devices, 4 busy)
$
```

Any client that already ignores `#` lines — which is any client written against
the relay protocol — is unaffected, and a human gets a readable answer instead of
a silent hang-up.

## Running one

```bash
pip install microbit-relayd
mbrelay serve                  # foreground; SIGTERM drains and cleans up
```

Day-to-day operation:

```bash
mbrelay devices                # what is attached, and what state it is in
mbrelay status --watch         # live sessions
mbrelay kick s-3               # boot a session off a board
mbrelay reset <name> --force   # force one board back to factory defaults
mbrelay disable <name>         # take a board out of the pool
mbrelay events --follow        # stream server events
mbrelay flash --all-relays     # reflash every board
mbrelay config show            # merged config, and where each value came from
```

`mbrelay devices` works **without** the server running — it falls back to a
direct USB scan, which is what you want when you are trying to work out why the
server sees nothing.

Exit codes are stable, so scripts can branch on them: `0` ok, `1` error, `2`
usage, `3` server not running, `4` device not found, `5` no free device, `6`
hardware or flash failure.

On the League fleet the server is installed by Ansible and runs under systemd, so
`systemctl status mbrelay` and `journalctl -u mbrelay` are the first things to
try when something looks wrong.

## How boards are identified

The key is the **DAPLink USB UID**, not the device path — `/dev/ttyACM*`
renumbers on every replug, and the UID does not.

> A DAPLink UID is laid out as `board(4) family(4) hic(8) unique(16) pad(8)
> hic(8)`, and **both ends are shared** by every board carrying the same
> interface chip. Four micro:bits from the same batch will end in the identical
> sixteen characters. If you are matching UIDs by eye, use the middle — which is
> what `mbrelay devices` prints.

Identifying a board means opening its port and asking `HELLO`, and **opening the
port reboots the board**. So the server caches what it learns: a board it already
knows is never re-probed, a board someone is using is never probed at all, and a
board with no relay firmware backs off to a five-minute retry rather than being
rebooted every few seconds.

Only `RADIOBRIDGE` boards are offered. The older MakeCode `RADIORELAY` firmware
announces itself too, but does not accept `!ECHO ON` or `!MODE`, so the server
cannot confirm it has restored a known state — and handing out a board it cannot
clean up would break the one promise it makes.

On Linux an installed udev rule also creates `/dev/microbit/<uid>` symlinks, which
are stable across replugs.

## Reflashing

`mbrelay flash` reflashes boards in place, driving pyOCD over SWD via
[`mbdeploy`](https://github.com/Busboombot/mbdeploy). It takes the board out of
the pool, kicks any session on it, flashes, and puts it back.

The server itself has no runtime dependency on that toolchain — a host that only
serves relays does not need it installed.
