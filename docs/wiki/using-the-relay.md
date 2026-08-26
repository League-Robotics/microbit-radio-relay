---
title: Using the Relay
blurb: Flash it, open the port, configure it, and push bytes over the radio — from bash, Python, or a serial terminal.
order: 20
slug: using-the-relay
tags: [microbit, radio, relay, howto, getting-started]
---

# Using the Relay

This page is the practical guide: how to get firmware onto a board and how to
drive it from a host. For the exhaustive grammar see the
[Protocol Reference](protocol); to reach a board plugged into *another* machine,
see the [Relay Server](relay-server).

## 1. Flash the firmware

Build produces a `MICROBIT.hex`. Flash it by copying it to the mounted
`MICROBIT` USB drive. Use `cat`, **not** `cp` — the drag-and-drop copy that `cp`
performs can race the board's mass-storage flasher and fail silently:

```bash
cat MICROBIT.hex > /Volumes/MICROBIT/MICROBIT.hex          # macOS
# cat MICROBIT.hex > /media/$USER/MICROBIT/MICROBIT.hex   # Linux
```

When two boards form a RAW250 link, **both must run firmware built with the same
`MICROBIT_RADIO_MAX_PACKET_SIZE`** (250 here). A mismatch silently drops the
larger packets on receive.

## 2. Open the port (and the reset gotcha)

The relay runs at **115200 baud**. Opening the host serial port toggles DTR,
which **resets the board** and drops it back into the command plane (config is
preserved). An in-place DTR toggle on an already-open port does *not* reset — the
host must fully **close and reopen**.

Because the boot banner is emitted during the window the port is still being
opened, it is easily missed. The reliable pattern is: open the port, send
`HELLO`, then read the banner from the reply:

```
DEVICE:RADIOBRIDGE:relay:<deviceName>:<serialNumber>
```

## 3. Command quick-start

Everything below is the **command plane** — line-oriented, `\n`-terminated, valid
only before `!GO`:

```text
HELLO            # re-request the banner if you missed it
?                # read back: # channel: <ch> group: <g> mode: <m> power: <p>
!C 5             # channel 5 (forces group 10)
!MODE RAW250     # (default) headerless ≤250-byte framing
!ECHO ON         # optional: make this board a transponder
!GO              # enter the transparent data plane
<...bytes...>    # everything after !GO is radio payload, both directions
# close + reopen the port to return to the command plane
```

Lines you receive *from* the relay are prefixed so a parser can classify them:

| Prefix | Meaning                                   |
| ------ | ----------------------------------------- |
| `<`    | a message received over the radio         |
| `#`    | comment / status / query reply from relay |

Common commands (full table in the [Protocol Reference](protocol)):

| Command            | What it does                                                       |
| ------------------ | ----------------------------------------------------------------- |
| `!C <ch>`          | channel 0–35, forces group 10                                     |
| `!CG <ch> <group>` | channel 0–83 and group 0–255 (`!RC` is an alias)                  |
| `!P <0-7>`         | transmit power                                                    |
| `!MODE RAW250`     | headerless ≤250-byte framing (default)                            |
| `!MODE MAKECODE`   | 32-byte CODAL packets for a stock MakeCode robot                  |
| `!ECHO [ON\|OFF]`  | transponder: bounce every received message back (bare = toggle)   |
| `!DEFAULTS`        | clear saved config; compiled-in defaults on next reset           |
| `!GO`              | leave command plane, enter data plane (exit only by reset)        |
| `?` / `!MODE?`     | query config / query mode                                        |

Config changes apply immediately, persist to flash, and are echoed back as a `#`
comment.

## 4. Driving it without a host

The buttons work in either plane, no host needed:

- **A** = channel down, **B** = channel up (wrapping 0–35, only when on group 10).
- **A+B** opens a menu on the 5×5 display. Each press advances; resting on an
  item for 3 seconds accepts it. Items toggle packet mode (`32`/`25`), toggle echo
  (ghost / west-arrow icon), or cancel (`X`).

The resting display shows the channel glyph (`0`–`9`, then `A`–`Z` for 10–35,
`?` for a custom group) — or a **ghost icon** when echo mode is on.

## 5. Worked examples

### bash — send one line, command plane

```bash
# stty configures the port; then write a command-plane send
stty -f /dev/tty.usbmodem* 115200 raw      # macOS (use -F on Linux)
printf '> hello over the radio\n' > /dev/tty.usbmodem*
```

### bash — transparent data plane

```bash
PORT=/dev/tty.usbmodem*
stty -f $PORT 115200 raw
# read incoming lines in the background
cat $PORT &
# configure, then go transparent
printf '!MODE RAW250\n!GO\n' > $PORT
# from here, every byte is radio payload
printf 'streaming bytes with no prefix\n' > $PORT
```

### Python (pyserial)

```python
import serial, time

port = serial.Serial("/dev/tty.usbmodem0001", 115200, timeout=1)
time.sleep(0.3)                 # let the DTR reset settle

port.write(b"HELLO\n")          # ask for the banner
print(port.readline())          # b'DEVICE:RADIOBRIDGE:relay:...\r\n'

port.write(b"!C 5\n")           # channel 5
port.write(b"?\n")              # read back config
print(port.readline())          # b'# channel: 5 group: 10 ...'

port.write(b"!GO\n")            # enter the data plane
port.write(b"payload bytes\n")  # now transparent radio payload
```

A two-board end-to-end harness (discovery, reset, messaging, channel isolation,
throughput) lives at `scripts/relay_test.py`; the standalone-peer echo/MAKECODE
checks are the other `scripts/*_test.py` files.

## 6. Talking to a stock MakeCode micro:bit

Switch to MAKECODE mode and send text lines; a stock robot fires
`on received string`:

```text
!MODE MAKECODE   # 32-byte CODAL string packets
!CG 0 10         # match the robot's channel/group (MakeCode default group 1 → set as needed)
!GO
hello robot      # a \n cuts each packet; lines >19 bytes truncate (or !FRAG ON to fragment)
```

Note MAKECODE caps a single string at **19 bytes**. Longer lines truncate by
default; `!FRAG ON` fragments them, but only another board running *this*
firmware can reassemble — a stock robot cannot. See the
[Protocol Reference](protocol) §4–5 for the framing detail.
