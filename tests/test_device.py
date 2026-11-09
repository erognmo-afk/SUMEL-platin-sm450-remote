import asyncio
import json
import unittest

from lemus_remote import protocol as p
from lemus_remote.device import CommandError, Speaker
from lemus_remote.web import WebServer

from fake_sm450 import FakeSM450

ALLOWED_PREFIXES = {(0x02, 0x89), (0x08, 0x04), (0x08, 0x01), (0x08, 0x06), (0x02, 0x0D), (0x02, 0x01), (0x02, 0x03), (0x02, 0x0F), (0x03, 0x05), (0x03, 0x04),
                    (0x07, 0x02), (0x07, 0x01), (0x08, 0x05), (0x08, 0x02),
                    (0x07, 0x09)}  # 07 09 = les romkurven (bare lesing)


class SpeakerAgainstFake(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        ctl, luci = await self.fake.start()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["luci_connected"]
                                     and s["room_correction"] is not None and s["preset"] is not None, 5)
        self.assertTrue(ok, self.sp.state)

    async def asyncTearDown(self):
        await self.sp.stop()
        await self.fake.stop()
        # Ingenting utenfor den lukkede listen skal ha blitt sendt.
        for f in self.fake.received:
            self.assertIn((f[3], f[4]), ALLOWED_PREFIXES, f.hex(" "))
        self.assertNotIn((0x02, 0x20), {(f[3], f[4]) for f in self.fake.received})

    async def test_initial_state(self):
        await self.sp._wait_for(lambda st: st["versions"] is not None, 3)
        self.assertEqual(self.sp.state["versions"]["mcu"], "177")
        s = self.sp.state
        self.assertTrue(s["power"])
        self.assertEqual(s["source"], 1)
        self.assertEqual(s["volume"], 13)
        self.assertTrue(s["manual_eq"])
        self.assertTrue(s["room_correction"])
        self.assertEqual(s["preset"], 0)
        self.assertEqual(len(s["sources"]), 4)

    async def test_follows_changes_from_other_clients(self):
        # Platin-appen slår av romkorreksjonen: kommandoen speiles i tunnelen, og vi leser på nytt.
        self.assertTrue(await self.sp._wait_for(lambda st: st["room_correction"] is True, 3))
        self.fake.room = False
        self.fake.tunnel(p.set_room_correction(False))
        self.assertTrue(await self.sp._wait_for(lambda st: st["room_correction"] is False, 4))
        # Lesinger fra andre klienter og våre egne kommandoer gir ingen oppfølging.
        if self.sp._follow_task:
            await self.sp._follow_task
        before = self.sp._follow_task
        self.sp._on_foreign(p.GET_POWER)
        self.assertIs(self.sp._follow_task, before)

    async def test_commands(self):
        await self.sp.set_manual_eq(False)
        self.assertFalse(self.fake.manual_eq)
        await self.sp.set_manual_eq(True)
        await self.sp.set_room_correction(False)  # bekreftes ved polling
        self.assertFalse(self.sp.state["room_correction"])
        await self.sp.select_preset(2)
        self.assertEqual(self.fake.preset, 2)
        await self.sp.select_source(4)
        self.assertEqual(self.sp.state["source"], 4)
        await self.sp.set_volume(20)
        self.assertEqual(self.fake.volume, 20)
        await self.sp.set_power(True)  # er allerede på: ingen veksling
        self.assertNotIn((0x02, 0x0F), {(f[3], f[4]) for f in self.fake.received})
        await self.sp.set_power(False)
        self.assertFalse(self.fake.power)
        woke = []

        async def fake_wake(host, local_ip):  # simulerer DLNA-vekking: kilde blir Streaming
            woke.append(host)
            self.fake.power, self.fake.source = True, 4
            self.fake._broadcast(bytes.fromhex("000400020e01"))

        self.sp.waker = fake_wake
        await self.sp.set_power(True)
        self.assertEqual(woke, ["127.0.0.1"])
        self.assertTrue(self.sp.state["power"])
        self.assertEqual(self.fake.source, 4 if self.sp.state["source"] == 4 else self.sp.state["source"])

    async def test_eq_write_roundtrip(self):
        from lemus_remote.eqskrivetest import Conn
        r, w = await asyncio.open_connection("127.0.0.1", self.fake.ctl_server.sockets[0].getsockname()[1])
        c = Conn(r, w)
        band = await c.read_band0()
        self.assertEqual((band["freq"], band["gain"]), (1000, 3.0))
        await c.send(p.write_parametric_band(0, 1000, 1.0, 1.0))
        await asyncio.sleep(0.2)
        band = await c.read_band0()
        self.assertEqual(band["gain"], 1.0)
        c.task.cancel()
        w.close()

    async def test_reconnects_when_device_goes_silent(self):
        ctl = self.fake.ctl_server.sockets[0].getsockname()[1]
        sp = Speaker("127.0.0.1", control_port=ctl, use_luci=False)
        sp.keepalive, sp.watchdog = 0.2, 0.6
        sp.start()
        try:
            self.assertTrue(await sp._wait_for(lambda st: st["connected"], 5))
            self.fake.silent = True  # svarer ikke lenger på noe, men holder forbindelsen åpen
            ok = await sp._wait_for(lambda st: not st["connected"], 5)
            self.assertTrue(ok, "skulle ha merket den døde forbindelsen")
            self.fake.silent = False
            ok = await sp._wait_for(lambda st: st["connected"], 8)
            self.assertTrue(ok, "skulle ha koblet til igjen")
        finally:
            await sp.stop()

    async def test_create_and_delete_band(self):
        from lemus_remote.eqskrivetest import Conn
        r, w = await asyncio.open_connection("127.0.0.1", self.fake.ctl_server.sockets[0].getsockname()[1])
        c = Conn(r, w)
        self.assertEqual(list(await c.read_all()), [0])
        await c.send(p.write_parametric_band(1, 3000, 1.0, -2.0))
        await asyncio.sleep(0.2)
        bands = await c.read_all()
        self.assertEqual(bands[1]["freq"], 3000)
        await c.send(p.delete_band(1))
        await asyncio.sleep(0.3)
        self.assertIn(1, c.deleted)
        self.assertEqual(list(await c.read_all()), [0])
        c.task.cancel()
        w.close()

    async def test_speaker_eq_bands(self):
        await self.sp.refresh_bands()
        self.assertEqual([b["ix"] for b in self.sp.state["bands"]], [0])
        ix = await self.sp.add_band(3000, 1.0, -2.0)
        self.assertEqual(ix, 1)
        self.assertEqual(self.sp.state["bands"][1]["freq"], 3000)
        await self.sp.set_band(1, 3500, 2.0, 1.5)
        self.assertEqual((self.sp.state["bands"][1]["freq"], self.sp.state["bands"][1]["gain"]), (3500, 1.5))
        await self.sp.delete_band(1)
        self.assertEqual([b["ix"] for b in self.sp.state["bands"]], [0])
        await self.sp.delete_band(0)
        self.assertEqual(self.sp.state["bands"], [])  # tom liste gir ikke svar, stillhet = ingen bånd
        with self.assertRaises(CommandError):
            await self.sp.set_band(5, 1000, 1.0, 0.0)

    async def test_new_band_reuses_lowest_free_slot(self):
        await self.sp.refresh_bands()
        self.assertEqual(await self.sp.add_band(500, 1.0, 1.0), 1)
        self.assertEqual(await self.sp.add_band(2000, 1.0, 1.0), 2)
        await self.sp.delete_band(1)
        # plassene flyttes ikke ved sletting, og et nytt bånd fyller hullet i stedet for å krype oppover
        self.assertEqual(await self.sp.add_band(700, 1.0, -1.0), 1)
        self.assertEqual([b["ix"] for b in self.sp.state["bands"]], [0, 1, 2])

    async def test_capacity_logic_against_fake(self):
        from lemus_remote.eqskrivetest import Conn
        r, w = await asyncio.open_connection("127.0.0.1", self.fake.ctl_server.sockets[0].getsockname()[1])
        c = Conn(r, w)
        for ix in (1, 2):
            await c.send(p.write_parametric_band(ix, 500 * ix, 1.0, 0.0))
        await asyncio.sleep(0.2)
        self.assertEqual(sorted(await c.read_all()), [0, 1, 2])
        for ix in (2, 1):
            await c.send(p.delete_band(ix))
        await asyncio.sleep(0.2)
        self.assertEqual(sorted(await c.read_all()), [0])
        c.task.cancel()
        w.close()

    async def test_apply_profile(self):
        self.fake.bands[1] = bytes.fromhex("010132ffffffff0002")  # 6 dB høypass (ikke lært): skal ikke røres
        await self.sp.apply_bands([{"freq": 100, "q": 0.7, "gain": 4}, {"freq": 2500, "q": 1, "gain": 3}])
        params = sorted((b["freq"], b["gain"]) for b in self.sp.state["bands"] if b["editable"])
        self.assertEqual(params, [(100, 4.0), (2500, 3.0)])
        self.assertIn(1, self.fake.bands)  # shelving-båndet står igjen
        await self.sp.apply_bands([])
        self.assertEqual([b["ix"] for b in self.sp.state["bands"]], [1])

    async def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            await self.sp.select_preset(0)
        with self.assertRaises(CommandError):
            await self.sp.select_source(9)

    async def test_web_api(self):
        web = WebServer(self.sp, "127.0.0.1", 0)
        await web.start()
        try:
            async def call(method, path, body=None):
                r, w = await asyncio.open_connection("127.0.0.1", web.bound_port)
                data = json.dumps(body).encode() if body is not None else b""
                w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
                raw = await r.read()
                w.close()
                head, _, payload = raw.partition(b"\r\n\r\n")
                return int(head.split()[1]), payload

            status, page = await call("GET", "/")
            self.assertEqual(status, 200)
            self.assertTrue(b"SUMEL" in page or b"Lemus" in page)  # nytt navn: SUMEL
            status, payload = await call("POST", "/api/manual_eq", {"on": False})
            self.assertEqual(status, 200, payload)
            self.assertFalse(json.loads(payload)["manual_eq"])
            status, _ = await call("POST", "/api/preset", {"n": 1})
            self.assertEqual(status, 400)  # tatt ut på SM450
            status, payload = await call("POST", "/api/band", {"freq": 4000, "q": 1.0, "gain": 2.0})
            self.assertEqual(status, 200, payload)
            self.assertIn(4000, [b["freq"] for b in json.loads(payload)["bands"]])
            status, _ = await call("POST", "/api/band", {"freq": 4000, "q": 1.0, "gain": 40.0})
            self.assertEqual(status, 400)  # utenfor grensene
            status, _ = await call("GET", "/api/manual_eq")
            self.assertEqual(status, 405)
        finally:
            await web.stop()


if __name__ == "__main__":
    unittest.main()
