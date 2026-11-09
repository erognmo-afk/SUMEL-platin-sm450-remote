"""Første test av å skrive et EQ-bånd fra PC.

Bruk: ``py -m lemus_remote.eqskrivetest --host 192.168.0.6``
      ``py -m lemus_remote.eqskrivetest --host 192.168.0.6 --lag-og-slett``

1. Leser bånd 0. Testen krever at bånd 0 er Parametric EQ og står på.
2. Skriver bånd 0 med samme frekvens og Q, men med 2 dB lavere gain.
3. Leser tilbake og sjekker at høyttaleren har lagret den nye verdien.
4. Skriver de opprinnelige verdiene tilbake og sjekker igjen.
   Det skjer også hvis du avbryter med Ctrl+C.

Skrivekommandoen er laget slik Platin-appen gjør det, og er bekreftet
bit for bit mot to opptak (protocol.write_parametric_band).
"""

from __future__ import annotations

import argparse
import asyncio

from . import protocol as p


class Conn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.frames = p.FrameReader()
        self.bands: dict[int, bytes] = {}
        self.deleted: list[int] = []
        self.task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        while data := await self.reader.read(4096):
            for frame in self.frames.feed(data):
                hit = p.decode_filter(frame)
                if hit:
                    self.bands[hit[0]] = hit[1]
                gone = p.decode_delete_ack(frame)
                if gone is not None:
                    self.deleted.append(gone)

    async def read_all(self) -> dict[int, dict]:
        """Les alle bånd i preset 0. Tom liste gir ikke noe svar, så vi venter litt."""
        for _ in range(2):
            self.bands.clear()
            await self.send(p.get_filters(0))
            await asyncio.sleep(2.5)
            if self.bands:
                break
        return {ix: p.parse_band(raw) for ix, raw in sorted(self.bands.items())}

    async def send(self, frame: bytes) -> None:
        self.writer.write(frame)
        await self.writer.drain()

    async def read_band0(self) -> dict | None:
        for _ in range(3):
            self.bands.clear()
            await self.send(p.get_filters(0))
            for _ in range(10):
                await asyncio.sleep(0.5)
                if 0 in self.bands:
                    await asyncio.sleep(0.5)
                    return p.parse_band(self.bands[0])
        return None


async def create_delete_test(c: Conn) -> None:
    """Lag et nytt bånd på neste ledige plass, sjekk det, og slett det igjen."""
    bands = await c.read_all()
    for ix, b in bands.items():
        print(f"  bånd {ix}: {p.describe_band(c.bands[ix])}")
    if not bands:
        print("  (ingen bånd)")
    index = max(bands) + 1 if bands else 0  # plassene er faste, så bruk plassen etter den høyeste
    if index > 9:
        print("Ingen ledig plass. Ingenting er skrevet.")
        return
    f, q, g = 3000, 1.0, -2.0
    answer = await asyncio.to_thread(
        input, f"\nLage nytt bånd {index}: Parametric {f} Hz, Q {q:.2f}, {g:+.2f} dB? Skriv j og trykk Enter: ")
    if answer.strip().lower() != "j":
        print("Avbrutt. Ingenting er skrevet.")
        return
    created = False
    try:
        print(f"\n--> lager bånd {index}")
        await c.send(p.write_parametric_band(index, f, q, g))
        created = True
        await asyncio.sleep(1.0)
        bands = await c.read_all()
        new = bands.get(index)
        ok1 = bool(new) and new["freq"] == f and new["gain"] == g
        print(f"  høyttaleren rapporterer: {p.describe_band(c.bands[index]) if new else '(fant ikke båndet)'}")
        print(f"Opprettet og bekreftet: {ok1}")
        await asyncio.to_thread(input, "\nSe gjerne i Platin-appen. Trykk Enter for å slette båndet igjen.")
        print(f"\n--> sletter bånd {index}")
        c.deleted.clear()
        await c.send(p.delete_band(index))
        await asyncio.sleep(1.5)
        acked = index in c.deleted
        bands = await c.read_all()
        ok2 = index not in bands
        created = not ok2
        print(f"Høyttaleren bekreftet sletting: {acked}. Båndet er borte ved tilbakelesing: {ok2}")
        for ix in bands:
            print(f"  bånd {ix}: {p.describe_band(c.bands[ix])}")
        if ok1 and ok2:
            print("\nRESULTAT: lage og slette bånd BESTÅTT")
        else:
            print("\nRESULTAT: ikke bekreftet. Se linjene over.")
    finally:
        if created:
            print(f"Sletter bånd {index} for sikkerhets skyld ...")
            await c.send(p.delete_band(index))
            await asyncio.sleep(1.0)


