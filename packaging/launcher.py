"""Startpunkt for PyInstaller (SUMEL.exe). Samme som ``py -m lemus_remote``.

I det frosne programmet er tray-modus standard (se ``lemus_remote.__main__.use_tray``).
"""

from lemus_remote.__main__ import cli

if __name__ == "__main__":
    cli()
