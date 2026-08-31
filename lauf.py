"""Testlauf fuer Flo - sammelt ALLE Fehler, statt beim ersten abzubrechen.

Der alte Runner (in test_games_logic.py) rief die Tests der Reihe nach auf und
liess den ersten AssertionError durchschlagen. Nach einem Umbau sah man damit
EINEN Fehler statt ALLER - man reparierte, lief neu, sah den naechsten. Bei
einem groesseren Umbau ist das der teuerste Teil des Tages.

    python lauf.py                  alle Tests, alle Fehler auf einmal
    python lauf.py --nur musik      nur Tests, deren Name 'musik' enthaelt
    python lauf.py --misch          zufaellige Reihenfolge (Seed wird genannt)
    python lauf.py --misch 12345    dieselbe Mischung noch einmal
    python lauf.py --namen          nur die Testnamen ausgeben (zum Vergleichen)
    python lauf.py --leise          nur Fehler zeigen, keine ok-Zeilen

Warum --misch wichtig ist: der alte Runner lief streng alphabetisch. Ein Test,
der nur gruen ist, weil ein anderer vorher etwas hinterlassen hat, faellt dabei
nie auf - bis man die Suite aufteilt und die Reihenfolge sich aendert. Mischen
macht solche Abhaengigkeiten sichtbar, BEVOR sie wehtun.

Rueckgabecode 0 = alles gruen, 1 = mindestens ein Fehler.
"""

import argparse
import importlib
import os
import random
import sys
import tempfile
import time
import traceback

# Der Datenordner MUSS umgebogen sein, bevor irgendein Modul importiert wird -
# store.DATA_DIR wird beim Import festgelegt und jeder JsonStore haengt daran.
# Ohne das schreibt ein Testlauf in die ECHTEN Daten (siehe den ausfuehrlichen
# Kommentar im Kopf von testhilfe.py).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="flobot-lauf-"))

# Die Testdateien, die es gibt. Neue kommen hier dazu - ein Test haelt die
# Liste gegen den Ordner, damit keine vergessen wird.
TESTDATEIEN = (
    "test_logic",
    "test_aktie", "test_arbeit", "test_casino", "test_ki", "test_konfig",
    "test_moderation", "test_module", "test_musik", "test_panel",
    "test_profil", "test_schulden", "test_spiele",
    "test_werkzeug", "test_wirtschaft",
)


# Module, deren An/Aus-Schalter ein Test versehentlich stehen lassen kann.
# Der Runner stellt sie nach JEDEM Test zurueck und merkt sich, wer geschlampt
# hat. Ohne das haengt das Ergebnis an der Reihenfolge: 'test_admin_owner_gate'
# prueft die Meldung "Economy ist aus" und war nur deshalb gruen, weil er
# alphabetisch VOR allen Tests lief, die economy anschalten. Nachgemessen:
# sieben Tests lassen economy angeschaltet zurueck.
#: "Attribut gibt es nicht" - None geht nicht, denn None ist ein gueltiger Wert.
_FEHLT = object()

WACHE = ("economy", "features", "guildcfg", "admin", "music", "words")

#: Auch der Panel-Login: ein Test, der ihn abschaltet und stehen laesst, macht
#: den naechsten Panel-Test gruen, ohne dass der seine Anmeldung geprueft haette.
#: Was ein Test ausser den An/Aus-Schaltern noch stehen lassen kann. Nachgemessen
#: mit einer breiteren Wache: es sind nicht 9 schlampige Tests, sondern 25 - die
#: Hauptursache sind uebriggebliebene _store-Objekte und ein gefuellter
#: ai._RE_CACHE. Beim Aufteilen der Suite aendert sich die Reihenfolge, und dann
#: entscheidet so ein Rest darueber, ob ein Test gruen ist.
#: Form: (Modulname, Attributpfad ab dem Modul).
WACHE_TIEF = (
    ("economy", "instance._store"),
    ("games", "instance._store"),
    ("schulden", "instance._store"),
    ("arbeit", "instance._store"),
    ("handel", "instance._store"),
    ("words", "instance._store"),
    ("guildcfg", "instance._store"),
    ("features", "_store"),
    ("ai", "instance._RE_CACHE"),
    ("ai", "instance._LEAD_CACHE"),
    ("ai", "instance._client"),
    ("music", "_resolve_track"),
    ("music", "_send_panel"),
    ("music", "_retire_panel"),
    ("laufzeit", "protect_message"),
    ("laufzeit", "release_message"),
    ("laufzeit", "client"),
    ("webpanel", "instance._auth"),
    ("webpanel", "instance._log_store"),
)


