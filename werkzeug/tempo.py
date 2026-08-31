#!/usr/bin/env python3
"""Tempo: wie lange Flo fuer eine Nachricht braucht - gemessen, nicht geschaetzt.

    python werkzeug/tempo.py --schreibe      Grundmessung aufnehmen
    python werkzeug/tempo.py --vergleiche    gegen die Grundmessung halten
    python werkzeug/tempo.py                 einmal messen und anzeigen

WOZU
====
"Die Performance soll besser werden" ist ohne Messung eine Meinung. Nach einem
Umbau glaubt man leicht, es sei schneller geworden - der Code sieht ja
aufgeraeumter aus. Genauso leicht wird es unbemerkt langsamer, weil eine
Abkuerzung beim Verschieben verlorenging.

Dieses Werkzeug misst die Wege, die WIRKLICH oft laufen. Der wichtigste ist
nicht der Befehl, sondern die normale Chatnachricht: die geht durch die
Tippfehler-Korrektur und danach durch ALLE Module der Handler-Kette, von denen
keines zustaendig ist. Das passiert bei jedem "hahaha" auf dem Server.

WAS GEMESSEN WIRD
=================
  durchfall     eine normale Chatnachricht durch die ganze Kette (der haeufigste
                Fall ueberhaupt) - hier zaehlt jede Mikrosekunde
  befehl        ein erkannter Befehl bis zur fertigen Antwort
  cmdnorm       die Tippfehler-Korrektur allein
  ansprache     ai.strip_lead allein - laeuft in JEDEM Modul noch einmal
  je_modul      welches Modul im Durchfall am meisten kostet

EHRLICH BLEIBEN
===============
Zeitmessungen schwanken. Damit aus Rauschen keine Erfolgsmeldung wird:
  - jede Messung laeuft in Runden, gewertet wird der MEDIAN, nicht der Schnitt
    (ein einzelner Ausreisser durch die Garbage Collection verzerrt sonst alles)
  - beim Vergleich gilt erst ein Unterschied ueber 10 Prozent als Aenderung
  - die Streuung wird mit ausgegeben: liegt sie hoch, ist die Messung selbst
    wackelig und man soll ihr nicht glauben

Rueckgabecodes: 0 gleich oder schneller · 2 spuerbar langsamer · 3 kaputt
"""

import argparse
import json
import os
import pathlib
import statistics
import sys
import tempfile
import time

WURZEL = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="flobot-tempo-"))
if str(WURZEL) not in sys.path:
    sys.path.insert(0, str(WURZEL))

ORDNER = WURZEL / "inventar"
TEMPO_DATEI = ORDNER / "tempo.json"

#: Ab hier gilt ein Unterschied als echt und nicht als Rauschen.
SCHWELLE = 0.10

#: Saetze, wie sie wirklich im Chat stehen - keine Befehle. Genau die laufen
#: durch die komplette Kette und werden von niemandem beantwortet.
GEPLAUDER = (
    "hahaha ja klar",
    "also ich fand den film echt gut",
    "wer ist heute abend dabei",
    "das war knapp lol",
    "bin gleich zurueck, muss kurz weg",
    "kp was du meinst",
    "gn8 leute",
)


class Uhr:
    """Misst eine Sache mehrfach und gibt den Median in Mikrosekunden.

    Median statt Mittelwert, weil ein einzelner Ausreisser (Garbage Collection,
    der Scheduler nimmt sich den Kern) den Mittelwert um Faktoren verzieht. Der
    Median haelt das aus. Die Streuung kommt mit heraus, damit man sieht, ob der
    Messung ueberhaupt zu trauen ist.
    """

    def __init__(self, runden=40, aufwaermen=5):
        self.runden = runden
        self.aufwaermen = aufwaermen

    def messen(self, fn):
        for _ in range(self.aufwaermen):
            fn()                       # Caches fuellen, JIT-Aehnliches aufwaermen
        zeiten = []
        for _ in range(self.runden):
            start = time.perf_counter()
            fn()
            zeiten.append((time.perf_counter() - start) * 1_000_000)
        median = statistics.median(zeiten)
        streuung = (statistics.pstdev(zeiten) / median) if median else 0.0
        return {"us": round(median, 1), "streuung": round(streuung, 2)}


