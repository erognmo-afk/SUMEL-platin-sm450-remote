"""Buss-lytter: viser alt som går over port 50006, uten å sende noe.

Bruk: ``py -m lemus_remote.busmon --host 192.168.0.6``

Port 50006 er en delt buss. Alle tilkoblede ser både svarene fra høyttaleren
og forespørslene andre klienter (for eksempel Platin-appen) sender. Denne
lytteren lytter, og sender bare én ufarlig lesekommando (romkorreksjon) hvert
30. sekund for å merke om forbindelsen har falt.

Skriv en merknad (for eksempel «lagrer bånd 0, gain +3») og trykk Enter når
som helst. Merknaden legges inn i loggen, så vi ser hvilke rammer som hører
til hvilken handling. Alt lagres i ``busmon-logg.txt``. Avslutt med Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import protocol as p

KNOWN_REQUESTS = {
    (0x02, 0x01): "les kildeliste", (0x02, 0x03): "velg kilde", (0x02, 0x0D): "les strøm",
    (0x02, 0x0F): "strøm veksle", (0x02, 0x20): "les kilde/volum", (0x03, 0x04): "sett manuell EQ",
    (0x03, 0x05): "les manuell EQ", (0x07, 0x01): "sett romkorreksjon", (0x07, 0x02): "les romkorreksjon",
    (0x07, 0x09): "les romkurve", (0x08, 0x01): "SKRIV EQ-BÅND", (0x08, 0x02): "velg/lagre preset",
    (0x08, 0x04): "les EQ-bånd", (0x08, 0x05): "les aktiv preset", (0x08, 0x06): "SLETT EQ-BÅND",
}


def describe(frame: bytes) -> str:
    if len(frame) < 5:
        return "kort ramme"
    key = (frame[3], frame[4])
    if frame[2] != 0:  # klientramme (sjekksum), altså en forespørsel fra en annen klient
        name = KNOWN_REQUESTS.get(key, "ukjent forespørsel")
        return f"KLIENT -> høyttaler: {name}"
    deleted = p.decode_delete_ack(frame)
    if deleted is not None:
        return f"høyttaler: bekrefter sletting av bånd {deleted}"
    band = p.decode_filter(frame)
    if band:
        return f"høyttaler: bånd {band[0]}: {p.describe_band(band[1])}"
    patch = p.decode_control(frame)
    if patch:
        if "sources" in patch:
            patch = {"source": patch["source"]}
        return f"høyttaler: {patch}"
    return f"høyttaler: ({key[0]:02x} {key[1]:02x})"


async def main() -> None:
    ap = argparse.ArgumentParser(prog="lemus_remote.busmon")
    ap.add_argument("--host", default="192.168.0.6")
    ap.add_argument("--file", default="busmon-logg.txt")
    ap.add_argument("--luci", action="store_true",
                    help="lytt også på port 7777. Sender bare registreringen (samme som fjernkontrollen)")
    ap.add_argument("--show-poll", dest="quiet_poll", action="store_false",
                    help="vis også svarene på holde-liv-lesingen av romkorreksjon")
    args = ap.parse_args()

    path = Path(args.file)
    fh = path.open("a", encoding="utf-8")

    def emit(line: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        text = f"{stamp}  {line}"
        print(text)
        fh.write(text + "\n")
        fh.flush()

    print("Lytter på høyttaleren. Sender bare en ufarlig lesing hvert 30. sekund. Skriv en merknad og trykk Enter når som helst.")
    print(f"Logger til {path.resolve()}  (avslutt med Ctrl+C)\n")
    emit("=== start ===")

    loop = asyncio.get_running_loop()

    def read_notes() -> None:  # egen tråd som ikke hindrer avslutning med Ctrl+C
        for line in sys.stdin:
            if line.strip():
                loop.call_soon_threadsafe(emit, f">>> MERKNAD: {line.strip()}")

    threading.Thread(target=read_notes, daemon=True).start()

    async def connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        while True:
            try:
                return await asyncio.wait_for(asyncio.open_connection(args.host, port), 10)
            except (OSError, asyncio.TimeoutError) as exc:
                emit(f"=== {port}: kunne ikke koble til ({exc}), prøver igjen om 3 s ===")
                await asyncio.sleep(3)

    async def keepalive(writer: asyncio.StreamWriter, frame: bytes) -> None:
        while True:
            await asyncio.sleep(30)
            writer.write(frame)
            await writer.drain()

    async def control() -> None:
        # Hvert 30. sekund sendes én ufarlig lesing (romkorreksjon). Uten den merker en
        # forbindelse som bare lytter, aldri at høyttaleren har forsvunnet.
        while True:
            reader, writer = await connect(50006)
            emit("=== 50006: tilkoblet, lytter ===")
            frames = p.FrameReader()
            ka = asyncio.create_task(keepalive(writer, p.GET_ROOM_CORRECTION))
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(reader.read(4096), 45)
                    except asyncio.TimeoutError:
                        emit("=== 50006: ingen svar på 45 s, kobler til igjen ===")
                        break
                    if not data:
                        emit("=== 50006: høyttaleren lukket forbindelsen, kobler til igjen ===")
                        break
                    for frame in frames.feed(data):
                        if args.quiet_poll and frame[2] == 0 and frame[3:5] == b"\x07\x03":
                            continue
                        emit(f"{describe(frame)}\n              {frame.hex(' ')}")
            except OSError as exc:
                emit(f"=== 50006: forbindelsen brutt ({exc}), kobler til igjen ===")
            finally:
                ka.cancel()
                writer.close()
            await asyncio.sleep(2)

    async def luci() -> None:
        while True:
            r7, w7 = await connect(7777)
            ka7 = None
            try:
                w7.write(p.luci_register(w7.get_extra_info("sockname")[0]))
                await w7.drain()
                emit("=== 7777: registrert, lytter ===")
                lr = p.LuciReader()
                ka7 = asyncio.create_task(keepalive(w7, p.LUCI_GET_VOLUME))
                while True:
                    try:
                        data = await asyncio.wait_for(r7.read(4096), 45)
                    except asyncio.TimeoutError:
                        emit("=== 7777: ingen svar på 45 s, kobler til igjen ===")
                        break
                    if not data:
                        break
                    for box, status, payload in lr.feed(data):
                        if args.quiet_poll and box == p.BOX_VOLUME and status == 1:
                            continue
                        if box == 0x0070:  # tunnel: 50006-kommandoer fra andre klienter
                            inner = list(p.FrameReader().feed(payload)) or [payload]
                            for f in inner:
                                emit(f"7777 tunnel: {describe(f)}\n              {f.hex(' ')}")
                        else:
                            text = payload.decode("utf-8", "replace")
                            short = text if len(text) <= 120 else text[:120] + "..."
                            emit(f"7777 boks 0x{box:04x} status {status}: {short!r}")
                emit("=== 7777: forbindelsen avsluttet, kobler til igjen ===")
            except OSError as exc:
                emit(f"=== 7777: forbindelsen brutt ({exc}), kobler til igjen ===")
            finally:
                if ka7:
                    ka7.cancel()
                w7.close()
            await asyncio.sleep(2)

    tasks = [asyncio.create_task(control())]
    if args.luci:
        tasks.append(asyncio.create_task(luci()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        fh.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAvsluttet. Send meg innholdet i busmon-logg.txt.")
