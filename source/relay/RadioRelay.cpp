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
//   * RAW250   - headerless payloads up to the radio ceiling, framed with the
//                §5 fragment header for a peer running matching firmware.
//

#include "MicroBit.h"
#include "RadioRelay.h"
#include "nrf.h"     // NRF_RADIO, RADIO_IRQn, NVIC_* for retuneReceiver()
#include "Radio.h"   // RADIO_EVT_DATA_READY (radio diagnostics)

#include <cstdarg>
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

    // Link defaults: channel/frequency-band 0 ("0"), group 10. (Earlier builds
    // defaulted to channel 10 to avoid colliding with the old MakeCode relays;
    // those are being retired, so we match their channel-0 default again.) Only
    // applies to a fresh board / after !DEFAULTS -- a configured board restores
    // its saved channel from flash. A peer on a different channel+group never
    // hears us.
    constexpr int  kDefaultChannel = 0;     // frequency band, 0..83 -> displays '0'
    constexpr int  kDefaultGroup   = 10;    // radio group, 0..255
    constexpr int  kDefaultPower   = 7;     // transmit power, 0..7

    // Buttons cycle the channel within this inclusive range (group 10 only),
    // matching the MakeCode relay: 0..35 shown as '0'..'9','A'..'Z'.
    constexpr int  kChannelMin = 0;
    constexpr int  kChannelMax = 35;

    // MakeCode PXT string packet (§4.1). Body the relay builds, before CODAL's
    // own 4-byte radio header:
    //   [type:1=0x02][timestamp:4][serial:4][len:1][utf8 string:<=19]
    constexpr uint8_t kPxtTypeString = 0x02;
    constexpr int     kPxtPrefix     = 9;   // type(1) + timestamp(4) + serial(4)
    constexpr int     kMakecodeStrMax = 19; // MakeCode caps radio strings at 19

    // On-air nRF PCNF1.MAXLEN per mode. A stock MakeCode device runs MAXLEN 32,
    // and a radio built for RAW250 (MAXLEN 250) cannot receive its packets at
    // all -- both ends must share the size. We compile with big buffers
    // (MICROBIT_RADIO_MAX_PACKET_SIZE=250) and switch MAXLEN at runtime so one
    // firmware speaks to stock MakeCode robots (32) AND RAW250 peers (250).
    constexpr uint32_t kMakecodeMaxLen = 32;
    constexpr uint32_t kRaw250MaxLen   = MICROBIT_RADIO_MAX_PACKET_SIZE;

    // §5.1 fragment header: [SEQ:1][FLAGS:1][LEN:1][payload:n]
    constexpr int     kFrameHeader = 3;
    constexpr uint8_t kFlagStart   = 0x01;  // first fragment of a message
    constexpr uint8_t kFlagMore    = 0x02;  // more fragments follow
    constexpr uint8_t kFlagEnd     = 0x04;  // last fragment
    constexpr uint8_t kFlagAckReq  = 0x08;  // sender requests acknowledgment
    constexpr uint8_t kFlagAck     = 0x10;  // this frame is an acknowledgment

    // Usable §5 payload per mode (§5.2). RAW frames ride the full radio packet;
    // MAKECODE fragments live inside the 19 string bytes.
    //
    // The radio packet is MICROBIT_RADIO_MAX_PACKET_SIZE bytes, set from the
    // codal.json config ("MICROBIT_RADIO_MAX_PACKET_SIZE": 250). The nRF
    // hardware MAXLEN is fixed from this value at radio init, so BOTH ends must
    // be built with the same size or the larger frames are dropped on receive.
    // At 250 the RAW250 mode carries up to 247 payload bytes per frame (250 - 3
    // header). Deriving kRawMtu from the macro keeps the relay in lock-step with
    // whatever the radio was actually built with -- the mode name (RAW250) is
    // just a label for the current MICROBIT_RADIO_MAX_PACKET_SIZE.
    constexpr int kRawMtu      = MICROBIT_RADIO_MAX_PACKET_SIZE - kFrameHeader;
    constexpr int kMakecodeMtu = kMakecodeStrMax - kFrameHeader;

    // Reassembly ceiling for inbound fragmented messages.
    constexpr int kReassemblyMax = 1024;

    // Half-duplex turnaround: give a peer's radio time to flip TX->RX before we
    // transmit a reply/ACK from the receive handler, or it lands in the gap.
    constexpr int kRadioTurnaroundMs = 4;

    // Echo-mode (transponder) retransmit delay. An immediate bounce arrives while
    // the original sender's radio is still in TX and gets dropped, so wait briefly
    // before echoing. The MakeCode/TS relay used 20ms; we start at 5ms here.
    constexpr int kEchoDelayMs = 5;

    // Idle flush for the transparent RAW250 stream: bytes that have sat in the
    // accumulator with nothing following get sent so a partial chunk is not
    // held forever.
    constexpr int kRawIdleFlushMs = 5;

    enum Mode
    {
        MODE_MAKECODE = 0,
        MODE_RAW250
    };

    // ---------------------------------------------------------------------
    // Relay state
    // ---------------------------------------------------------------------
    struct Config
    {
        int  channel = kDefaultChannel;
        int  group   = kDefaultGroup;
        int  power   = kDefaultPower;
        Mode mode    = MODE_RAW250;  // default 250-byte mode (old MakeCode relays retired)
        bool frag    = false;       // MAKECODE over-length policy: fragment vs truncate
    };

    Config cfg;
    volatile bool dataPlane = false;    // false = command plane, true = data plane
    volatile bool echoMode  = false;    // transponder: bounce every received msg back
    uint8_t txSeq = 0;                  // rolling §5 sequence number

    char announceLine[80] = {0};        // §3.4 boot banner

    // Inbound §5 reassembly buffer (RAW250).
    uint8_t reasmBuf[kReassemblyMax];
    int     reasmLen = 0;
    bool    reasmActive = false;

    // ---------------------------------------------------------------------
    // Persistent config (flash, via uBit.storage / KeyValueStorage)
    //
    // channel/group/power/mode/frag/echo survive reset AND power-cycle, so a
    // board configured once (e.g. "RAW250 echo on channel 1") comes back exactly
    // that way when replugged. Saved on each explicit change, loaded at boot.
    // ---------------------------------------------------------------------
    const char    kCfgKey[]    = "relaycfg";
    constexpr uint8_t kCfgMagic   = 0x52;   // 'R' validity marker
    constexpr uint8_t kCfgVersion = 1;      // bump to invalidate an old layout

    struct StoredConfig
    {
        uint8_t magic;
        uint8_t version;
        uint8_t channel;
        uint8_t group;
        uint8_t power;
        uint8_t mode;       // MODE_MAKECODE / MODE_RAW250
        uint8_t frag;
        uint8_t echo;
    };

    // Persist current config to flash, but only if it actually changed (flash has
    // limited erase cycles; redundant writes would wear it for no reason).
    void saveConfig()
    {
        StoredConfig sc;
        memset(&sc, 0, sizeof(sc));
        sc.magic   = kCfgMagic;
        sc.version = kCfgVersion;
        sc.channel = (uint8_t)cfg.channel;
        sc.group   = (uint8_t)cfg.group;
        sc.power   = (uint8_t)cfg.power;
        sc.mode    = (uint8_t)cfg.mode;
        sc.frag    = cfg.frag ? 1 : 0;
        sc.echo    = echoMode ? 1 : 0;

        KeyValuePair *kv = uBit.storage.get(kCfgKey);
        if (kv != NULL)
        {
            bool same = memcmp(kv->value, &sc, sizeof(sc)) == 0;
            delete kv;
            if (same)
                return;                     // unchanged -> skip the flash write
        }
        uBit.storage.put(kCfgKey, (uint8_t *)&sc, sizeof(sc));
    }

    // Load persisted config at boot (before applyRadioConfig). Missing or
    // stale/foreign records are ignored, leaving the compiled-in defaults.
    void loadConfig()
    {
        KeyValuePair *kv = uBit.storage.get(kCfgKey);
        if (kv == NULL)
            return;
        StoredConfig sc;
        memcpy(&sc, kv->value, sizeof(sc));
        delete kv;
        if (sc.magic != kCfgMagic || sc.version != kCfgVersion)
            return;
        if (sc.channel <= 83)
            cfg.channel = sc.channel;
        cfg.group = sc.group;               // full 0..255 range is valid
        if (sc.power <= 7)
            cfg.power = sc.power;
        cfg.mode  = (sc.mode == MODE_RAW250) ? MODE_RAW250 : MODE_MAKECODE;
        cfg.frag  = sc.frag != 0;
        echoMode  = sc.echo != 0;
    }

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
    // Debug instrumentation
    //
    // RELAY_DEBUG is a compile-time gate: define it to 0 to strip the whole
    // facility (DBG/DBGHEX become no-ops, the !DEBUG command disappears) for a
    // production build. When compiled in (the default), output is still off
    // until enabled at runtime with "!DEBUG ON" -- no reflash needed. Every
    // debug line is a '#'-prefixed comment so a host parser ignores it.
    // ---------------------------------------------------------------------
