"""Kurve-EQ: gjør en ønsket kurve (mange punkter) om til høyst tre EQ-bånd i SM450.

SM450 tåler bare tre EQ-bånd. Brukeren tegner i stedet en kurve med mange punkter, og her
finner vi de (høyst) tre filtrene som til sammen ligger nærmest den kurven.

* Målet er en jevn kurve gjennom brukerpunktene: monoton kubisk interpolasjon (PCHIP) i
  log-frekvens. Utenfor første og siste punkt holdes endeverdien.
* Responsen regnes fra de kvantiserte enhetskoeffisientene (filters.coefficients → 8.24-heltall),
  med de avrundede båndverdiene som faktisk skrives (hele Hz, Q med to desimaler, gain med én).
  Kurven som vises er altså det DSP-en faktisk gjør.
* Søket er deterministisk: grådig start (finn største avvik og legg et filter der: shelving mot
  endene, parametrisk ellers) og deretter koordinatsøk over alle parametre innen grensene.
  Færre bånd brukes når det holder. Ingen tilfeldighet, og et fast antall forsøk.

Bare beregning: ingenting her snakker med høyttaleren.
"""

from __future__ import annotations

import math

from . import filters
from . import protocol

F_MIN, F_MAX = 20.0, 20000.0
GRID_N = 160               # punkter i rutenettet, log-fordelt 20 Hz–20 kHz
GAIN_MAX = 12.0            # strengere enn høyttalerens ±15 dB
Q_MIN, Q_MAX = 0.3, 8.0    # strengere enn høyttalerens 0,1–20
MAX_POINTS = 64
FIT_TYPES = (filters.PARAMETRIC, 0x06, 0x05)  # parametrisk, lav shelving, høy shelving
LOW_SHELF, HIGH_SHELF = 0x06, 0x05
# Hjørnefrekvenser for shelving holdes innenfor et fornuftig område.
SHELF_RANGE = {LOW_SHELF: (25.0, 3000.0), HIGH_SHELF: (400.0, 16000.0)}
DONE_DB = 1.0              # under dette avviket (dB) holder det: ingen flere bånd (plassene holdes ledige,
                           # f.eks. til tonekontrollene, i stedet for små motfiltre rundt en enkel topp)
LIMIT_DB = GAIN_MAX        # summen av filtrene får aldri løfte (eller senke) mer enn dette, heller ikke utenfor 20 Hz–20 kHz
GUARD_HZ = (2.0, 5.0, 10.0, 14.0, 17.0, 21000.0, 22500.0, 23900.0)  # sjekkes i tillegg (infralyd og opp mot fs/2)
GUARD_W = 40.0             # straff per dB² over LIMIT_DB i søket
LEVEL_FREE_DB = 0.5        # løftes hele kurven mer enn dette, er det volum: den delen tas bort
Q_SOFT = math.log10(2.5)   # over denne Q-en koster smale filtre litt ekstra i søket
EVAL_BUDGET = 1400         # øvre grense for antall responsberegninger i ett søk (holder tiden nede)


def log_grid(n: int = GRID_N, lo: float = F_MIN, hi: float = F_MAX) -> list[float]:
    r = math.log10(hi / lo)
    return [lo * 10 ** (r * i / (n - 1)) for i in range(n)]


# ------------------------------------------------------------ mål: jevn kurve gjennom punktene

def clean_points(points) -> list[tuple[float, float]]:
    """Godta [{freq, gain}] (eller {freq, db}); sorter, fjern like frekvenser (siste vinner)."""
    if points is None:
        return []
    if not isinstance(points, list) or len(points) > MAX_POINTS:
        raise ValueError(f"points må være en liste med høyst {MAX_POINTS} punkter")
    by_f: dict[float, float] = {}
    for pt in points:
        f = float(pt["freq"])
        g = float(pt["gain"] if "gain" in pt else pt["db"])
        if not (math.isfinite(f) and math.isfinite(g)):
            raise ValueError("ugyldig punkt")
        if not F_MIN <= f <= F_MAX:
            raise ValueError("frekvens må være 20-20000 Hz")
        if not -15 <= g <= 15:
            raise ValueError("gain må være mellom -15 og +15 dB")
        by_f[f] = max(-GAIN_MAX, min(GAIN_MAX, g))
    return sorted(by_f.items())


