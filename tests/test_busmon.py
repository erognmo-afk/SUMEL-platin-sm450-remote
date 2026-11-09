import unittest

from lemus_remote import protocol as p
from lemus_remote.busmon import describe


class Describe(unittest.TestCase):
    def test_device_frames(self):
        self.assertIn("bånd 0: Parametric EQ, 1000 Hz", describe(bytes.fromhex("000f00080402000907010a000100023202")))
        self.assertIn("manual_eq", describe(bytes.fromhex("000400030501")))

    def test_client_requests(self):
        self.assertIn("KLIENT", describe(p.get_filters(0)))
        self.assertIn("les EQ-bånd", describe(p.get_filters(0)))
        write = bytes.fromhex("0023") + bytes([0x55]) + bytes([0x08, 0x01]) + bytes(30)
        self.assertIn("SKRIV EQ-BÅND", describe(write))


if __name__ == "__main__":
    unittest.main()