#ifndef RELAY_DEBUG
#define RELAY_DEBUG 1
#endif

#if RELAY_DEBUG
    volatile bool debugEnabled = false;

    // printf-style debug line: "# DBG <formatted>".
    void dbg(const char *fmt, ...)
    {
        if (!debugEnabled)
            return;
        char buf[160];
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(buf, sizeof(buf), fmt, ap);
        va_end(ap);
        sendStr("# DBG ");
        sendStr(buf);
        sendStr("\r\n");
    }

    // Hex dump of up to `maxBytes` bytes on one debug line: "# DBG <tag> n=.. : ..".
    void dbgHex(const char *tag, const uint8_t *b, int n, int maxBytes = 40)
    {
        if (!debugEnabled)
            return;
        char buf[160];
        int off = snprintf(buf, sizeof(buf), "%s n=%d:", tag, n);
        int show = (n < maxBytes) ? n : maxBytes;
        for (int i = 0; i < show && off < (int)sizeof(buf) - 5; i++)
            off += snprintf(buf + off, sizeof(buf) - off, " %02x", b[i]);
        if (show < n && off < (int)sizeof(buf) - 5)
            snprintf(buf + off, sizeof(buf) - off, " ...");
        sendStr("# DBG ");
        sendStr(buf);
        sendStr("\r\n");
    }
  #define DBG(...)          dbg(__VA_ARGS__)
  #define DBGHEX(tag, b, n) dbgHex((tag), (b), (n))
