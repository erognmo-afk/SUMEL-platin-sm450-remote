import unittest

from lemus_remote import protocol as p


def h(s: str) -> bytes:
    return bytes.fromhex(s)


class FrameBytes(unittest.TestCase):
    """Rammene må være byte for byte lik det som er dokumentert og testet."""

    def test_reads(self):
        self.assertEqual(p.GET_POWER, h("00030f020d"))
        self.assertEqual(p.GET_SOURCES, h("0003030201"))
        self.assertEqual(p.GET_MANUAL_EQ, h("0003080305"))
        self.assertEqual(p.GET_ROOM_CORRECTION, h("0003090702"))
        self.assertEqual(p.GET_ACTIVE_PRESET, h("00040d080500"))

    def test_writes(self):
        self.assertEqual(p.set_manual_eq(False), h("000407030400"))  # bekreftet på vår SM450
        self.assertEqual(p.set_manual_eq(True), h("000408030401"))   # bekreftet på vår SM450
        self.assertEqual(p.set_room_correction(True), h("000409070101"))
        self.assertEqual(p.set_room_correction(False), h("000408070100"))
        self.assertEqual(p.TOGGLE_POWER, h("000411020f00"))
        self.assertEqual(p.select_source(1), h("0004060203" "01"))
        self.assertEqual(p.select_preset(1), h("00070f080201010102"))
        self.assertEqual(p.select_preset(2), h("000710080201020102"))
        self.assertEqual(p.select_preset(3), h("000711080201030102"))

    def test_preset_never_stores(self):
        for n in (0, 4, -1):
            with self.assertRaises(ValueError):
                p.select_preset(n)
        for n in (1, 2, 3):
            self.assertEqual(p.select_preset(n)[-1], 0x02)

    def test_luci_closed_set(self):
        self.assertEqual(p.luci_set_volume(30), h("0000024000000000020033 30".replace(" ", "")))
        with self.assertRaises(ValueError):
            p.luci_frame(p.LUCI_COMMAND, 0x00E7)  # serienummer-boksen skal aldri skrives
        with self.assertRaises(ValueError):
            p.luci_set_volume(101)


class DecodeCaptured(unittest.TestCase):
    """Rammer kopiert fra lesetesten mot vår egen høyttaler."""

    def test_power(self):
        self.assertEqual(p.decode_control(h("00040002 0e01".replace(" ", ""))), {"power": True})

    def test_short_volume_frame(self):
        self.assertEqual(p.decode_control(h("0005000221010d")), {"source_code": 1, "volume": 13})

    def test_source_list(self):
        frame = h("00950002027b22737263223a5b7b226978223a312c226e616d65223a2248444d49222c22737473223a317d2c"
                  "7b226978223a322c226e616d65223a224155582f4f5054222c22737473223a307d2c7b226978223a332c22"
                  "6e616d65223a22426c7565746f6f7468222c22737473223a307d2c7b226978223a342c226e616d65223a22"
                  "53747265616d696e67222c22737473223a307d5d7d")
        patch = p.decode_control(frame)
        self.assertEqual(patch["source"], 1)
        self.assertEqual([s["name"] for s in patch["sources"]], ["HDMI", "AUX/OPT", "Bluetooth", "Streaming"])

    def test_eq_room_preset(self):
        self.assertEqual(p.decode_control(h("000400030501")), {"manual_eq": True})
        self.assertEqual(p.decode_control(h("000400070301")), {"room_correction": True})
        self.assertEqual(p.decode_control(h("0009000805020400010000")), {"preset": 0})

    def test_ignores_other_clients_requests_and_unknown(self):
        self.assertIsNone(p.decode_control(p.GET_POWER))
        self.assertIsNone(p.decode_control(h("000600024e010000")))
        self.assertIsNone(p.decode_control(h("000f00080402000907010300010002 0002".replace(" ", ""))))

    def test_reader_splits_and_resyncs(self):
        r = p.FrameReader()
        out = list(r.feed(h("ffff") + h("000400030501") + h("0005000221")))
        self.assertEqual(out, [h("000400030501")])
        self.assertEqual(list(r.feed(h("010d"))), [h("0005000221010d")])



