"""Kleiner, robuster JSON-Speicher fuer Bot-Daten (Level, Flo Coins, Spielstaende).

Bewusst OHNE externe Abhaengigkeit - nur Standardbibliothek. Eigenschaften:
- Atomar: schreibt erst in eine .tmp-Datei und benennt sie dann um (os.replace).
  So zerstoert ein Absturz mitten im Schreiben die alten Daten nicht.
- Async-sicher: ein asyncio.Lock serialisiert die Schreibzugriffe, das eigentliche
  Schreiben laeuft in einem Thread (to_thread), blockiert also den Bot nicht.
- Faellt das Laden aus (kaputte Datei), startet der Store leer statt zu crashen.

Jedes Feature legt sich einen eigenen JsonStore an (z. B. 'economy.json',
'games.json') und verwaltet die Struktur seiner Daten selbst.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("dcbot.store")

# Datenordner (per .env ueberschreibbar). Wird beim ersten Schreiben angelegt.
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent / "data")))


class JsonStore:
    """Ein einfacher Schluessel-Wert-Speicher, der als JSON-Datei persistiert."""

    def __init__(self, name, default = None):
        self.path = DATA_DIR / name
        self._lock = asyncio.Lock()
        self.data = dict(default or {})
        self._load()

    def _load(self):
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except FileNotFoundError:
            return  # erster Start - Datei gibt's noch nicht
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Konnte %s nicht laden (%s) - starte leer.", self.path, exc)
            return
        if isinstance(loaded, dict):
            self.data.update(loaded)
        else:
            log.warning("Inhalt von %s ist kein Objekt - ignoriere.", self.path)

    async def save(self):
        """Schreibt den aktuellen Stand atomar auf die Platte.

        Wichtig: json.dumps laeuft SYNCHRON im Event-Loop (kein await), damit es
        einen in sich konsistenten Schnappschuss gibt - sonst koennte ein anderer
        Task das dict waehrend der Serialisierung aendern. Nur das (langsame)
        Schreiben auf die Platte wandert in einen Thread.
        """
        async with self._lock:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
            await asyncio.to_thread(self._write_text, payload)

    def _write_text(self, payload):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            # Erst vollstaendig auf die Platte zwingen (flush + fsync), DANN atomar
            # umbenennen - sonst kann nach einem Stromausfall das Rename da sein, die
            # Datenbloecke aber nicht (klassische leere/abgeschnittene Datei).
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)  # atomar
        except OSError as exc:
            log.error("Konnte %s nicht speichern: %s", self.path, exc)
