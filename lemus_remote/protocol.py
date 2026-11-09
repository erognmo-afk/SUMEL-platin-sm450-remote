"""Rammer for Platin SM450 (Libre Wireless LibreSync).

Kun en lukket liste med kjente kommandoer kan bygges her. Kildene er
aiolibresync (docs/commands.md, MIT) og testene mot vår egen SM450
(docs/lesetest-resultat.md).

* TCP 50006, "System Control": ``[lengde:2 BE][sjekksum][klasse][opcode][data]``.
  Sjekksummen er ``sum(bytes[3:]) & 0xFF``.
* TCP 7777, "LUCI": ``[RemoteID:2][type][cmd:2][status][CRC:2][lengde:2][payload]``.
  Fra klient er 16-bits felt little-endian. Fra enheten er de big-endian.

Med vilje er det ingen generell "send vilkårlig ramme"-funksjon. Port 7777
adresserer interne meldingsbokser direkte, også fabrikkreset og
fastvareoppgradering, så bare boksene i ``LUCI_ALLOWED`` kan brukes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator

# ---------------------------------------------------------------- port 50006

MAX_FRAME = 1024


def control_frame(cls: int, op: int, data: bytes = b"") -> bytes:
    body = bytes([cls, op]) + data
    return (len(body) + 1).to_bytes(2, "big") + bytes([sum(body) & 0xFF]) + body


# Lesing
GET_POWER = control_frame(0x02, 0x0D)          # 00 03 0f 02 0d
GET_SOURCES = control_frame(0x02, 0x01)        # 00 03 03 02 01 (gir 02 02 + 02 21)
GET_MANUAL_EQ = control_frame(0x03, 0x05)      # 00 03 08 03 05
GET_ROOM_CORRECTION = control_frame(0x07, 0x02)  # 00 03 09 07 02
GET_ACTIVE_PRESET = control_frame(0x08, 0x05, b"\x00")  # 00 04 0d 08 05 00


def get_filters(preset: int = 0) -> bytes:
    """Les EQ-filtrene i et preset. Bekreftet som lesing på vår SM450 (fase C)."""
    if preset not in (0, 1, 2, 3):
        raise ValueError("preset må være 0-3")
    return control_frame(0x08, 0x04, bytes([0x00, preset]))  # 00 05 0c+n 08 04 00 n


FILTER_TYPES = {  # bekreftet på vår SM450 8.11.2026
    0x01: "6dB highpass",
    0x02: "12dB highpass",
    0x03: "1st order LP",
    0x04: "2nd order LP",
    0x05: "High shelving",
    0x06: "Low shelving",
    0x07: "Parametric EQ",
}


def _s8(b: int) -> int:
    return b - 256 if b >= 128 else b


def parse_band(raw: bytes) -> dict | None:
    """Ett EQ-bånd (10 byte) fra get_filters. Bekreftet mot Platin-appen 8.11.2026.

    raw[0] = 09 (lengde?), raw[1] = filtertype, raw[2] = 01 (ukjent),
    frekvens = raw[3] * 100 + raw[4] Hz,
    Q = raw[5] + raw[6] / 100, gain = s8(raw[7]) + s8(raw[8]) / 100 dB,
    ``ff ff`` = feltet brukes ikke for denne filtertypen,
    raw[9] = 02 på / 01 av.
    """
    if len(raw) < 10:
        return None
    q = None if raw[5:7] == b"\xff\xff" else round(raw[5] + raw[6] / 100, 2)
    gain = None if raw[7:9] == b"\xff\xff" else round(_s8(raw[7]) + _s8(raw[8]) / 100, 2)
    return {
        "type": FILTER_TYPES.get(raw[1], f"type {raw[1]:02x}"),
        "type_code": raw[1],
        "freq": raw[3] * 100 + raw[4],
        "q": q,
        "gain": gain,
        "enabled": {0x02: True, 0x01: False}.get(raw[9]),
        "b2": raw[2],
    }


FS = 48000  # samplingsfrekvens som Platin-appen regner koeffisienter for (bekreftet bit for bit)


def peaking_coefficients(freq: float, q: float, gain_db: float, fs: int = FS) -> list[int]:
    """RBJ Audio EQ Cookbook, peaking EQ, i SM450-rekkefølge og 8.24 fast komma.

    Rekkefølgen er [b2, b1, b0, -a2, -a1], normalisert med a0 og avrundet.
    Bekreftet bit for bit mot to opptak av Platin-appen (1000 Hz, Q 1, +2 og +3 dB).
    """
    a = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * freq / fs
    alpha = math.sin(w0) / (2 * q)
    a0 = 1 + alpha / a
    vals = [(1 - alpha * a) / a0, -2 * math.cos(w0) / a0, (1 + alpha * a) / a0,
            -(1 - alpha / a) / a0, 2 * math.cos(w0) / a0]
    return [round(v * 2 ** 24) for v in vals]


def _hundredths(value: float, signed: bool) -> bytes:
    total = round(value * 100)
    whole, frac = (abs(total) // 100, abs(total) % 100)
    if total < 0:
        if not signed:
            raise ValueError("negativ verdi ikke tillatt")
        whole, frac = -whole, -frac
    return bytes([whole & 0xFF, frac & 0xFF])


def encode_band_params(type_code: int, freq: int, q: float | None, gain: float | None,
                       enabled: bool = True) -> bytes:
    """De 9 siste bytene i et bånd: type, 01, frekvens, Q, gain, av/på (uten lengdebyten 09)."""
    if not 20 <= freq <= 20000:
        raise ValueError("frekvens må være 20-20000 Hz")
    if q is not None and not 0.1 <= q <= 20:
        raise ValueError("Q må være 0.1-20")
    if gain is not None and not -15 <= gain <= 15:
        raise ValueError("gain må være mellom -15 og +15 dB")
    qb = b"\xff\xff" if q is None else _hundredths(q, signed=False)
    gb = b"\xff\xff" if gain is None else _hundredths(gain, signed=True)
    return (bytes([type_code, 0x01, freq // 100, freq % 100]) + qb + gb
            + bytes([0x02 if enabled else 0x01]))


# Datablad SM450 v1.3: bare biquad 1, 3 og 5 i PRE-EQ2 er satt av til appens EQ. Resten er
# produsentens tuning (delefilter, høyttaler-EQ). SM450 tar trygt imot tre EQ-bånd (plass 0-2), som i Platin-appen. Et fjerde bånd (plass 3)
# ga et smell i høyttalerne og rar lyd 8.11.2026. Skriving av koeffisienter til plass 3 og
# høyere er derfor sperret her, på laveste nivå, slik at ingen del av programmet kan gjøre det.
MAX_BANDS = 3


def _check_write_index(index: int) -> None:
    if not 0 <= index < MAX_BANDS:
        raise ValueError(f"SM450 tar høyst {MAX_BANDS} EQ-bånd (plass {index + 1} er sperret)")


def write_parametric_band(index: int, freq: int, q: float, gain: float, enabled: bool = True) -> bytes:
    """Skriv ett Parametric EQ-bånd: ``08 01 01 <indeks> 1d <20 byte koeffisienter> <9 byte verdier>``.

    Formatet er tatt fra to opptak av Platin-appen 8.11.2026. Bare Parametric EQ er bekreftet.
    Byten ``01`` foran indeksen er ikke forstått, men den er lik i alle opptak.
    """
    _check_write_index(index)
    params = encode_band_params(0x07, freq, q, gain, enabled)
    coeffs = b"".join(c.to_bytes(4, "big", signed=True) for c in peaking_coefficients(freq, q, gain))
    body = coeffs + params
    return control_frame(0x08, 0x01, bytes([0x01, index, len(body)]) + body)


def write_band_raw(index: int, coeffs: list[int], params: bytes) -> bytes:
    """Generell skrivekommando for ett bånd: koeffisienter (5 x 8.24) + 9 byte båndverdier."""
    _check_write_index(index)
    if len(coeffs) != 5 or len(params) != 9:
        raise ValueError("feil lengde på koeffisienter eller båndverdier")
    body = b"".join(int(c).to_bytes(4, "big", signed=True) for c in coeffs) + params
    return control_frame(0x08, 0x01, bytes([0x01, index, len(body)]) + body)


def parse_write_band(frame: bytes) -> dict | None:
    """Tolker en skrivekommando (08 01) fra en annen klient, slik den ses i 7777-tunnelen."""
    if len(frame) != 37 or frame[3:5] != b"\x08\x01" or frame[5] != 0x01 or frame[7] != 0x1D:
        return None
    coeffs = [int.from_bytes(frame[8 + 4 * i:12 + 4 * i], "big", signed=True) for i in range(5)]
    band = parse_band(b"\x09" + frame[28:37])
    if not band:
        return None
    return {"index": frame[6], "coeffs": coeffs, **band}


BAND_DELETE, BAND_OFF, BAND_ON = 0x00, 0x01, 0x02


def set_band_state(index: int, state: int) -> bytes:
    """Båndstatus: ``08 06 01 <indeks> 01 <00 slett | 01 av | 02 på>`` (opptak 8.11.2026).

    Høyttaleren bekrefter med ``08 06 02 <indeks> 01 <status>``.
    """
    if not 0 <= index <= 9 or state not in (BAND_DELETE, BAND_OFF, BAND_ON):
        raise ValueError("ugyldig bånd eller status")
    return control_frame(0x08, 0x06, bytes([0x01, index, 0x01, state]))


def delete_band(index: int) -> bytes:
    """Slett bånd på fast plass ``index``: ``08 06 01 <indeks> 01 00``.

    Tatt fra opptak av Platin-appen 8.11.2026. Høyttaleren svarer ``08 06 02 <indeks> 01 00``.
    Andre bånd beholder plassene sine (de flyttes ikke).
    """
    return set_band_state(index, BAND_DELETE)


def decode_delete_ack(frame: bytes) -> int | None:
    """Høyttalerens bekreftelse på båndstatus (slett/av/på): ``00 07 00 08 06 02 <indeks> 01 <status>``."""
    if len(frame) >= 7 and frame[2] == 0 and frame[3:6] == b"\x08\x06\x02":
        return frame[6]
    return None


def describe_band(raw: bytes) -> str:
    b = parse_band(raw)
    if not b:
        return ""
    parts = [b["type"], f"{b['freq']} Hz"]
    if b["q"] is not None:
        parts.append(f"Q {b['q']:.2f}")
    if b["gain"] is not None:
        parts.append(f"gain {b['gain']:+.2f} dB")
    parts.append({True: "på", False: "AV", None: f"byte9={raw[9]:02x}"}[b["enabled"]])
    return ", ".join(parts)


def decode_filter(frame: bytes) -> tuple[int, bytes] | None:
    """Svar på get_filters: ``00 0f 00 08 04 02 <indeks> <10 byte>``. Returnerer (indeks, rådata)."""
    if len(frame) >= 7 and frame[2] == 0 and frame[3:6] == b"\x08\x04\x02":
        return frame[6], bytes(frame[7:])
    return None

GET_ROOM_CURVE = control_frame(0x07, 0x09)     # 00 03 10 07 09 -> 07 0a, 140 byte data (se decode_room_curve)
GET_VERSIONS = control_frame(0x02, 0x89)       # 00 03 8b 02 89 -> JSON {"mcu","ls9","cast"}

# Skriving (lukket sett)
TOGGLE_POWER = control_frame(0x02, 0x0F, b"\x00")  # 00 04 11 02 0f 00 - veksler!


def set_manual_eq(on: bool) -> bytes:
    return control_frame(0x03, 0x04, bytes([1 if on else 0]))


def set_room_correction(on: bool) -> bytes:
    return control_frame(0x07, 0x01, bytes([1 if on else 0]))


def select_source(ix: int) -> bytes:
    if not 0 <= ix <= 15:
        raise ValueError(f"ugyldig kildeindeks {ix}")
    return control_frame(0x02, 0x03, bytes([ix]))


def select_preset(n: int) -> bytes:
    """Velg EQ-preset 1-3.

    Siste byte MÅ være 02 (velg). 00/01 lagrer redigeringskurven over
    presetet og sletter det eieren har laget, så det kan ikke bygges her.
    """
    if n not in (1, 2, 3):
        raise ValueError(f"preset må være 1, 2 eller 3, ikke {n}")
    return control_frame(0x08, 0x02, bytes([0x01, n, 0x01, 0x02]))


class FrameReader:
    """Deler en byte-strøm i rammer etter lengdefeltet."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._buf += data
        while len(self._buf) >= 2:
            length = int.from_bytes(self._buf[0:2], "big")
            if length < 2 or length > MAX_FRAME:
                del self._buf[0]  # resynkroniser
                continue
            if len(self._buf) < length + 2:
                return
            frame = bytes(self._buf[: length + 2])
            del self._buf[: length + 2]
            yield frame