class Messung:
    """Faehrt den Bot hoch und misst die heissen Wege."""

    def __init__(self, laut=True, runden=40):
        self.laut = laut
        self.uhr = Uhr(runden=runden)
        self.module = {}
        self.kette = []
        self.loop = None

    def _sagen(self, text):
        if self.laut:
            print(text)

    def ruesten(self):
        import asyncio
        from werkzeug import inventar
        inv = inventar.Inventar(laut=False)
        inv.module_laden()
        self.module = {n: m for n, m in inv.module.items()
                       if n not in inventar.KEIN_CHATMODUL and hasattr(m, "handle")}
        inventar.Probe(self.module, []).ruesten()
        # Die Kette in der Reihenfolge, in der bot.py sie wirklich fragt.
        quelle = inventar.Quelltext.hol(WURZEL / "bot.py")
        try:
            ordnung = inventar.Reihenfolge(quelle).module()
        except LookupError:
            ordnung = sorted(self.module)
        self.kette = [(n, self.module[n]) for n in ordnung if n in self.module]
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def _durch_die_kette(self, text):
        from werkzeug.attrappe import rauch_nachricht

        async def lauf():
            for _name, modul in self.kette:
                if await modul.handle(rauch_nachricht(text)) is not None:
                    return
        self.loop.run_until_complete(lauf())

    def alles(self):
        import ai
        import cmdnorm
        from werkzeug.attrappe import rauch_nachricht

        raus = {}
        self._sagen("  durchfall (normale Chatnachricht durch die ganze Kette) ...")
        raus["durchfall"] = self.uhr.messen(
            lambda: [self._durch_die_kette(t) for t in GEPLAUDER])

        self._sagen("  befehl ...")
        raus["befehl"] = self.uhr.messen(lambda: self._durch_die_kette("coins"))

        self._sagen("  cmdnorm ...")
        raus["cmdnorm"] = self.uhr.messen(
            lambda: [cmdnorm.normalize(t) for t in GEPLAUDER])

        self._sagen("  ansprache (ai.strip_lead) ...")
        raus["ansprache"] = self.uhr.messen(
            lambda: [ai.strip_lead(t) for t in GEPLAUDER])

        self._sagen("  je Modul ...")
        je_modul = {}
        for name, modul in self.kette:
            def eins(m=modul):
                async def lauf():
                    for t in GEPLAUDER:
                        await m.handle(rauch_nachricht(t))
                self.loop.run_until_complete(lauf())
            je_modul[name] = self.uhr.messen(eins)
        raus["_je_modul"] = je_modul
        return raus

    def aufnehmen(self):
        self.ruesten()
        try:
            werte = self.alles()
        finally:
            self.loop.close()
        return {
            "werte": {k: v for k, v in werte.items() if not k.startswith("_")},
            "_je_modul": werte["_je_modul"],
            "_kette": [n for n, _ in self.kette],
            "umgebung": {"python": ".".join(str(x) for x in sys.version_info[:3])},
        }

    @staticmethod
    def lesen():
        if not TEMPO_DATEI.exists():
            return None
        return json.loads(TEMPO_DATEI.read_text(encoding="utf-8"))

    @staticmethod
    def schreiben(daten):
        ORDNER.mkdir(exist_ok=True)
        TEMPO_DATEI.write_text(
            json.dumps(daten, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")


def zeigen(daten, alt=None):
    """Die Messung ausgeben - mit Vergleich, wenn es einen gibt."""
    print()
    for name, wert in sorted(daten["werte"].items()):
        zeile = f"  {name:12} {wert['us']:>9.1f} us"
        if wert["streuung"] > 0.25:
            zeile += f"   (streut {wert['streuung']:.0%} - der Messung nicht trauen)"
        if alt and name in alt["werte"]:
            frueher = alt["werte"][name]["us"]
            if frueher:
                d = (wert["us"] - frueher) / frueher
                pfeil = "schneller" if d < 0 else "langsamer"
                zeile += f"   {abs(d):>6.0%} {pfeil} (war {frueher:.1f})"
        print(zeile)
    teuer = sorted(daten["_je_modul"].items(), key=lambda x: -x[1]["us"])[:8]
    print("\n  Teuerste Module im Durchfall (7 Chatnachrichten je Modul):")
    for name, wert in teuer:
        print(f"    {name:14} {wert['us']:>8.1f} us")
    print()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--schreibe", action="store_true")
    p.add_argument("--vergleiche", action="store_true")
    p.add_argument("--runden", type=int, default=40)
    p.add_argument("--leise", action="store_true")
    a = p.parse_args(argv)

    neu = Messung(laut=not a.leise, runden=a.runden).aufnehmen()
    alt = Messung.lesen() if a.vergleiche else None
    zeigen(neu, alt)

    if a.schreibe:
        Messung.schreiben(neu)
        print(f"Grundmessung geschrieben: {TEMPO_DATEI.relative_to(WURZEL)}")
        return 0
    if not a.vergleiche:
        return 0
    if alt is None:
        print("Keine Grundmessung da. Erst: python werkzeug/tempo.py --schreibe")
        return 3

    langsamer = []
    for name, wert in neu["werte"].items():
        frueher = alt["werte"].get(name, {}).get("us")
        if not frueher:
            continue
        if (wert["us"] - frueher) / frueher > SCHWELLE:
            langsamer.append(f"{name}: {frueher:.1f} -> {wert['us']:.1f} us "
                             f"(+{(wert['us'] - frueher) / frueher:.0%})")
    if langsamer:
        print("LANGSAMER GEWORDEN:\n  " + "\n  ".join(langsamer))
        print(f"\n(Erst ueber {SCHWELLE:.0%} gilt als echt - das hier ist mehr.)")
        return 2
    print("Nicht langsamer geworden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
