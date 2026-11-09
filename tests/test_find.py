"""«Finn høyttaler» skal aldri miste en adresse som virker, og oppdaging skal virke uten UDP.

Ingen nettverk utenfor 127.0.0.1: UPnP-søket og TCP-søket på /24-nettet er byttet ut.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lemus_remote import __main__ as app
from lemus_remote import discovery as d
from lemus_remote.config import Config
from lemus_remote.device import Speaker
from lemus_remote.profiles import ProfileStore
from lemus_remote.web import WebServer

from fake_sm450 import FakeSM450
from lemus_remote.discovery import close_server

DESCRIPTION = (Path(__file__).parent / "fixture_description.xml").read_text(encoding="utf-8")


def tmp_config() -> Config:
    return Config(Path(tempfile.mkdtemp()) / "config.json")


def no_ssdp(*_a, **_k):
    return []


class Hints(unittest.IsolatedAsyncioTestCase):
    async def test_hint_is_checked_directly_over_tcp(self):
        fetched, opened = [], []

        def fetch(url, timeout=4.0):
            fetched.append(url)
            return DESCRIPTION

        async def port_open(host, port, timeout=4.0, tries=3):
            opened.append((host, port))
            return True

        with mock.patch.object(d, "_ssdp_search", no_ssdp), mock.patch.object(d, "_fetch", fetch), \
                mock.patch.object(d, "_port_open", port_open), mock.patch.object(d, "scan_subnet") as scan:
            notes: list[str] = []
            found = await d.discover(timeout=0.1, hints=("192.168.0.6", "", "192.168.0.6"), report=notes, scan=True)
        self.assertEqual([(f.host, f.name) for f in found], [("192.168.0.6", "Lemus Home 1500")])
        self.assertEqual(fetched, ["http://192.168.0.6:38400/description.xml"])  # bare én gang
        self.assertEqual(opened, [("192.168.0.6", 50006)])
        scan.assert_not_called()  # en kjent adresse svarte: ikke søk på nettet
        self.assertTrue(any("svarte direkte" in n for n in notes), notes)

    async def test_hint_against_local_fake_with_custom_ports(self):
        async def http(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            body = DESCRIPTION.encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
            writer.close()

        srv = await asyncio.start_server(http, "127.0.0.1", 0)
        ctl = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        try:
            with mock.patch.object(d, "_ssdp_search", no_ssdp):
                f = await d.check_host("127.0.0.1", ctl.sockets[0].getsockname()[1], srv.sockets[0].getsockname()[1])
                found = await d.discover(timeout=0.1, hints=("127.0.0.1",),
                                         control_port=ctl.sockets[0].getsockname()[1],
                                         upnp_port=srv.sockets[0].getsockname()[1])
        finally:
            for s in (srv, ctl):
                await close_server(s)
        self.assertEqual(f.host, "127.0.0.1")
        self.assertEqual([x.host for x in found], ["127.0.0.1"])

    async def test_scan_runs_only_when_nothing_else_answered(self):
        hit = d.Found("192.168.0.6", "Lemus Home 1500", "uuid:x", "http://192.168.0.6:38400/description.xml")

        async def scan(lan_ip, upnp_port, control_port, report=None, skip=()):
            calls.append((lan_ip, upnp_port, control_port, skip))
            return [hit]

        async def nothing(*a, **k):
            return None

        calls: list = []
        with mock.patch.object(d, "_ssdp_search", no_ssdp), mock.patch.object(d, "scan_subnet", scan), \
                mock.patch.object(d, "check_host", nothing):
            self.assertEqual(await d.discover(timeout=0.1, hints=("192.168.0.7",)), [])  # scan er av som standard
            self.assertEqual(calls, [])
            found = await d.discover(timeout=0.1, hints=("192.168.0.7",), scan=True, lan_ip="192.168.0.9")
        self.assertEqual(found, [hit])
        self.assertEqual(calls, [("192.168.0.9", 38400, 50006, ("192.168.0.7",))])


class SubnetScan(unittest.IsolatedAsyncioTestCase):
    def test_only_private_slash_24(self):
        hosts = d.subnet_hosts("192.168.0.9")
        self.assertEqual(len(hosts), 253)
        self.assertEqual((hosts[0], hosts[-1]), ("192.168.0.1", "192.168.0.254"))
        self.assertNotIn("192.168.0.9", hosts)  # ikke PC-en selv
        self.assertEqual(d.subnet_hosts("10.1.2.3")[0], "10.1.2.1")
        self.assertEqual(len(d.subnet_hosts("172.31.255.1")), 253)
        for ip in ("8.8.8.8", "127.0.0.1", "169.254.1.2", "100.64.0.5", "172.32.0.1", "0.0.0.0",
                   "224.0.0.1", "fe80::1", "", None, "ikke-en-ip", "192.168.0.0/16"):
            self.assertEqual(d.subnet_hosts(ip), [], ip)

    async def test_concurrency_is_limited_and_only_upnp_port_is_probed(self):
        active, peak, probed, checked = 0, 0, [], []

        async def probe(host, port, timeout):
            nonlocal active, peak
            probed.append((host, port, timeout))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return host == "192.168.0.6"

        async def check(host, control_port, upnp_port, report=None):
            checked.append((host, control_port, upnp_port))
            return d.Found(host, "Lemus Home 1500", "uuid:x", "")

        notes: list[str] = []
        found = await d.scan_subnet("192.168.0.9", limit=16, report=notes, skip=("192.168.0.20",),
                                    probe=probe, check=check)
        self.assertEqual([f.host for f in found], ["192.168.0.6"])
        self.assertLessEqual(peak, 16)
        self.assertGreater(peak, 1)
        self.assertEqual(len(probed), 252)  # 254 minus PC-en og den som alt er sjekket
        self.assertEqual({p for _, p, _ in probed}, {38400})  # aldri 50006 eller 7777 under søket
        self.assertTrue(all(t == d.SCAN_TIMEOUT <= 1.5 for _, _, t in probed))
        self.assertTrue(all(h.startswith("192.168.0.") for h, _, _ in probed))
        self.assertEqual(checked, [("192.168.0.6", 50006, 38400)])  # bare den som svarte
        self.assertIn("1 av 252 svarte", notes[-1])

    async def test_public_network_is_never_scanned(self):
        async def probe(*a):
            raise AssertionError("skulle ikke søke")

        notes: list[str] = []
        self.assertEqual(await d.scan_subnet("81.0.0.5", probe=probe, report=notes), [])
        self.assertIn("ikke et privat nett", notes[0])

    async def test_probe_sends_nothing(self):
        got: list[bytes] = []
        done = asyncio.Event()

        async def handle(reader, writer):
            got.append(await reader.read())  # til EOF
            writer.close()
            done.set()

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            self.assertTrue(await d._tcp_probe("127.0.0.1", port, 1.0))
            await asyncio.wait_for(done.wait(), 2)
        finally:
            srv.close()
            await srv.wait_closed()
        self.assertEqual(got, [b""])
        self.assertFalse(await d._tcp_probe("127.0.0.1", port, 0.5))  # lukket port


class ScanDetails(unittest.IsolatedAsyncioTestCase):
    async def test_refused_hosts_get_one_retry_others_do_not(self):
        calls: dict[str, int] = {}

        async def probe(host, port, timeout):
            calls[host] = calls.get(host, 0) + 1
            if host == "192.168.0.6":  # avviser første gang, svarer andre gang
                return calls[host] > 1 or None
            return None if host == "192.168.0.1" else False

        async def check(host, control_port, upnp_port, report=None):
            return d.Found(host, "Lemus Home 1500", "uuid:x", "")

        with mock.patch.object(d, "SCAN_RETRY_PAUSE", 0):
            found = await d.scan_subnet("192.168.0.9", probe=probe, check=check)
        self.assertEqual([f.host for f in found], ["192.168.0.6"])
        self.assertEqual((calls["192.168.0.6"], calls["192.168.0.1"], calls["192.168.0.2"]), (2, 2, 1))
        self.assertEqual(sum(calls.values()), 253 + 2)

    async def test_default_check_uses_short_timeouts(self):
        got = []

        async def check_host(host, control_port, upnp_port, report=None, **kw):
            got.append((host, kw))
            return None

        async def probe(host, port, timeout):
            return host == "192.168.0.6"

        with mock.patch.object(d, "check_host", check_host):
            await d.scan_subnet("192.168.0.9", probe=probe)
        self.assertEqual(got, [("192.168.0.6", {"fetch_timeout": 2.0, "port_timeout": 2.0, "tries": 2})])

    async def test_port_open_does_not_sleep_after_last_try(self):
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        srv.close()
        await srv.wait_closed()
        t0 = asyncio.get_running_loop().time()
        self.assertFalse(await d._port_open("127.0.0.1", port, 0.5, tries=1))
        self.assertLess(asyncio.get_running_loop().time() - t0, 0.9)

    async def test_refused_probe_is_none(self):
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        srv.close()
        await srv.wait_closed()
        res = await d._tcp_probe("127.0.0.1", port, 0.5)
        # Linux avviser en lukket port med en gang (None). Windows prøver SYN på nytt i ca. 2 s,
        # så der blir det tidsavbrudd (False). Begge betyr «ingen høyttaler her».
        self.assertIn(res, (None, False))

    async def test_unknown_pc_address_gives_clear_note(self):
        for ip in (None, "127.0.0.1", "0.0.0.0"):
            notes: list[str] = []
            self.assertEqual(await d.scan_subnet(ip, report=notes), [])
            self.assertIn("fant ikke PC-ens adresse", notes[0])
        self.assertIsNone(d.scan_skip_reason("192.168.0.9"))
        self.assertIn("ikke et privat nett", d.scan_skip_reason("81.0.0.5"))


class Background(unittest.IsolatedAsyncioTestCase):
    def test_scan_budget_backs_off_and_resets(self):
        now = [0.0]
        b = app.ScanBudget((60.0, 120.0, 300.0), clock=lambda: now[0])
        waits = []
        for _ in range(5):
            self.assertTrue(b.due())
            b.spend()
            start = now[0]
            while not b.due():
                now[0] += 10
            waits.append(now[0] - start)
        self.assertEqual(waits, [60, 120, 300, 300, 300])
        b.spend()
        self.assertFalse(b.due())
        b.reset()
        self.assertTrue(b.due())

    async def test_search_skips_scan_without_lan_address(self):
        cfg = tmp_config()
        seen, spent = {}, []

        async def fake_discover(**kw):
            seen.update(kw)
            return []

        with mock.patch.object(app, "_pick_lan_ip", lambda h: "127.0.0.1"):
            _, notes = await app.search(cfg, None, discover_fn=fake_discover, on_scan=lambda: spent.append(1))
        self.assertFalse(seen["scan"])
        self.assertEqual(spent, [])  # kvoten brukes ikke opp når søket ble hoppet over
        self.assertIn("fant ikke PC-ens adresse", notes[0])
        with mock.patch.object(app, "_pick_lan_ip", lambda h: "192.168.0.9"):
            await app.search(cfg, None, discover_fn=fake_discover, on_scan=lambda: spent.append(1))
        self.assertTrue(seen["scan"])
        self.assertEqual(spent, [1])

    def test_strict_pick_never_switches_to_other_speaker(self):
        cfg = tmp_config()
        other = d.Found("192.168.0.7", "Lemus Bar", "uuid:other", "")
        mine = d.Found("192.168.0.6", "Lemus Home 1500", "uuid:mine", "")
        self.assertEqual(app.pick([other], cfg, strict=True), other)  # første oppdagelse: UDN ukjent
        cfg.data["udn"] = "uuid:mine"
        self.assertIsNone(app.pick([other], cfg, strict=True))
        self.assertEqual(app.pick([other], cfg), other)  # «Finn automatisk» kan bytte
        self.assertEqual(app.pick([other, mine], cfg), mine)
        self.assertEqual(app.pick([other, mine], cfg, strict=True), mine)

    async def test_shared_search_runs_one_network_search_at_a_time(self):
        runs, release = [], asyncio.Event()
        hit = d.Found("192.168.0.6", "Lemus Home 1500", "uuid:x", "")

        async def fake_discover(**kw):
            runs.append(kw["scan"])
            kw["report"].append(f"søk {len(runs)}")
            await release.wait()
            return [hit]

        shared = app.SharedSearch(fake_discover)
        n1, n2, n3 = [], [], []
        a = asyncio.create_task(shared(scan=True, report=n1))
        await asyncio.sleep(0)
        b = asyncio.create_task(shared(scan=False, report=n2))  # bruker svaret fra søket som går
        await asyncio.sleep(0.05)
        release.set()
        self.assertEqual((await a, await b), ([hit], [hit]))
        self.assertEqual((runs, n1, n2), ([True], ["søk 1"], ["søk 1"]))

        release.clear()
        a = asyncio.create_task(shared(scan=False, report=n1))
        await asyncio.sleep(0)
        b = asyncio.create_task(shared(scan=True, report=n3))  # vil søke grundigere: venter, så eget søk
        await asyncio.sleep(0.05)
        self.assertEqual(runs, [True, False])
        release.set()
        await a
        await b
        self.assertEqual(runs, [True, False, True])
        self.assertEqual(n3, ["søk 3"])

    async def test_shared_search_survives_a_caller_giving_up(self):
        release = asyncio.Event()
        runs = []

        async def fake_discover(**kw):
            runs.append(1)
            await release.wait()
            return []

        shared = app.SharedSearch(fake_discover)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(shared(scan=True, report=[]), 0.05)
        later = asyncio.create_task(shared(scan=True, report=[]))
        await asyncio.sleep(0.05)
        release.set()
        self.assertEqual(await later, [])
        self.assertEqual(runs, [1])  # søket som gikk, ble fullført og gjenbrukt


class KnownHosts(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_config_last_host_and_current_address(self):
        cfg = tmp_config()
        cfg.data.update({"host": None, "last_host": "192.168.0.6"})
        seen = {}

        async def fake_discover(**kw):
            seen.update(kw)
            return [d.Found("192.168.0.6", "Lemus Home 1500", "uuid:x", "")]

        with mock.patch.object(app, "_pick_lan_ip", lambda h: "192.168.0.9"):
            f, _ = await app.search(cfg, None, discover_fn=fake_discover)
            self.assertEqual(seen["hints"], ("192.168.0.6",))
            self.assertEqual((seen["lan_ip"], seen["scan"]), ("192.168.0.9", True))
            self.assertEqual(f.host, "192.168.0.6")
            cfg.data.update({"host": "192.168.0.6", "last_host": "192.168.0.6"})
            await app.search(cfg, Speaker("192.168.0.8"), scan=False, discover_fn=fake_discover)
            self.assertEqual(seen["hints"], ("192.168.0.6", "192.168.0.8"))
            self.assertFalse(seen["scan"])

    def test_last_host_is_saved_only_when_it_changes(self):
        cfg = tmp_config()
        remember = app.remember_host(cfg)
        remember("192.168.0.6")
        self.assertEqual(Config(cfg.path)["last_host"], "192.168.0.6")
        with mock.patch.object(cfg, "save") as save:
            remember("192.168.0.6")
            save.assert_not_called()


class LastHost(unittest.IsolatedAsyncioTestCase):
    async def test_last_host_is_stored_when_connection_succeeds(self):
        fake = FakeSM450()
        ctl, luci = await fake.start()
        cfg = tmp_config()
        sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci, use_luci=False,
                     on_connect=app.remember_host(cfg))
        sp.start()
        try:
            self.assertTrue(await sp._wait_for(lambda s: s["connected"], 5))
        finally:
            await sp.stop()
            await fake.stop()
        self.assertEqual(Config(cfg.path)["last_host"], "127.0.0.1")


class FindApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSM450()
        ctl, luci = await self.fake.start()
        self.cfg = tmp_config()
        self.cfg["host"] = "127.0.0.1"
        self.sp = Speaker("127.0.0.1", control_port=ctl, luci_port=luci)
        self.sp.start()
        self.assertTrue(await self.sp._wait_for(lambda s: s["connected"], 5))
        self.web = WebServer(self.sp, "127.0.0.1", 0, config=self.cfg, profiles=ProfileStore(self.cfg))
        await self.web.start()

    async def asyncTearDown(self):
        await self.web.stop()
        await self.sp.stop()
        await self.fake.stop()

    async def call(self, body):
        r, w = await asyncio.open_connection("127.0.0.1", self.web.bound_port)
        data = json.dumps(body).encode()
        w.write(f"POST /api/settings HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
        raw = await r.read()
        w.close()
        head, _, payload = raw.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(payload)

    def assert_kept(self):
        self.assertEqual((self.cfg["host"], Config(self.cfg.path)["host"], self.sp.host), ("127.0.0.1",) * 3)
        self.assertTrue(self.sp.state["connected"])
        self.assertFalse(self.sp.state["searching"])

    async def test_nothing_found_keeps_address_and_connection(self):
        async def finder():
            return None

        for f in (None, finder):  # uten finner og uten resolver: samme svar
            self.web.finder = f
            status, j = await self.call({"host": ""})
            self.assertEqual(status, 404)
            self.assertEqual(j["error"], "Fant ingen høyttaler. Beholder 127.0.0.1.")
            await asyncio.sleep(0.2)
            self.assert_kept()

    async def test_slow_or_broken_finder_keeps_address(self):
        async def slow():
            await asyncio.sleep(30)

        async def broken():
            raise OSError("nettverk nede")

        async def garbage():
            return {"host": "ikke-en-ip"}

        self.web.find_timeout = 0.3
        with self.assertLogs("lemus_remote.web", "ERROR"):  # feilen logges, men adressen beholdes
            for f in (slow, broken, garbage):
                self.web.finder = f
                status, j = await self.call({"host": ""})
                self.assertEqual(status, 404, f)
                self.assertIn("Beholder", j["error"])
                self.assert_kept()

    async def test_found_address_is_used(self):
        async def finder():
            return {"host": "127.0.0.2", "name": "Lemus Home 1500"}

        self.web.finder = finder
        status, j = await self.call({"host": ""})
        self.assertEqual(status, 200)
        self.assertEqual(j["found"], {"host": "127.0.0.2", "name": "Lemus Home 1500"})
        self.assertEqual((self.cfg["host"], self.sp.host, j["host"]), ("127.0.0.2",) * 3)

    async def test_same_address_found_does_not_reconnect(self):
        async def finder():
            return {"host": "127.0.0.1", "name": "Lemus Home 1500"}

        self.web.finder = finder
        with mock.patch.object(self.sp, "set_host") as set_host:
            status, j = await self.call({"host": ""})
        self.assertEqual((status, j["found"]["host"]), (200, "127.0.0.1"))
        set_host.assert_not_called()
        self.assert_kept()

    async def test_only_one_search_at_a_time(self):
        release = asyncio.Event()
        runs = []

        async def finder():
            runs.append(1)
            await release.wait()
            return None

        self.web.finder = finder
        first = asyncio.create_task(self.call({"host": ""}))
        await asyncio.sleep(0.2)
        status, j = await self.call({"host": ""})
        self.assertEqual(status, 409)
        self.assertIn("Leter allerede", j["error"])
        release.set()
        self.assertEqual((await first)[0], 404)
        self.assertEqual(runs, [1])
        self.assert_kept()

    async def test_manual_address_during_search_is_not_overwritten(self):
        release = asyncio.Event()

        async def finder():
            await release.wait()
            return {"host": "127.0.0.2", "name": "Lemus Home 1500"}

        self.web.finder = finder
        search = asyncio.create_task(self.call({"host": ""}))
        await asyncio.sleep(0.2)
        status, _ = await self.call({"host": "127.0.0.3"})
        self.assertEqual(status, 200)
        release.set()
        status, j = await search
        self.assertEqual(status, 409)
        self.assertEqual(j["error"], "Adressen ble endret mens søket pågikk. Beholder 127.0.0.3.")
        self.assertEqual((self.cfg["host"], self.sp.host), ("127.0.0.3", "127.0.0.3"))

    async def test_manual_address_still_works(self):
        status, j = await self.call({"host": "127.0.0.3"})
        self.assertEqual((status, self.cfg["host"], self.sp.host), (200, "127.0.0.3", "127.0.0.3"))


if __name__ == "__main__":
    unittest.main()
