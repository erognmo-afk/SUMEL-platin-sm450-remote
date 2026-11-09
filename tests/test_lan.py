"""Tester for valg av adressen iPhone skal bruke (uten nettverk: socket er byttet ut)."""

import socket
import unittest
from unittest import mock

from lemus_remote import __main__ as app


class FakeSocket:
    """Later som rutetabellen: hvilket eget nettkort som brukes mot hvilken adresse."""

    def __init__(self, routes: dict, calls: list):
        self.routes, self.calls, self.target = routes, calls, None

    def connect(self, addr):
        self.calls.append(addr[0])
        if self.routes.get(addr[0]) is None:
            raise OSError("Network is unreachable")
        self.target = addr[0]

    def getsockname(self):
        return (self.routes[self.target], 50000)

    def close(self):
        pass


def fake_socket(routes: dict):
    calls: list = []
    return mock.patch.object(app.socket, "socket", lambda *a, **k: FakeSocket(routes, calls)), calls


class PickLanIp(unittest.TestCase):
    def test_speaker_ip_selects_the_adapter_that_reaches_it(self):
        # Standardruten går via VPN; høyttaleren nås via Wi-Fi-kortet.
        patch, calls = fake_socket({"192.168.0.6": "192.168.0.9", app.FALLBACK_TARGET: "10.8.0.2"})
        with patch:
            self.assertEqual(app._pick_lan_ip("192.168.0.6"), "192.168.0.9")
        self.assertEqual(calls, ["192.168.0.6"])

    def test_unknown_speaker_uses_default_route(self):
        patch, calls = fake_socket({app.FALLBACK_TARGET: "192.168.0.9"})
        with patch:
            self.assertEqual(app._pick_lan_ip(None), "192.168.0.9")
            self.assertEqual(app._pick_lan_ip(""), "192.168.0.9")
        self.assertEqual(calls, [app.FALLBACK_TARGET] * 2)

    def test_falls_back_when_speaker_unreachable(self):
        patch, calls = fake_socket({app.FALLBACK_TARGET: "192.168.0.9"})
        with patch:
            self.assertEqual(app._pick_lan_ip("192.168.1.50"), "192.168.0.9")
        self.assertEqual(calls, ["192.168.1.50", app.FALLBACK_TARGET])

    def test_loopback_or_invalid_speaker_address_is_not_used(self):
        patch, calls = fake_socket({"127.0.0.1": "127.0.0.1", app.FALLBACK_TARGET: "192.168.0.9"})
        with patch:
            for host in ("127.0.0.1", "0.0.0.0", "lemus.local", "fe80::1", "999.1.1.1", "224.0.0.1"):
                self.assertEqual(app._pick_lan_ip(host), "192.168.0.9", host)
        self.assertEqual(set(calls), {app.FALLBACK_TARGET})

    def test_unusable_own_address_falls_back(self):
        patch, calls = fake_socket({"192.168.0.6": "0.0.0.0", app.FALLBACK_TARGET: "192.168.0.9"})
        with patch:
            self.assertEqual(app._pick_lan_ip("192.168.0.6"), "192.168.0.9")

    def test_no_network_at_all_gives_localhost(self):
        patch, _ = fake_socket({})
        with patch:
            self.assertEqual(app._pick_lan_ip("192.168.0.6"), "127.0.0.1")

    def test_real_socket_never_raises(self):
        # Ekte socket, men UDP-connect sender ingenting. Skal alltid gi en IPv4-adresse.
        ip = app._lan_ip()
        socket.inet_aton(ip)