class Filters(unittest.TestCase):
    def test_get_filters_bytes(self):
        self.assertEqual(p.get_filters(0), h("00050c08040000"))  # sendt i fase C av lesetesten
        self.assertEqual(p.get_filters(3), h("00050f08040003"))
        with self.assertRaises(ValueError):
            p.get_filters(4)

    def test_decode_captured_filters(self):
        self.assertEqual(p.decode_filter(h("000f0008040200090701030001000200 02".replace(" ", ""))),
                         (0, h("09070103000100020002")))
        self.assertEqual(p.decode_filter(h("000f00080402010905010600010003 0002".replace(" ", ""))),
                         (1, h("09050106000100030002")))
        self.assertIsNone(p.decode_filter(p.get_filters(0)))  # forespørsel, ikke svar



class BandFormat(unittest.TestCase):
    """Bånd lest fra vår SM450 med eqlog, med verdiene Platin-appen viste."""

    CASES = {
        "09070103000100020002": ("Parametric EQ", "300 Hz", "Q 1.00", "+2.00 dB"),
        "09070103320432020002": ("Parametric EQ", "350 Hz", "Q 4.50", "+2.00 dB"),
        "0907010a000100020002": ("Parametric EQ", "1000 Hz", "Q 1.00", "+2.00 dB"),
        "090701002d0100020002": ("Parametric EQ", "45 Hz", "Q 1.00", "+2.00 dB"),
        "09070178000100020002": ("Parametric EQ", "12000 Hz", "Q 1.00", "+2.00 dB"),
        "09070103000046020002": ("Parametric EQ", "300 Hz", "Q 0.70", "+2.00 dB"),
        "090701030001 00fc0002": ("Parametric EQ", "300 Hz", "Q 1.00", "-4.00 dB"),
        "09050106000100030002": ("High shelving", "600 Hz", "Q 1.00", "+3.00 dB"),
        "09060103000100020002": ("Low shelving", "300 Hz", "Q 1.00", "+2.00 dB"),
        # runde 2: ett bånd om gangen
        "0907010a000100023202": ("Parametric EQ", "1000 Hz", "Q 1.00", "+2.50 dB", "på"),
        "0907010a00010 0fece02": ("Parametric EQ", "1000 Hz", "-2.50 dB"),
        "0907010a0001000 0ce02": ("Parametric EQ", "1000 Hz", "-0.50 dB"),
        "0902010a000046ffff02": ("12dB highpass", "1000 Hz", "Q 0.70"),
        "0901010a00ffffffff02": ("6dB highpass", "1000 Hz"),
        "0904010a000046ffff02": ("2nd order LP", "1000 Hz", "Q 0.70"),
        "0903010a00ffffffff02": ("1st order LP", "1000 Hz", "på"),
        "0903010a00ffffffff01": ("1st order LP", "AV"),
    }

    def test_known_bands(self):
        for raw, parts in self.CASES.items():
            text = p.describe_band(bytes.fromhex(raw.replace(" ", "")))
            for part in parts:
                self.assertIn(part, text, raw)
            if "highpass" in text or "LP" in text:
                self.assertNotIn("gain", text, raw)



