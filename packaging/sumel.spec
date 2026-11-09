# PyInstaller-oppskrift for SUMEL.exe (én mappe, uten konsollvindu).
# SUMEL – fjernkontroll for Platin SM450 (uoffisiell; het tidligere Lemus Remote).
#
#   cd SUMEL
#   pyinstaller --noconfirm --clean packaging/sumel.spec
#
# Resultat: dist/SUMEL/SUMEL.exe (+ _internal/). Inno Setup (installer.iss)
# pakker hele mappen. Samme oppskrift bygger også på Linux (brukes til å teste oppskriften).
#
# Ikonet lages ved hvert bygg fra lemus_remote/static/icon-512.png (samme bilde som appen
# og ikonet ved klokken) til packaging/build/SUMEL.ico, som installer.iss også bruker.
# Mangler Pillow eller bildet, brukes packaging/sumel-reserve.ico.
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # repoens rot
STATIC = ROOT / "lemus_remote" / "static"
NAME = "SUMEL"
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "lemus_remote" / "__init__.py").read_text(encoding="utf-8")).group(1)


def version_file():
    """Versjonsinfo i exe-filens egenskaper (bare Windows)."""
    if sys.platform != "win32":
        return None
    nums = tuple((list(map(int, re.findall(r"\d+", VERSION))) + [0, 0, 0, 0])[:4])
    path = Path(workpath) / "version_info.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('041404b0', [
      StringStruct('CompanyName', 'SUMEL (uoffisiell)'),
      StringStruct('FileDescription', 'SUMEL – fjernkontroll for Platin SM450'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'SUMEL'),
      StringStruct('LegalCopyright', 'Uoffisiell. Ikke laget av Lemus eller Platin.'),
      StringStruct('OriginalFilename', 'SUMEL.exe'),
      StringStruct('ProductName', 'SUMEL'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [0x0414, 1200])])]
)
""", encoding="utf-8")
    return str(path)


def app_icon():
    """packaging/build/SUMEL.ico fra static/icon-512.png, ellers reserveikonet."""
    reserve = ROOT / "packaging" / "sumel-reserve.ico"
    src = STATIC / "icon-512.png"
    out = ROOT / "packaging" / "build" / f"{NAME}.ico"
    try:
        from PIL import Image
        out.parent.mkdir(parents=True, exist_ok=True)
        img = Image.open(src).convert("RGBA")
        img.save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        print(f"SUMEL: ikon laget fra {src.name} -> {out}")
        return str(out)
    except Exception as e:  # noqa: BLE001 – Pillow eller bildet mangler
        print(f"SUMEL: bruker reserveikonet ({e})")
        if out.exists():
            out.unlink()  # så installer.iss ikke bruker et gammelt ikon
        return str(reserve)


# webserveren leser filene med importlib.resources fra lemus_remote/static.
# Hele mappen (også undermapper) kopieres.
datas = [(str(STATIC), "lemus_remote/static")]

hidden = collect_submodules("lemus_remote")
try:  # pystray velger ikon-motor ved kjøring, så PyInstaller ser den ikke selv
    import pystray  # noqa: F401
    hidden += ["pystray._win32"] if sys.platform == "win32" else ["pystray._xorg", "pystray._util"]
except Exception:  # noqa: BLE001 – uten skjerm kan Xlib kaste ved import
    hidden += ["pystray._win32"] if sys.platform == "win32" else []

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=app_icon(),
    # Ingen konsollvindu. Loggen ligger i %APPDATA%\LemusRemote\lemus.log: mappen har
    # beholdt det gamle navnet, så innstillingene følger med ved oppgradering.
    console=False,
    upx=False,      # UPX gir oftere falsk alarm i antivirus
    version=version_file(),
)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