#else
  #define DBG(...)          do {} while (0)
  #define DBGHEX(tag, b, n) do {} while (0)
#endif

    // ---------------------------------------------------------------------
    // Radio configuration
    // ---------------------------------------------------------------------

    // Force the receiver to re-acquire the CURRENT frequency/group.
    //
    // The nRF RADIO latches FREQUENCY and the group address (PREFIX0) only at an
    // RX/TX ramp-up. NRF52Radio::enable() ramps RX at the *default* frequency
    // (7) and the *old* group, and setFrequencyBand()/setGroup() afterwards only
    // poke registers -- the running receiver keeps listening on the stale
    // channel because the IRQ handler just re-issues TASKS_START (no re-ramp).
    // Only NRF52Radio::send() re-ramps RX, so a listen-only relay would sit on
    // channel 7 / group 0 and hear nothing until its first transmit. We bounce
    // RX here, mirroring the RX restart at the tail of NRF52Radio::send(), so a
    // freshly configured channel/group take effect immediately.
    void retuneReceiver()
    {
        NVIC_DisableIRQ(RADIO_IRQn);

        NRF_RADIO->EVENTS_DISABLED = 0;
        NRF_RADIO->TASKS_DISABLE = 1;
        while (NRF_RADIO->EVENTS_DISABLED == 0);

        // PACKETPTR is left pointing at the driver's rx buffer (set in enable()),
        // so we only need to ramp the receiver back up on the new settings.
        NRF_RADIO->EVENTS_READY = 0;
        NRF_RADIO->TASKS_RXEN = 1;
        while (NRF_RADIO->EVENTS_READY == 0);

        NRF_RADIO->EVENTS_END = 0;
        NRF_RADIO->TASKS_START = 1;

        NVIC_ClearPendingIRQ(RADIO_IRQn);
        NVIC_EnableIRQ(RADIO_IRQn);
    }

    void applyRadioConfig()
    {
        // §2: disable / reconfigure / re-enable for a clean state.
        uBit.radio.disable();
        uBit.radio.enable();
        uBit.radio.setFrequencyBand(cfg.channel);
        uBit.radio.setGroup(cfg.group);
        uBit.radio.setTransmitPower(cfg.power);

        // Match the on-air MAXLEN to the mode (see kMakecodeMaxLen): MAKECODE
        // must look like a stock 32-byte radio to interoperate; RAW250 uses the
        // full packet. enable() set PCNF1 from the compile-time size, so we
        // rewrite just the MAXLEN byte here, then retuneReceiver() re-ramps RX
        // so the change takes effect.
        uint32_t maxlen = (cfg.mode == MODE_RAW250) ? kRaw250MaxLen : kMakecodeMaxLen;
        NRF_RADIO->PCNF1 = (NRF_RADIO->PCNF1 & ~0xFFUL) | maxlen;

        // Apply the channel/group/maxlen to the running receiver (see retuneReceiver).
        retuneReceiver();
        DBG("radio cfg applied [rx-retuned]: ch=%d grp=%d pwr=%d mode=%s pcnf1=0x%08lx",
            cfg.channel, cfg.group, cfg.power,
            cfg.mode == MODE_MAKECODE ? "MAKECODE" : "RAW250",
            (unsigned long)NRF_RADIO->PCNF1);
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
        DBG("TX makecode str len=%d", len);
        DBGHEX("TX makecode pkt", buf, kPxtPrefix + 1 + len);
        uBit.radio.datagram.send(buf, kPxtPrefix + 1 + len);
    }

    // Transmit one §5 frame. In RAW250 the frame is the radio payload directly;
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

        if (cfg.mode == MODE_RAW250)
        {
            DBGHEX("TX raw frame", frame, kFrameHeader + len);
            uBit.radio.datagram.send(frame, kFrameHeader + len);
        }
        else
            sendMakecodeString(frame, kFrameHeader + len);
    }

    // Fragment and transmit an arbitrary-length message across §5 frames.
    // Fire-and-forget (ACK_REQ clear) per §5.3 — the default for driving a
    // stock robot. Stop-and-wait reliability layers on top of these same
    // SEQ/FLAGS fields and is the documented next increment.
    void sendFramedMessage(const uint8_t *msg, int msgLen)
    {
        const int mtu = (cfg.mode == MODE_RAW250) ? kRawMtu : kMakecodeMtu;
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
        DBG("sendLine len=%d mode=%s frag=%d", len,
            cfg.mode == MODE_MAKECODE ? "MAKECODE" : "RAW250", cfg.frag);
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

        // Echo (transponder) mode: bounce the decoded message back over the radio
        // verbatim, in the current framing. Runs from the radio receive handler;
        // the short delay lets the original sender flip TX->RX so the echo isn't
        // lost in the half-duplex gap. Matches the MakeCode/TS relay's echo rule.
        if (echoMode)
        {
            uBit.sleep(kEchoDelayMs);
            sendLine(data, len);
        }
    }

    // A MAKECODE-framed inbound packet: strip the PXT string body and emit it.
    void handleMakecodeInbound(const uint8_t *b, int n)
    {
        if (n < kPxtPrefix + 1 || b[0] != kPxtTypeString)
        {
            DBG("RX makecode: NOT a PXT string pkt (n=%d type=0x%02x, want type=0x%02x)",
                n, n > 0 ? b[0] : 0, kPxtTypeString);
            return;                                 // not a PXT string packet
        }
        int slen = b[kPxtPrefix];
        if (slen > n - (kPxtPrefix + 1))
            slen = n - (kPxtPrefix + 1);
        DBG("RX makecode: PXT string len=%d -> emit", slen);
        emitInbound(b + kPxtPrefix + 1, slen);
    }

    // A RAW250 inbound §5 frame: ACK, reassemble, and emit complete messages.
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
        DBG("RX raw frame: seq=%u flags=0x%02x len=%d", seq, flags, len);

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

