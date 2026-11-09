"""Rettinger før 1.0 (RC-testen): S1-S4, B1 og R1, mot FakeSM450.

S1: 08 06 (av/på/slett) sendes aldri til plass 3 og høyere.
S2: volumvakten gjelder alltid fra API-et, og max_volume_step holdes innenfor 2-25.
S3: hele EQ-operasjoner går én om gangen (samtidige «nytt bånd» får ulike plasser).
S4: et låst eller ukjent filter på plass 0-2 overskrives ikke med eksplisitt ix.
B1: etter vekking går høyttaleren tilbake til forrige inngang, uansett rekkefølge på meldingene.
R1: ugyldig input gir 400 med feiltekst, aldri en lukket forbindelse.

Runde 2 etter kontroll:
- et bånd på plass 3+ tar ingen av de tre plassene (angre, profiler, kurve og gjenoppretting virker ved siden av det),
- av/på-felt må være ekte JSON-boolske verdier,
- rett etter vekking betyr stillhet fra lydprosessoren ikke «ingen bånd».

Runde 3:
- det samme gjelder ved oppstart og ny tilkobling,
- et låst filter på plass 0-2 kan heller ikke slås av eller slettes via API-et.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lemus_remote import protocol as p
from lemus_remote.config import Config, volume_step
from lemus_remote.device import CommandError, Speaker
from lemus_remote.profiles import ProfileStore, validate_bands
from lemus_remote.web import WebServer

from fake_sm450 import FakeSM450, ev

PARAM = p.encode_band_params(0x07, 1000, 1.0, -3.0)
TUNING = p.encode_band_params(0x07, 2500, 2.0, 1.5)       # «produsentens» bånd på plass 3+
LOCKED_HP6 = bytes.fromhex("01010028ffffffff02")          # 6 dB høypass 40 Hz: type SUMEL ikke kan skrive


def outside(ix: int) -> str:
    return f"bånd {ix + 1} ligger utenfor de tre plassene SUMEL kan bruke, og blir ikke rørt"


def forbidden(frame: bytes) -> str | None:
    """Rammer SUMEL aldri skal sende på 50006."""
    key = (frame[3], frame[4])
    if key == (0x02, 0x20):
        return "02 20"
    if key in ((0x08, 0x01), (0x08, 0x06)) and (len(frame) < 7 or frame[6] >= p.MAX_BANDS):
        return f"{key[0]:02x} {key[1]:02x} til plass {frame[6] if len(frame) > 6 else '?'}"
    if key == (0x08, 0x02) and frame[-1] != 0x02:
        return "08 02 uten 02 til slutt"
    return None


class Rig(unittest.IsolatedAsyncioTestCase):
    """FakeSM450 + Speaker + WebServer. Underklasser setter BANDS."""

    BANDS: dict[int, bytes] = {0: PARAM}

    def make_fake(self) -> FakeSM450:
        return FakeSM450()

    async def asyncSetUp(self):
        self.fake = self.make_fake()
        self.fake.bands = dict(self.BANDS)
        ctl, luci = await self.fake.start()
        self.cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.wake_refresh_delays = (0.3,)
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["luci_connected"] and s["bands_known"]
                                     and len(s["bands"]) == len(self.BANDS) and s["volume"] is not None
                                     and s["sources"], 6)
        self.assertTrue(ok, self.sp.state)
        self.web = WebServer(self.sp, "127.0.0.1", 0, config=self.cfg, profiles=ProfileStore(self.cfg))
        await self.web.start()

    async def asyncTearDown(self):
        await self.web.stop()
        await self.sp.stop()
        await self.fake.stop()
        bad = [(why, f.hex(" ")) for f in self.fake.received if (why := forbidden(f))]
        self.assertEqual(bad, [])
        for f in self.fake.luci_received:
            self.assertIn((f[2], int.from_bytes(f[3:5], "little")), p.LUCI_ALLOWED, f.hex(" "))
        self.assertFalse(self.sp._eq_lock.locked())

    async def call(self, path, body=None, method="POST", raw: bytes | None = None):
        r, w = await asyncio.open_connection("127.0.0.1", self.web.bound_port)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        w.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
        resp = await r.read()
        w.close()
        self.assertTrue(resp, f"forbindelsen ble lukket uten svar ({path} {body!r})")
        head, _, payload = resp.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(payload)

    def eq_writes(self, start: int = 0) -> list[bytes]:
        return [f for f in self.fake.received[start:] if f[3:5] in (b"\x08\x01", b"\x08\x06")]

    def editable(self):
        return sorted((b["ix"], b["type_code"], b["freq"], b["gain"]) for b in self.sp.state["bands"] if b["editable"])


# ---------------------------------------------------------------- S1


class SlotGuardInProtocol(unittest.TestCase):
    def test_band_state_only_for_slots_0_to_2(self):
        for ix in range(3):
            for state in (p.BAND_DELETE, p.BAND_OFF, p.BAND_ON):
                self.assertEqual(p.set_band_state(ix, state)[3:7], bytes([0x08, 0x06, 0x01, ix]))
        for ix in (-1, 3, 4, 9, 10, 255):
            for state in (p.BAND_DELETE, p.BAND_OFF, p.BAND_ON):
                with self.assertRaises(ValueError):
                    p.set_band_state(ix, state)
            with self.assertRaises(ValueError):
                p.delete_band(ix)
        with self.assertRaises(ValueError):
            p.set_band_state(0, 3)


class ManufacturerSlotsAreNotTouched(Rig):
    BANDS = {0: PARAM, 3: TUNING, 4: TUNING, 7: TUNING, 9: TUNING}

    async def test_device_refuses_slots_3_and_up(self):
        start = len(self.fake.received)
        for ix in (3, 4, 7, 9, 5):
            with self.assertRaises(CommandError) as cm:
                await self.sp.delete_band(ix)
            self.assertEqual(str(cm.exception), outside(ix))
            for on in (False, True):
                with self.assertRaises(CommandError) as cm:
                    await self.sp.set_band_enabled(ix, on)
                self.assertEqual(str(cm.exception), outside(ix))
            with self.assertRaises(CommandError) as cm:
                await self.sp.write_band(ix, 7, 1000, 1.0, 0.0)
            self.assertEqual(str(cm.exception), outside(ix))
        with self.assertRaises(CommandError) as cm:
            await self.sp.delete_band(-1)
        self.assertEqual(str(cm.exception), outside(-1))
        self.assertEqual(self.eq_writes(start), [])
        self.assertEqual(sorted(self.fake.bands), [0, 3, 4, 7, 9])
        self.assertTrue(all(not b["editable"] for b in self.sp.state["bands"] if b["ix"] >= 3))

    async def test_api_refuses_slots_3_and_up(self):
        start = len(self.fake.received)
        for path, body in (("/api/band_delete", {"ix": 3}), ("/api/band_enabled", {"ix": 4, "enabled": False}),
                           ("/api/band_enabled", {"ix": 9, "enabled": True}),
                           ("/api/band", {"ix": 3, "freq": 1000, "q": 1, "gain": 1}),
                           ("/api/band", {"ix": 7, "type": 7, "freq": 1000, "q": 1, "gain": 1})):
            status, res = await self.call(path, body)
            self.assertEqual((status, res["error"]), (502, outside(body["ix"])), path)
        self.assertEqual(self.eq_writes(start), [])
        self.assertEqual(self.fake.bands[3], TUNING)

    async def test_slots_0_to_2_still_work(self):
        await self.sp.set_band_enabled(0, False)
        self.assertFalse(self.sp._band(0)["enabled"])
        await self.sp.set_band_enabled(0, True)
        await self.sp.delete_band(0)
        self.assertNotIn(0, self.fake.bands)
        self.assertEqual(sorted(self.fake.bands), [3, 4, 7, 9])


# ---------------------------------------------------------------- S2


class VolumeGuard(Rig):
    async def test_force_from_client_is_ignored(self):
        self.assertEqual(self.sp.state["volume"], 13)
        for body in ({"level": 90, "force": True}, {"level": 100, "force": 1}, {"level": 60, "force": "ja"}):
            status, res = await self.call("/api/volume", body)
            self.assertEqual(status, 502, res)
            self.assertIn("volumhopp", res["error"])
        self.assertEqual(self.fake.volume, 13)
        status, _ = await self.call("/api/volume", {"level": 20, "force": True})  # innenfor vakten: virker
        self.assertEqual((status, self.fake.volume), (200, 20))

    async def test_max_volume_step_is_clamped(self):
        for sent, want in ((100, 25), (26, 25), (1, 2), (0, 2), (-5, 2), (10, 10), (25, 25), (2, 2)):
            status, res = await self.call("/api/settings", {"max_volume_step": sent})
            self.assertEqual(status, 200, res)
            self.assertEqual((self.sp.max_volume_step, self.cfg["max_volume_step"],
                              res["settings"]["max_volume_step"]), (want, want, want), sent)
        status, _ = await self.call("/api/settings", {"max_volume_step": "mye"})
        self.assertEqual(status, 400)
        status, _ = await self.call("/api/settings", {"max_volume_step": None})
        self.assertEqual(status, 400)
        # vakten står nå på 2: et hopp fra 13 til 100 sendes ikke
        status, _ = await self.call("/api/volume", {"level": 100})
        self.assertEqual(status, 502)
        self.assertEqual(self.fake.volume, 13)


class VolumeStepFromConfig(unittest.TestCase):
    def test_clamped_at_startup(self):
        for stored, want in ((500, 25), (100, 25), (25, 25), (10, 10), (2, 2), (1, 2), (0, 2), (-3, 2),
                             (None, 25), ("12", 12), ("mye", 25), ([5], 25), (7.9, 7), (float("inf"), 25)):
            self.assertEqual(volume_step(stored), want, stored)

    def test_value_read_from_config_json(self):
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps({"max_volume_step": 100, "access_code": "123456"}), encoding="utf-8")
        cfg = Config(path)
        self.assertEqual(volume_step(cfg["max_volume_step"]), 25)
        sp = Speaker("127.0.0.1", max_volume_step=volume_step(cfg["max_volume_step"]))
        self.assertEqual(sp.max_volume_step, 25)

    def test_main_uses_the_clamp(self):
        import inspect
        from lemus_remote import __main__ as main
        self.assertIn('max_volume_step=volume_step(cfg["max_volume_step"])', inspect.getsource(main))


# ---------------------------------------------------------------- S3


class ConcurrentEq(Rig):
    async def test_ten_new_bands_at_once_get_different_slots(self):
        start = len(self.fake.received)
        results = await asyncio.gather(*(self.call("/api/band", {"type": 7, "freq": 200 + 100 * i, "q": 1, "gain": 1})
                                          for i in range(10)))
        ok = [res for status, res in results if status == 200]
        refused = [res["error"] for status, res in results if status != 200]
        self.assertEqual(len(ok), 2, results)
        self.assertEqual(len(refused), 8, results)
        self.assertTrue(all("høyst 3" in e for e in refused), refused)
        writes = [f for f in self.eq_writes(start) if f[3:5] == b"\x08\x01"]
        self.assertEqual(sorted(f[6] for f in writes), [1, 2])   # én skriving per plass, ingen dobbeltskriving
        self.assertEqual(sorted(self.fake.bands), [0, 1, 2])
        freqs = {self.sp._band(1)["freq"], self.sp._band(2)["freq"]}
        self.assertEqual(len(freqs), 2)
        self.assertEqual(self.sp._band(0)["freq"], 1000)       # det første båndet er urørt

    async def test_direct_add_band_at_once(self):
        results = await asyncio.gather(*(self.sp.add_band(300 + 200 * i, 1.0, 1.0) for i in range(3)),
                                       return_exceptions=True)
        slots = [r for r in results if isinstance(r, int)]
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(sorted(slots), [1, 2])
        self.assertEqual(len(errors), 1)
        self.assertTrue(all(isinstance(e, CommandError) for e in errors), errors)

    async def test_two_profiles_at_once_are_not_mixed(self):
        # a (tre bånd) startes først, b (ett bånd) rett etter: b skal gjelde helt, uten rester av a
        a = [{"freq": 100, "q": 1.0, "gain": 2.0}, {"freq": 500, "q": 1.0, "gain": -2.0}, {"freq": 4000, "q": 1.0, "gain": 1.0}]
        b = [{"freq": 7000, "q": 2.0, "gain": -1.0}]
        await asyncio.gather(self.sp.apply_bands(a), self.sp.apply_bands(b))
        self.assertEqual([(t, f, g) for _, t, f, g in self.editable()], [(7, 7000, -1.0)])
        self.assertEqual(sorted(self.fake.bands), [0])

    async def test_ab_through_api_is_serialised(self):
        status, res = await self.call("/api/profiles/save", {"name": "B", "bands": [{"type": 6, "freq": 200, "gain": 3.0}]})
        self.assertEqual(status, 200, res)
        pid = res["profile"]["id"]
        (s1, _), (s2, r2) = await asyncio.gather(
            self.call("/api/bands_apply", {"bands": [{"freq": 150, "q": 1, "gain": 4}, {"freq": 3000, "q": 1, "gain": 2}]}),
            self.call("/api/profiles/apply", {"id": pid}))
        self.assertEqual((s1, s2), (200, 200), r2)
        final = self.editable()
        self.assertIn([(t, f, g) for _, t, f, g in final],
                      ([(6, 200, 3.0)], [(7, 150, 4.0), (7, 3000, 2.0)]))

    async def test_no_deadlock_with_wake_refresh(self):
        self.sp.wake_refresh_delays = (0.05, 0.05)
        self.sp._schedule_wake_refresh()
        await asyncio.wait_for(self.sp.apply_bands([{"freq": 250, "q": 1.0, "gain": 1.0},
                                                    {"freq": 2500, "q": 1.0, "gain": -1.0}]), 30)
        await asyncio.wait_for(self.sp._wake_task, 30)
        self.assertEqual(sorted(f for _, _, f, _ in self.editable()), [250, 2500])
        # og låsen slippes også etter en feil
        with self.assertRaises(CommandError):
            await self.sp.write_band(2, 7, 1000, 1.0, 0.0)   # plass 2 finnes ikke
        self.assertFalse(self.sp._eq_lock.locked())
        await asyncio.wait_for(self.sp.set_band_enabled(0, False), 10)


# ---------------------------------------------------------------- S4


class LockedFilterIsNotOverwritten(Rig):
    BANDS = {0: LOCKED_HP6, 1: PARAM}

    async def test_explicit_ix_on_locked_band(self):
        start = len(self.fake.received)
        self.assertFalse(self.sp._band(0)["editable"])
        for body in ({"ix": 0, "type": 7, "freq": 1000, "q": 1, "gain": 2},
                     {"ix": 0, "type": 6, "freq": 200, "gain": 2},
                     {"ix": 0, "freq": 60}):
            status, res = await self.call("/api/band", body)
            self.assertEqual((status, res["error"]), (502, "bånd 1 kan ikke endres i SUMEL"), body)
        with self.assertRaises(CommandError) as cm:
            await self.sp.write_band(0, 7, 1000, 1.0, 2.0)
        self.assertEqual(str(cm.exception), "bånd 1 kan ikke endres i SUMEL")
        self.assertEqual([f for f in self.eq_writes(start) if f[3:5] == b"\x08\x01"], [])
        self.assertEqual(self.fake.bands[0], LOCKED_HP6)
        status, res = await self.call("/api/band", {"ix": 2, "type": 7, "freq": 1000, "q": 1, "gain": 2})
        self.assertEqual((status, res["error"]), (502, "bånd 3 finnes ikke"))

    async def test_locked_band_is_not_switched_off_or_deleted(self):
        start = len(self.fake.received)
        for path, body in (("/api/band_enabled", {"ix": 0, "enabled": False}),
                           ("/api/band_enabled", {"ix": 0, "enabled": True}),
                           ("/api/band_delete", {"ix": 0})):
            status, res = await self.call(path, body)
            self.assertEqual((status, res["error"]), (502, "bånd 1 kan ikke endres i SUMEL"), (path, body))
        for op in (self.sp.set_band_enabled(0, False), self.sp.delete_band(0)):
            with self.assertRaises(CommandError):
                await op
        self.assertEqual(self.eq_writes(start), [])
        self.assertEqual(self.fake.bands[0], LOCKED_HP6)
        # et skrivbart bånd ved siden av kan fortsatt slås av og slettes
        status, res = await self.call("/api/band_enabled", {"ix": 1, "enabled": False})
        self.assertEqual(status, 200, res)
        self.assertIs(self.sp._band(1)["enabled"], False)
        status, res = await self.call("/api/band_delete", {"ix": 1})
        self.assertEqual(status, 200, res)
        self.assertEqual(sorted(self.fake.bands), [0])

    async def test_editable_band_and_new_band_still_work(self):
        status, res = await self.call("/api/band", {"ix": 1, "freq": 1200, "q": 1.0, "gain": 2.0})
        self.assertEqual(status, 200, res)
        self.assertEqual(self.sp._band(1)["freq"], 1200)
        status, res = await self.call("/api/band", {"type": 7, "freq": 5000, "q": 1.0, "gain": -1.0})
        self.assertEqual(status, 200, res)
        self.assertEqual(self.sp._band(2)["freq"], 5000)     # ny plass, ikke den låste
        self.assertEqual(self.fake.bands[0], LOCKED_HP6)


class NewBandNeedsKnownEq(Rig):
    async def test_new_band_is_refused_while_eq_is_unknown(self):
        self.fake.power = False
        self.fake.dsp_answers_in_standby = False
        await self.sp._apply({"power": False, "bands_known": False, "bands": []})
        start = len(self.fake.received)
        with self.assertRaises(CommandError) as cm:
            await self.sp.add_band(500, 1.0, 1.0)
        self.assertIn("ikke lest ennå", str(cm.exception))
        self.assertEqual([f for f in self.eq_writes(start) if f[3:5] == b"\x08\x01"], [])
        self.assertEqual(self.fake.bands[0], PARAM)


# ---------------------------------------------------------------- B1


class WakeFake(FakeSM450):
    """Vekking i valgt rekkefølge, slik meldingene kan komme fra en ekte SM450.

    source_first: kildebyttet til Streaming meldes før «på».
    power_first:  «på» meldes før kildebyttet.
    power_only:   bare «på»; kildebyttet meldes ikke av seg selv.
    keeps_input:  høyttaleren våkner på inngangen den hadde (ingen bytte).
    Med ``answer_source_query`` False etter vekking svarer den ikke på 02 01.
    """

    order = "power_first"
    answer_source_query = True

    async def wake(self, host, local_ip):
        await asyncio.sleep(0.2)
        self.power = True
        if self.order != "keeps_input":
            self.source = 4
        on = ev(0x02, 0x0E, b"\x01")
        if self.order == "source_first":
            self._source_events()
            await asyncio.sleep(0.05)
            self._broadcast(on)
        elif self.order == "power_first":
            self._broadcast(on)
            await asyncio.sleep(0.05)
            self._source_events()
        else:
            self._broadcast(on)

    async def _handle(self, f):
        if not self.answer_source_query and (f[3], f[4]) == (0x02, 0x01):
            return
        await super()._handle(f)


class WakeReturnsToPreviousInput(Rig):
    def make_fake(self):
        return WakeFake()

    async def _standby_from(self, source: int):
        await self.sp.select_source(source)
        await self.sp.set_power(False)
        self.assertFalse(self.fake.power)
        self.sp.waker = self.fake.wake

    async def _wake_in(self, order: str) -> None:
        self.fake.order = order
        await self._standby_from(3)
        await self.sp.set_power(True)
        self.assertTrue(self.sp.state["power"])
        self.assertEqual((order, self.fake.source, self.sp.state["source"]), (order, 3, 3))
        self.assertIsNone(self.sp.state["power_busy"])

    async def test_source_first(self):
        await self._wake_in("source_first")

    async def test_power_first(self):
        await self._wake_in("power_first")

    async def test_power_only(self):
        await self._wake_in("power_only")

    async def test_no_fresh_source_message_still_selects_previous(self):
        self.sp.wake_source_wait = 0.6
        self.fake.order = "power_only"
        await self._standby_from(2)
        before = self.fake.received.count(p.select_source(2))
        orig = self.fake.wake

        async def wake_silent(host, ip):
            self.fake.answer_source_query = False   # svarer ikke på 02 01 etter vekking
            await orig(host, ip)

        self.sp.waker = wake_silent
        await self.sp.set_power(True)
        await asyncio.sleep(0.3)   # kilden i tilstanden var allerede 2 (gammel): la høyttaleren behandle byttet
        self.assertEqual(self.fake.received.count(p.select_source(2)), before + 1)
        self.assertEqual(self.fake.source, 2)

    async def test_no_change_when_speaker_kept_the_input(self):
        self.fake.order = "keeps_input"
        await self._standby_from(3)
        before = self.fake.received.count(p.select_source(3))
        await self.sp.set_power(True)
        self.assertEqual(self.fake.received.count(p.select_source(3)), before)   # ingen unødvendig bytte
        self.assertEqual((self.fake.source, self.sp.state["source"]), (3, 3))


# ---------------------------------------------------------------- R1


class ValidateBands(unittest.TestCase):
    def test_every_band_must_be_an_object(self):
        for bands in ([None], ["x"], [1], [[1000]], [{"freq": 1000}, None]):
            with self.assertRaises(ValueError) as cm:
                validate_bands(bands)
            self.assertEqual(str(cm.exception), "hvert bånd må være et objekt")
        self.assertEqual(len(validate_bands([{"freq": 1000, "q": 1, "gain": 1}])), 1)


class BadInputGives400(Rig):
    async def test_slot_must_be_a_real_integer(self):
        start = len(self.fake.received)
        for bad in (True, False, 1.0, 0.5, "1", "0", [1], {"a": 1}):
            for path, body in (("/api/band", {"ix": bad, "freq": 1000, "q": 1, "gain": 1}),
                               ("/api/band_enabled", {"ix": bad, "enabled": False}),
                               ("/api/band_delete", {"ix": bad})):
                status, res = await self.call(path, body)
                self.assertEqual((status, res["error"]), (400, "ugyldig forespørsel: ix må være et heltall"),
                                 (path, bad))
        self.assertEqual(self.eq_writes(start), [])
        self.assertEqual(self.fake.bands, {0: PARAM})
        status, res = await self.call("/api/band_enabled", {"ix": 0, "enabled": False})
        self.assertEqual(status, 200, res)

    async def test_null_and_other_junk_in_lists(self):
        cases = [
            ("/api/bands_apply", {"bands": [None]}),
            ("/api/bands_apply", {"bands": ["x", 1]}),
            ("/api/bands_apply", {"bands": None}),
            ("/api/profiles/save", {"name": "x", "bands": [None]}),
            ("/api/profiles/apply", {"bands": [None]}),
            ("/api/restore", {"profiles": [None]}),
            ("/api/restore", {"profiles": "abc"}),
            ("/api/restore", {"device": "abc", "apply_device": True}),
            ("/api/restore", {"device": {"bands": [None]}, "apply_device": True}),
            ("/api/eq_fit", {"points": [None]}),
            ("/api/band", {"freq": 1e400, "q": 1, "gain": 1}),
            ("/api/band", {"freq": None}),
            ("/api/volume", {"level": 1e400}),
            ("/api/settings", {"max_volume_step": 1e400}),
            ("/api/profiles/save", {"name": "x", "bands": [], "curve": [None]}),
        ]
        start = len(self.fake.received)
        for path, body in cases:
            status, res = await self.call(path, body)
            self.assertEqual(status, 400, (path, body, res))
            self.assertTrue(res["error"].startswith("ugyldig forespørsel: "), res)
        self.assertEqual(self.eq_writes(start), [])
        status, res = await self.call("/api/bands_apply", {"bands": [None]})
        self.assertEqual(res["error"], "ugyldig forespørsel: hvert bånd må være et objekt")

    async def test_broken_json_bodies(self):
        for raw in (b"\xff\xfe{", b'{"ix": "\xc3\x28"}', b'{"ix": 1', b"[" * 50000 + b"]" * 50000, b'{"ix": ' + b"9" * 5000 + b"}"):
            status, res = await self.call("/api/band_delete", raw=raw)
            self.assertEqual((status, res["error"]), (400, "ugyldig JSON"), raw[:20])
        status, res = await self.call("/api/band_delete", raw=b"[1]")
        self.assertEqual((status, res["error"]), (400, "ugyldig JSON"))


class BackupOnlyEditableBands(Rig):
    BANDS = {0: LOCKED_HP6, 1: PARAM, 4: TUNING}

    async def test_restore_with_all_three_slots_in_use(self):
        # oppsettet over pluss ett punkt laget i Punkter (havner på plass 2): kopien kan gjenopprettes
        status, res = await self.call("/api/band", {"type": 7, "freq": 4000, "q": 1.0, "gain": 2.0})
        self.assertEqual(status, 200, res)
        self.assertEqual(sorted(self.fake.bands), [0, 1, 2, 4])
        status, backup = await self.call("/api/backup", method="GET")
        self.assertEqual(len(backup["device"]["bands"]), 2)
        start = len(self.fake.received)
        status, res = await self.call("/api/restore", {"profiles": [], "device": backup["device"], "apply_device": True})
        self.assertEqual(status, 200, res)
        # det låste filteret tar én plass: en profil med tre bånd får ikke plass, med to gjør den det
        status, res = await self.call("/api/bands_apply", {"bands": [{"freq": f, "q": 1, "gain": 1} for f in (100, 1000, 8000)]})
        self.assertEqual(status, 502, res)
        status, res = await self.call("/api/bands_apply", {"bands": [{"freq": f, "q": 1, "gain": 1} for f in (100, 8000)]})
        self.assertEqual(status, 200, res)
        self.assertEqual((self.fake.bands[0], self.fake.bands[4]), (LOCKED_HP6, TUNING))
        self.assertTrue(all(f[6] < 3 for f in self.eq_writes(start)))

    async def test_backup_skips_locked_and_manufacturer_bands(self):
        status, backup = await self.call("/api/backup", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(backup["device"]["bands"],
                         [{"type": 7, "freq": 1000, "q": 1.0, "gain": -3.0, "enabled": True}])
        # og gjenopprettingen av den rører verken det låste filteret eller plass 4
        start = len(self.fake.received)
        status, res = await self.call("/api/restore", {"profiles": [], "device": backup["device"], "apply_device": True})
        self.assertEqual(status, 200, res)
        self.assertEqual((self.fake.bands[0], self.fake.bands[4]), (LOCKED_HP6, TUNING))
        self.assertTrue(all(f[6] < 3 for f in self.eq_writes(start)))



# ---------------------------------------------------------------- runde 2: plass 3+ tar ingen plass


class OutsideBandTakesNoSlot(Rig):
    """Et bånd på plass 3+ (produsentens, eller igjen etter smellet) ligger utenfor SUMELs tre plasser.
    Det røres aldri, men det tar heller ingen av dem: alle veier inn (nytt bånd, angre, profil,
    kurve, gjenoppretting) har tre plasser."""

    BANDS = {0: PARAM, 3: TUNING}

    async def test_three_own_bands_next_to_slot_3(self):
        start = len(self.fake.received)
        for freq in (3000, 6000):
            status, res = await self.call("/api/band", {"type": 7, "freq": freq, "q": 1.0, "gain": 1.0})
            self.assertEqual(status, 200, res)
        self.assertEqual(sorted(self.fake.bands), [0, 1, 2, 3])
        own = [{"type": b["type_code"], "freq": b["freq"], "q": b["q"], "gain": b["gain"], "enabled": True}
               for b in sorted(self.sp.state["bands"], key=lambda b: b["ix"]) if b["editable"]]
        # angre: nøyaktig de båndene som står der nå
        status, res = await self.call("/api/bands_apply", {"bands": own})
        self.assertEqual(status, 200, res)
        # sikkerhetskopien SUMEL nettopp laget
        status, backup = await self.call("/api/backup", method="GET")
        self.assertEqual(len(backup["device"]["bands"]), 3)
        status, res = await self.call("/api/restore", {"profiles": [], "device": backup["device"], "apply_device": True})
        self.assertEqual(status, 200, res)
        # profil med tre bånd (beholder høypass o.l., som i appen)
        status, res = await self.call("/api/profiles/save", {"name": "Tre", "bands": [
            {"type": 6, "freq": 200, "gain": 2.0}, {"type": 7, "freq": 1000, "q": 0.5, "gain": -1.0},
            {"type": 5, "freq": 3500, "gain": 1.5}]})
        self.assertEqual(status, 200, res)
        status, res = await self.call("/api/profiles/apply", {"id": res["profile"]["id"]})
        self.assertEqual(status, 200, res)
        self.assertEqual([(t, f) for _, t, f, _ in self.editable()], [(6, 200), (7, 1000), (5, 3500)])
        # men fortsatt aldri mer enn tre egne
        status, res = await self.call("/api/band", {"type": 7, "freq": 500, "q": 1.0, "gain": 1.0})
        self.assertEqual(status, 502, res)
        self.assertEqual(self.fake.bands[3], TUNING)
        self.assertTrue(all(f[6] < 3 for f in self.eq_writes(start)))

    async def test_curve_fit_has_three_slots(self):
        status, res = await self.call("/api/eq_fit", {"points": [{"freq": 100, "gain": 4}, {"freq": 1000, "gain": -3},
                                                                 {"freq": 8000, "gain": 3}]})
        self.assertEqual(status, 200, res)
        self.assertEqual((res["max_bands"], res["kept"]), (3, []))
        self.assertEqual(res["base_unknown"], 0)   # formen på båndet på plass 4 er kjent og regnes med

    async def test_only_outside_bands(self):
        # plass 0-2 tomme, tre bånd på plass 3-5: alle tre plassene er ledige
        self.fake.bands = {3: TUNING, 4: TUNING, 5: TUNING}
        await self.sp.refresh_bands()
        status, res = await self.call("/api/eq_fit", {"points": [{"freq": 100, "gain": 4}]})
        self.assertEqual(res["max_bands"], 3)
        start = len(self.fake.received)
        status, res = await self.call("/api/bands_apply", {"bands": [{"freq": f, "q": 1, "gain": 1} for f in (100, 1000, 8000)]})
        self.assertEqual(status, 200, res)
        self.assertEqual(sorted(self.fake.bands), [0, 1, 2, 3, 4, 5])
        self.assertEqual([self.fake.bands[i] for i in (3, 4, 5)], [TUNING] * 3)
        self.assertTrue(all(f[6] < 3 for f in self.eq_writes(start)))


# ---------------------------------------------------------------- runde 2: av/på må være true/false


class OnOffMustBeBoolean(Rig):
    async def test_strings_null_and_numbers_give_400(self):
        before = (self.fake.power, self.fake.manual_eq, self.fake.room, self.fake.muted, dict(self.fake.bands))
        start = len(self.fake.received)
        for bad in ("false", "true", None, 0, 1, [], {}):
            for path, body, key in (("/api/power", {"on": bad}, "on"), ("/api/manual_eq", {"on": bad}, "on"),
                                    ("/api/room_correction", {"on": bad}, "on"), ("/api/mute", {"on": bad}, "on"),
                                    ("/api/band_enabled", {"ix": 0, "enabled": bad}, "enabled"),
                                    ("/api/band", {"ix": 0, "freq": 900, "q": 1, "gain": 1, "enabled": bad}, "enabled")):
                status, res = await self.call(path, body)
                self.assertEqual((status, res["error"]), (400, f"ugyldig forespørsel: {key} må være true eller false"),
                                 (path, bad))
        for path in ("/api/power", "/api/manual_eq", "/api/room_correction", "/api/mute"):
            status, _ = await self.call(path, {})
            self.assertEqual(status, 400, path)
        self.assertEqual(self.eq_writes(start), [])
        self.assertNotIn(p.TOGGLE_POWER, self.fake.received[start:])
        self.assertEqual((self.fake.power, self.fake.manual_eq, self.fake.room, self.fake.muted, dict(self.fake.bands)), before)
        # ekte boolske verdier, og «enabled» kan fortsatt utelates
        status, res = await self.call("/api/manual_eq", {"on": False})
        self.assertEqual((status, self.fake.manual_eq), (200, False), res)
        status, res = await self.call("/api/band", {"ix": 0, "freq": 900, "q": 1, "gain": 1})
        self.assertEqual(status, 200, res)
        status, res = await self.call("/api/band_enabled", {"ix": 0, "enabled": False})
        self.assertEqual((status, self.sp._band(0)["enabled"]), (200, False), res)


# ---------------------------------------------------------------- runde 2: lydprosessoren våkner sakte


class SlowDspFake(FakeSM450):
    """Etter vekking svarer ikke lydprosessoren på båndlesing (08 04) før ``dsp_ready``."""

    dsp_ready = True

    async def wake(self, host, local_ip):
        self.dsp_ready = False
        self.power, self.source = True, 4
        self._broadcast(ev(0x02, 0x0E, b"\x01"))

    async def _handle(self, f):
        if (f[3], f[4]) == (0x08, 0x04) and not self.dsp_ready:
            return
        await super()._handle(f)


class SilenceAfterWakeIsNotNoBands(Rig):
    BANDS = {0: LOCKED_HP6, 1: PARAM}

    def make_fake(self):
        return SlowDspFake()

    async def _wake_unknown(self):
        """Standby der SUMEL ikke har sett båndene (startet i standby), så vekking."""
        self.fake.power = False
        self.fake.dsp_answers_in_standby = False
        await self.sp._apply({"power": False, "bands_known": False, "bands": []})
        self.sp.waker = self.fake.wake
        self.sp.wake_source_wait = 0.3
        await self.sp.set_power(True)
        self.assertTrue(self.sp.state["power"])

    async def test_locked_filter_is_not_overwritten_right_after_wake(self):
        self.sp.wake_dsp_grace = 30.0
        await self._wake_unknown()
        start = len(self.fake.received)
        with self.assertRaises(CommandError) as cm:
            await self.sp.write_band(None, 7, 2000, 1.0, 2.0)
        self.assertIn("ikke lest ennå", str(cm.exception))
        with self.assertRaises(CommandError):
            await self.sp.apply_bands([{"type": 7, "freq": 2000, "q": 1.0, "gain": 2.0}])
        self.assertFalse(self.sp.state["bands_known"])
        self.assertEqual(self.eq_writes(start), [])
        self.assertEqual(self.fake.bands[0], LOCKED_HP6)
        # lydprosessoren svarer: båndene leses, og det nye båndet havner på den ledige plassen
        self.fake.dsp_ready = True
        ix = await self.sp.write_band(None, 7, 2000, 1.0, 2.0)
        self.assertEqual(ix, 2)
        self.assertEqual((self.fake.bands[0], self.fake.bands[1]), (LOCKED_HP6, PARAM))

    async def test_known_bands_are_kept_while_the_dsp_wakes(self):
        self.sp.wake_dsp_grace = 30.0
        self.sp.waker = self.fake.wake
        self.sp.wake_source_wait = 0.3
        await self.sp.set_power(False)
        self.assertEqual(sorted(b["ix"] for b in self.sp.state["bands"]), [0, 1])
        await self.sp.set_power(True)
        await self.sp.refresh_bands(0.5)
        self.assertTrue(self.sp.state["bands_known"])
        self.assertEqual(sorted(b["ix"] for b in self.sp.state["bands"]), [0, 1])

    async def test_silence_after_the_grace_still_means_no_bands(self):
        self.sp.wake_dsp_grace = 1.5
        self.fake.bands = {}
        await self._wake_unknown()
        await self.sp.refresh_bands(0.2)
        self.assertFalse(self.sp.state["bands_known"])
        await asyncio.sleep(1.6)
        await self.sp.refresh_bands(0.2)
        self.assertTrue(self.sp.state["bands_known"])
        self.assertEqual(self.sp.state["bands"], [])


class SilenceAfterConnectIsNotNoBands(unittest.IsolatedAsyncioTestCase):
    """Samme vekkeperiode når SUMEL starter mens høyttaleren er på, og når forbindelsen kommer
    tilbake (høyttaleren har vært av med bryteren eller i stikkontakten): stillhet er ikke «ingen bånd»."""

    async def asyncSetUp(self):
        self.fake = SlowDspFake()
        self.fake.bands = {0: LOCKED_HP6, 1: PARAM}
        ctl, luci = await self.fake.start()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.wake_dsp_grace = 30.0

    async def asyncTearDown(self):
        await self.sp.stop()
        await self.fake.stop()
        self.assertEqual([(why, f.hex(" ")) for f in self.fake.received if (why := forbidden(f))], [])

    async def connect(self):
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["power"] is True and s["sources"], 6)
        self.assertTrue(ok, self.sp.state)

    def eq_writes(self, start: int) -> list[bytes]:
        return [f for f in self.fake.received[start:] if f[3:5] in (b"\x08\x01", b"\x08\x06")]

    async def test_locked_filter_is_not_overwritten_at_startup(self):
        self.fake.dsp_ready = False
        await self.connect()
        await asyncio.sleep(3.2)   # første båndlesing etter tilkobling har gått ut på tid
        self.assertFalse(self.sp.state["bands_known"])
        start = len(self.fake.received)
        with self.assertRaises(CommandError) as cm:
            await self.sp.write_band(None, 7, 2000, 1.0, 2.0)
        self.assertIn("ikke lest ennå", str(cm.exception))
        with self.assertRaises(CommandError):
            await self.sp.apply_bands([{"type": 6, "freq": 200, "gain": 2.0}])
        self.assertEqual(self.eq_writes(start), [])
        self.assertEqual(self.fake.bands[0], LOCKED_HP6)
        self.fake.dsp_ready = True
        self.assertEqual(await self.sp.write_band(None, 7, 2000, 1.0, 2.0), 2)
        self.assertEqual((self.fake.bands[0], self.fake.bands[1]), (LOCKED_HP6, PARAM))

    async def test_known_bands_are_kept_after_a_new_connection(self):
        await self.connect()
        ok = await self.sp._wait_for(lambda s: s["bands_known"] and len(s["bands"]) == 2, 6)
        self.assertTrue(ok, self.sp.state)
        self.fake.dsp_ready = False
        for w in list(self.fake._clients):
            w.close()
        self.assertTrue(await self.sp._wait_for(lambda s: not s["connected"], 5))
        self.assertTrue(await self.sp._wait_for(lambda s: s["connected"], 10))
        await self.sp.refresh_bands(0.5)
        self.assertTrue(self.sp.state["bands_known"])
        self.assertEqual(sorted(b["ix"] for b in self.sp.state["bands"]), [0, 1])

    async def test_empty_eq_is_read_when_the_grace_is_over(self):
        self.fake.bands = {}
        self.sp.wake_dsp_grace = 5.0
        await self.connect()
        await asyncio.sleep(4.0)   # første lesing (stillhet) er over, men vekkeperioden er ikke det
        self.assertFalse(self.sp.state["bands_known"])
        ok = await self.sp._wait_for(lambda s: s["bands_known"], 6)   # leses igjen av seg selv etter perioden
        self.assertTrue(ok, self.sp.state)
        self.assertEqual(self.sp.state["bands"], [])


if __name__ == "__main__":
    unittest.main()
