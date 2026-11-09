"""Vekk høyttaleren fra standby ved å spille en kort lydfil via DLNA/UPnP.

I standby ignorerer SM450s styrebrikke «strøm veksle» og kildevalg (testet
8.11.2026), men nettmodulen (Libre LS9) er våken og er en vanlig UPnP
MediaRenderer på port 38400. Databladet sier at enheten våkner når det kommer
lydsignal. Vi ber derfor rendereren spille noen sekunder stillhet (eller en
svak tone) fra en liten HTTP-server på PC-en.

UPnP AVTransport er en åpen standard: SetAVTransportURI, Play og Stop.
"""

from __future__ import annotations

import asyncio
import io
import math
import re
import struct
import urllib.request
import wave
from xml.sax.saxutils import escape
from .discovery import close_server

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def make_wav(seconds: float = 3.0, tone_db: float | None = None, rate: int = 44100) -> bytes:
    """Stillhet, eller en svak 440 Hz-tone på tone_db dBFS."""
    n = int(seconds * rate)
    amp = 0 if tone_db is None else int(32767 * 10 ** (tone_db / 20))
    frames = bytearray()
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * 440 * i / rate)) if amp else 0
        frames += struct.pack("<hh", v, v)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def soap_body(action: str, args: dict[str, str]) -> bytes:
    inner = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{AVT}">{inner}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()


def didl(url: str, title: str = "Lemus vekking") -> str:
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="0" parentID="-1" restricted="1"><dc:title>{escape(title)}</dc:title>'
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res protocolInfo="http-get:*:audio/wav:*">{escape(url)}</res></item></DIDL-Lite>'
    )


def avtransport_control_url(description_xml: str, base: str) -> str:
    """Finn controlURL for AVTransport i UPnP-beskrivelsen."""
    for svc in re.findall(r"<service>(.*?)</service>", description_xml, re.S):
        if AVT in svc:
            m = re.search(r"<controlURL>(.*?)</controlURL>", svc)
            if m:
                path = m.group(1).strip()
                return path if path.startswith("http") else base.rstrip("/") + "/" + path.lstrip("/")
    return base.rstrip("/") + "/AVTransport"


def _soap(url: str, action: str, args: dict[str, str], timeout: float = 5.0) -> tuple[int, str]:
    req = urllib.request.Request(url, data=soap_body(action, args), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{AVT}#{action}"',
    })
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:  # aldri via proxy: lokalnett
            return resp.status, resp.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(8192).decode("utf-8", "replace")
    except OSError as exc:
        return 0, str(exc)


class WavServer:
    """Liten HTTP-server på PC-en som serverer én lydfil til høyttaleren."""

    def __init__(self, wav: bytes) -> None:
        self.wav = wav
        self.server: asyncio.base_events.Server | None = None
        self.requests = 0

    async def start(self, host: str = "0.0.0.0", port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, host, port)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            await close_server(self.server)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)).decode("latin-1")
            self.requests += 1
            method = head.split(" ", 1)[0]
            hdr = (f"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nContent-Length: {len(self.wav)}\r\n"
                   "Accept-Ranges: none\r\nConnection: close\r\n\r\n").encode()
            writer.write(hdr if method == "HEAD" else hdr + self.wav)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()


async def wake(speaker_host: str, local_ip: str, seconds: float = 3.0, tone_db: float | None = None,
               upnp_port: int = 38400) -> dict:
    """Spill en kort lydfil via DLNA. Returnerer hva som skjedde (for logging)."""
    base = f"http://{speaker_host}:{upnp_port}"
    desc = await asyncio.to_thread(lambda: _NO_PROXY.open(base + "/description.xml", timeout=5)
                                   .read().decode("utf-8", "replace"))
    control = avtransport_control_url(desc, base)
    srv = WavServer(make_wav(seconds, tone_db))
    port = await srv.start()
    url = f"http://{local_ip}:{port}/vekking.wav"
    try:
        r1 = await asyncio.to_thread(_soap, control, "SetAVTransportURI",
                                     {"InstanceID": "0", "CurrentURI": url, "CurrentURIMetaData": didl(url)})
        r2 = await asyncio.to_thread(_soap, control, "Play", {"InstanceID": "0", "Speed": "1"})
        await asyncio.sleep(seconds + 1.5)
        r3 = await asyncio.to_thread(_soap, control, "Stop", {"InstanceID": "0"})
        return {"control": control, "url": url, "set_uri": r1[0], "play": r2[0], "stop": r3[0],
                "fetched": srv.requests, "set_uri_body": r1[1][:300], "play_body": r2[1][:300]}
    finally:
        await srv.stop()
