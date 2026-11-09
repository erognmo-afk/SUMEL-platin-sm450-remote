"""Filterkoeffisienter for SM450 og «læring» av nye filtertyper.

SM450 tar imot hvert EQ-bånd som fem biquad-koeffisienter i 8.24 fast komma,
i rekkefølgen [b2, b1, b0, -a2, -a1] normalisert med a0, etterfulgt av
båndverdiene (protocol.write_band). For Parametric EQ er formelen bekreftet
bit for bit mot opptak av Platin-appen (RBJ Audio EQ Cookbook, peaking, 48 kHz).

For de andre typene vet vi ikke sikkert hvilken variant Platin bruker. I stedet
for å gjette lærer programmet det: når Platin-appen lagrer et bånd og vi ser
skrivekommandoen i 7777-tunnelen, regner vi ut alle kandidatformlene og
godtar en formel bare hvis alle fem koeffisientene treffer (±1 LSB). Først da
kan typen skrives fra Lemus Remote.
"""

from __future__ import annotations

import math
from collections.abc import Callable

FS = 48000
SCALE = 2 ** 24
TOLERANCE = 1  # LSB. To av seks Parametric-opptak avvek med 1 LSB (uhørbart).

TYPE_NAMES = {
    0x01: "6 dB høypass",
    0x02: "12 dB høypass",
    0x03: "6 dB lavpass",
    0x04: "12 dB lavpass",
    0x05: "Høy shelving",
    0x06: "Lav shelving",
    0x07: "Parametrisk",
}
PARAMETRIC = 0x07
HAS_GAIN = {0x05, 0x06, 0x07}
HAS_Q = {0x02, 0x04, 0x07}  # shelving bruker fast helning S = 1 (Q lagres som 1.00, brukes ikke)
SHELVING = {0x05, 0x06}

Coeffs = list[float]  # [b0, b1, b2, a0, a1, a2] før normalisering


def _rbj(kind: str, f: float, q: float, gain: float, fs: int = FS) -> Coeffs:
    a = 10 ** (gain / 40)
    w0 = 2 * math.pi * f / fs
    cs, sn = math.cos(w0), math.sin(w0)
    al = sn / (2 * q)
    if kind == "peak":
        return [1 + al * a, -2 * cs, 1 - al * a, 1 + al / a, -2 * cs, 1 - al / a]
    if kind == "lowshelf":
        s = 2 * math.sqrt(a) * al
        return [a * ((a + 1) - (a - 1) * cs + s), 2 * a * ((a - 1) - (a + 1) * cs), a * ((a + 1) - (a - 1) * cs - s),
                (a + 1) + (a - 1) * cs + s, -2 * ((a - 1) + (a + 1) * cs), (a + 1) + (a - 1) * cs - s]
    if kind == "highshelf":
        s = 2 * math.sqrt(a) * al
        return [a * ((a + 1) + (a - 1) * cs + s), -2 * a * ((a - 1) + (a + 1) * cs), a * ((a + 1) + (a - 1) * cs - s),
                (a + 1) - (a - 1) * cs + s, 2 * ((a - 1) - (a + 1) * cs), (a + 1) - (a - 1) * cs - s]
    if kind == "hp2":
        return [(1 + cs) / 2, -(1 + cs), (1 + cs) / 2, 1 + al, -2 * cs, 1 - al]
    if kind == "lp2":
        return [(1 - cs) / 2, 1 - cs, (1 - cs) / 2, 1 + al, -2 * cs, 1 - al]
    raise ValueError(kind)


def _first_order(kind: str, f: float, variant: str, fs: int = FS) -> Coeffs:
    if variant == "bilinear":
        k = math.tan(math.pi * f / fs)
        if kind == "hp1":
            return [1, -1, 0, 1 + k, k - 1, 0]
        return [k, k, 0, 1 + k, k - 1, 0]
    # én-pol (impulsinvariant)
    x = math.exp(-2 * math.pi * f / fs)
    if kind == "hp1":
        return [(1 + x) / 2, -(1 + x) / 2, 0, 1, -x, 0]
    return [1 - x, 0, 0, 1, -x, 0]


def to_device(c: Coeffs) -> list[int]:
    """[b0,b1,b2,a0,a1,a2] -> [b2, b1, b0, -a2, -a1] / a0 i 8.24."""
    b0, b1, b2, a0, a1, a2 = c
    vals = [b2 / a0, b1 / a0, b0 / a0, -a2 / a0, -a1 / a0]
    return [round(v * SCALE) for v in vals]


UNITY = [0, 0, SCALE, 0, 0]  # b0 = 1, resten 0


def _q(q: float | None, default: float) -> float:
    return q if q else default


