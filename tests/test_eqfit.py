"""Tester for Kurve-EQ (eqfit), responsen fra enhetskoeffisientene og /api/eq_fit."""

import asyncio
import json
import math
import tempfile
import time
import unittest
from pathlib import Path

from lemus_remote import eqfit, filters
from lemus_remote import protocol as p
from lemus_remote.config import Config
from lemus_remote.device import Speaker
from lemus_remote.profiles import ProfileStore
from lemus_remote.web import WebServer

from fake_sm450 import FakeSM450

HANDLES = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def exact_target(n=60):
    """Peak +6 dB @ 1 kHz Q 1,4 + low shelf +3 dB @ 150 Hz, samplet slik SM450 gjør det."""
    fr = eqfit.log_grid(n)
    a = filters.band_response_db(7, "rbj_peak", 1000, 1.4, 6.0, fr)
    b = filters.band_response_db(6, "rbj_lowshelf_s1", 150, None, 3.0, fr)
    return [{"freq": f, "gain": x + y} for f, x, y in zip(fr, a, b)]


def assert_valid(tc, res, max_bands=3, types=(7, 6, 5)):
    tc.assertLessEqual(len(res["bands"]), max_bands)
    for b in res["bands"]:
        tc.assertIn(b["type"], types)
        tc.assertTrue(20 <= b["freq"] <= 20000, b)
        tc.assertTrue(-12 <= b["gain"] <= 12, b)
        if b["type"] in filters.HAS_Q:
            tc.assertTrue(0.3 <= b["q"] <= 8, b)
        else:
            tc.assertIsNone(b["q"])
        p.encode_band_params(b["type"], b["freq"], b["q"], b["gain"])  # høyttalerens egne grenser
        if b["q"] is not None:  # Q må leses tilbake likt (parse_band: heltall + hundredeler / 100)
            raw = p.encode_band_params(b["type"], b["freq"], b["q"], b["gain"])
            tc.assertEqual(raw[4] + raw[5] / 100, b["q"], b)


class Response(unittest.TestCase):
    def test_device_response_matches_float_formula(self):
        fr = [50, 200, 1000, 5000, 15000]
        got = filters.band_response_db(7, "rbj_peak", 1000, 1.0, 6.0, fr)
        self.assertAlmostEqual(got[2], 6.0, places=2)  # toppen treffer nivået
        self.assertLess(abs(got[0]), 0.1)
        unity = filters.device_response_db(filters.UNITY, fr)
        self.assertTrue(all(abs(x) < 1e-9 for x in unity))