def interpolate(points: list[tuple[float, float]], freqs: list[float]) -> list[float]:
    """Monoton kubisk interpolasjon (Fritsch–Carlson) i log10(f). Endeverdiene holdes utenfor."""
    if not points:
        return [0.0] * len(freqs)
    if len(points) == 1:
        return [points[0][1]] * len(freqs)
    xs = [math.log10(f) for f, _ in points]
    ys = [g for _, g in points]
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0
        else:  # vektet harmonisk middel (PCHIP): ingen oversving mellom punktene
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    for i in (0, n - 1):  # endene: ingen oversving
        k = 0 if i == 0 else n - 2
        if m[i] * d[k] <= 0:
            m[i] = 0.0
        elif abs(m[i]) > 3 * abs(d[k]):
            m[i] = 3 * d[k]
    out = []
    j = 0
    for f in freqs:
        x = math.log10(f)
        if x <= xs[0]:
            out.append(ys[0])
            continue
        if x >= xs[-1]:
            out.append(ys[-1])
            continue
        while xs[j + 1] < x:
            j += 1
        t = (x - xs[j]) / h[j]
        t2, t3 = t * t, t * t * t
        out.append((2 * t3 - 3 * t2 + 1) * ys[j] + (t3 - 2 * t2 + t) * h[j] * m[j]
                   + (-2 * t3 + 3 * t2) * ys[j + 1] + (t3 - t2) * h[j] * m[j + 1])
    return out


# ------------------------------------------------------------ bånd og respons

class _Band:
    __slots__ = ("type", "lf", "lq", "g")

    def __init__(self, type_code: int, freq: float, q: float | None, gain: float) -> None:
        self.type = type_code
        self.lf = math.log10(freq)
        self.lq = math.log10(q) if q else 0.0
        self.g = gain

    def copy(self) -> "_Band":
        b = _Band(self.type, 1000.0, None, self.g)
        b.lf, b.lq = self.lf, self.lq
        return b

    def clamp(self) -> None:
        lo, hi = SHELF_RANGE.get(self.type, (F_MIN, F_MAX))
        self.lf = min(math.log10(hi), max(math.log10(lo), self.lf))
        self.lq = min(math.log10(Q_MAX), max(math.log10(Q_MIN), self.lq))
        self.g = min(GAIN_MAX, max(-GAIN_MAX, self.g))

    def params(self) -> tuple[int, int, float | None, float]:
        """Verdiene slik de skrives til høyttaleren (avrundet)."""
        freq = int(round(10 ** self.lf))
        q = _readback_safe_q(round(10 ** self.lq, 2)) if self.type in filters.HAS_Q else None
        return self.type, freq, q, round(self.g, 1)


def _readback_safe_q(q: float) -> float:
    """Q som leses tilbake likt fra høyttaleren (se protocol.readback_safe_q)."""
    return protocol.readback_safe_q(q, Q_MIN, Q_MAX)