#if RELAY_DEBUG
    // Diagnostics for packets the datagram path never surfaces.
    //
    // NRF52Radio fires RADIO_EVT_DATA_READY (on DEVICE_ID_RADIO) for every packet
    // it processes -- i.e. every packet that passed hardware address matching --
    // regardless of protocol. Packets whose protocol is neither DATAGRAM nor
    // EVENTBUS are routed to DEVICE_ID_RADIO_DATA_READY with value = the protocol
    // byte. So: if onAnyRadioPacket fires but onRadioFrame does not, packets are
    // arriving but not as datagrams (a protocol mismatch); if neither fires, the
    // receiver is not hearing the peer at all (tuning/address).
    void onAnyRadioPacket(MicroBitEvent)
    {
        DBG("radio: packet processed (RADIO_EVT_DATA_READY) -- something arrived");
    }

    void onUnknownProtocol(MicroBitEvent e)
    {
        DBG("radio: unknown-protocol packet, protocol=%d (DATAGRAM=1)", (int)e.value);
    }
#endif

    void onRadioFrame(MicroBitEvent)
    {
        PacketBuffer packet = uBit.radio.datagram.recv();
        int n = packet.length();
        DBG("onRadioFrame: datagram event, len=%d rssi=%d mode=%s",
            n, packet.getRSSI(), cfg.mode == MODE_MAKECODE ? "MAKECODE" : "RAW250");
        if (n <= 0)
        {
            DBG("onRadioFrame: empty packet, dropping");
            return;
        }
        uint8_t *b = packet.getBytes();
        DBGHEX("RX raw", b, n);

        if (cfg.mode == MODE_MAKECODE)
            handleMakecodeInbound(b, n);
        else
            handleRawInbound(b, n);
    }

    // ---------------------------------------------------------------------
    // Display feedback
    // ---------------------------------------------------------------------

    // 5x5 status icons. MicroBitImage pixels are brightness 0-255 (NOT on/off),
    // so lit pixels use 255 or the icon shows at ~0.4% brightness.
    const uint8_t kGhost[25] = {     // echo (transponder) mode
          0, 255, 255, 255,   0,
        255, 255, 255, 255, 255,
        255,   0, 255,   0, 255,
        255, 255, 255, 255, 255,
        255,   0, 255,   0, 255,
    };
    const uint8_t kWest[25] = {      // <- transmit/receive mode (opposite of echo)
          0,   0, 255,   0,   0,
          0, 255,   0,   0,   0,
        255, 255, 255, 255, 255,
          0, 255,   0,   0,   0,
          0,   0, 255,   0,   0,
    };
    const uint8_t kCheck[25] = {     // accept
          0,   0,   0,   0,   0,
          0,   0,   0,   0, 255,
          0,   0,   0, 255,   0,
        255,   0, 255,   0,   0,
          0, 255,   0,   0,   0,
    };
    const uint8_t kCross[25] = {     // cancel
        255,   0,   0,   0, 255,
          0, 255,   0, 255,   0,
          0,   0, 255,   0,   0,
          0, 255,   0, 255,   0,
        255,   0,   0,   0, 255,
    };

    void showIcon(const uint8_t *bmp)
    {
        MicroBitImage img(5, 5, bmp);
        uBit.display.print(img);
    }

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

    // The resting display: ghost in echo mode, otherwise the channel glyph.
    void updateDisplay()
    {
        if (echoMode)
            showIcon(kGhost);
        else
            showChannel(cfg.channel);
    }

    // Flash the packet-size mode one digit at a time: "32" for MAKECODE / 32-byte,
    // "25" for RAW250 / 250-byte.
    void flashMode()
    {
        const char *s = (cfg.mode == MODE_RAW250) ? "25" : "32";
        for (const char *p = s; *p; ++p)
        {
            uBit.display.printChar(*p);
            uBit.sleep(600);
        }
    }

    // Boot indication: echo-state icon (ghost / west arrow), then flash the
    // packet-size mode, then the channel, then settle (ghost in echo, channel in
    // transmit/receive). Reflects the persisted config loadConfig() restored.
    void startupDisplay()
    {
        showIcon(echoMode ? kGhost : kWest);
        uBit.sleep(800);
        flashMode();
        showChannel(cfg.channel);
        uBit.sleep(800);
        updateDisplay();
    }

    // Set channel and force group 10 (as !C and the buttons do), apply it to
    // the radio, and reflect it on the display. Shared by !C and the buttons.
    void setChannel(int ch)
    {
        cfg.channel = ch;
        cfg.group = 10;
        applyRadioConfig();
        updateDisplay();
        saveConfig();
    }

    // Set the echo transponder explicitly (the !ECHO serial command). The A+B
    // button uses the mode menu below instead.
    void setEcho(bool on)
    {
        echoMode = on;
        updateDisplay();
        saveConfig();
        comment(on ? "echo: ON" : "echo: OFF");
    }

    // ---------------------------------------------------------------------
    // A+B mode menu
    //
    // Each A+B press advances through the items; resting on one for
    // kMenuTimeoutMs accepts it (flash a check, apply + persist) -- except the
    // cancel (X) item, which just exits with no change. Items show the OPPOSITE
    // of the current state (the change you'd make):
    //   item 0: '3' (-> 32-byte) / '2' (-> 250-byte)
    //   item 1: ghost (-> echo)  / west arrow (-> transmit/receive)
    //   item 2: X  -> cancel
    // A background fiber (menuTimeoutFiber) handles the rest-to-accept timeout.
    // ---------------------------------------------------------------------
    constexpr int      kMenuItems     = 3;
    constexpr uint32_t kMenuTimeoutMs = 3000;
    volatile int       menuIndex      = -1;     // -1 = not in the menu
    volatile uint32_t  menuPressTime  = 0;      // systemTime() of last A+B press

    void showMenuItem(int idx)
    {
        if (idx == 0)
            uBit.display.printChar(cfg.mode == MODE_RAW250 ? '3' : '2');  // opposite packet mode
        else if (idx == 1)
            showIcon(echoMode ? kWest : kGhost);                  // opposite echo state
        else
            showIcon(kCross);                                     // cancel
    }

    void flashCheck()
    {
        showIcon(kCheck);
        uBit.sleep(900);
    }

    void commitMenu(int idx)
    {
        if (idx == 0)                       // accept packet-mode toggle (to shown opposite)
        {
            cfg.mode = (cfg.mode == MODE_RAW250) ? MODE_MAKECODE : MODE_RAW250;
            applyRadioConfig();             // switch on-air MAXLEN + retune RX
            saveConfig();
            flashCheck();
        }
        else if (idx == 1)                  // accept echo toggle (to shown opposite)
        {
            echoMode = !echoMode;
            saveConfig();
            flashCheck();
        }
        // idx == 2: cancel -- no check, no change.
        updateDisplay();
    }

    void menuTimeoutFiber()
    {
        while (true)
        {
            if (menuIndex >= 0 &&
                (uint32_t)(uBit.systemTime() - menuPressTime) >= kMenuTimeoutMs)
            {
                int idx = menuIndex;
                menuIndex = -1;             // leave the menu before committing
                commitMenu(idx);
            }
            uBit.sleep(100);
        }
    }

    // ---------------------------------------------------------------------
    // Buttons: A = channel down, B = channel up, wrapping within
    // [kChannelMin, kChannelMax]. Active only on group 10 (matching the
    // MakeCode relay) so a custom-group link is not disturbed by a press.
    // Ignored while the A+B menu is open.
    // ---------------------------------------------------------------------
    void stepChannel(int delta)
    {
        if (menuIndex >= 0)                 // menu owns the buttons
            return;
        if (cfg.group != 10)
            return;
        int ch = cfg.channel + delta;
        if (ch < kChannelMin)
            ch = kChannelMax;
        else if (ch > kChannelMax)
            ch = kChannelMin;
        setChannel(ch);
        char out[48];
        snprintf(out, sizeof(out), "channel: %d group: %d", cfg.channel, cfg.group);
        comment(out);
    }

    void onButtonA(MicroBitEvent) { stepChannel(-1); }
    void onButtonB(MicroBitEvent) { stepChannel(+1); }

    // A+B chord drives the mode menu: first press enters at item 0, each further
    // press advances; resting accepts (see commitMenu / menuTimeoutFiber).
    void onButtonAB(MicroBitEvent)
    {
        menuIndex = (menuIndex < 0) ? 0 : (menuIndex + 1) % kMenuItems;
        menuPressTime = (uint32_t)uBit.systemTime();
        showMenuItem(menuIndex);
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
        comment("!MODE MAKECODE     32-byte CODAL string framing");
        comment("!MODE RAW250       headerless framing, matching firmware (default)");
        comment("!FRAG ON|OFF       MAKECODE over-length: fragment vs truncate");
        comment("!ECHO [ON|OFF]     transponder: bounce received msgs back");
        comment("!DEFAULTS          clear saved config (defaults next reset)");
        comment("!GO                enter data plane (exit only by reset)");
        comment("> <text>           send one line over radio (command plane)");
#if RELAY_DEBUG
        comment("!DEBUG ON|OFF      toggle '# DBG' radio TX/RX logging");
#endif
        comment("buttons A/B        channel down/up (group 10)");
        comment("buttons A+B        mode menu: 32/250, echo/tx, cancel");
        comment("?                  show channel/group/mode/power");
        comment("!MODE?             show mode");
        comment("HELLO              re-request device banner");
    }

    void printConfig()
    {
        char out[80];
        snprintf(out, sizeof(out), "channel: %d group: %d mode: %s power: %d",
                 cfg.channel, cfg.group,
                 cfg.mode == MODE_MAKECODE ? "MAKECODE" : "RAW250", cfg.power);
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
            comment(cfg.mode == MODE_MAKECODE ? "mode: MAKECODE" : "mode: RAW250");
            return false;
        }
