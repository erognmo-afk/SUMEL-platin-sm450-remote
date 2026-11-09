"""EQ-logger: leser EQ-båndene fra høyttaleren hver gang du trykker Enter.

Bruk: ``py -m lemus_remote.eqlog --host 192.168.0.6``

Endre ÉN ting om gangen i Platin-appen (for eksempel bare frekvensen på bånd 1),
skriv hva du endret, og trykk Enter. Loggeren leser båndene og markerer
bytene som har endret seg siden forrige lesing med [klammer]. Alt lagres i
``eq-logg.csv``, så vi kan finne hvilke byte som er frekvens, dB, Q og type.

Den sender bare lesekommandoen ``00 05 0c 08 04 00 00`` (filtre i preset 0).
Den skriver ingenting til høyttaleren.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime
from pathlib import Path

from . import protocol as p


def show(bands: dict[int, bytes], previous: dict[int, bytes]) -> None:
    if not bands:
        print("  (ingen bånd. Preset 0 er tomt, eller svaret kom ikke)")
        return
    for ix in sorted(bands):
        old = previous.get(ix)
        cells = []
        for i, b in enumerate(bands[ix]):
            changed = old is not None and (i >= len(old) or old[i] != b)
            cells.append(f"[{b:02x}]" if changed else f" {b:02x} ")
        print(f"  bånd {ix}: " + "".join(cells) + "   " + p.describe_band(bands[ix]))
    for ix in sorted(set(previous) - set(bands)):
        print(f"  bånd {ix}: (borte)")


async def main() -> None:
    ap = argparse.ArgumentParser(prog="lemus_remote.eqlog")
    ap.add_argument("--host", default="192.168.0.6")
    ap.add_argument("--file", default="eq-logg.csv")
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

    collected: dict[int, bytes] = {}

    async def listen() -> None:
        frames = p.FrameReader()
        while data := await reader.read(4096):
            for frame in frames.feed(data):
                hit = p.decode_filter(frame)
                if hit:
                    collected[hit[0]] = hit[1]

    listener = asyncio.create_task(listen())
    path = Path(args.file)
    new_file = not path.exists()
    previous: dict[int, bytes] = {}
    print("Endre ÉN ting i Platin-appen, trykk Save, skriv hva du endret her og trykk Enter.")
    print(f"\nLogger til {path.resolve()}\n")
    with path.open("a", newline="", encoding="utf-8") as fh:
        out = csv.writer(fh, delimiter=";")
        if new_file:
            out.writerow(["tid", "endring", "bånd", "byte"])
        try:
            while True:
                note = await asyncio.to_thread(
                    input, "Hva endret du i Platin-appen? (Enter = bare les, q = avslutt): ")
                if note.strip().lower() == "q":
                    break
                bands: dict[int, bytes] = {}
                for attempt in range(3):  # SM450 svarer av og til sent eller ikke i det hele tatt
                    collected.clear()
                    writer.write(p.get_filters(0))
                    await writer.drain()
                    for _ in range(12):
                        await asyncio.sleep(0.5)
                        if collected:
                            await asyncio.sleep(1.0)  # vent på resten av båndene
                            break
                    bands = dict(collected)
                    if bands:
                        break
                    print(f"  (ingen svar, prøver igjen {attempt + 1}/2)" if attempt < 2 else "")
                show(bands, previous)
                stamp = datetime.now().isoformat(timespec="seconds")
                for ix in sorted(bands):
                    out.writerow([stamp, note, ix, bands[ix].hex(" ")])
                fh.flush()
                previous = bands
                print()
        finally:
            listener.cancel()
            writer.close()
    print(f"Ferdig. Send meg innholdet i {path.name}.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAvsluttet.")