class LanAddressTracking(unittest.TestCase):
    def setUp(self):
        self.routes = {None: "10.8.0.2", "192.168.0.6": "192.168.0.9", "192.168.0.7": "192.168.0.9",
                       "10.0.0.5": "10.0.0.2"}
        self.pick = lambda host: self.routes[host]

    def test_initial_address_and_url(self):
        lan = app.LanAddress(8080, None, pick=self.pick)
        self.assertEqual(lan.url, "http://10.8.0.2:8080")

    def test_speaker_found_after_start_changes_address(self):
        lan = app.LanAddress(8080, None, pick=self.pick)
        self.assertTrue(lan.update("192.168.0.6"))
        self.assertEqual(lan.url, "http://192.168.0.9:8080")
        self.assertFalse(lan.update("192.168.0.6"))  # samme høyttaler: ingen ny melding

    def test_same_adapter_gives_no_change(self):
        lan = app.LanAddress(8080, "192.168.0.6", pick=self.pick)
        self.assertFalse(lan.update("192.168.0.7"))  # ny IP på høyttaleren, samme nettkort
        self.assertEqual(lan.ip, "192.168.0.9")

    def test_searching_keeps_last_address(self):
        lan = app.LanAddress(8080, "192.168.0.6", pick=self.pick)
        self.assertFalse(lan.update(None))
        self.assertEqual(lan.ip, "192.168.0.9")
        self.assertTrue(lan.update("10.0.0.5"))
        self.assertEqual(lan.ip, "10.0.0.2")


class LanAddressRefresh(unittest.TestCase):
    """Nettet kommer opp etter oppstart (autostart før Wi-Fi), eller PC-en bytter nettkort."""

    def setUp(self):
        self.routes = {"192.168.0.6": "127.0.0.1", None: "127.0.0.1"}
        self.pick = lambda host: self.routes[host]

    def test_localhost_at_start_is_replaced_when_network_comes_up(self):
        lan = app.LanAddress(8080, "192.168.0.6", pick=self.pick)
        self.assertEqual(lan.ip, "127.0.0.1")
        state = {"host": "192.168.0.6", "connected": False}
        self.assertFalse(lan.observe(state))  # fortsatt ikke nett
        self.routes["192.168.0.6"] = "192.168.0.9"
        self.assertTrue(lan.observe({"host": "192.168.0.6", "connected": True}))
        self.assertEqual(lan.url, "http://192.168.0.9:8080")

    def test_localhost_is_retried_even_before_connected(self):
        lan = app.LanAddress(8080, "192.168.0.6", pick=self.pick)
        self.routes["192.168.0.6"] = "192.168.0.9"
        self.assertTrue(lan.observe({"host": "192.168.0.6", "connected": False, "volume": 3}))
        self.assertEqual(lan.ip, "192.168.0.9")

    def test_reconnect_picks_new_adapter(self):
        self.routes["192.168.0.6"] = "192.168.0.20"  # kabel
        lan = app.LanAddress(8080, "192.168.0.6", pick=self.pick)
        self.assertFalse(lan.observe({"host": "192.168.0.6", "connected": True}))
        self.assertFalse(lan.observe({"host": "192.168.0.6", "connected": False}))
        self.routes["192.168.0.6"] = "192.168.0.9"  # nå Wi-Fi
        self.assertTrue(lan.observe({"host": "192.168.0.6", "connected": True}))
        self.assertEqual(lan.ip, "192.168.0.9")

    def test_network_lost_keeps_last_good_address(self):
        self.routes["192.168.0.6"] = "192.168.0.9"
        lan = app.LanAddress(8080, "192.168.0.6", pick=self.pick)
        self.routes["192.168.0.6"] = "127.0.0.1"
        self.assertFalse(lan.observe({"host": "192.168.0.6", "connected": True}))
        self.assertEqual(lan.ip, "192.168.0.9")

    def test_no_repick_on_ordinary_state_changes(self):
        calls = []
        lan = app.LanAddress(8080, "192.168.0.6", pick=lambda h: calls.append(h) or "192.168.0.9")
        lan.observe({"host": "192.168.0.6", "connected": True})
        n = len(calls)
        for v in range(5):
            self.assertFalse(lan.observe({"host": "192.168.0.6", "connected": True, "volume": v}))
        self.assertEqual(len(calls), n)

    def test_non_text_host_in_config_does_not_crash(self):
        patch, _ = fake_socket({app.FALLBACK_TARGET: "192.168.0.9"})
        with patch:
            for host in (192, 3.5, ["192.168.0.6"], {"a": 1}):
                self.assertEqual(app._pick_lan_ip(host), "192.168.0.9", host)
            self.assertEqual(app.LanAddress(8080, 192).ip, "192.168.0.9")


if __name__ == "__main__":
    unittest.main()