#if RELAY_DEBUG
        if (strcmp(line, "!DEBUG ON") == 0)
        {
            debugEnabled = true;
            comment("debug: ON");
            return false;
        }
        if (strcmp(line, "!DEBUG OFF") == 0)
        {
            debugEnabled = false;
            comment("debug: OFF");
            return false;
        }
        if (strcmp(line, "!DEBUG?") == 0)
        {
            comment(debugEnabled ? "debug: ON" : "debug: OFF");
            return false;
        }
        // Dump the live nRF RADIO registers so we can compare against the
        // expected micro:bit values (BASE0=0x75626974 "ubit", PCNF0=0x00000008,
        // PCNF1=0x02040000|maxpkt, CRCPOLY=0x11021, DATAWHITEIV=0x18, MODE=0).
        // STATE: 0=DISABLED .. 3=RX .. 11=TX; if it isn't sitting in RX (3) the
        // receiver isn't actually listening.
        if (strcmp(line, "!REGS") == 0)
        {
            char out[80];
            snprintf(out, sizeof(out), "STATE=%lu FREQ=%lu MODE=%lu TXPWR=0x%lx",
                     (unsigned long)NRF_RADIO->STATE, (unsigned long)NRF_RADIO->FREQUENCY,
                     (unsigned long)NRF_RADIO->MODE, (unsigned long)NRF_RADIO->TXPOWER);
            comment(out);
            snprintf(out, sizeof(out), "BASE0=0x%08lx PREFIX0=0x%08lx RXADDR=0x%lx TXADDR=%lu",
                     (unsigned long)NRF_RADIO->BASE0, (unsigned long)NRF_RADIO->PREFIX0,
                     (unsigned long)NRF_RADIO->RXADDRESSES, (unsigned long)NRF_RADIO->TXADDRESS);
            comment(out);
            snprintf(out, sizeof(out), "PCNF0=0x%08lx PCNF1=0x%08lx",
                     (unsigned long)NRF_RADIO->PCNF0, (unsigned long)NRF_RADIO->PCNF1);
            comment(out);
            snprintf(out, sizeof(out), "CRCCNF=0x%lx CRCPOLY=0x%08lx CRCINIT=0x%08lx WHITEIV=0x%lx",
                     (unsigned long)NRF_RADIO->CRCCNF, (unsigned long)NRF_RADIO->CRCPOLY,
                     (unsigned long)NRF_RADIO->CRCINIT, (unsigned long)NRF_RADIO->DATAWHITEIV);
            comment(out);
            return false;
        }
