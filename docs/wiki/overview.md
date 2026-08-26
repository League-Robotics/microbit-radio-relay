---
title: Overview
blurb: What the radio relay is, how it bridges serial to the nRF radio, and where to dig deeper.
order: 10
slug: overview
tags: [microbit, radio, relay, firmware]
---

# micro:bit Radio Relay

The radio relay is a **micro:bit V2 running C++/[CODAL](https://github.com/lancaster-university/codal) firmware** that bridges a host
serial port (USB CDC, 115200 baud) to the on-board nRF radio. It forwards data in
both directions and exposes a small line-oriented command grammar for
configuration. Any "dumb" serial client can drive it — a bash `echo`/`cat`, a
Python `pyserial` script, or a serial terminal. **No special host library is
required.**

```
  Host (USB serial, 115200)            micro:bit relay              Peer radios
  ───────────────────────────►   ┌──────────────────────┐   ◄───────────────────►
   echo / cat / pyserial / term  │  command + data plane │   other relays, robots,
                                 │   nRF radio bridge    │   MakeCode micro:bits
  ◄───────────────────────────   └──────────────────────┘
```

## The two planes

The relay has two operating **planes**, and it always boots into the first one:

- **Command plane** — a line-oriented configuration grammar (`!C`, `!MODE`,
  `!ECHO`, `?` …). The radio is already live here: `> text` sends one line and a
  `<`-prefixed line is something received, so you can test before committing.
- **Data plane** — entered with `!GO`. From then on every serial byte is
  transparent radio payload, framed and chunked in both directions. There is no
  in-band escape sequence; the **only** way back to the command plane is a reset
  (close and reopen the port). That is what keeps the data plane fully
  transparent — no byte in the stream is reserved.

See the [Protocol Reference](protocol) for the full grammar and framing.

If the board you want is plugged into a different machine, the
[Relay Server](relay-server) hands relays out over TCP: the socket behaves
exactly like a local serial port, and every client gets a board reset to
factory defaults.

## The two radio modes

A single firmware speaks to two different kinds of peer; it switches the on-air
packet size at runtime when you change mode:

- **RAW250** *(default)* — headerless payloads up to 250 bytes to a peer running
  C++/CODAL or MicroPython built with the matching packet size. Maximum
  throughput, but a stock MakeCode micro:bit cannot participate.
- **MAKECODE** — 32-byte CODAL-compatible packets so a stock MakeCode micro:bit
  receives them on `on received string` with no custom code. The relay builds the
  full CODAL/PXT header itself.

Both ends of a RAW250 link must be built with the same
`MICROBIT_RADIO_MAX_PACKET_SIZE` (this firmware uses **250**) or the larger
packets are silently dropped on receive.

## What persists

Only the *plane* is reset-volatile — every boot starts in the command plane. The
**configuration persists**: `channel`, `group`, `power`, `mode`, `frag`, and
`echo` are saved to flash on every change and restored at boot, surviving both a
reset and a power-cycle. A board configured once (e.g. "RAW250 echo on channel 1")
comes back exactly that way when replugged. `!DEFAULTS` clears the saved record.

## Host-free operation

The relay is useful without a host attached:

- **Buttons A / B** step the channel down / up (when on group 10).
- **A+B chord** opens a small on-display menu to toggle packet mode and echo.
- **Echo / transponder mode** bounces every received message back over the radio,
  so a lone board becomes a round-trip test target.

## Where to go next

- **[Using the Relay](using-the-relay)** — flashing, opening the port, the
  command quick-start, buttons, echo, and worked host examples.
- **[Protocol Reference](protocol)** — the complete command grammar, radio
  framing modes, the data-plane streaming layer, and the device announcement.

The firmware lives in `source/relay/RadioRelay.cpp`; the canonical written spec
this wiki tracks is `docs/radio-relay-protocol.md` in the repository.
