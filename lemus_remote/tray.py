"""SUMEL som vanlig Windows-program: ikon ved klokken, ingen konsollvindu.

SUMEL – fjernkontroll for Platin SM450 – er et uoffisielt program (ikke laget av Lemus
eller Platin). Det het tidligere «Lemus Remote»; innstillingene ligger fortsatt i
``%APPDATA%\\LemusRemote`` (se ``config.default_path``), så en oppgradering beholder
høyttaleradresse, tilgangskode og profiler.

Starter samme server som ``py -m lemus_remote`` (``__main__.run``), men:

* logger til ``%APPDATA%\\LemusRemote\\lemus.log`` (roterende, liten) i stedet for konsollen
* kjører bare én gang: svarer SUMEL allerede på porten, åpnes nettleseren og
  den nye forekomsten avslutter
* åpner nettleseren ved start (ikke med ``--background``, som brukes når Windows starter)
* meny ved klokken: Åpne SUMEL, adressen for iPhone (kopieres), tilgangskoden,
  Start med Windows (HKCU\\...\\Run) og Avslutt

pystray og Pillow trengs bare for selve ikonet (de følger med Windows-pakken). Mangler de,
eller finnes ingen skjerm (Linux uten skrivebord), kjører serveren videre uten ikon.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import locale
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import default_path

log = logging.getLogger("lemus_remote.tray")

APP_NAME = "SUMEL"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "SUMEL"  # samme navn som installasjonsprogrammet bruker (packaging/installer.iss)
# Verdien fra da programmet het Lemus Remote. Den leses fortsatt (så «Start med Windows»
# vises riktig), flyttes til RUN_VALUE ved oppstart og fjernes når innstillingen endres.
LEGACY_RUN_VALUES = ("LemusRemote",)
ICON_FILES = ("icon-512.png", "icon-192.png")  # i lemus_remote/static; reserve: tegnet ikon
LOG_NAME = "lemus.log"
LOG_BYTES = 512 * 1024
LOG_BACKUPS = 2
OPEN_DEBOUNCE = 1.5  # s: et dobbeltklikk gir to klikk-meldinger i Windows, men skal åpne én fane
MENU_POLL = 4.0      # s: hvor ofte menyen sjekker om iPhone-adressen har endret seg

TEXTS: dict[str, dict[str, str]] = {
    "no": {
        "tooltip": "SUMEL {version} – fjernkontroll for Platin SM450",
        "open": "Åpne SUMEL",
        "iphone": "Adresse for iPhone: {url}",
        "iphone_none": "Adresse for iPhone: bare på denne PC-en",
        "code": "Tilgangskode: {code}",
        "autostart": "Start med Windows",
        "quit": "Avslutt",
        "copied": "Kopiert. Åpne adressen i Safari på iPhone.",
        "copied_code": "Tilgangskoden er kopiert.",
        "not_copied": "Skriv adressen inn i Safari på iPhone: {url}",
        "running": "SUMEL kjører. Høyreklikk ikonet for adressen til iPhone.",
        "port_busy": ("SUMEL kunne ikke starte fordi port {port} er opptatt av et annet program.\n\n"
                      "Lukk det andre programmet, eller velg en annen port i innstillingsfilen:\n{config}"),
        "failed": "SUMEL stoppet på grunn av en feil. Se loggen:\n{log}",
    },
    "en": {
        "tooltip": "SUMEL {version} – remote for Platin SM450",
        "open": "Open SUMEL",
        "iphone": "Address for iPhone: {url}",
        "iphone_none": "Address for iPhone: this PC only",
        "code": "Access code: {code}",
        "autostart": "Start with Windows",
        "quit": "Quit",
        "copied": "Copied. Open the address in Safari on your iPhone.",
        "copied_code": "The access code has been copied.",
        "not_copied": "Type this address into Safari on your iPhone: {url}",
        "running": "SUMEL is running. Right-click the icon for the iPhone address.",
        "port_busy": ("SUMEL could not start because port {port} is used by another program.\n\n"
                      "Close the other program, or choose another port in the settings file:\n{config}"),
        "failed": "SUMEL stopped because of an error. See the log:\n{log}",
    },
}

NORWEGIAN_LANGIDS = {0x14}  # LANG_NORWEGIAN (nb-NO 0x0414, nn-NO 0x0814)


# ------------------------------------------------------------------ språk og tekst

def ui_language(windll: Any = None, env: dict[str, str] | None = None,
                getlocale: Callable[[], tuple] | None = None) -> str:
    """«no» hvis Windows (eller systemet) er på norsk, ellers «en»."""
    if windll is None and os.name == "nt":
        try:
            import ctypes
            windll = ctypes.windll
        except (ImportError, AttributeError):
            windll = None
    if windll is not None:
        try:
            langid = int(windll.kernel32.GetUserDefaultUILanguage())
            return "no" if (langid & 0x3FF) in NORWEGIAN_LANGIDS else "en"
        except Exception:  # noqa: BLE001 – faller tilbake til miljøvariablene
            pass
    env = os.environ if env is None else env
    names = [env.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")]
    try:
        names.append((getlocale or locale.getlocale)()[0] or "")
    except (ValueError, TypeError):
        pass
    for name in names:
        low = name.lower()
        if low.startswith(("nb", "nn", "no_", "no-", "norwegian")) or low == "no":
            return "no"
        if low and low not in ("c", "posix", "c.utf-8"):
            return "en"
    return "en"


def texts(lang: str) -> dict[str, str]:
    return TEXTS.get(lang, TEXTS["en"])


def tooltip_text(lang: str, windows: bool | None = None) -> str:
    """Teksten når musen holdes over ikonet. Utenfor Windows tåler vindustittelen (X11) bare latin-1."""
    text = texts(lang)["tooltip"].format(version=__version__)
    if not (os.name == "nt" if windows is None else windows):
        text = text.replace("\u2013", "-").encode("latin-1", "replace").decode("latin-1")
    return text


def menu_labels(lang: str, lan_url: str | None, access_code: str | None) -> list[str]:
    """Menypunktene i rekkefølge (uten skillelinjer). Tilgangskoden vises bare når den kreves."""
    t = texts(lang)
    out = [t["open"], t["iphone"].format(url=lan_url) if lan_url else t["iphone_none"]]
    if access_code and lan_url:
        out.append(t["code"].format(code=access_code))
    out += [t["autostart"], t["quit"]]
    return out


# ------------------------------------------------------------------ logg

def log_path(config_path: str | os.PathLike | None = None) -> Path:
    """Loggen ligger ved innstillingsfilen: ``%APPDATA%\\LemusRemote\\lemus.log`` på Windows."""
    base = Path(config_path).parent if config_path else default_path().parent
    return base / LOG_NAME


class _LogStream:
    """Erstatter stdout/stderr i tray-modus, slik at ``print`` havner i loggen."""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger, self.level, self._buf = logger, level, ""
        self._busy = False  # loggen selv feiler og skriver til stderr: ikke gå i ring

    def write(self, text: str) -> int:
        if self._busy:
            return len(text)
        self._buf += text
        self._busy = True
        try:
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self.logger.log(self.level, line.rstrip())
        finally:
            self._busy = False
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            self.logger.log(self.level, self._buf.rstrip())
        self._buf = ""

    def isatty(self) -> bool:
        return False


def setup_logging(path: Path, verbose: bool = False) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.raiseExceptions = False  # en låst loggfil (to forekomster) skal ikke gi feil
    out = logging.getLogger("lemus_remote.console")
    sys.stdout = _LogStream(out, logging.INFO)  # type: ignore[assignment]
    sys.stderr = _LogStream(out, logging.WARNING)  # type: ignore[assignment]
    return handler


# ------------------------------------------------------------------ én forekomst

def is_lemus_state(body: bytes) -> bool:
    """Svarer porten som SUMEL (``/api/state`` gir JSON med «connected»)?

    Gjelder også en eldre «Lemus Remote» (samme server), så de to startes aldri samtidig.
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(data, dict) and "connected" in data