#endif

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
            applyRadioConfig();         // switch on-air MAXLEN to 32 (stock MakeCode)
            saveConfig();
            comment("mode: MAKECODE");
            return false;
        }
        // "RAW251" kept as a backward-compatible alias for the old mode name.
        if (strcmp(line, "!MODE RAW250") == 0 || strcmp(line, "!MODE RAW251") == 0)
        {
            cfg.mode = MODE_RAW250;
            applyRadioConfig();         // switch on-air MAXLEN to the full packet size
            saveConfig();
            comment("mode: RAW250");
            return false;
        }
        if (strcmp(line, "!FRAG ON") == 0)
        {
            cfg.frag = true;
            saveConfig();
            comment("frag: ON");
            return false;
        }
        if (strcmp(line, "!FRAG OFF") == 0)
        {
            cfg.frag = false;
            saveConfig();
            comment("frag: OFF");
            return false;
        }

        // Echo (transponder) mode -------------------------------------------
        // !ECHO toggles; !ECHO ON / !ECHO OFF set explicitly. Same effect as the
        // A+B chord. Lets the board run standalone as an echo server.
        if (strcmp(line, "!ECHO") == 0)
        {
            setEcho(!echoMode);
            return false;
        }
        if (strcmp(line, "!ECHO ON") == 0)
        {
            setEcho(true);
            return false;
        }
        if (strcmp(line, "!ECHO OFF") == 0)
        {
            setEcho(false);
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
                saveConfig();
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
                setChannel(ch);                     // §3.2: !C uses default group 10
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
                saveConfig();
                printConfig();
            }
            else
            {
                comment("error: usage !P <0-7>");
            }
            return false;
        }
        // Clear persisted config -> compiled-in defaults on next reset.
        if (strcmp(line, "!DEFAULTS") == 0)
        {
            uBit.storage.remove(kCfgKey);
            comment("stored config cleared; defaults apply on next reset");
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

    // RAW250: fully transparent byte stream. Accumulate up to one MTU and flush
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

    // Restore persisted channel/group/power/mode/frag/echo from flash (falls
    // back to compiled-in defaults if nothing is saved) BEFORE configuring the
    // radio, so the board comes back exactly as it was last set.
    loadConfig();

    // Bring the radio up on the (possibly restored) config and start listening.
    // The radio is live in the command plane too, so '>' sends and '<' receives
    // work before !GO (§3.1).
    applyRadioConfig();
    uBit.messageBus.listen(DEVICE_ID_RADIO, MICROBIT_RADIO_EVT_DATAGRAM, onRadioFrame);
#if RELAY_DEBUG
    // See onAnyRadioPacket/onUnknownProtocol. RADIO_EVT_DATA_READY fires per
    // processed packet (any protocol); DEVICE_ID_RADIO_DATA_READY (value 0 =
    // match any) catches non-datagram protocols.
    uBit.messageBus.listen(DEVICE_ID_RADIO, RADIO_EVT_DATA_READY, onAnyRadioPacket);
    uBit.messageBus.listen(DEVICE_ID_RADIO_DATA_READY, DEVICE_EVT_ANY, onUnknownProtocol);
#endif

    // Buttons change the channel (group 10) in either plane, with the same
    // glyph mapping as !C.
    uBit.messageBus.listen(MICROBIT_ID_BUTTON_A, MICROBIT_BUTTON_EVT_CLICK, onButtonA);
    uBit.messageBus.listen(MICROBIT_ID_BUTTON_B, MICROBIT_BUTTON_EVT_CLICK, onButtonB);
    uBit.messageBus.listen(MICROBIT_ID_BUTTON_AB, MICROBIT_BUTTON_EVT_CLICK, onButtonAB);

    // Background fiber for the A+B menu's rest-to-accept timeout (see commitMenu).
    create_fiber(menuTimeoutFiber);

    // §3.4 boot announcement: DEVICE:RADIOBRIDGE:relay:<deviceName>:<serial>
    snprintf(announceLine, sizeof(announceLine), "DEVICE:RADIOBRIDGE:relay:%s:%u",
             codal::microbit_friendly_name(),
             (unsigned)codal::microbit_serial_number());
    sendStr(announceLine);
    sendStr("\r\n");
    // Run the boot animation in the background so it doesn't delay the command
    // loop (a host can configure us immediately after the banner).
    create_fiber(startupDisplay);

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
