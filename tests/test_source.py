"""Kildebytte (HDMI m.fl.): riktig kommando, bekreftelse fra kildelisten (02 02) og at tilstanden
viser kilden høyttaleren faktisk melder – ikke en utdatert eller antatt kilde.

Bakgrunn (fysisk test, 1.0.0): bytte tilbake til HDMI fra SUMEL mislyktes, SUMEL viste
«Høyttaleren bekreftet ikke kildebytte», og HDMI kunne stå som aktiv selv om høyttaleren
brukte en annen kilde. Fjernkontrollen kunne bytte til HDMI.
"""
import asyncio
import json
import unittest

from lemus_remote import protocol as p
from lemus_remote.device import CommandError, Speaker

from fake_sm450 import FakeSM450, ev

HDMI, AUX, BT, STREAM = 1, 2, 3, 4


class SourceFake(FakeSM450):
    """FakeSM450 med valg for hvordan kildebytter meldes.

    json_delay:   sekunder før kildelisten viser en ny kilde (HDMI-håndtrykk o.l.)
    ignore_select: kilder som 02 03 ikke bytter til
    sts_extra:    kilder som alltid har sts 1 i kildelisten (i tillegg til den aktive)
    """

    def __init__(self) -> None:
        super().__init__()
        self.json_delay = 0.0
        self.ignore_select: set[int] = set()
        self.sts_extra: set[int] = set()
        self._shown = self.source       # kilden kildelisten viser nå

    def _source_events(self) -> None:
        doc = {"src": [{"ix": ix, "name": n, "sts": int(ix == self._shown or ix in self.sts_extra)}
                       for ix, n in self.SOURCES]}
        self._broadcast(ev(0x02, 0x02, json.dumps(doc, separators=(",", ":")).encode()))
        self._broadcast(ev(0x02, 0x21, bytes([self.source, self.volume])))

    async def _handle(self, f: bytes) -> None:
        if self.silent:
            return
        key, data = (f[3], f[4]), f[5:]
        if key == (0x02, 0x03):
            if data[0] in self.ignore_select:
                return
            self.source = data[0]
            if self.json_delay:
                self._broadcast(ev(0x02, 0x21, bytes([self.source, self.volume])))  # bare 02 21 først
                asyncio.get_running_loop().call_later(self.json_delay, self._show_now)
                return
            self._shown = self.source
            self._source_events()
            return
        await super()._handle(f)

    def _show_now(self) -> None:
        self._shown = self.source
        self._source_events()

    def remote_select(self, ix: int) -> None:
        """Fjernkontrollen bytter kilde: høyttaleren melder 02 02 + 02 21 til alle (busmon 9.11.2026)."""
        self.source = self._shown = ix
        self._source_events()


class SourceFrames(unittest.TestCase):
    def test_hdmi_select_frame_is_documented_form(self):
        # 00 04 <05+ix> 02 03 <ix> (PROTOCOL.md / aiolibresync); HDMI har ix 1 i SM450-listen
        self.assertEqual(p.select_source(HDMI).hex(" "), "00 04 06 02 03 01")
        self.assertEqual(p.select_source(STREAM).hex(" "), "00 04 09 02 03 04")

    def _decode(self, sts):
        doc = {"src": [{"ix": ix, "name": n, "sts": sts.get(ix, 0)} for ix, n in SourceFake.SOURCES]}
        return p.decode_control(ev(0x02, 0x02, json.dumps(doc).encode()))

    def test_single_active_source(self):
        self.assertEqual(self._decode({HDMI: 1})["source"], HDMI)
        self.assertEqual(self._decode({BT: 1})["source"], BT)

    def test_ambiguous_or_missing_active_source_is_unknown(self):
        # før ble den første med sts 1 (HDMI) valgt – da kunne HDMI vises som aktiv feilaktig
        self.assertIsNone(self._decode({HDMI: 1, BT: 1})["source"])
        self.assertIsNone(self._decode({})["source"])

    def test_source_code_from_02_21_does_not_set_source(self):
        patch = p.decode_control(ev(0x02, 0x21, bytes([BT, 13])))
        self.assertEqual(patch["source_code"], BT)
        self.assertNotIn("source", patch)


