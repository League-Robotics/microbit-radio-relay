# Device Announcement Line

The relay identifies itself with a single-line **announcement** (also called the
banner) over USB serial. A host reads it to confirm what it is talking to and
which board it is. This document specifies that line.

See also: [`radio-relay-protocol.md`](radio-relay-protocol.md) §3.4 (the banner
in the context of the command plane).

## Format

```
DEVICE:<role>:<common_name>:<device_name>:<serial>
```

Five colon-separated fields, no internal whitespace, terminated by `\r\n`. As
emitted by this firmware
([`source/relay/RadioRelay.cpp`](../source/relay/RadioRelay.cpp)):

```
DEVICE:RADIOBRIDGE:relay:getez:1779042496
```

| # | Field         | This firmware | Meaning |
|---|---------------|---------------|---------|
| 1 | sentinel      | `DEVICE`      | Literal prefix marking an announcement line. |
| 2 | `role`        | `RADIOBRIDGE` | Device class / firmware family (see below). |
| 3 | `common_name` | `relay`       | Generic role name within the class. |
| 4 | `device_name` | e.g. `getez`  | CODAL friendly name — the 5-character micro:bit name derived from its serial (`codal::microbit_friendly_name()`). Unique-ish, human-readable, stable per board. |
| 5 | `serial`      | e.g. `1779042496` | The nRF serial number (`codal::microbit_serial_number()`). Globally unique per board. |

## When it is emitted

- **At boot**, immediately after the radio comes up (before the boot display
  animation), so it appears as soon as the port settles after the DTR reset.
- **On demand**, in response to the `HELLO` command in the command plane.

Because opening the port resets the board, the boot banner is emitted during the
window the host is still opening the port and is easily missed. The reliable
pattern is: open the port, then send `HELLO` and read the banner from the reply.
See `reset_to_command()` in
[`scripts/relay_test.py`](../scripts/relay_test.py).

## Serial encoding — read carefully

The `serial` field is the **same physical value** across firmwares but is
**printed in different bases**, so a parser must not assume one:

- **This firmware (`RADIOBRIDGE`)** prints it in **decimal** (`%u` of the 32-bit
  `microbit_serial_number()`), e.g. `1779042496`.
- **The older `RADIORELAY` firmware** prints it in **hexadecimal**, e.g.
  `6a5d86c0` (as recorded in [`config/devices.json`](../config/devices.json)).

Match the field loosely (`[0-9A-Fa-f]+`) and interpret the base from the `role`
if you need the numeric value. The `device_name` (field 4) is a more stable join
key across firmwares since it is the same friendly-name string regardless of
base.

> Note: the `serial` in the announcement is **not** the long USB `uid` string
> that tooling like `config/devices.json` also stores (that comes from `ioreg` /
> the USB descriptor). The announcement serial is the nRF/CODAL device serial.

## Role tokens

Field 2 distinguishes firmware families. Two are known in this project:

| `role`        | Firmware | Notes |
|---------------|----------|-------|
| `RADIOBRIDGE` | This C++/CODAL relay | Dual-mode (RAW250 / MAKECODE), flash-persistent config. |
| `RADIORELAY`  | The older MakeCode/TypeScript relay | MAKECODE-only; verified to interoperate with a stock micro:bit. Host tooling auto-classifies boards by this token. |

Host code that needs to behave differently per family should branch on this
field (for example, the old relay echoes via a bare `!ECHO` while this firmware
uses `!ECHO ON`).

## Parsing

A tolerant regex that accepts either firmware family and either serial base:

```python
import re
BANNER_RE = re.compile(rb"DEVICE:(RADIOBRIDGE|RADIORELAY):relay:([^:]+):([0-9A-Fa-f]+)")
# groups: (1) role, (2) device_name, (3) serial
```

This firmware's stricter form (decimal serial only) is what
[`scripts/relay_test.py`](../scripts/relay_test.py) uses:
`DEVICE:RADIOBRIDGE:relay:([^:]+):(\d+)`.
