"""QR-koder uten tredjepartspakker (bare standardbiblioteket).

Brukes til å vise adressen iPhone skal åpne som en QR-kode på PC-en. Lager koder i byte-modus
(UTF-8) med feilretting M, versjon 1–10 (velges automatisk, minste som får plass), og velger
den av de åtte maskene som gir lavest straffepoeng etter ISO/IEC 18004. Utdata er en matrise
(liste av rader med True = mørk modul) og en SVG-tekst.

Kapasitet med M: versjon 1 tar 14 byte, versjon 10 tar 213 byte.
"""

from __future__ import annotations

# Feilretting M per versjon: (EC-kodeord per blokk, [(antall blokker, datakodeord per blokk), ...])
_M_BLOCKS = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
MAX_VERSION = 10
EC_M = 0b00  # formatbitene for feilretting M (L = 01, M = 00, Q = 11, H = 10)

# ------------------------------------------------------------------ Reed–Solomon i GF(256)

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """Generatorpolynomet (x - α^0)(x - α^1)…(x - α^(n-1)), høyeste grad først, uten ledende 1."""
    g = [1]
    for i in range(degree):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, _EXP[i])
        g = nxt
    return g[1:]


def rs_ecc(data: list[int], n: int) -> list[int]:
    """Feilrettingskodeordene (n stk.) for en datablokk."""
    gen = _generator(n)
    rem = [0] * n
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i, c in enumerate(gen):
            rem[i] ^= _mul(c, factor)
    return rem


# ------------------------------------------------------------------ data

def capacity(version: int) -> int:
    """Antall byte med tekst som får plass i en versjon (byte-modus, M)."""
    _, groups = _M_BLOCKS[version]
    bits = sum(n * k for n, k in groups) * 8
    return (bits - 4 - (8 if version < 10 else 16)) // 8


