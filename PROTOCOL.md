# SM450 network protocol notes

This document describes how a speaker with the **Platin SM450** amplifier module can be
read and controlled over the local network. It is written for developers who want to
build their own integration (for example for Home Assistant). Everything here was found by
testing one speaker (a Lemus HOME 1500 with SM450) and by watching the traffic that the
vendor app produces. Nothing here comes from the manufacturer except where the
**SM450 datasheet v1.3** is named as the source.

SUMEL is unofficial and not affiliated with or endorsed by any manufacturer. "Lemus",
"Platin", "Google Home" and "Chromecast" are trademarks of their respective owners.

> **Use at your own risk.** The control ports have no authentication and address the
> DSP and the firmware directly. Some commands on these ports can factory-reset the
> device, start a firmware update or overwrite the manufacturer's speaker tuning. Read
> [Safety](#8-safety) before you send anything.

## Status labels

Every statement below carries one of these labels:

| Label | Meaning |
|---|---|
| **Confirmed** | We sent it to a real SM450 and checked the result (read-back, the vendor app, or by ear). |
| **Observed** | Seen in traffic from the speaker or from the vendor app, but never sent by us. |
| **Hypothesis** | Our best interpretation. Not tested. |

Tested on: one SM450 reporting MCU `177`, LS9 streaming firmware `15262` and Cast
`1.52.272222` (November 2026). Other firmware may differ.

Hex is written byte by byte, e.g. `00 03 0f 02 0d`. `<cs>` means the checksum byte.

---

## Contents

1. [Overview](#1-overview)
2. [TCP 50006: system control](#2-tcp-50006-system-control)
3. [Command table (50006)](#3-command-table-50006)
4. [EQ bands](#4-eq-bands)
5. [Room correction curve](#5-room-correction-curve)
6. [Power and waking from standby](#6-power-and-waking-from-standby)
7. [TCP 7777: LUCI](#7-tcp-7777-luci)
8. [Safety](#8-safety)
9. [Discovery](#9-discovery)
10. [Unknown and open questions](#10-unknown-and-open-questions)
11. [References](#11-references)

---

## 1. Overview

The SM450 is a 4-channel amplifier module with a DSP, Wi-Fi streaming, Bluetooth, HDMI ARC
and AUX/optical inputs.

| Part | What we know | Status |
|---|---|---|
| Module | Made by Hansong (Nanjing). The open Cast `eureka_info` fields report `model_name` **Njord** and `product_name` "2018 SM450 INTEGRATED AMPLIFIER". | **Observed** |
| Network module | **Libre Wireless LS9** (LibreSync platform). The SM450 datasheet v1.3 names LS9 as the streaming input, and the UPnP description says `manufacturer` LibreWireless. | **Observed** |
| DSP | Very likely an **Analog Devices SigmaDSP**: the datasheet describes tuning with SigmaStudio and an ADI USBi programmer, which only work with ADI parts. The chip is not named. | **Hypothesis** (strong) |
| Control MCU | A separate controller behind port 50006 (it reports its own version, `mcu`). | **Observed** |

The protocol is the same family as the one documented for the **Platin Stereo Hub** by the
open-source project *aiolibresync* (MIT). The SM450 differs in several places; the
differences are noted below.

### Ports

| Port | Use | Status |
|---|---|---|
| TCP **50006** | "System control": binary protocol to the control MCU (power, source, EQ, room correction, presets). No authentication. A shared broadcast bus. | **Confirmed** |
| TCP **7777** | **LUCI**, the Libre message-box protocol: volume, transport, play state, mute, and a tunnel that mirrors other clients' control commands. No authentication, no TLS on LS9. | **Confirmed** |
| TCP **38400** | HTTP: UPnP `description.xml` and the standard UPnP services (AVTransport, RenderingControl, ConnectionManager, QPlay). | **Confirmed** |
| UDP **1900** | SSDP (UPnP discovery). Answers `M-SEARCH` for `urn:schemas-upnp-org:device:MediaRenderer:1`. | **Confirmed** |
| Others seen open | 7000 (AirPlay), 8008/8009/8443 (Cast), 8800, 9095 (Spotify Connect), 10001 (unknown). Not used by SUMEL. | **Observed** |

A typical client keeps **one TCP connection to 50006 and one to 7777** open, and uses
38400 only for discovery and for waking the speaker.

---

## 2. TCP 50006: system control

### Framing

```
[length: 2 bytes, big-endian] [checksum: 1] [class: 1] [opcode: 1] [data: 0..n]
```

- `length` counts every byte **after** the length field: checksum + class + opcode + data.
- `checksum` = `(class + opcode + sum(data)) & 0xFF`, i.e. the sum of all bytes after the
  checksum, truncated to 8 bits.

Example, *read power*: class `02`, opcode `0d`, no data.
`length` = 3, checksum = `0x02 + 0x0d` = `0x0f` → `00 03 0f 02 0d`.

```python
def control_frame(cls: int, op: int, data: bytes = b"") -> bytes:
    body = bytes([cls, op]) + data
    return (len(body) + 1).to_bytes(2, "big") + bytes([sum(body) & 0xFF]) + body
```

| Fact | Status |
|---|---|
| Frames built with the real checksum are accepted. | **Confirmed** |
| Frames **from the speaker** carry `00` in the checksum position. SUMEL uses `checksum == 0` to tell device frames from requests sent by other clients. | **Confirmed** |
| In every vendor-app frame we captured (EQ band writes and deletes), the checksum byte was `03`, and the speaker accepted them. So the firmware probably does not validate the checksum. | **Observed** / **Hypothesis** |
| A frame length above about 1 KB has never been seen. If the length field looks impossible, drop one byte and resynchronise. | implementation advice |

### Broadcast behaviour

Port 50006 is a **shared bus**. Every connected client receives every response, no matter
who asked.

- When a source is changed (by the remote, the TV or any app), the speaker sends the source
  list (`02 02`) followed by a source/volume frame (`02 21`) to all clients unprompted.
  Volume changes from the RF remote also appear as `02 21`. **Observed**
- Responses to the vendor app's reads (room curve, room correction, manual EQ, active preset)
  arrive on our connection too. **Observed**
- When another client connected during a test, a `02 4e` response appeared on the bus.
  **Observed** (meaning not decoded)
- On the SM450 the vendor app's **own requests are not visible** on 50006 (on the Stereo Hub
  they are). The app's commands show up instead in the LUCI tunnel, box `0x70`
  (see [section 7](#7-tcp-7777-luci)). **Observed**
- Room-correction state and manual-EQ state are **not announced by themselves** when changed
  elsewhere. You only learn the new value when somebody reads it. **Observed**

Consequences for a client:

- Build your state from the frames the speaker sends, not from what you wrote. Treat a
  command as successful only when the speaker reports the new value.
- Responses can arrive late (in one test, several seconds later and all at once) and can be
  duplicated. Match on content, not on order. **Observed**
- Anyone on the LAN can change the speaker. Expect other apps to change state while you
  are connected.

### Connecting, keep-alive and watchdog

| Fact / advice | Status |
|---|---|
| The **first connection attempts are often refused or fail**. In the first test the first connection failed twice and then worked. Retry: SUMEL's discovery check tries three times (pausing 1 s, then 2 s), and the control connection keeps retrying every 3 s. | **Observed** |
| A connection that only listens never notices that the speaker has gone away (Wi-Fi drop, power loss). | **Observed** |
| SUMEL sends harmless reads every **10 s** on 50006 (room correction, power, source list, manual EQ). These double as keep-alive and catch changes that are not broadcast. | implementation advice |
| If **no data at all** arrives for **45 s**, SUMEL treats the connection as dead, closes it and reconnects. | implementation advice |
| On 7777, SUMEL sends a volume query every **30 s** as keep-alive, with the same 45 s watchdog. | implementation advice |
| Space out bursts of commands slightly (SUMEL waits about 0.2 s between start-up reads). | implementation advice |

---

## 3. Command table (50006)

`→` is what the client sends, `←` is what the speaker answers. All frames are shown with
the correct checksum.

### Reads

| Function | Request | Response | Status |
|---|---|---|---|
| Power state | `00 03 0f 02 0d` | `02 0e 01` = on, `02 0e 00` = standby. Answered even in standby. | **Confirmed** |
| Source list + volume | `00 03 03 02 01` | `02 02 <JSON>`, then `02 21 <source> <volume>` | **Confirmed** |
| Source/volume/mute (Stereo Hub `02 20`) | `00 03 22 02 20` | **No answer on SM450.** Use `02 01` instead. | **Confirmed** |
| Manual EQ on/off | `00 03 08 03 05` | `03 05 01` = on, `03 05 00` = off | **Confirmed** |
| Room correction on/off | `00 03 09 07 02` | `07 03 01` = on, `07 03 00` = off | **Confirmed** |
| Active EQ preset | `00 04 0d 08 05 00` | `08 05 02 04 00 01 00 00` (byte 7 of the frame = preset, here 0) | **Confirmed** (read) |
| EQ bands of preset *n* (0–3) | `00 05 <cs> 08 04 00 <n>` | One frame per band, see [section 4](#4-eq-bands). No bands → no answer. | **Confirmed** |
| Room correction curve | `00 03 10 07 09` | `07 0a` + 140 bytes, see [section 5](#5-room-correction-curve) | **Confirmed** |
| Unknown room-EQ value | `00 03 13 07 0c` | `07 0c 14` (value 20). Sent by the vendor app when its room-EQ screen opens. | **Observed** (meaning unknown, SUMEL does not send it) |
| Versions | `00 03 8b 02 89` | `02 89 <JSON>`, e.g. `{"mcu":"177","ls9":"15262","cast":"1.52.272222"}`. Same numbers as the vendor app's settings page. | **Confirmed** |

#### Source list (`02 02`) and source/volume (`02 21`)

The source list is UTF-8 JSON. Captured from our speaker (HDMI active):

```json
{"src":[{"ix":1,"name":"HDMI","sts":1},{"ix":2,"name":"AUX/OPT","sts":0},
        {"ix":3,"name":"Bluetooth","sts":0},{"ix":4,"name":"Streaming","sts":0}]}
```

- `ix` is the source code used by *select source*; `sts: 1` marks the active source.
  **Confirmed**
- On the SM450 the codes are **1–4** (Stereo Hub uses 0–9). Always take them from the list.
  **Confirmed**

The source/volume frame on SM450 is **7 bytes** (Stereo Hub: 9 bytes, with mute and audio
activity). Captured: `00 05 00 02 21 01 0d` = source 1, volume 13.

| Fact | Status |
|---|---|
| Byte 6 = volume (0–100). Follows the RF remote. | **Confirmed** |
| Byte 5 = source code. Matched the JSON `ix` of the active source in our captures. | **Observed** |
| No mute or audio-activity field on SM450. | **Observed** |

### Writes

| Function | Command | Effect / answer | Status |
|---|---|---|---|
| Manual EQ off | `00 04 07 03 04 00` | `03 05 00` on read-back; vendor app shows EQ disabled | **Confirmed** |
| Manual EQ on | `00 04 08 03 04 01` | `03 05 01` on read-back | **Confirmed** |
| Room correction off | `00 04 08 07 01 00` | Read back with `07 02` (not broadcast). The stored measurement is kept. | **Confirmed** |
| Room correction on | `00 04 09 07 01 01` | as above | **Confirmed** |
| Select source *ix* | `00 04 <cs> 02 03 <ix>`, e.g. `00 04 09 02 03 04` (Streaming) | `02 02` + `02 21` with the new source. Ignored in standby. | **Confirmed** |
| Power toggle | `00 04 11 02 0f 00` | **Toggles.** Turns the speaker off when on. Ignored in standby. See [section 6](#6-power-and-waking-from-standby). | **Confirmed** (off) |
| Select preset 1–3 | `00 07 <cs> 08 02 01 <n> 01 02` | On SM450 the EQ changes for a few seconds and then **reverts** to preset 0. Not useful. | **Confirmed** (behaviour) |
| Write EQ band | `00 23 <cs> 08 01 ...` (37-byte frame, length field 35) | see [section 4](#4-eq-bands) | **Confirmed** (Parametric EQ) |
| Band on / off / delete | `00 07 <cs> 08 06 01 <ix> 01 <state>` | see [section 4](#4-eq-bands) | **Confirmed** (delete), **Observed** (on/off) |
| Remote pairing? | `03 03` | A frame with class `03`, opcode `03` was recorded in traffic. The vendor app contains names such as `REMOTE_PAIRING_GET/SET`, so it is probably related to pairing the RF remote. The reply and data were not decoded. Never sent by us. | **Observed** (meaning: **Hypothesis**) |
| Unknown, after EQ changes | `07 0d 06` | The vendor app sends it after each band change. Meaning unknown. | **Observed** (not sent by us) |

> **Preset warning.** In `08 02 01 <n> 01 <op>` the last byte **must be `02` (select)**.
> According to the Stereo Hub documentation, `00`/`01` store the current editing curve over
> the preset and erase what was there. Never send anything but `02`. (**Hypothesis** for
> SM450; we have never sent `00`/`01`.)

### Mute

Mute is **not** a 50006 command on the SM450; see LUCI box `0x3F` in
[section 7](#7-tcp-7777-luci).

### Volume

Volume is **set** over LUCI box `0x40` (section 7) and **read** from either `02 21` (50006)
or LUCI box `0x40`. **Confirmed**

---

## 4. EQ bands

The vendor app's "manual EQ" is a short list of filters ("bands") stored in the speaker.
Each band has a fixed slot (index). Slots do not move when another band is deleted.

**Only slots 0, 1 and 2 may be written.** See [Safety](#8-safety).

### Reading bands (`08 04`)

Request bands of preset 0 (the vendor app's editing slot):

```
→ 00 05 0c 08 04 00 00
← 00 0f 00 08 04 02 <ix> <10 band bytes>      (one frame per band)
```

If there are no bands, the speaker sends nothing. Use a timeout and treat silence as an
empty list. **Confirmed**

Example (captured): `00 0f 00 08 04 02 00 09 07 01 03 00 01 00 02 00 02`
= slot 0, Parametric EQ, 300 Hz, Q 1.00, +2.00 dB, on.

The 10 band bytes:

| Byte | Content | Status |
|---|---|---|
| 0 | `09` (probably the length of what follows) | **Observed**, never changes |
| 1 | Filter type, see table below | **Confirmed** (all seven) |
| 2 | `01`, meaning unknown | **Observed**, never changes |
| 3–4 | Frequency in Hz = `b3 × 100 + b4` (`00 2d` = 45 Hz, `78 00` = 12 000 Hz) | **Confirmed** |
| 5–6 | Q = `b5 + b6 / 100` (`00 46` = 0.70, `04 32` = 4.50). `ff ff` = not used by this type | **Confirmed** |
| 7–8 | Gain dB = `s8(b7) + s8(b8) / 100`, both bytes signed (`fe ce` = −2.50, `00 ce` = −0.50). `ff ff` = not used | **Confirmed** |
| 9 | `02` = band on, `01` = band off | **Confirmed** |

Note on Q: the speaker decodes Q as `int + hundredths / 100` without rounding. Some values
(e.g. 1.89) do not survive a float round-trip exactly. SUMEL nudges such values by 0.01 so a
read-back compares equal.

Filter type codes (byte 1):

| Code | Type | Uses Q | Uses gain |
|---|---|---|---|
| `01` | 6 dB high-pass (1st order) | no (`ff ff`) | no (`ff ff`) |
| `02` | 12 dB high-pass (2nd order) | yes | no |
| `03` | 6 dB low-pass (1st order) | no | no |
| `04` | 12 dB low-pass (2nd order) | yes | no |
| `05` | High shelving | stored as 1.00, not used | yes |
| `06` | Low shelving | stored as 1.00, not used | yes |
| `07` | Parametric EQ (peaking) | yes | yes |

All seven codes were **Confirmed** by setting each type in the vendor app and reading it back.

### Writing a band (`08 01`)

Creating a new band and changing an existing one use the same command. The band is written
as five biquad coefficients followed by the band parameters.

```
00 23 <cs> 08 01  01 <ix> 1d  <20 bytes: 5 coefficients>  <9 bytes: band bytes 1..9>
```

- `01` before the index: meaning unknown, identical in all captures. **Observed**
- `<ix>`: slot 0–2. A new band goes into a free slot. **Confirmed**
- `1d` = 29 = number of bytes that follow (20 + 9). **Confirmed**
- The 9 band bytes are bytes 1–9 of the read format above (without the leading `09`).
  **Confirmed**

**Coefficients:** five signed 32-bit big-endian integers in **8.24 fixed point**
(value × 2²⁴, rounded), in the order

```
[ b2, b1, b0, −a2, −a1 ]   all divided by a0
```

computed for **fs = 48 kHz**. **Confirmed** (the speaker accepts our coefficients, and reading
the band back shows the new band values; the coefficients themselves cannot be read back).

Full example, slot 0, Parametric EQ, 1000 Hz, Q 1.00, +2.00 dB, on:

```
00 23 38 08 01 01 00 1d
00 e0 36 69   fe 20 48 88   01 03 a4 c2   ff 1c 24 d5   01 df b7 78
07 01 0a 00 01 00 02 00 02
```

The coefficient integers are `[14693993, -31438712, 17016002, -14932779, 31438712]`.
The vendor app sent the same frame byte for byte, except `03` in the checksum position.
**Confirmed** (and **Observed** from the app)

After a write the vendor app sends *manual EQ on* (`03 04 01`) and reads the bands back.
Do the same: re-read with `08 04` and compare. Read-backs may be delayed or stale, so retry
the read a few times before declaring failure. **Confirmed**

### Band on / off / delete (`08 06`)

```
→ 00 07 <cs> 08 06 01 <ix> 01 <state>      state: 00 = delete, 01 = off, 02 = on
← 00 07 00 08 06 02 <ix> 01 <state>        acknowledgement
```

Examples: delete slot 2 = `00 07 12 08 06 01 02 01 00`; turn slot 1 off =
`00 07 12 08 06 01 01 01 01`.

| Fact | Status |
|---|---|
| Delete works when sent by SUMEL. | **Confirmed** |
| Other bands keep their slots (in a vendor-app capture, deleting slot 1 of 0/1/2 left 0 and 2; the next delete used index 2). | **Observed** |
| Off/on: the vendor app sends state `01`/`02`, the speaker acknowledges, and read-back shows byte 9 = `01` / `02`. SUMEL sends the same frame, but this was not tested separately. | **Observed** |

### Filter formulas

The vendor app computes the coefficients itself. Which formula it uses per type was found by
comparing captured write frames with candidate formulas. A formula is accepted only if **all
five** coefficients match to within **±1 LSB** (2⁻²⁴; two of six peaking captures differed by
1 LSB in b1/−a1, which is inaudible).

| Type | Formula | Status |
|---|---|---|
| `07` Parametric | RBJ Audio EQ Cookbook *peakingEQ* with the band's Q | **Confirmed**: 4 of 6 captures bit-exact, 2 within 1 LSB (gains −3…+3 dB, Q 1 and 2), and written by us |
| `06` Low shelving | RBJ *lowShelf*, fixed shelf slope **S = 1** (Q ignored) | **Observed** bit-exact (±1 LSB) |
| `05` High shelving | RBJ *highShelf*, S = 1 | **Observed** bit-exact (±1 LSB) |
| `02` 12 dB high-pass | RBJ *HPF* with the band's Q | **Observed** bit-exact (±1 LSB) |
| `04` 12 dB low-pass | not captured; candidates: RBJ *LPF* with Q, or Butterworth | **Hypothesis** |
| `01` 6 dB high-pass | not captured; candidates: bilinear 1st order, or one-pole | **Hypothesis** |
| `03` 6 dB low-pass | not captured; candidates as above | **Hypothesis** |

RBJ peaking in device order (Python):

```python
import math
def peaking(freq, q, gain_db, fs=48000):
    A = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * freq / fs
    alpha = math.sin(w0) / (2 * q)
    a0 = 1 + alpha / A
    vals = [(1 - alpha * A) / a0,        # b2
            -2 * math.cos(w0) / a0,      # b1
            (1 + alpha * A) / a0,        # b0
            -(1 - alpha / A) / a0,       # -a2
            2 * math.cos(w0) / a0]       # -a1
    return [round(v * 2**24) for v in vals]
```

**Learning the rest.** SUMEL does not guess the unconfirmed types. While it is connected to
7777 it sees the vendor app's write frames in the tunnel (box `0x70`). For each captured
`08 01` frame it computes every candidate formula for that type from the frame's own
frequency/Q/gain, and adopts a formula only if all five coefficients match within ±1 LSB.
Only then can that type be written. The same mechanism records how the app writes a band that
is saved as "off" (unity coefficients or unchanged coefficients); this is not yet known.

---

## 5. Room correction curve

Room correction is measured with the vendor app; the result is stored in the speaker.
*Hypothesis:* the filters are calculated in the app and uploaded (the app contains names such as
`calcRoomEqFilter` and `biquadFilters`). The datasheet shows room correction as its own two-channel
filter block, controlled by the firmware and switched on/off as a whole; it is separate from the
three biquads reserved for the app EQ, and its address and number of filters are not given.
Over the network we can only switch it on/off (`07 01`) and read the stored curve.

```
→ 00 03 10 07 09
← 00 8f 00 07 0a <140 bytes>          (length 0x8f = 143 = checksum + class + opcode + 140)
```

The curve is stored in the speaker: when the vendor app opened its room-EQ screen, the reply
was byte for byte the same as in our earlier read. **Observed**

| Bytes | Content | Status |
|---|---|---|
| 0–1 | `00 0a` header, meaning unknown | **Observed** |
| 2–70 | **Measured** response, 69 points (red curve in the vendor app) | **Confirmed** |
| 71 | `12` (0x12), meaning unknown | **Observed** |
| 72–139 | **Result after correction**, 68 points (green curve) | **Confirmed** |

Scale: point *i* is at `20 Hz × 2^(i/12)` (1/12 octave from 20 Hz to about 1 kHz), value in
dB = `(byte − 100) / 10` (so `0x64` = 0 dB).

Checked against the vendor app's graph: the result curve matches within about ±0.3 dB; the
measured curve has the same shape and peak/dip positions, but the app draws its peaks lower
(probably smoothed or clipped for display). On our speaker both curves are flat above about
550 Hz, so the correction only acts in the bass and lower midrange. **Confirmed**

---

## 6. Power and waking from standby

| Fact | Status |
|---|---|
| `02 0f 00` is a **toggle**. Read `02 0d` first and only send it when the speaker is on, to turn it off. | **Confirmed** |
| In **standby** the control MCU still answers reads (`02 0e 00`) but **ignores** power toggle and source select. | **Confirmed** |
| The RF remote talks directly to the amplifier (2-way RF, not Bluetooth or network). | **Observed** (datasheet + behaviour) |
| With an HDMI source the speaker sometimes turned itself on again 3–4 s after being switched off, probably from the TV's signal or CEC. | **Observed** |
| According to the SM450 datasheet v1.3, the module goes to standby after 20 minutes without audio (not adjustable), wakes by itself when an input signal appears, takes about 8 s to wake, and the DSP is not reachable while in standby. | datasheet |

### Waking via DLNA (**Confirmed**)

The LS9 network module stays awake in standby and is a normal UPnP MediaRenderer on port
38400. Playing audio on it wakes the amplifier.

1. `GET http://<speaker>:38400/description.xml` and find the `controlURL` of
   `urn:schemas-upnp-org:service:AVTransport:1`.
2. Serve a short WAV (SUMEL uses 3 s of stereo 16-bit 44.1 kHz silence) from a small HTTP
   server on the client.
3. SOAP `SetAVTransportURI` (InstanceID 0, the WAV URL, DIDL-Lite metadata with
   `protocolInfo="http-get:*:audio/wav:*"`), then `Play` (Speed 1).
4. Wait a few seconds, then `Stop`.
5. Poll `02 0d` until it reports on (allow about 8 s).
6. The speaker is now on the **Streaming** source. Re-select the source that was active
   before standby with `02 03 <ix>`.

The whole sequence takes about 5–8 s.

---

## 7. TCP 7777: LUCI

### Framing

```
[RemoteID: 2] [type: 1] [box: 2] [status: 1] [CRC: 2] [length: 2] [payload: length]
```

The header is 10 bytes.

| Field | Client → speaker | Speaker → client | Status |
|---|---|---|---|
| 16-bit fields (RemoteID, box, CRC, length) | **little-endian** | **big-endian** | **Confirmed** |
| RemoteID | `00 00` | | **Confirmed** |
| type | `01` = query (get), `02` = command (set) | | **Confirmed** |
| status | `00` | | **Confirmed** |
| CRC | `00 00` (accepted) | | **Confirmed** |
| payload | ASCII text | ASCII text | **Confirmed** |

### Registration

Right after connecting, register by sending your own LAN IP address as ASCII to box
`0x0003`. **Confirmed** (SUMEL always registers first; behaviour without registration has not been tested.)

```
00 00 02 03 00 00 00 00 0b 00  31 39 32 2e 31 36 38 2e 30 2e 39      "192.168.0.9"
```

### Boxes used

| Box | Use | Example | Status |
|---|---|---|---|
| `0x0040` (64) | **Volume** 0–100, ASCII decimal. Query or set. The speaker reports changes. | set 13: `00 00 02 40 00 00 00 00 02 00 31 33`; query: `00 00 01 40 00 00 00 00 00 00` | **Confirmed** |
| `0x0028` (40) | **Transport**: `PLAY`, `PAUSE`, `STOP`, `NEXT`, `PREV` (streaming sources) | pause: `00 00 02 28 00 00 00 00 05 00 50 41 55 53 45` | **Confirmed** (play/pause/next/prev) |
| `0x0033` (51) | **Play state** notifications, ASCII number: `0` playing, `1` stopped, `2` paused, `5` loading (values as documented by aiolibresync for the Stereo Hub; SUMEL only listens) | | **Hypothesis** for SM450 (not recorded in our log) |
| `0x003F` (63) | **Mute** state: the speaker reports `MUTE` / `UNMUTE` when the RF remote's mute key is pressed. Volume is not changed. No 50006 traffic is produced. | | **Observed** |
| `0x003F` (63) | **Mute command**: sending `MUTE` / `UNMUTE` (type 2) to the same box, and querying it (type 1). | `00 00 02 3f 00 00 00 00 04 00 4d 55 54 45` | **Hypothesis** |
| `0x0070` (112) | **Tunnel**: the payload contains one or more 50006-format frames that **other clients** (the vendor app) send to the control MCU. Use it to follow and learn from the vendor app. | | **Observed** |

Volume safety: never send a volume on connect or page load, and limit the size of each step
(SUMEL refuses jumps larger than 25 by default). Lowering the volume is always safe.

Mute fallback (as implemented in SUMEL): send `MUTE` to box `0x3F` and wait about 2 s for
the box to report `MUTE`. If it does not, remember the volume, set volume 0, and on unmute
send `UNMUTE` once (in case the unconfirmed `MUTE` worked silently) and ramp the volume back
in small steps.

> **WARNING: do not sweep LUCI boxes.** Port 7777 addresses the Libre module's internal
> message boxes directly. The same message space contains **factory reset**, **firmware
> update** and device identity boxes. Sending to an unknown box can reset or brick the
> network module. Use a closed allow-list of boxes, as SUMEL does
> (`lemus_remote/protocol.py`, `LUCI_ALLOWED`): register (`0x03`), transport (`0x28`),
> volume (`0x40`) and mute (`0x3F`). Nothing else is ever sent.

Note: on the newer Libre **LS10** module, port 7777 is reported to be wrapped in mutual TLS.
The SM450's LS9 is plain TCP. A future firmware could change this. **Observed** (third-party report)

---

## 8. Safety

### Only three EQ slots

**Write EQ coefficients only to slots 0, 1 and 2.**

- The SM450 datasheet v1.3 describes the PRE-EQ2 block of the DSP as having ten biquads, of
  which only three are set aside for the app's EQ. The other biquads in PRE-EQ2, and all of
  PRE-EQ1 and the per-channel EQ blocks, hold the **manufacturer's tuning** of the speaker
  (crossover, driver EQ, delay). (datasheet)
- The vendor app also uses at most three bands. **Observed**
- When we created a **fourth band (slot 3)**, the speaker made an audible **pop** and sounded
  wrong afterwards. **Confirmed** (by ear, once)
- Our interpretation: slot 3 maps onto a biquad that belongs to the manufacturer's tuning.
  A full power cycle (mains off/on) probably reloads the DSP from its stored configuration.
  **Hypothesis**, not verified.

SUMEL enforces the limit at the lowest level (`protocol.MAX_BANDS = 3`): no frame with
coefficients can be built for slot 3 or higher. A band found in slot 3+ is shown as
read-only and can only be deleted.

### Never touch

- **Crossover, sub low-pass, bass high-pass, per-channel EQ, channel delay/levels.** These
  are the manufacturer's tuning. According to the datasheet each driver channel has its own
  EQ blocks (14 + 8 biquads) and delay; none of it is reachable through any command we know,
  and none should be.
- The datasheet lists several licensed algorithms (loudness, adaptive bass, excursion
  control, pink noise, all-pass) that are not meant to be changed.
- **Factory reset, DSP reset, firmware update.** Present in the vendor app's settings and in
  the LUCI message space. Never send.
- **`08 02` with a last byte other than `02`** (overwrites presets).
- **Commands derived by guessing** (e.g. "the set version of a known get"). Only send frames
  that have been seen working.

The datasheet states that the DSP configuration is not overwritten by the module's automatic
firmware updates (the firmware itself is updated automatically from the manufacturer's server
when the speaker is online, which may change this protocol at any time).

---

## 9. Discovery

SUMEL finds speakers without sending anything to the control ports.

1. **SSDP**: send `M-SEARCH` to `239.255.255.250:1900` (send it three times; UDP gets lost):

   ```
   M-SEARCH * HTTP/1.1
   HOST: 239.255.255.250:1900
   MAN: "ssdp:discover"
   MX: 2
   ST: urn:schemas-upnp-org:device:MediaRenderer:1
   ```

   The SM450 answers with `LOCATION: http://<ip>:38400/description.xml` and
   `SERVER: Platform 1.0 CyberLinkC/3.0 UPnP/1.0 DLNADOC/1.50`. **Confirmed**
2. **Description**: `GET` the `LOCATION` (only if its host equals the address the SSDP reply
   came from). Accept the device if `<manufacturer>` contains **LibreWireless** or
   `<modelDescription>` contains **LibreSync** (the SM450 says `LibreSyncDMR`). Use
   `<friendlyName>` (e.g. "Lemus Home 1500") as the name and `<UDN>` as a stable ID.
   `<serialNumber>` is a placeholder (`1234567890`) and must not be used. **Confirmed**
3. **Confirm**: open a TCP connection to **50006** and close it without sending anything. This
   separates a controllable SM450 from other Libre devices. Retry a few times; first attempts
   are often refused. **Confirmed**

Fallbacks for networks where multicast does not work (firewalls, some routers with Wi-Fi
client isolation or multicast filtering):

- send the same `M-SEARCH` as **unicast** to known addresses (e.g. the last address that
  worked);
- fetch `http://<known-ip>:38400/description.xml` directly;
- as a last resort, a gentle TCP scan of the client's own private /24: connect to port
  **38400** only (close without sending; at most 64 at a time, 1.5 s timeout, one retry for
  hosts that refused), then do step 2 and 3 for hosts that answered. Never send anything to
  50006 or 7777 during a search.

Alternative: the Cast `eureka_info` endpoint reports `model_name` **Njord**, which can be used
together with Bonjour `_googlecast._tcp`. **Observed** (SUMEL does not use it.)

---

## 10. Unknown and open questions

| Item | Notes |
|---|---|
| `02 21` byte 5 | Matched the JSON `ix` in our captures; not proven for all sources. |
| Band byte 0 (`09`) and byte 2 (`01`) | Constant. Possibly length and a channel/flag. |
| `01` before the slot index in `08 01` / `08 06` | Constant in all captures. |
| Mapping of slots 0–2 to datasheet biquads | Probably biquads 1, 3 and 5 of PRE-EQ2; not verified. |
| What exactly slot 3+ overwrites, and whether a power cycle restores it | Unknown. Do not test. |
| Formulas for 12 dB low-pass, 6 dB high-pass, 6 dB low-pass | Not captured (learned at runtime by SUMEL). |
| How a band saved as "off" is written (coefficients) | Not captured. Use `08 06` state `01` instead. |
| Presets 1–3 on SM450 | They revert to preset 0 after a few seconds. The vendor app only uses preset 0. |
| Room curve header `00 0a` and byte 71 (`0x12`) | Unknown. |
| `07 0c` → `07 0c 14` | Unknown value 20 (candidates: number of correction filters, a dB limit). |
| `07 0d 06` | Sent by the vendor app after every EQ change. Unknown. |
| `02 4e` | Appeared when another client connected. Not decoded. |
| Mute command on box `0x3F` | Status messages are observed; whether the box accepts `MUTE`/`UNMUTE` is untested. |
| Remote pairing (`03 03`) | A `03 03` frame was recorded; that it pairs the RF remote is a guess based on names in the vendor app. Data and reply are not decoded. Never sent by us. |
| Standby time / auto-standby | Names exist in the vendor app, but there is no such setting for the SM450, and the datasheet says the 20-minute timeout is fixed. |
| Input balance, loudness | Only names seen in the vendor app. No frames captured. |
| Firmware changes | The protocol may change with any automatic firmware update. |

---

## 11. References

- *aiolibresync* (MIT) – open-source documentation of the LibreSync system-control and LUCI
  protocols for the Platin Stereo Hub: <https://pypi.org/project/aiolibresync/>
- SM450 datasheet v1.3 (Platin Solutions) – facts cited above are paraphrased.
- RBJ Audio EQ Cookbook – biquad formulas.
- UPnP Device Architecture and AVTransport:1 – SSDP, description.xml, SOAP actions.
- SUMEL source: `lemus_remote/protocol.py` (frames), `device.py` (connection, keep-alive,
  confirmation), `filters.py` (formulas and learning), `discovery.py`, `vekking.py` (DLNA wake).