class WriteBand(unittest.TestCase):
    """Opptak av Platin-appen via 7777-tunnelen (busmon 8.11.2026)."""

    CAPTURED = {
        2.0: "002303080101001d00e03669fe2048880103a4c2ff1c24d501dfb77807010a000100020002",
        3.0: "002303080101001d00dfd9e5fe1ecdc001057f54ff1aa6c801e1324007010a000100030002",
    }
    CAPTURED_OTHER = {  # (frekvens, Q, gain) -> opptak
        (2000, 2.0, -3.0): "002303080101001d00e0c6ebfe34c31e00faa934ff248fe001cb3ce2070114000200fd0002",
    }

    # Oppretting av tre bånd på rad (indeks 0, 1, 2), gain 0. To av dem avviker med 1 LSB
    # (2^-24, ca. 6e-8) i b1/-a1. Det er uhørbart. Appen regner trolig litt annerledes.
    CAPTURED_CREATE = {
        (0, 100): "002303080101001d00fcabbdfe035f6c01000000ff03544301fca094070101000100000002",
        (1, 1000): "002303080101011d00e0a1e8fe237abc01000000ff1f5e1801dc854407010a000100000002",
        (2, 5000): "002303080101021d00888600fec8971801000000ff777a00013768e8070132000100000002",
    }

    def test_create_bands_index_and_coefficients(self):
        for (ix, f), hexstr in self.CAPTURED_CREATE.items():
            cap = bytes.fromhex(hexstr)
            mine = p.write_parametric_band(ix, f, 1.0, 0.0)
            self.assertEqual(mine[:2] + mine[3:8], cap[:2] + cap[3:8], "header og indeks")
            self.assertEqual(mine[-9:], cap[-9:], "båndverdier")
            for i in range(5):
                m = int.from_bytes(mine[8 + 4 * i:12 + 4 * i], "big", signed=True)
                c = int.from_bytes(cap[8 + 4 * i:12 + 4 * i], "big", signed=True)
                self.assertLessEqual(abs(m - c), 1, (ix, f, i))

    def test_delete_matches_capture(self):
        for ix, hexstr in ((0, "000703080601000100"), (1, "000703080601010100"), (2, "000703080601020100")):
            cap, mine = bytes.fromhex(hexstr), p.delete_band(ix)
            self.assertEqual(mine[:2] + mine[3:], cap[:2] + cap[3:])
        self.assertEqual(p.decode_delete_ack(bytes.fromhex("000700080602010100")), 1)
        self.assertIsNone(p.decode_delete_ack(p.delete_band(1)))

    def test_bit_exact_other_values(self):
        for (f, q, g), hexstr in self.CAPTURED_OTHER.items():
            cap = bytes.fromhex(hexstr)
            mine = p.write_parametric_band(0, f, q, g)
            self.assertEqual(mine[:2] + mine[3:], cap[:2] + cap[3:], (f, q, g))

    def test_bit_exact_except_checksum(self):
        for gain, hexstr in self.CAPTURED.items():
            cap = bytes.fromhex(hexstr)
            mine = p.write_parametric_band(0, 1000, 1.0, gain)
            self.assertEqual(len(mine), len(cap))
            # Byte 2: appen sender alltid 03, vi sender ekte sjekksum. Firmwaren sjekker den ikke.
            self.assertEqual(mine[:2] + mine[3:], cap[:2] + cap[3:], gain)

    def test_params_roundtrip_with_reader(self):
        frame = p.write_parametric_band(0, 350, 4.5, -2.5)
        params = frame[-9:]
        self.assertIn("350 Hz, Q 4.50, gain -2.50 dB", p.describe_band(b"\x09" + params))
        self.assertIn("-0.50 dB", p.describe_band(b"\x09" + p.encode_band_params(7, 1000, 1.0, -0.5)))

    def test_limits(self):
        for kw in ({"freq": 10}, {"q": 0.0}, {"gain": 20.0}):
            args = {"freq": 1000, "q": 1.0, "gain": 0.0} | kw
            with self.assertRaises(ValueError):
                p.write_parametric_band(0, **args)


class RoomCurve(unittest.TestCase):
    def test_decode_matches_platin_graph(self):
        from fake_sm450 import FakeSM450
        c = p.decode_room_curve(FakeSM450().room_curve)
        self.assertEqual((len(c["measured"]), len(c["result"])), (69, 68))
        res = {round(x["freq"]): x["db"] for x in c["result"]}
        self.assertEqual((res[20], res[214], res[240]), (-3.2, 2.0, -2.6))   # avlest i Platin-appen: -3.3, +1.7, -2.6
        top = max(c["measured"], key=lambda x: x["db"])
        self.assertAlmostEqual(top["freq"], 89.8, places=1)                  # rød topp ved ca. 88 Hz i appen
        self.assertTrue(all(x["db"] == 0 for x in c["result"] if x["freq"] > 600))
        self.assertIsNone(p.decode_room_curve(b"\x00" * 10))


if __name__ == "__main__":
    unittest.main()