def decode_control(frame: bytes) -> dict | None:
    """Gjør en enhetsramme om til en tilstandsendring. Returnerer None for alt annet."""
    if len(frame) < 5 or frame[2] != 0:
        return None  # for kort, eller en forespørsel fra en annen klient
    key = (frame[3], frame[4])
    data = frame[5:]
    if key == (0x02, 0x0E) and data:
        return {"power": data[0] == 1}
    if key == (0x02, 0x21) and len(data) >= 2:
        # SM450: 7-byte ramme med bare kilde og volum (Stereo Hub har 9 byte)
        patch = {"source_code": data[0], "volume": data[1]}
        if len(data) >= 3:
            patch["mute"] = data[2] == 1
        return patch
    if key == (0x02, 0x02):
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        sources = [
            {"ix": int(s["ix"]), "name": str(s.get("name", s["ix"]))}
            for s in doc.get("src", [])
            if "ix" in s
        ]
        current = next((int(s["ix"]) for s in doc.get("src", []) if s.get("sts") == 1), None)
        return {"sources": sources, "source": current}
    if key == (0x02, 0x89):
        try:
            return {"versions": json.loads(data.decode("utf-8"))}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    if key == (0x03, 0x05) and data:
        return {"manual_eq": data[0] == 1}
    if key == (0x07, 0x03) and data:
        return {"room_correction": data[0] == 1}
    if key == (0x08, 0x05) and len(frame) >= 8:
        return {"preset": frame[7]}
    return None


