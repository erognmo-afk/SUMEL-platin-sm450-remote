import asyncio
import unittest
import wave
import io
import urllib.request

from lemus_remote import vekking as v


class Parts(unittest.TestCase):
    def test_wav_is_valid_and_silent(self):
        data = v.make_wav(0.5)
        with wave.open(io.BytesIO(data)) as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (2, 44100))
            self.assertEqual(set(w.readframes(100)), {0})

    def test_soap_and_control_url(self):
        body = v.soap_body("Play", {"InstanceID": "0", "Speed": "1"}).decode()
        self.assertIn('<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">', body)
        desc = open(__file__.replace("test_vekking.py", "fixture_description.xml"), encoding="utf-8").read()
        desc = desc.replace("</device>", "<serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1"
                            "</serviceType><controlURL>/AVTransport</controlURL></service></serviceList></device>")
        self.assertEqual(v.avtransport_control_url(desc, "http://192.168.0.6:38400"),
                         "http://192.168.0.6:38400/AVTransport")


class Server(unittest.IsolatedAsyncioTestCase):
    async def test_wav_server_serves_file(self):
        srv = v.WavServer(v.make_wav(0.2))
        port = await srv.start("127.0.0.1", 0)
        data = await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/x.wav").read())
        await srv.stop()
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(srv.requests, 1)


if __name__ == "__main__":
    unittest.main()
