"""QR-generatoren (lemus_remote/qr.py) og GET /api/qr.svg."""

import asyncio
import unittest
from urllib.parse import quote

from lemus_remote import qr
from lemus_remote.config import Config
from lemus_remote.device import Speaker
from lemus_remote.web import PUBLIC, QR_MAX_TEXT, WebServer

try:  # bare for sammenligning i utviklingsmiljøet; appen bruker aldri qrcode
    import qrcode
    from qrcode.util import MODE_8BIT_BYTE, QRData
except ImportError:  # pragma: no cover
    qrcode = None

# Fasit: «SUMEL», versjon 1, feilretting M, maske 2 (laget én gang med qrcode-biblioteket)
SUMEL_V1_M2 = [
    "#######...#...#######",
    "#.....#...###.#.....#",
    "#.###.#.#.#.#.#.###.#",
    "#.###.#.#..#..#.###.#",
    "#.###.#.##..#.#.###.#",
    "#.....#.####..#.....#",
    "#######.#.#.#.#######",
    "........###..........",
    "#.#####..###..#####..",
    "####.#...#.####..###.",
    "....####.##.#.##.###.",
    ".#.#...###.####..####",
    "##...#####..#..#..#..",
    "........#...#..#.#.#.",
    "#######...##.#..####.",
    "#.....#.##.....#####.",
    "#.###.#.####.#..#.##.",
    "#.###.#.#.######.##..",
    "#.###.#.##..#.##.##..",
    "#.....#..#######.##..",
    "#######.###.#...#.##.",
]

TEXTS = ["SUMEL", "http://192.168.0.9:8080/?key=123456", "æøå ÆØÅ – fjernkontroll", "x" * 100,
         "https://example.com/" + "a" * 150, "y" * 213]


def read_format(m: list[list[bool]]) -> tuple[int, int]:
    """Les begge kopiene av formatinformasjonen (15 bit) tilbake fra matrisen."""
    n = len(m)
    first = [m[i][8] for i in range(6)] + [m[7][8], m[8][8], m[8][7]] + [m[8][14 - i] for i in range(9, 15)]
    second = [m[8][n - 1 - i] for i in range(8)] + [m[n - 15 + i][8] for i in range(8, 15)]
    as_int = lambda bits: sum(1 << i for i, b in enumerate(bits) if b)
    return as_int(first), as_int(second)


def bch_ok(word: int) -> bool:
    rem = word
    for i in range(14, 9, -1):
        if rem >> i & 1:
            rem ^= 0x537 << (i - 10)
    return rem == 0


class QrOwn(unittest.TestCase):
    def test_reed_solomon_known_answer(self):
        # Kjent eksempel (1-M, «HELLO WORLD» i alfanumerisk modus): datakodeord og fasit for EC-kodeordene
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr.rs_ecc(data, 10), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_known_matrix(self):
        m = qr.encode("SUMEL", mask=2)
        self.assertEqual(["".join("#" if x else "." for x in row) for row in m], SUMEL_V1_M2)

    def test_version_and_size(self):
        for text, version in (("SUMEL", 1), ("a" * 14, 1), ("a" * 15, 2), ("a" * 213, 10)):
            self.assertEqual(len(qr.encode(text)), 17 + 4 * version, text[:10])
        self.assertEqual([qr.capacity(v) for v in (1, 2, 5, 10)], [14, 26, 84, 213])
        with self.assertRaises(ValueError):
            qr.encode("a" * 214)

    def test_finder_patterns_and_timing(self):
        m = qr.encode("http://192.168.0.9:8080/?key=123456")
        n = len(m)
        finder = ["#######", "#.....#", "#.###.#", "#.###.#", "#.###.#", "#.....#", "#######"]
        for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
            got = ["".join("#" if m[r0 + r][c0 + c] else "." for c in range(7)) for r in range(7)]
            self.assertEqual(got, finder, (r0, c0))
        self.assertEqual([m[6][i] for i in range(8, n - 8)], [i % 2 == 0 for i in range(8, n - 8)])
        self.assertTrue(m[n - 8][8])  # alltid mørk modul

    def test_format_info_decodes_itself(self):
        for text in TEXTS:
            for mask in range(8):
                m = qr.encode(text, mask=mask)
                a, b = read_format(m)
                self.assertEqual(a, b)
                word = a ^ 0x5412
                self.assertTrue(bch_ok(word), (text[:10], mask))
                self.assertEqual(word >> 13, 0b00)  # feilretting M
                self.assertEqual((word >> 10) & 7, mask)

    def test_auto_mask_has_lowest_penalty(self):
        text = "http://192.168.0.9:8080/?key=123456"
        scores = [qr.penalty(qr.encode(text, mask=k)) for k in range(8)]
        self.assertEqual(qr.penalty(qr.encode(text)), min(scores))

    def test_svg(self):
        svg = qr.svg(qr.encode("SUMEL"))
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
        self.assertIn('viewBox="0 0 29 29"', svg)  # 21 + 2 * 4 stillesone


