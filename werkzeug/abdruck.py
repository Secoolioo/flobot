#!/usr/bin/env python3
"""Abdruck: was Flo im Discord ANTWORTET - als vergleichbarer Fingerabdruck.

    python werkzeug/abdruck.py --schreibe      Abdruck aufnehmen
    python werkzeug/abdruck.py --vergleiche    gegen den Abdruck pruefen
    python werkzeug/abdruck.py --stabil        nur messen, was reproduzierbar ist

WOZU
====
Das Inventar (werkzeug/inventar.py) sagt, WELCHE Befehle es gibt. Fuer den Umbau
ist das die halbe Miete. Die Bedingung des Betreibers lautet aber:

    "alles frontend im discord so bleibt"

Also nicht nur: der Befehl existiert noch. Sondern: er antwortet noch DASSELBE.
Ein Modul kann nach dem Verschieben weiterhin auf 'flo level' reagieren und
trotzdem eine andere Ueberschrift, andere Felder oder keine Knoepfe mehr
schicken. Das Inventar waere gruen, der Testlauf auch, und im Discord saehe es
anders aus.

Der Abdruck nimmt deshalb die FORM der Antwort auf: Typ, Ueberschrift,
Feldnamen, Knopfbeschriftungen, das Textgeruest. Nicht den Inhalt - der haengt
an Kontostaenden, Wuerfelwuerfen und der Uhrzeit.

DAS SCHWIERIGE DARAN: REPRODUZIERBARKEIT
========================================
Flo wuerfelt, zieht Karten, sagt Zufallssprueche und schreibt die Uhrzeit
hinein. Ein naiver Abdruck waere bei jedem Lauf anders und damit wertlos -
schlimmer noch: er wuerde bei jedem Vergleich Aenderungen melden, man gewoehnt
sich an rote Meldungen, und die eine echte geht darin unter.

Dagegen drei Stufen:

  1. Zufall festnageln  random.seed() vor jedem Aufruf, gleicher Datenordner
  2. Wegkuerzen         Zahlen, IDs, Zeiten, Emoji-Zaehler werden zu Platzhaltern
  3. Nachmessen         --stabil laeuft ALLES dreimal und behaelt nur, was
                        dreimal gleich herauskam. Was wackelt, fliegt raus und
                        wird gezaehlt. Ein Abdruck, der nur die stabilen Teile
                        enthaelt, ist klein - aber jede Meldung daraus stimmt.

Rueckgabecodes:  0 gleich  ·  2 Antwort hat sich geaendert  ·  3 Werkzeug kaputt
"""

import argparse
import json
import os
import pathlib
import random
import re
import sys
import tempfile

WURZEL = pathlib.Path(__file__).resolve().parent.parent
# ---------------------------------------------------------------------------
# IMMER ein eigener, frischer Datenordner - und zwar bevor irgendein Bot-Modul
# importiert wird. store.DATA_DIR wird beim Import festgelegt, jeder JsonStore
# haengt daran.
#
# Bewusst ein hartes Setzen und kein setdefault: dieses Werkzeug FAEHRT DEN BOT
# HOCH und laesst ihn Befehle ausfuehren. Wuerde es einen von aussen geerbten
# DATA_DIR uebernehmen, haette ein Aufruf in einer Shell, in der DATA_DIR auf
# die echten Daten zeigt, Konten angelegt und Kurse verstellt.
#
# Der zweite Grund ist Messbarkeit: als Unterprozess aus dem Testlauf heraus
# erbte das Werkzeug den Ordner der Tests - mit allem, was vorherige Tests dort
# hinterlassen hatten. Je nach Testreihenfolge sah der Bot damit einen anderen
# Zustand und antwortete anders. Der Waechter fuer "im Discord bleibt alles
# gleich" wackelte also selbst; unter 'lauf.py --misch 4242' fiel er um.
# ---------------------------------------------------------------------------
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="flobot-abdruck-")
if str(WURZEL) not in sys.path:
    sys.path.insert(0, str(WURZEL))

