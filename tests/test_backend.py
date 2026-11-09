"""Tester for innstillinger, tilgangskode, profiler, læring av filtertyper og API-et."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lemus_remote import filters
from lemus_remote import protocol as p
from lemus_remote.config import Config
from lemus_remote.device import CommandError, Speaker
from lemus_remote.profiles import BUILTIN, ProfileStore
from lemus_remote.web import WebServer

from fake_sm450 import FakeSM450


def tmp_config() -> Config:
    d = tempfile.mkdtemp()
    return Config(Path(d) / "config.json")


class ConfigAndProfiles(unittest.TestCase):
    def test_config_creates_access_code_and_persists(self):
        cfg = tmp_config()
        code = cfg["access_code"]
        self.assertRegex(code, r"^\d{6}$")
        cfg["host"] = "192.168.0.6"
        again = Config(cfg.path)
        self.assertEqual((again["access_code"], again["host"]), (code, "192.168.0.6"))

    def test_profiles_crud(self):
        store = ProfileStore(tmp_config())
        self.assertEqual(len(store.all()), len(BUILTIN))
        prof = store.save("Min lyd", [{"freq": 100, "q": 0.7, "gain": 3}])
        self.assertEqual(store.get(prof["id"])["bands"][0]["type"], filters.PARAMETRIC)
        dup = store.duplicate(prof["id"])
        self.assertIn("(kopi)", dup["name"])
        store.rename(dup["id"], "Kveld 2")
        self.assertEqual(store.get(dup["id"])["name"], "Kveld 2")
        store.delete(prof["id"])
        self.assertIsNone(store.get(prof["id"]))
        with self.assertRaises(ValueError):
            store.save("Feil", [{"freq": 5, "q": 1, "gain": 0}])  # under 20 Hz
        with self.assertRaises(ValueError):
            store.delete("noytral")  # innebygd: bruk reset


class Learner(unittest.TestCase):
    """Læreren skal bare godta en formel som treffer koeffisientene bit for bit (±1 LSB)."""

    def _frame(self, t, f, q, g, coeffs, enabled=True):
        params = p.encode_band_params(t, f, q, g, enabled)
        return p.write_band_raw(0, coeffs, params)

    def test_learns_lowpass_from_matching_coefficients(self):
        sp = Speaker("127.0.0.1")
        self.assertFalse(sp.state["eq_types"]["4"]["editable"])
        coeffs = filters.coefficients(4, "rbj_lp2_q", 15000, 0.7, None)
        sp._learn_from(self._frame(4, 15000, 0.7, None, coeffs))
        self.assertEqual(sp.formulas[4], "rbj_lp2_q")
        self.assertTrue(sp.state["eq_types"]["4"]["editable"])

    def test_captured_shelving_and_highpass_are_builtin_and_bit_exact(self):
        caps = {
            (6, 1000, 1.0, 2.0): "002303080101001d00d4ac81fe2d1c960102bcbaff290dd501d35a7a06010a000100020002",
            (5, 1000, 1.0, -3.0): "002303080101011d0096813afeb4bea000b8268bff27fcf201d49caa05010a000100fd0002",
            (2, 1000, 0.7, None): "002303080101021d00e92a8afe2daaec00e92a8aff2baa1201d0543802010a000046ffff02",
        }
        sp = Speaker("127.0.0.1")
        for (t, f_, q, g), hexstr in caps.items():
            cap = bytes.fromhex(hexstr)
            mine = sp.band_frame(cap[6], t, f_, q, g)
            self.assertEqual(mine[:2] + mine[3:8] + mine[28:], cap[:2] + cap[3:8] + cap[28:], t)
            m = p.parse_write_band(mine)["coeffs"]
            c = p.parse_write_band(cap)["coeffs"]
            self.assertTrue(filters.close(m, c), (t, m, c))  # ±1 LSB (avrunding)

    def test_band_state_frames_match_capture(self):
        for state, hexstr in ((p.BAND_OFF, "000703080601000101"), (p.BAND_ON, "000703080601000102"),
                              (p.BAND_DELETE, "000703080601020100")):
            ix = bytes.fromhex(hexstr)[6]
            mine, cap = p.set_band_state(ix, state), bytes.fromhex(hexstr)
            self.assertEqual(mine[:2] + mine[3:], cap[:2] + cap[3:])

    def test_rejects_unknown_coefficients(self):
        sp = Speaker("127.0.0.1")
        sp._learn_from(self._frame(3, 5000, None, None, [1, 2, 3, 4, 5]))
        self.assertNotIn(3, sp.formulas)

    def test_unlearned_type_is_refused(self):
        sp = Speaker("127.0.0.1")
        with self.assertRaises(CommandError):
            sp.band_frame(0, 1, 40, None, None)


class Api(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        ctl, luci = await self.fake.start()
        self.cfg = tmp_config()
        learned = []
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci,
                          on_learn=lambda l, d: learned.append((l, d)))
        self.learned = learned
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["luci_connected"] and s["volume"] is not None, 5)
        self.assertTrue(ok)
        self.web = WebServer(self.sp, "127.0.0.1", 0, config=self.cfg, profiles=ProfileStore(self.cfg))
        await self.web.start()

    async def asyncTearDown(self):
        await self.web.stop()
        await self.sp.stop()
        await self.fake.stop()

    async def call(self, method, path, body=None, headers=""):
        r, w = await asyncio.open_connection("127.0.0.1", self.web.bound_port)
        data = json.dumps(body).encode() if body is not None else b""
        w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n{headers}Content-Length: {len(data)}\r\n\r\n".encode() + data)
        raw = await r.read()
        w.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), head.decode(), payload

    async def test_access_code_for_remote_devices(self):
        # Fra localhost er alt tillatt. For en annen enhet kreves koden.
        self.assertTrue(self.web._authorized("127.0.0.1", {}))
        self.assertFalse(self.web._authorized("192.168.0.50", {}))
        code = self.cfg["access_code"]
        self.assertTrue(self.web._authorized("192.168.0.50", {"cookie": f"a=b; lemus_key={code}"}))
        self.assertFalse(self.web._authorized("192.168.0.50", {"cookie": "lemus_key=000000x"}))
        status, head, _ = await self.call("GET", f"/?key={code}")
        self.assertEqual(status, 302)
        self.assertIn(f"lemus_key={code}", head)
        status, _, payload = await self.call("POST", "/api/login", {"code": "nope"})
        self.assertEqual(status, 401)
        status, _, payload = await self.call("GET", "/api/state")
        self.assertEqual(json.loads(payload)["settings"]["access_code"], code)  # bare vist lokalt

    async def test_volume_guard(self):
        self.assertEqual(self.sp.state["volume"], 13)
        status, _, payload = await self.call("POST", "/api/volume", {"level": 90})
        self.assertEqual(status, 502)
        self.assertIn("volumhopp", json.loads(payload)["error"])
        self.assertEqual(self.fake.volume, 13)
        status, _, _ = await self.call("POST", "/api/volume", {"level": 20})
        self.assertEqual((status, self.fake.volume), (200, 20))

    async def test_profiles_api_and_apply(self):
        status, _, payload = await self.call("POST", "/api/profiles/apply", {"id": "tv"})
        self.assertEqual(status, 200, payload)
        # tone-oppsett: bass = lav shelving 200 Hz, mellomtone = parametrisk 1 kHz Q 0,5 (diskant 0 utelatt)
        params = sorted((b["type_code"], b["freq"], b["gain"]) for b in self.sp.state["bands"] if b["editable"])
        self.assertEqual(params, [(6, 200, -2.0), (7, 1000, 2.5)])
        self.assertEqual(next(b["q"] for b in self.sp.state["bands"] if b["type_code"] == 7), 0.5)
        self.assertTrue(self.sp.state["manual_eq"])  # profilen slår på Egen EQ
        status, _, payload = await self.call("POST", "/api/profiles/save_current", {"name": "Fra stua"})
        prof = json.loads(payload)["profile"]
        self.assertEqual(len(prof["bands"]), 2)
        status, _, payload = await self.call("GET", "/api/backup")
        backup = json.loads(payload)
        self.assertEqual(backup["profiles"][0]["name"], "Fra stua")
        self.assertEqual(len(backup["device"]["bands"]), 2)

    async def test_learning_through_tunnel_unlocks_type(self):
        coeffs = filters.coefficients(4, "rbj_lp2_q", 15000, 0.7, None)
        frame = p.write_band_raw(1, coeffs, p.encode_band_params(4, 15000, 0.7, None))
        self.fake.tunnel(frame)
        ok = await self.sp._wait_for(lambda s: s["eq_types"]["4"]["editable"], 3)
        self.assertTrue(ok)
        self.assertEqual(self.learned[-1][0], {"4": "rbj_lp2_q"})
        status, _, payload = await self.call("POST", "/api/band", {"type": 4, "freq": 16000, "q": 0.7})
        self.assertEqual(status, 200, payload)
        self.assertTrue(any(b["type_code"] == 4 and b["freq"] == 16000 for b in self.sp.state["bands"]))

    async def test_shelving_write_and_band_on_off(self):
        status, _, payload = await self.call("POST", "/api/band", {"type": 6, "freq": 200, "gain": 3})
        self.assertEqual(status, 200, payload)
        ix = next(b["ix"] for b in self.sp.state["bands"] if b["type_code"] == 6)
        status, _, payload = await self.call("POST", "/api/band_enabled", {"ix": ix, "enabled": False})
        self.assertEqual(status, 200, payload)
        self.assertFalse(next(b for b in self.sp.state["bands"] if b["ix"] == ix)["enabled"])
        status, _, _ = await self.call("POST", "/api/band_enabled", {"ix": ix, "enabled": True})
        self.assertTrue(next(b for b in self.sp.state["bands"] if b["ix"] == ix)["enabled"])

    async def test_fourth_band_is_refused(self):
        # Et fjerde bånd ga smell i høyttalerne: det skal aldri sendes.
        await self.sp.refresh_bands()
        for i in range(3 - len(self.sp.state["bands"])):
            status, _, payload = await self.call("POST", "/api/band", {"type": 7, "freq": 200 + 1000 * i, "q": 1, "gain": 1})
            self.assertEqual(status, 200, payload)
        status, _, payload = await self.call("POST", "/api/band", {"type": 7, "freq": 9000, "q": 1, "gain": 1})
        self.assertEqual(status, 502)
        self.assertIn("høyst 3", json.loads(payload)["error"])
        self.assertTrue(all(b["ix"] < 3 for b in self.sp.state["bands"]))
        writes = [f for f in self.fake.received if bytes(f)[3:5] in (b"\x08\x01", b"\x08\x06")]
        self.assertTrue(all(bytes(f)[6] < 3 for f in writes), writes)  # aldri noe til plass 4
        status, _, payload = await self.call("POST", "/api/profiles/save", {"name": "Fire", "bands": [
            {"freq": f, "q": 1, "gain": 1} for f in (100, 500, 2000, 8000)]})
        self.assertEqual(status, 400)
        with self.assertRaises(ValueError):
            p.write_band_raw(3, filters.UNITY, p.encode_band_params(7, 1000, 1.0, 0.0))

    async def test_unlearned_type_gives_clear_error(self):
        status, _, payload = await self.call("POST", "/api/band", {"type": 1, "freq": 40})
        self.assertEqual(status, 502)
        self.assertIn("Platin-appen", json.loads(payload)["error"])

    async def test_room_curve_and_settings(self):
        status, _, payload = await self.call("POST", "/api/room_curve", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(payload)["room_curve_raw"]), 140)
        status, _, _ = await self.call("POST", "/api/settings", {"host": "ikke-en-ip"})
        self.assertEqual(status, 400)
        status, _, _ = await self.call("POST", "/api/settings", {"max_volume_step": 10})
        self.assertEqual((status, self.sp.max_volume_step, self.cfg["max_volume_step"]), (200, 10, 10))


if __name__ == "__main__":
    unittest.main()
