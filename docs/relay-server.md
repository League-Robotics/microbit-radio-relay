# micro:bit Relay Server (`mbrelay`)

**Status:** implemented — this document tracks the daemon in [`server/`](../server).
**See also:** [`radio-relay-protocol.md`](radio-relay-protocol.md) for the wire
protocol the boards speak, and [`announce.md`](announce.md) for the banner.

---

## 1. What it is and why

A relay is only useful to whoever is sitting at the machine it is plugged into.
`mbrelay` fixes that: it runs on the host with the boards and hands them out over
TCP. A client connects to the **pool port**, the daemon binds it to a free relay,
and from then on the socket is a transparent byte pipe to that board's serial
port.

The design target is a **drop-in replacement for opening the serial port
directly**. No client library, no framing, no handshake — `nc`, `pyserial` over a
socket, or a serial terminal pointed at a TCP port all work unchanged.

```
   client                    mbrelay (on the host with the boards)         radio
  ─────────►  TCP :8760  ►  ┌──────────────────────────────────┐  ►  other relays,
   nc / socket / pyserial   │  pool: pick a free relay          │     robots,
  ◄─────────             ◄  │  reset it, verify factory default │  ◄  MakeCode
                            │  then get out of the way          │     micro:bits
                            └──────────────────────────────────┘
```

## 2. The guarantee

When you are bound, you have a board that has **just been reset and verified at
factory defaults**: channel 0, group 10, RAW250, power 7, echo off, frag off.
The first thing you read is the board's announcement banner, exactly as a direct
serial open would give you.

When you disconnect, the daemon resets the board and restores those defaults
before anyone else can have it.

That last part is the reason this is a service and not a `socat` one-liner. The
relay firmware **persists its configuration in flash** across resets and
power-cycles ([protocol §2.1](radio-relay-protocol.md)), so a board left on
channel 23 with echo on comes back that way for the next user — silently, on the
wrong frequency. `mbrelay` normalizes on **both** release and acquire; the second
one is what makes it a guarantee rather than a hope, because a power cut or a
`SIGKILL` can prevent the first.

### What the socket cannot carry

**BREAK, DTR and RTS.** There is no way to express them over a plain TCP stream,
so a client cannot reset the board mid-session. This matters more than it sounds:
the relay's data plane has no in-band escape, so once you send `!GO`, a reset is
the *only* way back to the command plane.

The substitute is to disconnect and reconnect — and because the daemon resets and
re-verifies on every acquire, the reconnect lands on a freshly clean board. RFC
2217 would model the control lines properly; it is deliberately out of scope.

### No rate limiting

A client writing flat out at 115200 will overrun the board's USB receive buffer,
because the radio is far slower than the serial link. That is **identical to what
happens on a direct serial connection**, so reproducing it faithfully is correct.
Pace your writes; `scripts/relay_test.py` uses a 10 ms inter-frame gap for exactly
this reason.

### Which board you get

There is no way to request a specific board, but you usually get **the same one
back**. The pool remembers which boards each client address used recently and
prefers them, so per-robot work keeps the same hardware and its logs stay
comparable across sessions.

It is a preference, not a reservation: if your board is taken you get the
least-recently-used free one instead, so nothing blocks and wear stays spread
when nobody has a history.

Affinity is keyed on the client address **without** its source port (every
connection has a fresh ephemeral port, so keying on it would make affinity
useless), expires after `server.sticky_ttl_s`, and is capped at
`server.sticky_max_clients` entries so a public port cannot grow the table
without bound. Several clients behind one address share an affinity list capped
at the pool size, so they settle onto different boards rather than fighting over
one. Set `server.sticky_allocation = false` for strict least-used rotation.

### The release window

Releasing a board takes two to three seconds — the daemon has to close the port,
reopen it (which is what resets the board), confirm the command plane, restore
the settings and verify them. A client that reconnects the instant it disconnects
may be refused, because its old board is still being cleaned up.

This surprises test-script authors first and hardest. Either wait, or set
`server.acquire_wait_ms` so the connect blocks instead of failing.

## 3. When nothing is free