class Ergebnis:
    """Was ein Lauf ergeben hat. Bewusst eine Klasse: der Runner soll sich
    genauso lesen wie der Rest des Repos."""

    def __init__(self):
        self.ok = []
        self.fehler = []      # [(name, kurz, traceback)]
        self.dauer = {}
        self.schlampig = []   # Tests, die globalen Zustand stehen liessen

    @property
    def anzahl(self):
        return len(self.ok) + len(self.fehler)


class Lauf:
    """Sammelt die Tests aus den Testdateien und fuehrt sie einzeln aus."""

    def __init__(self, muster="", misch=None, leise=False):
        self.muster = (muster or "").lower()
        self.misch = misch
        self.leise = leise

    def sammeln(self):
        """(name, funktion) aus allen Testdateien - Duplikate fliegen auf.

        Ein doppelter Testname waere still: die zweite Datei ueberschriebe die
        erste im Ergebnis, und ein Test liefe nie. Beim Aufteilen der Suite ist
        das der wahrscheinlichste Fehler, deshalb wird er hier laut."""
        gefunden = {}
        doppelt = []
        for modulname in TESTDATEIEN:
            if not os.path.exists(f"{modulname}.py") and not os.path.isdir(modulname):
                # Frueher stand hier ein stilles 'continue'. Das ist derselbe
                # Fehler wie der verschluckte ImportError zwei Zeilen weiter
                # unten, nur leiser: ein Tippfehler beim Aufteilen der Suite
                # haette eine ganze Datei verschwinden lassen, und der Lauf
                # haette trotzdem 'alles gruen' gemeldet. Ein Werkzeug, das
                # Vollstaendigkeit behauptet, darf nie still weniger finden.
                print(f"FEHLER: {modulname}.py steht in TESTDATEIEN, "
                      f"existiert aber nicht.")
                print("  Entweder ist die Datei weg oder der Name ist vertippt. "
                      "Beides heisst:\n  hier fehlen Tests, und der Lauf waere "
                      "gruen, ohne sie gefahren zu haben.")
                raise SystemExit(2)
            try:
                modul = importlib.import_module(modulname)
            except Exception as exc:  # noqa: BLE001
                # NICHT verschlucken. Eine Testdatei, die sich nicht importieren
                # laesst, liefert sonst still null Tests - der Lauf meldet
                # "6 Tests bestanden" und sieht gruen aus, obwohl 324 fehlen.
                print(f"FEHLER: {modulname} laesst sich nicht importieren:")
                print(f"  {type(exc).__name__}: {exc}")
                raise SystemExit(2)
            for name in dir(modul):
                if not name.startswith("test_"):
                    continue
                fn = getattr(modul, name)
                if not callable(fn):
                    continue
                if name in gefunden:
                    doppelt.append(f"{name} (in {gefunden[name][0]} und {modulname})")
                    continue
                gefunden[name] = (modulname, fn)
        if doppelt:
            print("FEHLER: doppelte Testnamen - einer davon laeuft nie:")
            for d in sorted(doppelt):
                print(f"  {d}")
            raise SystemExit(2)
        paare = [(name, fn) for name, (_datei, fn) in gefunden.items()]
        if self.muster:
            paare = [p for p in paare if self.muster in p[0].lower()]
        paare.sort(key=lambda p: p[0])
        if self.misch is not None:
            random.Random(self.misch).shuffle(paare)
        return paare

    @staticmethod
    def _zustand():
        """Was GERADE global gesetzt ist - Schalter und die tiefen Stellen."""
        stand = {}
        for modulname in WACHE:
            modul = sys.modules.get(modulname)
            inst = getattr(modul, "instance", None) if modul else None
            if inst is not None and hasattr(inst, "_enabled"):
                stand[f"{modulname}._enabled"] = (modulname, "instance._enabled",
                                                  inst._enabled)
        for modulname, pfad in WACHE_TIEF:
            wert, da = Lauf._hole(modulname, pfad)
            if da:
                stand[f"{modulname}.{pfad}"] = (modulname, pfad, wert)
        return stand

    @staticmethod
    def _hole(modulname, pfad):
        """Wert hinter einem Attributpfad -> (wert, gefunden)."""
        ding = sys.modules.get(modulname)
        if ding is None:
            return None, False
        for stueck in pfad.split("."):
            ding = getattr(ding, stueck, _FEHLT)
            if ding is _FEHLT:
                return None, False
        return ding, True

    @staticmethod
    def _zustand_zurueck(vorher):
        """Stellt alles wieder her. Gibt zurueck, WAS verstellt war.

        Verglichen wird mit 'is' und nicht mit '==': ein _store ist ein Objekt,
        und zwei verschiedene Stores koennen gleich aussehen und trotzdem auf
        verschiedene Dateien zeigen. Bei den Schaltern (True/False) ist 'is'
        genauso richtig.
        """
        verstellt = []
        for schluessel, (modulname, pfad, wert) in vorher.items():
            jetzt, da = Lauf._hole(modulname, pfad)
            if not da or jetzt is wert:
                continue
            verstellt.append(schluessel)
            ziel = sys.modules.get(modulname)
            stuecke = pfad.split(".")
            for stueck in stuecke[:-1]:
                ziel = getattr(ziel, stueck, None)
                if ziel is None:
                    break
            if ziel is not None:
                setattr(ziel, stuecke[-1], wert)
        return verstellt

    def einer(self, name, fn, erg):
        """Einen Test fahren. Faengt ALLES - auch KeyboardInterrupt nicht, das
        soll weiterhin abbrechen koennen."""
        # Vor jedem Test denselben Zufall: sonst haengt ein Test, der random
        # benutzt, am Ergebnis des vorherigen - und mit --misch waere die Suite
        # nicht wiederholbar.
        random.seed(0)
        vorher = self._zustand()
        start = time.monotonic()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - genau das sammeln wir
            erg.fehler.append((name, f"{type(exc).__name__}: {exc}",
                               traceback.format_exc()))
            if not self.leise:
                print(f"FEHLER  {name}")
            return
        except SystemExit as exc:  # ein Test, der sys.exit ruft, ist ein Fehler
            erg.fehler.append((name, f"SystemExit: {exc}", traceback.format_exc()))
            print(f"FEHLER  {name}  (hat sys.exit gerufen)")
            return
        finally:
            erg.dauer[name] = time.monotonic() - start
            verstellt = self._zustand_zurueck(vorher)
            if verstellt:
                erg.schlampig.append((name, ", ".join(verstellt)))
        erg.ok.append(name)
        if not self.leise:
            print(f"ok  {name}")

    def start(self):
        erg = Ergebnis()
        paare = self.sammeln()
        if self.misch is not None:
            print(f"Reihenfolge gemischt (Seed {self.misch}) - "
                  f"wiederholbar mit  python lauf.py --misch {self.misch}\n")
        for name, fn in paare:
            self.einer(name, fn, erg)
        return erg

    @staticmethod
    def bericht(erg):
        print()
        if erg.schlampig:
            # Kein Fehler, aber eine Warnung: diese Tests haengen von der
            # Reihenfolge ab. Der Runner hat es geradegezogen - beim Aufteilen
            # der Suite sollte man sie trotzdem kennen.
            print(f"Hinweis: {len(erg.schlampig)} Test(s) liessen globalen "
                  f"Zustand stehen (wurde zurueckgesetzt):")
            for name, was in erg.schlampig:
                print(f"  {name}  ->  {was}")
            print()
        if not erg.fehler:
            print(f"{erg.anzahl} Tests bestanden.")
            langsam = sorted(erg.dauer.items(), key=lambda p: -p[1])[:3]
            if langsam and langsam[0][1] > 1.0:
                print("Langsamste: " + ", ".join(
                    f"{n} ({s:.1f}s)" for n, s in langsam if s > 1.0))
            return 0
        print(f"{len(erg.fehler)} von {erg.anzahl} Tests GESCHEITERT:\n")
        for name, kurz, spur in erg.fehler:
            print(f"--- {name} ---")
            print(kurz)
            # Nur die letzten Zeilen der Spur - der Rest ist Runner-Rahmen.
            zeilen = spur.rstrip().splitlines()
            for zeile in zeilen[-8:]:
                print(f"  {zeile}")
            print()
        print("Gescheitert: " + ", ".join(n for n, _k, _s in erg.fehler))
        return 1


def main(argv=None):
    p = argparse.ArgumentParser(description="Testlauf fuer Flo")
    p.add_argument("--nur", default="", metavar="MUSTER",
                   help="nur Tests, deren Name das Muster enthaelt")
    p.add_argument("--misch", nargs="?", const=-1, type=int, metavar="SEED",
                   help="zufaellige Reihenfolge (ohne Zahl: neuer Seed)")
    p.add_argument("--namen", action="store_true",
                   help="nur die Testnamen ausgeben (fuer den Vorher/Nachher-Abgleich)")
    p.add_argument("--leise", action="store_true", help="nur Fehler zeigen")
    a = p.parse_args(argv)

    misch = None
    if a.misch is not None:
        misch = random.randrange(1, 10**6) if a.misch == -1 else a.misch

    lauf = Lauf(muster=a.nur, misch=misch, leise=a.leise)
    if a.namen:
        for name, _fn in sorted(lauf.sammeln()):
            print(name)
        return 0
    return lauf.bericht(lauf.start())


if __name__ == "__main__":
    sys.exit(main())
