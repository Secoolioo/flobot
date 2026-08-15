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
import copy
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("dcbot.store")

# Datenordner (per .env ueberschreibbar). Wird beim ersten Schreiben angelegt.
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent / "data")))


class JsonStore:
    """Ein einfacher Schluessel-Wert-Speicher, der als JSON-Datei persistiert."""

    def __init__(self, name, default = None):
        self.path = DATA_DIR / name
        # Letzter GUTER Stand. Wird bei jedem Speichern mitgezogen und ist die
        # Rettung, wenn die Hauptdatei unlesbar wird.
        self._bak = self.path.with_name(self.path.name + ".bak")
        self._lock = asyncio.Lock()
        # Der Standard ist zugleich die TYP-SCHABLONE (siehe _schablone_pruefen).
        self._default = copy.deepcopy(default or {})
        self.data = copy.deepcopy(self._default)
        self._load()

    def _schablone_pruefen(self):
        """Jeder Schluessel aus dem Standard muss auch dessen TYP haben.

        Sonst kippt der Bot beim START: eine von Hand editierte oder halb
        geschriebene Datei mit "users": null hat in economy.setup() ein
        TypeError geworfen, und weil die setup()-Kette in bot.py auf
        Modulebene laeuft, kam der GANZE Bot nicht mehr hoch - nicht nur das
        eine Feature. Nachgemessen galt das fuer economy, words, features,
        lotto, giveaway und schulden.

        Repariert wird nur der betroffene Schluessel, alles andere bleibt
        stehen - und es wird laut geloggt, damit es niemand uebersieht."""
        for key, muster in self._default.items():
            wert = self.data.get(key, None)
            if self._passt(wert, muster):
                continue
            log.error("%s: '%s' hat den falschen Typ (%s statt %s) - "
                      "setze diesen Schluessel auf den Standard zurueck.",
                      self.path.name, key, type(wert).__name__, type(muster).__name__)
            self.data[key] = copy.deepcopy(muster)

    @staticmethod
    def _passt(wert, muster):
        """Passt der geladene Wert zum Typ des Standards?"""
        if isinstance(muster, bool):
            return isinstance(wert, bool)
        if isinstance(muster, (int, float)):
            # Zahl bleibt Zahl - int und float duerfen sich mischen (JSON macht
            # aus 0 gern 0.0), ein bool ist hier aber keine Zahl.
            return isinstance(wert, (int, float)) and not isinstance(wert, bool)
        if isinstance(muster, dict):
            return isinstance(wert, dict)
        if isinstance(muster, list):
            return isinstance(wert, list)
        if isinstance(muster, str):
            return isinstance(wert, str)
        return True          # exotischer Standard -> nicht hineinreden

    def _load(self):
        """Laedt den Stand. Bei kaputter Datei: NICHTS still wegwerfen.

        Vorher hiess "kaputt" schlicht: leer starten - und der erste save()
        wenige Sekunden spaeter hat die kaputte, aber vielleicht noch rettbare
        Datei mit einer leeren ueberschrieben. Bei economy.json waeren das alle
        Coins, Level und Voice-Stunden gewesen, endgueltig und unbemerkt.
        Jetzt wird die kaputte Datei beiseitegelegt und die letzte gute
        Sicherung (.bak) genommen."""
        loaded = self._lies(self.path)
        if loaded is None and self.path.exists():
            # Datei ist da, aber unlesbar -> beiseitelegen, damit sie der
            # naechste save() NICHT ueberschreibt.
            self._beiseite()
            loaded = None
        if loaded is None:
            loaded = self._lies(self._bak)
            if loaded is not None:
                log.warning("%s: Sicherung .bak eingespielt (%d Eintraege).",
                            self.path.name, len(loaded))
        if isinstance(loaded, dict):
            self.data.update(loaded)
        self._schablone_pruefen()

    def _lies(self, pfad):
        """Liest EINE Datei. Rueckgabe: dict, oder None wenn nicht nutzbar."""
        try:
            with pfad.open("r", encoding="utf-8") as fh:
                inhalt = json.load(fh)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log.error("%s ist nicht lesbar (%s).", pfad, exc)
            return None
        if not isinstance(inhalt, dict):
            log.error("%s enthaelt kein Objekt - ignoriert.", pfad)
            return None
        return inhalt

    def _beiseite(self):
        """Legt eine kaputte Datei mit Zeitstempel zur Seite, statt sie zu verlieren."""
        ziel = self.path.with_name(
            f"{self.path.name}.kaputt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(self.path, ziel)
            log.error("%s war kaputt und liegt jetzt unter %s - der Bot startet "
                      "mit der Sicherung bzw. leer weiter. NICHTS wurde geloescht.",
                      self.path.name, ziel.name)
        except OSError as exc:
            log.error("%s ist kaputt und liess sich nicht beiseitelegen: %s",
                      self.path, exc)

    async def save(self):
        """Schreibt den aktuellen Stand atomar auf die Platte.

        Wichtig: json.dumps laeuft SYNCHRON im Event-Loop (kein await), damit es
        einen in sich konsistenten Schnappschuss gibt - sonst koennte ein anderer
        Task das dict waehrend der Serialisierung aendern. Nur das (langsame)
        Schreiben auf die Platte wandert in einen Thread.

        Rueckgabe: True = geschrieben, False = fehlgeschlagen (z. B. Platte
        voll). Frueher gab es hier gar keine Rueckmeldung - ein dauerhaft
        fehlschlagendes Speichern fiel nur im Log auf.
        """
        async with self._lock:
            payload = json.dumps(self.data, ensure_ascii=False, indent=2)
            return await asyncio.to_thread(self._write_text, payload)

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
            # Den bisherigen Stand als .bak behalten, BEVOR er ueberschrieben
            # wird. Kostet nur ein Rename (Metadaten), rettet aber genau den
            # Fall, in dem die Hauptdatei nach einem Stromausfall unlesbar ist.
            if self.path.exists():
                try:
                    os.replace(self.path, self._bak)
                except OSError:
                    pass                       # Sicherung ist nice-to-have
            os.replace(tmp, self.path)  # atomar
        except OSError as exc:
            log.error("Konnte %s nicht speichern: %s", self.path, exc)
            return False
        return True
