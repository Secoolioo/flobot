#!/usr/bin/env python3
"""Inventar: der Funktionsumfang von Flo, AUS DEM CODE erzeugt.

    python werkzeug/inventar.py --schreibe      Grundstand neu aufnehmen
    python werkzeug/inventar.py --vergleiche    gegen den Grundstand pruefen
    python werkzeug/inventar.py --zeige         Stand lesbar ausgeben
    python werkzeug/inventar.py --cmdnorm       cmdnorm-Fehlgriffe messen

WOZU
====
Der Umbau von Flo (Panel neu, eine Konfiguration, Dateien aufteilen) laeuft
unter einer harten Bedingung: keine Funktion darf verlorengehen. Ein gruener
Testlauf beweist das NICHT - er beweist nur, dass die getesteten Wege noch
gehen. Ein Befehl, der beim Verschieben seine Registrierung verliert, faellt
still an die KI durch. Die antwortet irgendwas. Es sieht aus, als ginge es.

Dieses Werkzeug nimmt deshalb auf, was es heute gibt, und sagt nach jedem
Umbauschritt, was fehlt.

WARUM ERZEUGT UND NICHT ABGESCHRIEBEN
=====================================
Eine von Hand gepflegte Liste ist nach dem dritten Umbauschritt veraltet und
wiegt danach in falscher Sicherheit. Also zwei Quellen, die sich gegenseitig
kontrollieren:

  statisch   was im Quelltext DEKLARIERT steht (AST) - liefert die Kandidaten
  dynamisch  was der laufende Code auf ein Reizwort ANTWORTET - liefert die
             Wahrheit

Der statische Fund ist die Liste der Fragen. Die Probe gibt die Antworten.

DAS LEITPRINZIP DES VERGLEICHS
==============================
    Was verlorengehen kann, ist ein Schluessel.
    Was sich aendern darf, ist ein Wert.

Ein Befehlswort, eine Route, ein Katalogeintrag, ein Loop: Schluessel - ihr
Verschwinden ist ein Fehler. Ein Hilfetext, eine Zahl, eine Reihenfolge: Wert -
darf sich aendern. Alles mit fuehrendem '_' ist Interna und faellt aus dem
Vergleich.

Und weil beim Umbau Sachen UMZIEHEN sollen, sind die Schluessel bewusst
besitzerlos: 'wordle' ist ein Befehl, egal welches Modul ihn beantwortet. Wer
ihn beantwortet, steht als Wert daneben - ein Umzug ist eine Notiz, kein
Fehler. Nur das Verschwinden ist einer.

Rueckgabecodes:  0 alles da  ·  1 nur angekuendigte Verluste  ·
                 2 echter Verlust  ·  3 das Werkzeug selbst ist kaputt
"""

import argparse
import ast
import asyncio
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import traceback

# ---------------------------------------------------------------------------
# ZUERST den Datenordner umbiegen - VOR jedem Bot-Import. store.DATA_DIR wird
# beim Import festgelegt, und jeder JsonStore haengt daran. Ohne das schreibt
# die Probe in die ECHTEN Daten des laufenden Servers.
# ---------------------------------------------------------------------------
WURZEL = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="flobot-inventar-"))
if str(WURZEL) not in sys.path:
    sys.path.insert(0, str(WURZEL))

ORDNER = WURZEL / "inventar"
STAND_DATEI = ORDNER / "stand.json"
ERWARTET_DATEI = ORDNER / "erwartet.json"


# --- Ausnahmen, offen ausgewiesen -------------------------------------------

#: Module, deren handle() zu heikel ist, um es blind mit hunderten Woertern zu
#: fuettern. Sie bekommen KEIN Orakel und werden im Stand ehrlich als Luecke
#: markiert - lieber eine sichtbare Luecke als eine getarnte.
NICHT_PROBIEREN = {
    "admin": "Owner-Befehle: Neustart, Update, Sendepause - Nebenwirkungen",
    "giveaway": "legt Verlosungen an und startet Timer",
    "voicegags": "will in den Voice-Channel und ruft TTS",
}

#: Dateien, die ueberhaupt keine Chat-Befehle haben. Ohne diese Liste landeten
#: im ersten Lauf 59 Woerter aus bot.py und webpanel.py in der Befehlsliste -
#: aus Log-Texten und HTTP-Feldern. Ein Inventar, das Sachen erfindet, ist
#: genauso wertlos wie eines, das Sachen verliert: beim naechsten Umbau steht
#: dann 'verloren: content-type', und man gewoehnt sich an rote Meldungen.
KEIN_CHATMODUL = ("bot", "webpanel", "store", "basis", "render", "numfmt",
                  "leaderboard_img", "schedule_logic", "soundpack",
                  "economy_reset")

#: Module ohne gefahrlosen Handler, aber mit einem reinen Muster-Pruefer.
#: Der wird statt handle() gefragt - gleiche Aussage, keine Nebenwirkung.
MUSTER_ORAKEL = ("music", "moderation")

#: Konstanten, die nach Katalog AUSSEHEN, aber keiner sind (Wortlisten fuer
#: Zufallstexte, Rangordnungen, Emoji-Tabellen). Sie wuerden den Vergleich mit
#: tausenden bedeutungslosen Schluesseln zumuellen.
KEIN_KATALOG = {
    "ai": "*", "fun": "*", "media": "*", "titles": ("_ADJ", "_NOUN", "_PLACE",
                                                    "_EMOJI", "_GEN"),
    "words": "*", "gehirn": "*", "terraria": "*",
}

#: Untergrenzen je Kategorie. Sie sind das Gegenmittel gegen die gefaehrlichste
#: Art, wie dieses Werkzeug versagen kann: es laeuft in einer kaputten Umgebung
#: (Pakete weg, Import kaputt), findet ZU WENIG, schreibt diese magere Liste als
#: Grundstand - und danach ist jeder Vergleich trivial gruen. Das Sicherheits-
#: netz haette dann ein Loch in genau der Groesse des Problems.
#: Die Zahlen liegen bewusst unter dem gemessenen Stand (Luft fuer Umbauten),
#: aber weit ueber Null.
#: Gemessen am 31.08.2026: 536 Befehle, 39 Routen, 249 Katalogeintraege,
#: 16 Loops, 426 Modul-Funktionen, 35 Frontend-Aufrufe. Die Grenzen liegen bei
#: rund drei Vierteln davon - genug Luft, um beim Aufraeumen auch mal etwas
#: bewusst zusammenzulegen, aber weit weg von einem leeren Lauf.
MINDESTENS = {
    "befehle": 400,
    "routen": 30,
    "kataloge": 180,
    "loops": 12,
    "modulapi": 320,
    "frontend": 25,
}