# 20 tekster for sammenligningen med qrcode-biblioteket (alle versjoner 1-10 og typiske adresser med ?key=)
LIB_TEXTS = TEXTS + ["a", "Hello, World!", "http://10.0.0.25:8080/?key=987654", "http://lemus-pc.local:8080/?key=424242",
                     "https://192.168.1.100:8443/?key=000111&lang=en", "SUMEL – fjernkontroll for Platin SM450",
                     "WIFI:S:Stua;T:WPA;P:hemmelig;;", "\U0001F642 emoji", "a" * 26, "a" * 27, "z" * 84, "z" * 85,
                     "0123456789" * 15, "mixed CASE 123 !@#$%^&*()"]


@unittest.skipIf(qrcode is None, "qrcode er ikke installert (pip install qrcode, bare for denne testen)")
class QrAgainstLibrary(unittest.TestCase):
    def lib(self, text, version, mask):
        q = qrcode.QRCode(version=version, error_correction=qrcode.constants.ERROR_CORRECT_M, border=0,
                          mask_pattern=mask)
        q.add_data(QRData(text.encode("utf-8"), mode=MODE_8BIT_BYTE))
        q.make(fit=False)
        return [[bool(x) for x in row] for row in q.modules]

    def test_same_version_as_library(self):
        self.assertGreaterEqual(len(LIB_TEXTS), 20)
        for text in LIB_TEXTS:
            q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
            q.add_data(QRData(text.encode("utf-8"), mode=MODE_8BIT_BYTE))
            q.make(fit=True)
            self.assertEqual((len(qr.encode(text)) - 17) // 4, q.version, text[:12])

    def test_same_matrix_for_every_mask(self):
        # Maskevalget kan avvike fra biblioteket (det regner straffepoeng litt annerledes); alle åtte
        # masker gir gyldige koder, så vi sammenligner matrisen for hver maske.
        for text in LIB_TEXTS:
            version = (len(qr.encode(text)) - 17) // 4
            for mask in range(8):
                self.assertEqual(qr.encode(text, mask=mask), self.lib(text, version, mask), (text[:12], mask))


class QrApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path
        self.cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
        self.sp = Speaker("127.0.0.1", control_port=1, luci_port=1, use_luci=False)  # ikke startet
        self.web = WebServer(self.sp, "127.0.0.1", 0, config=self.cfg)
        await self.web.start()

    async def asyncTearDown(self):
        await self.web.stop()

    async def get(self, path):
        r, w = await asyncio.open_connection("127.0.0.1", self.web.bound_port)
        w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        raw = await r.read()
        w.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), head.decode(), payload

    async def test_qr_svg(self):
        url = f"http://192.168.0.9:8080/?key={self.cfg['access_code']}"
        status, head, payload = await self.get("/api/qr.svg?text=" + quote(url, safe=""))
        self.assertEqual(status, 200)
        self.assertIn("Content-Type: image/svg+xml", head)
        self.assertIn("Cache-Control: no-store", head)  # lenken kan inneholde tilgangskoden
        self.assertTrue(payload.startswith(b"<svg"))
        self.assertEqual((await self.get("/api/qr.svg"))[0], 400)
        self.assertEqual((await self.get("/api/qr.svg?text=" + "a" * 513))[0], 400)
        self.assertEqual((await self.get("/api/qr.svg?text=" + "a" * 300))[0], 400)  # får ikke plass i v10
        # Grensen er det versjon 10-M rommer (213 byte), regnet i UTF-8.
        self.assertEqual(QR_MAX_TEXT, qr.capacity(qr.MAX_VERSION))
        self.assertEqual((await self.get("/api/qr.svg?text=" + "a" * QR_MAX_TEXT))[0], 200)
        status, _, payload = await self.get("/api/qr.svg?text=" + "a" * (QR_MAX_TEXT + 1))
        self.assertEqual(status, 400)
        self.assertIn("byte".encode(), payload)
        self.assertEqual((await self.get("/api/qr.svg?text=" + quote("ø" * 107)))[0], 400)  # 214 byte

    async def test_qr_needs_access(self):
        self.assertFalse(self.web._authorized("192.168.0.50", {}))
        self.assertNotIn("/api/qr.svg", PUBLIC)  # bare for autoriserte (lenken har tilgangskoden)


if __name__ == "__main__":
    unittest.main()
