"""Lydprofiler: innebygde forslag og brukerens egne, lagret lokalt i appen (config.json).

En profil beskriver EQ-båndene (og valgfritt romkorreksjon og EQ av/på). Å bruke en
profil skriver båndene til SM450. Profilene selv ligger aldri i høyttaleren.
"""

from __future__ import annotations

import copy
import secrets
import time

from . import protocol as p
from .config import Config
from .filters import PARAMETRIC

# Ferdige profiler i tone-oppsettet, de samme båndene som tonehjulene bruker (index.html, TONES):
# Bass = lav shelving @ 200 Hz, Mellomtone = parametrisk @ 1 kHz Q 0,5, Diskant = høy shelving @ 3,5 kHz.
# Tonehjulene viser da profilens verdier. Bånd med 0 dB er utelatt. ``manual_eq: True`` slår på
# Egen EQ, ellers høres ikke profilen (unntatt Nøytral, som ikke rører bryteren). Navn og beskrivelser
# er de samme som før (index.html oversetter dem etter id og tekst, PROFILE_NO/PROFILE_EN).
LOW_SHELF, HIGH_SHELF = 0x06, 0x05
TONE_BASS = {"type": LOW_SHELF, "freq": 200, "q": None}
TONE_MID = {"type": PARAMETRIC, "freq": 1000, "q": 0.5}
TONE_TREBLE = {"type": HIGH_SHELF, "freq": 3500, "q": None}


def tone_bands(bass: float = 0.0, mid: float = 0.0, treble: float = 0.0) -> list[dict]:
    """Bånd for tonehjulene (bass, mellomtone, diskant i dB). Bånd med 0 dB tas ikke med."""
    return [{**spec, "gain": float(g)} for spec, g in ((TONE_BASS, bass), (TONE_MID, mid), (TONE_TREBLE, treble))
            if g]


BUILTIN = [
    {"id": "noytral", "name": "Nøytral", "desc": "Ingen egen EQ. Bare romkorreksjon.", "builtin": True, "bands": []},
    {"id": "varm", "name": "Varm", "desc": "Litt fyldigere bunn, mykere topp", "builtin": True,
     "bands": tone_bands(bass=2.5, treble=-1.5), "manual_eq": True},
    {"id": "musikk", "name": "Musikk", "desc": "Svak loudness-kurve", "builtin": True,
     "bands": tone_bands(bass=3.0, treble=1.5), "manual_eq": True},
    {"id": "tv", "name": "TV / dialog", "desc": "Tydeligere stemmer", "builtin": True,
     "bands": tone_bands(bass=-2.0, mid=2.5), "manual_eq": True},
    {"id": "kveld", "name": "Kveld", "desc": "Mindre dyp bass, mindre skarp topp", "builtin": True,
     "bands": tone_bands(bass=-4.0, treble=-2.0), "manual_eq": True},
]


CURVE_MAX_POINTS = 64


def validate_curve(curve) -> list[dict] | None:
    """Valgfri kurve fra Kurve-EQ: [{freq, gain}]. Lagres bare i appen, aldri i høyttaleren."""
    if curve is None:
        return None
    if not isinstance(curve, list) or len(curve) > CURVE_MAX_POINTS:
        raise ValueError(f"curve må være en liste med høyst {CURVE_MAX_POINTS} punkter")
    out = []
    for pt in curve:
        freq = round(float(pt["freq"]), 1)
        gain = round(float(pt["gain"]), 2)
        if not 20 <= freq <= 20000:
            raise ValueError("frekvens må være 20-20000 Hz")
        if not -15 <= gain <= 15:
            raise ValueError("gain må være mellom -15 og +15 dB")
        out.append({"freq": freq, "gain": gain})
    return out


def validate_bands(bands) -> list[dict]:
    if not isinstance(bands, list) or len(bands) > p.MAX_BANDS:
        raise ValueError(f"bands må være en liste med høyst {p.MAX_BANDS} bånd (grensen i SM450)")
    out = []
    for b in bands:
        if not isinstance(b, dict):
            raise ValueError("hvert bånd må være et objekt")
        t = int(b.get("type", PARAMETRIC))
        freq = int(b["freq"])
        q = None if b.get("q") is None else p.readback_safe_q(float(b["q"]))  # leses tilbake likt
        gain = None if b.get("gain") is None else round(float(b["gain"]), 2)
        p.encode_band_params(t, freq, q, gain)  # samme grenser som høyttaleren
        out.append({"type": t, "freq": freq, "q": q, "gain": gain, "enabled": bool(b.get("enabled", True))})
    return out


BUILTIN_IDS = frozenset(x["id"] for x in BUILTIN)
OVERRIDE_KEYS = ("name", "desc", "bands", "room_correction", "manual_eq", "curve", "saved")