#: Wortform eines moeglichen Befehls. Bewusst grosszuegig: Umlaute, Ziffern,
#: Bindestrich, Punkt. Was hier durchkommt, entscheidet spaeter die Probe.
WORT_RE = re.compile(r"^[a-zA-ZÀ-ɏ][\wÀ-ɏ.\-]{1,23}$")

#: Variablennamen, die in einem Handler das ERSTE WORT der Nachricht halten.
#: Nur fuer die Module ohne Orakel gebraucht: dort muss der Quelltext allein
#: entscheiden, was ein Befehl ist, und 'if first in (...)' ist das
#: verlaessliche Zeichen dafuer. Nachgesehen in admin, giveaway und voicegags.
BEFEHLSSTELLE = {"first", "erstes", "erstes_wort", "cmd", "befehl", "wort",
                 "low", "token", "kopf", "kommando", "aktion"}

#: Woerter, die im Code als Vergleichswerte vorkommen, aber sicher keine
#: Befehle sind: Umgebungsschalter und Datentyp-Namen.
KEINE_BEFEHLE = {
    "0", "1", "true", "false", "no", "off", "on", "yes", "none", "null",
    "str", "int", "float", "bool", "dict", "list", "tuple", "text", "json",
}


# --- Quellen ----------------------------------------------------------------

class Quelltext:
    """Eine Python-Datei, einmal als Baum gelesen.

    Jede Frage an den Quelltext geht hier durch, damit keine Datei zweimal
    geparst wird und alle Sucher dieselbe Sicht haben.
    """

    _zwischenlager = {}

    def __init__(self, pfad):
        self.pfad = pathlib.Path(pfad)
        self.name = self.pfad.stem
        self.text = self.pfad.read_text(encoding="utf-8")
        self.baum = ast.parse(self.text, filename=str(self.pfad))

    @classmethod
    def hol(cls, pfad):
        pfad = str(pathlib.Path(pfad))
        if pfad not in cls._zwischenlager:
            cls._zwischenlager[pfad] = cls(pfad)
        return cls._zwischenlager[pfad]

    def knoten(self, art):
        """Alle Knoten einer Art (oder mehrerer), egal wie tief."""
        return [k for k in ast.walk(self.baum) if isinstance(k, art)]

    @staticmethod
    def literal(knoten):
        """Wert eines Knotens, falls er ein reines Literal ist - sonst None."""
        try:
            return ast.literal_eval(knoten)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return None


class Woerterjagd:
    """Sammelt aus einer Moduldatei alles, was ein Befehlswort SEIN KOENNTE.

    Die Module deklarieren ihre Woerter auf vier Arten, und alle vier kommen
    im Repo vor:

      1. Modul-Konstante        _CMDS = ("work", "arbeit", ...)
      2. Klassen-Konstante      class X:  _CMDS = (...)
      3. Inline im Vergleich    if first in ("quiz", "trivia"):
      4. Regex-Muster           _CMD_RE = re.compile(r"^(kalorien|kcal)\\b")

    Der Fund ist ausdruecklich nur ein KANDIDAT. Hier wird grosszuegig
    gesammelt - auch "J", "Q", "K" aus einer Blackjack-Kartenpruefung. Aussieben
    tut die Probe: was kein Modul beantwortet, fliegt raus. Andersherum waere es
    gefaehrlich - ein zu enger Filter verliert echte Befehle, und genau das soll
    dieses Werkzeug ja verhindern.
    """

    def __init__(self, quelle):
        self.quelle = quelle

    def alles(self):
        w = set()
        w |= self._aus_zuweisungen()
        w |= self._aus_vergleichen()
        w |= self._aus_mustern()
        return {x for x in w if self._brauchbar(x)}

    @staticmethod
    def _brauchbar(wort):
        return (bool(WORT_RE.match(wort)) and wort.lower() not in KEINE_BEFEHLE)

    def _aus_zuweisungen(self):
        """Fall 1 und 2: jede Zuweisung einer Wortliste, egal wie sie heisst.

        Der erste Versuch filterte nach Namen (_CMDS, BEFEHLE, *_ALIAS ...) und
        verlor damit sofort echte Befehle: terraria haelt seine Woerter in
        _PREFIXE, das Modul war im ersten Lauf komplett unsichtbar. Der Filter
        war eine verfruehte Sparmassnahme - Kandidaten kosten fast nichts, ein
        verlorener Befehl kostet genau das, was dieses Werkzeug verhindern soll.

        Also: jede Zuweisung, deren Wert eine Sammlung von Zeichenketten ist.
        Aussieben tut die Probe.
        """
        treffer = set()
        for zuw in self.quelle.knoten((ast.Assign, ast.AnnAssign)):
            treffer |= self._flach(Quelltext.literal(zuw.value))
        return treffer

    def _aus_vergleichen(self):
        """Fall 3: 'if first in (...)' - die haeufigste Form im Repo.

        Auch 'not in' wird genommen. Ohne das fehlt zum Beispiel in moderation
        die halbe Liste, weil dort Ausschluesse geprueft werden - und in
        terraria die Prefix-Pruefung 'if erstes not in self._PREFIXE'.
        """
        treffer = set()
        for vgl in self.quelle.knoten(ast.Compare):
            if not any(isinstance(op, (ast.In, ast.NotIn)) for op in vgl.ops):
                continue
            for rechts in vgl.comparators:
                treffer |= self._flach(Quelltext.literal(rechts))
        return treffer

    def _aus_mustern(self):
        """Fall 4: Alternativen aus einem Regex ernten.

        Nur die Wortstuecke, keine Regex-Sonderzeichen. Wieder: Kandidaten.
        """
        treffer = set()
        for ruf in self.quelle.knoten(ast.Call):
            ziel = getattr(ruf.func, "attr", None) or getattr(ruf.func, "id", None)
            if ziel != "compile" or not ruf.args:
                continue
            muster = Quelltext.literal(ruf.args[0])
            if not isinstance(muster, str):
                continue
            for stueck in re.findall(r"[a-zA-ZÀ-ɏ][\wÀ-ɏ]{1,23}", muster):
                treffer.add(stueck)
        # Regex-Kuerzel wie \b, (?:...) hinterlassen Muell - der faellt in der
        # Probe durch, aber die haeufigsten kann man hier schon wegwerfen.
        return treffer - {"re", "I", "S", "M", "X", "U", "A"}

    def nur_befehlsstellen(self):
        """Die ENGE Ernte - fuer Module, die kein Orakel haben.

        Bei allen anderen Modulen darf grosszuegig gesammelt werden, weil die
        Probe hinterher aussiebt. Bei admin, giveaway und voicegags gibt es
        niemanden, der aussiebt - dort wuerde die grosse Ernte jede Zahlwort-
        Tabelle zum Befehl erklaeren. Gemessen: giveaway kam so auf 216
        angebliche Befehle, darunter 'acht', 'drei' und 'egal'.

        Also hier nur, was eindeutig an der Befehlsstelle geprueft wird:
        'if first in (...)'. Lieber ein paar Woerter weniger als eine Liste,
        der man nicht mehr glaubt.
        """
        treffer = set()
        for vgl in self.quelle.knoten(ast.Compare):
            if not any(isinstance(op, (ast.In, ast.NotIn)) for op in vgl.ops):
                continue
            name = getattr(vgl.left, "id", None) or getattr(vgl.left, "attr", None)
            if name not in BEFEHLSSTELLE:
                continue
            for rechts in vgl.comparators:
                treffer |= self._flach(Quelltext.literal(rechts))
        return {x for x in treffer if self._brauchbar(x)}

    @staticmethod
    def _flach(wert):
        """Strings aus einer beliebig verschachtelten Literal-Struktur."""
        raus = set()
        stapel = [wert]
        while stapel:
            x = stapel.pop()
            if isinstance(x, str):
                raus.add(x)
            elif isinstance(x, (list, tuple, set, frozenset)):
                stapel.extend(x)
            elif isinstance(x, dict):
                stapel.extend(x.keys())
                stapel.extend(x.values())
        return raus