ORDNER = WURZEL / "inventar"
ABDRUCK_DATEI = ORDNER / "abdruck.json"
STAND_DATEI = ORDNER / "stand.json"

#: Fester Wuerfel. Muss vor JEDEM Aufruf neu gesetzt werden, nicht einmal am
#: Anfang: sonst haengt das Ergebnis eines Befehls davon ab, wie viele Zufalls-
#: zahlen die Befehle davor verbraucht haben - und ein neuer Befehl in der Mitte
#: wuerde alle folgenden Abdruecke verschieben.
SAAT = 20260831

#: Alles, was sich von Lauf zu Lauf aendern darf, ohne dass sich die Oberflaeche
#: geaendert hat. Wird durch einen Platzhalter ersetzt, BEVOR verglichen wird.
WEGKUERZEN = (
    (re.compile(r"<@!?\d+>"), "@P"),                       # Erwaehnungen
    (re.compile(r"<#\d+>"), "#K"),                         # Kanaele
    (re.compile(r"<a?:\w+:\d+>"), ":E:"),                  # Server-Emoji
    (re.compile(r"https?://\S+"), "URL"),
    (re.compile(r"\b\d{1,2}[.:]\d{2}(?::\d{2})?\b"), "ZEIT"),
    (re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b"), "DATUM"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "DATUM"),
    (re.compile(r"\d[\d.,]*"), "N"),                       # zuletzt: alle Zahlen
)


def kuerzen(text):
    """Aus einem Antworttext das Geruest machen."""
    if not isinstance(text, str):
        return ""
    for muster, ersatz in WEGKUERZEN:
        text = muster.sub(ersatz, text)
    return re.sub(r"\s+", " ", text).strip()[:400]


class Form:
    """Die Gestalt einer Antwort - alles, was ein Nutzer im Discord SIEHT.

    Bewusst kein Abbild der Antwort, sondern ihr Geruest: Ueberschrift ja/nein,
    welche Felder, welche Knoepfe, wie das Textmuster aussieht. Genau das darf
    sich beim Umbau nicht aendern. Der Inhalt (Kontostand, Wuerfelwurf) darf.
    """

    @classmethod
    def von(cls, antwort, kanal):
        """-> vergleichbare Struktur, oder None wenn nichts geantwortet wurde."""
        import discord
        teile = []
        # Was der Handler zurueckgab ...
        if antwort is not None and not isinstance(antwort, bool):
            teile.append(cls._eins(antwort, discord))
        # ... und was er selbst in den Kanal geschickt hat (HANDLED-Faelle).
        for inhalt, embed, view in kanal.gesendet:
            teile.append(cls._eins(inhalt if embed is None else embed, discord,
                                   view=view))
        return teile or None

    @classmethod
    def _eins(cls, ding, discord, view=None):
        if isinstance(ding, discord.Embed):
            form = {"art": "embed", "titel": kuerzen(ding.title or ""),
                    "felder": [kuerzen(f.name or "") for f in ding.fields],
                    "fuss": bool(ding.footer and ding.footer.text),
                    "bild": bool(ding.image and ding.image.url),
                    "text": kuerzen(ding.description or "")}
        elif isinstance(ding, discord.File):
            form = {"art": "datei", "endung": (ding.filename or "").split(".")[-1]}
        elif isinstance(ding, str):
            form = {"art": "text", "text": kuerzen(ding)}
        elif ding is None:
            form = {"art": "nichts"}
        else:
            # HANDLED-Sentinel oder etwas Exotisches: nur die Sorte merken.
            form = {"art": type(ding).__name__}
        if view is not None:
            form["knoepfe"] = cls._knoepfe(view)
        return form

    @staticmethod
    def _knoepfe(view):
        """Beschriftungen der Bedienelemente - die sieht der Nutzer direkt."""
        raus = []
        for teil in getattr(view, "children", []):
            beschriftung = (getattr(teil, "label", None)
                            or getattr(teil, "placeholder", None) or "")
            raus.append(f"{type(teil).__name__}:{kuerzen(beschriftung)}")
        return sorted(raus)


class Abdrucknahme:
    """Nimmt fuer jeden Befehl aus dem Inventar die Form seiner Antwort auf."""

    def __init__(self, laut=True):
        self.laut = laut
        self.module = {}
        self.fehler = []

    def _sagen(self, text):
        if self.laut:
            print(text)

    def ruesten(self):
        """Module laden, hochfahren, einschalten - wie die Inventar-Probe."""
        from werkzeug import inventar
        inv = inventar.Inventar(laut=False)
        inv.module_laden()
        self.fehler.extend(inv.fehler)
        self.module = {n: m for n, m in inv.module.items()
                       if n not in inventar.KEIN_CHATMODUL and hasattr(m, "handle")}
        probe = inventar.Probe(self.module, [])
        probe.ruesten()
        self.fehler.extend(probe.fehler)
        return self.module

    @staticmethod
    def befehle():
        """Die Woerter aus dem Inventar - nur die, die ein Modul wirklich beantwortet."""
        if not STAND_DATEI.exists():
            raise SystemExit("Kein Inventar da. Erst: python werkzeug/inventar.py --schreibe")
        stand = json.loads(STAND_DATEI.read_text(encoding="utf-8"))
        return {w: d for w, d in stand["befehle"].items()
                if d.get("_orakel") != "statisch"}

    def durchlauf(self, loop, worte):
        from werkzeug.attrappe import RauchKanal, rauch_nachricht
        raus = {}
        for wort, wer in sorted(worte.items()):
            modul = self.module.get(wer["gewinner"])
            if modul is None:
                continue
            random.seed(SAAT)                 # vor JEDEM Aufruf, siehe oben
            kanal = RauchKanal()
            try:
                antwort = loop.run_until_complete(
                    modul.handle(rauch_nachricht(wort, kanal=kanal)))
            except Exception as exc:  # noqa: BLE001
                raus[wort] = [{"art": "absturz", "typ": type(exc).__name__}]
                continue
            form = Form.von(antwort, kanal)
            if form is not None:
                raus[wort] = form
        return raus

    def aufnehmen(self, runden=3):
        """Mehrfach durchlaufen und nur behalten, was JEDES MAL gleich war.

        Das ist der Kern des Ganzen. Flo wuerfelt, zieht Karten und sagt
        Zufallssprueche; ein einmal aufgenommener Abdruck wuerde beim naechsten
        Lauf an dutzenden Stellen abweichen. Man wuerde die Meldungen nach
        zwei Tagen ignorieren - und dann geht die eine echte darin unter.

        Also: dreimal laufen, und was nicht dreimal identisch herauskam, wird
        aussortiert UND GEZAEHLT. Lieber ein kleinerer Abdruck, dem man glaubt.
        """
        import asyncio
        from werkzeug import inventar
        worte = self.befehle()
        self.ruesten()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        laeufe = []
        try:
            with inventar.KeinNetz():
                for i in range(runden):
                    self._sagen(f"  Durchlauf {i + 1}/{runden} ...")
                    laeufe.append(self.durchlauf(loop, worte))
        finally:
            loop.close()
            asyncio.set_event_loop(None)

        stabil, wackelig = {}, []
        for wort in laeufe[0]:
            formen = [json.dumps(lauf.get(wort), sort_keys=True, ensure_ascii=False)
                      for lauf in laeufe]
            if len(set(formen)) == 1:
                stabil[wort] = laeufe[0][wort]
            else:
                wackelig.append(wort)
        return {
            "befehle": stabil,
            "_wackelig": sorted(wackelig),
            "_runden": runden,
            "_fehler": self.fehler,
            "zaehlung": {"stabil": len(stabil), "wackelig": len(wackelig),
                         "befragt": len(worte)},
        }

    @staticmethod
    def lesen():
        if not ABDRUCK_DATEI.exists():
            return None
        return json.loads(ABDRUCK_DATEI.read_text(encoding="utf-8"))

    @staticmethod
    def schreiben(abdruck):
        ORDNER.mkdir(exist_ok=True)
        ABDRUCK_DATEI.write_text(
            json.dumps(abdruck, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")


def vergleichen(alt, neu):
    """-> (verschwunden, veraendert) - beides Listen von Befehlswoertern."""
    a, n = alt["befehle"], neu["befehle"]
    verschwunden = sorted(set(a) - set(n))
    veraendert = []
    for wort in sorted(set(a) & set(n)):
        if (json.dumps(a[wort], sort_keys=True, ensure_ascii=False)
                != json.dumps(n[wort], sort_keys=True, ensure_ascii=False)):
            veraendert.append(wort)
    return verschwunden, veraendert


def _kurz(form):
    """Eine Form in einer Zeile, damit ein Unterschied lesbar bleibt."""
    stuecke = []
    for teil in form or []:
        art = teil.get("art")
        if art == "embed":
            stuecke.append(f"Embed[{teil.get('titel', '')[:40]}]"
                           f"{'+' + str(len(teil['felder'])) + 'F' if teil.get('felder') else ''}"
                           f"{'+Knoepfe' if teil.get('knoepfe') else ''}")
        elif art == "text":
            stuecke.append(f"Text[{teil.get('text', '')[:50]}]")
        else:
            stuecke.append(str(art))
    return " | ".join(stuecke) or "-"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--schreibe", action="store_true")
    p.add_argument("--vergleiche", action="store_true")
    p.add_argument("--stabil", action="store_true",
                   help="nur messen, wie viel ueberhaupt reproduzierbar ist")
    p.add_argument("--runden", type=int, default=3)
    p.add_argument("--leise", action="store_true")
    a = p.parse_args(argv)
    if not any((a.schreibe, a.vergleiche, a.stabil)):
        p.print_help()
        return 3

    nahme = Abdrucknahme(laut=not a.leise)
    neu = nahme.aufnehmen(runden=a.runden)
    z = neu["zaehlung"]
    print(f"\n  ABDRUCK  {z['stabil']} stabil von {z['befragt']} befragt "
          f"({z['wackelig']} wackelig, {a.runden} Runden)")
    if neu["_fehler"]:
        print(f"  {len(neu['_fehler'])} Warnung(en):")
        for zeile in neu["_fehler"][:10]:
            print(f"    - {zeile}")
    print()

    if a.stabil:
        if neu["_wackelig"]:
            print("Nicht reproduzierbar (Zufall, Uhrzeit, Zustand) - bleiben "
                  "draussen:\n  " + ", ".join(neu["_wackelig"]))
        return 0

    if z["stabil"] < 100:
        print("ABBRUCH - viel zu wenig stabile Antworten. Das heisst fast immer, "
              "dass die\nUmgebung kaputt ist, nicht der Bot. Ein Abdruck aus so "
              "einem Lauf wuerde jeden\nspaeteren Vergleich trivial gruen machen.")
        return 3

    if a.schreibe:
        Abdrucknahme.schreiben(neu)
        print(f"Abdruck geschrieben: {ABDRUCK_DATEI.relative_to(WURZEL)}")
        return 0

    alt = Abdrucknahme.lesen()
    if alt is None:
        print("Kein Abdruck da. Erst: python werkzeug/abdruck.py --schreibe")
        return 3
    verschwunden, veraendert = vergleichen(alt, neu)
    if not verschwunden and not veraendert:
        print("Flo antwortet ueberall noch genauso.")
        return 0
    if verschwunden:
        print(f"KEINE ANTWORT MEHR ({len(verschwunden)}):")
        for w in verschwunden:
            print(f"  {w:22} war: {_kurz(alt['befehle'][w])}")
    if veraendert:
        print(f"\nANTWORT HAT SICH GEAENDERT ({len(veraendert)}):")
        for w in veraendert:
            print(f"  {w}")
            print(f"      vorher: {_kurz(alt['befehle'][w])}")
            print(f"      jetzt : {_kurz(neu['befehle'][w])}")
    print("\nWenn das Absicht war, den Abdruck neu schreiben. Wenn nicht: das ist "
          "eine\nsichtbare Aenderung im Discord.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