A byte pipe has no error channel, so the daemon says why in the relay's own
comment syntax and then closes:

```
$ nc relay-host 8760
# ERROR: no relay available (4 devices, 3 in use, 1 being handed back)
$
```

Any client that already ignores `#` lines — which is any client written against
the relay protocol — is unaffected, and a human gets a readable answer.

Note the two counts. A board that is *in use* is held by another client; a board
*being handed back* is mid-reset and belongs to nobody — it will be free in a
second or two. Lumping them together made it look as though a colleague had a
board when nobody did. Set
`server.reject_message = ""` to close with zero bytes instead.

### You do not always need `!GO`

`!GO` is a one-way door — the only way back to the command plane is to
disconnect and reconnect. For a single query that is a heavy way to do it, and
the command plane can already talk to the radio:

```
> ping            # send one line over the radio, no !GO needed
< pong            # anything received comes back on a "<" line
```

`HELLO` re-requests the board's announcement at any time, which is a quick way
to confirm which board you are holding.

Use `!GO` when you want a transparent byte stream. Use `>` for one-shot
request/response, which is most interactive use.

## 4. Using it

```bash
# Anything that speaks bytes works.
nc relay-host 8760

# Or the bundled terminal, which sets TCP_NODELAY for you.
mbrelay connect relay-host:8760

# Or let it find the host itself, which is what you want on a laptop.
mbrelay connect

# Or from Python, with no mbrelay dependency at all:
import socket
s = socket.create_connection(("relay-host", 8760))
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
print(s.recv(200))            # DEVICE:RADIOBRIDGE:relay:<name>:<serial>
s.sendall(b"!C 5\n!GO\n")     # channel 5, then transparent
s.sendall(b"hello over the radio")
```

Set `TCP_NODELAY`. Nagle delays small writes that follow one another closely,
which can add tens of milliseconds to a radio round-trip. Pacing writes a few
hundred milliseconds apart hides the effect, but it costs nothing to set.

### Finding the host without being told it

The daemon advertises itself over mDNS as `_mbrelay._tcp`, so nobody has to know
an address:

```
$ mbrelay discover
NAME     HOST            ADDRESS       PORT  VERSION
-------  --------------  ------------  ----  ------------
torture  torture.local   192.168.1.12  8760  0.20260826.9
agony    agony.local     192.168.1.19  8760  0.20260826.9

$ mbrelay connect
2 relay hosts found:
  1) torture      192.168.1.12:8760        0.20260826.9
  2) agony        192.168.1.19:8760        0.20260826.9
Which? [1-2, or q] 1
DEVICE:RADIOBRIDGE:relay:vevov:1779042496
```

With exactly one host it connects straight away; with none it says why and falls
back to `127.0.0.1:8760`, which is what `mbrelay connect` did before discovery
existed. **A typed target skips discovery entirely** — no browse, no delay. Use
`--discover NAME` to pick by advertised name instead of by address, and
`--no-discover` to never browse at all.

`mbrelay discover --probe` adds a `LIVE` column by TCP-connecting to each host.
That is the difference between "advertised" and "actually serving": a node whose
daemon is crash-looping still looks perfect in a plain browse.

The browser is stdlib — no `zeroconf`, no D-Bus, no new dependency. It binds an
**ephemeral** UDP port rather than 5353, which under RFC 6762 §6.7 obliges every
responder to answer by unicast straight back to it; that sidesteps the port
mDNSResponder and avahi-daemon already own. `_mbrelay._tcp` is not registered
with IANA, which is legal (RFC 6763 §7 allows 15 characters).

Discovery is a **convenience and never a prerequisite**. Publishing needs
`avahi-utils` on the node (`apt install avahi-utils`); without it the daemon logs
one warning at startup and serves boards exactly as before. On macOS the daemon
uses `dns-sd` instead, so a dev laptop advertises too.

### The radio is shared — check your channel

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

If you are driving a robot, agree a channel with whoever else is using the pool,
and check `mbrelay status` before you start.

## 5. Operating it

