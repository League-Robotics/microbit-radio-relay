---
title: Protocol Reference
blurb: The complete relay protocol — command grammar, radio framing modes, the data-plane streaming layer, and the device announcement.
order: 30
slug: protocol
tags: [microbit, radio, relay, protocol, specification]
---

# micro:bit Radio Relay Protocol

**Version:** 1.0 · **Target firmware:** C++ / CODAL on micro:bit V2 · **Status:**
implemented. This page tracks the firmware in `source/relay/RadioRelay.cpp`; the
in-repo source of truth is `docs/radio-relay-protocol.md`.

New here? Start with the [Overview](overview) and [Using the Relay](using-the-relay).

---

## 1. Purpose and scope

The radio relay is a micro:bit running C++/CODAL firmware that bridges a host
serial port (USB CDC, 115200 baud) to the nRF radio. It forwards data in both
directions and exposes a small line-oriented command grammar for configuration.
Any "dumb" serial client — a bash `echo`/`cat`, a Python `pyserial` script, a
serial terminal — can drive it; no special host library is required.

It supports two radio framing modes:

- **RAW250** *(default)* — emits headerless payloads up to 250 bytes to a peer
  running C++/CODAL or MicroPython configured with the matching packet size. No
  CODAL/MakeCode header; the relay passes payload through the §5 framing layer
  directly. A stock MakeCode robot cannot participate in this mode.
- **MAKECODE** — emits 32-byte CODAL-compatible packets so a stock MakeCode
  robot receives them on `on received string` with no custom code. The relay
  constructs the full CODAL/PXT header itself.

The firmware is compiled with `MICROBIT_RADIO_MAX_PACKET_SIZE = 250` (set in
`codal.json`). This is the radio packet size and the value the mode name tracks;
CODAL caps it at 250, and **both ends must be built with the same value** or the
larger packets are dropped on receive. MAKECODE mode constrains the payload to the
32-byte CODAL layout at runtime; it does not require a separate build. The relay
switches the on-air `PCNF1.MAXLEN` (32 vs. 250) at runtime when the mode changes,
so a single firmware speaks to both stock MakeCode robots and RAW250 peers.

> **Naming:** earlier drafts called the wide mode RAW251 (after a theoretical
> 254 − 3 PHY ceiling). The configurable CODAL value is 250, so the mode is
> **RAW250**; `!MODE RAW251` is accepted as a backward-compatible alias.

### Out of scope

- The ESP8266 / Espressif AT command set. The ESP is downstream of the *robot*,
  configured once by the robot at boot over the robot's own serial line. The host
  and the relay never see AT traffic, and radio payloads cannot collide with it.

---

## 2. Physical and lifecycle model

The relay has two **planes**:

- **Command plane** — line-oriented configuration grammar (§3). The radio is
  already live here: `> text` sends and `<` receives work before `!GO`.
- **Data plane** — entered with `!GO`; serial bytes become transparent radio
  payload, framed and chunked both ways (§5). There is no in-band escape sequence
  (no `+++`, no guard timing). The only way out of the data plane is a reset —
  close and reopen the port. This is what keeps the data plane fully transparent:
  no byte in the stream is reserved.

```
host opens serial port
  -> relay resets, restores saved config from flash, boots into COMMAND plane,
     emits DEVICE banner
host queries/configures: ?, !MODE RAW250, !CG 5 42, !P 6, !ECHO ON ...
host sends !GO
  -> relay re-applies radio config (disable/reconfigure/re-enable), enters DATA plane
DATA plane: bytes are transparent payload, framed + chunked to radio, both ways
host closes port  ->  next open = reset = back to COMMAND plane (config preserved)
```

### 2.1 What resets and what persists

Only the **plane** is reset-volatile: every boot starts in the command plane,
never the data plane. The **configuration** persists.

`channel`, `group`, `power`, `mode`, `frag`, and `echo` are saved to flash (via
`uBit.storage` / `KeyValueStorage`, key `"relaycfg"`) on every explicit change and
reloaded at boot. They survive both a reset **and** a power-cycle, so a board
configured once (e.g. "RAW250 echo on channel 1") comes back exactly that way when
replugged or repowered. Writes are skipped when the value is unchanged, to spare
the flash erase budget. Use `!DEFAULTS` to clear the saved record and fall back to
the compiled-in defaults on the next reset (§3.6).