class Fit(unittest.TestCase):
    def test_exactly_representable_target(self):
        res = eqfit.fit(exact_target())
        self.assertLess(res["max_err"], 0.3, res["bands"])
        self.assertLessEqual(len(res["bands"]), 3)
        assert_valid(self, res)
        self.assertTrue(150 <= len(res["target"]) <= 250)
        self.assertEqual(len(res["target"]), len(res["result"]))

    def test_flat_target_gives_no_bands(self):
        for pts in ([], [{"freq": f, "gain": 0} for f in HANDLES]):
            res = eqfit.fit(pts)
            self.assertEqual(res["bands"], [])
            self.assertEqual(res["max_err"], 0.0)

    def test_simple_target_uses_few_bands(self):
        res = eqfit.fit([{"freq": 20, "gain": 4}, {"freq": 100, "gain": 4}, {"freq": 400, "gain": 0},
                         {"freq": 20000, "gain": 0}])
        self.assertLessEqual(len(res["bands"]), 2)
        self.assertLess(res["max_err"], 0.6)

    def test_extreme_zigzag_is_valid_and_honest(self):
        pts = [{"freq": f, "gain": 12 if i % 2 else -12} for i, f in enumerate(HANDLES)]
        res = eqfit.fit(pts)
        assert_valid(self, res)
        self.assertGreater(res["max_err"], 3)  # tre bånd kan ikke følge sikksakken, og det sies ærlig
        worst = max(abs(t["db"] - r["db"]) for t, r in zip(res["target"], res["result"]))
        self.assertAlmostEqual(worst, res["max_err"], delta=0.02)

    def test_deterministic(self):
        pts = [{"freq": f, "gain": g} for f, g in zip(HANDLES, [5, 3, 1, 0, -2, -1, 0, 2, 3, 4])]
        self.assertEqual(json.dumps(eqfit.fit(pts)), json.dumps(eqfit.fit(pts)))

    def test_only_allowed_types_and_max_bands(self):
        pts = [{"freq": f, "gain": g} for f, g in zip(HANDLES, [6, 5, 3, 0, -3, 2, 0, 3, 5, 6])]
        res = eqfit.fit(pts, types=[7])
        assert_valid(self, res, types=(7,))
        res = eqfit.fit(pts, max_bands=1)
        assert_valid(self, res, max_bands=1)
        self.assertEqual(eqfit.fit(pts, max_bands=0)["bands"], [])
        self.assertEqual(eqfit.fit(pts, types=[2, 4])["bands"], [])  # pass-filtre brukes ikke
        self.assertLessEqual(len(eqfit.fit(pts, max_bands=9)["bands"]), p.MAX_BANDS)

    def test_result_is_computed_from_written_values(self):
        pts = [{"freq": f, "gain": g} for f, g in zip(HANDLES, [3, 3, 2, 0, -1, 2, 1, 0, -2, -3])]
        res = eqfit.fit(pts)
        fr = [x["freq"] for x in res["result"]]
        total = [0.0] * len(fr)
        for b in res["bands"]:
            r = filters.band_response_db(b["type"], filters.BUILTIN_FORMULAS[b["type"]], b["freq"], b["q"], b["gain"], fr)
            total = [x + y for x, y in zip(total, r)]
        for got, want in zip(res["result"], total):
            self.assertAlmostEqual(got["db"], want, delta=0.02)

    def test_time_limit(self):
        cases = [[{"freq": f, "gain": g} for f, g in zip(HANDLES, gs)] for gs in (
            [5, 3, 1, 0, -2, -1, 0, 2, 3, 4], [12, -12, 12, -12, 12, -12, 12, -12, 12, -12],
            [0, 0, 0, 0, 6, 0, 0, 0, 0, 0], [-6, -4, -2, 0, 2, 4, 6, 8, 10, 12])]
        for pts in cases:
            t = time.perf_counter()
            eqfit.fit(pts)
            self.assertLess(time.perf_counter() - t, 0.5)

    def test_interpolation_is_smooth_and_holds_ends(self):
        pts = eqfit.clean_points([{"freq": 100, "gain": 0}, {"freq": 1000, "gain": 6}, {"freq": 10000, "gain": 0}])
        fr = eqfit.log_grid()
        tg = eqfit.interpolate(pts, fr)
        self.assertEqual(tg[0], 0.0)
        self.assertEqual(tg[-1], 0.0)
        self.assertLessEqual(max(tg), 6.0 + 1e-9)  # ingen oversving
        self.assertGreaterEqual(min(tg), -1e-9)

    def test_summed_boost_stays_within_limit_also_outside_audio_band(self):
        # To shelving-filtre oppå hverandre ga før +15 dB under 20 Hz (stor membranbevegelse).
        probe = [1, 5, 10, 15, 20, 20000, 23000, 23900]
        cases = []
        for i in (0, 9, 1, 8):
            for v in (12, -12):
                g = [0] * 10
                g[i] = v
                cases.append(g)
        cases += [[12, 12, 12, 0, 0, 0, 0, 0, 12, 12], [12, -12] * 5, [6, 12, 6, 0, 0, 0, 0, 6, 12, 12]]
        for g in cases:
            res = eqfit.fit([{"freq": f, "gain": x} for f, x in zip(HANDLES, g)])
            assert_valid(self, res)
            tot = [0.0] * len(probe)
            for b in res["bands"]:
                r = filters.band_response_db(b["type"], filters.BUILTIN_FORMULAS[b["type"]], b["freq"], b["q"], b["gain"], probe)
                tot = [x + y for x, y in zip(tot, r)]
            peak = max([abs(x) for x in tot] + [abs(x["db"]) for x in res["result"]])
            self.assertLessEqual(peak, 12.5, (g, res["bands"], tot))
            self.assertLessEqual(res["peak_db"], 12.5)

    def test_whole_curve_raised_is_volume_not_eq(self):
        res = eqfit.fit([{"freq": f, "gain": 12} for f in HANDLES])
        self.assertEqual(res["bands"], [])  # ikke +12 dB bredbånd med ett trykk på «Bruk»
        self.assertEqual(res["level_offset"], 12.0)
        res = eqfit.fit([{"freq": f, "gain": g} for f, g in zip(HANDLES, [8, 6, 6, 6, 6, 6, 6, 6, 6, 6])])
        self.assertEqual(res["level_offset"], 6.0)
        self.assertLessEqual(len(res["bands"]), 1)
        self.assertLess(max(b["gain"] for b in res["bands"]), 3)

    def test_single_bump_uses_one_band(self):
        for i, v in ((5, 6), (2, 4), (7, -5)):
            g = [0] * 10
            g[i] = v
            res = eqfit.fit([{"freq": f, "gain": x} for f, x in zip(HANDLES, g)])
            self.assertEqual(len(res["bands"]), 1, res["bands"])  # ingen små motfiltre som tar opp plassene
            self.assertLess(res["max_err"], 1.2)

    def test_base_bands_are_part_of_target_and_result(self):
        g = [0, 0, 0, 0, 0, 3, 0, 0, 0, 0]
        hp = {"type": 2, "freq": 40, "q": 0.71, "gain": None, "enabled": True}
        plain = eqfit.fit([{"freq": f, "gain": x} for f, x in zip(HANDLES, g)])
        res = eqfit.fit([{"freq": f, "gain": x} for f, x in zip(HANDLES, g)], max_bands=2, base_bands=[hp])
        self.assertEqual(res["bands"], plain["bands"])  # høypasset fylles ikke ut med bassløft
        self.assertLess(res["result"][0]["db"], -6)  # men vises: det er det DSP-en gjør
        self.assertLess(res["target"][0]["db"], -6)
        self.assertEqual(res["max_err"], plain["max_err"])
        self.assertEqual(res["base_unknown"], 0)
        self.assertEqual(eqfit.fit([], base_bands=[{"type": 3, "freq": 9000}])["base_unknown"], 1)

    def test_readback_safe_q(self):
        for q100 in range(10, 2001):
            q = p.readback_safe_q(q100 / 100)
            raw = p.encode_band_params(7, 1000, q, 0)
            self.assertEqual(raw[4] + raw[5] / 100, q)
            self.assertLessEqual(abs(q - q100 / 100), 0.021)

    def test_bad_points_are_refused(self):
        for bad in ([{"freq": 5, "gain": 0}], [{"freq": 100, "gain": 40}], [{"freq": "x", "gain": 0}],
                    [{"freq": 100, "gain": float("nan")}], [{"freq": 100 + i, "gain": 0} for i in range(65)]):
            with self.assertRaises((ValueError, KeyError, TypeError)):
                eqfit.fit(bad)


