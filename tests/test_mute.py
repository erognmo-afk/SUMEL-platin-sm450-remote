"""Demping (mute), strøm-status for alle enheter (power_busy) og ping i SSE."""

import asyncio
import json
import unittest

from lemus_remote import protocol as p
from lemus_remote.device import CommandError, Speaker
from lemus_remote.web import WebServer

from fake_sm450 import FakeSM450


def volume_commands(fake: FakeSM450) -> list[int]:
    """Volumene vi har sendt til høyttaleren (LUCI boks 0x40), i rekkefølge."""
    out = []
    for f in fake.luci_received:
        if f[2] == p.LUCI_COMMAND and int.from_bytes(f[3:5], "little") == p.BOX_VOLUME:
            out.append(int(f[10:]))
    return out


def mute_commands(fake: FakeSM450) -> list[bytes]:
    return [bytes(f[10:]) for f in fake.luci_received
            if f[2] == p.LUCI_COMMAND and int.from_bytes(f[3:5], "little") == p.BOX_MUTE]


class MuteProtocol(unittest.TestCase):
    def test_frames_and_decode(self):
        self.assertIn((p.LUCI_COMMAND, p.BOX_MUTE), p.LUCI_ALLOWED)
        self.assertIn((p.LUCI_QUERY, p.BOX_MUTE), p.LUCI_ALLOWED)
        self.assertEqual(p.luci_set_mute(True), bytes.fromhex("0000023f000000000400") + b"MUTE")
        self.assertEqual(p.luci_set_mute(False), bytes.fromhex("0000023f000000000600") + b"UNMUTE")
        self.assertEqual(p.LUCI_GET_MUTE, bytes.fromhex("0000013f000000000000"))
        self.assertEqual(p.decode_luci(0x3F, b"MUTE"), {"mute": True})
        self.assertEqual(p.decode_luci(0x3F, b"UNMUTE\x00"), {"mute": False})
        self.assertIsNone(p.decode_luci(0x3F, b"noe annet"))


class MuteAgainstFake(unittest.IsolatedAsyncioTestCase):
    native = True

    async def asyncSetUp(self):
        self.fake = FakeSM450()
        self.fake.native_mute = self.native
        self.fake.volume = 40
        ctl, luci = await self.fake.start()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.mute_confirm = 0.6
        self.sp.mute_ramp_delay = 0.01
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["luci_connected"]
                                     and s["volume"] is not None and s["mute"] is not None, 5)
        self.assertTrue(ok, self.sp.state)

    async def asyncTearDown(self):
        await self.sp.stop()
        await self.fake.stop()
        # aldri et volumhopp oppover større enn volumvakten (ned til 0 er lov)
        prev = 40
        for v in volume_commands(self.fake):
            if v > prev:
                self.assertLessEqual(v - prev, min(5, self.sp.max_volume_step), volume_commands(self.fake))
            prev = v