class SourceSwitching(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = SourceFake()
        ctl, luci = await self.fake.start()
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.source_confirm = 3.0
        self.sp.start()
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["source"] == HDMI and s["sources"]
                                     and s.get("source_code") is not None, 5)
        self.assertTrue(ok, self.sp.state)

    async def asyncTearDown(self):
        await self.sp.stop()
        await self.fake.stop()
        for f in self.fake.received:
            self.assertNotIn((f[3], f[4]), {(0x08, 0x01), (0x08, 0x06), (0x08, 0x02), (0x02, 0x20)}, f.hex(" "))

    def _selects(self):
        return [f[5] for f in self.fake.received if (f[3], f[4]) == (0x02, 0x03)]

    async def test_switch_away_and_back_to_hdmi(self):
        await self.sp.select_source(BT)
        self.assertEqual((self.fake.source, self.sp.state["source"]), (BT, BT))
        await self.sp.select_source(HDMI)
        self.assertEqual((self.fake.source, self.sp.state["source"]), (HDMI, HDMI))
        self.assertEqual(self._selects(), [BT, HDMI])

    async def test_hdmi_is_sent_even_when_state_wrongly_shows_hdmi(self):
        # tilstanden er utdatert: viser HDMI, men høyttaleren står på Bluetooth
        self.fake.source = self.fake._shown = BT
        self.sp.state["source"] = HDMI
        await self.sp.select_source(HDMI)
        self.assertEqual(self._selects(), [HDMI])
        self.assertEqual((self.fake.source, self.sp.state["source"]), (HDMI, HDMI))

    async def test_slow_hdmi_confirmation_within_window(self):
        await self.sp.select_source(BT)
        self.fake.json_delay = 2.0   # kildelisten viser HDMI først etter 2 s (gammel grense var 3 s inkl. alt)
        await self.sp.select_source(HDMI)
        self.assertEqual(self.sp.state["source"], HDMI)

    async def test_unconfirmed_switch_shows_actual_source_not_requested(self):
        await self.sp.select_source(BT)
        self.fake.ignore_select = {HDMI}   # høyttaleren bytter ikke
        with self.assertRaises(CommandError) as cm:
            await self.sp.select_source(HDMI)
        self.assertIn("melder Bluetooth", str(cm.exception))
        self.assertEqual(self.sp.state["source"], BT)   # ikke HDMI, som vi ba om

    async def test_remote_change_is_followed(self):
        # som i busmon-opptaket 9.11.2026: fjernkontrollen bytter, høyttaleren melder 02 02 + 02 21 straks
        self.fake.remote_select(BT)
        ok = await self.sp._wait_for(lambda s: s["source"] == BT, 2)
        self.assertTrue(ok, self.sp.state)

    async def test_no_answer_reconnects_and_does_not_claim_new_source(self):
        await self.sp.select_source(BT)
        self.fake.silent = True   # forbindelsen svarer ikke lenger
        with self.assertRaises(CommandError) as cm:
            await self.sp.select_source(HDMI)
        self.assertIn("fikk ikke svar", str(cm.exception))
        self.assertNotEqual(self.sp.state["source"], HDMI)
        self.fake.silent = False
        ok = await self.sp._wait_for(lambda s: not s["connected"], 3)
        self.assertTrue(ok, "skulle koble fra for å koble til på nytt")
        ok = await self.sp._wait_for(lambda s: s["connected"] and s["source"] == BT, 10)
        self.assertTrue(ok, self.sp.state)
        await self.sp.select_source(HDMI)
        self.assertEqual((self.fake.source, self.sp.state["source"]), (HDMI, HDMI))

    async def test_ambiguous_list_does_not_show_hdmi_as_active(self):
        self.fake.sts_extra = {HDMI}   # HDMI har sts 1 i tillegg til den aktive
        self.fake.remote_select(BT)
        ok = await self.sp._wait_for(lambda s: s["source"] is None, 3)
        self.assertTrue(ok, self.sp.state)


if __name__ == "__main__":
    unittest.main()
