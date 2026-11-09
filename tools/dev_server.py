"""Utviklingsserver: SUMEL mot en simulert SM450 (ingen ekte høyttaler).

Bruk:  python tools/dev_server.py [port] [--find-none] [--no-native-mute]      (standard 8090)

«Finn automatisk» finner den simulerte høyttaleren etter 1,5 s. Med ``--find-none``
finner den ingenting, slik som på et nett der verken UPnP eller TCP-søket svarer.

Demping (POST /api/mute) virker med høyttalerens egen demping (boks 0x3F). Med
``--no-native-mute`` svarer den simulerte høyttaleren ikke på MUTE-kommandoen, så appen
bruker reserven (volum 0, og opp igjen i små steg). «Slå på» tar ca. 2 s, så «Vekker …»
(power_busy) kan ses. SSE sender «event: ping» hvert 10. s, og /api/qr.svg?text=… gir QR-koder.

Den simulerte høyttaleren svarer med rammene vår ekte SM450 sendte i
lesetesten: kilder HDMI/AUX/OPT/Bluetooth/Streaming, volum 13, romkorreksjon og
manuell EQ på, tre bånd (to Parametric og én High shelving), og romkurve.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fake_sm450 import FakeSM450  # noqa: E402

from lemus_remote.config import Config  # noqa: E402
from lemus_remote.device import Speaker  # noqa: E402
from lemus_remote.profiles import ProfileStore  # noqa: E402
from lemus_remote.web import WebServer  # noqa: E402


async def main(port: int, find_none: bool = False, native_mute: bool = True) -> None:
    fake = FakeSM450()
    fake.native_mute = native_mute
    fake.bands = {0: bytes.fromhex("070101000100020002"), 1: bytes.fromhex("07010a000100fd0002"),
                  2: bytes.fromhex("050132000100030002")}
    ctl, luci = await fake.start()
    cfg = Config(Path(tempfile.mkdtemp()) / "config.json")
    cfg.data["name"] = "Lemus Home 1500"

    async def wake(host, ip):  # simulert DLNA-vekking (3 s stillhet på den ekte høyttaleren)
        await asyncio.sleep(2)
        fake.power, fake.source = True, 4
        fake._broadcast(bytes.fromhex("000400020e01"))

    sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci, name="Lemus Home 1500")
    sp.waker = wake
    sp.start()
    async def finder():  # simulert søk
        await asyncio.sleep(1.5)
        return None if find_none else {"host": "127.0.0.1", "name": "Lemus Home 1500"}

    web = WebServer(sp, "127.0.0.1", port, config=cfg, profiles=ProfileStore(cfg), require_auth=False,
                    lan_url="http://192.168.0.9:8080", finder=finder)
    await web.start()
    print(f"Simulert SUMEL på http://127.0.0.1:{port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    asyncio.run(main(int(nums[0]) if nums else 8090, "--find-none" in sys.argv,
                     "--no-native-mute" not in sys.argv))
