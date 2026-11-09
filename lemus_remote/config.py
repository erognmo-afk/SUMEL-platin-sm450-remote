"""Innstillinger og lokale data for Lemus Remote.

Lagres som JSON:
* Windows: ``%APPDATA%\\LemusRemote\\config.json``
* ellers:  ``~/.config/lemus-remote/config.json``
* eller filen i miljøvariabelen ``LEMUS_REMOTE_CONFIG`` / ``--config``.

Innhold: høyttalerens adresse (valgfri, ellers automatisk oppdaging), siste adresse som
virket, tilgangskode
for lokalnettet, lagrede lydprofiler og filterformler som er lært fra Platin-appen.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any


def default_path() -> Path:
    env = os.environ.get("LEMUS_REMOTE_CONFIG")
    if env:
        return Path(env)
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "LemusRemote" / "config.json"
    return Path.home() / ".config" / "lemus-remote" / "config.json"


DEFAULTS: dict[str, Any] = {
    "host": None,          # None = finn høyttaleren automatisk
    "last_host": None,     # siste adresse vi faktisk var tilkoblet (brukes som hint ved søk)
    "name": None,
    "udn": None,
    "port": 8080,
    "access_code": None,   # lages ved første start
    "profiles": [],        # brukerens egne profiler
    "learned_formulas": {},  # type_code (str) -> formelnavn
    "disable_mode": None,    # hvordan Platin skriver et avslått bånd, når det er lært
    "max_volume_step": 25,   # største volumhopp i én kommando
}


class Config:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()
        if not self.data.get("access_code"):
            self.data["access_code"] = f"{secrets.randbelow(10**6):06d}"
            self.save()

    def load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def __getitem__(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()