# Kandidatformler per type: navn -> funksjon(freq, q, gain) -> enhetskoeffisienter.
CANDIDATES: dict[int, dict[str, Callable[[float, float | None, float | None], list[int]]]] = {
    0x07: {"rbj_peak": lambda f, q, g: to_device(_rbj("peak", f, _q(q, 1.0), g or 0.0))},
    0x06: {
        "rbj_lowshelf_q": lambda f, q, g: to_device(_rbj("lowshelf", f, _q(q, 0.7071), g or 0.0)),
        "rbj_lowshelf_s1": lambda f, q, g: to_device(_rbj("lowshelf", f, 1 / math.sqrt(2), g or 0.0)),
        "rbj_lowshelf_q05": lambda f, q, g: to_device(_rbj("lowshelf", f, 0.5, g or 0.0)),
    },
    0x05: {
        "rbj_highshelf_q": lambda f, q, g: to_device(_rbj("highshelf", f, _q(q, 0.7071), g or 0.0)),
        "rbj_highshelf_s1": lambda f, q, g: to_device(_rbj("highshelf", f, 1 / math.sqrt(2), g or 0.0)),
        "rbj_highshelf_q05": lambda f, q, g: to_device(_rbj("highshelf", f, 0.5, g or 0.0)),
    },
    0x02: {
        "rbj_hp2_q": lambda f, q, g: to_device(_rbj("hp2", f, _q(q, 0.7071), 0.0)),
        "rbj_hp2_butter": lambda f, q, g: to_device(_rbj("hp2", f, 1 / math.sqrt(2), 0.0)),
    },
    0x04: {
        "rbj_lp2_q": lambda f, q, g: to_device(_rbj("lp2", f, _q(q, 0.7071), 0.0)),
        "rbj_lp2_butter": lambda f, q, g: to_device(_rbj("lp2", f, 1 / math.sqrt(2), 0.0)),
    },
    0x01: {
        "hp1_bilinear": lambda f, q, g: to_device(_first_order("hp1", f, "bilinear")),
        "hp1_onepole": lambda f, q, g: to_device(_first_order("hp1", f, "onepole")),
    },
    0x03: {
        "lp1_bilinear": lambda f, q, g: to_device(_first_order("lp1", f, "bilinear")),
        "lp1_onepole": lambda f, q, g: to_device(_first_order("lp1", f, "onepole")),
    },
}

# Bekreftet bit for bit mot opptak av Platin-appen 8.11.2026. Andre typer (12 dB lavpass,
# 6 dB høy-/lavpass) legges til av læreren og lagres i config.
BUILTIN_FORMULAS = {PARAMETRIC: "rbj_peak", 0x06: "rbj_lowshelf_s1", 0x05: "rbj_highshelf_s1", 0x02: "rbj_hp2_q"}


def coefficients(type_code: int, formula: str, freq: float, q: float | None, gain: float | None) -> list[int]:
    fn = CANDIDATES.get(type_code, {}).get(formula)
    if fn is None:
        raise ValueError(f"ukjent formel {formula} for type {type_code}")
    return fn(freq, q, gain)


def close(a: list[int], b: list[int], tol: int = TOLERANCE) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


def match_formula(type_code: int, freq: float, q: float | None, gain: float | None, coeffs: list[int]) -> str | None:
    """Finn kandidatformelen som gir de observerte koeffisientene (±1 LSB)."""
    for name, fn in CANDIDATES.get(type_code, {}).items():
        try:
            if close(fn(freq, q, gain), coeffs):
                return name
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
    return None


# ------------------------------------------------------------ respons (det DSP-en faktisk gjør)

def grid_trig(freqs: list[float], fs: int = FS) -> list[tuple[float, float, float, float]]:
    """cos/sin av w og 2w for hver frekvens, regnet ut én gang (brukes av device_response_db)."""
    out = []
    for f in freqs:
        w = 2 * math.pi * f / fs
        out.append((math.cos(w), math.sin(w), math.cos(2 * w), math.sin(2 * w)))
    return out


def device_response_db(coeffs: list[int], freqs: list[float] | None = None, fs: int = FS,
                       trig: list[tuple[float, float, float, float]] | None = None) -> list[float]:
    """|H(e^jw)| i dB for ett bånd, regnet fra enhetskoeffisientene [b2, b1, b0, -a2, -a1] i 8.24.

    Koeffisientene er heltallene som faktisk sendes til SM450, så avrundingen i DSP-en er med.
    """
    if trig is None:
        trig = grid_trig(freqs or [], fs)
    nb2, nb1, nb0, na2, na1 = (c / SCALE for c in coeffs)
    a1, a2 = -na1, -na2
    out = []
    log10 = math.log10
    for c1, s1, c2, s2 in trig:
        nr = nb0 + nb1 * c1 + nb2 * c2
        ni = nb1 * s1 + nb2 * s2
        dr = 1.0 + a1 * c1 + a2 * c2
        di = a1 * s1 + a2 * s2
        den = dr * dr + di * di
        num = nr * nr + ni * ni
        out.append(10 * log10(num / den) if num > 1e-30 and den > 1e-30 else -300.0)
    return out


def band_response_db(type_code: int, formula: str, freq: float, q: float | None, gain: float | None,
                     freqs: list[float], fs: int = FS) -> list[float]:
    """Respons i dB for ett bånd slik SM450 regner det (kvantiserte koeffisienter)."""
    return device_response_db(coefficients(type_code, formula, freq, q, gain), freqs, fs)