async def capacity_test(c: Conn) -> None:
    """Finn hvor mange EQ-bånd høyttaleren tar imot. Lager bånd på 0 dB (uhørbart)
    på nye plasser til ett ikke blir lagret, og sletter alle testbåndene etterpå."""
    bands = await c.read_all()
    print(f"Nå: {len(bands)} bånd på plass {sorted(bands)}")
    answer = await asyncio.to_thread(
        input, "Lage testbånd på 0 dB til det stopper, og slette dem igjen? Skriv j og trykk Enter: ")
    if answer.strip().lower() != "j":
        print("Avbrutt. Ingenting er skrevet.")
        return
    created: list[int] = []
    index = max(bands) + 1 if bands else 0
    try:
        while index <= 9:
            freq = 200 + 300 * index
            await c.send(p.write_parametric_band(index, freq, 1.0, 0.0))
            await asyncio.sleep(1.0)
            now = await c.read_all()
            if index in now and now[index]["freq"] == freq:
                created.append(index)
                print(f"  plass {index}: lagret ({len(now)} bånd totalt)")
                index += 1
            else:
                print(f"  plass {index}: IKKE lagret. Høyttaleren tar imot {len(now)} bånd.")
                break
        else:
            print("  alle plasser 0-9 ble lagret")
    finally:
        for ix in reversed(created):
            await c.send(p.delete_band(ix))
            await asyncio.sleep(0.8)
        left = await c.read_all()
        print(f"Testbåndene er slettet. Igjen: {len(left)} bånd på plass {sorted(left)}")
        print(f"\nRESULTAT: maks antall bånd = {len(bands) + len(created)}")


async def main() -> None:
    ap = argparse.ArgumentParser(prog="lemus_remote.eqskrivetest")
    ap.add_argument("--host", default="192.168.0.6")
    ap.add_argument("--kapasitet", dest="capacity", action="store_true",
                    help="finn hvor mange EQ-bånd høyttaleren tar imot (lager og sletter testbånd på 0 dB)")
    ap.add_argument("--lag-og-slett", dest="create_delete", action="store_true",
                    help="test å lage et nytt bånd og slette det igjen (i stedet for å endre bånd 0)")
    args = ap.parse_args()

    for attempt in range(1, 6):
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(args.host, 50006), 10)
            break
        except (OSError, asyncio.TimeoutError):
            print(f"Tilkobling {attempt} feilet, prøver igjen ...")
            await asyncio.sleep(3)
    else:
        print("Fikk ikke koblet til høyttaleren.")
        return
    c = Conn(reader, writer)

    if args.capacity:
        print("Kapasitetstesten er sperret: et fjerde bånd ga smell i høyttalerne 8.11.2026.")
        print("SM450 tar trygt imot 3 bånd. Ingenting er sendt.")
        c.task.cancel()
        writer.close()
        return
    if False:
        try:
            await capacity_test(c)
        finally:
            c.task.cancel()
            writer.close()
        return

    if args.create_delete:
        try:
            await create_delete_test(c)
        finally:
            c.task.cancel()
            writer.close()
        return

    original = await c.read_band0()
    if not original:
        print("Fant ikke bånd 0. Legg til ett Parametric EQ-bånd i Platin-appen og prøv igjen.")
        writer.close()
        return
    print(f"Bånd 0 nå: {p.describe_band(c.bands[0])}")
    if original["type_code"] != 0x07 or not original["enabled"] or original["q"] is None or original["gain"] is None:
        print("Testen krever at bånd 0 er Parametric EQ og står på. Ingenting er skrevet.")
        writer.close()
        return

    f, q, g = original["freq"], original["q"], original["gain"]
    new_gain = max(-15.0, g - 2.0)
    restored = False

    async def write_and_check(gain: float) -> bool:
        await c.send(p.write_parametric_band(0, f, q, gain))
        await asyncio.sleep(1.0)
        band = await c.read_band0()
        if band:
            print(f"  høyttaleren rapporterer: {p.describe_band(c.bands[0])}")
        return bool(band) and band["gain"] == round(gain, 2) and band["freq"] == f

    try:
        answer = await asyncio.to_thread(
            input, f"\nSkrive bånd 0 med gain {new_gain:+.2f} dB (nå {g:+.2f})? Skriv j og trykk Enter: ")
        if answer.strip().lower() != "j":
            print("Avbrutt. Ingenting er skrevet.")
            restored = True
            return
        print(f"\n--> skriver bånd 0: {f} Hz, Q {q:.2f}, gain {new_gain:+.2f} dB")
        ok = await write_and_check(new_gain)
        print(f"Bekreftet av høyttaleren: {ok}")
        await asyncio.to_thread(input, "\nHørte du forskjell? Se gjerne i Platin-appen også. Trykk Enter for å sette tilbake.")
        print(f"\n--> setter tilbake: {f} Hz, Q {q:.2f}, gain {g:+.2f} dB")
        restored = await write_and_check(g)
        print(f"Bekreftet av høyttaleren: {restored}")
        if ok and restored:
            print("\nRESULTAT: EQ-skriving BESTÅTT (endret og satt tilbake, bekreftet av høyttaleren)")
        else:
            print("\nRESULTAT: ikke bekreftet. Se linjene over.")
    finally:
        if not restored:
            print("Setter tilbake opprinnelig verdi ...")
            await c.send(p.write_parametric_band(0, f, q, g))
            await asyncio.sleep(1.0)
        c.task.cancel()
        writer.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAvsluttet.")
