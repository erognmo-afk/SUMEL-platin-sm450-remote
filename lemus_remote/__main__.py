"""Start SUMEL – fjernkontroll for Platin SM450.

``py -m lemus_remote``                  finner høyttaleren selv (husker den til neste gang)
``py -m lemus_remote --host 192.168.0.6``  bruker en bestemt adresse (lagres)
``py -m lemus_remote --tray``            uten konsollvindu, med ikon ved klokken (se ``tray.py``)

``SUMEL.exe`` (Windows-programmet) starter alltid med ikon ved klokken.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import socket
import sys
import time
import webbrowser
from dataclasses import dataclass
from typing import Callable

from . import __version__
from .config import Config
from .device import Speaker
from .discovery import Found, discover, scan_skip_reason
from .profiles import ProfileStore
from .web import WebServer


FALLBACK_TARGET = "192.0.2.1"  # dokumentasjonsadresse: finner nettkortet med standardrute
# Letingen i bakgrunnen (bare uten fast adresse) søker eget nett over TCP stadig sjeldnere:
# straks, så etter 1, 2, 5, 15 og deretter hvert 30. minutt. Nullstilles ved tilkobling.
SCAN_BACKOFF = (60.0, 120.0, 300.0, 900.0, 1800.0)
MOBILE_HINT = "Får ikke mobilen kontakt? Dobbeltklikk tillat-mobil.bat"


def _lan_ip(target: str = FALLBACK_TARGET) -> str:
    """PC-ens egen adresse på nettkortet som brukes for å nå ``target``.

    En UDP-«tilkobling» sender ingenting. Den får bare Windows til å velge nettkort ut fra
    rutetabellen, og så leser vi av hvilken adresse det nettkortet har.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _ipv4(text: object) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(str(text or "").strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _usable(ip: str | None) -> bool:
    """En adresse mobilen kan bruke: IPv4, ikke 127.x, 0.0.0.0, multicast eller lignende."""
    a = _ipv4(ip)
    return a is not None and not (a.is_loopback or a.is_unspecified or a.is_multicast or a.is_reserved)


def _pick_lan_ip(speaker_host: str | None) -> str:
    """Adressen iPhone skal bruke.

    Kjenner vi høyttalerens IP, velger vi nettkortet som faktisk når høyttaleren. Mobilen
    er på samme Wi-Fi som høyttaleren, så det er riktig nett også med VPN eller virtuelle
    nettkort (Hyper-V, WSL, VirtualBox). Ellers: nettkortet med standardruten.
    """
    if _usable(speaker_host):
        ip = _lan_ip(str(_ipv4(speaker_host)))
        if _usable(ip):
            return ip
    return _lan_ip()


class LanAddress:
    """Holder iPhone-adressen oppdatert når høyttalerens adresse blir kjent eller endres."""

    def __init__(self, port: int, speaker_host: str | None,
                 pick: Callable[[str | None], str] | None = None) -> None:
        self.port = port
        self._pick = pick or _pick_lan_ip
        self._host = speaker_host
        self._connected = False
        self.ip = self._pick(speaker_host)

    @property
    def url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    def update(self, speaker_host: str | None) -> bool:
        """Ny høyttaleradresse. Gir True hvis iPhone-adressen ble en annen."""
        if speaker_host == self._host:
            return False
        self._host = speaker_host
        if not speaker_host:  # leter etter høyttaleren: behold adressen vi har
            return False
        ip = self._pick(speaker_host)
        if ip == self.ip:
            return False
        self.ip = ip
        return True

    def refresh(self) -> bool:
        """Velg nettkort på nytt med samme høyttaleradresse (nettet kom opp, kabel → Wi-Fi).

        Gir True hvis iPhone-adressen ble en annen. Blir det bare 127.0.0.1 (ingen nett nå),
        beholdes adressen vi har.
        """
        ip = self._pick(self._host)
        if ip == self.ip or not _usable(ip):
            return False
        self.ip = ip
        return True

    def observe(self, state: dict) -> bool:
        """Kalles ved hver statusendring fra høyttaleren. Gir True hvis adressen ble en annen.

        Høyttaleren funnet (eller flyttet) etter oppstart: nettkortet som når den, er det
        mobilen også bruker. Når kontakten kommer (igjen), eller vi bare har 127.0.0.1
        (nettet var ikke oppe ved autostart), velges nettkort på nytt med samme adresse.
        """
        connected = bool(state.get("connected"))
        came_up = connected and not self._connected
        self._connected = connected
        if self.update(state.get("host")):
            return True
        if came_up or not _usable(self.ip):
            return self.refresh()
        return False


def known_hosts(cfg: Config, speaker: Speaker | None) -> tuple[str, ...]:
    """Adresser vi kjenner fra før: lagret adresse, siste som virket og den vi bruker nå."""
    out: list[str] = []
    for h in (cfg["host"], cfg["last_host"], speaker.host if speaker else None):
        if isinstance(h, str) and h.strip() and h.strip() not in out:
            out.append(h.strip())
    return tuple(out)


def remember_host(cfg: Config) -> Callable[[str], None]:
    """Lag en on_connect-funksjon som lagrer siste adresse som virket (bare når den endres)."""
    def on_connect(host: str) -> None:
        if host and cfg["last_host"] != host:
            cfg["last_host"] = host
    return on_connect


class ScanBudget:
    """Hvor ofte letingen i bakgrunnen får søke eget nett over TCP (se ``SCAN_BACKOFF``)."""

    def __init__(self, steps: tuple[float, ...] = SCAN_BACKOFF,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.steps, self.clock = steps, clock
        self.reset()

    def reset(self) -> None:
        self._n, self._next = 0, float("-inf")

    def due(self) -> bool:
        return self.clock() >= self._next

    def spend(self) -> None:
        self._next = self.clock() + self.steps[min(self._n, len(self.steps) - 1)]
        self._n += 1


class SharedSearch:
    """Bare ett nettverkssøk om gangen, også når bakgrunnsletingen og «Finn automatisk»
    vil søke samtidig. Den som kommer sist, bruker svaret fra søket som går hvis det er
    minst like grundig (med TCP-søk), ellers venter den og søker selv etterpå.

    Brukes som ``discover_fn`` i ``search``. Et søk som går, fullføres selv om den som
    ventet gir opp (tidsgrensen i webserveren), og svaret kan da brukes av neste.
    """

    def __init__(self, discover_fn=discover) -> None:
        self._discover = discover_fn
        self._task: asyncio.Future | None = None
        self._scan = False

    async def _run(self, kw: dict) -> tuple[list[Found], list[str]]:
        notes: list[str] = []
        return await self._discover(**{**kw, "report": notes}), notes

    async def __call__(self, **kw) -> list[Found]:
        report = kw.get("report")
        scan = bool(kw.get("scan"))
        while self._task is not None and not self._task.done():
            task, reuse = self._task, self._scan or not scan
            try:
                found, notes = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 – søket som gikk feilet: søk selv
                continue
            if reuse:
                if report is not None:
                    report.extend(notes)
                return list(found)
        self._scan = scan
        self._task = asyncio.ensure_future(self._run(kw))
        self._task.add_done_callback(lambda t: t.cancelled() or t.exception())  # ingen «never retrieved»
        found, notes = await asyncio.shield(self._task)
        if report is not None:
            report.extend(notes)
        return list(found)


def pick(found: list[Found], cfg: Config, name: str | None = None, strict: bool = False) -> Found | None:
    """Beste treff: samme UDN som før går foran. Med ``strict`` (letingen i bakgrunnen) godtas
    bare samme UDN når den er kjent, så appen aldri bytter til en annen Libre-enhet av seg selv."""
    if name:
        found = [f for f in found if name.lower() in f.name.lower()]
    if cfg["udn"]:
        same = [f for f in found if f.udn == cfg["udn"]]
        found = same if strict else (same or found)
    return found[0] if found else None


async def search(cfg: Config, speaker: Speaker | None, name: str | None = None, scan: bool = True,
                 discover_fn=discover, strict: bool = False,
                 on_scan: Callable[[], None] | None = None) -> tuple[Found | None, list[str]]:
    """Ett søk etter høyttaleren: UPnP, kjente adresser direkte over TCP og (med ``scan``)
    eget /24-nett. Gir den beste treffet (se ``pick``) og hva som ble prøvd.
    ``on_scan`` kalles når TCP-søket faktisk er slått på (ikke når PC-ens adresse er ukjent)."""
    notes: list[str] = []
    hints = known_hosts(cfg, speaker)
    lan_ip = _pick_lan_ip(hints[0] if hints else None)
    if scan:
        reason = scan_skip_reason(lan_ip)
        if reason:
            notes.append(reason)
            scan = False
        elif on_scan:
            on_scan()
    found = await discover_fn(hints=hints, report=notes, scan=scan, lan_ip=lan_ip, timeout=3.0)
    return pick(found, cfg, name, strict), notes


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lemus_remote", description="SUMEL – uoffisiell fjernkontroll for Platin SM450 (Lemus HOME)")
    ap.add_argument("--host", default=None, help="høyttalerens IP-adresse (lagres). Uten denne finnes den automatisk")
    ap.add_argument("--auto", action="store_true", help="glem lagret adresse og finn høyttaleren automatisk")
    ap.add_argument("--name", default=None, help="velg høyttaler etter navn når flere finnes, f.eks. \"1500\"")
    ap.add_argument("--port", type=int, default=None, help="porten websiden kjører på (standard 8080)")
    ap.add_argument("--local-only", action="store_true", help="bare tilgjengelig fra denne PC-en")
    ap.add_argument("--no-auth", action="store_true", help="ikke krev tilgangskode fra andre enheter")
    ap.add_argument("--no-scan", action="store_true", help="ikke søk etter høyttaleren over TCP på eget nett")
    ap.add_argument("--no-luci", action="store_true", help="ikke bruk port 7777 (da virker ikke volum og avspilling)")
    ap.add_argument("--config", default=None, help="sti til innstillingsfilen")
    ap.add_argument("--open", action="store_true", help="åpne websiden i nettleseren ved oppstart")
    ap.add_argument("--tray", action="store_true",
                    help="uten konsollvindu, med ikon ved klokken (standard i SUMEL.exe)")
    ap.add_argument("--no-tray", action="store_true", help="kjør i konsollvinduet også i SUMEL.exe")
    ap.add_argument("--background", action="store_true",
                    help="med --tray: ikke åpne nettleseren (brukes når Windows starter)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"SUMEL {__version__}")
    return ap


@dataclass
class Running:
    """Det tray-ikonet trenger å vite når serveren kjører."""
    port: int
    cfg: Config
    lan: LanAddress | None  # None med --local-only
    auth: bool

    @property
    def local_url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def lan_url(self) -> str | None:
        return self.lan.url if self.lan else None

    @property
    def access_code(self) -> str | None:
        return str(self.cfg["access_code"]) if self.auth else None


async def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    await run(args)


async def run(args: argparse.Namespace, on_ready: Callable[[Running], None] | None = None,
              stop: asyncio.Event | None = None, tray: bool = False) -> None:
    """Start høyttalertilkoblingen og webserveren og kjør til ``stop`` settes (eller for alltid).

    ``on_ready`` kalles når webserveren lytter. Feiler oppstarten (porten er opptatt),
    kastes ``OSError`` før ``on_ready``.
    """
    cfg = Config(args.config)
    if cfg["host"] and not cfg["last_host"]:  # eldre innstillingsfil: ta vare på adressen før --auto
        cfg["last_host"] = cfg["host"]
    if args.auto:
        cfg["host"] = None
    if args.host:
        cfg["host"] = args.host
    port = args.port or cfg["port"] or 8080

    speaker: Speaker | None = None

    budget = ScanBudget()
    shared = SharedSearch()
    last_miss = [""]

    async def resolver() -> str | None:
        # Letingen i bakgrunnen kalles igjen og igjen når høyttaleren ikke svarer (av om natten,
        # uten strøm). Med fast adresse holder UPnP og de kjente adressene; uten fast adresse
        # søkes også eget nett over TCP, men stadig sjeldnere (ScanBudget).
        scan = not args.no_scan and not cfg["host"] and budget.due()
        try:
            f, notes = await search(cfg, speaker, args.name, scan=scan, discover_fn=shared,
                                    strict=True, on_scan=budget.spend)
        except Exception:  # noqa: BLE001 – et feilet søk skal ikke stoppe tilkoblingsløkka
            logging.exception("søket etter høyttaleren feilet")
            return None
        if not f:
            miss = "; ".join(notes)
            if miss != last_miss[0]:  # samme melding hvert minutt hjelper ingen
                last_miss[0] = miss
                print("Fant ingen Lemus-høyttaler ennå (" + miss + "). Prøver igjen ...")
                print("  Tips: " + ("tillat SUMEL.exe i brannmuren/antivirusprogrammet" if tray
                                    else "tillat Python i Windows-brannmuren")
                      + ", eller skriv adressen under «Mer» i appen.")
            return None
        last_miss[0] = ""
        print(f"Fant {f.name} på {f.host}")
        cfg.data.update({"host": f.host, "name": f.name, "udn": f.udn})
        cfg.save()
        if speaker:
            await speaker._apply({"name": f.name})
        return f.host

    async def finder() -> dict | None:
        """«Finn automatisk» i appen: ett søk. Webserveren bytter adresse bare ved treff."""
        f, notes = await search(cfg, speaker, args.name, scan=not args.no_scan, discover_fn=shared)
        if not f:
            print("Fant ingen Lemus-høyttaler (" + "; ".join(notes) + "). Beholder adressen.")
            return None
        print(f"Fant {f.name} på {f.host}")
        cfg.data.update({"name": f.name, "udn": f.udn})
        cfg.save()
        if speaker:
            await speaker._apply({"name": f.name})
        return {"host": f.host, "name": f.name}

    remember = remember_host(cfg)

    def on_connect(host: str) -> None:
        budget.reset()
        remember(host)

    def on_learn(learned: dict, disable_mode) -> None:
        cfg.data["learned_formulas"] = learned
        cfg.data["disable_mode"] = disable_mode
        cfg.save()

    speaker = Speaker(cfg["host"], use_luci=not args.no_luci, resolver=resolver, name=cfg["name"],
                      learned=cfg["learned_formulas"], disable_mode=cfg["disable_mode"], on_learn=on_learn,
                      max_volume_step=int(cfg["max_volume_step"] or 25), on_connect=on_connect)
    lan = LanAddress(port, speaker.host)
    web = WebServer(speaker, "127.0.0.1" if args.local_only else "0.0.0.0", port,
                    config=cfg, profiles=ProfileStore(cfg), require_auth=not args.no_auth,
                    lan_url=None if args.local_only else lan.url, finder=finder)

    def on_state(state: dict) -> None:
        # Appen får den nye adressen med neste statusoppdatering.
        if lan.observe(state):
            web.lan_url = lan.url
            print(f"\n  Ny adresse på iPhone: {lan.url}\n", flush=True)

    if not args.local_only:
        speaker.subscribe(on_state)
    try:
        await web.start()
    except OSError:
        await web.stop()
        raise
    speaker.start()

    print(f"\n  SUMEL {__version__} – fjernkontroll for Platin SM450")
    print(f"  På denne PC-en:  http://localhost:{port}")
    if not args.local_only:
        print(f"  På iPhone:       {lan.url}")
        if not args.no_auth:
            print(f"  Tilgangskode:    {cfg['access_code']}   (skrives inn én gang på hver enhet)")
        if tray:
            pass  # installasjonsprogrammet lager brannmurregelen for SUMEL.exe
        elif port == (cfg["port"] or 8080):
            print(f"  {MOBILE_HINT}")
        else:  # skriptet leser porten fra innstillingsfilen, så den må oppgis her
            print(f"  Får ikke mobilen kontakt? Åpne Ledetekst i SUMEL-mappen og skriv:  tillat-mobil.bat {port}")
    print(f"  Høyttaler:       {cfg['host'] or 'finner automatisk ...'}")
    if not cfg["host"] and cfg["last_host"]:
        print(f"                   (prøver først {cfg['last_host']}, der den var sist)")
    print(f"  Innstillinger:   {cfg.path}")
    if tray:
        print("  Avslutt fra ikonet ved klokken (høyreklikk → Avslutt).\n")
    else:
        print("  Avslutt ved å lukke dette vinduet (eller Ctrl+C).\n")
    if args.open:
        webbrowser.open(f"http://localhost:{port}")
    if on_ready:
        on_ready(Running(port, cfg, None if args.local_only else lan, not args.no_auth))
    try:
        await (stop or asyncio.Event()).wait()
    finally:
        await web.stop()
        await speaker.stop()


def use_tray(args: argparse.Namespace) -> bool:
    """Tray-modus med ``--tray``, og alltid i det frosne Windows-programmet (uten ``--no-tray``)."""
    return not args.no_tray and (args.tray or bool(getattr(sys, "frozen", False)))


def cli(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if use_tray(args):
        from . import tray  # pystray/Pillow er valgfrie og hentes bare her
        raise SystemExit(tray.main(args))
    try:
        asyncio.run(main(argv))
    except KeyboardInterrupt:
        print("Avsluttet.")


if __name__ == "__main__":
    cli()