> **Reset mechanics — and they are platform-specific.**
>
> On **macOS**, closing **and reopening** the port resets the board: the open
> toggles DTR, and DAPLink resets its target on that transition. An in-place DTR
> toggle on an already-open port does *not* reset, so the host must fully close
> and reopen. After reopen, send `HELLO` and wait for the banner to confirm the
> command plane — the boot banner emitted during the closed window is missed. See
> `scripts/relay_test.py` (`reset_to_command`).
>
> On **Linux this does not work at all.** Measured on Ubuntu 24.04 against
> DAPLink v0257 on a micro:bit V2: close/reopen, an explicit DTR pulse, holding
> DTR low for two seconds, a 1200-baud touch, and an RTS pulse *all* leave the
> target running. The device never even re-enumerates. A board parked in the data
> plane therefore stays deaf indefinitely, and since the data plane has no in-band
> escape, nothing short of a reflash recovers it.
>
> **A break condition does reset it, reliably** — verified on four boards, every
> attempt. So a host that needs to guarantee it can reach the command plane must
> send a break when `HELLO` goes unanswered rather than trusting the reopen.
> `mbrelay` does exactly that (see [Relay Server](relay-server)).

---

## 3. Serial command plane

Line-oriented, `\n`-terminated (a trailing `\r` is trimmed), valid only before
`!GO`. Baud is 115200.

### 3.1 Prefix characters

| Prefix | Direction     | Meaning                                           |
| ------ | ------------- | ------------------------------------------------- |
| `>`    | host → relay  | Send rest of line over radio (command plane only) |
| `<`    | relay → host  | A message received from radio                     |
| `!`    | host → relay  | Command (see 3.2)                                 |
| `?`    | host → relay  | Query current config                              |
| `#`    | relay → host  | Comment / status / debug from the relay           |

`>` in the command plane preserves single-line send for quick testing and for
interoperability checks (the radio is live before `!GO`). Bulk/transparent sending
is done in the data plane after `!GO`, with no prefix.

### 3.2 Commands

| Command            | Description                                                          |
| ------------------ | ------------------------------------------------------------------- |
| `!C <ch>`          | Set channel (0–35), forces group 10. Display shows the channel glyph. |
| `!CG <ch> <group>` | Set channel (0–83) and group (0–255). Display shows `?`.            |
| `!RC <ch> <group>` | Alias of `!CG`.                                                     |
| `!N <name>`        | Set channel and group from a micro:bit name (§3.7). Display shows `?`. |
| `!P <0-7>`         | Set transmit power.                                                 |
| `!MODE MAKECODE`   | Select 32-byte CODAL framing.                                       |
| `!MODE RAW250`     | Select headerless ≤250-byte framing (default). `RAW251` accepted as alias. |
| `!FRAG ON\|OFF`    | MAKECODE over-length policy: fragment vs. truncate (default OFF).   |
| `!ECHO [ON\|OFF]`  | Transponder: bounce every received message back over radio. Bare `!ECHO` toggles. |
| `!DEFAULTS`        | Clear the saved config; compiled-in defaults apply on next reset.  |
| `!GO`              | Leave command plane, enter data plane. Exit only via reset.        |
| `!HELP`            | Print protocol summary.                                            |
| `HELLO`            | Re-request the device announcement banner.                         |

Config changes (`!C`, `!CG`/`!RC`, `!N`, `!P`, `!MODE`, `!FRAG`, `!ECHO`) are applied
immediately, persisted to flash (§2.1), and echoed back as a `#` comment.

**Debug commands** (compiled in by default; the whole facility is stripped when
the firmware is built with `RELAY_DEBUG=0`):

| Command          | Description                                                       |
| ---------------- | ----------------------------------------------------------------- |
| `!DEBUG ON\|OFF` | Toggle `# DBG ...` radio TX/RX logging (off at boot, no reflash). |
| `!DEBUG?`        | Report whether debug logging is on.                               |
| `!REGS`          | Dump the live nRF `RADIO` registers as `#` comments (tuning/diagnostics). |

Every debug line is a `#`-prefixed comment, so a host parser that ignores `#`
lines is unaffected when debug is on.

### 3.3 Queries

| Query     | Response (relay → host, `#`-prefixed)                   |
| --------- | ------------------------------------------------------- |
| `?`       | `# channel: <ch> group: <g> mode: <m> power: <p>`       |
| `!MODE?`  | `# mode: MAKECODE` or `# mode: RAW250`                  |
| `!N?`     | `# name: <name>`, or `# name: -` if the link was not chosen by name (§3.7) |
| `!DEBUG?` | `# debug: ON` or `# debug: OFF` (debug build only)      |

Query support matters because the host cannot otherwise see relay state. Even
though config persists across resets (§2.1), after opening the port the host
should read config back rather than assume.

### 3.4 Boot announcement

```
DEVICE:RADIOBRIDGE:relay:<deviceName>:<serialNumber>
```