class Api(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        # ett bånd av en type vi ikke kan skrive (6 dB høypass): det opptar en av de tre plassene
        self.fake.bands = {0: bytes.fromhex("07010a000100030002"), 1: bytes.fromhex("01010028ffffffff02")}
        ctl, luci = await self.fake.start()
        d = tempfile.mkdtemp()
        self.cfg = Config(Path(d) / "config.json")
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and len(s["bands"]) == 2, 5)
        self.assertTrue(ok)
        self.web = WebServer(self.sp, "127.0.0.1", 0, config=self.cfg, profiles=ProfileStore(self.cfg))
        await self.web.start()

    async def asyncTearDown(self):
        await self.web.stop()
        await self.sp.stop()
        await self.fake.stop()

    async def call(self, method, path, body=None):
        r, w = await asyncio.open_connection("127.0.0.1", self.web.bound_port)
        data = json.dumps(body).encode() if body is not None else b""
        w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
        raw = await r.read()
        w.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(payload)

    async def test_eq_fit_does_not_write_and_respects_locked_slots(self):
        before = len(self.fake.received)
        pts = [{"freq": f, "gain": g} for f, g in zip(HANDLES, [6, 5, 3, 0, -3, 2, 0, 3, 5, 6])]
        status, res = await self.call("POST", "/api/eq_fit", {"points": pts})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["max_bands"], 2)
        self.assertLessEqual(len(res["bands"]), 2)
        await asyncio.sleep(0.1)
        self.assertEqual(self.fake.received[before:], [])  # ingenting sendt til høyttaleren
        status, res = await self.call("POST", "/api/eq_fit", {"points": [{"freq": 3, "gain": 0}]})
        self.assertEqual(status, 400)

    async def test_fit_result_can_be_applied(self):
        pts = [{"freq": f, "gain": g} for f, g in zip(HANDLES, [0, 0, 6, 0, 0, 0, 0, -4, 0, 0])]
        status, res = await self.call("POST", "/api/eq_fit", {"points": pts})
        self.assertEqual(status, 200, res)
        status, st = await self.call("POST", "/api/bands_apply", {"bands": res["bands"]})
        self.assertEqual(status, 200, st)
        got = sorted((b["type_code"], b["freq"], b["gain"]) for b in st["bands"] if b["editable"])
        self.assertEqual(got, sorted((b["type"], b["freq"], b["gain"]) for b in res["bands"]))
        self.assertTrue(any(b["type_code"] == 1 for b in st["bands"]))  # det låste båndet er urørt
        self.assertTrue(all(b["ix"] < 3 for b in st["bands"]))

    async def test_non_object_body_is_refused(self):
        for body in ([1, 2], "x", 3):
            status, res = await self.call("POST", "/api/eq_fit", body)
            self.assertEqual(status, 400, res)

    async def test_readback_unsafe_q_is_confirmed(self):
        # 1,89 og 1,61 leses tilbake som 1.8900000000000001 / 1.6099999999999999; skrivingen må likevel bekreftes
        for q in (1.89, 1.61):
            status, st = await self.call("POST", "/api/band", {"ix": 0, "type": 7, "freq": 100, "q": q, "gain": 2})
            self.assertEqual(status, 200, st)
            b = next(x for x in st["bands"] if x["ix"] == 0)
            self.assertAlmostEqual(b["q"], q, delta=0.021)

    async def test_update_profile_keeps_curve(self):
        curve = [{"freq": f, "gain": 2.0} for f in HANDLES]
        status, res = await self.call("POST", "/api/profiles/save", {"name": "K", "bands": [], "curve": curve})
        pid = res["profile"]["id"]
        status, res = await self.call("POST", "/api/profiles/save_current", {"name": "K", "id": pid})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["profile"]["curve"], curve)
        status, res = await self.call("POST", "/api/profiles/save_current", {"name": "K", "id": pid, "curve": None})
        self.assertNotIn("curve", res["profile"])

    async def test_room_curve_is_decoded_in_state(self):
        status, st = await self.call("GET", "/api/state")
        self.assertIn("room_curve", st)  # leses automatisk ved tilkobling, kan være None et øyeblikk
        status, st = await self.call("POST", "/api/room_curve", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(st["room_curve"]["measured"]), 69)
        self.assertEqual(len(st["room_curve"]["result"]), 68)
        self.assertEqual(st["room_curve"]["measured"][0]["freq"], 20.0)

    async def test_profile_keeps_curve(self):
        curve = [{"freq": f, "gain": 1.5} for f in HANDLES]
        status, res = await self.call("POST", "/api/profiles/save", {
            "name": "Kurve", "bands": [{"type": 6, "freq": 150, "gain": 1.5}], "curve": curve})
        self.assertEqual(status, 200, res)
        pid = res["profile"]["id"]
        self.assertEqual(res["profile"]["curve"], curve)
        status, res = await self.call("POST", "/api/profiles/rename", {"id": pid, "name": "Kurve 2"})
        prof = next(x for x in res["profiles"] if x["id"] == pid)
        self.assertEqual((prof["name"], prof["curve"]), ("Kurve 2", curve))
        status, res = await self.call("POST", "/api/profiles/duplicate", {"id": pid})
        self.assertEqual(sum(1 for x in res["profiles"] if x.get("curve") == curve), 2)
        status, res = await self.call("POST", "/api/profiles/save", {
            "name": "Feil", "bands": [], "curve": [{"freq": 10, "gain": 0}]})
        self.assertEqual(status, 400)
        store = ProfileStore(Config(self.cfg.path))  # lagret på disk
        self.assertEqual(store.get(pid)["curve"], curve)
        n = store.import_many([{"name": "Imp", "bands": [], "curve": curve}])
        self.assertEqual(n, 1)
        self.assertTrue(any(x["name"] == "Imp" and x["curve"] == curve for x in store.all()))


