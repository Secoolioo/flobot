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
import shutil
import time
import weakref
from pathlib import Path

log = logging.getLogger("dcbot.store")

# Datenordner (per .env ueberschreibbar). Wird beim ersten Schreiben angelegt.
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent / "data")))

#: Wie lange gesammelt wird, bevor wirklich geschrieben wird (Sekunden).
#: Klein halten: in diesem Fenster liegt eine Buchung nur im Speicher, ein
#: kill -9 wuerde sie kosten. Eine Sekunde reicht, um aus 30 Serialisierungen
#: einer Casino-Runde eine einzige zu machen.
SAMMELZEIT = float(os.getenv("STORE_SAVE_DEBOUNCE", "1.0") or 1.0)

#: Alle lebenden Stores - fuer alle_sichern() vor einem Neustart.
#: Schwache Verweise, damit ein Store, den niemand mehr haelt, verschwinden darf.
_ALLE = weakref.WeakSet()


async def alle_sichern():
    """Alles, was noch aussteht, sofort auf die Platte - vor einem Neustart.

    Ohne das wuerde das gesammelte Speichern (save_soon) beim Neustart die
    letzte Sekunde Coins und XP verlieren. bot._restart_bot hat solche
    Einzelaufrufe schon fuer den Wort-Zaehler und das Gedaechtnis; diese Stelle
    hier gilt fuer ALLE Stores, auch fuer die, die es noch gar nicht gibt.
    """
    for laden in list(_ALLE):
        try:
            await laden.flush_now()
        except Exception:  # noqa: BLE001 - ein kaputter Store darf die anderen
            log.exception("Sichern von %s vor dem Neustart fehlgeschlagen",
                          getattr(laden, "path", "?"))


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
        self._verdaechtig = False   # schon eine Sicherheitskopie angelegt?
        # Gesammeltes Speichern (siehe save_soon).
        self._schmutzig = False
        self._sammler = None
        self._laufende = set()
        self._load()
        _ALLE.add(self)

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
            # ERST sichern, dann zuruecksetzen. Ohne das war der alte Inhalt
            # beim naechsten save() endgueltig weg: der Schluessel wird durch
            # den Standard ersetzt, und save() schreibt genau den auf die
            # Platte. Der Lese-Weg legt eine kaputte Datei laengst beiseite
            # (_beiseite), dieser Weg tat es als einziger nicht.
            #
            # KOPIE, nicht verschieben: die uebrigen Schluessel der Datei sind
            # in Ordnung und sollen weiterlaufen.
            if not self._verdaechtig:
                self._verdaechtig = True
                if self.path.exists():
                    ziel = self.path.with_name(
                        f"{self.path.name}.kaputt-{time.strftime('%Y%m%d-%H%M%S')}")
                    try:
                        shutil.copy2(self.path, ziel)
                        log.error("%s: Sicherheitskopie unter %s abgelegt.",
                                  self.path.name, ziel.name)
                    except OSError as exc:
                        log.error("%s liess sich nicht sichern: %s", self.path.name, exc)
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
            self._beiseite(self.path)
            loaded = None
        if loaded is None:
            loaded = self._lies(self._bak)
            if loaded is not None:
                log.warning("%s: Sicherung .bak eingespielt (%d Eintraege).",
                            self.path.name, len(loaded))
            elif self._bak.exists():
                # Beide kaputt. Auch die Sicherung muss beiseite - sonst
                # ueberschreibt sie der zweite save() unwiederbringlich (der
                # erste laesst sie liegen, weil die Hauptdatei nach der
                # Quarantaene gar nicht mehr da ist). Genau das widersprach dem
                # Versprechen dieser Klasse: NICHTS still wegwerfen.
                self._beiseite(self._bak)
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

    def _beiseite(self, pfad):
        """Legt eine kaputte Datei mit Zeitstempel zur Seite, statt sie zu verlieren."""
        ziel = pfad.with_name(
            f"{pfad.name}.kaputt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(pfad, ziel)
            log.error("%s war kaputt und liegt jetzt unter %s - der Bot startet "
                      "mit der Sicherung bzw. leer weiter. NICHTS wurde geloescht.",
                      pfad.name, ziel.name)
        except OSError as exc:
            log.error("%s ist kaputt und liess sich nicht beiseitelegen: %s",
                      pfad, exc)

    async def save(self):
        """Schreibt den aktuellen Stand atomar auf die Platte.

        Wichtig: json.dumps laeuft SYNCHRON im Event-Loop (kein await), damit es
        einen in sich konsistenten Schnappschuss gibt - sonst koennte ein anderer
        Task das dict waehrend der Serialisierung aendern. Nur das (langsame)
        Schreiben auf die Platte wandert in einen Thread.

        Weil dumps also echte Blockierzeit ist, wird KOMPAKT geschrieben. Das
        frueher genutzte indent=2 kostete gemessen das Vierfache: bei 3.000
        economy-Nutzern 42 ms statt 10 ms je Speichern - und economy wird nach
        jeder Coin-Bewegung geschrieben. Lesbar bleibt die Datei ueber
        `python3 -m json.tool`; ein Auslagern in einen Thread hilft hier nicht,
        weil der C-Encoder die GIL nicht freigibt.

        Rueckgabe: True = geschrieben, False = fehlgeschlagen (z. B. Platte
        voll). Frueher gab es hier gar keine Rueckmeldung - ein dauerhaft
        fehlschlagendes Speichern fiel nur im Log auf.
        """
        async with self._lock:
            payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
            return await asyncio.to_thread(self._write_text, payload)

    def save_soon(self):
        """Speichern SAMMELN statt bei jeder Aenderung neu zu schreiben.

        save() serialisiert den ganzen Store SYNCHRON im Event-Loop - das muss
        so sein, damit der Schnappschuss in sich stimmt, aber es ist echte
        Blockierzeit fuer alle. Bei economy sind das mit 3.000 Nutzern rund
        20 ms pro Aufruf, und economy wird nach JEDER Coin-Bewegung geschrieben.
        Eine Casino-Runde mit 30 Buchungen hielt den Bot damit eine halbe
        Sekunde lang immer wieder an.

        Jetzt laeuft hoechstens EIN Sammel-Task je Store. Kommt waehrend des
        Wartens etwas Neues dazu, wird danach genau einmal nachgespeichert.

        Der Preis, offen benannt: in diesem Fenster liegt die Aenderung nur im
        Speicher. Ein kill -9 kostet sie. Deshalb ist das Fenster klein und
        alle_sichern() haengt im Neustart-Weg.

        (handel.py hat dieses Muster als Erstes gebaut und dabei begruendet -
        hier steht es jetzt einmal fuer alle Stores.)
        """
        self._schmutzig = True
        if self._sammler is not None and not self._sammler.done():
            return
        try:
            self._sammler = asyncio.get_running_loop().create_task(self._sammeln())
        except RuntimeError:
            # Kein laufender Loop (Tests, Werkzeuge). Dann bleibt es schmutzig
            # und der naechste Aufruf mit Loop schreibt mit.
            self._sammler = None
            return
        self._laufende.add(self._sammler)
        self._sammler.add_done_callback(self._laufende.discard)

    async def _sammeln(self):
        """Schreibt, solange zwischendurch neue Aenderungen aufgelaufen sind."""
        try:
            while self._schmutzig:
                self._schmutzig = False
                await asyncio.sleep(SAMMELZEIT)
                await self.save()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - Speichern darf nie etwas sprengen
            log.exception("Gesammeltes Speichern von %s fehlgeschlagen", self.path)

    async def flush_now(self):
        """Sofort schreiben, wenn etwas aussteht. Sonst nichts tun."""
        if not self._schmutzig and (self._sammler is None or self._sammler.done()):
            return False
        self._schmutzig = False
        if self._sammler is not None and not self._sammler.done():
            self._sammler.cancel()
        return await self.save()

    def _write_text(self, payload):
        tmp = None
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # Der Zwischenname traegt die Prozess-ID: laeuft der Bot aus Versehen
            # zweimal (oder greift ein Werkzeug auf dieselbe Datei zu), zogen sich
            # sonst beide denselben Puffer unter den Fuessen weg.
            tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
            # Erst vollstaendig auf die Platte zwingen (flush + fsync), DANN atomar
            # umbenennen - sonst kann nach einem Stromausfall das Rename da sein, die
            # Datenbloecke aber nicht (klassische leere/abgeschnittene Datei).
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # Den bisherigen Stand als .bak behalten, BEVOR er ueberschrieben
            # wird - rettet genau den Fall, in dem die Hauptdatei nach einem
            # Stromausfall unlesbar ist.
            #
            # Per HARDLINK, nicht per Rename: ein Rename hat die Hauptdatei kurz
            # WEGgenommen. In diesem Fenster gab es economy.json schlicht nicht,
            # und wer dann las (ein zweiter _load, das Panel, repair_db.sh),
            # bekam ENOENT oder fiel auf den veralteten .bak-Stand zurueck.
            # Schlug danach das zweite os.replace fehl, war die Hauptdatei
            # dauerhaft verschwunden. Der Link kostet nur Metadaten und zeigt
            # weiter auf den alten Inhalt, wenn tmp gleich darueber gehaengt wird.
            if self.path.exists():
                try:
                    if self._bak.exists():
                        self._bak.unlink()
                    os.link(self.path, self._bak)
                except OSError:
                    pass                       # Sicherung ist nice-to-have
            os.replace(tmp, self.path)  # atomar
        except OSError as exc:
            log.error("Konnte %s nicht speichern: %s", self.path, exc, exc_info=True)
            # Keine .tmp-Leiche liegen lassen (sonst sammeln sich die bei einer
            # vollen Platte).
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return False
        return True