`<deviceName>` is the CODAL friendly name; `<serialNumber>` is the nRF serial.
Emitted on boot and on `HELLO`. The full field-by-field specification (including
the per-firmware serial encoding and a tolerant parsing regex) is in
`docs/announce.md` in the repository.

> Earlier MakeCode/TypeScript relays announced as `DEVICE:RADIORELAY:relay:...`.
> This C++ firmware uses `RADIOBRIDGE`; host tooling that auto-classifies boards
> keys off this difference.

### 3.5 Buttons

Buttons work without a host, in either plane.

**A / B — channel control:**

- **A** — channel down, **B** — channel up, wrapping within 0–35.
- Active only when `group == 10` (a custom-group link is left undisturbed) and
  when the A+B menu is not open.
- The new channel is applied immediately, persisted, shown on the display with the
  glyph mapping below, and echoed to the host as `# channel: <ch> group: 10`.

**A+B — mode menu:** the A+B chord opens a small menu. Each press advances to the
next item; resting on an item for 3 seconds accepts it. Each item shows the
**opposite** of the current state (i.e. the change you would make):

| Item | Display when chosen advances to            | Action on accept                |
| ---- | ------------------------------------------ | ------------------------------- |
| 0    | `3` (→ 32-byte MAKECODE) / `2` (→ 250-byte RAW250) | toggle packet mode, persist |
| 1    | ghost icon (→ echo) / west-arrow (→ transmit/receive) | toggle echo mode, persist |
| 2    | `X` (cross icon)                           | cancel — no change              |

On accept, a check icon flashes briefly; on cancel the display just returns to
rest. The menu is the host-free equivalent of `!MODE` and `!ECHO`.

### 3.6 Defaults and display

On a fresh board (or after `!DEFAULTS`) the relay boots on **channel 0 (`0`),
group 10, power 7, mode RAW250, FRAG off, echo off**. A board that has been
configured restores its saved values instead (§2.1).

> Channel 0 is intentional: the old MakeCode/TypeScript relays also boot on
> channel 0, so this firmware lands on the same default link as the hardware it
> replaces.

The 5×5 display reflects state:

- **Resting:** the channel glyph — `0`–`9` for channels 0–9, `A`–`Z` for 10–35,
  `?` for a custom-group config — or a **ghost icon** when echo mode is on.
- **Boot animation:** echo-state icon (ghost / west-arrow) → flash the packet-size
  mode (`32` for MAKECODE, `25` for RAW250) → channel glyph → settle to the
  resting display. This reflects the persisted config restored at boot.
- **Entering the data plane:** a single `.`.

### 3.7 Named links

A micro:bit's five-letter name **is** its radio address: the CODAL friendly
name is `NRF_FICR->DEVICEID[1]` written in base 5, so a robot derives its own
`(channel, group)` at boot, and anyone who knows the name derives the same pair
— no registry, no allocation. `!N <name>` retunes the relay to that pair:

```
!N tovez
# channel: 55 group: 108 mode: RAW250 power: 7 name: tovez
```

The name is trimmed and lower-cased (`TOVEZ`, ` tovez ` and `tovez` are one
board) and must match exactly `[zvgpt][uoiea][zvgpt][uoiea][zvgpt]`; anything
else is answered with `# error: usage !N <name>`. The link is applied and
persisted exactly like `!CG`, and the name is persisted with it, so `?` keeps
reporting `name: <name>` after a reset and `!N?` answers `# name: tovez`. `!C`,
`!CG`/`!RC` and the A/B buttons choose a link by *number* and therefore forget
the name (`!N?` then answers `# name: -`).

`name:` is always the **last** field of the config line, so a parser anchored on
`# channel:` — every existing one — is unaffected.

**The mapping** is owned by pxt-nezha-diffdrive (`docs/radio-addressing.md`,
normative, with the machine-readable `docs/radio-address-vectors.json`). This
firmware (`nameToRadio` in `source/relay/RadioRelay.cpp`) and the server (`server/src/mbrelay/naming.py`) implement the same
steps; the server's tests mirror the vectors file and check the **entire
3125-name space** against its published sha256 rather than a sampled table.

```
positions 0, 2, 4   consonant   z v g p t   = 0 1 2 3 4
positions 1, 3      vowel       u o i e a   = 0 1 2 3 4

n       = base5(name)          # name[0] is the MOST significant digit, 0–3124
channel = 25 + 2 * (n mod 25)  # 25, 27, … 73
group   = 1 + (n div 25)       # then: if group ≥ 10, group = group + 1 → 1–9, 11–126
```

