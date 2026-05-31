# micro:bit Radio Relay Protocol Specification

**Version:** 0.1 (draft)
**Target firmware:** C++ / CODAL on micro:bit V2
**Status:** design draft for review

---

## 1. Purpose and scope

The radio relay is a micro:bit running C++/CODAL firmware that bridges a host
serial port to the nRF radio. It forwards data in both directions and exposes a
small command grammar for configuration.

It supports two radio framing modes:

- **MAKECODE** — emits 32-byte CODAL-compatible packets so a stock MakeCode
  robot receives them on `on received string` with no custom code. The relay
  constructs the full CODAL header itself, so any dumb serial client (a bash
  `echo`, `cat`, a Python script) can drive it.
- **RAW251** — emits headerless payloads up to 251 bytes to a peer running
  C++/CODAL or MicroPython configured with the matching packet size. No CODAL
  header; the relay passes payload through to the framing layer directly.

The firmware is always compiled with `MICROBIT_RADIO_MAX_PACKET_SIZE = 251`.
MAKECODE mode constrains the payload to the 32-byte CODAL layout at runtime; it
does not require a separate build.

### Out of scope

- The ESP8266 / Espressif AT command set. The ESP is downstream of the *robot*,
  configured once by the robot at boot over the robot's own serial line. The
  host and the relay never see AT traffic, and radio payloads cannot collide
  with it.

---

## 2. Physical and lifecycle model

The relay is **stateless across resets**. Opening the host serial port toggles
DTR, which resets the device. On boot the relay enters **command plane** at a
known baud and emits its announcement banner. The host reads the banner to
confirm what it is talking to, configures the relay, then issues `!GO` to enter
the **data plane**.

```
host opens serial port
  -> relay resets, boots into COMMAND plane, emits DEVICE banner
host queries/configures: ?, !MODE RAW251, !CG 5 42, !P 6
host sends !GO
  -> relay configures radio (disable/reconfigure/re-enable), enters DATA plane
DATA plane: bytes are transparent payload, framed + chunked to radio, both ways
host closes port  ->  next open = reset = back to COMMAND plane
```

There is no in-band escape sequence (no `+++`, no guard timing). The only way
out of the data plane is a reset, i.e. close and reopen the port. This is what
keeps the data plane fully transparent — no byte in the stream is reserved.

> **Open decision:** confirm DTR-on-open actually resets the nRF on your
> interface-chip build. If it does not, fall back to firmware soft-reset on
> DTR-drop (`machine.reset()` equivalent in CODAL). Test by watching for the
> boot banner after opening the port.

---

## 3. Serial command plane

Line-oriented, `\n`-terminated, valid only before `!GO`. Carries forward the
existing relay grammar.

### 3.1 Prefix characters

| Prefix | Direction    | Meaning                                          |
| ------ | ------------ | ------------------------------------------------ |
| `>`    | host -> relay | Send rest of line over radio (command plane only)|
| `<`    | relay -> host | A message received from radio                    |
| `!`    | host -> relay | Command (see 3.2)                                |
| `?`    | host -> relay | Query current config                             |
| `#`    | relay -> host | Comment / status from the relay                  |

`>` in the command plane preserves the old single-line send behavior for
backward compatibility and quick testing. Bulk/transparent sending is done in
the data plane after `!GO`, with no prefix.

### 3.2 Commands

| Command            | Description                                                       |
| ------------------ | ----------------------------------------------------------------- |
| `!C <ch>`          | Set channel (0–35), default group 10. Display shows channel char. |
| `!CG <ch> <group>` | Set channel (0–83) and group (0–255). Display shows `?`.          |
| `!RC <ch> <group>` | Alias of `!CG`.                                                   |
| `!P <0-7>`         | Set transmit power.                                               |
| `!MODE MAKECODE`   | Select 32-byte CODAL framing (default).                           |
| `!MODE RAW251`     | Select headerless ≤251-byte framing.                              |
| `!FRAG ON\|OFF`     | MAKECODE over-length policy: fragment vs. truncate (default OFF). |
| `!GO`              | Leave command plane, enter data plane. Exit only via reset.       |
| `!HELP`            | Print protocol summary.                                           |
| `HELLO`            | Re-request the device announcement banner.                        |

### 3.3 Queries

| Query     | Response (relay -> host, `#`-prefixed)                |
| --------- | ----------------------------------------------------- |
| `?`       | `# channel: <ch> group: <g> mode: <m> power: <p>`     |
| `!MODE?`  | `# mode: MAKECODE` or `# mode: RAW251`                |

Query support matters because the host cannot see relay state across a reset.
After opening the port the host should read back config rather than assume.

### 3.4 Boot announcement

