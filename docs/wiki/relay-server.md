---
title: Relay Server
blurb: Serve USB-attached relays over TCP, so a radio bridge is a socket away instead of a walk across the room — and connect to a robot by name.
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

You do not choose which board you get, but you usually get **the same one back**:
the pool remembers which boards your machine used recently and prefers them. That
keeps per-robot work on the same hardware so its logs stay comparable.

It is a preference, not a reservation. If your board is taken you get another —
the least recently used one — so nothing blocks and wear stays spread. Read the
banner to see which you got.

That last part is why this is a service rather than a `socat` one-liner. The
relay firmware **persists its configuration in flash** across resets and
power-cycles (see the [Protocol Reference](protocol)), so a board someone left on
channel 23 with echo on would come back that way for the next person — silently,
on the wrong frequency, with no error anywhere. The server normalizes on both
release *and* acquire, so a crash or a power cut cannot leave a dirty board in
the pool.

## Connecting

```bash
# Name the ROBOT. The server works out where it is and which board to use.
mbrelay connect tovez

# You do not have to know the address either: the servers announce themselves.
mbrelay connect

# Or name one. A typed address skips discovery entirely.
mbrelay connect <host>:<port>
mbrelay connect tovez@<host>      # a robot, through a named server

# Anything that speaks bytes works, too.
nc <host> <port>
```

`mbrelay connect tovez` is the one to reach for. You name the robot, not a
channel, a group, a board, a host or a port: the server looks tovez up in its
**name registry** (below), takes whichever relay is free, tunes it, and drops
you into the data plane talking to the robot.

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

Set `TCP_NODELAY`. Nagle delays small writes that follow one another closely,
which can add tens of milliseconds to a radio round-trip. If you pace your
writes a few hundred milliseconds apart you will not notice the difference —
but it costs nothing to set, and it matters as soon as you send back-to-back.

### Which server?

`mbrelay discover` lists every relay server advertising itself on your network:

```
$ mbrelay discover
NAME     HOST            ADDRESS       PORT  VERSION
-------  --------------  ------------  ----  ------------
torture  torture.local   192.168.1.12  8760  0.20260826.9
agony    agony.local     192.168.1.19  8760  0.20260826.9
```

`mbrelay connect` with no address does the same lookup for you: one server and it
connects straight away, several and it asks which. Add `--probe` to `discover` to
see which of them is actually answering on its port, as opposed to merely
advertising.

If nothing is listed you are probably on a different network from the servers —
discovery is link-local and does not cross a router or most VPNs. Name the
address instead (`mbrelay connect 192.168.1.12:8760`); everything else works the
same.

## You do not always need `!GO`

`!GO` is a one-way door — the only way back to the command plane is to
disconnect and reconnect. For a single query that is a heavy way to do it, and
the command plane can already reach the radio:

```
> ping            # send one line over the radio, no !GO needed
< pong            # anything received arrives on a "<" line
```

`HELLO` re-requests the board's announcement at any time, which is the quick way
to confirm which board you are holding.

Use `!GO` when you want a transparent byte stream; use `>` for one-shot
request/response, which covers most interactive use. `!HELP` on the board lists
the rest of the grammar.

## Three things that will catch you out

**You cannot reset the board mid-session.** BREAK and DTR have no representation
in a TCP stream. This matters because the relay's data plane has no in-band
escape: once you send `!GO`, a reset is the *only* way back to the command plane.
The substitute is to **disconnect and reconnect** — and since the server resets
and re-verifies on every bind, the reconnect lands on a freshly clean board.

**Reconnecting instantly may be refused when the pool is full.** Releasing a board takes two to three
seconds: the server has to close the port, reopen it (which is what resets the
board), confirm the command plane, restore the settings and verify them. If your script disconnects and immediately reconnects it normally gets a
*different* board and succeeds — the problem only appears when every other
board is taken and it needs its own back. Wait a moment, or ask for the
server's `acquire_wait_ms` to be raised so the connect blocks instead of
failing.

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
# ERROR: no relay available (4 devices, 3 in use, 1 being handed back)
$
```

Any client that already ignores `#` lines — which is any client written against
the relay protocol — is unaffected, and a human gets a readable answer instead of
a silent hang-up.

Note the two counts. A board that is *in use* is held by another client; a board
*being handed back* is mid-reset and belongs to nobody — it will be free in a
second or two. Lumping them together made it look as though a colleague had a
board when nobody did.

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

## The name registry — where a robot actually is

