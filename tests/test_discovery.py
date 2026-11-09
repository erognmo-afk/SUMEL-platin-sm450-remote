import asyncio
import unittest

from lemus_remote import discovery as d
from lemus_remote.discovery import close_server

DESCRIPTION = open(__file__.replace("test_discovery.py", "fixture_description.xml"), encoding="utf-8").read()


class Parsing(unittest.TestCase):
    def test_description_from_our_speaker(self):
        self.assertEqual(d.parse_description(DESCRIPTION),
                         {"name": "Lemus Home 1500", "udn": "uuid:00000000-0000-0000-0000-000000001500"})

    def test_rejects_other_renderers(self):
        other = DESCRIPTION.replace("LibreWireless", "Sonos").replace("LibreSyncDMR", "Speaker")
        self.assertIsNone(d.parse_description(other))

    def test_ssdp_headers(self):
        h = d.parse_ssdp(b"HTTP/1.1 200 OK\r\nLocation: http://192.168.0.6:38400/description.xml\r\nST: x\r\n\r\n")
        self.assertEqual(h["location"], "http://192.168.0.6:38400/description.xml")


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    """Simulert høyttaler på 127.0.0.1: SSDP-svar, HTTP-beskrivelse og port 50006."""

    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        self.http = await asyncio.start_server(self._http, "127.0.0.1", 0)
        self.ctl = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        http_port = self.http.sockets[0].getsockname()[1]
        location = f"http://127.0.0.1:{http_port}/description.xml"

        class Responder(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.t = transport

            def datagram_received(self, data, addr):
                if data.startswith(b"M-SEARCH"):
                    self.t.sendto(f"HTTP/1.1 200 OK\r\nLOCATION: {location}\r\nST: x\r\n\r\n".encode(), addr)

        self.udp, _ = await loop.create_datagram_endpoint(Responder, local_addr=("127.0.0.1", 0))

    async def asyncTearDown(self):
        self.udp.close()
        for srv in (self.http, self.ctl):
            await close_server(srv)

    async def _http(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        body = DESCRIPTION.encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    async def test_discovers_speaker(self):
        found = await d.discover(timeout=1.5, target=self.udp.get_extra_info("sockname"),
                                 control_port=self.ctl.sockets[0].getsockname()[1])
        self.assertEqual([(f.host, f.name) for f in found], [("127.0.0.1", "Lemus Home 1500")])

    async def test_ignores_speaker_without_control_port(self):
        found = await d.discover(timeout=1.5, target=self.udp.get_extra_info("sockname"), control_port=1)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