class _Fitter:
    def __init__(self, target: list[float], freqs: list[float], formulas: dict[int, str]) -> None:
        # Responsen regnes på rutenettet (n punkter) og i tillegg på vaktpunktene utenfor 20 Hz–20 kHz,
        # så summen av filtrene aldri kan løfte mer enn LIMIT_DB der heller (infralyd gir store utslag).
        self.target = target
        self.n = len(freqs)
        self.trig = filters.grid_trig(list(freqs) + list(GUARD_HZ))
        self.formulas = formulas
        self.n_all = self.n + len(GUARD_HZ)
        self.evals = 0
        self._cache: dict[tuple, list[float]] = {}

    def response(self, b: _Band) -> list[float]:
        key = b.params()
        r = self._cache.get(key)
        if r is None:
            t, freq, q, g = key
            self.evals += 1
            r = filters.device_response_db(filters.coefficients(t, self.formulas[t], freq, q, g), trig=self.trig)
            if len(self._cache) > 4000:
                self._cache.clear()
            self._cache[key] = r
        return r

    def total(self, bands: list[_Band]) -> list[float]:
        s = [0.0] * self.n_all
        for b in bands:
            r = self.response(b)
            s = [x + y for x, y in zip(s, r)]
        return s

    def cost_with(self, base: list[float], r: list[float]) -> float:
        """Middel av kvadratavviket pluss litt av det største avviket (så toppene ikke glemmes)."""
        sq = 0.0
        mx = 0.0
        for t, a, b in zip(self.target, base, r):
            e = t - a - b
            e2 = e * e
            sq += e2
            if e2 > mx:
                mx = e2
        over = 0.0
        for a, b in zip(base, r):  # hele summen (også vaktpunktene) holdes innen ±LIMIT_DB
            x = abs(a + b) - LIMIT_DB
            if x > 0:
                over += x * x
        return sq / self.n + 0.15 * mx + GUARD_W * over

    def peak(self, bands: list[_Band]) -> float:
        """Største utslag (dB) av summen, også utenfor 20 Hz–20 kHz."""
        return max((abs(x) for x in self.total(bands)), default=0.0)

    @staticmethod
    def penalty(bands: list[_Band]) -> float:
        """Litt straff for smale filtre (høy Q): brede filtre foretrekkes når de er nesten like gode."""
        pen = 0.0
        for b in bands:
            if b.type in filters.HAS_Q and b.lq > Q_SOFT:
                pen += 0.12 * (b.lq - Q_SOFT) ** 2
        return pen

    def cost(self, bands: list[_Band]) -> float:
        return self.cost_with(self.total(bands), [0.0] * self.n_all) + self.penalty(bands)

    # -------------------------------------------------- koordinatsøk

    def refine(self, bands: list[_Band], which: list[int], budget: int) -> float:
        """Koordinatsøk over frekvens, Q og nivå for båndene i «which». Endrer bands på stedet."""
        names = []
        for i in which:
            names += [(i, "g"), (i, "lf")]
            if bands[i].type in filters.HAS_Q:
                names.append((i, "lq"))
        step = {k: (1.0 if k[1] == "g" else 0.06 if k[1] == "lf" else 0.08) for k in names}
        min_step = {"g": 0.04, "lf": 0.0015, "lq": 0.003}
        max_step = {"g": 4.0, "lf": 0.3, "lq": 0.3}
        stop = self.evals + budget
        best = self.cost(bands)
        while self.evals < stop:
            active = [k for k in names if step[k] >= min_step[k[1]]]
            if not active:
                break
            for k in active:
                i, attr = k
                b = bands[i]
                others = self.total(bands[:i] + bands[i + 1:])
                old = getattr(b, attr)
                moved = False
                for sign in (1.0, -1.0):
                    setattr(b, attr, old + sign * step[k])
                    b.clamp()
                    if getattr(b, attr) == old:
                        continue
                    c = self.cost_with(others, self.response(b)) + self.penalty(bands)
                    if c < best - 1e-9:
                        best, moved = c, True
                        break
                    setattr(b, attr, old)
                if moved:
                    step[k] = min(max_step[attr], step[k] * 1.6)
                else:
                    setattr(b, attr, old)
                    step[k] *= 0.5
                if self.evals >= stop:
                    break
        return best

    # -------------------------------------------------- grådig start

    def candidates(self, resid: list[float], freqs: list[float], types: list[int]) -> list[_Band]:
        n = len(resid)
        i0 = max(range(n), key=lambda i: abs(resid[i]))
        e0 = resid[i0]
        half = abs(e0) / 2
        lo = i0
        while lo > 0 and resid[lo - 1] * e0 > 0 and abs(resid[lo - 1]) >= half:
            lo -= 1
        hi = i0
        while hi < n - 1 and resid[hi + 1] * e0 > 0 and abs(resid[hi + 1]) >= half:
            hi += 1
        out = []
        octaves = max(0.15, math.log2(freqs[hi] / freqs[lo]))
        near_low = math.log2(freqs[lo] / F_MIN) < 1.2
        near_high = math.log2(F_MAX / freqs[hi]) < 1.2
        if filters.PARAMETRIC in types:
            p2 = 2 ** octaves
            q = math.sqrt(p2) / (p2 - 1)
            f = math.sqrt(freqs[lo] * freqs[hi]) if not (near_low or near_high) else freqs[i0]
            out.append(_Band(filters.PARAMETRIC, f, q, e0))
        if LOW_SHELF in types and near_low:
            g = resid[0] if resid[0] * e0 > 0 else e0
            out.append(_Band(LOW_SHELF, max(30.0, freqs[hi]), None, g))
        if HIGH_SHELF in types and near_high:
            g = resid[-1] if resid[-1] * e0 > 0 else e0
            out.append(_Band(HIGH_SHELF, min(12000.0, freqs[lo]), None, g))
        if not out:  # ingen passende type: ta det som finnes
            for t in types:
                if t == LOW_SHELF:
                    out.append(_Band(t, max(30.0, freqs[hi]), None, e0))
                elif t == HIGH_SHELF:
                    out.append(_Band(t, min(12000.0, freqs[lo]), None, e0))
        for b in out:
            b.clamp()
        return out


