"""Tester for redigerbare innebygde profiler: overstyring, «Tilbakestill til standard»,
nytt navn, avvist sletting og sikkerhetskopi/gjenoppretting."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lemus_remote import filters
from lemus_remote.config import Config
from lemus_remote.device import Speaker
from lemus_remote.profiles import BUILTIN, ProfileStore, validate_bands
from lemus_remote.web import WebServer

from fake_sm450 import FakeSM450

BANDS = [{"type": filters.PARAMETRIC, "freq": 150, "q": 0.7, "gain": 4.0}]
CURVE = [{"freq": 100.0, "gain": 2.0}, {"freq": 1000.0, "gain": -1.0}]


def tmp_config() -> Config:
    return Config(Path(tempfile.mkdtemp()) / "config.json")


class BuiltinOverrides(unittest.TestCase):
    def setUp(self):
        self.cfg = tmp_config()
        self.store = ProfileStore(self.cfg)

    def test_unmodified_builtins_are_flagged(self):
        profs = self.store.all()
        self.assertEqual([p["id"] for p in profs], [b["id"] for b in BUILTIN])
        self.assertTrue(all(p["builtin"] and p["modified"] is False for p in profs))

    def test_builtins_use_tone_setup(self):
        # Bass = lav shelving 200 Hz, mellomtone = parametrisk 1 kHz Q 0,5, diskant = høy shelving 3,5 kHz
        tone = {6: (200, None), 7: (1000, 0.5), 5: (3500, None)}
        want = {"varm": {6: 2.5, 5: -1.5}, "musikk": {6: 3.0, 5: 1.5}, "tv": {6: -2.0, 7: 2.5},
                "kveld": {6: -4.0, 5: -2.0}}
        for pid, gains in want.items():
            prof = self.store.get(pid)
            self.assertIs(prof["manual_eq"], True, pid)
            self.assertEqual({b["type"]: b["gain"] for b in prof["bands"]}, gains, pid)
            for b in prof["bands"]:
                self.assertEqual((b["freq"], b["q"]), tone[b["type"]], pid)
            self.assertEqual(validate_bands(prof["bands"])[0]["type"], prof["bands"][0]["type"])
        self.assertEqual(self.store.get("noytral")["bands"], [])
        self.assertIsNot(self.store.get("noytral").get("manual_eq"), True)

    def test_existing_override_survives_new_defaults(self):
        self.cfg["profile_overrides"] = {"musikk": {"name": "Rock", "bands": BANDS}}  # lagret av en eldre versjon
        prof = ProfileStore(self.cfg).get("musikk")
        self.assertEqual((prof["name"], prof["bands"], prof["modified"]), ("Rock", BANDS, True))
        self.assertTrue(ProfileStore(self.cfg).get("varm")["manual_eq"])

    def test_save_builtin_stores_override_with_same_id(self):
        prof = self.store.save("Varm+", BANDS, "varm", desc="Min varme", curve=CURVE)
        self.assertEqual((prof["id"], prof["name"], prof["builtin"], prof["modified"]), ("varm", "Varm+", True, True))
        self.assertEqual(prof["bands"][0]["freq"], 150)
        self.assertEqual(prof["curve"], CURVE)
        self.assertEqual(self.cfg["profiles"], [])  # ikke en ny egen profil
        self.assertIn("varm", self.cfg["profile_overrides"])
        self.assertEqual(len(self.store.all()), len(BUILTIN))
        again = ProfileStore(Config(self.cfg.path)).get("varm")  # lagret på disk
        self.assertEqual((again["name"], again["desc"], again["modified"]), ("Varm+", "Min varme", True))
        self.assertFalse(self.store.get("musikk")["modified"])  # de andre er urørt

    def test_save_without_desc_keeps_description(self):
        prof = self.store.save("Kveld", BANDS, "kveld")
        self.assertEqual(prof["desc"], next(b for b in BUILTIN if b["id"] == "kveld")["desc"])

    def test_rename_builtin(self):
        prof = self.store.rename("tv", "Nyheter")
        self.assertEqual((prof["name"], prof["modified"]), ("Nyheter", True))
        orig = next(b for b in BUILTIN if b["id"] == "tv")
        self.assertEqual([(b["freq"], b["gain"]) for b in prof["bands"]], [(b["freq"], b["gain"]) for b in orig["bands"]])
        self.assertEqual(BUILTIN[3]["name"], "TV / dialog")  # selve standarden er urørt

    def test_reset_removes_override(self):
        self.store.save("Varm+", BANDS, "varm", curve=CURVE)
        prof = self.store.reset("varm")
        orig = next(b for b in BUILTIN if b["id"] == "varm")
        self.assertEqual((prof["name"], prof["modified"]), (orig["name"], False))
        self.assertNotIn("curve", prof)
        self.assertEqual(len(prof["bands"]), len(orig["bands"]))
        self.assertNotIn("varm", self.cfg["profile_overrides"])
        self.assertFalse(self.store.reset("varm")["modified"])  # igjen: ingen feil
        own = self.store.save("Egen", BANDS)
        with self.assertRaisesRegex(ValueError, "bare innebygde profiler kan tilbakestilles"):
            self.store.reset(own["id"])

    def test_delete_builtin_is_refused(self):
        self.store.save("Varm+", BANDS, "varm")
        with self.assertRaisesRegex(ValueError, "innebygde profiler kan ikke slettes"):
            self.store.delete("varm")
        self.assertTrue(self.store.get("varm")["modified"])

    def test_duplicate_modified_builtin(self):
        self.store.save("Varm+", BANDS, "varm", curve=CURVE)
        dup = self.store.duplicate("varm")
        self.assertEqual((dup["name"], dup["builtin"], dup["curve"]), ("Varm+ (kopi)", False, CURVE))
        self.assertNotEqual(dup["id"], "varm")

    def test_import_overrides(self):
        n = ProfileStore(self.cfg).import_many([], {"kveld": {"name": "Natt", "bands": BANDS}, "ukjent": {}})
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get("kveld")["name"], "Natt")
        # eldre form: endret innebygd profil i profillisten
        n = self.store.import_many([{**self.store.get("kveld"), "id": "musikk", "name": "Rock"}])
        self.assertEqual((n, self.store.get("musikk")["name"]), (1, "Rock"))


class ProfileApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        ctl, luci = await self.fake.start()
        self.cfg = tmp_config()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["luci_connected"] and s["volume"] is not None, 5)
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

    def prof(self, res, pid):
        return next(x for x in res["profiles"] if x["id"] == pid)

    async def test_edit_rename_reset_delete(self):
        status, res = await self.call("POST", "/api/profiles/save", {"id": "varm", "name": "Varm", "bands": BANDS,
                                                                     "curve": CURVE})
        self.assertEqual(status, 200, res)
        self.assertEqual((res["profile"]["modified"], res["profile"]["curve"]), (True, CURVE))
        status, res = await self.call("POST", "/api/profiles/save_current", {"id": "tv", "name": "TV / dialog"})
        self.assertEqual(status, 200, res)
        self.assertTrue(self.prof(res, "tv")["modified"])
        status, res = await self.call("POST", "/api/profiles/rename", {"id": "kveld", "name": "Natt"})
        self.assertEqual((status, self.prof(res, "kveld")["name"]), (200, "Natt"))
        status, res = await self.call("POST", "/api/profiles/delete", {"id": "kveld"})
        self.assertEqual(status, 400)
        self.assertIn("innebygde profiler kan ikke slettes", res["error"])
        status, res = await self.call("POST", "/api/profiles/reset", {"id": "kveld"})
        self.assertEqual(status, 200, res)
        self.assertEqual((res["profile"]["name"], res["profile"]["modified"]), ("Kveld", False))
        status, res = await self.call("POST", "/api/profiles/reset", {"id": "nope"})
        self.assertEqual(status, 400)
        # Bruk: den endrede versjonen skrives til høyttaleren
        status, res = await self.call("POST", "/api/profiles/apply", {"id": "varm"})
        self.assertEqual(status, 200, res)
        self.assertEqual([(b["freq"], b["gain"]) for b in self.sp.state["bands"] if b["editable"]], [(150, 4.0)])

    async def test_apply_every_builtin(self):
        self.fake.manual_eq = False
        await self.sp.set_manual_eq(False)
        for pid in ("varm", "tv", "noytral"):  # lav + høy shelving, lav shelving + parametrisk, ingen bånd
            status, res = await self.call("POST", "/api/profiles/apply", {"id": pid})
            self.assertEqual(status, 200, res)
            want = sorted((b["type"], b["freq"], b["gain"]) for b in self.web.profiles.get(pid)["bands"])
            got = sorted((b["type_code"], b["freq"], b["gain"]) for b in self.sp.state["bands"] if b["editable"])
            self.assertEqual(got, want, pid)
            self.assertTrue(self.sp.state["manual_eq"], pid)  # Egen EQ slått på (Nøytral rører den ikke)
        self.assertTrue(all(b["ix"] < 3 for b in self.sp.state["bands"]))

    async def test_backup_and_restore_keep_overrides(self):
        await self.call("POST", "/api/profiles/save", {"id": "musikk", "name": "Rock", "bands": BANDS, "curve": CURVE})
        await self.call("POST", "/api/profiles/save", {"name": "Egen", "bands": BANDS})
        status, backup = await self.call("GET", "/api/backup")
        self.assertEqual(status, 200)
        self.assertEqual(backup["profile_overrides"]["musikk"]["name"], "Rock")
        self.assertEqual([p["name"] for p in backup["profiles"]], ["Egen"])
        # ny maskin: tom config
        self.web.profiles = ProfileStore(tmp_config())
        status, res = await self.call("POST", "/api/restore", backup)
        self.assertEqual((status, res["imported"]), (200, 2))
        musikk = self.prof(res, "musikk")
        self.assertEqual((musikk["name"], musikk["modified"], musikk["curve"]), ("Rock", True, CURVE))
        self.assertFalse(self.prof(res, "varm")["modified"])


if __name__ == "__main__":
    unittest.main()