class ApiHighPass(Api):
    """Et skrivbart 12 dB høypassfilter (type 2) skal beholdes av Kurve-EQ, ikke overskrives."""

    async def asyncSetUp(self):
        self.fake = FakeSM450()
        self.fake.bands = {0: bytes.fromhex("020100280047ffff02"), 1: bytes.fromhex("07010a000100030002")}
        ctl, luci = await self.fake.start()
        d = tempfile.mkdtemp()
        self.cfg = Config(Path(d) / "config.json")
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and len(s["bands"]) == 2, 5)
        self.assertTrue(ok)
        self.web = WebServer(self.sp, "127.0.0.1", 0, config=self.cfg, profiles=ProfileStore(self.cfg))
        await self.web.start()

    async def test_high_pass_is_kept(self):
        pts = [{"freq": f, "gain": g} for f, g in zip(HANDLES, [12, 0, 0, 0, 0, 0, 0, 0, 0, 0])]
        status, res = await self.call("POST", "/api/eq_fit", {"points": pts})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["max_bands"], 2)
        self.assertEqual([(k["type"], k["freq"]) for k in res["kept"]], [(2, 40)])
        self.assertLess(res["result"][0]["db"], 6)  # høypasset er med i resultatet
        # slik appen sender det: høypasset på sin plass, kurvefiltrene rundt
        bands = [{"type": 2, "freq": 40, "q": 0.71}] + res["bands"]
        status, st = await self.call("POST", "/api/bands_apply", {"bands": bands})
        self.assertEqual(status, 200, st)
        self.assertTrue(any(b["type_code"] == 2 and b["freq"] == 40 for b in st["bands"]))
        self.assertLessEqual(len(st["bands"]), 3)

    # de arvede testene forutsetter det låste 6 dB-høypasset; de som gjelder her:
    test_eq_fit_does_not_write_and_respects_locked_slots = None
    test_fit_result_can_be_applied = None
    test_room_curve_is_decoded_in_state = None
    test_profile_keeps_curve = None
    test_non_object_body_is_refused = None
    test_readback_unsafe_q_is_confirmed = None
    test_update_profile_keeps_curve = None


if __name__ == "__main__":
    unittest.main()