```bash
mbrelay serve                  # foreground; systemd-friendly, SIGTERM drains cleanly
mbrelay devices                # what is attached and what state it is in
mbrelay status --watch         # live sessions
mbrelay sessions
mbrelay kick s-3               # boot a session off a board
mbrelay reset <name> --force   # force one board back to factory defaults
mbrelay disable <name>         # take a board out of the pool
mbrelay events --follow        # stream daemon events
mbrelay discover               # relay hosts advertising themselves on the LAN
mbrelay flash --all-relays     # reflash every board (needs mbdeploy)
mbrelay config show            # merged config, and where each value came from
```

`mbrelay devices` works **without** the daemon running — it falls back to a
direct USB scan, which is exactly what you want when you are trying to work out
why the daemon sees nothing.

Exit codes are stable, so scripts can branch on them: `0` ok, `1` error, `2`
usage, `3` daemon not running, `4` device not found, `5` no free device, `6`
hardware/flash failure.

### Administration

The admin channel is a Unix domain socket speaking newline-delimited JSON,
mode `0660`. It is never exposed over TCP, and authorization is filesystem
permissions and nothing else.

## 6. How a board is identified

The key is the **DAPLink USB UID**, not the device path — `/dev/ttyACM*`
renumbers on every replug, and the UID does not. `pyserial` reports the same UID
that pyOCD and `mbdeploy` use, on both Linux and macOS, so it is the join key
across all the tooling.

> One trap worth knowing: a DAPLink UID is
> `board(4) family(4) hic(8) unique(16) pad(8) hic(8)`, and **both ends are
> shared** by every board with the same interface chip. All four micro:bits on
> the bench end in the identical `000000006e052820`. A tail slice names them all
> the same thing; the distinguishing field is in the middle. `mbrelay devices`
> prints that middle slice.

On Linux the installed udev rule also creates `/dev/microbit/<uid>`, which is
stable across replugs; the daemon prefers it when present.

Boards are classified by probing them once — opening the port and asking `HELLO`.
**Probing reboots the board**, so identity is cached and a board already known is
never re-probed, a bound board is never probed at all, and a board with no relay
firmware backs off to a five-minute retry rather than being rebooted every scan.

Only `RADIOBRIDGE` boards are offered. The older MakeCode `RADIORELAY` firmware
announces itself too, but does not accept `!ECHO ON` / `!MODE`, so the daemon
cannot verify it has restored a known state — and handing out a board it cannot
clean up would break the one promise the service makes.

## 7. Configuration

TOML, merged from `/etc/mbrelay/mbrelay.toml`, `/etc/mbrelay/conf.d/*.toml`, the
XDG config dir, `./mbrelay.toml`, `MBRELAY_*` environment variables, and CLI
flags, in that order of increasing precedence. `mbrelay config show` reports the
winning source for every key.

**Unknown keys are a startup error**, not a warning — a typo in an ops file must
not leave the service running with a silently different setting.

The knobs worth knowing:

| Key | Default | Why you would change it |
| --- | --- | --- |
| `server.port` | `8760` | The pool port. |
| `server.reject_message` | a `#` line | `""` closes with zero bytes instead. |
| `server.acquire_wait_ms` | `0` | Raise it for harnesses that reconnect instantly. |
| `server.preamble` | `banner` | `none` to send nothing and let the client `HELLO`. |
| `devices.allow` / `deny` | empty | Restrict which boards are offered. |
| `devices.labels` | empty | Friendly names, keyed by UID. |
| `mdns.enabled` | `true` | `false` on a network that forbids multicast. |
| `mdns.instance` | the hostname | The name `mbrelay discover` shows. |
| `mdns.service` | `_mbrelay._tcp` | Only to run two independent fleets on one LAN. |
| `mdns.publish_cmd` | `avahi-publish` | Falls back to `dns-sd` on macOS. |
| `state.shutdown_grace_s` | `20` | Must stay below the unit's `TimeoutStopSec`. |

## 8. Reflashing