Every intermediate is 0–3124, so MakeCode int32, C++ `int` and Python agree
with no shifts or negative modulo. The map is a bijection: 3125 names → 3125
distinct pairs, 125 names per channel, 25 per group. It **never emits channels
3, 4 or 7, nor groups 0 or 10** — channels 3/4 with group 10 are the legacy
hand-allocated fleet, band 7 with group 0 is MakeCode's unconfigured default,
and group 10 is this relay's `!C`/button space (§3.5). So a hand-dialled `!C`
can never land on a derived link, `?` on the display always means a custom or
named link — and `!C` cannot reach a named board at all; only `!CG` or `!N` can.

| name    | n    | channel | group | board                           |
| ------- | ---- | ------- | ----- | ------------------------------- |
| `zeguz` | 425  | 25      | 19    | robot (channel 25 is inclusive) |
| `zetuv` | 476  | 27      | 21    | robot                           |
| `vevov` | 1031 | 37      | 43    | robot                           |
| `gopiv` | 1461 | 47      | 60    | bench rig                       |
| `getez` | 1740 | 55      | 71    | relay                           |
| `zavaz` | 545  | 65      | 23    | relay                           |
| `tovez` | 2665 | 55      | 108   | robot                           |

A relay has no address of its own — it adopts the robot's, so `getez` serving
`tovez` tunes to 55/108 and its own pair never goes on air. Two *robots* on one
channel (`vevov` and `togov` both derive 37) contend for airtime even though
neither parses the other's traffic; the group is only an address filter.

> **Endianness.** Base-5 conversion naturally emits the least significant digit
> first, but the name is big-endian. A reversed encoder still yields 3125
> well-formed, distinct names — silently wrong. `zuzuv` is n = 1 (a reversed
> encoder says `vuzuz`); palindromes like `zuzuz` or `zavaz` cannot catch it.

---

## 4. Radio framing modes

### 4.1 RAW250 mode (headerless, ≤250 bytes) — default

No CODAL header. The payload handed to the radio is whatever the §5 framing layer
produces, up to 250 bytes — `MICROBIT_RADIO_MAX_PACKET_SIZE`, which CODAL caps at
250. The peer must be C++/CODAL or MicroPython with a matching packet size. A
stock MakeCode robot cannot participate in this mode.

### 4.2 MAKECODE mode (32-byte CODAL string packet)

The relay constructs the full CODAL/PXT header so a stock MakeCode robot fires
`on received string`. Layout of the radio payload the relay builds (CODAL prepends
its own 4-byte radio header — version/group/protocol — on the air, which is the
`01 <grp> 01` the diagram shows ahead of the type byte):

```
[type:1=0x02] | TS TS TS TS | SN SN SN SN | LEN | <string bytes...>
    typ          \_timestamp_/  \_serial #_/  len   up to 19 bytes
     1                4              4          1        ≤19
```

- **Type** `02` = string. (Number `00` and value `01` are not emitted in this
  mode — see decision below.)
- **Timestamp** (4): left zero. A receiving MakeCode robot does not use it for
  `on received string`.
- **Serial** (4): left zero (serial-number sending disabled), conventional default.
- **Length** (1): number of string bytes that follow.
- **String** (≤19): UTF-8 bytes of the line.

**Send trigger:** a `\n` on the serial input cuts one packet (a `\r` is ignored);
the terminator itself is not transmitted.

**Over-length lines:** default **truncate** to 19 bytes with a `#` warning. With
`!FRAG ON`, the line is handed to the §5 streaming layer instead — but a stock
MakeCode robot cannot reassemble fragments, so `!FRAG ON` in MAKECODE mode is only
meaningful when both ends run this firmware.

> **Decision (string-only):** MAKECODE mode emits type `02` only. This keeps the
> serial side transparent text (a bash `echo` is a string) and gives the robot a
> single entry point (`on received string`). Typed number/value sends, if ever
> needed, would be added as explicit `!`-commands, not in the data stream.

> **Reclaimable fields:** when both ends run this firmware (not a stock robot), the
> 8 timestamp+serial bytes are free for your own use. In strict MAKECODE-compat
> they stay conventional so nothing downstream chokes.

---

## 5. Streaming / framing layer (data plane)

Raw radio is datagram, not stream: each send is one packet, lossy, unordered, no
delivery guarantee. To carry arbitrary-length messages, the data plane wraps
payload in a small frame. Identical logic in both modes; only the chunk-size
constant differs.

### 5.1 Frame header

```
[SEQ:1][FLAGS:1][LEN:1][payload:n]
```