A micro:bit's five-letter name derives a `(channel, group)` all by itself (see
the [Protocol Reference](protocol)), which is how `mbrelay connect tovez` works
with no configuration at all. But the mapping has 3125 names and only **25 channels**,
so 125 names share each one — `togov` and `vevov` both derive channel 37. When
two robots collide you have to move one, and its name then no longer says where
it is.

So the derived pair is a **default**, and the registry is what records the
exceptions. Asking it about a name always answers:

```bash
mbrelay names                          # everything on record
mbrelay names get tovez                # where tovez is
mbrelay names set tovez 12/4           # move it
mbrelay names clear tovez              # back to its derived address
```

A name nobody has ever asked about is not an error — its default is computed
and recorded on the spot, so `mbrelay names` really is the list of every robot
this relay knows about. Only a *malformed* name is refused: `pipip` is a legal
address nobody happens to be on, while `robot1` has no address at all.

Over HTTP, on `registry.port` (8761 by default), because the people who need
this are usually not on the relay host — building a robot's config, or running
a channel survey across the fleet:

```
GET    /names            every association, and who shares a link
GET    /names/<name>     where that robot is; creates the record on a miss
PUT    /names/<name>     {"channel": 12, "group": 4}
DELETE /names/<name>     back to the derived address
GET    /status           version and counts
```

**There is no authentication.** This is an internal lab service whose entire
content is which radio channel a robot sits on, which anyone with an antenna can
determine anyway. Do not expose the port beyond your LAN. If a node should keep
the registry to itself, set `registry.bind = "127.0.0.1"`.

Three layers answer a lookup, highest first:

| source     | where                          | when                          |
| ---------- | ------------------------------ | ----------------------------- |
| `config`   | `[registry.names]` in the TOML | pinned; a survey's output     |
| `registry` | `<state.dir>/names.json`       | set through the API or CLI    |
| `derived`  | the name itself                | computed, then recorded       |

A config pin outranks anything set through the API — that is the point of it, so
a restart cannot quietly reinstate a stale learned value — and `mbrelay names
set` refuses to shadow one rather than pretending to succeed. Changing a pin
needs a daemon restart.

Two robots may end up on one link. The registry **reports** that rather than
refusing it: a survey is expected to pass through a clash halfway, and an
operator moving robots by hand needs to see it rather than be stopped by it.

> **Moving a robot is a two-sided change.** A robot derives its own address from
> its own name at boot, so it has to be reconfigured to match (the deploy-time
> channel and group constants in pxt-nezha-diffdrive). The registry only tells
> the relay where to tune; it cannot move a robot.

The relay firmware knows nothing about any of this — it is told a channel and a
group with `!CG` and does as it is asked. That is deliberate: a tune-by-name
command on the board would compute the *derived* pair and so mistune exactly the
robots that were moved because they had a problem. It also means
`mbrelay connect <robot>` works against **every** firmware version in the fleet,
since `!CG` is as old as the protocol. If the registry cannot be reached at all,
`mbrelay connect` falls back to the derived address and says so on stderr —
never silently.

## The radio is shared — check your channel

Four boards means four simultaneous users, and they all transmit into the same
air. **Two clients that pick the same channel will hear each other's robots, and
each robot will act on the other's commands.** Nothing in the service prevents
this: a shared channel is sometimes exactly what you want, and the data plane is
a transparent pipe with no place to interpose.

What the server does do is make it visible. The channel each client selects shows
up in `mbrelay status`, and a collision is logged:

```
channel_collision channel=4 sessions=s-2(10.0.0.9:52344), s-5(10.0.0.14:41022)
```

If you are driving a robot, check `mbrelay status` before you start and agree a
channel with whoever else is using the pool.

Two *robots* can also collide, which is a different problem: 125 names derive
each channel, so `togov` and `vevov` both land on 37 no matter who is driving
them. Agreeing anything cannot fix that, because the address comes from the
name. That is what the name registry is for — move one of them and record where
it went.

## If a board stops answering

A board can end up stranded in the data plane — for example if the server was
killed outright rather than stopped cleanly, so it never got to reset the board
it was holding. A stranded board answers `HELLO` with silence, because in the
data plane your text is radio payload rather than a command.

The server recovers this by itself: when a board does not answer, it sends a
**break condition**, which reboots it. That fallback exists because resetting a
micro:bit turns out to be platform-specific — closing and reopening the serial
port resets the board on macOS but does nothing at all on Linux, which is what
the fleet runs. (Neither does a DTR pulse, nor a 1200-baud touch. A break does,
every time.)

If a board still will not answer, reflash it: that resets the chip
unconditionally. And prefer `systemctl stop mbrelay` over killing the process —
`SIGTERM` lets the daemon hand every board back cleanly first.

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