```
DEVICE:RADIOBRIDGE:relay:<deviceName>:<serialNumber>
```

Emitted on boot and on `HELLO`.

---

## 4. Radio framing modes

### 4.1 MAKECODE mode (32-byte CODAL string packet)

The relay constructs the full CODAL/PXT header so a stock MakeCode robot fires
`on received string`. Layout of the 32-byte radio payload:

```
01 <grp> 01 | 02 | TS TS TS TS | SN SN SN SN | LEN | <string bytes...>
\__DAL hdr_/  typ  \_timestamp_/  \_serial #_/  len   up to 19 bytes
   3 bytes    1         4              4          1        ≤19
```

- **DAL header** `01 <group> 01`: raw-payload marker, group, version 1.
- **Type** `02` = string. (Number `00` and value `01` are not emitted in this
  mode — see decision below.)
- **Timestamp** (4): relay running time, or zero. A receiving MakeCode robot
  does not use it for `on received string`.
- **Serial** (4): zero (serial-number sending disabled), conventional default.
- **Length** (1): number of string bytes that follow.
- **String** (≤19): UTF-8 bytes of the line.

**Send trigger:** a `\n` on the serial input cuts one packet.

**Over-length lines:** default **truncate** to 19 bytes with a `#` warning,
matching the old relay. With `!FRAG ON`, the line is handed to the streaming
layer (§5) instead — but note a stock MakeCode robot cannot reassemble
fragments, so `!FRAG ON` in MAKECODE mode is only meaningful when both ends are
your own firmware.

> **Decision (string-only):** MAKECODE mode emits type `02` only. This keeps the
> serial side transparent text (a bash `echo` is a string) and gives the robot a
> single entry point (`on received string`). Typed number/value sends, if ever
> needed, would be added as explicit `!`-commands in the command plane, not in
> the data stream.

> **Reclaimable fields:** when both ends are your firmware (not a stock robot),
> the 8 timestamp+serial bytes are free for your own use. In strict
> MAKECODE-compat they stay conventional so nothing downstream chokes.

### 4.2 RAW251 mode (headerless, ≤251 bytes)

No CODAL header. The payload handed to the radio is whatever the framing layer
(§5) produces, up to 251 bytes — the hardware ceiling (254 − 3 for S0/LENGTH/S1).
Peer must be C++/CODAL or MicroPython with matching packet size. A stock
MakeCode robot cannot participate in this mode.

---

## 5. Streaming / framing layer (data plane)

Raw radio is datagram, not stream: each send is one packet, lossy, unordered,
no delivery guarantee. To carry arbitrary-length messages, the data plane wraps
payload in a small frame. Identical logic in both modes; only the chunk-size
constant differs.

### 5.1 Frame header

```
[SEQ:1][FLAGS:1][LEN:1][payload:n]
```

- **SEQ** — rolling sequence number; reassembly + duplicate/loss detection.
- **FLAGS** — bitfield:
  - bit 0 `START` — first fragment of a message
  - bit 1 `MORE`  — more fragments follow
  - bit 2 `END`   — last fragment
  - bit 3 `ACK_REQ` — sender requests acknowledgment
  - bit 4 `ACK`   — this frame is an acknowledgment (SEQ = acked seq)
- **LEN** — payload byte count in this frame.

### 5.2 MTU per mode

| Mode     | Radio payload | Frame header | Usable payload `n` |
| -------- | ------------- | ------------ | ------------------ |
| MAKECODE | 19 (in CODAL) | 3            | ~16                |
| RAW251   | 251           | 3            | ~248               |

In MAKECODE mode the frame header lives *inside* the 19 CODAL string bytes, so a
fragmented stream is only decodable by your own firmware, not a stock robot.

### 5.3 Reliability

Start with **stop-and-wait**: send fragment with `ACK_REQ`, wait for matching
`ACK` (by SEQ) or timeout+retransmit. The half-duplex radio makes windowing
fiddly; windowed ACK can come later using the same SEQ/FLAGS fields. For
fire-and-forget (e.g. driving a stock MakeCode robot), leave `ACK_REQ` clear.

---

## 6. Open decisions to close before implementation

1. **DTR reset behavior** — confirm port-open resets the nRF on your build, else
   add firmware soft-reset on DTR-drop. (§2)
2. **MAKECODE over-length** — confirm truncate-by-default is desired, with
   `!FRAG` as the opt-in. (§4.1)
3. **Line terminator** — spec assumes `\n` cuts a MAKECODE packet. Confirm. (§4.1)
4. **SEQ width** — 1 byte (wraps at 256) assumed sufficient for stop-and-wait;
   revisit if windowing is added. (§5.1)