def _errors(target: list[float], result: list[float]) -> tuple[float, float]:
    errs = [t - r for t, r in zip(target, result)]  # zip stopper ved rutenettet (vaktpunktene telles ikke)
    mx = max((abs(e) for e in errs), default=0.0)
    rms = math.sqrt(sum(e * e for e in errs) / len(errs)) if errs else 0.0
    return mx, rms


def _base_response(base_bands, freqs: list[float], formulas: dict[int, str]) -> tuple[list[float], int]:
    """Respons for bånd som beholdes (låste plasser, høypass o.l.). Gir (respons, antall med ukjent form)."""
    resp = [0.0] * len(freqs)
    unknown = 0
    for b in base_bands or []:
        if b.get("enabled") is False:
            continue
        t = int(b.get("type", b.get("type_code", 0)))
        if t not in formulas or not b.get("freq"):
            unknown += 1
            continue
        try:
            r = filters.band_response_db(t, formulas[t], float(b["freq"]), b.get("q"), b.get("gain"), freqs)
        except (ValueError, ZeroDivisionError, OverflowError, KeyError, TypeError):
            unknown += 1
            continue
        resp = [x + y for x, y in zip(resp, r)]
    return resp, unknown


def fit(points, types=None, max_bands: int = protocol.MAX_BANDS, formulas: dict[int, str] | None = None,
        grid_n: int = GRID_N, base_bands=None) -> dict:
    """Finn høyst max_bands bånd som ligger nærmest kurven gjennom points.

    points: [{freq, gain}]. types: filtertyper som kan skrives (bare 7, 6 og 5 brukes her).
    base_bands: bånd som blir liggende i høyttaleren (for eksempel et høypassfilter). Kurven legges
    oppå dem: target og result tar dem med, så result er det DSP-en faktisk gjør.
    Gir {bands:[{type,freq,q,gain}], target:[{freq,db}], result:[{freq,db}], max_err, rms_err,
    level_offset, peak_db, base_unknown}.

    Sikkerhet: hvert bånd holdes innen ±12 dB, og summen av filtrene holdes innen ±12 dB overalt,
    også under 20 Hz og opp mot fs/2. Løftes hele kurven (alle punktene over 0), er det det samme
    som mer volum; den delen (level_offset) tas bort, så «Bruk» aldri gir et stort nivåhopp.
    """
    formulas = dict(filters.BUILTIN_FORMULAS if formulas is None else formulas)
    allowed = [t for t in (FIT_TYPES if types is None else [int(x) for x in types])
               if t in FIT_TYPES and t in formulas]
    max_bands = max(0, min(int(max_bands), protocol.MAX_BANDS))  # SM450: høyst 3 bånd
    pts = clean_points(points)
    freqs = log_grid(grid_n)
    wanted = interpolate(pts, freqs)
    level = min(wanted) if wanted else 0.0
    level_offset = round(level, 2) if level > LEVEL_FREE_DB else 0.0
    target = [x - level_offset for x in wanted]
    fitter = _Fitter(target, freqs, formulas)
    bands: list[_Band] = []
    if allowed and max_bands:
        best = fitter.cost(bands)
        per_band = EVAL_BUDGET // (max_bands + 1)
        while len(bands) < max_bands:
            result = fitter.total(bands)
            resid = [t - r for t, r in zip(target, result)]
            mx_old = max(abs(e) for e in resid)
            if mx_old < DONE_DB:
                break
            options = []
            for cand in fitter.candidates(resid, freqs, allowed):
                trial = [b.copy() for b in bands] + [cand]
                c = fitter.refine(trial, [len(trial) - 1], per_band // 3)
                options.append((c, trial))
            if not options:
                break
            # shelving mot endene foretrekkes når den er omtrent like god (mer naturlig og bredere)
            c, trial = min(options, key=lambda x: x[0] * (0.9 if x[1][-1].type in SHELF_RANGE else 1.0))
            c = fitter.refine(trial, list(range(len(trial))), per_band)
            mx_new, _ = _errors(target, fitter.total(trial))
            if c > best * 0.97 and mx_new > mx_old - 0.1:
                break  # et bånd til hjelper ikke merkbart
            bands, best = trial, c
        # Siste finpuss av alt sammen, og fjern bånd som nesten ikke gjør noe.
        if bands:
            fitter.refine(bands, list(range(len(bands))), per_band)
        bands = [b for b in bands if abs(b.params()[3]) >= 0.15]
        # Hard grense: summen skal aldri over ±LIMIT_DB (heller ikke i infralyd). Skaler ned om nødvendig.
        for _ in range(12):
            pk = fitter.peak(bands)
            if pk <= LIMIT_DB + 0.2:
                break
            f = (LIMIT_DB - 0.1) / pk
            for b in bands:
                b.g = round(b.g * f, 1) if abs(b.g * f) >= 0.1 else 0.0
        bands = [b for b in bands if abs(b.params()[3]) >= 0.15]
    fit_resp = fitter.total(bands)[:len(freqs)] if bands else [0.0] * len(freqs)
    peak = fitter.peak(bands) if bands else 0.0
    mx, rms = _errors(target, fit_resp)
    base, base_unknown = _base_response(base_bands, freqs, formulas)
    out_bands = []
    for b in sorted(bands, key=lambda x: x.lf):
        t, freq, q, g = b.params()
        out_bands.append({"type": t, "freq": freq, "q": q, "gain": g})
    return {
        "bands": out_bands,
        "target": [{"freq": round(f, 1), "db": round(v + o, 2)} for f, v, o in zip(freqs, target, base)],
        "result": [{"freq": round(f, 1), "db": round(v + o, 2)} for f, v, o in zip(freqs, fit_resp, base)],
        "max_err": round(mx, 2),
        "rms_err": round(rms, 2),
        "level_offset": level_offset,
        "peak_db": round(peak, 2),
        "base_unknown": base_unknown,
    }
