"""Dyp standby: styrebrikken svarer ikke på 50006 (lemus.log 9.–11.11.2026). «Slå på» skal da
vekke høyttaleren via DLNA i stedet for å gi opp fordi strømstatus ikke kan leses."""
import asyncio
import unittest

from lemus_remote.device import CommandError, Speaker

from fake_sm450 import FakeSM450


class DeepStandby(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        ctl, luci = await self.fake.start()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.wake_wait = 2.0
        self.sp.wake_reconnect_wait = 6.0
        self.sp.wake_source_wait = 0.3
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["power"] is True, 5)
        self.assertTrue(ok, self.sp.state)
        # høyttaleren sovner helt: svarer ikke lenger
        self.fake.power = False
        self.fake.silent = True
        await self.sp._apply({"power": None})
        self.woke = []

    async def asyncTearDown(self):
        await self.sp.stop()
        await self.fake.stop()

    async def test_switch_on_wakes_even_when_power_is_unknown(self):
        async def wake(host, ip):   # DLNA-vekking: høyttaleren våkner og svarer igjen
            self.woke.append(host)
            self.fake.silent = False
            self.fake.power = True
            self.fake._broadcast(bytes.fromhex("000400020e01"))
        self.sp.waker = wake
        await self.sp.set_power(True)
        self.assertEqual(self.woke, ["127.0.0.1"])
        self.assertTrue(self.sp.state["power"])

    async def test_reconnects_when_old_connection_stays_silent_after_wake(self):
        async def wake(host, ip):   # våkner, men svarer først på en ny forbindelse
            self.woke.append(host)
            self.fake.power = True
            asyncio.get_running_loop().call_later(2.5, setattr, self.fake, "silent", False)
        self.sp.waker = wake
        await self.sp.set_power(True)
        self.assertEqual(self.woke, ["127.0.0.1"])
        self.assertTrue(self.sp.state["power"])

    async def test_switch_off_still_needs_a_known_state(self):
        with self.assertRaises(CommandError):
            await self.sp.set_power(False)
        self.assertEqual(self.woke, [])


if __name__ == "__main__":
    unittest.main()