class ProfileStore:
    """Innebygde profiler kan endres: endringen lagres som en overstyring med samme id i
    config["profile_overrides"], og «Tilbakestill til standard» (reset) fjerner den igjen."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def _overrides(self) -> dict:
        ov = self.config["profile_overrides"]
        return dict(ov) if isinstance(ov, dict) else {}

    def overrides(self) -> dict:
        """Overstyringene av innebygde profiler (for sikkerhetskopi)."""
        return copy.deepcopy({k: v for k, v in self._overrides().items() if k in BUILTIN_IDS})

    def _builtin(self, base: dict, ov: dict | None) -> dict:
        prof = copy.deepcopy(base)
        if ov:
            prof.update({k: copy.deepcopy(ov[k]) for k in OVERRIDE_KEYS if k in ov})
            if "curve" not in ov:
                prof.pop("curve", None)
        prof["id"], prof["builtin"], prof["modified"] = base["id"], True, bool(ov)
        return prof

    def all(self) -> list[dict]:
        ov = self._overrides()
        return [self._builtin(b, ov.get(b["id"])) for b in BUILTIN] + copy.deepcopy(self.config["profiles"] or [])

    def get(self, pid: str) -> dict | None:
        return next((x for x in self.all() if x["id"] == pid), None)

    def save(self, name: str, bands, pid: str | None = None, room_correction: bool | None = None,
             manual_eq: bool | None = None, desc: str | None = None, curve=None) -> dict:
        name = str(name).strip()[:40]
        if not name:
            raise ValueError("profilen må ha et navn")
        if desc is None:  # ikke oppgitt: behold beskrivelsen profilen har
            desc = ((self.get(pid) if pid else None) or {}).get("desc") or ""
        prof = {"id": pid or f"p{int(time.time())}{secrets.token_hex(2)}", "name": name, "desc": str(desc)[:80],
                "builtin": False, "bands": validate_bands(bands), "room_correction": room_correction,
                "manual_eq": manual_eq, "saved": time.strftime("%Y-%m-%d %H:%M")}
        curve = validate_curve(curve)
        if curve is not None:
            prof["curve"] = curve  # punktene i Kurve-EQ, så kurven kan redigeres videre
        if pid in BUILTIN_IDS:  # innebygd: lagres som overstyring med samme id
            ov = self._overrides()
            ov[pid] = {k: prof[k] for k in OVERRIDE_KEYS if k in prof}
            self.config["profile_overrides"] = ov
            return self.get(pid)
        items = list(self.config["profiles"] or [])
        idx = next((i for i, x in enumerate(items) if x["id"] == prof["id"]), None)
        if idx is None:
            items.append(prof)
        else:
            items[idx] = prof
        self.config["profiles"] = items
        return prof

    def rename(self, pid: str, name: str) -> dict:
        prof = self.get(pid)
        if not prof:
            raise ValueError("fant ikke profilen")
        return self.save(name, prof["bands"], pid, prof.get("room_correction"), prof.get("manual_eq"), prof.get("desc", ""),
                         prof.get("curve"))

    def reset(self, pid: str) -> dict:
        """«Tilbakestill til standard»: fjerner overstyringen av en innebygd profil."""
        if pid not in BUILTIN_IDS:
            raise ValueError("bare innebygde profiler kan tilbakestilles")
        ov = self._overrides()
        if ov.pop(pid, None) is not None:
            self.config["profile_overrides"] = ov
        return self.get(pid)

    def duplicate(self, pid: str) -> dict:
        prof = self.get(pid)
        if not prof:
            raise ValueError("fant ikke profilen")
        return self.save(f"{prof['name']} (kopi)", prof["bands"], None, prof.get("room_correction"),
                         prof.get("manual_eq"), prof.get("desc", ""), prof.get("curve"))

    def delete(self, pid: str) -> None:
        if pid in BUILTIN_IDS:
            raise ValueError("innebygde profiler kan ikke slettes. Bruk «Tilbakestill til standard»")
        items = [x for x in (self.config["profiles"] or []) if x["id"] != pid]
        if len(items) == len(self.config["profiles"] or []):
            raise ValueError("fant ikke en egen profil med den id-en")
        self.config["profiles"] = items

    def import_many(self, profiles: list, overrides: dict | None = None) -> int:
        """Gjenoppretter egne profiler og overstyringer av innebygde (fra sikkerhetskopi)."""
        n = 0
        for prof in profiles or []:
            if prof.get("builtin"):
                if prof.get("modified") and prof.get("id") in BUILTIN_IDS and not (overrides or {}).get(prof["id"]):
                    overrides = {**(overrides or {}), prof["id"]: prof}
                continue
            self.save(prof.get("name", "Importert"), prof.get("bands", []), prof.get("id"),
                      prof.get("room_correction"), prof.get("manual_eq"), prof.get("desc", ""), prof.get("curve"))
            n += 1
        for pid, ov in (overrides or {}).items():
            if pid not in BUILTIN_IDS or not isinstance(ov, dict):
                continue
            base = next(b for b in BUILTIN if b["id"] == pid)
            self.save(ov.get("name", base["name"]), ov.get("bands", []), pid, ov.get("room_correction"),
                      ov.get("manual_eq"), ov.get("desc", base["desc"]), ov.get("curve"))
            n += 1
        return n