def _codewords(data: bytes, version: int) -> list[int]:
    ec_len, groups = _M_BLOCKS[version]
    total = sum(n * k for n, k in groups)
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> (length - 1 - i)) & 1 for i in range(length))

    put(0b0100, 4)  # byte-modus
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, total * 8 - len(bits)))  # avslutning
    put(0, (-len(bits)) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < total:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks, pos = [], 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(words[pos:pos + k])
            pos += k
    eccs = [rs_ecc(b, ec_len) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_len):
        out.extend(e[i] for e in eccs)
    return out


# ------------------------------------------------------------------ matrise

def format_bits(mask: int, ec: int = EC_M) -> int:
    """15 bit formatinformasjon (feilretting + maske) med BCH(15,5) og XOR-maske 0x5412."""
    data = (ec << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def version_bits(version: int) -> int:
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


class _Grid:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 17 + 4 * version
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]  # funksjonsmoduler (ikke data)

    def set(self, r: int, c: int, dark: bool) -> None:
        self.m[r][c] = dark
        self.fn[r][c] = True

    def function_patterns(self) -> None:
        n = self.size
        for i in range(n):  # tidsmønstre
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for r0, c0 in ((3, 3), (3, n - 4), (n - 4, 3)):  # søkemønstre med skillelinje
            for dr in range(-4, 5):
                for dc in range(-4, 5):
                    r, c = r0 + dr, c0 + dc
                    if 0 <= r < n and 0 <= c < n:
                        d = max(abs(dr), abs(dc))
                        self.set(r, c, d not in (2, 4))
        pos = _ALIGN[self.version]
        for i, r0 in enumerate(pos):
            for j, c0 in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue  # overlapper søkemønstrene
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.set(r0 + dr, c0 + dc, max(abs(dr), abs(dc)) != 1)
        self.format(0)  # reserver plassen; skrives på nytt med riktig maske
        if self.version >= 7:
            bits = version_bits(self.version)
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set(b, a, dark)
                self.set(a, b, dark)

    def format(self, mask: int) -> None:
        n = self.size
        bits = format_bits(mask)
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        for i in range(6):
            self.set(i, 8, bit[i])
        self.set(7, 8, bit[6])
        self.set(8, 8, bit[7])
        self.set(8, 7, bit[8])
        for i in range(9, 15):
            self.set(8, 14 - i, bit[i])
        for i in range(8):
            self.set(8, n - 1 - i, bit[i])
        for i in range(8, 15):
            self.set(n - 15 + i, 8, bit[i])
        self.set(n - 8, 8, True)  # alltid mørk modul

    def place(self, words: list[int]) -> None:
        n = self.size
        i, total = 0, len(words) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(n):
                r = n - 1 - vert if upward else vert
                for j in range(2):
                    c = right - j
                    if not self.fn[r][c] and i < total:
                        self.m[r][c] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        f = MASKS[mask]
        for r in range(self.size):
            for c in range(self.size):
                if not self.fn[r][c] and f(r, c):
                    self.m[r][c] = not self.m[r][c]


def penalty(m: list[list[bool]]) -> int:
    """Straffepoeng etter ISO/IEC 18004 (regel 1–4). Lavest er best."""
    n = len(m)
    score = 0
    lines = [row for row in m] + [[m[r][c] for r in range(n)] for c in range(n)]
    finder_a = [True, False, True, True, True, False, True, False, False, False, False]
    finder_b = finder_a[::-1]
    for line in lines:
        run, prev = 0, None
        for v in line:  # regel 1: fem eller flere like på rad
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run, prev = 1, v
        if run >= 5:
            score += 3 + run - 5
        padded = [False] * 4 + list(line) + [False] * 4  # utenfor koden regnes som lyst
        for i in range(len(padded) - 10):  # regel 3: søkemønster-lignende 1:1:3:1:1 med lyst felt
            window = padded[i:i + 11]
            if window == finder_a or window == finder_b:
                score += 40
    for r in range(n - 1):  # regel 2: 2x2 blokker i samme farge
        for c in range(n - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(map(sum, m))  # regel 4: andel mørke moduler
    total = n * n
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + 10 * max(0, k)


def encode(text: str | bytes, version: int | None = None, mask: int | None = None) -> list[list[bool]]:
    """Lag QR-matrisen (rader, True = mørk). Uten ``version``: minste versjon 1–10 som får plass.
    Uten ``mask``: masken med lavest straffepoeng. ValueError hvis teksten er for lang."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    if version is None:
        version = next((v for v in range(1, MAX_VERSION + 1) if len(data) <= capacity(v)), None)
        if version is None:
            raise ValueError(f"teksten er for lang for en QR-kode (høyst {capacity(MAX_VERSION)} byte)")
    elif not 1 <= version <= MAX_VERSION or len(data) > capacity(version):
        raise ValueError("teksten får ikke plass i den versjonen")
    if mask is not None and mask not in range(8):
        raise ValueError("maske må være 0-7")
    base = _Grid(version)
    base.function_patterns()
    base.place(_codewords(data, version))
    best = None
    for k in ([mask] if mask is not None else range(8)):
        g = _Grid(version)
        g.m = [row[:] for row in base.m]
        g.fn = base.fn
        g.apply_mask(k)
        g.format(k)
        if mask is not None:
            return g.m
        score = penalty(g.m)
        if best is None or score < best[0]:
            best = (score, g.m)
    return best[1]


def svg(matrix: list[list[bool]], border: int = 4, dark: str = "#000", light: str = "#fff") -> str:
    """SVG med hvit bakgrunn og stillesone på ``border`` moduler. Skalerer fritt (viewBox)."""
    n = len(matrix)
    size = n + 2 * border
    parts = []
    for r, row in enumerate(matrix):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                parts.append(f"M{start + border},{r + border}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR">'
            f'<rect width="{size}" height="{size}" fill="{light}"/>'
            f'<path fill="{dark}" d="{"".join(parts)}"/></svg>')