class Reihenfolge:
    """Die Reihenfolge, in der bot.py die Module fragt.

    Sie steht in on_message als eine Schleife ueber ein Tupel aus
    (schalter, modul.handle). Wer frueher drankommt, gewinnt eine Kollision -
    deshalb wird die Reihenfolge hier aus dem Quelltext gelesen und nicht
    geraten. Faellt die Schleife beim Umbau weg, sagt das jemand laut, statt
    dass die Kollisionsauswertung still falsch wird.
    """

    def __init__(self, quelle):
        self.quelle = quelle

    def module(self):
        for schleife in self.quelle.knoten(ast.For):
            ziel = schleife.target
            if not (isinstance(ziel, ast.Tuple) and len(ziel.elts) == 2):
                continue
            namen = [getattr(z, "id", "") for z in ziel.elts]
            if namen != ["enabled", "handler"]:
                continue
            raus = []
            for paar in getattr(schleife.iter, "elts", []):
                for knoten in ast.walk(paar):
                    if (isinstance(knoten, ast.Attribute)
                            and knoten.attr.startswith("handle")
                            and isinstance(knoten.value, ast.Name)):
                        if knoten.value.id not in raus:
                            raus.append(knoten.value.id)
            if raus:
                return raus
        raise LookupError(
            "Die Handler-Schleife in bot.py (for enabled, handler in ...) ist "
            "nicht mehr auffindbar. Ohne sie weiss das Inventar nicht mehr, wer "
            "eine Kollision gewinnt - bitte Reihenfolge.module() nachziehen.")


class Probe:
    """Fragt die laufenden Module: reagierst du auf dieses Wort?

    Das ist die Wahrheit im Inventar. Der Quelltext liefert nur die Fragen.

    Gefragt werden ALLE Module, nicht nur das, von dem das Wort stammt. Das
    kostet ein paar Sekunden und findet dafuer die Kollisionen - zwei Module,
    die auf dasselbe Wort hoeren. Beim Aufteilen von Dateien ist das die
    gefaehrlichste Sorte Fehler, weil sich der Gewinner still aendern kann.

    Sicherheiten, in dieser Reihenfolge:
      - DATA_DIR liegt im Temp-Ordner (ganz oben in dieser Datei)
      - kein Netz waehrend der Probe
      - ai._client = None, damit nichts zur KI durchfaellt
      - ein einziger Event-Loop fuer alle Aufrufe
    """

    def __init__(self, module, reihenfolge):
        self.module = module              # name -> Modulobjekt
        self.reihenfolge = reihenfolge    # Liste von Namen, bot.py-Ordnung
        self.fehler = []
        self._loop = None

    # -- Vorbereitung --------------------------------------------------------
    def ruesten(self):
        """Alle Module hochfahren und einschalten.

        handle() prueft als Allererstes den eigenen An/Aus-Schalter. Ohne
        Einschalten antwortet kein einziges Modul, das Inventar waere leer -
        und der Vergleich danach trivial gruen.
        """
        import ai
        ai.instance._client = None
        for name, modul in self.module.items():
            aufbau = getattr(modul, "setup", None)
            if callable(aufbau):
                try:
                    aufbau()
                except Exception as exc:  # noqa: BLE001
                    self.fehler.append(f"{name}.setup(): {type(exc).__name__}: {exc}")
            kern = getattr(modul, "instance", None)
            if kern is not None and hasattr(kern, "_enabled"):
                kern._enabled = True

    # -- Orakel --------------------------------------------------------------
    def _orakel_art(self, name):
        if name in NICHT_PROBIEREN:
            return "keins"
        if name in MUSTER_ORAKEL:
            return "muster"
        return "handler"

    async def _handler_orakel(self, modul, wort):
        from werkzeug.attrappe import RauchKanal, rauch_nachricht
        kanal = RauchKanal()
        antwort = await modul.handle(rauch_nachricht(wort, kanal=kanal))
        return antwort is not None or bool(kanal.gesendet)

    def _muster_orakel(self, name, modul, wort):
        if name == "music":
            return modul.parse_command(wort) is not None
        if name == "moderation":
            return any(muster.match(wort) or muster.match(wort + " <@1> x")
                       for _label, muster in modul._ROUTES)
        return False

    def _reagiert(self, name, wort):
        art = self._orakel_art(name)
        if art == "keins":
            return False
        modul = self.module[name]
        try:
            if art == "muster":
                return self._muster_orakel(name, modul, wort)
            return self._loop.run_until_complete(self._handler_orakel(modul, wort))
        except Exception as exc:  # noqa: BLE001
            # Ein Absturz IST eine Reaktion - das Modul hat das Wort erkannt und
            # ist erst danach gestolpert. Als Reaktion zaehlen und den Absturz
            # melden ist ehrlicher, als das Wort stillschweigend zu verlieren.
            self.fehler.append(f"{name} bei '{wort}': {type(exc).__name__}: "
                               f"{str(exc)[:120]}")
            return True

    # -- Durchlauf -----------------------------------------------------------
    def statischer_rueckfall(self, gefunden, je_modul):
        """Die nicht probierbaren Module wenigstens statisch aufnehmen.

        admin, giveaway und voicegags duerfen nicht blind mit tausend Woertern
        gefuettert werden - admin startet den Bot neu, voicegags will in den
        Voice-Channel. Sie deshalb ganz wegzulassen waere aber die falsche
        Konsequenz: 'flo soundboard' waere dann in keiner Liste, und beim
        Aufteilen von voicegags koennte es spurlos verschwinden.

        Also kommen ihre Woerter aus dem Quelltext hinein - aber sichtbar
        gekennzeichnet: geschuetzt, nicht geprueft. Eine ausgewiesene Luecke
        ist ehrlich; eine getarnte waere gefaehrlich.
        """
        for modname, woerter in je_modul.items():
            if modname not in NICHT_PROBIEREN:
                continue
            for wort in woerter:
                if wort in gefunden:
                    continue          # ein geprobtes Modul hat es schon
                gefunden[wort] = {"module": [modname], "gewinner": modname,
                                  "_orakel": "statisch"}
        return gefunden

    def laufen(self, kandidaten):
        """kandidaten -> {wort: {"module": [...], "gewinner": name}}"""
        self.ruesten()
        # Der Event-Loop MUSS vor der Netzsperre stehen: asyncio baut sich beim
        # Start selbst ein Socket-Paar zur internen Verstaendigung. Eine Sperre
        # auf socket.socket wuerde also nicht den Bot am Telefonieren hindern,
        # sondern asyncio am Starten.
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        rang = {n: i for i, n in enumerate(self.reihenfolge)}
        gefunden = {}
        with KeinNetz():
            try:
                for wort in sorted(kandidaten):
                    trifft = [n for n in self.module if self._reagiert(n, wort)]
                    if not trifft:
                        continue
                    trifft.sort(key=lambda n: (rang.get(n, 999), n))
                    gefunden[wort] = {"module": trifft, "gewinner": trifft[0]}
            finally:
                self._loop.close()
                asyncio.set_event_loop(None)
        return gefunden