def already_running(port: int, timeout: float = 1.5,
                    fetch: Callable[[str, float], bytes] | None = None) -> bool:
    """True hvis SUMEL allerede svarer på denne PC-en (på ``port``)."""
    def _fetch(url: str, t: float) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "SUMEL-tray"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # aldri via proxy
        with opener.open(req, timeout=t) as r:
            return r.read(256 * 1024)
    try:
        return is_lemus_state((fetch or _fetch)(f"http://127.0.0.1:{port}/api/state", timeout))
    except Exception:  # noqa: BLE001 – ingenting svarer, eller noe annet enn oss
        return False


# ------------------------------------------------------------------ Start med Windows

def autostart_command(frozen: bool | None = None, executable: str | None = None,
                      root: str | os.PathLike | None = None) -> str:
    """Kommandoen som legges i HKCU\\...\\Run. Starter uten å åpne nettleseren."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = executable or sys.executable
    if frozen:
        return f'"{exe}" --background'
    # Fra kildekoden: pythonw.exe (uten konsoll) og pakken fra denne mappen.
    p = Path(exe)
    if p.name.lower() == "python.exe" and (p.parent / "pythonw.exe").exists():
        exe = str(p.parent / "pythonw.exe")
    root = str(root or Path(__file__).resolve().parent.parent)
    code = (f"import sys;sys.path.insert(0,r'{root}');"
            "from lemus_remote.__main__ import cli;cli(['--tray','--background'])")
    return f'"{exe}" -c "{code}"'


class Autostart:
    """«Start med Windows»: verdien ``SUMEL`` under HKCU\\...\\Run (bare Windows).

    Den gamle verdien ``LemusRemote`` (fra Lemus Remote) teller også som på, og fjernes
    når innstillingen endres, så programmet aldri startes to ganger.
    """

    def __init__(self, winreg_mod: Any = None, command: str | None = None) -> None:
        if winreg_mod is None and os.name == "nt":
            import winreg as winreg_mod  # noqa: PLC0415 – finnes bare på Windows
        self.reg = winreg_mod
        self.command = command or autostart_command()

    @property
    def available(self) -> bool:
        return self.reg is not None

    def _has(self, name: str) -> bool:
        try:
            with self.reg.OpenKey(self.reg.HKEY_CURRENT_USER, RUN_KEY, 0, self.reg.KEY_READ) as k:
                value, _ = self.reg.QueryValueEx(k, name)
                return bool(value)
        except OSError:
            return False

    def is_enabled(self) -> bool:
        if not self.reg:
            return False
        return any(self._has(name) for name in (RUN_VALUE, *LEGACY_RUN_VALUES))

    def set(self, on: bool) -> None:
        if not self.reg:
            return
        with self.reg.CreateKeyEx(self.reg.HKEY_CURRENT_USER, RUN_KEY, 0, self.reg.KEY_SET_VALUE) as k:
            if on:
                self.reg.SetValueEx(k, RUN_VALUE, 0, self.reg.REG_SZ, self.command)
            for name in ((RUN_VALUE,) if not on else ()) + LEGACY_RUN_VALUES:
                try:
                    self.reg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass

    def migrate(self) -> bool:
        """Var «Start med Windows» på under det gamle navnet, flyttes den til ``SUMEL``.

        Gir True hvis noe ble flyttet. Feil (låst register) ignoreres: da gjelder den gamle verdien.
        """
        if not self.reg or not any(self._has(name) for name in LEGACY_RUN_VALUES):
            return False
        try:
            self.set(True)
        except OSError:
            log.warning("kunne ikke flytte «Start med Windows» til det nye navnet", exc_info=True)
            return False
        return True

    def toggle(self) -> bool:
        on = not self.is_enabled()
        self.set(on)
        return on


# ------------------------------------------------------------------ små hjelpere

def copy_to_clipboard(text: str, run: Callable[..., Any] = subprocess.run,
                      windows: bool | None = None) -> bool:
    """Kopier til utklippstavlen. Windows: clip.exe; ellers pbcopy/xclip hvis de finnes."""
    if os.name == "nt" if windows is None else windows:
        cmds = [["clip"]]
        # clip.exe leser ASCII rett; annen tekst må være UTF-16 med BOM
        data = text.encode("ascii") if text.isascii() else b"\xff\xfe" + text.encode("utf-16-le")
        extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        cmds = [["pbcopy"], ["xclip", "-selection", "clipboard"], ["wl-copy"]]
        data, extra = text.encode("utf-8"), {}
    for cmd in cmds:
        try:
            run(cmd, input=data, check=True, timeout=5, **extra)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def is_port_busy(err: BaseException | None) -> bool:
    """Bare «adressen er i bruk» er opptatt port; andre OSError (disk, rettigheter) er noe annet."""
    if not isinstance(err, OSError):
        return False
    return err.errno in (errno.EADDRINUSE, 10048) or getattr(err, "winerror", None) == 10048


def message_box(text: str, title: str = APP_NAME) -> None:
    """Feilmelding til brukeren (Windows), ellers bare i loggen."""
    log.error(text)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
        except Exception:  # noqa: BLE001
            pass


def fallback_icon(size: int = 64):
    """Reserveikon når bildefilene mangler eller ikke kan leses: ravgul ring på grafitt."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 6, fill=(38, 40, 44, 255))
    m, w = size // 5, max(2, size // 12)
    d.ellipse((m, m, size - 1 - m, size - 1 - m), outline=(232, 160, 48, 255), width=w)
    c = size // 2
    d.line((c, c, c, m + w), fill=(240, 240, 240, 255), width=max(2, w // 2))
    return img


def icon_image(size: int = 64):
    """Ikonet ved klokken (Pillow-bilde): static/icon-512.png, ellers icon-192.png, ellers et tegnet ikon."""
    from importlib import resources
    from io import BytesIO

    from PIL import Image
    static = resources.files("lemus_remote").joinpath("static")
    for name in ICON_FILES:
        try:
            data = static.joinpath(name).read_bytes()
            img = Image.open(BytesIO(data)).convert("RGBA")
            return img.resize((size, size), Image.LANCZOS)
        except Exception:  # noqa: BLE001 – mangler eller er ødelagt: prøv neste
            log.debug("kunne ikke lese ikonet %s", name, exc_info=True)
    return fallback_icon(size)


def load_pystray():
    """pystray, eller None når den mangler eller ingen skjerm finnes."""
    if os.name != "nt" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    try:
        import pystray
        return pystray
    except Exception as e:  # noqa: BLE001 – Xlib uten skjerm kaster ved import
        log.info("ikon ved klokken er ikke tilgjengelig (%s); kjører uten", e)
        return None


# ------------------------------------------------------------------ selve programmet

class ServerThread(threading.Thread):
    """Kjører ``__main__.run`` i egen tråd med egen asyncio-løkke."""

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(name="lemus-server", daemon=True)
        self.args = args
        self.ready = threading.Event()
        self.running = None  # __main__.Running når serveren lytter
        self.error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None

    def run(self) -> None:
        from .__main__ import run as run_app

        async def go() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop_event = asyncio.Event()
            await run_app(self.args, on_ready=self._on_ready, stop=self._stop_event, tray=True)

        try:
            asyncio.run(go())
        except BaseException as e:  # noqa: BLE001 – rapporteres til hovedtråden
            self.error = e
            if is_port_busy(e):
                log.error("porten er opptatt: %s", e)
            else:
                log.exception("serveren stoppet")
        finally:
            self.ready.set()

    def _on_ready(self, running) -> None:
        self.running = running
        self.ready.set()

    def stop(self) -> None:
        if self._loop and self._stop_event and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass


class TrayApp:
    def __init__(self, server: ServerThread, pystray, lang: str, autostart: Autostart) -> None:
        self.server, self.pystray, self.lang, self.autostart = server, pystray, lang, autostart
        self.t = texts(lang)
        self.icon = None
        self._stopped = False
        self._lock = threading.Lock()
        self._last_open = float("-inf")
        self._shown_url: str | None = None  # iPhone-adressen menyen sist ble bygget med

    # Menyhandlinger. pystray kaller dem med (icon, item).
    def open_app(self, *_: Any, now: Callable[[], float] = time.monotonic) -> None:
        t = now()
        with self._lock:
            if t - self._last_open < OPEN_DEBOUNCE:  # andre halvdel av et dobbeltklikk
                return
            self._last_open = t
        webbrowser.open(self.server.running.local_url)

    def refresh_menu(self) -> bool:
        """Bygg menyen på nytt hvis iPhone-adressen er endret (pystray på Windows bufrer menyen)."""
        url = self.server.running.lan_url if self.server.running else None
        if url == self._shown_url:
            return False
        self._shown_url = url
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:  # noqa: BLE001 – menyen oppdateres igjen neste gang
                log.debug("kunne ikke oppdatere menyen", exc_info=True)
        log.info("ny adresse for iPhone i menyen: %s", url)
        return True

    def copy_iphone(self, *_: Any) -> None:
        url = self.server.running.lan_url
        if not url:
            self._last_open = float("-inf")
            self.open_app()
            return
        self._notify(self.t["copied"] if copy_to_clipboard(url) else self.t["not_copied"].format(url=url))

    def copy_code(self, *_: Any) -> None:
        code = self.server.running.access_code
        if code and copy_to_clipboard(code):
            self._notify(self.t["copied_code"])

    def toggle_autostart(self, *_: Any) -> None:
        try:
            self.autostart.toggle()
        except OSError:
            log.exception("kunne ikke endre «Start med Windows»")
        if self.icon:
            self.icon.update_menu()

    def quit(self, *_: Any) -> None:
        # Ikonet fjernes når serveren har stoppet (se run). Henger stoppen, fjernes det etter 5 s.
        self.server.stop()
        t = threading.Timer(5.0, self.stop_icon)
        t.daemon = True
        t.start()

    def stop_icon(self) -> None:
        """Fjern ikonet nøyaktig én gang (to kall på pystray sin stop kan henge)."""
        with self._lock:
            if self._stopped or not self.icon:
                return
            self._stopped = True
        self.icon.stop()

    def _notify(self, text: str) -> None:
        try:
            self.icon.notify(text, APP_NAME)
        except Exception:  # noqa: BLE001 – ikke alle systemer har varsler
            log.info(text)

    def menu(self):
        ps, r = self.pystray, self.server.running
        item, sep = ps.MenuItem, ps.Menu.SEPARATOR

        def iphone_text(_item) -> str:
            url = r.lan_url  # endres når PC-en får ny adresse
            return self.t["iphone"].format(url=url) if url else self.t["iphone_none"]

        items = [item(self.t["open"], self.open_app, default=True), sep,
                 item(iphone_text, self.copy_iphone)]
        if r.access_code and r.lan_url:
            items.append(item(self.t["code"].format(code=r.access_code), self.copy_code))
        items.append(sep)
        if self.autostart.available:
            items.append(item(self.t["autostart"], self.toggle_autostart,
                              checked=lambda _i: self.autostart.is_enabled()))
        items.append(item(self.t["quit"], self.quit))
        return ps.Menu(*items)

    def run(self) -> None:
        self._shown_url = self.server.running.lan_url
        self.icon = self.pystray.Icon(APP_NAME, icon_image(), tooltip_text(self.lang), self.menu())

        def watch() -> None:
            # Følg med på iPhone-adressen (Wi-Fi kommer ofte etter at Windows har startet oss),
            # og fjern ikonet når serveren stopper (Avslutt eller feil).
            while self.server.is_alive():
                self.server.join(MENU_POLL)
                if self.server.is_alive():
                    self.refresh_menu()
            self.stop_icon()

        def show(icon) -> None:
            icon.visible = True

        threading.Thread(target=watch, name="lemus-tray-watch", daemon=True).start()
        self.icon.run(setup=show)


def main(args: argparse.Namespace) -> int:
    """Tray-modus. Gir avslutningskoden."""
    from .config import Config

    lang = ui_language()
    t = texts(lang)
    path = log_path(args.config)
    try:
        setup_logging(path, args.verbose)
    except OSError:
        logging.basicConfig(level=logging.INFO)
    log.info("SUMEL %s starter (tray, %s)", __version__, "exe" if getattr(sys, "frozen", False) else "py")

    cfg = Config(args.config)
    port = args.port or cfg["port"] or 8080
    if already_running(port):
        log.info("kjører allerede på port %s%s", port, "" if args.background else ": åpner nettleseren")
        if not args.background:
            webbrowser.open(f"http://localhost:{port}")
        return 0
    config_file = cfg.path  # run() leser innstillingene på nytt selv

    args.open = not args.background
    server = ServerThread(args)
    server.start()
    server.ready.wait()
    if server.running is None:  # oppstarten feilet
        err = server.error
        if is_port_busy(err):
            message_box(t["port_busy"].format(port=port, config=config_file))
        else:
            message_box(t["failed"].format(log=path))
        return 1

    autostart = Autostart()
    try:
        if autostart.migrate():
            log.info("«Start med Windows» flyttet fra LemusRemote til %s", RUN_VALUE)
    except Exception:  # noqa: BLE001 – aldri stopp oppstarten for dette
        log.debug("sjekk av gammel «Start med Windows» feilet", exc_info=True)

    ps = load_pystray()
    if ps is not None:
        try:
            TrayApp(server, ps, lang, autostart).run()
        except Exception:  # noqa: BLE001 – ikonet feilet: serveren skal fortsatt virke
            log.exception("ikonet ved klokken feilet; kjører uten")
    if server.is_alive():
        if ps is None:
            log.info("kjører uten ikon ved klokken")
        try:
            while server.is_alive():
                server.join(0.5)
        except KeyboardInterrupt:
            pass
    server.stop()
    server.join(10)
    if server.error is not None:
        message_box(t["failed"].format(log=path))
        return 1
    log.info("Avsluttet.")
    return 0
