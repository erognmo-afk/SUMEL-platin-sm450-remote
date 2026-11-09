"""Strømtest: finn ut hva som skal til for å slå høyttaleren PÅ over nettet.

Bruk: ``py -m lemus_remote.stromtest --host 192.168.0.6``

Testen logger alt høyttaleren sender, og spør deg før hvert steg:
1. Leser strømstatus.
2. Ber deg sette høyttaleren i standby med fjernkontrollen. Ingenting sendes da.
3. Sjekker om høyttaleren fortsatt svarer på styringsporten i standby.
4. Prøver «strøm veksle» (00 04 11 02 0f 00), samme kommando som virket for «av».
5. Hvis den fortsatt er av: prøver å velge Streaming-kilden.
Alt lagres i stromtest-logg.txt.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from . import protocol as p
from .busmon import describe


async def main() -> None:
    ap = argparse.ArgumentParser(prog="lemus_remote.stromtest")
    ap.add_argument("--host", default="192.168.0.6")
    args = ap.parse_args()
    log = Path("stromtest-logg.txt").open("a", encoding="utf-8")

    def emit(line: str) -> None:
        text = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {line}"
        print(text)
        log.write(text + "\n")
        log.flush()

    state: dict = {"power": None, "frames": 0}

    async def connect():
        for attempt in range(1, 6):
            try:
                return await asyncio.wait_for(asyncio.open_connection(args.host, 50006), 10)
            except (OSError, asyncio.TimeoutError) as exc:
                emit(f"tilkobling {attempt} feilet ({exc})")
                await asyncio.sleep(3)
        return None

    conn = await connect()
    if not conn:
        emit("Fikk ikke koblet til.")
        return
    reader, writer = conn
    frames = p.FrameReader()

    async def listen():
        try:
            while data := await reader.read(4096):
                for f in frames.feed(data):
                    state["frames"] += 1
                    patch = p.decode_control(f)
                    if patch and "power" in patch:
                        state["power"] = patch["power"]
                    emit(f"{describe(f)}   [{f.hex(' ')}]")
            emit("=== høyttaleren lukket forbindelsen ===")
        except OSError as exc:
            emit(f"=== forbindelsen brutt ({exc}) ===")

    task = asyncio.create_task(listen())

    async def send(frame: bytes, label: str, wait: float = 4.0):
        emit(f"--> {label}: {frame.hex(' ')}")
        try:
            writer.write(frame)
            await writer.drain()
        except OSError as exc:
            emit(f"    kunne ikke sende ({exc})")
        await asyncio.sleep(wait)

    async def ask(text: str) -> str:
        return (await asyncio.to_thread(input, text)).strip().lower()

    emit("=== strømtest start ===")
    await send(p.GET_POWER, "les strøm", 2)
    emit(f"Strøm nå: {state['power']}")
    emit("TIPS: Slå av TV-en eller velg en kilde uten lyd først. Høyttaleren våkner av seg selv")
    emit("      når det kommer signal (for eksempel fra TV-en over HDMI ARC).")
    await ask("\nSett høyttaleren i STANDBY med fjernkontrollen, vent 20 s, og trykk Enter: ")
    await send(p.GET_POWER, "les strøm (i standby)", 3)
    emit(f"Strøm nå: {state['power']}. Forbindelsen lever: {not task.done()}")
    if state["power"] is True:
        emit("RESULTAT: høyttaleren er PÅ igjen (våknet av seg selv). Testen stopper, ingenting er sendt.")
        emit("          Slå av TV-en, eller velg Streaming uten avspilling, og kjør testen igjen.")
        task.cancel()
        writer.close()
        return
    if task.done():
        emit("Forbindelsen falt i standby. Prøver å koble til på nytt ...")
        conn = await connect()
        if not conn:
            emit("RESULTAT: styringsporten svarer ikke i standby.")
            return
        reader, writer = conn
        frames = p.FrameReader()
        task = asyncio.create_task(listen())
        await send(p.GET_POWER, "les strøm (ny forbindelse)", 3)
    if state["power"] is not False:
        emit("Strømstatus er ukjent. Sender ikke 'strøm veksle', for den kunne ha slått av i stedet.")
    elif await ask("\nSende 'strøm veksle' for å slå PÅ? j/n: ") == "j":
        await send(p.TOGGLE_POWER, "strøm veksle", 6)
        emit(f"Strøm nå: {state['power']}")
        if state["power"] is True and await ask("Ble høyttaleren slått på? j/n: ") == "j":
            emit("RESULTAT: strøm veksle slår PÅ fra standby")
            task.cancel()
            return
    if await ask("\nPrøve å velge Streaming-kilden for å vekke den? j/n: ") == "j":
        await send(p.select_source(4), "velg kilde Streaming (ix 4)", 6)
        await send(p.GET_POWER, "les strøm", 2)
        emit(f"Strøm nå: {state['power']}")
        ok = await ask("Ble høyttaleren slått på? j/n: ")
        emit("RESULTAT: kildevalg vekker høyttaleren" if ok == "j" else "RESULTAT: ingen av kommandoene vekket den")
    task.cancel()
    writer.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAvsluttet.")