`mbrelay flash` wraps [`mbdeploy`](https://github.com/Busboombot/mbdeploy), which
drives pyOCD over SWD. It takes the board out of the pool, kicks any session,
runs `mbdeploy probe` (required — mbdeploy resolves targets only against its own
registry), then deploys and puts the board back.

The daemon itself has **no runtime dependency** on mbdeploy or pyOCD; a host that
only serves relays does not need them, and `flash` fails with an install hint.

Two things here are silently fatal if you do them by hand:

* **`--force-relay` is required.** mbdeploy refuses to flash a board whose role
  contains `RELAY` or `BRIDGE`, which is every board here.
* **pyOCD reads `pyocd.yaml` from its working directory.** That file sets
  `chip_erase: chip`; without it pyOCD sector-erases, which fails on the
  nRF52833's MBR region at `0x0`. `mbrelay flash` sets the cwd for you and
  refuses to start if the file is missing.

## 9. Design notes

**asyncio, with the serial fd on `loop.add_reader`.** The argument is
correctness, not throughput. The registry is touched by allocation, hot-plug,
status and shutdown; under asyncio the `FREE -> ACQUIRING` transition is a
synchronous statement in the loop thread, so double-booking is impossible by
construction rather than by lock discipline. Killing a thread parked in
`serial.read()` on a vanished tty means closing its fd from another thread, which
hangs on macOS; `remove_reader()` plus an explicit close is deterministic. And
`pause_reading()`/`resume_reading()` gives real TCP-window backpressure for free.

Measured on a micro:bit V2 over `/dev/cu.*` on macOS: the reader callback fires
**1.3 ms** after a write.

**Verification uses a standalone `?`.** The obvious implementation scans the
replies to the settings commands for a config line. That is wrong: `!P` *also*
calls `printConfig()`, so the batch emits more than one config line and the
earlier one still shows the channel you are about to change. Matching it reports
a spurious failure, which then retries into a board that is mid-reply and
cascades into `# error: unknown command`. A separate `?` produces exactly one
line, describing the state that actually ended up on the board.

**Settings are sent one at a time.** `!MODE` reconfigures the radio
(`applyRadioConfig` disables the peripheral and spins on hardware flags with the
radio IRQ masked), and a burst arriving behind that lands in a board that is not
reading.

**Probing is scheduled, not just started.** Probes queue behind a concurrency
semaphore, and a record that stays `UNKNOWN` while queued gets a *second* probe
scheduled by the next scan. Since every probe opens the port and therefore
reboots the board, duplicates reset boards out from under each other and healthy
relays come back as `no_firmware`. The record is marked `PROBING` synchronously
at schedule time. This only shows up with three or more boards.

**Resetting a board is platform-specific, and Linux is the hard case.** This is
the single most surprising thing in the project. Measured on Ubuntu 24.04 against
DAPLink v0257 on a micro:bit V2:

| Attempted reset | macOS | Linux |
| --- | --- | --- |
| close + reopen the port | resets | **no effect** |
| explicit DTR pulse | — | no effect |
| DTR held low for 2 s | — | no effect |
| 1200-baud touch | — | no effect |
| RTS pulse | — | no effect |
| **break condition** | — | **resets, every time** |
| pyOCD `reset` over SWD | resets | resets |

On Linux the device does not even re-enumerate on the open, so nothing is
happening at all. A board parked in the data plane therefore stays deaf
indefinitely, and since the data plane has no in-band escape it looks exactly
like a board with no firmware — which is how an hour went into chasing a phantom
firmware fault before the measurements above settled it.

So `hello()` sends a **break** when nothing answers, and only then. The break
costs about 1.6 s, so keeping it off the happy path matters; a healthy board
never pays for it. `mbrelay flash` remains the last resort, since reflashing
resets the chip unconditionally.

Stopping the service cleanly (`systemctl stop`, i.e. `SIGTERM`) avoids stranding
boards in the first place: the daemon drains and restores every board it holds
before exiting. A `SIGKILL` skips that, which is how boards get stranded.

**`!DEFAULTS` is not enough on its own.** It clears the stored flash record; the
*live* configuration is untouched until the next reset. Normalizing therefore
sends explicit values. There is a unit test pinning this so a future
simplification fails loudly.
