"""Første start med en ny høyttaler (0.10.1): standby ved start, profiler som beholder høypass,
og romkurve uten måling."""
import asyncio
import unittest

from lemus_remote import eqfit
from lemus_remote import protocol as p
from lemus_remote.device import CommandError, Speaker

from fake_sm450 import FakeSM450, ev

HIGHPASS = p.encode_band_params(0x02, 80, 0.71, None)   # 12 dB høypass 80 Hz
PARAM = p.encode_band_params(0x07, 1000, 1.0, -3.0)
KEEP = frozenset(eqfit.FIT_TYPES)


class StandbyAtStart(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        self.fake.power = False
        self.fake.dsp_answers_in_standby = False
        self.fake.bands = {0: PARAM}
        ctl, luci = await self.fake.start()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.wake_refresh_delays = (0.3, 0.6)
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["power"] is False, 5)
        self.assertTrue(ok, self.sp.state)

    async def asyncTearDown(self):
        await self.sp.stop()
        await self.fake.stop()

    async def test_bands_unknown_in_standby_and_read_after_wake(self):
        await asyncio.sleep(3.2)   # første båndlesing har gått ut på tid
        self.assertFalse(self.sp.state["bands_known"])
        self.assertEqual(self.sp.state["bands"], [])
        with self.assertRaises(CommandError):   # skriver aldri over bånd vi ikke har sett
            await self.sp.apply_bands([])
        self.assertIn(0, self.fake.bands)
        # høyttaleren våkner (TV slått på): EQ og romkurve leses på nytt av seg selv
        self.fake.power = True
        self.fake._broadcast(ev(0x02, 0x0E, b"\x01"))
        ok = await self.sp._wait_for(lambda s: s["bands_known"] and len(s["bands"]) == 1, 6)
        self.assertTrue(ok, self.sp.state)
        self.assertEqual(self.sp.state["bands"][0]["freq"], 1000)


class ProfileKeepsHighpass(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        self.fake.bands = {0: HIGHPASS, 1: PARAM}
        ctl, luci = await self.fake.start()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["bands_known"] and len(s["bands"]) == 2, 6)
        self.assertTrue(ok, self.sp.state)

    async def asyncTearDown(self):
        await self.sp.stop()
        await self.fake.stop()

    def _types(self):
        return [(b["ix"], b["type_code"], b["freq"]) for b in sorted(self.sp.state["bands"], key=lambda b: b["ix"])]

    async def test_neutral_keeps_highpass(self):
        await self.sp.apply_bands([], keep_types=KEEP)
        self.assertEqual(self._types(), [(0, 0x02, 80)])

    async def test_warm_fills_free_slots_and_keeps_highpass(self):
        await self.sp.apply_bands([{"type": 6, "freq": 200, "gain": 2.0}, {"type": 5, "freq": 3500, "gain": -1.0}], keep_types=KEEP)
        self.assertEqual(self._types(), [(0, 0x02, 80), (1, 6, 200), (2, 5, 3500)])

    async def test_too_many_with_kept_band_is_refused(self):
        three = [{"type": 7, "freq": f, "q": 1.0, "gain": 1.0} for f in (100, 1000, 5000)]
        with self.assertRaises(CommandError):
            await self.sp.apply_bands(three, keep_types=KEEP)
        self.assertEqual(self._types(), [(0, 0x02, 80), (1, 7, 1000)])

    async def test_profile_with_own_highpass_replaces_all(self):
        # en profil lagret fra Punkter med eget høypass erstatter alle skrivbare bånd (som før)
        await self.sp.apply_bands([{"type": 2, "freq": 60, "q": 0.71}], keep_types=KEEP)
        self.assertEqual(self._types(), [(0, 0x02, 60)])


class EmptyRoomCurve(unittest.TestCase):
    def test_constant_curves_are_not_a_measurement(self):
        self.assertTrue(p.room_curve_is_empty(bytes(140)))
        self.assertTrue(p.room_curve_is_empty(bytes([0, 10] + [100] * 69 + [0x12] + [100] * 68)))
        self.assertFalse(p.room_curve_is_empty(FakeSM450().room_curve))
        self.assertFalse(p.room_curve_is_empty(bytes(2)))   # feil lengde: ukjent format, ikke «tom»


if __name__ == "__main__":
    unittest.main()
