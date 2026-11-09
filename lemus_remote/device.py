"""Tilkobling til én SM450, med tilstand og kommandoer.

Port 50006 er en delt kringkastingsbuss. Vi ser alle endringer, også de
som gjøres med fjernkontrollen eller Platin-appen. Tilstanden bygges derfor
fra hendelsene enheten sender, og hver kommando bekreftes ved at enheten
melder den nye verdien. At vi selv har skrevet verdien, regnes ikke som
bekreftelse.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import time
from collections import deque

from . import filters
from . import protocol as p

log = logging.getLogger(__name__)

CONTROL_PORT = 50006
LUCI_PORT = 7777

# Funksjoner som er prøvd og bekreftet på vår egen SM450 (se docs/lesetest-resultat.md).
# Listen brukes i dag bare til visning; websiden spør ikke før andre funksjoner brukes (sikker modus mangler, se oppgave #7).
VERIFIED = {"manual_eq", "room_correction", "volume", "transport", "eq_bands", "power", "source"}  # bekreftet 8.11.2026

# Preset 1-3 er tatt ut: på SM450 faller de tilbake til Platin-appens kurve.
# Strøm: "av" med strøm veksle. "På" virker ikke med kommandoer i standby, men
# høyttaleren våkner av 3 s stillhet via DLNA (vekking.py, bekreftet 8.11.2026).
FEATURES = ["power", "source", "volume", "manual_eq", "room_correction", "transport", "eq_bands"]
MAX_BAND_INDEX = p.MAX_BANDS - 1  # plass 3 og høyere ga smell (se protocol.MAX_BANDS)


KEEPALIVE = 30.0  # sekunder mellom ufarlige lesinger som holder forbindelsen i live
WATCHDOG = 45.0   # uten data så lenge: anta at forbindelsen er død og koble til igjen
POLL = 10.0       # sekunder mellom lette lesinger av strøm, kilde/volum, egen EQ og romkorreksjon
MUTE_CONFIRM = 2.0  # sekunder vi venter på at boks 0x3F bekrefter demping fra høyttaleren
MUTE_RAMP_STEP = 5  # volum-reserve: skru opp igjen i steg på høyst så mye ...
MUTE_RAMP_DELAY = 0.12  # ... med så mange sekunder mellom hvert steg
FOLLOW_DELAY = 0.6  # vent litt etter en kommando fra en annen klient før vi leser på nytt
# Lesekommandoer (class, opcode) – slike fra andre klienter trenger ingen oppfølging
READ_OPS = {(0x02, 0x0D), (0x02, 0x01), (0x03, 0x05), (0x07, 0x02), (0x08, 0x05), (0x08, 0x04),
            (0x07, 0x09), (0x02, 0x89), (0x07, 0x0C)}


class CommandError(Exception):
    pass


class Speaker:
    def __init__(self, host: str | None, control_port: int = CONTROL_PORT, luci_port: int = LUCI_PORT,
                 use_luci: bool = True,
                 resolver: Callable[[], Awaitable[str | None]] | None = None,
                 name: str | None = None,
                 learned: dict[str, str] | None = None,
                 disable_mode: str | None = None,
                 on_learn: Callable[[dict[str, str], str | None], None] | None = None,
                 max_volume_step: int = 25,
                 on_connect: Callable[[str], None] | None = None) -> None:
        self.host = host
        # Kalles med adressen hver gang kontrollforbindelsen (50006) lykkes. Brukes til å
        # huske siste adresse som virket, så den aldri går tapt.
        self.on_connect = on_connect
        # Filterformler: Parametric er bekreftet. Andre typer læres fra Platin-appen.
        self.formulas: dict[int, str] = dict(filters.BUILTIN_FORMULAS)
        for k, v in (learned or {}).items():
            if int(k) in filters.CANDIDATES and v in filters.CANDIDATES[int(k)]:
                self.formulas[int(k)] = v
        self.disable_mode = disable_mode  # "unity" eller "same" når det er lært
        self.on_learn = on_learn
        self.max_volume_step = max_volume_step
        self.diag: deque = deque(maxlen=300)
        # Kalles når høyttaleren ikke svarer på adressen sin flere ganger på rad.
        # Brukes til å finne den igjen hvis ruteren har gitt den ny IP-adresse.
        self.resolver = resolver
        self._failures = 0
        from .vekking import wake
        self.waker = wake  # kan byttes ut i tester
        self.control_port = control_port
        self.luci_port = luci_port
        self.use_luci = use_luci
        self.keepalive = KEEPALIVE
        self.poll = POLL
        self._own: deque = deque(maxlen=64)   # (tid, ramme) for egne kommandoer, så vi ikke følger opp oss selv
        self._follow_task: asyncio.Task | None = None
        self._follow_bands = False
        self.watchdog = WATCHDOG
        self.state: dict = {
            "connected": False,
            "luci_connected": False,
            "power": None,
            "source": None,
            "sources": [],
            "volume": None,
            "manual_eq": None,
            "room_correction": None,
            "preset": None,
            "play_state": None,
            "bands": [],
            "versions": None,
            "name": name,
            "host": host,
            "searching": host is None,
            "verified": sorted(VERIFIED),
            "features": FEATURES,
            "eq_types": {},
            "can_disable_band": False,
            "room_curve_raw": None,
            "learn_events": [],
            "mute": None,          # True/False når kjent (boks 0x3F eller volum-reserven), ellers None
            "mute_mode": None,     # "native" (høyttalerens egen demping) | "volume" (volum 0) | None
            "power_busy": None,    # "on"/"off" mens strømmen settes, ellers None
        }
        # Demping: None = ikke prøvd ennå, True = høyttaleren bekreftet MUTE/UNMUTE, False = bruk volum 0
        self.native_mute: bool | None = None
        self.mute_confirm = MUTE_CONFIRM
        self.mute_ramp_delay = MUTE_RAMP_DELAY
        self._mute_level: int | None = None   # volumet før volum-demping
        self._mute_seen = 0                   # antall meldinger fra boks 0x3F
        self._box_mute: bool | None = None    # siste verdi boks 0x3F meldte
        self._ramping = False                 # skrur opp igjen etter volum-demping
        self._mute_unconfirmed = False        # vi sendte MUTE uten bekreftelse (kan likevel ha virket)
        self._power_calls = 0                 # set_power-kall som pågår (power_busy fjernes når siste er ferdig)
        self._mute_lock = asyncio.Lock()
        self._refresh_eq_types()
        self._listeners: set[Callable[[dict], None]] = set()
        self._changed = asyncio.Condition()
        self._ctl: asyncio.StreamWriter | None = None
        self._luci: asyncio.StreamWriter | None = None
        self._cmd_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        # Svar på get_filters kommer som én ramme per bånd. Vi samler en "bølge" og
        # publiserer den samlet når det har vært stille en liten stund.
        self._burst: dict[int, bytes] | None = None
        self._burst_timer: asyncio.Task | None = None
        self._bands_version = 0

    # ------------------------------------------------------------ tilstand

    def subscribe(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    async def _apply(self, patch: dict) -> None:
        if patch.get("power") is False and self.state.get("power") is True:
            self._source_before_standby = self.state.get("source")
        if ("volume" in patch and self.state.get("mute_mode") == "volume" and not self._ramping
                and patch["volume"] not in (0, None)):
            # Volumet ble skrudd opp et annet sted (fjernkontroll, TV): da er ikke lyden dempet lenger.
            patch = {**patch, "mute": False, "mute_mode": None}
            self._mute_level = None
        changed = {k: v for k, v in patch.items() if self.state.get(k) != v}
        if not changed:
            return
        self.state.update(changed)
        for cb in list(self._listeners):
            try:
                cb(dict(self.state))
            except Exception:  # en feil hos en lytter skal ikke stoppe tilkoblingen
                log.exception("lytter feilet")
        async with self._changed:
            self._changed.notify_all()

    async def _wait_for(self, predicate: Callable[[dict], bool], timeout: float,
                        resend: bytes | None = None, every: float = 0.5) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate(self.state):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if resend is not None:
                await self._send_control(resend)
            try:
                async with self._changed:
                    await asyncio.wait_for(self._changed.wait(), min(every, remaining))
            except asyncio.TimeoutError:
                pass
        return True

    # ------------------------------------------------------------ tilkobling

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._control_loop()))
        if self.use_luci:
            self._tasks.append(asyncio.create_task(self._luci_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def _ensure_host(self) -> bool:
        """Uten adresse: spør resolveren (automatisk oppdaging) til vi har en."""
        if self.host:
            return True
        await self._apply({"searching": True})
        new_host = await self.resolver() if self.resolver else None
        if new_host:
            self.host = new_host
            await self._apply({"host": new_host, "searching": False})
            return True
        return False

    async def set_host(self, host: str | None) -> None:
        """Bytt høyttaleradresse (None = finn automatisk). Kobler til på nytt."""
        self.host = host or None
        await self._apply({"host": self.host, "searching": self.host is None, "connected": False})
        for w in (self._ctl, self._luci):
            if w:
                w.close()

    async def _control_loop(self) -> None:
        while True:
            if not await self._ensure_host():
                await asyncio.sleep(5)
                continue
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.control_port), 10)
            except (OSError, asyncio.TimeoutError) as exc:
                log.info("50006: kunne ikke koble til %s (%s), prøver igjen", self.host, exc)
                self._failures += 1
                if self.resolver and self._failures >= 3:
                    self._failures = 0
                    new_host = await self.resolver()
                    if new_host and new_host != self.host:
                        log.info("fant høyttaleren på ny adresse %s", new_host)
                        self.host = new_host
                        await self._apply({"host": new_host})
                await asyncio.sleep(3)
                continue
            self._failures = 0
            self._ctl = writer
            await self._apply({"connected": True})
            log.info("50006: tilkoblet %s", self.host)
            if self.on_connect:
                try:
                    self.on_connect(self.host)
                except Exception:  # lagringsfeil skal ikke stoppe tilkoblingen
                    log.exception("on_connect feilet")
            asyncio.create_task(self._initial_queries())
            frames = p.FrameReader()
            # En forbindelse som bare lytter, merker aldri at høyttaleren er borte. Vi leser
            # derfor romkorreksjon (meldes aldri av seg selv) og strøm jevnlig.
            # Lette lesinger hvert POLL-sekund fanger opp endringer fra fjernkontroll, TV og andre apper
            # (romkorreksjon og egen EQ meldes aldri av seg selv).
            ka = asyncio.create_task(self._keepalive(writer, p.GET_ROOM_CORRECTION, p.GET_POWER,
                                                     p.GET_SOURCES, p.GET_MANUAL_EQ, interval=self.poll))
            try:
                while True:
                    data = await asyncio.wait_for(reader.read(4096), self.watchdog)
                    if not data:
                        break
                    for frame in frames.feed(data):
                        self._log_frame("50006", frame)
                        band = p.decode_filter(frame)
                        if band:
                            self._collect_band(*band)
                            continue
                        if len(frame) > 5 and frame[2] == 0 and frame[3:5] == b"\x07\x0a":
                            await self._apply({"room_curve_raw": list(frame[5:]),
                                               "room_curve_time": time.strftime("%H:%M:%S")})
                            continue
                        if p.decode_delete_ack(frame) is not None:
                            if not self._cmd_lock.locked():  # sletting fra en annen klient
                                asyncio.create_task(self._quiet_refresh())
                            continue
                        patch = p.decode_control(frame)
                        if patch:
                            await self._apply(patch)
            except asyncio.TimeoutError:
                log.info("50006: ingen svar på %.0f s, kobler til igjen", self.watchdog)
            except (OSError, asyncio.IncompleteReadError) as exc:
                log.info("50006: forbindelsen brutt (%s)", exc)
            finally:
                ka.cancel()
                self._ctl = None
                writer.close()
                await self._apply({"connected": False})
            await asyncio.sleep(2)

    async def _initial_queries(self) -> None:
        # 02 20 svarer ikke på SM450. 02 01 gir både kildeliste og volum (02 21).
        try:
            for frame in (p.GET_POWER, p.GET_SOURCES, p.GET_MANUAL_EQ,
                          p.GET_ROOM_CORRECTION, p.GET_ACTIVE_PRESET, p.GET_VERSIONS,
                          p.GET_ROOM_CURVE):  # romkurven er bare en lesing (Platin-appen gjør det samme)
                await self._send_control(frame)
                await asyncio.sleep(0.2)
            await self._quiet_refresh()
        except (CommandError, OSError):
            pass  # forbindelsen falt underveis. Neste tilkobling spør på nytt.

    # ------------------------------------------------------------ EQ-bånd

    def _collect_band(self, ix: int, raw: bytes) -> None:
        if self._burst is None:
            self._burst = {}
        self._burst[ix] = raw
        if self._burst_timer:
            self._burst_timer.cancel()
        self._burst_timer = asyncio.create_task(self._commit_burst_later())

    async def _commit_burst_later(self, delay: float = 0.8) -> None:
        await asyncio.sleep(delay)
        burst, self._burst = self._burst or {}, None
        await self._publish_bands(burst)

    async def _publish_bands(self, raw_bands: dict[int, bytes]) -> None:
        bands = []
        for ix, raw in sorted(raw_bands.items()):
            info = p.parse_band(raw)
            if info:
                info.pop("b2", None)
                bands.append({"ix": ix, "editable": info["type_code"] in self.formulas and ix <= MAX_BAND_INDEX, **info})
        self._bands_version += 1
        await self._apply({"bands": bands, "bands_version": self._bands_version})

    async def refresh_bands(self, timeout: float = 2.5) -> None:
        """Les alle bånd på nytt. En tom liste gir ikke noe svar, så stillhet betyr ingen bånd."""
        before = self._bands_version
        await self._send_control(p.get_filters(0))
        if not await self._wait_for(lambda s: self._bands_version != before, timeout):
            if self._burst is None:
                await self._publish_bands({})

    async def _quiet_refresh(self) -> None:
        try:
            await self.refresh_bands()
        except (CommandError, OSError):
            pass

    async def _refresh_until(self, cond: Callable[[], bool], tries: int = 4) -> bool:
        """Les bånd på nytt til betingelsen holder. Et forsinket svar fra en tidligere
        lesing kan komme etter en endring, så én lesing alene er ikke nok."""
        for _ in range(tries):
            await self.refresh_bands()
            if cond():
                return True
            await asyncio.sleep(0.3)
        return False

    def _band(self, ix: int) -> dict | None:
        return next((b for b in self.state["bands"] if b["ix"] == ix), None)

    # ------------------------------------------------------------ filtertyper og læring

    def _refresh_eq_types(self) -> None:
        types = {}
        for code, name in filters.TYPE_NAMES.items():
            types[str(code)] = {"name": name, "editable": code in self.formulas,
                                "has_q": code in filters.HAS_Q, "has_gain": code in filters.HAS_GAIN,
                                "learned": code in self.formulas and code not in filters.BUILTIN_FORMULAS}
        self.state["eq_types"] = types
        self.state["can_disable_band"] = True  # 08 06 status 01/02 (opptak 8.11.2026)

    def _log_frame(self, where: str, frame: bytes) -> None:
        self.diag.append({"t": time.strftime("%H:%M:%S"), "port": where, "hex": frame.hex(" ")})

    def _learn_from(self, frame: bytes) -> None:
        """Se på en skrivekommando fra Platin-appen og lær formelen hvis den treffer."""
        w = p.parse_write_band(frame)
        if not w:
            return
        t = w["type_code"]
        event = None
        if w["enabled"] is False:
            if self.disable_mode is None:
                if filters.close(w["coeffs"], filters.UNITY):
                    self.disable_mode = "unity"
                elif t in self.formulas and filters.close(
                        w["coeffs"], filters.coefficients(t, self.formulas[t], w["freq"], w["q"], w["gain"])):
                    self.disable_mode = "same"
                if self.disable_mode:
                    event = f"Lært hvordan et bånd slås av ({self.disable_mode})"
        elif t not in self.formulas:
            formula = filters.match_formula(t, w["freq"], w["q"], w["gain"], w["coeffs"])
            if formula:
                self.formulas[t] = formula
                event = f"Lært filtertypen «{filters.TYPE_NAMES.get(t, t)}» ({formula})"
            else:
                event = f"Så «{filters.TYPE_NAMES.get(t, t)}» fra Platin-appen, men ingen kjent formel passet"
        if event:
            log.info(event)
            self._refresh_eq_types()
            events = (self.state["learn_events"] + [{"t": time.strftime("%H:%M:%S"), "text": event}])[-10:]
            patch = {"learn_events": events, "eq_types": dict(self.state["eq_types"])}
            try:
                asyncio.get_running_loop().create_task(self._apply(patch))
            except RuntimeError:  # ingen løkke (enhetstester): oppdater direkte
                self.state.update(patch)
            if self.on_learn:
                self.on_learn({str(k): v for k, v in self.formulas.items() if k not in filters.BUILTIN_FORMULAS},
                              self.disable_mode)

    def band_frame(self, ix: int, type_code: int, freq: int, q: float | None, gain: float | None,
                   enabled: bool = True) -> bytes:
        """Bygg skrivekommandoen for ett bånd. Avviser typer vi ikke kjenner formelen for."""
        if type_code not in self.formulas:
            raise CommandError(f"«{filters.TYPE_NAMES.get(type_code, type_code)}» kan ikke skrives ennå. "
                               "Lagre ett slikt filter i Platin-appen mens SUMEL kjører, så lærer appen det.")
        q = (q or 0.71) if type_code in filters.HAS_Q else None
        gain = (gain if gain is not None else 0.0) if type_code in filters.HAS_GAIN else None
        q_param = 1.0 if type_code in filters.SHELVING else q  # Platin lagrer Q 1.00 for shelving
        params = p.encode_band_params(type_code, int(freq), None if q_param is None else round(q_param, 2),
                                      None if gain is None else round(gain, 2), enabled)
        coeffs = filters.coefficients(type_code, self.formulas[type_code], freq, q, gain)
        return p.write_band_raw(ix, coeffs, params)

    @staticmethod
    def _same(b: dict, type_code: int, freq: int, q, gain, enabled: bool) -> bool:
        if type_code in filters.SHELVING:
            q = None
        return (b["type_code"] == type_code and b["freq"] == int(freq)
                and (q is None or b["q"] == round(q, 2)) and (gain is None or b["gain"] == round(gain, 2))
                and b["enabled"] is enabled)

    async def write_band(self, ix: int | None, type_code: int, freq: int, q: float | None,
                         gain: float | None, enabled: bool = True) -> int:
        """Endre bånd ``ix``, eller lag et nytt når ``ix`` er None. Bekreftes ved tilbakelesing."""
        if ix is None:
            await self.refresh_bands()
            used = {b["ix"] for b in self.state["bands"]}
            # plassene er faste og flyttes ikke ved sletting: bruk den laveste ledige, slik at
            # sletting + angre gir båndet tilbake på samme plass og plassene ikke «kryper» oppover
            free = [i for i in range(MAX_BAND_INDEX + 1) if i not in used]
            if not free:
                raise CommandError(f"SM450 tar høyst {p.MAX_BANDS} EQ-bånd. Slett ett før du lager et nytt.")
            ix = free[0]
        elif self._band(ix) is None:
            raise CommandError(f"bånd {ix + 1} finnes ikke")
        if ix > MAX_BAND_INDEX:
            raise CommandError(f"bånd {ix + 1} kan ikke endres: SM450 tar høyst {p.MAX_BANDS} bånd. Slett det.")
        frame = self.band_frame(ix, type_code, freq, q, gain, True)
        q_n = (q or 0.71) if type_code in filters.HAS_Q else None
        g_n = (gain if gain is not None else 0.0) if type_code in filters.HAS_GAIN else None
        async with self._cmd_lock:
            await self._send_control(frame)
            await asyncio.sleep(0.5)
            ok = await self._refresh_until(
                lambda: (b := self._band(ix)) is not None and self._same(b, type_code, freq, q_n, g_n, True))
            if not ok:
                raise CommandError("høyttaleren bekreftet ikke endringen av båndet")
        if not enabled:
            await self.set_band_enabled(ix, False)
        return ix

    async def set_band(self, ix: int, freq: int, q: float, gain: float) -> None:
        band = self._band(ix)
        if band is None:
            raise CommandError(f"bånd {ix + 1} finnes ikke")
        await self.write_band(ix, band["type_code"], freq, q, gain, band["enabled"] is not False)

    async def add_band(self, freq: int, q: float, gain: float, type_code: int = filters.PARAMETRIC) -> int:
        return await self.write_band(None, type_code, freq, q, gain)

    async def set_band_enabled(self, ix: int, enabled: bool) -> None:
        """Slå ett bånd av eller på uten å endre verdiene (08 06, status 01/02)."""
        if self._band(ix) is None:
            raise CommandError(f"bånd {ix + 1} finnes ikke")
        async with self._cmd_lock:
            await self._send_control(p.set_band_state(ix, p.BAND_ON if enabled else p.BAND_OFF))
            await asyncio.sleep(0.5)
            if not await self._refresh_until(lambda: (b := self._band(ix)) is not None and b["enabled"] is enabled):
                raise CommandError("høyttaleren bekreftet ikke av/på for båndet")

    async def apply_bands(self, target: list[dict]) -> None:
        """Sett alle skrivbare bånd til en profil: endre eksisterende i rekkefølge, slett de
        som er til overs og lag nye ved behov. Bånd av typer vi ikke kan skrive, røres ikke."""
        norm = []
        for t in target:
            code = int(t.get("type", filters.PARAMETRIC))
            if code not in self.formulas:
                raise CommandError(f"profilen bruker «{filters.TYPE_NAMES.get(code, code)}», som ikke kan skrives ennå")
            norm.append((code, int(t["freq"]), t.get("q"), t.get("gain"), bool(t.get("enabled", True))))
            p.encode_band_params(code, int(t["freq"]), None if t.get("q") is None else float(t["q"]),
                                 None if t.get("gain") is None else float(t["gain"]))
        if len(norm) > p.MAX_BANDS:
            raise CommandError(f"profilen har {len(norm)} bånd, men SM450 tar høyst {p.MAX_BANDS}")
        await self.refresh_bands()
        if len(self.state["bands"]) - len([b for b in self.state["bands"] if b["editable"]]) + len(norm) > p.MAX_BANDS:
            raise CommandError(f"profilen og båndene som ikke kan skrives blir til sammen mer enn {p.MAX_BANDS} bånd")
        existing = [b for b in self.state["bands"] if b["editable"]]
        for b, (code, freq, q, gain, en) in zip(existing, norm):
            qn = (q or 0.71) if code in filters.HAS_Q else None
            gn = (gain if gain is not None else 0.0) if code in filters.HAS_GAIN else None
            if not self._same(b, code, freq, qn, gn, en):
                await self.write_band(b["ix"], code, freq, q, gain, en)
        for b in existing[len(norm):]:
            await self.delete_band(b["ix"])
        for code, freq, q, gain, en in norm[len(existing):]:
            ix = await self.write_band(None, code, freq, q, gain)
            if not en:
                await self.write_band(ix, code, freq, q, gain, False)

    async def read_room_curve(self) -> list[int] | None:
        """Les romkalibreringskurven (07 09 -> 07 0a, 143 byte). Formatet er ikke dekodet."""
        before = self.state.get("room_curve_time")
        await self._send_control(p.GET_ROOM_CURVE)
        await self._wait_for(lambda s: s.get("room_curve_time") != before, 4)
        return self.state.get("room_curve_raw")

    async def delete_band(self, ix: int) -> None:
        if self._band(ix) is None:
            raise CommandError(f"bånd {ix} finnes ikke")
        async with self._cmd_lock:
            await self._send_control(p.delete_band(ix))
            await asyncio.sleep(0.8)
            if not await self._refresh_until(lambda: self._band(ix) is None):
                raise CommandError("høyttaleren bekreftet ikke slettingen")

    async def _luci_loop(self) -> None:
        while True:
            if not self.host:
                await asyncio.sleep(2)
                continue
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.luci_port), 10)
            except (OSError, asyncio.TimeoutError) as exc:
                log.info("7777: kunne ikke koble til (%s), prøver igjen", exc)
                await asyncio.sleep(5)
                continue
            local_ip = writer.get_extra_info("sockname")[0]
            writer.write(p.luci_register(local_ip))
            await writer.drain()
            await asyncio.sleep(0.15)
            self._luci = writer
            await self._apply({"luci_connected": True})
            writer.write(p.LUCI_GET_VOLUME)
            writer.write(p.LUCI_GET_MUTE)  # demping (boks 0x3F); svarer den ikke, forblir mute None
            frames = p.LuciReader()
            ka = asyncio.create_task(self._keepalive(writer, p.LUCI_GET_VOLUME))
            try:
                while True:
                    data = await asyncio.wait_for(reader.read(4096), self.watchdog)
                    if not data:
                        break
                    for box, _status, payload in frames.feed(data):
                        if box == 0x0070:  # 50006-kommandoer fra andre klienter (Platin-appen)
                            for inner in p.FrameReader().feed(payload):
                                self._log_frame("tunnel", inner)
                                self._learn_from(inner)
                                self._on_foreign(inner)
                            continue
                        patch = p.decode_luci(box, payload)
                        if patch and box == p.BOX_MUTE:
                            await self._on_box_mute(patch["mute"])
                        elif patch:
                            await self._apply(patch)
            except asyncio.TimeoutError:
                log.info("7777: ingen svar på %.0f s, kobler til igjen", self.watchdog)
            except (OSError, asyncio.IncompleteReadError) as exc:
                log.info("7777: forbindelsen brutt (%s)", exc)
            finally:
                ka.cancel()
                self._luci = None
                writer.close()
                await self._apply({"luci_connected": False})
            await asyncio.sleep(3)

    def _on_foreign(self, frame: bytes) -> None:
        """En annen klient (Platin-appen, Lemus-appen) sendte en kommando via høyttaleren. Er det en
        endring, leser vi den berørte tilstanden på nytt litt etterpå, så appen viser det samme."""
        if len(frame) < 5:
            return
        op = (frame[3], frame[4])
        if op in READ_OPS:
            return
        now = time.monotonic()
        if any(now - t < 3.0 and f == bytes(frame) for t, f in self._own):
            return  # vår egen kommando, speilet tilbake
        if frame[3] == 0x08:
            self._follow_bands = True
        if self._follow_task and not self._follow_task.done():
            return  # en oppfølging er allerede planlagt
        try:
            self._follow_task = asyncio.get_running_loop().create_task(self._follow_up())
        except RuntimeError:
            pass

    async def _follow_up(self) -> None:
        await asyncio.sleep(FOLLOW_DELAY)
        bands, self._follow_bands = self._follow_bands, False
        try:
            for frame in (p.GET_POWER, p.GET_SOURCES, p.GET_MANUAL_EQ, p.GET_ROOM_CORRECTION):
                await self._send_control(frame)
                await asyncio.sleep(0.1)
            if bands:
                await self._send_control(p.GET_ACTIVE_PRESET)
                await self._quiet_refresh()
        except (CommandError, OSError):
            pass

    async def _keepalive(self, writer: asyncio.StreamWriter, *frames: bytes, interval: float | None = None) -> None:
        try:
            while True:
                await asyncio.sleep(interval or self.keepalive)
                for frame in frames:
                    writer.write(frame)
                await writer.drain()
        except (OSError, ConnectionError):
            pass

    async def _send_control(self, frame: bytes) -> None:
        if self._ctl is None:
            raise CommandError("ikke tilkoblet høyttaleren (port 50006)")
        self._own.append((time.monotonic(), bytes(frame)))
        self._ctl.write(frame)
        await self._ctl.drain()

    async def _send_luci(self, frame: bytes) -> None:
        if self._luci is None:
            raise CommandError("ikke tilkoblet høyttaleren (port 7777)")
        self._luci.write(frame)
        await self._luci.drain()

    # ------------------------------------------------------------ kommandoer

    async def set_manual_eq(self, on: bool) -> None:
        async with self._cmd_lock:
            await self._send_control(p.set_manual_eq(on))
            if not await self._wait_for(lambda s: s["manual_eq"] is on, 3, resend=p.GET_MANUAL_EQ):
                raise CommandError("høyttaleren bekreftet ikke manuell EQ")

    async def set_room_correction(self, on: bool) -> None:
        async with self._cmd_lock:
            await self._send_control(p.set_room_correction(on))
            # Romkorreksjon meldes aldri av seg selv, så vi spør til verdien endres.
            if not await self._wait_for(lambda s: s["room_correction"] is on, 4,
                                        resend=p.GET_ROOM_CORRECTION, every=0.4):
                raise CommandError("høyttaleren bekreftet ikke romkorreksjon")

    async def select_preset(self, n: int) -> None:
        frame = p.select_preset(n)  # avviser alt annet enn 1-3
        async with self._cmd_lock:
            await self._send_control(frame)
            if not await self._wait_for(lambda s: s["preset"] == n, 3, resend=p.GET_ACTIVE_PRESET):
                raise CommandError("høyttaleren bekreftet ikke preset")

    async def select_source(self, ix: int) -> None:
        if ix not in {s["ix"] for s in self.state["sources"]}:
            raise CommandError(f"ukjent kilde {ix}")
        async with self._cmd_lock:
            await self._send_control(p.select_source(ix))
            if not await self._wait_for(lambda s: s["source"] == ix, 3, resend=p.GET_SOURCES, every=1.0):
                raise CommandError("høyttaleren bekreftet ikke kildebytte")

    async def set_power(self, on: bool) -> None:
        """Slå på/av. Mens det pågår, står ``power_busy`` ("on"/"off") i tilstanden, så alle
        enheter kan vise «Vekker …» og skjule knappen. Fjernes etterpå, også ved feil."""
        self._power_calls += 1
        await self._apply({"power_busy": "on" if on else "off"})
        try:
            await self._set_power(on)
        finally:
            self._power_calls -= 1
            if self._power_calls == 0:
                await self._apply({"power_busy": None})

    async def _set_power(self, on: bool) -> None:
        async with self._cmd_lock:
            # Strøm er en veksler, så vi må vite nåværende tilstand først.
            self.state["power"] = None
            await self._send_control(p.GET_POWER)
            if not await self._wait_for(lambda s: s["power"] is not None, 3):
                raise CommandError("fikk ikke lest strømstatus")
            if self.state["power"] is on:
                return
            if not on:
                self._source_before_standby = self.state["source"]
                await self._send_control(p.TOGGLE_POWER)
                if not await self._wait_for(lambda s: s["power"] is False, 5, resend=p.GET_POWER, every=1.0):
                    raise CommandError("høyttaleren bekreftet ikke at den slo seg av")
                return
            # PÅ: styrebrikken ignorerer kommandoer i standby. Vi spiller 3 s stillhet via DLNA,
            # så forsterkeren får signal og våkner (testet 8.11.2026).
            local_ip = self._ctl.get_extra_info("sockname")[0] if self._ctl else None
            if not local_ip:
                raise CommandError("ikke tilkoblet høyttaleren")
            try:
                await self.waker(self.host, local_ip)
            except Exception as exc:  # noqa: BLE001
                raise CommandError(f"kunne ikke vekke høyttaleren via DLNA ({exc})") from exc
            if not await self._wait_for(lambda s: s["power"] is True, 8, resend=p.GET_POWER, every=1.0):
                raise CommandError("høyttaleren våknet ikke")
        # Vekkingen bytter til Streaming. Gå tilbake til kilden som var i bruk før standby.
        previous = getattr(self, "_source_before_standby", None)
        if previous and previous != self.state["source"] and previous in {x["ix"] for x in self.state["sources"]}:
            try:
                await self.select_source(previous)
            except CommandError:
                pass

    async def set_volume(self, level: int, force: bool = False) -> None:
        frame = p.luci_set_volume(level)
        current = self.state["volume"]
        if current is None and not force:
            raise CommandError("volumet er ikke lest fra høyttaleren ennå")
        if current is not None and abs(level - current) > self.max_volume_step and not force:
            raise CommandError(f"for stort volumhopp ({current} → {level}). Maks {self.max_volume_step} om gangen")
        async with self._cmd_lock:
            if self.state["volume"] == level:
                return
            await self._send_luci(frame)
            if not await self._wait_for(lambda s: s["volume"] == level, 3):
                raise CommandError("høyttaleren bekreftet ikke volum")

    async def transport(self, action: str) -> None:
        if action not in p.TRANSPORT_ACTIONS:
            raise CommandError(f"ukjent handling {action}")
        async with self._cmd_lock:
            await self._send_luci(p.luci_transport(action))

    # ------------------------------------------------------------ demping (mute)

    async def _on_box_mute(self, on: bool) -> None:
        """Melding fra boks 0x3F ('MUTE'/'UNMUTE'), fra oss selv eller fra fjernkontrollen."""
        self._mute_seen += 1
        self._box_mute = on
        if self.state.get("mute_mode") == "volume":
            return  # vi har dempet med volum 0: det gjelder til vi skrur opp igjen
        await self._apply({"mute": on, "mute_mode": "native" if on else None})

    async def set_mute(self, on: bool) -> None:
        """Demp eller slå på lyden igjen.

        Først høyttalerens egen demping (LUCI boks 0x3F, 'MUTE'/'UNMUTE'), bekreftet ved at boksen
        melder den nye verdien innen ``mute_confirm`` sekunder. At boksen tar imot kommandoen, er ikke
        bekreftet på vår SM450. Uten bekreftelse brukes en reserve: husk volumet og sett volum 0
        (nedover er alltid trygt). Når lyden slås på igjen, skrus volumet opp i små steg
        (høyst ``MUTE_RAMP_STEP`` og aldri mer enn volumvakten tillater per steg). Det som virket,
        huskes, så neste gang går rett på riktig metode.
        """
        on = bool(on)
        async with self._mute_lock:
            mode = self.state.get("mute_mode")
            if on:
                if self.state.get("mute") is True:
                    return
                if self.native_mute is not False and await self._native_mute(True):
                    return
                await self._mute_by_volume()
                return
            if mode == "volume":
                await self._unmute_by_volume()
                return
            if self.state.get("mute") is False:
                return
            if await self._native_mute(False):
                return
            if self.state.get("mute") is True:
                raise CommandError("høyttaleren bekreftet ikke at lyden ble slått på igjen")
            # ukjent tilstand og ingen bekreftelse: ingenting kjent å oppheve

    async def _native_mute(self, on: bool) -> bool:
        before = self._mute_seen
        async with self._cmd_lock:
            await self._send_luci(p.luci_set_mute(on))
            ok = await self._wait_for(lambda s: self._mute_seen != before and self._box_mute is on,
                                      self.mute_confirm)
        if ok:
            self.native_mute = True
            self._mute_unconfirmed = False
            await self._apply({"mute": on, "mute_mode": "native" if on else None})
            return ok
        if on:
            # Høyttaleren kan ha dempet uten å melde det. Da må UNMUTE sendes når lyden slås på igjen,
            # ellers forblir den stum selv om volumet skrus opp.
            self._mute_unconfirmed = True
        if self.native_mute is None:
            log.info("demping: boks 0x3F bekreftet ikke, bruker volum 0 som reserve")
            self.native_mute = False
        return ok

    async def _mute_by_volume(self) -> None:
        level = self.state.get("volume")
        if level is None:
            raise CommandError("kunne ikke dempe: volumet er ikke lest fra høyttaleren ennå")
        try:
            await self.set_volume(0, force=True)  # ned er alltid trygt, forbi volumvakten
        except CommandError as exc:
            raise CommandError(f"kunne ikke dempe lyden ({exc})") from exc
        self._mute_level = level
        await self._apply({"mute": True, "mute_mode": "volume"})

    async def _unmute_by_volume(self) -> None:
        goal = self._mute_level or 0
        if self._box_mute is True or self._mute_unconfirmed:
            # høyttaleren meldte egen demping i mellomtiden, eller vi sendte en ubekreftet MUTE: opphev den
            try:
                async with self._cmd_lock:
                    await self._send_luci(p.luci_set_mute(False))
            except CommandError:
                pass
            self._mute_unconfirmed = False
        step = max(1, min(MUTE_RAMP_STEP, self.max_volume_step))
        expected = self.state.get("volume")
        self._ramping = True
        try:
            while expected is not None and expected < goal:
                if self.state.get("volume") != expected:
                    break  # volumet ble endret et annet sted underveis: det gjelder
                nxt = min(goal, expected + step)
                try:
                    await self.set_volume(nxt)
                except CommandError as exc:
                    raise CommandError(f"kunne ikke skru opp lyden igjen ({exc})") from exc
                expected = nxt
                if expected < goal:
                    await asyncio.sleep(self.mute_ramp_delay)
        finally:
            self._ramping = False
        self._mute_level = None
        await self._apply({"mute": False, "mute_mode": None})
