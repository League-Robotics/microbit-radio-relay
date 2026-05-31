/*
The MIT License (MIT)

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/

//
// micro:bit Radio Relay
// =====================
//
// Bridges the host USB serial port to the nRF radio in both directions and
// exposes a small command grammar for configuration. See
// docs/radio-relay-protocol.md for the wire/grammar specification; section
// numbers below (e.g. "§4.1") refer to that document.
//
// Lifecycle (§2): boot -> COMMAND plane (configure) -> !GO -> DATA plane. The
// relay is stateless across resets; closing/reopening the host port toggles
// DTR which resets the board and returns it to the command plane.
//
// Two radio framing modes (§4):
//   * MAKECODE - 32-byte CODAL string packets, so a stock MakeCode robot fires
//                `on received string`. The relay builds the PXT packet body;
//                CODAL prepends its own 4-byte radio header (version/group/
//                protocol) on the air, which is the "01 <grp> 01" the spec
//                diagram shows ahead of the PXT type byte.
//   * RAW251   - headerless payloads up to the radio ceiling, framed with the
//                §5 fragment header for a peer running matching firmware.
//

#include "MicroBit.h"
#include "RadioRelay.h"

#include <cstdio>
#include <cstring>

extern MicroBit uBit;

namespace
{
    // ---------------------------------------------------------------------
    // Tunables / wire constants
    // ---------------------------------------------------------------------

    // USB CDC buffers. CODAL stores these sizes in a uint8_t, so 250 is the
    // practical ceiling.
    constexpr int kUsbBufferSize = 250;

    // Link defaults. These match the companion firmware (relay-speed-test and
    // the MakeCode radio_relay): channel/frequency-band 0, group 10. A peer on
    // a different channel+group never hears us.
    constexpr int  kDefaultChannel = 0;     // frequency band, 0..83
    constexpr int  kDefaultGroup   = 10;    // radio group, 0..255
    constexpr int  kDefaultPower   = 7;     // transmit power, 0..7

    // MakeCode PXT string packet (§4.1). Body the relay builds, before CODAL's
    // own 4-byte radio header:
    //   [type:1=0x02][timestamp:4][serial:4][len:1][utf8 string:<=19]
    constexpr uint8_t kPxtTypeString = 0x02;
    constexpr int     kPxtPrefix     = 9;   // type(1) + timestamp(4) + serial(4)
    constexpr int     kMakecodeStrMax = 19; // MakeCode caps radio strings at 19

    // §5.1 fragment header: [SEQ:1][FLAGS:1][LEN:1][payload:n]
    constexpr int     kFrameHeader = 3;
    constexpr uint8_t kFlagStart   = 0x01;  // first fragment of a message
    constexpr uint8_t kFlagMore    = 0x02;  // more fragments follow
    constexpr uint8_t kFlagEnd     = 0x04;  // last fragment
    constexpr uint8_t kFlagAckReq  = 0x08;  // sender requests acknowledgment
    constexpr uint8_t kFlagAck     = 0x10;  // this frame is an acknowledgment

    // Usable §5 payload per mode (§5.2). RAW251 rides the full radio packet;
    // MAKECODE fragments live inside the 19 string bytes.
    constexpr int kRawMtu      = MICROBIT_RADIO_MAX_PACKET_SIZE - kFrameHeader;
    constexpr int kMakecodeMtu = kMakecodeStrMax - kFrameHeader;

    // Reassembly ceiling for inbound fragmented messages.
    constexpr int kReassemblyMax = 1024;

    // Half-duplex turnaround: give a peer's radio time to flip TX->RX before we
    // transmit a reply/ACK from the receive handler, or it lands in the gap.
    constexpr int kRadioTurnaroundMs = 4;

    // Idle flush for the transparent RAW251 stream: bytes that have sat in the
    // accumulator with nothing following get sent so a partial chunk is not
    // held forever.
    constexpr int kRawIdleFlushMs = 5;

    enum Mode
    {
        MODE_MAKECODE = 0,
        MODE_RAW251
    };

    // ---------------------------------------------------------------------
    // Relay state
    // ---------------------------------------------------------------------
    struct Config
    {
        int  channel = kDefaultChannel;
        int  group   = kDefaultGroup;
        int  power   = kDefaultPower;
        Mode mode    = MODE_MAKECODE;
        bool frag    = false;       // MAKECODE over-length policy: fragment vs truncate
    };

    Config cfg;
    volatile bool dataPlane = false;    // false = command plane, true = data plane
    uint8_t txSeq = 0;                  // rolling §5 sequence number

    char announceLine[80] = {0};        // §3.4 boot banner

    // Inbound §5 reassembly buffer (RAW251).
    uint8_t reasmBuf[kReassemblyMax];
    int     reasmLen = 0;
    bool    reasmActive = false;

    // ---------------------------------------------------------------------
    // Serial helpers
    // ---------------------------------------------------------------------
    void sendBytes(const uint8_t *b, int len)
    {
        if (len > 0)
            uBit.serial.send((uint8_t *)b, len, SYNC_SLEEP);
    }

    void sendStr(const char *s)
    {
        uBit.serial.send((uint8_t *)s, strlen(s), SYNC_SLEEP);
    }

    // A '#'-prefixed status/comment line back to the host (§3.1).
    void comment(const char *s)
    {
        sendStr("# ");
        sendStr(s);
        sendStr("\r\n");
    }

    bool startsWith(const char *s, const char *prefix)
    {
        return strncmp(s, prefix, strlen(prefix)) == 0;
    }

    // ---------------------------------------------------------------------
    // Radio configuration
    // ---------------------------------------------------------------------
    void applyRadioConfig()
    {
        // §2: disable / reconfigure / re-enable for a clean state.
        uBit.radio.disable();
        uBit.radio.enable();
        uBit.radio.setFrequencyBand(cfg.channel);
        uBit.radio.setGroup(cfg.group);
        uBit.radio.setTransmitPower(cfg.power);
    }

    // ---------------------------------------------------------------------
    // Outbound framing
    // ---------------------------------------------------------------------

    // Transmit one MakeCode PXT string packet carrying up to 19 raw string
    // bytes, so a stock MakeCode robot receives it on `on received string`.
    void sendMakecodeString(const uint8_t *str, int len)
    {
        if (len > kMakecodeStrMax)
            len = kMakecodeStrMax;

        uint8_t buf[kPxtPrefix + 1 + kMakecodeStrMax];
        memset(buf, 0, sizeof(buf));
        buf[0] = kPxtTypeString;            // type = string
        // bytes 1..8 (timestamp + serial) left zero: a receiving MakeCode robot
        // does not use them for `on received string` (§4.1).
        buf[kPxtPrefix] = (uint8_t)len;     // string length
        memcpy(buf + kPxtPrefix + 1, str, len);
        uBit.radio.datagram.send(buf, kPxtPrefix + 1 + len);
    }

    // Transmit one §5 frame. In RAW251 the frame is the radio payload directly;
    // in MAKECODE the frame bytes ride inside a PXT string packet (only your own
    // firmware can decode that, not a stock robot).
    void sendFrame(uint8_t seq, uint8_t flags, const uint8_t *payload, int len)
    {
        uint8_t frame[kFrameHeader + (kRawMtu > kMakecodeMtu ? kRawMtu : kMakecodeMtu)];
        frame[0] = seq;
        frame[1] = flags;
        frame[2] = (uint8_t)len;
        if (len > 0)
            memcpy(frame + kFrameHeader, payload, len);

        if (cfg.mode == MODE_RAW251)
            uBit.radio.datagram.send(frame, kFrameHeader + len);
        else
            sendMakecodeString(frame, kFrameHeader + len);
    }

    // Fragment and transmit an arbitrary-length message across §5 frames.
    // Fire-and-forget (ACK_REQ clear) per §5.3 — the default for driving a
    // stock robot. Stop-and-wait reliability layers on top of these same
    // SEQ/FLAGS fields and is the documented next increment.
    void sendFramedMessage(const uint8_t *msg, int msgLen)
    {
        const int mtu = (cfg.mode == MODE_RAW251) ? kRawMtu : kMakecodeMtu;
        int off = 0;
        bool first = true;

        do
        {
            int chunk = msgLen - off;
            if (chunk > mtu)
                chunk = mtu;

            uint8_t flags = 0;
            if (first)
                flags |= kFlagStart;
            if (off + chunk < msgLen)
                flags |= kFlagMore;
            else
                flags |= kFlagEnd;

            sendFrame(txSeq++, flags, msg + off, chunk);
            off += chunk;
            first = false;
        } while (off < msgLen);
    }

    // Send one host line over the radio using the current mode (the command
    // plane '>' send, and the MAKECODE data-plane per-line send).
    void sendLine(const uint8_t *data, int len)
    {
        if (cfg.mode == MODE_MAKECODE)
        {
            if (len <= kMakecodeStrMax || !cfg.frag)
            {
                if (len > kMakecodeStrMax)
                {
                    len = kMakecodeStrMax;          // §4.1 default: truncate
                    comment("truncated to 19 bytes");
                }
                sendMakecodeString(data, len);
            }
            else
            {
                // !FRAG ON: hand the over-length line to the framing layer.
                sendFramedMessage(data, len);
            }
        }
        else
        {
            sendFramedMessage(data, len);
        }
    }

    // ---------------------------------------------------------------------
    // Inbound radio handling
    // ---------------------------------------------------------------------

    // Emit a received message to the host: '<'-prefixed line in the command
    // plane (§3.1), transparent bytes in the data plane.
    void emitInbound(const uint8_t *data, int len)
    {
        if (dataPlane)
        {
            sendBytes(data, len);
            if (cfg.mode == MODE_MAKECODE)
                sendStr("\n");      // one MakeCode packet == one line
        }
        else
        {
            sendStr("< ");
            sendBytes(data, len);
            sendStr("\r\n");
        }
    }

    // A MAKECODE-framed inbound packet: strip the PXT string body and emit it.
    void handleMakecodeInbound(const uint8_t *b, int n)
    {
        if (n < kPxtPrefix + 1 || b[0] != kPxtTypeString)
            return;                                 // not a PXT string packet
        int slen = b[kPxtPrefix];
        if (slen > n - (kPxtPrefix + 1))
            slen = n - (kPxtPrefix + 1);
        emitInbound(b + kPxtPrefix + 1, slen);
    }

    // A RAW251 inbound §5 frame: ACK, reassemble, and emit complete messages.
    void handleRawInbound(const uint8_t *b, int n)
    {
        if (n < kFrameHeader)
            return;
        uint8_t seq   = b[0];
        uint8_t flags = b[1];
        int     len   = b[2];
        const uint8_t *payload = b + kFrameHeader;
        if (len > n - kFrameHeader)
            len = n - kFrameHeader;

        if (flags & kFlagAck)
            return;                                 // ACK for us; nothing to emit

        if (flags & kFlagStart)
        {
            reasmLen = 0;
            reasmActive = true;
        }
        if (reasmActive && len > 0)
        {
            if (reasmLen + len > kReassemblyMax)
                len = kReassemblyMax - reasmLen;    // guard against overrun
            memcpy(reasmBuf + reasmLen, payload, len);
            reasmLen += len;
        }
        if (flags & kFlagEnd)
        {
            if (reasmActive)
                emitInbound(reasmBuf, reasmLen);
            reasmActive = false;
            reasmLen = 0;
        }

        // Acknowledge if the sender asked for it (§5.3 responder side).
        if (flags & kFlagAckReq)
        {
            uBit.sleep(kRadioTurnaroundMs);
            sendFrame(seq, kFlagAck, nullptr, 0);
        }
    }

    void onRadioFrame(MicroBitEvent)
    {
        PacketBuffer packet = uBit.radio.datagram.recv();
        int n = packet.length();
        if (n <= 0)
            return;
        uint8_t *b = packet.getBytes();

        if (cfg.mode == MODE_MAKECODE)
            handleMakecodeInbound(b, n);
        else
            handleRawInbound(b, n);
    }

    // ---------------------------------------------------------------------
    // Display feedback
    // ---------------------------------------------------------------------

    // Channel as a single glyph: 0-9 -> '0'..'9', 10-35 -> 'A'..'Z' (§3.2).
    void showChannel(int ch)
    {
        char c;
        if (ch >= 0 && ch <= 9)
            c = (char)('0' + ch);
        else if (ch >= 10 && ch <= 35)
            c = (char)('A' + (ch - 10));
        else
            c = '?';
        uBit.display.printChar(c);
    }

    // ---------------------------------------------------------------------
    // Command plane (§3)
    // ---------------------------------------------------------------------
    void printHelp()
    {
        comment("micro:bit radio relay");
        comment("!C <ch>            set channel 0-35 (group 10)");
        comment("!CG <ch> <group>   set channel 0-83 and group 0-255");
        comment("!RC <ch> <group>   alias of !CG");
        comment("!P <0-7>           set transmit power");
        comment("!MODE MAKECODE     32-byte CODAL string framing (default)");
        comment("!MODE RAW251       headerless framing for matching firmware");
        comment("!FRAG ON|OFF       MAKECODE over-length: fragment vs truncate");
        comment("!GO                enter data plane (exit only by reset)");
        comment("> <text>           send one line over radio (command plane)");
        comment("?                  show channel/group/mode/power");
        comment("!MODE?             show mode");
        comment("HELLO              re-request device banner");
    }

    void printConfig()
    {
        char out[80];
        snprintf(out, sizeof(out), "channel: %d group: %d mode: %s power: %d",
                 cfg.channel, cfg.group,
                 cfg.mode == MODE_MAKECODE ? "MAKECODE" : "RAW251", cfg.power);
        comment(out);
    }

    // Parse one '\n'-terminated command line. Returns true once !GO was seen.
    bool handleCommand(ManagedString lineStr)
    {
        // Trim trailing CR/LF.
        while (lineStr.length() > 0)
        {
            char t = lineStr.charAt(lineStr.length() - 1);
            if (t != '\r' && t != '\n')
                break;
            lineStr = lineStr.substring(0, lineStr.length() - 1);
        }
        if (lineStr.length() == 0)
            return false;

        const char *line = lineStr.toCharArray();

        // Queries -----------------------------------------------------------
        if (strcmp(line, "?") == 0)
        {
            printConfig();
            return false;
        }
        if (strcmp(line, "!MODE?") == 0)
        {
            comment(cfg.mode == MODE_MAKECODE ? "mode: MAKECODE" : "mode: RAW251");
            return false;
        }

        // Banner / help -----------------------------------------------------
        if (strcmp(line, "HELLO") == 0)
        {
            sendStr(announceLine);
            sendStr("\r\n");
            return false;
        }
        if (strcmp(line, "!HELP") == 0)
        {
            printHelp();
            return false;
        }

        // Single-line radio send (command plane) ----------------------------
        if (line[0] == '>')
        {
            const char *text = line + 1;
            if (*text == ' ')
                text++;                             // tolerate "> text"
            sendLine((const uint8_t *)text, strlen(text));
            return false;
        }

        // Mode / fragmentation ----------------------------------------------
        if (strcmp(line, "!MODE MAKECODE") == 0)
        {
            cfg.mode = MODE_MAKECODE;
            comment("mode: MAKECODE");
            return false;
        }
        if (strcmp(line, "!MODE RAW251") == 0)
        {
            cfg.mode = MODE_RAW251;
            comment("mode: RAW251");
            return false;
        }
        if (strcmp(line, "!FRAG ON") == 0)
        {
            cfg.frag = true;
            comment("frag: ON");
            return false;
        }
        if (strcmp(line, "!FRAG OFF") == 0)
        {
            cfg.frag = false;
            comment("frag: OFF");
            return false;
        }

        // Channel / group / power -------------------------------------------
        if (startsWith(line, "!CG ") || startsWith(line, "!RC "))
        {
            int ch = -1, grp = -1;
            if (sscanf(line + 4, "%d %d", &ch, &grp) == 2 &&
                ch >= 0 && ch <= 83 && grp >= 0 && grp <= 255)
            {
                cfg.channel = ch;
                cfg.group = grp;
                applyRadioConfig();
                uBit.display.printChar('?');        // §3.2: !CG/!RC show '?'
                printConfig();
            }
            else
            {
                comment("error: usage !CG <ch 0-83> <group 0-255>");
            }
            return false;
        }
        if (startsWith(line, "!C "))
        {
            int ch = -1;
            if (sscanf(line + 3, "%d", &ch) == 1 && ch >= 0 && ch <= 35)
            {
                cfg.channel = ch;
                cfg.group = 10;                     // §3.2: !C uses default group 10
                applyRadioConfig();
                showChannel(ch);
                printConfig();
            }
            else
            {
                comment("error: usage !C <ch 0-35>");
            }
            return false;
        }
        if (startsWith(line, "!P "))
        {
            int p = -1;
            if (sscanf(line + 3, "%d", &p) == 1 && p >= 0 && p <= 7)
            {
                cfg.power = p;
                applyRadioConfig();
                printConfig();
            }
            else
            {
                comment("error: usage !P <0-7>");
            }
            return false;
        }

        // Transition --------------------------------------------------------
        if (strcmp(line, "!GO") == 0)
        {
            return true;
        }

        comment("error: unknown command (try !HELP)");
        return false;
    }

    // ---------------------------------------------------------------------
    // Data plane (§5): transparent host -> radio
    // ---------------------------------------------------------------------

    // MAKECODE: line-oriented. A '\n' cuts one packet (§4.1); the terminator is
    // not transmitted.
    void dataPlaneMakecode()
    {
        uint8_t lineBuf[256];
        int len = 0;
        while (true)
        {
            int c = uBit.serial.read(ASYNC);
            if (c < 0)
            {
                uBit.sleep(1);
                continue;
            }
            if (c == '\n')
            {
                sendLine(lineBuf, len);
                len = 0;
            }
            else if (c == '\r')
            {
                // ignore; '\n' is the cut
            }
            else if (len < (int)sizeof(lineBuf))
            {
                lineBuf[len++] = (uint8_t)c;
            }
            else
            {
                // Over-length line with no terminator yet: flush what we have.
                sendLine(lineBuf, len);
                len = 0;
                lineBuf[len++] = (uint8_t)c;
            }
        }
    }

    // RAW251: fully transparent byte stream. Accumulate up to one MTU and flush
    // on fill or a short idle gap, so no byte is reserved and partial chunks are
    // not held indefinitely.
    void dataPlaneRaw()
    {
        uint8_t buf[kRawMtu];
        int len = 0;
        int idle = 0;
        while (true)
        {
            int c = uBit.serial.read(ASYNC);
            if (c < 0)
            {
                if (len > 0 && ++idle >= kRawIdleFlushMs)
                {
                    sendFramedMessage(buf, len);
                    len = 0;
                    idle = 0;
                }
                uBit.sleep(1);
                continue;
            }
            idle = 0;
            buf[len++] = (uint8_t)c;
            if (len >= kRawMtu)
            {
                sendFramedMessage(buf, len);
                len = 0;
            }
        }
    }
}

// -------------------------------------------------------------------------
// Entry point
// -------------------------------------------------------------------------
void radio_relay_main()
{
    // Generous USB buffers so bursts of host data are not dropped between reads.
    uBit.serial.setTxBufferSize(kUsbBufferSize);
    uBit.serial.setRxBufferSize(kUsbBufferSize);

    // Bring the radio up on the link defaults and start listening. The radio is
    // live in the command plane too, so '>' sends and '<' receives work before
    // !GO (§3.1).
    applyRadioConfig();
    uBit.messageBus.listen(DEVICE_ID_RADIO, MICROBIT_RADIO_EVT_DATAGRAM, onRadioFrame);

    // §3.4 boot announcement: DEVICE:RADIOBRIDGE:relay:<deviceName>:<serial>
    snprintf(announceLine, sizeof(announceLine), "DEVICE:RADIOBRIDGE:relay:%s:%u",
             codal::microbit_friendly_name(),
             (unsigned)codal::microbit_serial_number());
    sendStr(announceLine);
    sendStr("\r\n");
    showChannel(cfg.channel);

    // Command plane: read '\n'-terminated lines until !GO.
    while (!dataPlane)
    {
        ManagedString line = uBit.serial.readUntil(ManagedString("\n"), SYNC_SLEEP);
        if (handleCommand(line))
            dataPlane = true;
    }

    // Transition to the data plane (§2): clean radio reconfigure, then run the
    // transparent forwarder forever. Only a reset leaves here.
    applyRadioConfig();
    comment("entering data plane");
    uBit.display.printChar('.');

    if (cfg.mode == MODE_MAKECODE)
        dataPlaneMakecode();
    else
        dataPlaneRaw();
}