- **SEQ** — rolling sequence number; reassembly + duplicate/loss detection.
- **FLAGS** — bitfield:
  - bit 0 `START` (0x01) — first fragment of a message
  - bit 1 `MORE`  (0x02) — more fragments follow
  - bit 2 `END`   (0x04) — last fragment
  - bit 3 `ACK_REQ` (0x08) — sender requests acknowledgment
  - bit 4 `ACK`   (0x10) — this frame is an acknowledgment (SEQ = acked seq)
- **LEN** — payload byte count in this frame.

### 5.2 MTU per mode

| Mode     | Radio payload | Frame header | Usable payload `n` |
| -------- | ------------- | ------------ | ------------------ |
| MAKECODE | 19 (in CODAL) | 3            | 16                 |
| RAW250   | 250           | 3            | 247                |

In MAKECODE mode the frame header lives *inside* the 19 CODAL string bytes, so a
fragmented stream is only decodable by this firmware, not a stock robot.

### 5.3 Reliability

The current sender is **fire-and-forget** (`ACK_REQ` clear) — the right default
for driving a stock MakeCode robot, and what keeps single-frame messages simple.
For reliability, a sender can set `ACK_REQ`; the **receiver side is already
wired** — on a frame with `ACK_REQ` set it waits one half-duplex turnaround
(`kRadioTurnaroundMs`) and replies with an `ACK` frame carrying the same SEQ. The
matching stop-and-wait sender (send, wait for the SEQ'd `ACK` or timeout +
retransmit) is the next increment; windowed ACK can layer on later using the same
SEQ/FLAGS fields.

> **Single-frame is most reliable.** Because there is no retransmit yet, a
> multi-fragment message fails if any one fragment is lost. Keeping each message
> within one frame (≤16 bytes MAKECODE, ≤247 bytes RAW250) avoids that.

### 5.4 Data-plane host I/O

- **RAW250** is a fully transparent byte stream. The relay accumulates up to one
  MTU and flushes on fill or after a short idle gap, so no byte is reserved and a
  partial chunk is not held indefinitely.
- **MAKECODE** is line-oriented: a `\n` cuts one packet (the terminator is not
  sent); a `\r` is ignored. On receive, each decoded MakeCode packet is emitted to
  the host as one line (a `\n` is appended).

### 5.5 Echo / transponder mode

With echo on (`!ECHO ON`, the A+B menu, or restored from flash), every message the
relay receives over the radio is, in addition to being delivered to the host,
bounced back over the radio verbatim in the current framing — after a short delay
(`kEchoDelayMs`) so the original sender's radio has flipped TX→RX and does not miss
the echo in the half-duplex gap. This lets a board run standalone (no host) as an
echo server for round-trip testing; it works in either plane. The resting display
shows the ghost icon while echo is on.

---

## 6. Quick start (host side)

The relay needs no special host software. At 115200 baud:

```
# open the port (DTR toggle resets the board); read the banner:
#   DEVICE:RADIOBRIDGE:relay:<name>:<serial>
HELLO            # re-request the banner if you missed it
?                # read back channel/group/mode/power
!C 5             # channel 5, group 10
!MODE RAW250     # (default) headerless framing
!GO              # enter the transparent data plane
<...bytes...>    # everything after !GO is radio payload, both ways
# close + reopen the port to return to the command plane
```

A two-board end-to-end harness (discovery, reset, messaging, channel isolation,
throughput) lives at `scripts/relay_test.py`; standalone-peer echo/MAKECODE tests
are the other `scripts/*_test.py` files. See [Using the Relay](using-the-relay)
for worked bash and Python examples.

---

## 7. Implementation status

1. **DTR reset behavior** — *Resolved.* Close **and reopen** the port to reset; an
   in-place DTR toggle does not. (§2.1)
2. **Config persistence** — *Implemented.* channel/group/power/mode/frag/echo
   persist to flash across reset and power-cycle; `!DEFAULTS` clears it. (§2.1)
3. **MAKECODE over-length** — *Implemented:* truncate by default, `!FRAG ON` to
   fragment. (§4.2)
4. **Line terminator** — *Implemented:* `\n` cuts a MAKECODE packet; `\r` ignored. (§4.2)
5. **Echo / transponder** — *Implemented:* `!ECHO`, A+B menu, persisted. (§5.5)
6. **SEQ width** — 1 byte (wraps at 256), sufficient for fire-and-forget /
   stop-and-wait; revisit if windowing is added. (§5.1)
7. **Radio packet size** — *Resolved:* `MICROBIT_RADIO_MAX_PACKET_SIZE = 250`
   (RAW250); both ends must match.
8. **Reliability** — fire-and-forget today; the `ACK_REQ`/`ACK` responder is
   wired, the stop-and-wait sender is the next step. (§5.3)