# ---------------------------------------------------------------- port 7777

LUCI_QUERY = 0x01
LUCI_COMMAND = 0x02

BOX_REGISTER = 0x0003
BOX_TRANSPORT = 0x0028
BOX_PLAY_STATE = 0x0033
BOX_MUTE = 0x003F
BOX_VOLUME = 0x0040

# Eneste bokser denne koden noen gang adresserer på 7777.
LUCI_ALLOWED = {
    (LUCI_COMMAND, BOX_REGISTER),
    (LUCI_COMMAND, BOX_VOLUME),
    (LUCI_COMMAND, BOX_TRANSPORT),
    (LUCI_QUERY, BOX_VOLUME),
    # Demping: fjernkontrollens mute ga statusmeldingene 'MUTE'/'UNMUTE' i boks 0x3F (opptak).
    # At samme boks tar imot kommandoen, er en hypotese: device.set_mute venter på bekreftelse
    # og bruker volum 0 som reserve.
    (LUCI_COMMAND, BOX_MUTE),
    (LUCI_QUERY, BOX_MUTE),
}

TRANSPORT_ACTIONS = {"play": b"PLAY", "pause": b"PAUSE", "stop": b"STOP", "next": b"NEXT", "prev": b"PREV"}


def luci_frame(msg_type: int, box: int, payload: bytes = b"") -> bytes:
    if (msg_type, box) not in LUCI_ALLOWED:
        raise ValueError(f"LUCI-boks 0x{box:04x} type {msg_type} er ikke tillatt")
    return (
        b"\x00\x00"
        + bytes([msg_type])
        + box.to_bytes(2, "little")
        + b"\x00"          # status
        + b"\x00\x00"      # CRC: enheten godtar 0
        + len(payload).to_bytes(2, "little")
        + payload
    )


