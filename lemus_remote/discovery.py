"""Finn Lemus/SM450-høyttalere på lokalnettet uten å kjenne IP-adressen.

Fremgangsmåten er den samme som i kartleggingen 7.11.2026, og den sender ingen
styringskommandoer:

1. Standard SSDP-søk (UPnP M-SEARCH) etter MediaRenderer. Ufarlig, det er det
   alle mediespillere gjør.
2. HTTP GET av beskrivelsen i svarets LOCATION. Vi godtar enheter med
   ``manufacturer`` LibreWireless eller ``modelDescription`` LibreSync, og
   bare når LOCATION peker på samme adresse som svaret kom fra.
3. TCP-tilkobling til port 50006, som lukkes med en gang uten at noe sendes.
   Det skiller en styrbar SM450 fra andre Libre-enheter.

Virker ikke UDP (brannmur, antivirus eller et Wi-Fi som stopper multicast), sjekkes
kjente adresser direkte over TCP. Finnes fortsatt ingenting, kan det gjøres et skånsomt
TCP-søk på eget /24-nett (``scan=True``): bare tilkobling til port 38400 som lukkes uten
at noe sendes, og for dem som svarer, HTTP GET av beskrivelsen. Ingenting sendes til
50006 eller 7777 under søket.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from urllib.parse import urlparse

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"
).encode()


@dataclass
class Found:
    host: str
    name: str
    udn: str
    location: str


UPNP_PORT = 38400
SCAN_TIMEOUT = 1.5   # sekunder per tilkoblingsforsøk i TCP-søket (Wi-Fi + ARP kan bruke over 1 s)
SCAN_LIMIT = 64      # høyst så mange samtidige tilkoblingsforsøk (253 adresser ≈ 4 × 1,5 s)
SCAN_RETRY_PAUSE = 0.5  # pause før et nytt forsøk mot verter som avviste tilkoblingen
# Bare private nett (RFC 1918) søkes, og aldri større enn /24.
PRIVATE_NETS = tuple(ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _lan_ip(target: str = "192.0.2.1") -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))  # sender ingenting, velger bare nettkort
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def parse_ssdp(data: bytes) -> dict[str, str]:
    headers = {}
    for line in data.decode("latin-1").split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


def _tag(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return m.group(1).strip() if m else ""


def parse_description(xml: str) -> dict[str, str] | None:
    """Returnerer navn og UDN hvis beskrivelsen er en Libre/LibreSync-enhet, ellers None."""
    manufacturer = _tag(xml, "manufacturer")
    model_desc = _tag(xml, "modelDescription")
    if "librewireless" not in manufacturer.lower() and "libresync" not in model_desc.lower():
        return None
    return {"name": _tag(xml, "friendlyName") or "Lemus", "udn": _tag(xml, "UDN")}


def _ssdp_search(timeout: float, target: tuple[str, int], hints: tuple[str, ...] = ()) -> list[tuple[str, bytes]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        ip = _lan_ip()
        if ip and target == SSDP_ADDR:
            try:  # send på riktig nettkort når PC-en har flere
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            except OSError:
                pass
        sock.bind(("", 0))
        sock.settimeout(0.3)
        replies = []
        end = time.monotonic() + timeout
        next_send = 0.0
        sent = 0
        while time.monotonic() < end:
            if sent < 3 and time.monotonic() >= next_send:  # UDP kan gå tapt: send tre ganger
                sock.sendto(SEARCH, target)
                for h in hints:  # spør også kjente adresser direkte (unicast), det virker bak flere brannmurer
                    try:
                        sock.sendto(SEARCH, (h, 1900))
                    except OSError:
                        pass
                sent += 1
                next_send = time.monotonic() + 0.7
            try:
                data, addr = sock.recvfrom(4096)
                replies.append((addr[0], data))
            except (TimeoutError, socket.timeout):
                continue
        return replies
    finally:
        sock.close()


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fetch(url: str, timeout: float = 4.0) -> str | None:
    try:
        with _NO_PROXY.open(url, timeout=timeout) as resp:  # aldri via proxy: dette er lokalnettet
            return resp.read(64 * 1024).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


async def _port_open(host: str, port: int, timeout: float = 4.0, tries: int = 3) -> bool:
    # SM450 avviser ofte de første tilkoblingene (sett i lesetesten), så prøv flere ganger.
    for i in range(tries):
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError):
            if i + 1 < tries:  # ingen pause etter siste forsøk
                await asyncio.sleep(1.0 + i)
            continue
        writer.close()  # sender ingenting
        return True
    return False


async def check_host(host: str, control_port: int = 50006, upnp_port: int = UPNP_PORT,
                     report: list[str] | None = None, fetch_timeout: float = 4.0,
                     port_timeout: float = 4.0, tries: int = 3) -> Found | None:
    """Sjekk en kjent adresse direkte: beskrivelse på 38400 og port 50006."""
    location = f"http://{host}:{upnp_port}/description.xml"
    xml = await asyncio.to_thread(_fetch, location, fetch_timeout)
    info = parse_description(xml or "")
    if not info:
        if report is not None:
            report.append(f"{host}: ingen Libre-beskrivelse på {location}")
        return None
    if not await _port_open(host, control_port, port_timeout, tries):
        if report is not None:
            report.append(f"{host}: {info['name']} funnet, men port {control_port} svarte ikke")
        return None
    return Found(host=host, name=info["name"], udn=info["udn"], location=location)


def scan_skip_reason(lan_ip: str | None) -> str | None:
    """Hvorfor TCP-søket ikke kan kjøres med denne PC-adressen, eller None når det kan."""
    try:
        ip = ipaddress.IPv4Address(str(lan_ip or "").strip())
    except ValueError:
        ip = None
    if ip is None or ip.is_loopback or ip.is_unspecified:
        return "TCP-søk hoppet over: fant ikke PC-ens adresse på hjemmenettet (er nettet oppe?)"
    if not any(ip in net for net in PRIVATE_NETS):
        return f"TCP-søk hoppet over: {ip} er ikke et privat nett"
    return None


def subnet_hosts(lan_ip: str | None) -> list[str]:
    """Adressene på eget /24-nett (x.x.x.1-254, uten PC-en selv). Tomt for alt som ikke er RFC 1918."""
    try:
        ip = ipaddress.IPv4Address(str(lan_ip or "").strip())
    except ValueError:
        return []
    if not any(ip in net for net in PRIVATE_NETS):
        return []
    net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
    return [str(h) for h in net.hosts() if h != ip]


async def _tcp_probe(host: str, port: int, timeout: float = SCAN_TIMEOUT) -> bool | None:
    """Svarer noen på porten? Kobler til og lukker igjen uten å sende noe.

    True = åpen, None = verten finnes men avviste tilkoblingen, False = ingen svar.
    """
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except ConnectionRefusedError:
        return None
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def scan_subnet(lan_ip: str | None, upnp_port: int = UPNP_PORT, control_port: int = 50006,
                      timeout: float = SCAN_TIMEOUT, limit: int = SCAN_LIMIT,
                      report: list[str] | None = None, skip: tuple[str, ...] = (),
                      probe: Callable[[str, int, float], Awaitable[bool]] | None = None,
                      check: Callable[..., Awaitable[Found | None]] | None = None) -> list[Found]:
    """Skånsomt TCP-søk på eget /24-nett for nett der UDP/SSDP ikke virker.

    Bare tilkobling til ``upnp_port`` (lukkes uten at noe sendes), med høyst ``limit``
    forsøk samtidig. Verter som avviste tilkoblingen (``probe`` gir None), prøves én gang
    til etter en kort pause. Bare de som svarer, sjekkes videre med ``check_host`` (med
    kortere tidsfrister, så hele søket holder seg godt under 20 s).
    """
    reason = scan_skip_reason(lan_ip)
    hosts = [h for h in subnet_hosts(lan_ip) if h not in skip] if not reason else []
    if not hosts:
        if report is not None:
            report.append(reason or "TCP-søk hoppet over: ingen adresser å søke på")
        return []
    probe = probe or _tcp_probe
    check = check or partial(check_host, fetch_timeout=2.0, port_timeout=2.0, tries=2)
    gate = asyncio.Semaphore(max(1, limit))

    async def one(h: str) -> bool | None:
        async with gate:
            return await probe(h, upnp_port, timeout)

    results = dict(zip(hosts, await asyncio.gather(*(one(h) for h in hosts))))
    refused = [h for h, r in results.items() if r is None]
    if refused:  # SM450 avviser av og til de første tilkoblingene: ett forsøk til
        await asyncio.sleep(SCAN_RETRY_PAUSE)
        results.update(zip(refused, await asyncio.gather(*(one(h) for h in refused))))
    answered = [h for h in hosts if results[h] is True]
    net = ipaddress.IPv4Network(f"{lan_ip}/24", strict=False)
    if report is not None:
        report.append(f"TCP-søk på {net} (port {upnp_port}): {len(answered)} av {len(hosts)} svarte")
    found = await asyncio.gather(*(check(h, control_port, upnp_port, report=report) for h in answered))
    return [f for f in found if f]


async def discover(timeout: float = 4.0, target: tuple[str, int] = SSDP_ADDR,
                   control_port: int = 50006, hints: tuple[str, ...] = (),
                   report: list[str] | None = None, upnp_port: int = UPNP_PORT,
                   scan: bool = False, lan_ip: str | None = None) -> list[Found]:
    """Finn høyttalere. ``hints`` er adresser som også sjekkes direkte over TCP (for eksempel
    sist kjente), samtidig med UPnP-søket. Med ``scan`` søkes eget /24-nett over TCP når
    ingenting ble funnet (``lan_ip`` = PC-ens adresse, ellers nettkortet med standardruten).
    ``report`` fylles med forklaringer på hva som ble prøvd og forkastet."""
    hints = tuple(dict.fromkeys(h for h in hints if h))
    replies, direct = await asyncio.gather(
        asyncio.to_thread(_ssdp_search, timeout, target, hints),
        asyncio.gather(*(check_host(h, control_port, upnp_port, report=report) for h in hints)))
    if report is not None:
        report.append(f"{len(replies)} UPnP-svar mottatt")
    seen: dict[str, Found] = {}
    for h, f in zip(hints, direct):
        if f:
            seen[h] = f
            if report is not None:
                report.append(f"{h}: {f.name} svarte direkte")
    for sender, data in replies:
        location = parse_ssdp(data).get("location", "")
        parsed = urlparse(location)
        # Følg bare en LOCATION som peker på samme adresse som svaret kom fra.
        if parsed.scheme != "http" or parsed.hostname != sender or sender in seen:
            continue
        xml = await asyncio.to_thread(_fetch, location)
        info = parse_description(xml or "")
        if not info:
            continue
        if not await _port_open(sender, control_port):
            if report is not None:
                report.append(f"{sender}: {info['name']} funnet, men port {control_port} svarte ikke")
            continue
        seen[sender] = Found(host=sender, name=info["name"], udn=info["udn"], location=location)
    # Verken UPnP-svar eller kjente adresser: søk på eget nett over TCP.
    if not seen and scan:
        for f in await scan_subnet(lan_ip or _lan_ip(), upnp_port, control_port, report=report, skip=hints):
            seen.setdefault(f.host, f)
    return sorted(seen.values(), key=lambda f: f.name)


if __name__ == "__main__":
    import sys
    hints = tuple(a for a in sys.argv[1:] if not a.startswith("-"))
    notes: list[str] = []
    found = asyncio.run(discover(hints=hints, report=notes, scan="--no-scan" not in sys.argv))
    for n in notes:
        print("  ·", n)
    if not found:
        print("Fant ingen Lemus/SM450 på nettet.")
        print("Tips: tillat Python i Windows-brannmuren (Private nettverk), eller oppgi adressen:")
        print("      py -m lemus_remote.discovery 192.168.0.6")
    for f in found:
        print(f"{f.name}  {f.host}  {f.udn}")

async def close_server(server, timeout: float = 2.0) -> None:
    """Steng en asyncio-server uten å henge. Fra Python 3.12 venter wait_closed() på alle åpne
    tilkoblinger (f.eks. SSE), og på Windows kan det ta evig tid. Vi lukker klientene og venter kort."""
    server.close()
    if hasattr(server, "close_clients"):
        server.close_clients()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        pass