class KeinNetz:
    """Steckdose gezogen: waehrend der Probe darf nichts nach DRAUSSEN.

    Ein Modul, das in seinem Handler eine Anfrage stellt, wuerde die Probe
    sonst minutenlang haengen lassen - und schlimmstenfalls echte Dienste
    anfassen (YouTube, Spotify, die KI, Discord selbst).

    Gesperrt wird bewusst nur das WAEHLEN, nicht das Anlegen eines Sockets.
    Der erste Versuch sperrte socket.socket komplett - damit konnte asyncio
    sein internes Socket-Paar nicht mehr bauen und der Event-Loop startete gar
    nicht erst. Eine Sperre, die den Falschen trifft, ist schlimmer als keine:
    sie sieht nach Sicherheit aus und verhindert nur die eigene Arbeit.
    """

    ZIELE = ("getaddrinfo", "create_connection")

    def __enter__(self):
        import socket
        self._socket = socket
        self._alt = {name: getattr(socket, name) for name in self.ZIELE}
        self._alt_connect = socket.socket.connect
        for name in self.ZIELE:
            setattr(socket, name, self._nein)
        socket.socket.connect = self._nein
        return self

    def __exit__(self, *_a):
        for name, wert in self._alt.items():
            setattr(self._socket, name, wert)
        self._socket.socket.connect = self._alt_connect
        return False

    @staticmethod
    def _nein(*_a, **_k):
        raise OSError("Inventar-Probe: Netzzugriff ist abgeschaltet")


class Kataloge:
    """Alles, was der Bot als Liste von Eintraegen fuehrt.

    Server-Einstellungen (guildcfg.KATALOG), Funktionsschalter
    (features.CATALOG), Shop, Titel-Seltenheiten, Luxus-Stufen. Das sind die
    Sachen, die im Panel als Formular auftauchen - und beim Panel-Neubau am
    ehesten einzeln verlorengehen.

    Gesucht wird nicht nach Namen, sondern nach FORM: eine oeffentliche
    Konstante, deren Eintraege einen Schluessel haben. So wird ein neuer
    Katalog automatisch mitgenommen, statt hier nachgetragen werden zu muessen.
    """

    def __init__(self, module):
        self.module = module

    def sammeln(self):
        raus = {}
        for modname, modul in sorted(self.module.items()):
            gesperrt = KEIN_KATALOG.get(modname)
            if gesperrt == "*":
                continue
            for name in dir(modul):
                if name.startswith("_") or not name.isupper():
                    continue
                if gesperrt and name in gesperrt:
                    continue
                for key, label in self._eintraege(getattr(modul, name, None)):
                    raus[f"{name}:{key}"] = {"quelle": f"{modname}.{name}",
                                             "label": label}
        return raus

    @staticmethod
    def _eintraege(obj):
        """(schluessel, label) je Eintrag - oder nichts, wenn es kein Katalog ist."""
        if isinstance(obj, dict):
            posten = list(obj.items())
            if len(posten) < 2:
                return []
            raus = []
            for k, v in posten:
                if not isinstance(k, str) or not WORT_RE.match(k):
                    return []
                raus.append((k, str(v)[:60] if isinstance(v, (str, int, float)) else ""))
            return raus
        if not isinstance(obj, (list, tuple)) or len(obj) < 2:
            return []
        raus = []
        for eintrag in obj:
            if isinstance(eintrag, dict) and isinstance(eintrag.get("key"), str):
                raus.append((eintrag["key"], str(eintrag.get("label", ""))[:60]))
            elif isinstance(getattr(eintrag, "key", None), str):
                raus.append((eintrag.key, str(getattr(eintrag, "label", ""))[:60]))
            else:
                return []      # kein Katalog, sondern irgendeine andere Liste
        return raus


