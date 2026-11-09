"""Tester for Windows-programmet SUMEL (tray.py og packaging/) som kan kjøres uten Windows, skjerm eller pystray."""

import http.server
import io
import json
import logging
import os
import socket
import sys
import errno
import tempfile
import threading
import time
import re
import unittest
from pathlib import Path
from unittest import mock

from lemus_remote import __main__ as app
from lemus_remote import __version__, tray


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Version(unittest.TestCase):
    def test_version(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        with mock.patch("sys.stdout", new=io.StringIO()) as out, self.assertRaises(SystemExit):
            app.build_parser().parse_args(["--version"])
        self.assertIn(__version__, out.getvalue())
        self.assertIn("SUMEL", out.getvalue())

    def test_version_number(self):
        self.assertEqual(__version__, "1.0.0")
        # «Om SUMEL» i appen viser samme versjon
        from pathlib import Path
        html = (Path(__file__).resolve().parents[1] / "lemus_remote" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f"<b>SUMEL</b> {__version__} ·", html)


class Texts(unittest.TestCase):
    def test_same_keys_in_both_languages(self):
        self.assertEqual(set(tray.TEXTS["no"]), set(tray.TEXTS["en"]))

    def test_menu_labels_norwegian(self):
        self.assertEqual(tray.menu_labels("no", "http://192.168.0.9:8080", "123456"),
                         ["Åpne SUMEL", "Adresse for iPhone: http://192.168.0.9:8080",
                          "Tilgangskode: 123456", "Start med Windows", "Avslutt"])

    def test_menu_labels_english_without_code(self):
        self.assertEqual(tray.menu_labels("en", "http://192.168.0.9:8080", None),
                         ["Open SUMEL", "Address for iPhone: http://192.168.0.9:8080",
                          "Start with Windows", "Quit"])

    def test_menu_labels_local_only(self):
        labels = tray.menu_labels("no", None, "123456")
        self.assertIn("Adresse for iPhone: bare på denne PC-en", labels)
        self.assertNotIn("Tilgangskode: 123456", labels)

    def test_unknown_language_is_english(self):
        self.assertEqual(tray.texts("de"), tray.TEXTS["en"])

    def test_new_name_everywhere(self):
        self.assertEqual(tray.APP_NAME, "SUMEL")
        for lang in ("no", "en"):
            t = tray.texts(lang)
            self.assertTrue(t["tooltip"].format(version="1.2.3").startswith("SUMEL 1.2.3"))
            self.assertLess(len(t["tooltip"].format(version="10.20.30")), 64)  # Windows kutter lengre tips
            for key, text in t.items():
                self.assertNotIn("Lemus Remote", text, key)
            self.assertIn("SUMEL", t["port_busy"])
            self.assertIn("SUMEL", t["failed"])

    def test_tooltip(self):
        self.assertEqual(tray.tooltip_text("no", windows=True), f"SUMEL {__version__} – fjernkontroll for Platin SM450")
        self.assertEqual(tray.tooltip_text("en", windows=True), f"SUMEL {__version__} – remote for Platin SM450")
        # X11 (pystray på Linux) setter vindustittelen som latin-1: ingen tankestrek der.
        linux = tray.tooltip_text("no", windows=False)
        self.assertEqual(linux, f"SUMEL {__version__} - fjernkontroll for Platin SM450")
        linux.encode("latin-1")


class Language(unittest.TestCase):
    def windll(self, langid=None, error=False):
        k = mock.Mock()
        if error:
            k.GetUserDefaultUILanguage.side_effect = OSError("nei")
        else:
            k.GetUserDefaultUILanguage.return_value = langid
        return mock.Mock(kernel32=k)

    def test_windows_norwegian(self):
        for langid in (0x0414, 0x0814):  # bokmål, nynorsk
            self.assertEqual(tray.ui_language(windll=self.windll(langid), env={}), "no")

    def test_windows_other(self):
        for langid in (0x0409, 0x041D, 0x0406):  # engelsk, svensk, dansk
            self.assertEqual(tray.ui_language(windll=self.windll(langid), env={}), "en")

    def test_falls_back_to_environment(self):
        none = lambda: (None, None)  # noqa: E731
        w = self.windll(error=True)
        self.assertEqual(tray.ui_language(windll=w, env={"LANG": "nb_NO.UTF-8"}, getlocale=none), "no")
        self.assertEqual(tray.ui_language(windll=w, env={"LANG": "en_US.UTF-8"}, getlocale=none), "en")
        self.assertEqual(tray.ui_language(windll=w, env={}, getlocale=lambda: ("Norwegian_Norway", "1252")), "no")
        self.assertEqual(tray.ui_language(windll=w, env={"LANG": "C.UTF-8"}, getlocale=none), "en")


class SingleInstance(unittest.TestCase):
    def test_recognises_lemus_state(self):
        self.assertTrue(tray.is_lemus_state(json.dumps({"connected": False, "volume": None}).encode()))
        self.assertFalse(tray.is_lemus_state(b'{"status": "ok"}'))
        self.assertFalse(tray.is_lemus_state(b"<html>noe annet</html>"))
        self.assertFalse(tray.is_lemus_state(b"[1, 2]"))
        self.assertFalse(tray.is_lemus_state(b"\xff\xfe"))

    def test_already_running_with_fake_fetch(self):
        urls = []

        def fetch(url, timeout):
            urls.append(url)
            return b'{"connected": true}'
        self.assertTrue(tray.already_running(8080, fetch=fetch))
        self.assertEqual(urls, ["http://127.0.0.1:8080/api/state"])

        def refused(url, timeout):
            raise ConnectionRefusedError
        self.assertFalse(tray.already_running(8080, fetch=refused))

    def test_nothing_listening(self):
        self.assertFalse(tray.already_running(free_port(), timeout=1.0))

    def test_other_program_on_port(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html>et annet program</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertFalse(tray.already_running(srv.server_address[1], timeout=2.0))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_second_start_opens_browser_and_exits(self):
        d = tempfile.mkdtemp()
        args = app.build_parser().parse_args(["--tray", "--config", str(Path(d) / "config.json"), "--port", "8123"])
        with mock.patch.object(tray, "setup_logging"), \
                mock.patch.object(tray, "already_running", return_value=True) as running, \
                mock.patch.object(tray, "ServerThread") as server, \
                mock.patch.object(tray.webbrowser, "open") as browser:
            self.assertEqual(tray.main(args), 0)
        running.assert_called_once_with(8123)
        browser.assert_called_once_with("http://localhost:8123")
        server.assert_not_called()

    def test_second_start_in_background_opens_nothing(self):
        d = tempfile.mkdtemp()
        args = app.build_parser().parse_args(["--tray", "--background", "--config", str(Path(d) / "c.json")])
        with mock.patch.object(tray, "setup_logging"), \
                mock.patch.object(tray, "already_running", return_value=True), \
                mock.patch.object(tray.webbrowser, "open") as browser:
            self.assertEqual(tray.main(args), 0)
        browser.assert_not_called()


class FakeReg:
    """Det lille av winreg som Autostart bruker."""
    HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ = "HKCU", 1, 2, 1

    def __init__(self):
        self.values: dict = {}

    class _Key:
        def __init__(self, reg, path):
            self.reg, self.path = reg, path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, reserved, access):
        return self._Key(self, (root, path))

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        return self.values[(key.path, name)], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[(key.path, name)] = value

    def DeleteValue(self, key, name):
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[(key.path, name)]


class StartWithWindows(unittest.TestCase):
    def test_command_for_exe(self):
        cmd = tray.autostart_command(frozen=True, executable=r"C:\Program Files\SUMEL\SUMEL.exe")
        self.assertEqual(cmd, r'"C:\Program Files\SUMEL\SUMEL.exe" --background')

    def test_command_from_source_prefers_pythonw(self):
        d = Path(tempfile.mkdtemp())
        (d / "python.exe").write_bytes(b"")
        (d / "pythonw.exe").write_bytes(b"")
        cmd = tray.autostart_command(frozen=False, executable=str(d / "python.exe"), root=r"C:\lemus-remote")
        self.assertTrue(cmd.startswith(f'"{d / "pythonw.exe"}" -c "'))
        self.assertIn(r"sys.path.insert(0,r'C:\lemus-remote')", cmd)
        self.assertIn("cli(['--tray','--background'])", cmd)

    def test_enable_disable_toggle(self):
        reg = FakeReg()
        a = tray.Autostart(winreg_mod=reg, command='"C:\\x\\SUMEL.exe" --background')
        self.assertTrue(a.available)
        self.assertFalse(a.is_enabled())
        a.set(True)
        self.assertTrue(a.is_enabled())
        self.assertEqual(reg.values[(("HKCU", tray.RUN_KEY), "SUMEL")], '"C:\\x\\SUMEL.exe" --background')
        self.assertFalse(a.toggle())
        self.assertFalse(a.is_enabled())
        self.assertEqual(reg.values, {})
        a.set(False)  # allerede av: ingen feil
        self.assertTrue(a.toggle())
        self.assertTrue(a.is_enabled())

    def test_old_value_counts_and_is_removed(self):
        # Lemus Remote skrev «LemusRemote». Den vises som på, og fjernes når innstillingen endres.
        reg = FakeReg()
        old = (("HKCU", tray.RUN_KEY), "LemusRemote")
        reg.values[old] = '"C:\\Program Files\\Lemus Remote\\LemusRemote.exe" --background'
        a = tray.Autostart(winreg_mod=reg, command='"C:\\x\\SUMEL.exe" --background')
        self.assertTrue(a.is_enabled())
        self.assertFalse(a.toggle())  # av: begge verdiene borte
        self.assertEqual(reg.values, {})
        reg.values[old] = "gammel"
        a.set(True)  # på: bare den nye verdien
        self.assertEqual(reg.values, {(("HKCU", tray.RUN_KEY), "SUMEL"): '"C:\\x\\SUMEL.exe" --background'})

    def test_migrate_moves_old_value(self):
        reg = FakeReg()
        a = tray.Autostart(winreg_mod=reg, command='"C:\\x\\SUMEL.exe" --background')
        self.assertFalse(a.migrate())  # ingenting å flytte
        self.assertEqual(reg.values, {})
        reg.values[(("HKCU", tray.RUN_KEY), "LemusRemote")] = "gammel"
        self.assertTrue(a.migrate())
        self.assertEqual(reg.values, {(("HKCU", tray.RUN_KEY), "SUMEL"): '"C:\\x\\SUMEL.exe" --background'})
        self.assertFalse(a.migrate())
        self.assertFalse(tray.Autostart(winreg_mod=None, command="x").migrate() if os.name != "nt" else False)

    def test_run_key_is_current_user_run(self):
        self.assertEqual(tray.RUN_KEY, r"Software\Microsoft\Windows\CurrentVersion\Run")
        self.assertEqual(tray.RUN_VALUE, "SUMEL")
        self.assertEqual(tray.LEGACY_RUN_VALUES, ("LemusRemote",))
        iss = ISS.read_text(encoding="utf-8-sig")
        # installasjonsprogrammet bruker samme verdier
        self.assertIn('#define RunValue "SUMEL"', iss)
        self.assertIn('#define OldRunValue "LemusRemote"', iss)
        self.assertIn('#define RunKey "Software\\Microsoft\\Windows\\CurrentVersion\\Run"', iss)
        self.assertIn('ValueName: "{#RunValue}"', iss)
        self.assertRegex(iss, r'ValueName: "\{#OldRunValue\}"; Flags: deletevalue uninsdeletevalue')


ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "installer.iss"
SPEC = ROOT / "packaging" / "sumel.spec"
WORKFLOW = ROOT.parent / ".github" / "workflows" / "windows.yml"


class InstallerScript(unittest.TestCase):
    def test_utf8_with_bom(self):
        # Uten BOM leser Inno Setup filen som ANSI, og «når» blir «nÃ¥r» i installasjonsprogrammet.
        self.assertTrue(ISS.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_removes_old_startup_shortcut(self):
        # lag-snarvei.ps1 -Oppstart startet Python-versjonen på samme port ved pålogging.
        iss = ISS.read_text(encoding="utf-8-sig")
        self.assertIn('Name: "{userstartup}\\Lemus fjernkontroll.lnk"', iss)
        self.assertIn('Name: "{userstartup}\\SUMEL fjernkontroll.lnk"', iss)
        ps1 = (ROOT / "lag-snarvei.ps1").read_text(encoding="ascii")
        self.assertIn("'SUMEL fjernkontroll.lnk'", ps1)
        self.assertIn("'Lemus fjernkontroll.lnk'", ps1)  # den gamle fjernes

    def test_new_name_same_appid(self):
        iss = ISS.read_text(encoding="utf-8-sig")
        # Samme AppId som Lemus Remote: oppgraderer den gamle installasjonen på plass.
        self.assertIn("AppId={{6D1C2B0E-4F3A-4B8E-9C61-5A7E2F1D0B42}", iss)
        self.assertIn('#define AppName "SUMEL"', iss)
        self.assertIn('#define AppExe "SUMEL.exe"', iss)
        self.assertIn("OutputBaseFilename=SUMEL-Setup-{#AppVersion}", iss)
        self.assertIn('Source: "dist\\SUMEL\\*"', iss)
        self.assertIn("DefaultDirName={autopf}\\{#AppName}", iss)

    def test_cleans_up_lemus_remote(self):
        iss = ISS.read_text(encoding="utf-8-sig")
        self.assertIn('#define OldExe "LemusRemote.exe"', iss)
        self.assertIn('#define OldFirewallRule "Lemus Remote"', iss)
        # Kjørende programmer avsluttes før filene byttes (begge navn), feil ignoreres.
        code = iss.split("[Code]", 1)[1]
        self.assertIn("'/F /IM {#AppExe}'", code)
        self.assertIn("'/F /IM {#OldExe}'", code)
        # Gammel brannmurregel fjernes, ny lages for SUMEL.exe på private nettverk.
        run = iss.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
        self.assertIn('delete rule name=""{#OldFirewallRule}""', run)
        self.assertIn('add rule name=""{#FirewallRule}"" dir=in action=allow program=""{app}\\{#AppExe}""', run)
        self.assertIn("profile=private", run)
        self.assertIn('#define FirewallRule "SUMEL"', iss)
        # Gamle snarveier og exe-filen fjernes.
        self.assertIn('Type: files; Name: "{app}\\{#OldExe}"', iss)
        self.assertIn('Type: files; Name: "{autodesktop}\\{#OldName}.lnk"', iss)
        self.assertIn('Type: files; Name: "{autoprograms}\\{#OldName}\\{#OldName}.lnk"', iss)
        # Avinstallering fjerner begge brannmurreglene og stopper begge programmene.
        un = iss.split("[UninstallRun]", 1)[1].split("[UninstallDelete]", 1)[0]
        for name in ("{#FirewallRule}", "{#OldFirewallRule}"):
            self.assertIn(f'delete rule name=""{name}""', un)
        for exe in ("{#AppExe}", "{#OldExe}"):
            self.assertIn(f"/F /IM {exe}", un)
        # Avinstallering rydder også alle gamle snarveier som installasjonen ville fjernet.
        undel = iss.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
        for lnk in ("{autoprograms}\\{#OldName}\\{#OldName}.lnk", "{autoprograms}\\{#OldName}.lnk",
                    "{autodesktop}\\{#OldName}.lnk", "{userdesktop}\\{#OldName}.lnk"):
            self.assertIn(f'Type: files; Name: "{lnk}"', undel)
        # Innstillingsmappen beholder det gamle navnet (forklart i skriptet).
        self.assertIn("%APPDATA%\\LemusRemote", iss)

    def test_disclaimer_in_both_languages(self):
        iss = ISS.read_text(encoding="utf-8-sig")
        self.assertIn("ikke laget av Lemus eller Platin", iss)
        self.assertIn("not made by Lemus or Platin", iss)
        self.assertIn("DisableWelcomePage=no", iss)

    def test_spec_builds_sumel_exe(self):
        spec = SPEC.read_text(encoding="utf-8")
        self.assertIn('NAME = "SUMEL"', spec)
        self.assertIn("name=NAME", spec)
        self.assertIn('"icon-512.png"', spec)
        self.assertTrue((ROOT / "packaging" / "sumel-reserve.ico").exists())
        self.assertFalse((ROOT / "packaging" / "lemus.spec").exists())
        build = (ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("packaging\\sumel.spec", build)
        self.assertIn("packaging\\dist\\SUMEL\\SUMEL.exe", build)
        self.assertIn("SUMEL-Setup-*.exe", build)

    @unittest.skipUnless(WORKFLOW.exists(), "workflow-filen finnes ikke i denne utsjekken")
    def test_workflow_uses_new_names(self):
        wf = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: SUMEL-Setup", wf)
        self.assertIn("packaging/Output/SUMEL-Setup-*.exe", wf)
        self.assertIn("packaging\\dist\\SUMEL\\SUMEL.exe", wf)
        self.assertNotIn("LemusRemote-Setup", wf)
        self.assertNotIn("LemusRemote.exe", wf)
        # Kjøres bare manuelt eller på versjonstagger (koster penger på private repoer).
        self.assertIn("workflow_dispatch:", wf)
        self.assertIn("-match '<title>SUMEL'", wf)  # en tilbakefallen «Lemus»-tittel skal ikke godtas
        self.assertNotIn("SUMEL|Lemus", wf)
        self.assertIsNone(re.search(r"^\s*push:\s*\n\s*branches", wf, re.M))

    @unittest.skipIf(os.name == "nt", "på Windows finnes winreg")
    def test_without_winreg_does_nothing(self):
        a = tray.Autostart(command="x")
        self.assertFalse(a.available)
        self.assertFalse(a.is_enabled())
        a.set(True)
        self.assertFalse(a.is_enabled())


class Helpers(unittest.TestCase):
    def test_clipboard_windows_uses_clip(self):
        calls = []
        self.assertTrue(tray.copy_to_clipboard("http://192.168.0.9:8080", run=lambda cmd, **kw: calls.append((cmd, kw)),
                                               windows=True))
        self.assertEqual(calls[0][0], ["clip"])
        self.assertEqual(calls[0][1]["input"], b"http://192.168.0.9:8080")
        calls.clear()
        tray.copy_to_clipboard("Åpne", run=lambda cmd, **kw: calls.append((cmd, kw)), windows=True)
        self.assertEqual(calls[0][1]["input"], b"\xff\xfe" + "Åpne".encode("utf-16-le"))

    def test_clipboard_tries_next_tool_and_gives_up(self):
        tried = []

        def run(cmd, **kw):
            tried.append(cmd[0])
            raise FileNotFoundError(cmd[0])
        self.assertFalse(tray.copy_to_clipboard("x", run=run, windows=False))
        self.assertEqual(tried, ["pbcopy", "xclip", "wl-copy"])

    def test_log_path_next_to_config(self):
        self.assertEqual(tray.log_path(Path("/a/b/config.json")), Path("/a/b/lemus.log"))
        with mock.patch.dict(os.environ, {"LEMUS_REMOTE_CONFIG": str(Path("/x/y/config.json"))}):
            self.assertEqual(tray.log_path(None), Path("/x/y/lemus.log"))

    def test_log_stream_writes_lines(self):
        logger = logging.getLogger("lemus_remote.test_tray")
        with self.assertLogs(logger, level="INFO") as cm:
            s = tray._LogStream(logger, logging.INFO)
            print("  LEMUS REMOTE", file=s)
            s.write("del ")
            s.write("to\n\n")
            s.write("rest")
            s.flush()
        self.assertEqual([r.getMessage() for r in cm.records], ["  LEMUS REMOTE", "del to", "rest"])

    def test_log_stream_does_not_recurse(self):
        logger = mock.Mock()
        s = tray._LogStream(logger, logging.INFO)
        logger.log.side_effect = lambda *a: s.write("feil i loggen\n")
        s.write("linje\n")
        logger.log.assert_called_once_with(logging.INFO, "linje")

    def test_icon_from_static_or_fallback(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow mangler")
        img = tray.icon_image()
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.mode, "RGBA")
        # Mangler bildene (f.eks. midt i et bytte av ikoner), brukes det tegnede reserveikonet.
        with mock.patch.object(tray, "ICON_FILES", ("finnes-ikke.png",)):
            img = tray.icon_image(32)
        self.assertEqual(img.size, (32, 32))
        self.assertEqual(tray.fallback_icon(32).tobytes(), img.tobytes())

    def test_icon_prefers_512(self):
        self.assertEqual(tray.ICON_FILES[0], "icon-512.png")

    def test_no_display_means_no_icon(self):
        if os.name == "nt":
            self.skipTest("Windows har alltid skrivebord")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(tray.load_pystray())


class StartupErrors(unittest.TestCase):
    def test_only_address_in_use_is_port_busy(self):
        self.assertTrue(tray.is_port_busy(OSError(errno.EADDRINUSE, "in use")))
        self.assertTrue(tray.is_port_busy(OSError(10048, "WSAEADDRINUSE")))
        self.assertFalse(tray.is_port_busy(PermissionError(errno.EACCES, "no access to config")))
        self.assertFalse(tray.is_port_busy(OSError("network card")))
        self.assertFalse(tray.is_port_busy(ValueError("x")))
        self.assertFalse(tray.is_port_busy(None))

    def _main_with_error(self, err):
        d = tempfile.mkdtemp()
        args = app.build_parser().parse_args(["--tray", "--background", "--config", str(Path(d) / "c.json"),
                                              "--port", str(free_port())])

        class Failing:
            def __init__(self, a):
                self.ready, self.running, self.error = threading.Event(), None, err

            def start(self):
                self.ready.set()

        root = logging.getLogger()
        old = (list(root.handlers), root.level, sys.stdout, sys.stderr)
        try:
            with mock.patch.object(tray, "ServerThread", Failing), \
                    mock.patch.object(tray, "message_box") as box:
                self.assertEqual(tray.main(args), 1)
        finally:
            for h in list(root.handlers):
                root.removeHandler(h)
                h.close()
            for h in old[0]:
                root.addHandler(h)
            root.setLevel(old[1])
            sys.stdout, sys.stderr = old[2], old[3]
        return box.call_args.args[0]

    def test_main_port_busy_message(self):
        self.assertIn("opptatt" if tray.ui_language() == "no" else "used by another program",
                      self._main_with_error(OSError(errno.EADDRINUSE, "in use")))

    def test_main_other_oserror_points_to_log(self):
        msg = self._main_with_error(PermissionError(errno.EACCES, "denied"))
        self.assertIn("lemus.log", msg)
        self.assertNotIn("opptatt", msg)


class TrayMode(unittest.TestCase):
    def test_tray_flag_and_frozen_default(self):
        p = app.build_parser()
        self.assertFalse(app.use_tray(p.parse_args([])))
        self.assertTrue(app.use_tray(p.parse_args(["--tray"])))
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(app.use_tray(p.parse_args([])))
            self.assertFalse(app.use_tray(p.parse_args(["--no-tray"])))

    def test_console_mode_unchanged(self):
        with mock.patch.object(app.asyncio, "run") as run, mock.patch.object(tray, "main") as tmain:
            run.side_effect = lambda coro: coro.close()
            app.cli(["--port", "9999"])
        run.assert_called_once()
        tmain.assert_not_called()


class FakePystray:
    """Nok av pystray til å bygge menyen."""

    class MenuItem:
        def __init__(self, text, action, checked=None, default=False):
            self.text, self.action, self.checked, self.default = text, action, checked, default

        def label(self):
            return self.text(self) if callable(self.text) else self.text

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items


class TrayMenu(unittest.TestCase):
    def make(self, lan_url="http://192.168.0.9:8080", code="123456"):
        running = mock.Mock(local_url="http://localhost:8080", lan_url=lan_url, access_code=code)
        server = mock.Mock(running=running)
        a = tray.Autostart(winreg_mod=FakeReg(), command="x")
        t = tray.TrayApp(server, FakePystray, "no", a)
        t.icon = mock.Mock()
        return t, server

    def items(self, t):
        return [i for i in t.menu().items if i is not FakePystray.Menu.SEPARATOR]

    def test_menu_matches_labels(self):
        t, _ = self.make()
        items = self.items(t)
        self.assertEqual([i.label() for i in items], tray.menu_labels("no", "http://192.168.0.9:8080", "123456"))
        self.assertTrue(items[0].default)  # dobbeltklikk/klikk på ikonet åpner Lemus
        self.assertEqual(sum(1 for i in items if i.default), 1)

    def test_iphone_address_follows_new_ip(self):
        t, server = self.make()
        item = self.items(t)[1]
        server.running.lan_url = "http://10.0.0.5:8080"
        self.assertEqual(item.label(), "Adresse for iPhone: http://10.0.0.5:8080")

    def test_double_click_opens_one_tab(self):
        t, _ = self.make()
        clock = iter([100.0, 100.3, 103.0])
        with mock.patch.object(tray.webbrowser, "open") as browser:
            for _ in range(3):
                t.open_app(now=lambda: next(clock))
        self.assertEqual(browser.call_count, 2)  # 100.0 og 103.0; 100.3 er andre halvdel av dobbeltklikket

    def test_menu_rebuilt_when_address_changes(self):
        # pystray på Windows viser en bufret meny: update_menu må kalles når adressen endres.
        t, server = self.make(lan_url="http://127.0.0.1:8080")
        t._shown_url = "http://127.0.0.1:8080"
        self.assertFalse(t.refresh_menu())
        t.icon.update_menu.assert_not_called()
        server.running.lan_url = "http://192.168.0.9:8080"  # Wi-Fi kom opp etter oppstart
        self.assertTrue(t.refresh_menu())
        self.assertEqual(t.icon.update_menu.call_count, 1)
        self.assertFalse(t.refresh_menu())
        self.assertEqual(t.icon.update_menu.call_count, 1)

    def test_run_watches_address_and_stops_with_server(self):
        t, _ = self.make(lan_url="http://127.0.0.1:8080")
        alive = threading.Event()
        alive.set()

        class Server(threading.Thread):
            running = t.server.running

            def run(self):
                while alive.is_set():
                    time.sleep(0.01)

        server = Server(daemon=True)
        t.server = server
        updates = []

        class Icon:
            def __init__(self, *a):
                self.stopped = threading.Event()

            def update_menu(self):
                updates.append(server.running.lan_url)

            def run(self, setup):
                setup(self)
                self.stopped.wait(10)

            def stop(self):
                self.stopped.set()

        ps = mock.Mock(Icon=Icon, MenuItem=FakePystray.MenuItem, Menu=FakePystray.Menu)
        t.pystray = ps
        server.start()

        def later():
            time.sleep(0.15)
            server.running.lan_url = "http://192.168.0.9:8080"
            time.sleep(0.15)
            alive.clear()  # serveren stopper: ikonet skal forsvinne

        with mock.patch.object(tray, "MENU_POLL", 0.02), mock.patch.object(tray, "icon_image"):
            threading.Thread(target=later, daemon=True).start()
            done = threading.Thread(target=t.run, daemon=True)
            done.start()
            done.join(10)
        self.assertFalse(done.is_alive())
        self.assertEqual(updates, ["http://192.168.0.9:8080"])

    def test_actions(self):
        t, server = self.make()
        open_, iphone, code, auto, quit_ = self.items(t)
        with mock.patch.object(tray.webbrowser, "open") as browser:
            open_.action(t.icon, open_)
        browser.assert_called_once_with("http://localhost:8080")
        with mock.patch.object(tray.webbrowser, "open") as browser:
            t.server.running.lan_url = None  # bare denne PC-en: «Adresse for iPhone» åpner Lemus
            iphone.action(t.icon, iphone)
        browser.assert_called_once_with("http://localhost:8080")
        t.server.running.lan_url = "http://192.168.0.9:8080"
        with mock.patch.object(tray, "copy_to_clipboard", return_value=True) as copy:
            iphone.action(t.icon, iphone)
            code.action(t.icon, code)
        self.assertEqual([c.args[0] for c in copy.call_args_list], ["http://192.168.0.9:8080", "123456"])
        self.assertEqual(t.icon.notify.call_count, 2)
        self.assertFalse(auto.checked(auto))
        auto.action(t.icon, auto)
        self.assertTrue(auto.checked(auto))
        t.icon.update_menu.assert_called()
        with mock.patch.object(tray.threading, "Timer") as timer:
            quit_.action(t.icon, quit_)
        server.stop.assert_called_once()
        timer.assert_called_once_with(5.0, t.stop_icon)
        t.stop_icon()
        t.stop_icon()  # bare én gang
        t.icon.stop.assert_called_once()


class ServerInThread(unittest.TestCase):
    """Samme server som ``py -m lemus_remote``, startet og stoppet slik tray-ikonet gjør."""

    def test_start_answer_and_stop(self):
        d = tempfile.mkdtemp()
        port = free_port()
        args = app.build_parser().parse_args(["--tray", "--background", "--config", str(Path(d) / "config.json"),
                                              "--host", "127.0.0.1", "--port", str(port), "--no-scan",
                                              "--no-luci", "--local-only"])
        server = tray.ServerThread(args)
        with mock.patch("sys.stdout", new=io.StringIO()):
            server.start()
            self.assertTrue(server.ready.wait(15))
            self.assertIsNone(server.error)
            self.assertEqual(server.running.port, port)
            self.assertIsNone(server.running.lan_url)  # --local-only
            self.assertTrue(tray.already_running(port, timeout=3.0))
            server.stop()
            server.join(15)
        self.assertFalse(server.is_alive())
        self.assertIsNone(server.error)

    def test_port_busy_is_reported(self):
        d = tempfile.mkdtemp()
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        args = app.build_parser().parse_args(["--tray", "--config", str(Path(d) / "config.json"),
                                              "--host", "127.0.0.1", "--port", str(port), "--no-scan",
                                              "--no-luci", "--local-only"])
        args.open = False
        try:
            server = tray.ServerThread(args)
            with mock.patch("sys.stdout", new=io.StringIO()):
                server.start()
                self.assertTrue(server.ready.wait(15))
                server.join(15)
            self.assertIsNone(server.running)
            self.assertIsInstance(server.error, OSError)
            self.assertTrue(tray.is_port_busy(server.error))
        finally:
            busy.close()


if __name__ == "__main__":
    unittest.main()
