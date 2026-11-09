"""HTTP-server for SUMEL (pakken lemus_remote): webapp, JSON-API og server-sent events.

Bruker bare standardbiblioteket, så ingen pakker må installeres.

Tilgang: forespørsler fra PC-en selv (localhost) er alltid tillatt. Andre enheter på
lokalnettet (for eksempel iPhone) må oppgi tilgangskoden én gang. Den lagres så i en
informasjonskapsel. Koden vises i programvinduet og under «Mer» i appen på PC-en.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from collections.abc import Awaitable, Callable
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from . import eqfit
from . import qr
from . import protocol as p
from .config import VOLUME_STEP_MAX, VOLUME_STEP_MIN, Config
from .device import MAX_BAND_INDEX, CommandError, Speaker
from .filters import PARAMETRIC
from .profiles import ProfileStore, validate_bands
from .discovery import close_server

log = logging.getLogger(__name__)

# Filer som kan hentes (navn -> innholdstype). Alt annet gir 404.
STATIC_FILES = {
    "manifest.webmanifest": "application/manifest+json",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "login.html": "text/html; charset=utf-8",
}
PUBLIC = {"/login.html", "/api/login", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png",
          "/apple-touch-icon.png"}

STATUS_TEXT = {200: "OK", 302: "Found", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
               405: "Method Not Allowed", 409: "Conflict", 429: "Too Many Requests", 502: "Bad Gateway",
               503: "Service Unavailable"}
COOKIE = "lemus_key"
SSE_PING = 10.0  # sekunder mellom «event: ping» i /api/events, så siden merker en død forbindelse
QR_MAX_TEXT = qr.capacity(qr.MAX_VERSION)  # byte (UTF-8) i /api/qr.svg?text= – det en QR-kode versjon 10-M rommer (213)
FIND_TIMEOUT = 20.0  # «Finn automatisk» gir opp etter så mange sekunder og beholder adressen

# Finner: ett søk etter høyttaleren. Gir {"host", "name"} eller None.
Finder = Callable[[], Awaitable[dict | None]]

# Ugyldig input i en forespørsel gir 400 med feiltekst, aldri en lukket forbindelse.
BAD_INPUT = (KeyError, TypeError, ValueError, AttributeError, OverflowError)


def _slot(value) -> int:
    """Plassnummer (ix) fra JSON: bare ekte heltall, ikke true/false, 1.0 eller "1"."""
    if type(value) is not int:
        raise ValueError("ix må være et heltall")
    return value


def _flag(data: dict, key: str, default: bool | None = None) -> bool:
    """Av/på fra JSON: bare true eller false. "false", null, 0 og 1 gir 400 (bool("false") er True).
    Uten ``default`` må feltet være med."""
    value = data[key] if default is None else data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} må være true eller false")
    return value


class WebServer:
    def __init__(self, speaker: Speaker, host: str = "0.0.0.0", port: int = 8080,
                 config: Config | None = None, profiles: ProfileStore | None = None,
                 require_auth: bool = True, lan_url: str | None = None,
                 finder: Finder | None = None) -> None:
        self.lan_url = lan_url  # adressen iPhone bruker, vises under «Mer» på PC-en
        self.finder = finder
        self.find_timeout = FIND_TIMEOUT
        self._finding = asyncio.Lock()
        self.speaker = speaker
        self.host = host
        self.port = port
        self.config = config
        self.profiles = profiles
        self.require_auth = require_auth and config is not None
        self.sse_ping = SSE_PING
        self._server: asyncio.base_events.Server | None = None
        self._failed: dict[str, list[float]] = {}

    def _static(self, name: str) -> bytes:
        return resources.files(__package__).joinpath("static", name).read_bytes()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            await close_server(self._server)

    @property
    def bound_port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    # ------------------------------------------------------------ tilgang

    @staticmethod
    def _is_local(peer: str) -> bool:
        try:
            return ipaddress.ip_address(peer.split("%")[0]).is_loopback
        except ValueError:
            return False

    def _authorized(self, peer: str, headers: dict[str, str]) -> bool:
        if not self.require_auth or self._is_local(peer):
            return True
        code = str(self.config["access_code"])
        if headers.get("x-lemus-key") == code:
            return True
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v == code:
                return True
        return False

    def _too_many_attempts(self, peer: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._failed.get(peer, []) if now - t < 60]
        self._failed[peer] = recent
        return len(recent) >= 5

    # ------------------------------------------------------------ HTTP

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            body = b""
            length = int(headers.get("content-length", "0") or 0)
            if 0 < length <= 256 * 1024:
                body = await reader.readexactly(length)
            url = urlsplit(target)
            path, query = url.path, parse_qs(url.query)
            # ?key=123456 i adressen: logg inn og gå videre til forsiden.
            if self.require_auth and "key" in query and query["key"][0] == str(self.config["access_code"]):
                self._write(writer, 302, "text/plain", b"", extra=[
                    f"Set-Cookie: {COOKIE}={self.config['access_code']}; Path=/; Max-Age=31536000; SameSite=Lax",
                    "Location: /"])
                await writer.drain()
                return
            authed = self._authorized(peer, headers)
            if not authed and path not in PUBLIC:
                if path.startswith("/api/"):
                    self._write(writer, 401, "application/json", b'{"error": "tilgangskode mangler"}')
                else:
                    self._write(writer, 200, "text/html; charset=utf-8", self._static("login.html"))
                await writer.drain()
                return
            if method == "GET" and path == "/api/events":
                await self._events(writer)
                return
            try:
                status, ctype, payload, extra = await self._route(method, path, body, peer, headers, query)
            except BAD_INPUT as exc:  # sikkerhetsnett: ugyldig input gir alltid et svar
                status, ctype, payload, extra = self._json(400, {"error": f"ugyldig forespørsel: {exc}"})
            self._write(writer, status, ctype, payload, extra)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError,
                ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _write(writer: asyncio.StreamWriter, status: int, ctype: str, payload: bytes,
               extra: list[str] | None = None) -> None:
        lines = [f"HTTP/1.1 {status} {STATUS_TEXT.get(status, '')}", f"Content-Type: {ctype}",
                 f"Content-Length: {len(payload)}", "Connection: close",
                 "X-Content-Type-Options: nosniff", *(extra or [])]
        if not any(x.lower().startswith("cache-control") for x in extra or []):
            lines.append("Cache-Control: no-store")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)

    @staticmethod
    def _json(status: int, obj, extra: list[str] | None = None):
        return status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode(), extra or []

    def _public_state(self, peer: str) -> dict:
        st = dict(self.speaker.state)
        # Romkurven fra Platin-appen, dekodet (målt + etter korreksjon). Rådataene beholdes også.
        raw = st.get("room_curve_raw")
        st["room_curve_empty"] = bool(raw) and p.room_curve_is_empty(bytes(raw))
        st["room_curve"] = p.decode_room_curve(bytes(raw)) if raw and not st["room_curve_empty"] else None
        if self.config is not None:
            st["settings"] = {"host": self.config["host"], "auto": not self.config["host"],
                              "max_volume_step": self.speaker.max_volume_step,
                              "config_path": str(self.config.path), "auth": self.require_auth,
                              "lan_url": self.lan_url}
            if self._is_local(peer) and self.require_auth:
                st["settings"]["access_code"] = self.config["access_code"]  # vises bare på PC-en
        return st

    async def _route(self, method: str, path: str, body: bytes, peer: str, headers: dict,
                     query: dict | None = None):
        if path in ("/", "/index.html"):
            if method != "GET":
                return self._json(405, {"error": "bruk GET"})
            return 200, "text/html; charset=utf-8", self._static("index.html"), []
        if method == "GET" and path.lstrip("/") in STATIC_FILES:
            name = path.lstrip("/")
            return 200, STATIC_FILES[name], self._static(name), ["Cache-Control: max-age=3600"]
        s = self.speaker
        if method == "GET":
            match path:
                case "/api/state":
                    return self._json(200, self._public_state(peer))
                case "/api/profiles":
                    return self._json(200, {"profiles": self.profiles.all() if self.profiles else []})
                case "/api/diag":
                    return self._json(200, {"frames": list(s.diag)[-200:],
                                            "formulas": {str(k): v for k, v in s.formulas.items()},
                                            "disable_mode": s.disable_mode})
                case "/api/backup":
                    return self._json(200, self._backup(),
                                      ["Content-Disposition: attachment; filename=sumel-backup.json"])
                case "/api/qr.svg":
                    return self._qr((query or {}).get("text", [""])[0])
            if not path.startswith("/api/"):
                return self._json(404, {"error": "finnes ikke"})
            return self._json(405, {"error": "bruk POST"})
        if method != "POST":
            return self._json(405, {"error": "bruk POST"})
        try:
            data = json.loads(body or b"{}")
        except (ValueError, RecursionError):  # også ugyldig UTF-8 og svært dyp nøsting
            return self._json(400, {"error": "ugyldig JSON"})
        if not isinstance(data, dict):
            return self._json(400, {"error": "ugyldig JSON"})
        try:
            match path:
                case "/api/login":
                    if not self.require_auth:
                        return self._json(200, {"ok": True})
                    if self._too_many_attempts(peer):
                        return self._json(429, {"error": "for mange forsøk. Vent ett minutt"})
                    if str(data.get("code", "")).strip() != str(self.config["access_code"]):
                        self._failed.setdefault(peer, []).append(time.monotonic())
                        return self._json(401, {"error": "feil kode"})
                    return self._json(200, {"ok": True}, [
                        f"Set-Cookie: {COOKIE}={self.config['access_code']}; Path=/; Max-Age=31536000; SameSite=Lax"])
                case "/api/preset":
                    # SM450: preset 1-3 faller tilbake til Platin-appens kurve etter noen sekunder.
                    return self._json(400, {"error": "ikke støttet på SM450"})
                case "/api/manual_eq":
                    await s.set_manual_eq(_flag(data, "on"))
                case "/api/room_correction":
                    await s.set_room_correction(_flag(data, "on"))
                case "/api/source":
                    await s.select_source(int(data["ix"]))
                case "/api/power":
                    await s.set_power(_flag(data, "on"))
                case "/api/mute":
                    await s.set_mute(_flag(data, "on"))
                case "/api/volume":
                    # Volumvakten gjelder alltid fra API-et: «force» fra klienten tas ikke imot.
                    await s.set_volume(int(data["level"]), force=False)
                case "/api/transport":
                    await s.transport(str(data["action"]))
                case "/api/band":
                    ix = None if data.get("ix") is None else _slot(data["ix"])
                    t = int(data.get("type", PARAMETRIC))
                    if ix is not None and "type" not in data and s._band(ix):
                        t = s._band(ix)["type_code"]
                    # Q som leses tilbake likt (ellers blir skrivingen gjort, men ikke bekreftet)
                    q = None if data.get("q") is None else p.readback_safe_q(float(data["q"]))
                    g = None if data.get("gain") is None else float(data["gain"])
                    await s.write_band(ix, t, int(data["freq"]), q, g, _flag(data, "enabled", True))
                case "/api/eq_fit":
                    # Bare beregning: finner høyst tre bånd for kurven. Skriver ingenting til høyttaleren.
                    return self._json(200, await asyncio.to_thread(self._eq_fit, data.get("points")))
                case "/api/band_enabled":
                    await s.set_band_enabled(_slot(data["ix"]), _flag(data, "enabled"))
                case "/api/bands_apply":
                    await s.apply_bands(validate_bands(data["bands"]))
                case "/api/band_delete":
                    await s.delete_band(_slot(data["ix"]))
                case "/api/bands_refresh":
                    await s.refresh_bands()
                case "/api/room_curve":
                    await s.read_room_curve()
                case "/api/settings":
                    if self.config is None:
                        raise ValueError("innstillinger er ikke tilgjengelige")
                    if "max_volume_step" in data:
                        step = max(VOLUME_STEP_MIN, min(VOLUME_STEP_MAX, int(data["max_volume_step"])))
                        self.config["max_volume_step"] = step
                        s.max_volume_step = step
                    if "host" in data:
                        host = (data["host"] or "").strip() or None
                        if not host:  # «Finn automatisk»: ett søk, og adressen beholdes uten treff
                            return await self._find(peer)
                        ipaddress.ip_address(host)  # bare IP-adresser
                        self.config["host"] = host
                        await s.set_host(host)
                case "/api/profiles/save":
                    prof = self.profiles.save(data["name"], data.get("bands", []), data.get("id"),
                                              data.get("room_correction"), data.get("manual_eq"), data.get("desc"),
                                              data.get("curve"))
                    return self._json(200, {"profile": prof, "profiles": self.profiles.all()})
                case "/api/profiles/save_current":
                    bands = [{"type": b["type_code"], "freq": b["freq"], "q": b["q"], "gain": b["gain"],
                              "enabled": b["enabled"] is not False} for b in s.state["bands"] if b["editable"]]
                    # «Oppdater» beholder kurven profilen hadde, med mindre forespørselen sier noe annet
                    old = self.profiles.get(str(data["id"])) if data.get("id") else None
                    curve = data["curve"] if "curve" in data else (old or {}).get("curve")
                    prof = self.profiles.save(data["name"], bands, data.get("id"), s.state["room_correction"],
                                              s.state["manual_eq"], data.get("desc"), curve)
                    return self._json(200, {"profile": prof, "profiles": self.profiles.all()})
                case "/api/profiles/rename":
                    self.profiles.rename(str(data["id"]), str(data["name"]))
                    return self._json(200, {"profiles": self.profiles.all()})
                case "/api/profiles/reset":
                    prof = self.profiles.reset(str(data["id"]))
                    return self._json(200, {"profile": prof, "profiles": self.profiles.all()})
                case "/api/profiles/duplicate":
                    self.profiles.duplicate(str(data["id"]))
                    return self._json(200, {"profiles": self.profiles.all()})
                case "/api/profiles/delete":
                    self.profiles.delete(str(data["id"]))
                    return self._json(200, {"profiles": self.profiles.all()})
                case "/api/profiles/apply":
                    prof = self.profiles.get(str(data["id"])) if "id" in data else {"bands": data.get("bands", [])}
                    if not prof:
                        raise ValueError("fant ikke profilen")
                    # Høypass og andre filtre profilen ikke bruker, blir stående (som i kurve-EQ)
                    await s.apply_bands(validate_bands(prof["bands"]), keep_types=frozenset(eqfit.FIT_TYPES))
                    if prof.get("room_correction") is not None and prof["room_correction"] != s.state["room_correction"]:
                        await s.set_room_correction(bool(prof["room_correction"]))
                    if prof.get("manual_eq") is not None and prof["manual_eq"] != s.state["manual_eq"]:
                        await s.set_manual_eq(bool(prof["manual_eq"]))
                case "/api/restore":
                    n = self.profiles.import_many(data.get("profiles", []), data.get("profile_overrides"))
                    if data.get("apply_device") and (data.get("device") or {}).get("bands") is not None:
                        await s.apply_bands(validate_bands(data["device"]["bands"]))
                    return self._json(200, {"imported": n, "profiles": self.profiles.all()})
                case _:
                    return self._json(404, {"error": "finnes ikke"})
        except BAD_INPUT as exc:
            return self._json(400, {"error": f"ugyldig forespørsel: {exc}"})
        except CommandError as exc:
            return self._json(502, {"error": str(exc)})
        return self._json(200, self._public_state(peer))

    def _qr(self, text: str):
        """QR-kode (SVG) for en tekst, f.eks. adressen iPhone skal åpne. Lages lokalt (qr.py)."""
        if not text:
            return self._json(400, {"error": "text mangler"})
        if len(text.encode("utf-8")) > QR_MAX_TEXT:
            return self._json(400, {"error": f"teksten er for lang for en QR-kode (høyst {QR_MAX_TEXT} byte)"})
        try:
            svg = qr.svg(qr.encode(text))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        return 200, "image/svg+xml; charset=utf-8", svg.encode(), []

    def _eq_fit(self, points) -> dict:
        """Kurve-EQ: bånd som kurven ikke skal røre, opptar plasser og trekkes fra de tre.

        Det gjelder bånd av typer vi ikke kan skrive, og skrivbare bånd som ikke er EQ-kurver
        (for eksempel et 12 dB høypassfilter som beskytter basselementet). De beholdes som de er,
        og responsen deres tas med i kurven der formelen er kjent.
        """
        s = self.speaker
        kept = [b for b in s.state.get("bands") or []
                if not b.get("editable") or b.get("type_code") not in eqfit.FIT_TYPES]
        # Bare det som ligger på plass 0-2, tar av de tre plassene. Et bånd på plass 3+ (utenfor SUMELs plasser)
        # beholdes også og regnes med i lyden, men tar ingen plass og står ikke i «kept».
        in_slots = [b for b in kept if isinstance(b.get("ix"), int) and b["ix"] <= MAX_BAND_INDEX]
        max_bands = max(0, p.MAX_BANDS - len(in_slots))
        # alle som beholdes, regnes med i kurven der formelen er kjent (eqfit teller resten i base_unknown)
        base = [{"type": b.get("type_code"), "freq": b.get("freq"), "q": b.get("q"), "gain": b.get("gain"),
                 "enabled": b.get("enabled")} for b in kept]
        res = eqfit.fit(points, types=list(s.formulas), max_bands=max_bands, formulas=s.formulas, base_bands=base)
        res["max_bands"] = max_bands
        res["kept"] = [{"ix": b.get("ix"), "type": b.get("type_code"), "freq": b.get("freq"),
                        "editable": bool(b.get("editable"))} for b in in_slots]
        return res

    async def _default_finder(self) -> dict | None:
        """Uten egen finner: bruk høyttalerens resolver (automatisk oppdaging), hvis den finnes."""
        if not self.speaker.resolver:
            return None
        host = await self.speaker.resolver()
        return {"host": host, "name": self.speaker.state.get("name")} if host else None

    async def _find(self, peer: str):
        """Let etter høyttaleren én gang. Funnet: bytt til den. Ellers: behold adressen vi har.

        Tidligere ble adressen slettet og appen lette i det uendelige når UDP ikke virket.
        """
        if self._finding.locked():
            return self._json(409, {"error": "Leter allerede etter høyttaleren. Vent litt."})
        s = self.speaker
        async with self._finding:
            before = (self.config["host"], s.host)
            try:
                found = await asyncio.wait_for((self.finder or self._default_finder)(), self.find_timeout)
            except asyncio.TimeoutError:
                found = None
            except Exception:  # noqa: BLE001 – et feilet søk skal aldri ta med seg adressen
                log.exception("søket etter høyttaleren feilet")
                found = None
            host = str((found or {}).get("host") or "").strip() or None
            if host:
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    host = None
            if not host:
                keep = s.host or self.config["host"]
                msg = f"Fant ingen høyttaler. Beholder {keep}." if keep else "Fant ingen høyttaler."
                return self._json(404, {"error": msg})
            if (self.config["host"], s.host) != before and host != s.host:
                # Adressen ble valgt på nytt mens vi lette (skrevet inn for hånd): den gjelder.
                keep = s.host or self.config["host"]
                return self._json(409, {"error": f"Adressen ble endret mens søket pågikk. Beholder {keep}."})
            self.config["host"] = host
            if host != s.host:
                await s.set_host(host)
            st = self._public_state(peer)
            st["found"] = {"host": host, "name": found.get("name") or s.state.get("name")}
            return self._json(200, st)

    def _backup(self) -> dict:
        s = self.speaker.state
        return {
            "app": "SUMEL", "version": 1, "created": time.strftime("%Y-%m-%d %H:%M"),
            "speaker": {"name": s.get("name"), "host": s.get("host"), "versions": s.get("versions")},
            # bare bånd SUMEL kan skrive: låste/ukjente filtre og plass 3+ kan ikke gjenopprettes herfra
            "device": {"room_correction": s.get("room_correction"), "manual_eq": s.get("manual_eq"),
                       "bands": [{"type": b["type_code"], "freq": b["freq"], "q": b["q"], "gain": b["gain"],
                                  "enabled": b["enabled"] is not False} for b in s.get("bands", [])
                                 if b.get("editable") is True]},
            "profiles": [p for p in (self.profiles.all() if self.profiles else []) if not p.get("builtin")],
            "profile_overrides": self.profiles.overrides() if self.profiles else {},  # endrede innebygde profiler
            "learned_formulas": self.config["learned_formulas"] if self.config else {},
        }

    async def _events(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n")
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=50)

        def on_change(state: dict) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(state)

        peer = (writer.get_extra_info("peername") or ("?",))[0]
        unsubscribe = self.speaker.subscribe(on_change)
        try:
            writer.write(f"data: {json.dumps(self._public_state(peer), ensure_ascii=False)}\n\n".encode())
            await writer.drain()
            loop = asyncio.get_running_loop()
            next_ping = loop.time() + self.sse_ping
            while True:
                try:
                    await asyncio.wait_for(queue.get(), max(0.0, next_ping - loop.time()))
                    await asyncio.sleep(0.05)
                    while not queue.empty():  # slå sammen raske endringer
                        queue.get_nowait()
                    writer.write(f"data: {json.dumps(self._public_state(peer), ensure_ascii=False)}\n\n".encode())
                except asyncio.TimeoutError:
                    pass
                if loop.time() >= next_ping:
                    # Ekte hendelse (ikke en kommentar) hvert sse_ping-sekund, også når tilstanden
                    # endres ofte, så siden ser at forbindelsen lever (vaktklokke i index.html).
                    writer.write(b"event: ping\ndata: {}\n\n")
                    next_ping = loop.time() + self.sse_ping
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            unsubscribe()