class Routen:
    """Die Endpunkte des Web-Panels - aus der frisch gebauten App gelesen.

    Nicht aus dem Quelltext geraten: die App weiss selbst am besten, was sie
    bedient. HEAD-Routen legt aiohttp neben jedem GET automatisch an; die
    fliegen raus, aber nur wenn Pfad UND Handler zu einem GET passen - eine
    echte, eigene HEAD-Route bliebe stehen.
    """

    def sammeln(self):
        import webpanel
        app = webpanel.WebPanel()._build_app()
        roh = []
        for route in app.router.routes():
            pfad = getattr(route.resource, "canonical", None) or str(route.resource)
            roh.append((route.method, pfad, getattr(route.handler, "__name__", "?")))
        gets = {(p, h) for m, p, h in roh if m == "GET"}
        raus = {}
        for methode, pfad, handler in roh:
            if methode == "HEAD" and (pfad, handler) in gets:
                continue
            raus[f"{methode} {pfad}"] = {"handler": handler}
        return raus


class Frontend:
    """Welche Endpunkte ruft die ausgelieferte Oberflaeche wirklich auf?

    Ein Endpunkt, den niemand aufruft, ist ein vergessener Knopf - genau der
    Fall 'Wordle jetzt starten wurde beim Neubau uebersehen'. Umgekehrt ist ein
    Aufruf ohne Endpunkt ein toter Knopf. Beides sieht man nur, wenn man beide
    Seiten aufschreibt.
    """

    def __init__(self, datei):
        self.datei = pathlib.Path(datei)

    def sammeln(self):
        text = self.datei.read_text(encoding="utf-8")
        raus = {}
        for treffer in re.findall(r"/api/[a-zA-Z0-9/_{}$-]*", text):
            pfad = treffer.rstrip("/?&")
            # Vorlagen-Einsetzungen (`/api/user/${id}`) auf die Route-Form bringen
            pfad = re.sub(r"\$\{[^}]*\}", "{x}", pfad)
            if len(pfad) > len("/api/"):
                raus.setdefault(pfad, {"quelle": self.datei.name})
        return raus


class ModulAPI:
    """Was ein Modul nach aussen anbietet.

    Am Dateiende steht in jedem Modul der OOP-Vertrag: 'handle = instance.handle'
    und Geschwister. Faellt so eine Zeile beim Aufteilen weg, merkt man es erst
    zur Laufzeit - und in einem tasks.loop stoppt so ein AttributeError den Loop
    dauerhaft. Genau so ist der Haendler-Loop schon einmal gestorben.
    """

    def __init__(self, module):
        self.module = module

    def sammeln(self):
        raus = {}
        for modname, modul in sorted(self.module.items()):
            for name in dir(modul):
                if name.startswith("_"):
                    continue
                wert = getattr(modul, name, None)
                if not callable(wert):
                    continue
                # Nur was WIRKLICH hier zuhause ist - sonst steht jeder Import
                # (json.dumps, re.compile) in der Liste.
                heimat = getattr(wert, "__module__", None)
                if heimat != modname:
                    continue
                raus[f"{modname}.{name}"] = {"art": type(wert).__name__}
        return raus


class Loops:
    """Alle @tasks.loop mit ihrem Intervall.

    Der AST ist hier die Hauptquelle, nicht der laufende Bot: er ist unabhaengig
    von der Umgebung und haelt den AUSDRUCK fest ('CHECK_INTERVAL_SECONDS'),
    nicht den Wert, den eine zufaellig geladene .env gerade ergibt.
    """

    def __init__(self, quelle):
        self.quelle = quelle

    def sammeln(self):
        raus = {}
        for fn in self.quelle.knoten(ast.AsyncFunctionDef):
            for schmuck in fn.decorator_list:
                if not isinstance(schmuck, ast.Call):
                    continue
                if getattr(schmuck.func, "attr", None) != "loop":
                    continue
                takt = {k.arg: ast.unparse(k.value) for k in schmuck.keywords}
                raus[fn.name] = {"takt": takt}
        return raus


# --- Zusammenfuehrung -------------------------------------------------------

