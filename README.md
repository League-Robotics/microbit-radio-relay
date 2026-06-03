# micro:bit Radio Relay

Firmware that turns a **micro:bit V2** into a **USB-serial ↔ nRF radio bridge**.
A host (any program that can open a serial port — `cat`, `echo`, a `pyserial`
script, a serial terminal) talks to the relay over USB at **115200 baud**; the
relay forwards bytes to and from the 2.4 GHz micro:bit radio. It needs no host
library and no custom code on the receiving robot.

It is built on the [lancaster-university/microbit-v2-samples](https://github.com/lancaster-university/microbit-v2-samples)
CODAL build system (toolchain/build/deploy instructions below are inherited from
it). The relay firmware itself lives in [`source/relay/`](source/relay/) and is
launched from [`source/main.cpp`](source/main.cpp).

## What it does

- **Two framing modes**, switchable at runtime (one firmware does both):
  - **RAW250** *(default)* — headerless payloads up to 250 bytes to a peer
    running matching C++/CODAL or MicroPython firmware.
  - **MAKECODE** — 32-byte CODAL string packets, so a **stock MakeCode robot**
    receives them on `on received string` with no custom code.
- **Command plane → data plane lifecycle.** On boot the relay is in a
  line-oriented **command plane** for configuration; `!GO` switches it to a
  fully transparent **data plane**. The radio is live in both.
- **Config persists to flash.** Channel, group, power, mode, fragmentation, and
  echo survive reset *and* power-cycle. Configure a board once and replug it.
- **Standalone operation.** Buttons set the channel (A/B) and toggle mode/echo
  (A+B menu) with no host attached. **Echo/transponder mode** turns a board into
  a self-contained radio echo server.

## Protocol — start here

**[`docs/radio-relay-protocol.md`](docs/radio-relay-protocol.md)** is the full
wire/grammar specification and the canonical reference;
**[`docs/announce.md`](docs/announce.md)** specifies the device announcement/banner
line. Quick taste (115200 baud):

```
# Open the port (this resets the board). It prints its banner:
#   DEVICE:RADIOBRIDGE:relay:<name>:<serial>
HELLO            # re-request the banner if you missed it
?                # show: # channel: <ch> group: <g> mode: <m> power: <p>
!C 5             # set channel 5 (group 10)
!MODE RAW250     # default; or !MODE MAKECODE to talk to a stock MakeCode robot
!GO              # enter the transparent data plane
...bytes...      # everything after !GO is radio payload, both directions
# close + reopen the port to return to the command plane (config is kept)
```

Defaults on a fresh board: **channel 0, group 10, power 7, mode RAW250, echo off**.
Command summary (`!HELP` prints this on the device):

| Command | Effect |
| --- | --- |
| `!C <0-35>` | set channel, group 10 |
| `!CG <ch> <group>` / `!RC ...` | set channel (0–83) and group (0–255) |
| `!P <0-7>` | transmit power |
| `!MODE MAKECODE` / `!MODE RAW250` | select framing (`RAW251` = alias of RAW250) |
| `!FRAG ON\|OFF` | MAKECODE over-length: fragment vs. truncate |
| `!ECHO [ON\|OFF]` | transponder: bounce received messages back |
| `!DEFAULTS` | clear saved config (defaults next reset) |
| `!GO` | enter data plane (exit only by reset) |
| `?` / `!MODE?` | query config / mode |
| `HELLO` | re-print the device banner |
| buttons A / B | channel down / up (group 10) |
| buttons A+B | menu: packet mode 32/250, echo, cancel |

> **Both ends must share the radio packet size.** The firmware is built with
> `MICROBIT_RADIO_MAX_PACKET_SIZE = 250` ([`codal.json`](codal.json)); a peer
> built with a different size will not receive the larger packets.

## Test harness

Two relay boards on USB:

```
uv run python3 scripts/relay_test.py                  # all phases
uv run python3 scripts/relay_test.py --phase throughput --mode RAW250
```

It auto-discovers boards by their banner, then exercises reset, messaging,
channel isolation, round-trip, and throughput. The other `scripts/*_test.py`
files drive a single host board against standalone echo/MAKECODE peers.

---

# Building and deploying

## Prerequisites

- [GNU Arm Embedded Toolchain](https://developer.arm.com/tools-and-software/open-source-software/developer-tools/gnu-toolchain/gnu-rm/downloads)
- [Git](https://git-scm.com), [CMake](https://cmake.org/download/), [Python 3](https://www.python.org/downloads/)

On Ubuntu:

```
sudo apt install gcc git cmake gcc-arm-none-eabi binutils-arm-none-eabi
```

### macOS clean setup (recommended)

Use these exact commands to avoid mixed/incomplete ARM toolchains:

```
brew uninstall arm-none-eabi-gcc arm-none-eabi-binutils
brew install --cask gcc-arm-embedded
brew install uv
```

If `arm-none-eabi-gcc` is not found after installation, link the tools once
(adjust the version path to your install) — or run `just link-arm-tools`:

```
ln -s /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-gcc    /opt/homebrew/bin/arm-none-eabi-gcc
ln -s /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-g++    /opt/homebrew/bin/arm-none-eabi-g++
ln -s /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-ar     /opt/homebrew/bin/arm-none-eabi-ar
ln -s /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-ranlib /opt/homebrew/bin/arm-none-eabi-ranlib
ln -s /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-objcopy /opt/homebrew/bin/arm-none-eabi-objcopy
ln -s /Applications/ArmGNUToolchain/15.2.rel1/arm-none-eabi/bin/arm-none-eabi-size   /opt/homebrew/bin/arm-none-eabi-size
```

## Python + UV setup

From the repository root:

```
uv venv
uv sync
```

This installs the Python modules used by the deploy/test scripts (`requests`,
`python-dotenv`, `pyserial`).

## Build

```
uv run python3 build.py            # produces ./MICROBIT.hex
uv run python3 build.py --clean    # clean rebuild
```

## Deploy

`scripts/deploy.py` flashes the hex to a local USB-mounted micro:bit, or POSTs it
to a console (`CONSOLE_URL` + `CONSOLE_KEY`, via `.env` or flags):

```
uv run python3 scripts/build_and_deploy.py --clean              # build then deploy
uv run python3 scripts/deploy.py                                # deploy existing hex
uv run python3 scripts/deploy.py --usb-mount /Volumes/MICROBIT  # explicit USB mount
uv run python3 scripts/build_and_deploy.py --console-url https://your-console.example --console-key YOUR_KEY
```

> **Flashing a USB-mounted micro:bit:** copy the hex with `cat`, not `cp`, onto
> the `MICROBIT` volume — some macOS setups mishandle the MSD copy otherwise.

## `just` recipes

A [`justfile`](justfile) wraps the common workflows:

```
just --list
just uv-sync
just build            # / just build-clean
just deploy -- --usb-mount /Volumes/MICROBIT
just build-deploy -- --clean
just setup-macos      # / just link-arm-tools
```

## Docker

Build without installing the toolchain (hex/bin land in `out/`):

```
docker build -t microbit-tools --output out .
```

## Debugging

VS Code launch configs for OpenOCD and PyOCD are provided (install the
[`marus25.cortex-debug`](https://marketplace.visualstudio.com/items?itemName=marus25.cortex-debug)
extension, build, then Run and Debug). On the relay itself, `!DEBUG ON` turns on
`# DBG ...` radio TX/RX logging over serial without a reflash, and `!REGS` dumps
the live nRF radio registers (see the protocol doc §3.2).

## Compatibility & issues

This repo follows the APIs of the original micro:bit and includes a
[microbit-dal](https://www.github.com/lancaster-university/microbit-dal)
compatibility layer. micro:bit platform issues should be raised on
[lancaster-university/codal-microbit-v2](https://github.com/lancaster-university/codal-microbit-v2).
