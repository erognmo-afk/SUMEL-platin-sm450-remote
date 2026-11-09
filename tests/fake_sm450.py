"""En simulert SM450 for tester.

Svarene er de rammene vår egen høyttaler sendte under lesetesten og
skrivetesten 8. november 2026 (docs/lesetest-resultat.md):
7-byte 02 21, ingen svar på 02 20, kilder 1-4, og romkorreksjon som
aldri meldes av seg selv.
"""

from __future__ import annotations

import asyncio
import json

from lemus_remote import protocol as p
from lemus_remote.discovery import close_server


def ev(cls: int, op: int, data: bytes = b"") -> bytes:
    body = bytes([cls, op]) + data
    return (len(body) + 1).to_bytes(2, "big") + b"\x00" + body


class FakeSM450:
    SOURCES = [(1, "HDMI"), (2, "AUX/OPT"), (3, "Bluetooth"), (4, "Streaming")]

    def __init__(self) -> None:
        self.power = True
        self.source = 1
        self.volume = 13
        self.manual_eq = True
        self.room = True
        self.preset = 0
        self.bands = {0: bytes.fromhex("07010a000100030002")}  # 9 byte uten lengdebyten
        self.received: list[bytes] = []
        self.silent = False
        # Demping i boks 0x3F. native_mute=False: høyttaleren svarer ikke på MUTE/UNMUTE-kommandoen
        # (for å teste reserven med volum 0). Spørringen besvares likevel.
        self.muted = False
        self.native_mute = True
        self.answer_mute_query = True
        # mute_echo=False: kommandoen virker (lyden dempes), men boks 0x3F melder det ikke
        self.mute_echo = True
        self.luci_received: list[bytes] = []
        self._clients: list[asyncio.StreamWriter] = []
        self._luci_clients: list[asyncio.StreamWriter] = []
        # Romkurve slik vår SM450 svarte i lesetesten (143 byte data)
        self.room_curve = bytes.fromhex(
            "000a443b3c3a3a393b383d3d40414446464849403a3a3b3d4a5c83c0f2b87f6d7989786a504a4a513e383c605c43535b47576"
            "16e6562615b5f5a63646464646464646464646464124444484c515153 4f585b6161646563666962 5f67696163636962636a60"
            "5c6569656 95b5e5d6c636358786a4a5d6a566268736865625c605b646565646464646464646464".replace(" ", ""))
        self.ctl_server = None
        self.luci_server = None

    async def start(self) -> tuple[int, int]:
        self.ctl_server = await asyncio.start_server(self._ctl, "127.0.0.1", 0)
        self.luci_server = await asyncio.start_server(self._luci, "127.0.0.1", 0)
        return (self.ctl_server.sockets[0].getsockname()[1],
                self.luci_server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        for w in self._clients + self._luci_clients:
            w.close()
        for s in (self.ctl_server, self.luci_server):
            await close_server(s)

    def _broadcast(self, frame: bytes) -> None:
        for w in list(self._clients):
            w.write(frame)

    def _source_events(self) -> None:
        doc = {"src": [{"ix": ix, "name": n, "sts": int(ix == self.source)} for ix, n in self.SOURCES]}
        self._broadcast(ev(0x02, 0x02, json.dumps(doc, separators=(",", ":")).encode()))
        self._broadcast(ev(0x02, 0x21, bytes([self.source, self.volume])))

    async def _ctl(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._clients.append(writer)
        frames = p.FrameReader()
        try:
            while data := await reader.read(4096):
                for f in frames.feed(data):
                    self.received.append(f)
                    await self._handle(f)
        finally:
            self._clients.remove(writer)

    async def _handle(self, f: bytes) -> None:
        if self.silent:
            return
        key, data = (f[3], f[4]), f[5:]
        if key == (0x02, 0x0D):
            self._broadcast(ev(0x02, 0x0E, bytes([self.power])))
        elif key == (0x02, 0x0F):
            self.power = not self.power
            self._broadcast(ev(0x02, 0x0E, bytes([self.power])) * 2)
        elif key == (0x02, 0x01):
            self._source_events()
        elif key == (0x02, 0x03):
            self.source = data[0]
            self._source_events()
        elif key == (0x02, 0x89):
            self._broadcast(ev(0x02, 0x89, b'{"mcu":"177","ls9":"15262","cast":"1.52.272222"}'))
        elif key == (0x02, 0x20):
            pass  # SM450 svarer ikke på denne
        elif key == (0x03, 0x05):
            self._broadcast(ev(0x03, 0x05, bytes([self.manual_eq])))
        elif key == (0x03, 0x04):
            self.manual_eq = bool(data[0])
            await asyncio.sleep(0.2)
            self._broadcast(ev(0x03, 0x05, bytes([self.manual_eq])))
        elif key == (0x07, 0x09):
            self._broadcast(ev(0x07, 0x0A, self.room_curve))
        elif key == (0x07, 0x02):
            self._broadcast(ev(0x07, 0x03, bytes([self.room])))
        elif key == (0x07, 0x01):
            self.room = bool(data[0])  # meldes aldri av seg selv
        elif key == (0x08, 0x05):
            self._broadcast(ev(0x08, 0x05, bytes([0x02, 0x04, self.preset, 0x01, 0x00, 0x00])))
        elif key == (0x08, 0x04):
            for ix, params in sorted(self.bands.items()):
                self._broadcast(ev(0x08, 0x04, bytes([0x02, ix, 0x09]) + params))
        elif key == (0x08, 0x01):
            assert data[0] == 0x01 and data[2] == 0x1D and len(data) == 32, data.hex(" ")
            self.bands[data[1]] = bytes(data[-9:])
        elif key == (0x08, 0x06):  # båndstatus: 00 slett, 01 av, 02 på
            ix, state = data[1], data[3]
            if state == 0:
                self.bands.pop(ix, None)
            elif ix in self.bands:
                self.bands[ix] = self.bands[ix][:-1] + bytes([state])
            self._broadcast(ev(0x08, 0x06, bytes([0x02, ix, 0x01, state])))
        elif key == (0x08, 0x02):
            assert data[-1] == 0x02, "lagring av preset skal aldri sendes"
            self.preset = data[1]
            self._broadcast(ev(0x08, 0x05, bytes([0x02, 0x04, self.preset, 0x01, 0x01, 0x01])))

    def tunnel(self, frame: bytes) -> None:
        """Simuler at Platin-appen sender en 50006-kommando: speiles i 7777-boks 0x70."""
        ev7 = self._luci_event(0x0070, frame)
        for w in list(self._luci_clients):
            w.write(ev7)

    async def _luci(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._luci_clients.append(writer)
        buf = bytearray()
        while data := await reader.read(4096):
            buf += data
            while len(buf) >= 10:
                length = int.from_bytes(buf[8:10], "little")
                if len(buf) < 10 + length:
                    break
                frame, buf = bytes(buf[: 10 + length]), buf[10 + length :]
                self.luci_received.append(frame)
                box = int.from_bytes(frame[3:5], "little")
                payload = frame[10:]
                if frame[2] == 0x02 and box == p.BOX_VOLUME:
                    self.volume = int(payload)
                    reply = self._luci_event(box, payload)
                    writer.write(reply * 2)
                    self._broadcast(ev(0x02, 0x21, bytes([self.source, self.volume])))
                elif frame[2] == 0x01 and box == p.BOX_VOLUME:
                    writer.write(self._luci_event(box, str(self.volume).encode(), status=1))
                elif frame[2] == 0x02 and box == p.BOX_MUTE:
                    assert payload in (b"MUTE", b"UNMUTE"), payload
                    if self.native_mute:
                        self.muted = payload == b"MUTE"
                        if self.mute_echo:
                            self._mute_event()
                elif frame[2] == 0x01 and box == p.BOX_MUTE:
                    if self.answer_mute_query:
                        writer.write(self._luci_event(box, b"MUTE" if self.muted else b"UNMUTE", status=1))

    def _mute_event(self) -> None:
        ev7 = self._luci_event(p.BOX_MUTE, b"MUTE" if self.muted else b"UNMUTE")
        for w in list(self._luci_clients):
            w.write(ev7)

    def remote_mute(self, on: bool) -> None:
        """Simuler mute-knappen på fjernkontrollen (meldes i boks 0x3F, som i opptaket)."""
        self.muted = on
        self._mute_event()

    def remote_volume(self, level: int) -> None:
        """Simuler at volumet endres med fjernkontrollen eller TV-en."""
        self.volume = level
        for w in list(self._luci_clients):
            w.write(self._luci_event(p.BOX_VOLUME, str(level).encode()))
        self._broadcast(ev(0x02, 0x21, bytes([self.source, self.volume])))

    @staticmethod
    def _luci_event(box: int, payload: bytes, status: int = 0) -> bytes:
        return (b"\x00\x00\x02" + box.to_bytes(2, "big") + bytes([status]) + b"\x00\x00"
                + len(payload).to_bytes(2, "big") + payload)