class Inventar:
    """Fuehrt alle Quellen zusammen und schreibt/liest den Stand."""

    def __init__(self, laut=True):
        self.laut = laut
        self.module = {}
        self.fehler = []

    def _sagen(self, text):
        if self.laut:
            print(text)

    # -- Module laden --------------------------------------------------------
    def module_laden(self):
        """Jede Moduldatei im Wurzelverzeichnis importieren.

        Ein Importfehler wird NICHT verschluckt. Er wandert in self.fehler, und
        --schreibe verweigert danach den Dienst: ein Grundstand aus einer halb
        kaputten Umgebung waere schlimmer als gar keiner.
        """
        import importlib
        # .env neutralisieren: der Stand soll nicht davon abhaengen, welche
        # Werte auf DIESER Maschine gerade in der .env stehen.
        try:
            import dotenv
            dotenv.load_dotenv = lambda *a, **k: False
        except ImportError:
            pass
        namen = sorted(p.stem for p in WURZEL.glob("*.py")
                       if not p.stem.startswith(("test_", "tools_", "lauf")))
        for name in namen:
            if name in ("economy_reset",):
                continue          # Reset-Skript: wird NIE importiert
            try:
                self.module[name] = importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001
                self.fehler.append(f"import {name}: {type(exc).__name__}: {exc}")
        return self.module

    # -- Aufnehmen -----------------------------------------------------------
    def aufnehmen(self, mit_probe=True):
        start = time.time()
        self.module_laden()
        bot_quelle = Quelltext.hol(WURZEL / "bot.py")

        self._sagen("Quelltext lesen ...")
        kandidaten, eng = set(), {}
        for name in sorted(self.module):
            pfad = WURZEL / f"{name}.py"
            if not pfad.exists() or name in KEIN_CHATMODUL:
                continue
            jagd = Woerterjagd(Quelltext.hol(pfad))
            kandidaten |= jagd.alles()
            if name in NICHT_PROBIEREN:
                eng[name] = jagd.nur_befehlsstellen()
        self._sagen(f"  {len(kandidaten)} Kandidatenwoerter")

        try:
            reihenfolge = Reihenfolge(bot_quelle).module()
        except LookupError as exc:
            self.fehler.append(str(exc))
            reihenfolge = sorted(self.module)

        befehle, kollisionen = {}, {}
        if mit_probe:
            self._sagen("Module befragen ...")
            probe = Probe({n: m for n, m in self.module.items()
                           if n not in KEIN_CHATMODUL
                           and (hasattr(m, "handle") or n in MUSTER_ORAKEL)},
                          reihenfolge)
            befehle = probe.laufen(kandidaten)
            probe.statischer_rueckfall(befehle, eng)
            self.fehler.extend(probe.fehler)
            kollisionen = {w: d for w, d in befehle.items()
                           if len(d["module"]) > 1}
            self._sagen(f"  {len(befehle)} Woerter werden beantwortet, "
                        f"{len(kollisionen)} davon von mehreren Modulen")

        stand = {
            "erzeugt": time.strftime("%Y-%m-%d %H:%M"),
            "umgebung": {
                "python": ".".join(str(x) for x in sys.version_info[:3]),
                "module_geladen": len(self.module),
                "probe_gelaufen": bool(mit_probe),
            },
            "befehle": befehle,
            "routen": self._sicher("routen", Routen().sammeln),
            "kataloge": self._sicher("kataloge", Kataloge(self.module).sammeln),
            "loops": self._sicher("loops", Loops(bot_quelle).sammeln),
            "modulapi": self._sicher("modulapi", ModulAPI(self.module).sammeln),
            "frontend": self._sicher(
                "frontend", Frontend(WURZEL / "webpanel.html").sammeln),
            "_kollisionen": kollisionen,
            "_nicht_geprobt": NICHT_PROBIEREN,
            "_fehler": self.fehler,
            "_dauer": round(time.time() - start, 1),
        }
        stand["zaehlung"] = {k: len(stand[k]) for k in MENGEN}
        return stand

    def _sicher(self, name, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            self.fehler.append(f"{name}: {type(exc).__name__}: {exc}")
            if self.laut:
                traceback.print_exc()
            return {}

    # -- Datei ---------------------------------------------------------------
    @staticmethod
    def lesen():
        if not STAND_DATEI.exists():
            return None
        return json.loads(STAND_DATEI.read_text(encoding="utf-8"))

    @staticmethod
    def schreiben(stand):
        ORDNER.mkdir(exist_ok=True)
        STAND_DATEI.write_text(
            json.dumps(stand, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")


#: Die Kategorien, die gezaehlt und verglichen werden. Reihenfolge = Ausgabe.
MENGEN = ("befehle", "routen", "kataloge", "loops", "modulapi", "frontend")


class Vergleich:
    """Alt gegen neu - und zwar nur ueber die Schluessel.

    Werte (Hilfetexte, Zahlen, welches Modul gerade zustaendig ist) duerfen sich
    beim Umbau aendern. Schluessel duerfen nur dazukommen.
    """

    def __init__(self, alt, neu, erwartet=None):
        self.alt = alt
        self.neu = neu
        self.erwartet = erwartet or {}

    def fehlend(self):
        raus = {}
        for kategorie in MENGEN:
            weg = sorted(set(self.alt.get(kategorie, {}))
                         - set(self.neu.get(kategorie, {})))
            if weg:
                raus[kategorie] = weg
        return raus

    def neuzugang(self):
        raus = {}
        for kategorie in MENGEN:
            dazu = sorted(set(self.neu.get(kategorie, {}))
                          - set(self.alt.get(kategorie, {})))
            if dazu:
                raus[kategorie] = dazu
        return raus

    def umgezogen(self):
        """Schluessel, die es noch gibt, aber woanders. Notiz, kein Fehler."""
        raus = {}
        for kategorie, feld in (("befehle", "gewinner"), ("routen", "handler"),
                                ("kataloge", "quelle")):
            for key, wert in self.neu.get(kategorie, {}).items():
                frueher = self.alt.get(kategorie, {}).get(key)
                if not isinstance(frueher, dict) or not isinstance(wert, dict):
                    continue
                if frueher.get(feld) != wert.get(feld):
                    raus[f"{kategorie}:{key}"] = (frueher.get(feld), wert.get(feld))
        return raus

    def abgenickt(self, kategorie, schluessel):
        """Steht dieser Verlust angekuendigt in erwartet.json?"""
        eintrag = self.erwartet.get(kategorie, {}).get(schluessel)
        return isinstance(eintrag, dict) and eintrag.get("grund") and eintrag.get("datum")


# --- Angekuendigte Verluste -------------------------------------------------

class Erwartet:
    """Verluste, die jemand vorher angekuendigt und begruendet hat.

    Ohne so eine Liste wird das Werkzeug beim ersten absichtlichen Umbau
    laestig und danach ignoriert. Mit ihr besteht die Gefahr, dass man alles
    reinschreibt, was gerade rot ist. Vier Regeln halten dagegen:

      1. Jeder Eintrag braucht 'grund' und 'datum' - man muss es hinschreiben.
      2. Ein Eintrag gilt fuer EINEN --schreibe-Lauf. Danach ist er verbraucht
         und wird entfernt. Ein Freibrief laeuft ab.
      3. Ein Eintrag, der im Vergleich gar nicht auftaucht, ist selbst ein
         Fehler: du hast etwas angekuendigt, das nie passiert ist.
      4. Ganze Kategorien lassen sich nicht abnicken, nur einzelne Schluessel.
    """

    @staticmethod
    def lesen():
        if not ERWARTET_DATEI.exists():
            return {}
        daten = json.loads(ERWARTET_DATEI.read_text(encoding="utf-8"))
        for kategorie, posten in list(daten.items()):
            if kategorie.startswith("_"):
                daten.pop(kategorie)
                continue
            if not isinstance(posten, dict):
                raise ValueError(
                    f"erwartet.json: '{kategorie}' muss einzelne Schluessel "
                    f"auflisten. Ganze Kategorien kann man nicht abnicken.")
        return daten

    @staticmethod
    def leeren():
        ORDNER.mkdir(exist_ok=True)
        ERWARTET_DATEI.write_text(json.dumps({
            "_hinweis": ("Angekuendigte Verluste. Jeder Eintrag braucht 'grund' "
                         "und 'datum' und gilt fuer GENAU EINEN --schreibe-Lauf; "
                         "danach loescht --schreibe ihn hier heraus."),
            "_beispiel": {"befehle": {"altwort": {
                "grund": "durch 'neuwort' ersetzt", "datum": "2026-08-31"}}},
        }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# --- Ausgabe ----------------------------------------------------------------

def zaehlung_zeigen(stand):
    """Immer und gut sichtbar: wie viel wurde ueberhaupt gefunden?

    Das ist die wichtigste Zeile der ganzen Ausgabe. Ein Einbruch hier heisst
    nicht 'der Bot ist kleiner geworden', sondern meistens 'das Werkzeug lief
    in einer kaputten Umgebung' - und ein Grundstand aus so einem Lauf waere
    ein Sicherheitsnetz mit Loch.
    """
    zahlen = stand.get("zaehlung", {})
    print("\n  ZAEHLUNG  " + " · ".join(
        f"{zahlen.get(k, 0)} {k}" for k in MENGEN))
    fehler = stand.get("_fehler", [])
    if fehler:
        print(f"  {len(fehler)} Warnung(en) beim Aufnehmen:")
        for zeile in fehler[:15]:
            print(f"    - {zeile}")
        if len(fehler) > 15:
            print(f"    ... und {len(fehler) - 15} weitere")
    print()


def untergrenze_pruefen(stand):
    """Zu wenig gefunden -> das Werkzeug ist kaputt, nicht der Bot."""
    zahlen = stand.get("zaehlung", {})
    pruefen = dict(MINDESTENS)
    if not stand.get("umgebung", {}).get("probe_gelaufen"):
        pruefen.pop("befehle")     # ohne Probe gibt es naturgemaess keine
    return [f"{k}: {zahlen.get(k, 0)} gefunden, mindestens {pruefen[k]} erwartet"
            for k in pruefen if zahlen.get(k, 0) < pruefen[k]]


# --- cmdnorm messen statt raten ---------------------------------------------

class CmdnormPruefung:
    """Welche Woerter verbiegt cmdnorm, ohne dass es jemand so wollte?

    cmdnorm korrigiert Vertipper auf Befehle. Das geschieht in drei Toepfen,
    und nur einer davon ist gefaehrlich:

        ALIAS     exakte Uebersetzung ('cash' -> 'coins')     - gewollt
        DIALECT   exakte Mundart ('fladern' -> 'klau')        - gewollt
        _fuzzy    Tippfehler-Toleranz nach Buchstabenabstand  - raet

    Nur _fuzzy raet. Und weil es raet, kann es ein ganz normales Wort auf einen
    Befehl biegen: 'arten' wird zu 'raten', 'eich' zu 'reich'. Wer das schreibt,
    loest einen Befehl aus, den er nicht gemeint hat.

    Der erste Messversuch warf alle drei Toepfe zusammen und meldete 299
    'Fehlgriffe' - fast alle davon absichtliche Aliase. Eine Liste, in der das
    Gewollte und das Verunglueckte nebeneinanderstehen, liest niemand zweimal.
    Deshalb wird hier nach MECHANISMUS getrennt, und nur der ratende Topf
    ausgewertet.

    Drei Schweregrade:

      hijack      Das Wort ist selbst ein funktionierender Befehl eines Moduls,
                  steht aber nicht in cmdnorm.KNOWN. cmdnorm schreibt es um,
                  BEVOR das Modul es je sieht - der Befehl ist unerreichbar.
                  Das ist die schlimmste Sorte: eine verlorene Funktion.
      fehlgriff   Ein ANDERES Wort landet auf einem Befehl ('arten' -> 'raten').
      beugung     Dieselbe Wortfamilie ('bannen' -> 'banne') - genau dafuer ist
                  die Toleranz da, das soll so bleiben.
      kurz        Unter vier Zeichen - da ist jeder Abstand klein, und es tippt
                  ohnehin kaum jemand.

    Diese Klasse AENDERT NICHTS. Sie liefert die Zahlen, auf deren Grundlage
    man cmdnorm dann von Hand repariert (naemlich per STOPWORDS).
    """

    def __init__(self, stand):
        self.befehle = {w.lower() for w in stand.get("befehle", {})}

    def wortschatz(self):
        """Alle Woerter, die im Repo als Text vorkommen - die Sprache des Bots."""
        woerter = set()
        for pfad in sorted(WURZEL.glob("*.py")):
            for treffer in re.findall(r"[a-zA-ZÀ-ɏ][\wÀ-ɏ]{2,23}",
                                      pfad.read_text(encoding="utf-8")):
                woerter.add(treffer.lower())
        return woerter

    def messen(self):
        """-> {"hijack": {...}, "alltagswort": {...}, "kurz": {...}}"""
        import cmdnorm
        raus = {"hijack": {}, "fehlgriff": {}, "beugung": {}, "kurz": {}}
        for wort in sorted(self.wortschatz()):
            if wort in cmdnorm.KNOWN:
                continue                 # cmdnorm laesst echte Befehle in Ruhe
            if wort in cmdnorm.ALIAS or wort in cmdnorm.DIALECT:
                continue                 # gewollte Uebersetzung, kein Fehlgriff
            if "_" in wort:
                continue                 # Bezeichner aus dem Code, kein Chatwort
            try:
                nachher = cmdnorm.normalize(wort)
            except Exception:            # noqa: BLE001
                continue
            if not nachher:
                continue
            ziel = nachher.split()[0].lower()
            if ziel == wort:
                continue
            if wort in self.befehle:
                raus["hijack"][wort] = ziel
            elif len(wort) < 4:
                raus["kurz"][wort] = ziel
            elif self._ist_beugung(wort, ziel):
                raus["beugung"][wort] = ziel
            else:
                raus["fehlgriff"][wort] = ziel
        return raus

    @staticmethod
    def _ist_beugung(wort, ziel):
        """Ist das eine Beugung des Befehls - oder ein anderes Wort?

        Diese Unterscheidung ist der ganze Nutzen der Messung. 'bannen' ->
        'banne' und 'charts' -> 'chart' sind genau das, wofuer die Toleranz
        gebaut wurde: derselbe Wortstamm, nur anders gebeugt. 'arten' ->
        'raten' und 'erlass' -> 'verlass' sind etwas voellig anderes - zwei
        verschiedene Woerter, die zufaellig einen Buchstaben auseinanderliegen.

        Ohne die Trennung stehen beide in derselben Liste, die Liste hat 218
        Zeilen, und niemand liest sie zu Ende. Das Kennzeichen der Beugung: der
        eine ist der Anfang des anderen.
        """
        return wort.startswith(ziel) or ziel.startswith(wort)

    @staticmethod
    def vorschlag_pruefen(woerter):
        """Waere es sicher, diese Woerter in STOPWORDS aufzunehmen?

        Ein Wort in STOPWORDS laesst cmdnorm in Ruhe. Steht dort aber ein
        echter Befehl, waere die Tippfehler-Toleranz FUER diesen Befehl weg -
        aus einer Reparatur wuerde ein neuer Schaden. cmdnorm faengt das in der
        Klasse schon ab (STOPWORDS -= KNOWN), aber ein stiller Abfang ist keine
        Antwort: hier wird der Konflikt benannt.
        """
        import cmdnorm
        return sorted(set(woerter) & set(cmdnorm.KNOWN))


# --- Kommandozeile ----------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--schreibe", action="store_true",
                   help="Grundstand neu aufnehmen (inventar/stand.json)")
    p.add_argument("--vergleiche", action="store_true",
                   help="aktuellen Code gegen den Grundstand pruefen")
    p.add_argument("--zeige", action="store_true", help="Stand lesbar ausgeben")
    p.add_argument("--cmdnorm", action="store_true",
                   help="messen, welche Woerter cmdnorm heute verbiegt")
    p.add_argument("--ohne-probe", action="store_true",
                   help="nur den Quelltext lesen, keine Module befragen (schnell)")
    p.add_argument("--leise", action="store_true")
    a = p.parse_args(argv)

    if not any((a.schreibe, a.vergleiche, a.zeige, a.cmdnorm)):
        p.print_help()
        return 3

    if a.zeige:
        stand = Inventar.lesen()
        if stand is None:
            print("Kein Grundstand da. Erst: python werkzeug/inventar.py --schreibe")
            return 3
        zaehlung_zeigen(stand)
        for wort, d in sorted(stand.get("_kollisionen", {}).items()):
            print(f"  Kollision  {wort:20} {', '.join(d['module'])}"
                  f"   -> {d['gewinner']}")
        return 0

    inv = Inventar(laut=not a.leise)
    neu = inv.aufnehmen(mit_probe=not a.ohne_probe)
    zaehlung_zeigen(neu)

    if a.cmdnorm:
        fund = CmdnormPruefung(neu).messen()
        for art, titel in (
                ("hijack", "HIJACK - das Wort IST ein Befehl, kommt aber nie an"),
                ("fehlgriff", "FEHLGRIFF - ein anderes Wort landet auf einem Befehl"),
                ("beugung", "Beugungen (so soll die Toleranz arbeiten)"),
                ("kurz", "kurze Woerter (unter 4 Zeichen)")):
            posten = fund[art]
            print(f"\n{titel}: {len(posten)}")
            for wort, ziel in sorted(posten.items())[:60]:
                print(f"  {wort:24} -> {ziel}")
            if len(posten) > 60:
                print(f"  ... und {len(posten) - 60} weitere")
        konflikt = CmdnormPruefung.vorschlag_pruefen(
            list(fund["hijack"]) + list(fund["fehlgriff"]))
        if konflikt:
            print("\nACHTUNG - diese Woerter sind selbst Befehle in cmdnorm.KNOWN "
                  "und duerfen\nNICHT einfach in STOPWORDS: " + ", ".join(konflikt))
        return 0

    zu_wenig = untergrenze_pruefen(neu)
    if zu_wenig:
        print("ABBRUCH - das Inventar hat zu wenig gefunden. Das heisst fast "
              "immer:\ndie Umgebung ist kaputt (Pakete fehlen, ein Import "
              "fliegt), nicht der Bot.\n")
        for zeile in zu_wenig:
            print(f"  {zeile}")
        return 3

    if a.schreibe:
        if neu["_fehler"]:
            print("ABBRUCH - beim Aufnehmen gab es Fehler (siehe oben). Ein "
                  "Grundstand aus\neiner halb kaputten Umgebung waere schlimmer "
                  "als gar keiner: er macht\njeden spaeteren Vergleich trivial "
                  "gruen. Erst die Fehler klaeren.")
            return 3
        Inventar.schreiben(neu)
        Erwartet.leeren()      # Regel 2: Freibriefe sind nach dem Schreiben weg
        print(f"Grundstand geschrieben: {STAND_DATEI.relative_to(WURZEL)}")
        return 0

    # --vergleiche
    alt = Inventar.lesen()
    if alt is None:
        print("Kein Grundstand da. Erst: python werkzeug/inventar.py --schreibe")
        return 3
    try:
        erwartet = Erwartet.lesen()
    except ValueError as exc:
        print(exc)
        return 3

    v = Vergleich(alt, neu, erwartet)
    fehlend, dazu, umzug = v.fehlend(), v.neuzugang(), v.umgezogen()

    echt, abgenickt = {}, {}
    for kategorie, schluessel in fehlend.items():
        for k in schluessel:
            (abgenickt if v.abgenickt(kategorie, k) else echt).setdefault(
                kategorie, []).append(k)

    for kategorie, schluessel in sorted(dazu.items()):
        print(f"  NEU  {kategorie}: {len(schluessel)}")
        for k in schluessel[:12]:
            print(f"         + {k}")
        if len(schluessel) > 12:
            print(f"         ... und {len(schluessel) - 12} weitere")
    for key, (frueher, jetzt) in sorted(umzug.items()):
        print(f"  UMZUG  {key}: {frueher} -> {jetzt}")
    for kategorie, schluessel in sorted(abgenickt.items()):
        print(f"  ANGEKUENDIGT WEG  {kategorie}: {', '.join(schluessel)}")

    # Regel 3: angekuendigt, aber nie passiert
    luft = []
    for kategorie, posten in erwartet.items():
        for k in posten:
            if k not in fehlend.get(kategorie, []):
                luft.append(f"{kategorie}:{k}")
    if luft:
        print("\nFEHLER - in erwartet.json steht ein Verlust, den es gar nicht "
              "gibt:\n  " + "\n  ".join(luft) +
              "\nEntweder ist der Eintrag alt, oder er ist ein Tippfehler. Beides "
              "gehoert weg,\nbevor er beim naechsten echten Verlust unbemerkt "
              "danebensteht.")
        return 2

    if not echt:
        print("Alles noch da." if not (dazu or umzug or abgenickt)
              else "Nichts unangekuendigt verloren.")
        return 1 if abgenickt else 0

    print("\nVERLOREN - das hier gab es vorher und gibt es jetzt nicht mehr:\n")
    for kategorie, schluessel in sorted(echt.items()):
        print(f"  {kategorie} ({len(schluessel)}):")
        for k in schluessel:
            frueher = alt[kategorie].get(k, {})
            zusatz = frueher.get("gewinner") or frueher.get("handler") \
                or frueher.get("quelle") or ""
            print(f"    - {k}" + (f"   (war: {zusatz})" if zusatz else ""))
    print("\nEntweder war es ein Versehen - dann zurueckbauen. Oder es war "
          "Absicht -\ndann in inventar/erwartet.json eintragen, mit Grund und "
          "Datum.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
