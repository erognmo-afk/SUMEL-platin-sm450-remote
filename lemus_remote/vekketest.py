"""Vekketest: kan høyttaleren vekkes fra standby ved å spille en kort lydfil via DLNA?

Bruk: ``py -m lemus_remote.vekketest --host 192.168.0.6``  (legg til ``--tone`` for en svak tone)

1. Leser strømstatus. Testen krever at høyttaleren står i standby.
2. Spiller 3 sekunder stillhet (eller en svak tone) via UPnP/DLNA fra en liten
   HTTP-server på PC-en.
3. Følger strømstatus i 10 sekunder og rapporterer om den våknet.
Alt lagres i vekketest-logg.txt.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from . import protocol as p
from .vekking import wake


async def main() -> None:
    ap = argparse.ArgumentParser(prog="lemus_remote.vekketest")
    ap.add_argument("--host", default="192.168.0.6")
    ap.add_argument("--tone", action="store_true", help="spill en svak 440 Hz-tone (-45 dBFS) i stedet for stillhet")
    args = ap.parse_args()
    log = Path("vekketest-logg.txt").open("a", encoding="utf-8")

    def emit(line: str) -> None:
        text = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {line}"
        print(text)
        log.write(text + "\n")
        log.flush()

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(args.host, 50006), 10)
    except (OSError, asyncio.TimeoutError) as exc:
        emit(f"Fikk ikke koblet til 50006 ({exc})")
        return
    local_ip = writer.get_extra_info("sockname")[0]
    state = {"power": None, "source": None}
    frames = p.FrameReader()

    async def listen():
        while data := await reader.read(4096):
            for f in frames.feed(data):
                patch = p.decode_control(f) or {}
                for k in ("power", "source"):
                    if k in patch:
                        state[k] = patch[k]
                        emit(f"høyttaler: {k} = {patch[k]}")

    task = asyncio.create_task(listen())
    emit("=== vekketest start ===")
    writer.write(p.GET_POWER)
    await writer.drain()
    await asyncio.sleep(1.5)
    if state["power"] is not False:
        emit(f"Strøm er {state['power']}. Sett høyttaleren i standby (TV av) og kjør testen igjen.")
        task.cancel()
        writer.close()
        return
    answer = await asyncio.to_thread(
        input, f"Høyttaleren står i standby. Spille 3 s {'svak tone' if args.tone else 'stillhet'} via DLNA for å vekke den? j/n: ")
    if answer.strip().lower() != "j":
        emit("Avbrutt.")
        task.cancel()
        writer.close()
        return
    try:
        result = await wake(args.host, local_ip, tone_db=-45.0 if args.tone else None)
        emit(f"DLNA: SetAVTransportURI={result['set_uri']} Play={result['play']} Stop={result['stop']}, "
             f"høyttaleren hentet lydfilen {result['fetched']} gang(er)")
        if result["set_uri"] != 200 or result["play"] != 200:
            emit(f"  svar: {result['set_uri_body']!r} / {result['play_body']!r}")
    except Exception as exc:  # noqa: BLE001 - alt skal logges
        emit(f"DLNA feilet: {exc}")
    for _ in range(10):
        writer.write(p.GET_POWER)
        await writer.drain()
        await asyncio.sleep(1.0)
        if state["power"] is True:
            break
    emit(f"RESULTAT: strøm = {state['power']} -> " +
         ("høyttaleren VÅKNET av DLNA-avspilling" if state["power"] is True else "den våknet ikke"))
    task.cancel()
    writer.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAvsluttet.")