class NativeMute(MuteAgainstFake):
    async def test_reads_mute_on_connect(self):
        self.assertIs(self.sp.state["mute"], False)
        self.assertIsNone(self.sp.state["mute_mode"])
        self.assertIn(p.LUCI_GET_MUTE, [bytes(f) for f in self.fake.luci_received])

    async def test_native_mute_and_unmute(self):
        await self.sp.set_mute(True)
        self.assertTrue(self.fake.muted)
        self.assertEqual((self.sp.state["mute"], self.sp.state["mute_mode"]), (True, "native"))
        self.assertEqual(self.fake.volume, 40)  # volumet røres ikke
        self.assertTrue(self.sp.native_mute)
        await self.sp.set_mute(False)
        self.assertFalse(self.fake.muted)
        self.assertEqual((self.sp.state["mute"], self.sp.state["mute_mode"]), (False, None))
        self.assertEqual(volume_commands(self.fake), [])
        self.assertEqual(mute_commands(self.fake), [b"MUTE", b"UNMUTE"])

    async def test_remote_control_mute_is_followed(self):
        self.fake.remote_mute(True)
        self.assertTrue(await self.sp._wait_for(lambda s: s["mute"] is True, 2))
        self.assertEqual(self.sp.state["mute_mode"], "native")
        await self.sp.set_mute(False)
        self.assertFalse(self.fake.muted)

    async def test_web_api(self):
        web = WebServer(self.sp, "127.0.0.1", 0)
        await web.start()
        try:
            async def call(body):
                r, w = await asyncio.open_connection("127.0.0.1", web.bound_port)
                data = json.dumps(body).encode()
                w.write(f"POST /api/mute HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
                raw = await r.read()
                w.close()
                head, _, payload = raw.partition(b"\r\n\r\n")
                return int(head.split()[1]), json.loads(payload)

            status, st = await call({"on": True})
            self.assertEqual((status, st["mute"], st["mute_mode"]), (200, True, "native"))
            status, st = await call({"on": False})
            self.assertEqual((status, st["mute"]), (200, False))
            status, st = await call({"on": "ja"})
            self.assertEqual(status, 400)
            status, st = await call({})
            self.assertEqual(status, 400)
        finally:
            await web.stop()


class VolumeFallbackMute(MuteAgainstFake):
    native = False  # høyttaleren svarer ikke på MUTE-kommandoen

    async def test_falls_back_to_volume_and_ramps_back(self):
        self.sp.max_volume_step = 25
        await self.sp.set_mute(True)
        self.assertEqual((self.sp.state["mute"], self.sp.state["mute_mode"]), (True, "volume"))
        self.assertEqual(self.fake.volume, 0)
        self.assertIs(self.sp.native_mute, False)
        await self.sp.set_mute(False)
        self.assertEqual(self.fake.volume, 40)
        self.assertEqual((self.sp.state["mute"], self.sp.state["mute_mode"]), (False, None))
        self.assertEqual(volume_commands(self.fake), [0, 5, 10, 15, 20, 25, 30, 35, 40])
        # én UNMUTE til sikkerhet (MUTE-en ble ikke bekreftet, men kan ha virket);
        # neste gang: rett på volum-reserven (ingen ny MUTE-kommando)
        await self.sp.set_mute(True)
        self.assertEqual(mute_commands(self.fake), [b"MUTE", b"UNMUTE"])
        self.assertEqual(self.fake.volume, 0)

    async def test_ramp_respects_volume_guard(self):
        self.sp.max_volume_step = 3
        await self.sp.set_mute(True)
        await self.sp.set_mute(False)
        self.assertEqual(self.fake.volume, 40)
        ups = volume_commands(self.fake)[1:]
        self.assertEqual(ups[0], 3)
        self.assertTrue(all(b - a <= 3 for a, b in zip([0] + ups, ups)), ups)

    async def test_volume_change_elsewhere_clears_mute(self):
        await self.sp.set_mute(True)
        self.assertEqual(self.sp.state["mute_mode"], "volume")
        self.fake.remote_volume(12)  # fjernkontrollen/TV-en skrur opp
        self.assertTrue(await self.sp._wait_for(lambda s: s["mute"] is False, 2), self.sp.state)
        self.assertIsNone(self.sp.state["mute_mode"])
        await self.sp.set_mute(False)  # ingenting å oppheve: volumet blir der det er
        self.assertEqual(self.fake.volume, 12)

    async def test_volume_up_in_app_clears_mute(self):
        await self.sp.set_mute(True)
        await self.sp.set_volume(2)
        self.assertEqual((self.sp.state["mute"], self.sp.state["mute_mode"]), (False, None))

    async def test_mute_without_known_volume_is_refused(self):
        await asyncio.sleep(1.5)  # la de første lesingene (som også gir volumet) bli ferdige
        self.sp.native_mute = False
        self.sp.state["volume"] = None
        with self.assertRaises(CommandError):
            await self.sp.set_mute(True)
        self.assertIsNot(self.sp.state["mute"], True)


class SilentNativeMute(MuteAgainstFake):
    """Høyttaleren demper på MUTE, men melder det ikke i boks 0x3F (mulig på ekte SM450)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake.mute_echo = False

    async def test_unmute_also_sends_unmute(self):
        self.sp.max_volume_step = 25
        await self.sp.set_mute(True)
        self.assertEqual((self.sp.state["mute"], self.sp.state["mute_mode"]), (True, "volume"))
        self.assertTrue(self.fake.muted)  # dempet likevel, uten melding
        await self.sp.set_mute(False)
        self.assertFalse(self.fake.muted)  # UNMUTE sendt, ellers ville den forbli stum
        self.assertEqual(self.fake.volume, 40)
        self.assertEqual(mute_commands(self.fake), [b"MUTE", b"UNMUTE"])
        # neste gang: rett på volum-reserven, og ingen unødvendig UNMUTE
        await self.sp.set_mute(True)
        await self.sp.set_mute(False)
        self.assertEqual(mute_commands(self.fake), [b"MUTE", b"UNMUTE"])
        self.assertEqual(self.fake.volume, 40)


class PowerBusy(MuteAgainstFake):
    async def test_power_busy_is_published_and_cleared(self):
        seen = []
        self.sp.subscribe(lambda st: seen.append(st.get("power_busy")))
        await self.sp.set_power(False)
        self.assertIn("off", seen)
        self.assertIsNone(self.sp.state["power_busy"])

        async def broken_wake(host, ip):
            raise OSError("ingen DLNA")

        self.sp.waker = broken_wake
        with self.assertRaises(CommandError):
            await self.sp.set_power(True)
        self.assertIn("on", seen)
        self.assertIsNone(self.sp.state["power_busy"])  # fjernet også ved feil

    async def test_power_busy_stays_while_another_call_runs(self):
        gate = asyncio.Event()

        async def slow_wake(host, ip):
            await gate.wait()
            raise OSError("ingen DLNA")

        self.sp.waker = slow_wake
        await self.sp.set_power(False)
        first = asyncio.create_task(self.sp.set_power(True))
        await asyncio.sleep(0.2)
        second = asyncio.create_task(self.sp.set_power(True))
        await asyncio.sleep(0.2)
        self.assertEqual(self.sp.state["power_busy"], "on")
        gate.set()
        await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsNone(self.sp.state["power_busy"])


class SsePing(MuteAgainstFake):
    async def test_ping_is_a_real_event(self):
        web = WebServer(self.sp, "127.0.0.1", 0)
        web.sse_ping = 0.3
        await web.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", web.bound_port)
            w.write(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
            await w.drain()
            buf = b""
            loop = asyncio.get_running_loop()
            end = loop.time() + 3
            while buf.count(b"event: ping\ndata: {}\n\n") < 2 and loop.time() < end:
                buf += await asyncio.wait_for(r.read(65536), 3)
            w.close()
            self.assertIn(b"text/event-stream", buf)
            self.assertGreaterEqual(buf.count(b"event: ping\ndata: {}\n\n"), 2)
            self.assertNotIn(b"\n: ping", buf)  # ikke lenger en kommentar
        finally:
            await web.stop()

    async def test_ping_also_when_state_changes_often(self):
        web = WebServer(self.sp, "127.0.0.1", 0)
        web.sse_ping = 0.4
        await web.start()
        stop = asyncio.Event()

        async def churn():
            i = 0
            while not stop.is_set():
                i += 1
                await self.sp._apply({"churn": i})
                await asyncio.sleep(0.1)

        task = asyncio.create_task(churn())
        try:
            r, w = await asyncio.open_connection("127.0.0.1", web.bound_port)
            w.write(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
            buf = b""
            end = asyncio.get_running_loop().time() + 3
            while b"event: ping" not in buf and asyncio.get_running_loop().time() < end:
                buf += await asyncio.wait_for(r.read(65536), 3)
            w.close()
            self.assertIn(b"event: ping", buf)
        finally:
            stop.set()
            await task
            await web.stop()


if __name__ == "__main__":
    unittest.main()