def luci_register(client_ip: str) -> bytes:
    return luci_frame(LUCI_COMMAND, BOX_REGISTER, client_ip.encode("ascii"))


def luci_set_volume(level: int) -> bytes:
    if not 0 <= level <= 100:
        raise ValueError("volum må være 0-100")
    return luci_frame(LUCI_COMMAND, BOX_VOLUME, str(level).encode("ascii"))


def luci_transport(action: str) -> bytes:
    return luci_frame(LUCI_COMMAND, BOX_TRANSPORT, TRANSPORT_ACTIONS[action])


def luci_set_mute(on: bool) -> bytes:
    return luci_frame(LUCI_COMMAND, BOX_MUTE, b"MUTE" if on else b"UNMUTE")


LUCI_GET_VOLUME = luci_frame(LUCI_QUERY, BOX_VOLUME)
LUCI_GET_MUTE = luci_frame(LUCI_QUERY, BOX_MUTE)


class LuciReader:
    """Deler LUCI-rammer fra enheten (big-endian header på 10 byte)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[tuple[int, int, bytes]]:
        self._buf += data
        while len(self._buf) >= 10:
            length = int.from_bytes(self._buf[8:10], "big")
            if length > 64 * 1024:
                del self._buf[0]
                continue
            if len(self._buf) < 10 + length:
                return
            box = int.from_bytes(self._buf[3:5], "big")
            status = self._buf[5]
            payload = bytes(self._buf[10 : 10 + length])
            del self._buf[: 10 + length]
            yield box, status, payload


PLAY_STATES = {0: "playing", 1: "stopped", 2: "paused", 5: "loading"}


def decode_luci(box: int, payload: bytes) -> dict | None:
    text = payload.decode("utf-8", "replace").strip("\x00 ")
    if box == BOX_VOLUME and text.isdigit():
        return {"volume": int(text)}
    if box == BOX_PLAY_STATE and text.lstrip("-").isdigit():
        return {"play_state": PLAY_STATES.get(int(text), text)}
    if box == BOX_MUTE:
        word = text.upper()
        if word == "MUTE":
            return {"mute": True}
        if word == "UNMUTE":
            return {"mute": False}
    return None

def decode_room_curve(raw: bytes) -> dict | None:
    """Tolker romkurven fra ``07 0a`` (140 byte). Dekodet 8.11.2026 ved å sammenligne med Platin-appens graf.

    Byte 0-1: hode (``00 0a``, ukjent betydning). Byte 2-70: målt kurve (rød i Platin-appen),
    69 punkter. Byte 71: ukjent (``0x12``). Byte 72-139: kurven etter korreksjon (grønn),
    68 punkter. Punkt i ligger på 20 Hz * 2^(i/12) (1/12 oktav), og verdien er (byte - 100) / 10 dB.
    Den grønne kurven treffer grafen innen ±0,3 dB. Den røde har riktig form, men Platin-appen
    viser toppene lavere (trolig glattet eller begrenset i visningen).
    """
    if raw is None or len(raw) != 140:
        return None
    def curve(part: bytes) -> list[dict]:
        return [{"freq": round(20 * 2 ** (i / 12), 1), "db": round((v - 100) / 10, 1)} for i, v in enumerate(part)]
    return {"measured": curve(raw[2:71]), "result": curve(raw[72:140])}


def readback_safe_q(q: float | None, lo: float = 0.1, hi: float = 20.0) -> float | None:
    """Velg en Q (to desimaler) som parse_band leser tilbake som nøyaktig samme tall.

    parse_band regner Q som heltall + hundredeler / 100 uten avrunding, så for eksempel 1,89 kommer
    tilbake som 1.8900000000000001, og skrivingen blir da ikke «bekreftet» (eksakt sammenligning).
    For slike verdier flyttes Q én hundredel (uhørbart) til nærmeste verdi som leses likt.
    """
    if q is None:
        return None
    q = round(float(q), 2)
    for cand in (q, round(q + 0.01, 2), round(q - 0.01, 2), round(q + 0.02, 2), round(q - 0.02, 2)):
        if lo <= cand <= hi:
            hundredths = round(cand * 100)
            if hundredths // 100 + (hundredths % 100) / 100 == cand:
                return cand
    return q
