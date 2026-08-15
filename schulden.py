"""Flos Schuldbuch: wer wem WIRKLICH etwas schuldet - und warum.

Der alte Bau hatte einen Konstruktionsfehler in der Wurzel: JEDE 'Flo pay'-
Zahlung erzeugte automatisch eine Forderung des Zahlers gegen den Empfaenger.
Ein Geschenk, ein verlorener Wetteinsatz, eine geteilte Rechnung - alles wurde
stillschweigend zur Schuld, ohne dass einer der beiden das je so gemeint hat.
Der Rest (Tilgung, Mahnung, Erlassen) war solide Buchfuehrung auf falschem
Fundament.

    Der Grundsatz jetzt: eine Schuld entsteht nur durch ZUSTIMMUNG.
    Wer zahlt, schenkt. Wer leiht, leiht ausdruecklich.

Drei Wege fuehren zu einer Schuld, und alle drei brauchen einen Klick der
anderen Person:

1. 'Flo leih @wer 5k [Grund] [bis freitag]' - der klassische Kredit. Erst der
   Klick auf "Annehmen" bewegt Geld UND legt den Posten an.
2. 'Flo pay @wer 5k als leihgabe'          - Zahlung, die der Zahler
   ausdruecklich als Leihgabe markiert. Ohne den Zusatz bleibt pay ein
   Geschenk.
3. 'Flo schuldschein @wer 5k [Grund]'      - Schuld OHNE Geldfluss ("du
   schuldest mir noch vom Kino"). Hier bestaetigt der SCHULDNER; niemand kann
   einem Fremden per Befehl Schulden anhaengen.

Gefuehrt werden EINZELNE POSTEN, nicht mehr ein Netto-Saldo je Paar. Nur so
gibt es Faelligkeit, Grund und Historie je Schuld - und nur so laesst sich
fair tilgen. Der Saldo zwischen zwei Leuten wird BERECHNET, nie gespeichert.

Befehle (nach 'Flo'):
- schulden                      deine Uebersicht (offen, Forderungen, Score)
- schulden @wer                 Stand mit dieser Person, Posten einzeln
- schulden top                  die groessten offenen Posten
- schulden erlassen @wer [x]    als Glaeubiger streichen (ganz oder teils)
- leih @wer 5k [grund] [bis..]  Kredit anbieten (Empfaenger bestaetigt)
- schuldschein @wer 5k [grund]  Schuld ohne Geldfluss (Schuldner bestaetigt)
- tilg @wer [betrag]            freiwillige Sondertilgung
- insolvenz                     Privatinsolvenz (mit Rueckfrage)

Zustand: data/schulden.json
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import discord

import ai
import economy
import numfmt
from store import JsonStore

log = logging.getLogger("dcbot.schulden")

# So lange werden Aenderungen gesammelt, bevor geschrieben wird (Sekunden).
SAVE_DEBOUNCE = float(os.getenv("SCHULDEN_SAVE_DEBOUNCE", "3") or "3")

# Sentinel: das Modul hat selbst geantwortet -> bot.py schweigt.
HANDLED = object()

fmt = numfmt.fmt

TAG = 86400.0

# Bewusst OHNE "offen"/"soll": das sind zu allgemeine Woerter und wuerden normale
# Saetze ("Flo offen gesagt ...") als Befehl abfangen.
_CMDS = ("schulden", "schuld", "kreide", "kreidetafel", "zettel", "schuldenbuch",
         "debt", "debts")
# Eigenstaendige Befehle rund ums Schuldbuch.
_CMD_LEIH = ("leih", "leihe", "leihen", "verleih", "verleihe", "kredit", "borg",
             "borge", "borgen")
_CMD_SCHEIN = ("schuldschein", "schein", "iou", "anschreiben")
_CMD_TILG = ("tilg", "tilge", "tilgen", "abzahl", "abzahlen", "zurueckzahlen",
             "zurückzahlen")
_CMD_INSOLVENZ = ("insolvenz", "privatinsolvenz", "bankrott", "pleite")

# So viele Bewegungen bleiben je Posten erhalten (fuer die Detail-Ansicht).
LOG_KEPT = 12
# Ab dieser Summe faellt der Hinweis etwas deutlicher aus.
DEUTLICH_AB = int(os.getenv("SCHULDEN_DEUTLICH_AB", "100000") or "100000")

# --- Automatische Tilgung ("was zwingt mich zu zahlen?") ---------------------
# Von JEDER echten Einnahme wandert dieser Anteil automatisch an die Glaeubiger,
# bis nichts mehr offen ist. Coins entstehen dabei nie und verschwinden nie -
# sie wechseln nur den Besitzer, und das Buch schreibt es mit.
TILGUNG_PCT = float(os.getenv("SCHULDEN_TILGUNG_PCT", "0.20") or "0.20")
# Kleinstbetraege in Ruhe lassen (sonst wird jede 5-Coin-Gutschrift angeknabbert).
TILGUNG_MIN_EINNAHME = int(os.getenv("SCHULDEN_TILGUNG_MIN", "50") or "50")
# Unter diesem Betrag lohnt sich eine Ueberweisung an einen einzelnen
# Glaeubiger nicht - sonst entstehen 1-Coin-Buchungen.
TILGUNG_MIN_JE_GLAEUBIGER = 10
# Quellen, die NICHT angetastet werden:
# - schulden-tilgung: eigene Buchung (Rekursions-Schutz)
# - panel: Korrekturen aus dem Dashboard muessen exakt bleiben
# - floaktie: Aktien-Erloese, damit die Kurs-Rechnung sauber bleibt
# - giveaway/-rueck: Escrow gehoert dem Veranstalter
# - pay: direkte Ueberweisungen zwischen Menschen. Sonst passiert Unsinn: der
#   Glaeubiger gibt dem Schuldner 500, und 100 davon wandern im selben Moment
#   automatisch zu ihm zurueck.
# Getilgt wird also aus ECHTEN Einnahmen: Daily, Casino, Spiele, Gewinne, Raub ...
#
# Diese Liste und die '-rueck'-Ausnahme sind aus dem alten Bau UNVERAENDERT
# uebernommen - sie ist getestet und richtig.
TILGUNG_TABU = ("schulden-tilgung", "panel", "floaktie", "giveaway-rueck",
                "giveaway", "dividende", "pay",
                # Rueckbuchungen sind keine Einnahme: sonst frisst die Tilgung
                # einen Teil des zurueckgegebenen Geldes.
                "shop-rueck", "floaktie-rueck",
                # Korrekturen des Besitzers muessen EXAKT ankommen - genau wie
                # beim Panel ('panel' steht schon oben). 'gib 1000' soll 1000
                # geben und nicht 800, nur weil der Empfaenger Schulden hat.
                "admin", "setcoins")

# Mahnung: hoechstens so oft eine DM je Schuldner.
MAHN_ABSTAND = int(os.getenv("SCHULDEN_MAHN_ABSTAND", "86400") or "86400")
# Erst ab dieser Summe mahnt Flo ueberhaupt.
MAHN_AB = int(os.getenv("SCHULDEN_MAHN_AB", "1000") or "1000")
# Eskalationsstufen (Tage ueber der Faelligkeit).
MAHN_STUFE_2 = 7
MAHN_STUFE_3 = 14

# --- Grenzen & Missbrauchsschutz --------------------------------------------
# Unter diesem Betrag lohnt die Buchfuehrung nicht.
MIN_POSTEN = int(os.getenv("SCHULDEN_MIN", "50") or "50")
MAX_POSTEN_JE_PAAR = 5
MAX_POSTEN_JE_PERSON = 25
# Gesamtschuld hoechstens so viel mal das eigene Vermoegen - verhindert
# Schuldenberge, die nie tilgbar sind.
MAX_SCHULD_FAKTOR = float(os.getenv("SCHULDEN_MAX_FAKTOR", "3") or "3")
# Ohne jede Bewegung verfaellt ein Posten nach so vielen Tagen (kein ewiges
# Druckmittel). So viele Tage vorher bekommt der Glaeubiger eine letzte DM.
VERFALL_TAGE = int(os.getenv("SCHULDEN_VERFALL_TAGE", "60") or "60")
VERFALL_WARNUNG_TAGE = 7
# Nach einer Insolvenz so lange keine neuen Schulden.
INSOLVENZ_SPERRE_TAGE = 14
INSOLVENZ_QUOTE = 0.5          # so viel des Vermoegens wird verteilt

# Wie lange eine Anfrage (Leihe/Schuldschein) auf den Klick wartet.
ANFRAGE_TIMEOUT = 300

FARBE = 0xC9A227
FARBE_SCHULDEN = 0xE04B3C      # rot: da ist was offen
FARBE_SAUBER = 0x2ECC71        # gruen: blitzsauber

# 'Flo pay @wer 5k als leihgabe' - nur MIT diesem Zusatz wird aus einer Zahlung
# eine Schuld. Ohne ihn bleibt pay ein Geschenk.
LEIHGABE_RE = re.compile(
    r"\bals\s+(?:leihgabe|kredit|darlehen|geliehen|leihe)\b|\bauf\s+pump\b|"
    r"\bleihweise\b", re.IGNORECASE)

# 'bis freitag', 'bis in 7 tagen', 'faellig am 24.12.'
_FRIST_RE = re.compile(
    r"\bbis\s+(?:zum\s+|in\s+)?(\d+)\s*(tag|tage|tagen|woche|wochen|monat|monate|monaten)\b",
    re.IGNORECASE)
_WOCHENTAGE = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4,
    "samstag": 5, "sonntag": 6, "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4,
    "sa": 5, "so": 6,
}
_FRIST_TAG_RE = re.compile(
    r"\bbis\s+(?:zum\s+)?(" + "|".join(_WOCHENTAGE) + r")\b", re.IGNORECASE)


def _echtes_ziel(message):
    """Die erste Erwaehnung, die ein MENSCH ist - oder None.

    Wer Flo per @Mention anspricht, hat den Bot selbst in message.mentions
    stehen. Ohne diesen Filter zeigte '@Flo schulden' die Paar-Tafel mit Flo
    als Gegenueber, und '@Flo schulden erlassen @Kumpel' richtete sich gegen
    den Bot."""
    for m in (getattr(message, "mentions", None) or []):
        if not getattr(m, "bot", False):
            return m
    return None


def _zahl(wert, standard=0):
    """Zahl aus dem Buch. Murks zaehlt als 'standard' statt zu crashen."""
    try:
        return int(wert)
    except (TypeError, ValueError):
        return standard


@dataclass
class Posten:
    """EINE Schuld: wer, bei wem, wie viel, warum, bis wann.

    'urspruenglich' bleibt fuer immer stehen (damit "3k von 5k getilgt"
    anzeigbar ist), 'offen' schrumpft. Ein erledigter Posten wird NICHT
    geloescht - er bleibt als Historie und traegt die Kreditwuerdigkeit."""

    id: int
    glaeubiger: int
    schuldner: int
    urspruenglich: int
    offen: int
    grund: str = ""
    entstanden: float = 0.0
    faellig: float = 0.0            # 0 = ohne Termin
    status: str = "offen"           # offen | getilgt | erlassen | verfallen
    # Wurde die Ueberfaelligkeit schon einmal auf den Score gebucht? (einmalig)
    ueberfaellig_gebucht: bool = False
    # Bis wohin ist gemahnt worden (0 = gar nicht, sonst die Stufe 1-3).
    mahnstufe: int = 0
    letzte_mahnung: float = 0.0
    log: list = field(default_factory=list)

    @property
    def bewegt(self):
        """Zeitpunkt der letzten Bewegung (fuer den Verfall)."""
        if self.log:
            return max(_zahl(e.get("t", 0)) for e in self.log) or self.entstanden
        return self.entstanden

    def ist_offen(self):
        return self.status == "offen" and self.offen > 0

    def ist_ueberfaellig(self, jetzt=None):
        if not self.ist_offen() or not self.faellig:
            return False
        return (jetzt or time.time()) > self.faellig

    def notiere(self, art, betrag):
        self.log.append({"t": time.time(), "art": art, "betrag": int(betrag)})
        del self.log[:-LOG_KEPT]

    def als_dict(self):
        d = dict(self.__dict__)
        d["log"] = list(self.log)
        return d

    @classmethod
    def aus_dict(cls, roh):
        """Baut einen Posten aus dem Store. Unbrauchbares gibt None.

        Muss jeden Murks aushalten: die Typ-Schablone des JsonStore prueft nur,
        dass 'posten' eine Liste ist, nicht was darin steht."""
        if not isinstance(roh, dict):
            return None
        try:
            p = cls(
                id=_zahl(roh.get("id")),
                glaeubiger=_zahl(roh.get("glaeubiger")),
                schuldner=_zahl(roh.get("schuldner")),
                urspruenglich=max(0, _zahl(roh.get("urspruenglich"))),
                offen=max(0, _zahl(roh.get("offen"))),
                grund=str(roh.get("grund") or "")[:100],
                entstanden=float(_zahl(roh.get("entstanden"))),
                faellig=float(_zahl(roh.get("faellig"))),
                status=str(roh.get("status") or "offen"),
                ueberfaellig_gebucht=bool(roh.get("ueberfaellig_gebucht")),
                mahnstufe=_zahl(roh.get("mahnstufe")),
                letzte_mahnung=float(_zahl(roh.get("letzte_mahnung"))),
                log=[e for e in (roh.get("log") or []) if isinstance(e, dict)][-LOG_KEPT:],
            )
        except (TypeError, ValueError):
            return None
        if not p.glaeubiger or not p.schuldner or p.glaeubiger == p.schuldner:
            return None
        if p.status not in ("offen", "getilgt", "erlassen", "verfallen"):
            p.status = "offen"
        return p


class Kreditwuerdigkeit:
    """Score 0-100 je Person, NUR aus dem eigenen Verhalten.

    Wer puenktlich zurueckzahlt, darf mehr leihen; wer Posten verfallen laesst,
    weniger. Bewusst nachvollziehbar statt clever: jede Bewegung ist ein
    Eintrag mit Zeitpunkt, und was aelter als 90 Tage ist, zaehlt nur noch
    halb - ein Ausrutscher von vor einem halben Jahr soll niemanden ewig
    verfolgen."""

    START = 50
    HALBWERT_TAGE = 90
    MAX_EREIGNISSE = 60          # aeltere fallen hinten raus (Datei klein halten)

    PUENKTLICH = 5
    SONDERTILGUNG = 2
    UEBERFAELLIG = -10
    AUSGEFALLEN = -15
    INSOLVENZ_SCORE = 10         # darauf faellt der Score bei Privatinsolvenz

    # Als "puenktlich" zaehlt auch ein Posten ohne Termin, wenn er schnell
    # zurueckgezahlt wurde.
    SCHNELL_TAGE = 14

    def __init__(self, buch):
        self._buch = buch

    def _eintraege(self, uid):
        eintraege = self._buch.score_daten().setdefault(str(int(uid)), [])
        if not isinstance(eintraege, list):
            eintraege = self._buch.score_daten()[str(int(uid))] = []
        return eintraege

    def notiere(self, uid, delta, grund=""):
        eintraege = self._eintraege(uid)
        eintraege.append({"t": time.time(), "d": int(delta), "g": str(grund)[:40]})
        del eintraege[:-self.MAX_EREIGNISSE]

    def score(self, uid):
        """0-100. Alles aelter als 90 Tage zaehlt halb."""
        jetzt = time.time()
        wert = float(self.START)
        for e in self._eintraege(uid):
            if not isinstance(e, dict):
                continue
            delta = _zahl(e.get("d"))
            alter = jetzt - float(_zahl(e.get("t")))
            if alter > self.HALBWERT_TAGE * TAG:
                delta *= 0.5
            wert += delta
        return int(max(0, min(100, round(wert))))

    def ampel(self, uid):
        s = self.score(uid)
        return "🟢" if s >= 60 else ("🟡" if s >= 30 else "🔴")

    def leih_limit(self, glaeubiger, schuldner):
        """Obergrenze fuer EINEN neuen Posten.

        Schuetzt den Verleiher vor sich selbst: je Posten hoechstens
        Score-Prozent des eigenen Vermoegens. Bei Score 50 also die Haelfte,
        bei 100 alles, bei 20 ein Fuenftel. Der Score der SCHULDNER-Seite
        entscheidet, das Vermoegen der Glaeubiger-Seite."""
        habe = max(0, economy.get_coins(glaeubiger))
        anteil = habe * (self.score(schuldner) / 100.0)
        return max(MIN_POSTEN, int(anteil))

    def gesperrt(self, uid):
        """(gesperrt, Grund) - unter Score 20 gibt es nichts Neues."""
        if self.score(uid) < 20:
            return True, ("deine Kreditwürdigkeit ist gerade zu niedrig "
                          "(unter 20). Tilge erst etwas.")
        bis = float(_zahl(self._buch.stats(uid).get("insolvenz_bis")))
        if bis > time.time():
            tage = max(1, int((bis - time.time()) / TAG))
            return True, (f"nach einer Privatinsolvenz sind {tage} Tag(e) lang "
                          f"keine neuen Schulden möglich.")
        return False, ""


class Schuldbuch:
    """Haelt die Posten, rechnet Salden und kennt niemanden persoenlich.

    Bewusst ohne Discord: alles hier ist reine Buchfuehrung und laesst sich
    einzeln testen. Der Saldo zwischen zwei Leuten wird IMMER berechnet - ein
    gespeicherter Saldo waere eine zweite Wahrheit, die irgendwann von der
    ersten abweicht."""

    def __init__(self):
        self._store = None
        self._posten = []          # Liste von Posten-Objekten (Arbeitskopie)

    # --- Laden / Speichern ------------------------------------------------
    def laden(self, store):
        self._store = store
        roh = store.data.get("posten")
        self._posten = []
        for eintrag in (roh if isinstance(roh, list) else []):
            p = Posten.aus_dict(eintrag)
            if p is not None:
                self._posten.append(p)
        self._migrieren()
        return len(self._posten)

    def _migrieren(self):
        """Alte Kreide-Tafel ('pairs' mit einem Netto-Saldo) uebernehmen.

        Je Paar mit net != 0 entsteht EIN Posten. Nichts wird geloescht: die
        alten Volumen-/Zaehlwerte wandern nach 'archiv', damit das Handelsbuch
        seine Historie behaelt."""
        if self._store is None:
            return
        pairs = self._store.data.get("pairs")
        if not isinstance(pairs, dict) or not pairs:
            return
        uebernommen = 0
        for key, p in list(pairs.items()):
            if not isinstance(p, dict):
                continue
            net = _zahl(p.get("net"))
            if not net:
                continue
            try:
                a, b = (int(x) for x in str(key).split(":"))
            except (TypeError, ValueError):
                continue
            # net = "der ZWEITE schuldet dem ERSTEN".
            glaeubiger, schuldner = (a, b) if net > 0 else (b, a)
            self.anlegen(glaeubiger, schuldner, abs(net),
                         grund="Übernahme alte Kreide-Tafel",
                         entstanden=float(_zahl(p.get("first"))) or time.time())
            uebernommen += 1
        archiv = self._store.data.setdefault("archiv", {})
        if not isinstance(archiv, dict):
            archiv = self._store.data["archiv"] = {}
        archiv["pairs"] = pairs
        self._store.data["pairs"] = {}
        log.info("Schuldbuch: %d Paar-Salden in einzelne Posten uebernommen "
                 "(die alte Tafel liegt unter 'archiv').", uebernommen)

    def schreiben(self):
        """Arbeitskopie zurueck in den Store (vor jedem Speichern)."""
        if self._store is not None:
            self._store.data["posten"] = [p.als_dict() for p in self._posten]

    def score_daten(self):
        if self._store is None:
            return {}
        daten = self._store.data.setdefault("score", {})
        if not isinstance(daten, dict):
            daten = self._store.data["score"] = {}
        return daten

    def stats(self, uid):
        if self._store is None:
            return {}
        alle = self._store.data.setdefault("stats", {})
        if not isinstance(alle, dict):
            alle = self._store.data["stats"] = {}
        eintrag = alle.get(str(int(uid)))
        if not isinstance(eintrag, dict):
            eintrag = alle[str(int(uid))] = {"getilgt": 0, "erhalten": 0,
                                             "mahnung": 0}
        return eintrag

    # --- Anlegen / Aendern ------------------------------------------------
    def _naechste_id(self):
        if self._store is None:
            return len(self._posten) + 1
        nr = max(_zahl(self._store.data.get("next_id"), 1), 1)
        # Auch bei kaputtem Zaehler nie eine ID doppelt vergeben.
        nr = max(nr, max((p.id for p in self._posten), default=0) + 1)
        self._store.data["next_id"] = nr + 1
        return nr

    def anlegen(self, glaeubiger, schuldner, betrag, *, grund="", faellig=0.0,
                entstanden=None):
        p = Posten(
            id=self._naechste_id(),
            glaeubiger=int(glaeubiger), schuldner=int(schuldner),
            urspruenglich=int(betrag), offen=int(betrag),
            grund=str(grund or "")[:100],
            entstanden=float(entstanden if entstanden is not None else time.time()),
            faellig=float(faellig or 0.0))
        p.notiere("entstanden", betrag)
        self._posten.append(p)
        return p

    def posten_von(self, uid):
        """Offene Posten, in denen 'uid' der SCHULDNER ist (aeltester zuerst)."""
        uid = int(uid)
        raus = [p for p in self._posten if p.schuldner == uid and p.ist_offen()]
        raus.sort(key=lambda p: p.entstanden)
        return raus

    def posten_gegen(self, uid):
        """Offene Posten, in denen 'uid' der GLAEUBIGER ist."""
        uid = int(uid)
        raus = [p for p in self._posten if p.glaeubiger == uid and p.ist_offen()]
        raus.sort(key=lambda p: p.entstanden)
        return raus

    def zwischen(self, a, b, *, nur_offen=True):
        a, b = int(a), int(b)
        raus = [p for p in self._posten
                if {p.glaeubiger, p.schuldner} == {a, b}
                and (p.ist_offen() or not nur_offen)]
        raus.sort(key=lambda p: p.entstanden)
        return raus

    def alle(self):
        return list(self._posten)

    def saldo(self, wer, gegen):
        """Stand aus Sicht von 'wer': positiv = 'gegen' schuldet ihm etwas."""
        wer, gegen = int(wer), int(gegen)
        summe = 0
        for p in self._posten:
            if not p.ist_offen():
                continue
            if p.glaeubiger == wer and p.schuldner == gegen:
                summe += p.offen
            elif p.glaeubiger == gegen and p.schuldner == wer:
                summe -= p.offen
        return summe

    def summen(self, uid):
        """(bekommt_insgesamt, schuldet_insgesamt, netto)."""
        haben = sum(p.offen for p in self.posten_gegen(uid))
        soll = sum(p.offen for p in self.posten_von(uid))
        return haben, soll, haben - soll

    def glaeubiger_von(self, uid):
        """[(glaeubiger_id, offene_summe), ...] absteigend."""
        je = {}
        for p in self.posten_von(uid):
            je[p.glaeubiger] = je.get(p.glaeubiger, 0) + p.offen
        return sorted(je.items(), key=lambda kv: kv[1], reverse=True)

    def schuldner_von(self, uid):
        je = {}
        for p in self.posten_gegen(uid):
            je[p.schuldner] = je.get(p.schuldner, 0) + p.offen
        return sorted(je.items(), key=lambda kv: kv[1], reverse=True)

    def soll_je_person(self):
        """{schuldner: offene_summe} in EINEM Durchlauf.

        Die Mahnung brauchte frueher je Nutzer einen eigenen Durchlauf ueber
        alle Paare - das war quadratisch und stand bei 2.000 Paaren fast fuenf
        Sekunden am Stueck."""
        je = {}
        for p in self._posten:
            if p.ist_offen():
                je[p.schuldner] = je.get(p.schuldner, 0) + p.offen
        return je

    def top(self, limit=10):
        """Groesste offene Posten: [(glaeubiger, schuldner, betrag), ...]"""
        offen = [p for p in self._posten if p.ist_offen()]
        offen.sort(key=lambda p: p.offen, reverse=True)
        return [(p.glaeubiger, p.schuldner, p.offen) for p in offen[:max(1, int(limit))]]


class Tilgungsplan:
    """Verteilt EINE Einnahme fair auf alle Glaeubiger.

    Frueher bekam schlicht der groesste Glaeubiger alles - wer bei drei Leuten
    in der Kreide stand, sah bei den anderen beiden monatelang keinen Cent.
    Jetzt: anteilig nach offener Summe, ueberfaellige Posten zuerst, innerhalb
    eines Glaeubigers der aelteste Posten zuerst."""

    def __init__(self, buch):
        self._buch = buch

    def verteilen(self, uid, budget):
        """[(posten, betrag), ...] - was von 'budget' wohin geht.

        Rechnet nur, bucht nichts. Das macht der Aufrufer, damit die
        Buchfuehrung an EINER Stelle steht."""
        budget = int(budget)
        if budget <= 0:
            return []
        offen = self._buch.posten_von(uid)
        if not offen:
            return []
        jetzt = time.time()
        # Ueberfaellige zuerst - erst wenn die weg sind, kommt der Rest dran.
        gruppen = [[p for p in offen if p.ist_ueberfaellig(jetzt)],
                   [p for p in offen if not p.ist_ueberfaellig(jetzt)]]
        plan = []
        for gruppe in gruppen:
            if budget <= 0 or not gruppe:
                continue
            teil, budget = self._auf_glaeubiger(gruppe, budget)
            plan.extend(teil)
        return plan

    def _auf_glaeubiger(self, posten, budget):
        """Verteilt 'budget' anteilig auf die Glaeubiger dieser Posten."""
        je_glaeubiger = {}
        for p in posten:
            je_glaeubiger.setdefault(p.glaeubiger, []).append(p)
        gesamt = sum(p.offen for p in posten)
        if gesamt <= 0:
            return [], budget
        verteilbar = min(budget, gesamt)
        anteile = {}
        for g, liste in je_glaeubiger.items():
            summe = sum(p.offen for p in liste)
            anteile[g] = int(verteilbar * summe / gesamt)
        # Rundungsrest an den groessten Glaeubiger - sonst bleiben durch das
        # Abrunden ein paar Coins liegen, und die Schuld wird nie ganz null.
        rest = verteilbar - sum(anteile.values())
        if rest > 0 and anteile:
            groesster = max(anteile, key=lambda g: sum(p.offen for p in je_glaeubiger[g]))
            anteile[groesster] += rest
        # Kleinstbetraege sammeln und dem groessten geben: eine Ueberweisung
        # ueber 3 Coins ist fuer beide Seiten nur Rauschen.
        krumen = 0
        for g in list(anteile):
            if 0 < anteile[g] < TILGUNG_MIN_JE_GLAEUBIGER:
                krumen += anteile[g]
                anteile[g] = 0
        if krumen:
            kandidaten = [g for g, betrag in anteile.items() if betrag > 0]
            if kandidaten:
                anteile[max(kandidaten, key=lambda g: anteile[g])] += krumen
            elif anteile:
                # Alle waren zu klein - dann bekommt der groesste Glaeubiger
                # alles auf einmal, statt dass gar nichts passiert.
                groesster = max(anteile, key=lambda g: sum(p.offen for p in je_glaeubiger[g]))
                anteile[groesster] = min(krumen, sum(p.offen for p in je_glaeubiger[groesster]))
        plan = []
        gezahlt = 0
        for g, betrag in anteile.items():
            if betrag <= 0:
                continue
            # Innerhalb eines Glaeubigers: aeltester Posten zuerst (FIFO).
            for p in sorted(je_glaeubiger[g], key=lambda x: x.entstanden):
                if betrag <= 0:
                    break
                nimm = min(betrag, p.offen)
                if nimm <= 0:
                    continue
                plan.append((p, nimm))
                betrag -= nimm
                gezahlt += nimm
        return plan, budget - gezahlt


class Schulden:
    """Das Feature: Befehle, Anzeige und die Bruecke zu economy/bot."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._bot_name = "Flo"
        self.buch = Schuldbuch()
        self.score = Kreditwuerdigkeit(self.buch)
        self.plan = Tilgungsplan(self.buch)
        # Riegel gegen Rekursion: die Tilgung bucht selbst Coins um, und das ruft
        # add_coins - was ohne Riegel wieder die Tilgung anstossen wuerde.
        self._tilgung_laeuft = False
        self._save_tasks = set()
        self._save_task = None     # laufender Sammel-Speicherer
        self._dirty = False

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("SCHULDEN_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Schuldbuch aus (SCHULDEN_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Schuldbuch aus (economy ist aus - ohne Coins nichts zu buchen).")
            return False
        self._store = JsonStore("schulden.json", default={
            "posten": [], "next_id": 1, "score": {}, "stats": {}, "archiv": {},
            # Bleibt fuer die Migration alter Staende stehen.
            "pairs": {},
        })
        anzahl = self.buch.laden(self._store)
        self._enabled = True
        offen = sum(1 for p in self.buch.alle() if p.ist_offen())
        log.info("Schuldbuch aktiv (%d Posten, davon %d offen).", anzahl, offen)
        return True

    def is_enabled(self):
        return self._enabled

    async def _save(self):
        if self._store is not None:
            self.buch.schreiben()
            await self._store.save()

    def _save_soon(self):
        """Speichern SAMMELN statt bei jeder Buchung neu zu schreiben.

        Vorher startete jede einzelne Coin-Bewegung einen eigenen save()-Task, und
        store.save serialisiert das ganze Buch SYNCHRON im Event-Loop. Bei einer
        Casino-Runde mit vielen Buchungen waren das dutzende volle
        Serialisierungen pro Sekunde - der Bot ruckelt dann fuer alle."""
        self._dirty = True
        if self._save_task is not None and not self._save_task.done():
            return
        try:
            self._save_task = asyncio.get_running_loop().create_task(self._save_loop())
        except RuntimeError:
            self._save_task = None      # kein Loop (Tests) - beim naechsten Mal
            return
        self._save_tasks.add(self._save_task)
        self._save_task.add_done_callback(self._save_tasks.discard)

    async def _save_loop(self):
        """Schreibt, solange zwischendurch neue Aenderungen aufgelaufen sind."""
        try:
            while self._dirty:
                self._dirty = False
                await asyncio.sleep(SAVE_DEBOUNCE)
                await self._save()
        except Exception:  # noqa: BLE001 - Speichern darf nie ein Spiel sprengen
            log.exception("Verzoegertes Speichern fehlgeschlagen")

    # --- Auskunft fuer andere Module --------------------------------------
    def saldo(self, wer, gegen):
        return self.buch.saldo(wer, gegen) if self._enabled else 0

    def summen(self, uid):
        return self.buch.summen(uid) if self._enabled else (0, 0, 0)

    def posten(self, uid):
        """(forderungen, schulden) je [(andere_id, betrag), ...], absteigend.

        Alte Signatur, damit Panel und Profil-Lookup unveraendert bleiben."""
        if not self._enabled:
            return [], []
        return self.buch.schuldner_von(uid), self.buch.glaeubiger_von(uid)

    def top(self, limit=10):
        return self.buch.top(limit) if self._enabled else []

    def getilgt_summe(self, uid):
        """Wie viel bei diesem Nutzer schon automatisch abgezogen wurde."""
        if not self._enabled:
            return 0
        return _zahl(self.buch.stats(uid).get("getilgt"))

    def kredit_score(self, uid):
        """Fuer den Profil-Lookup: (Score, Ampel-Emoji)."""
        if not self._enabled:
            return None, ""
        return self.score.score(uid), self.score.ampel(uid)

    # --- Zahlungen mitschreiben (OHNE Forderung!) -------------------------
    def pay_block(self, von, an, betrag, *, ziel_name=None, guild=None):
        """Was 'Flo pay' unter die Bestaetigung haengt.

        WICHTIG: eine Zahlung erzeugt hier KEINE Forderung mehr. Sie kann aber
        eine bestehende Schuld tilgen - wer seinem Glaeubiger etwas
        ueberweist, meint fast immer genau das. Der Rest ist ein Geschenk."""
        leer = {"stand": "", "warnung": "", "soll": 0, "saldo": 0,
                "farbe": FARBE_SAUBER, "offen_bei_anderen": 0}
        if not self._enabled:
            return leer
        try:
            von, an, betrag = int(von), int(an), int(betrag)
        except (TypeError, ValueError):
            return leer
        if von == an or betrag <= 0:
            return leer
        name = ziel_name or self._name(an, guild)
        # Zahlung auf eigene Schulden bei genau dieser Person anrechnen.
        angerechnet = self._anrechnen(von, an, betrag, art="zahlung")
        offen_danach = self.buch.saldo(an, von)
        if angerechnet and offen_danach > 0:
            stand = (f"Angerechnet: **{fmt(angerechnet)}** {economy.COIN} gehen auf "
                     f"deine Schuld bei **{name}** – offen bleiben "
                     f"**{fmt(offen_danach)}**.")
        elif angerechnet:
            stand = (f"Damit ist deine Schuld bei **{name}** **beglichen**. ✨"
                     + (f" (**{fmt(betrag - angerechnet)}** {economy.COIN} davon "
                        f"waren geschenkt.)" if betrag > angerechnet else ""))
        else:
            stand = (f"Ein Geschenk an **{name}** – daraus wird keine Schuld. "
                     f"Wenn du **verleihen** willst: "
                     f"`{self._bot_name} leih @{name} {fmt(betrag)}`.")
        eigene = self.buch.glaeubiger_von(von)
        andere = [(u, b) for u, b in eigene if int(u) != an]
        rest = sum(b for _u, b in andere)
        warnung = ""
        if rest > 0:
            liste = "\n".join(f"• {self._name(u, guild)} — **{fmt(b)}** {economy.COIN}"
                              for u, b in andere[:5])
            mehr = f"\n… und {len(andere) - 5} weitere" if len(andere) > 5 else ""
            warnung = (f"{liste}{mehr}\n_{int(TILGUNG_PCT * 100)} % deiner Einnahmen "
                       f"(Daily, Casino, Spiele, Gewinne) gehen automatisch dorthin – "
                       f"schneller gehts mit `{self._bot_name} tilg @wer`._")
        soll = sum(b for _u, b in eigene)
        farbe = FARBE_SCHULDEN if soll > 0 else FARBE_SAUBER
        return {"stand": stand, "warnung": warnung, "soll": soll,
                "saldo": -offen_danach, "farbe": farbe, "offen_bei_anderen": rest}

    def _anrechnen(self, zahler, empfaenger, betrag, *, art="zahlung"):
        """Rechnet 'betrag' auf offene Posten des Zahlers bei diesem Empfaenger
        an (aeltester zuerst). Rueckgabe: was wirklich angerechnet wurde.

        Bewegt KEINE Coins - die sind an der Aufrufstelle schon geflossen."""
        rest = int(betrag)
        angerechnet = 0
        for p in self.buch.zwischen(zahler, empfaenger):
            if rest <= 0:
                break
            if p.glaeubiger != int(empfaenger):
                continue          # das ist eine Forderung IN DIE ANDERE Richtung
            nimm = min(rest, p.offen)
            if nimm <= 0:
                continue
            self._abbuchen(p, nimm, art=art)
            rest -= nimm
            angerechnet += nimm
        if angerechnet and art == "sondertilgung":
            self.score.notiere(zahler, Kreditwuerdigkeit.SONDERTILGUNG,
                               "freiwillig getilgt")
        return angerechnet

    def _abbuchen(self, posten, betrag, *, art="zahlung"):
        """Verringert EINEN Posten und schliesst ihn ggf. ab."""
        betrag = min(int(betrag), posten.offen)
        if betrag <= 0:
            return 0
        posten.offen -= betrag
        posten.notiere(art, betrag)
        if posten.offen <= 0:
            posten.status = "getilgt"
            self._score_bei_abschluss(posten)
        return betrag

    def _score_bei_abschluss(self, posten):
        """Puenktlich zurueckgezahlt? Dann gibt es Punkte."""
        jetzt = time.time()
        puenktlich = (
            (posten.faellig and jetzt <= posten.faellig)
            or (not posten.faellig
                and jetzt - posten.entstanden <= Kreditwuerdigkeit.SCHNELL_TAGE * TAG))
        if puenktlich:
            self.score.notiere(posten.schuldner, Kreditwuerdigkeit.PUENKTLICH,
                               "pünktlich getilgt")

    async def note_pay_block(self, von, an, betrag, *, ziel_name=None, guild=None):
        """pay_block + speichern. Wirft nie."""
        try:
            block = self.pay_block(von, an, betrag, ziel_name=ziel_name, guild=guild)
        except Exception:  # noqa: BLE001
            log.exception("Schuldbuch-Notiz fehlgeschlagen")
            return {"stand": "", "warnung": "", "soll": 0, "saldo": 0,
                    "farbe": FARBE_SAUBER, "offen_bei_anderen": 0}
        try:
            await self._save()
        except Exception:  # noqa: BLE001
            log.exception("Schuldbuch konnte nicht gespeichert werden")
        return block

    async def note_pay(self, von, an, betrag, *, ziel_name=None, guild=None):
        """Text-Variante fuer Aufrufer ohne Embed. Wirft NIE."""
        block = await self.note_pay_block(von, an, betrag, ziel_name=ziel_name,
                                          guild=guild)
        return ("\n📒 " + block["stand"]) if block.get("stand") else ""

    # --- Automatische Tilgung --------------------------------------------
    def tilgen_von_einnahme(self, uid, einnahme, reason=""):
        """Zieht von einer Einnahme einen Teil ab und verteilt ihn auf die
        Glaeubiger. Rueckgabe: (betrag, glaeubiger_id) - die ID ist der
        groesste Empfaenger, damit der alte Aufrufer unveraendert bleibt.

        DAS ist der Zwang zum Zahlen: wer Schulden hat, bekommt von jeder
        Einnahme nur einen Teil - der Rest geht automatisch an die, denen er
        etwas schuldet. Es wird nichts erschaffen und nichts vernichtet."""
        if not self._enabled or self._tilgung_laeuft:
            return 0, 0
        if TILGUNG_PCT <= 0:
            return 0, 0
        grund = str(reason or "").lower()
        # Jede RUECKBUCHUNG ist tabu, nicht nur die namentlich gelisteten: die
        # Spiele buchen ihre Einsatz-Rueckgabe schlicht als "spiele", und wer
        # Schulden hatte, bekam bei "Einsatz zurueck" nur 80 % wieder.
        if grund in TILGUNG_TABU or grund.endswith(("-rueck", "-rueckgabe")):
            return 0, 0
        einnahme = _zahl(einnahme)
        if einnahme < TILGUNG_MIN_EINNAHME:
            return 0, 0
        habe = max(0, economy.get_coins(uid))
        budget = min(int(einnahme * TILGUNG_PCT), habe)
        plan = self.plan.verteilen(uid, budget)
        if not plan:
            return 0, 0
        self._tilgung_laeuft = True
        gezahlt_je = {}
        try:
            for posten, betrag in plan:
                echt = self._abbuchen(posten, betrag, art="tilgung")
                if echt <= 0:
                    continue
                economy.add_coins(uid, -echt, reason="schulden-tilgung")
                economy.add_coins(posten.glaeubiger, echt, reason="schulden-tilgung")
                gezahlt_je[posten.glaeubiger] = gezahlt_je.get(posten.glaeubiger, 0) + echt
        finally:
            self._tilgung_laeuft = False
        gesamt = sum(gezahlt_je.values())
        if not gesamt:
            return 0, 0
        groesster = max(gezahlt_je, key=lambda g: gezahlt_je[g])
        self.buch.stats(uid)["getilgt"] = self.getilgt_summe(uid) + gesamt
        for g, betrag in gezahlt_je.items():
            st = self.buch.stats(g)
            st["erhalten"] = _zahl(st.get("erhalten")) + betrag
        log.info("Tilgung: %s zahlt %d Coins an %d Glaeubiger (Einnahme %d aus '%s').",
                 uid, gesamt, len(gezahlt_je), einnahme, reason)
        self._save_soon()
        return gesamt, groesster

    # --- Verfall & Mahnwesen ----------------------------------------------
    def verfall_pruefen(self):
        """Posten ohne jede Bewegung verfallen - kein ewiges Druckmittel.

        Rueckgabe: (verfallen, [(posten, tage_bis_verfall), ...]) - die zweite
        Liste sind die, bei denen der Glaeubiger noch gewarnt werden soll."""
        if not self._enabled:
            return [], []
        jetzt = time.time()
        verfallen, warnen = [], []
        for p in list(self.buch.alle()):
            if not p.ist_offen():
                continue
            ruhe_tage = (jetzt - p.bewegt) / TAG
            if ruhe_tage >= VERFALL_TAGE:
                p.status = "verfallen"
                p.notiere("verfallen", p.offen)
                self.score.notiere(p.schuldner, Kreditwuerdigkeit.AUSGEFALLEN,
                                   "Posten verfallen")
                verfallen.append(p)
            elif ruhe_tage >= VERFALL_TAGE - VERFALL_WARNUNG_TAGE:
                warnen.append((p, int(VERFALL_TAGE - ruhe_tage)))
        if verfallen:
            self._save_soon()
        return verfallen, warnen

    def faellige_stufen(self):
        """[(schuldner, stufe, posten)] - wer welche Mahnstufe erreicht hat.

        Eskalation statt Dauer-DM: 1 = freundlich an den Schuldner,
        2 = an beide mit Tilgungsvorschlag, 3 = auf Wunsch des Servers eine
        neutrale Notiz im Ansage-Kanal."""
        jetzt = time.time()
        raus = []
        for p in self.buch.alle():
            if not p.ist_ueberfaellig(jetzt):
                continue
            tage = (jetzt - p.faellig) / TAG
            stufe = 3 if tage >= MAHN_STUFE_3 else (2 if tage >= MAHN_STUFE_2 else 1)
            if stufe <= p.mahnstufe:
                continue
            if jetzt - p.letzte_mahnung < MAHN_ABSTAND:
                continue
            raus.append((p.schuldner, stufe, p))
        return raus

    async def mahn_tick(self, client):
        """Von bot.py periodisch: erinnert an offene und ueberfaellige Posten."""
        if not self._enabled or client is None:
            return 0
        jetzt = time.time()
        gemahnt = 0
        # 1) Verfall zuerst - was verfaellt, muss nicht mehr gemahnt werden.
        verfallen, warnen = self.verfall_pruefen()
        for p in verfallen:
            await self._dm(client, p.glaeubiger, self._verfall_embed(p))
        for p, tage in warnen:
            if jetzt - p.letzte_mahnung < MAHN_ABSTAND:
                continue
            p.letzte_mahnung = jetzt
            await self._dm(client, p.glaeubiger, self._verfall_warnung_embed(p, tage))

        # 2) Ueberfaellige Posten eskalieren.
        for schuldner, stufe, p in self.faellige_stufen():
            p.mahnstufe = stufe
            p.letzte_mahnung = jetzt
            if not p.ueberfaellig_gebucht:
                p.ueberfaellig_gebucht = True
                self.score.notiere(schuldner, Kreditwuerdigkeit.UEBERFAELLIG,
                                   "Posten überfällig")
            if await self._dm(client, schuldner, self._mahn_embed_posten(p, stufe)):
                gemahnt += 1
            if stufe >= 2:
                await self._dm(client, p.glaeubiger, self._mahn_embed_glaeubiger(p))
            if stufe >= 3:
                await self._pranger(client, p)

        # 3) Der allgemeine Erinnerer fuer alles ohne Termin.
        for uid, soll in self.buch.soll_je_person().items():
            if soll < MAHN_AB:
                continue
            st = self.buch.stats(uid)
            if jetzt - float(_zahl(st.get("mahnung"))) < MAHN_ABSTAND:
                continue
            st["mahnung"] = jetzt
            if await self._dm(client, uid, self._mahn_embed(uid, soll)):
                gemahnt += 1
        if gemahnt or verfallen:
            self._save_soon()
        return gemahnt

    async def _dm(self, client, uid, embed):
        """Schickt eine DM. Wirft nie - DMs koennen einfach zu sein."""
        try:
            user = client.get_user(int(uid))
            if user is None and hasattr(client, "fetch_user"):
                user = await client.fetch_user(int(uid))
            if user is None:
                return False
            await user.send(embed=embed)
            return True
        except Exception:  # noqa: BLE001
            log.debug("DM an %s nicht moeglich", uid, exc_info=True)
            return False

    async def _pranger(self, client, posten):
        """Stufe 3: eine NEUTRALE Notiz im Ansage-Kanal - aber nur, wenn der
        Server das ausdruecklich eingeschaltet hat. Nie mit Betrag."""
        try:
            import guildcfg
        except Exception:  # noqa: BLE001
            return
        for guild in getattr(client, "guilds", []) or []:
            try:
                if not guildcfg.an(guild.id, "schulden_pranger"):
                    continue
                mitglieder = (guild.get_member(posten.schuldner),
                              guild.get_member(posten.glaeubiger))
                if not all(mitglieder):
                    continue
                kanal = guild.get_channel(guildcfg.get(guild.id, "ansage_channel"))
                if kanal is None:
                    continue
                await kanal.send(
                    f"🧾 <@{posten.schuldner}> hat einen **überfälligen Posten** "
                    f"bei <@{posten.glaeubiger}>.")
            except Exception:  # noqa: BLE001
                log.debug("Pranger-Notiz nicht moeglich", exc_info=True)

    # --- Namen ------------------------------------------------------------
    def _name(self, uid, guild=None):
        """Anzeigename OHNE Ping (fuer Nachrichten-Text)."""
        n = ""
        try:
            n = economy.display_name_of(uid) or ""
        except Exception:  # noqa: BLE001
            n = ""
        if not n and guild is not None:
            m = guild.get_member(int(uid)) if hasattr(guild, "get_member") else None
            n = getattr(m, "display_name", "") or ""
        return n or f"ID {uid}"

    # --- Befehle ----------------------------------------------------------
    async def handle(self, message):
        """Alle Schuldbuch-Befehle. Rueckgabe: None | Text | Embed | HANDLED."""
        if not self._enabled or message.guild is None:
            return None
        roh = ai.strip_lead(message.content or "")
        teile = roh.split()
        if not teile:
            return None
        erstes = teile[0].lower().strip(".,!?")
        rest = " ".join(teile[1:]).strip()

        if erstes in _CMD_LEIH:
            return await self._cmd_leih(message, rest)
        if erstes in _CMD_SCHEIN:
            return await self._cmd_schuldschein(message, rest)
        if erstes in _CMD_TILG:
            return await self._cmd_tilgen(message, rest)
        if erstes in _CMD_INSOLVENZ:
            return await self._cmd_insolvenz(message)
        if erstes not in _CMDS:
            return None

        low = rest.lower()
        if low.startswith(("hilfe", "help", "?", "was ")):
            return self._hilfe_embed()
        if low.startswith(("top", "liste", "ranking", "meiste", "größte", "groesste")):
            return self._top_embed(message.guild)
        if low.startswith(("erlass", "streich", "vergeb", "schenk", "verzicht")):
            return await self._erlassen(message)
        ziel = _echtes_ziel(message)
        if ziel is not None:
            if ziel.id == message.author.id:
                return "Bei dir selbst hast du nichts offen. 😄"
            return self._paar_embed(message.author, ziel)
        return self._eigene_embed(message.author, message.guild)

    # --- Befehl: leihen ----------------------------------------------------
    def _lies_betrag(self, message, rest):
        """Betrag aus dem Rest-Text (auch '5k', '2,5 mio')."""
        ohne_ping = re.sub(r"<@!?\d+>", " ", rest or "")
        for token in ohne_ping.split():
            wert = economy.parse_amount(token)
            if wert:
                return wert
        return None

    @staticmethod
    def _lies_frist(text):
        """'bis freitag' / 'bis in 7 tagen' -> Zeitstempel (0 = ohne Termin)."""
        m = _FRIST_RE.search(text or "")
        if m:
            n = max(1, _zahl(m.group(1), 1))
            einheit = m.group(2).lower()
            faktor = TAG if einheit.startswith("tag") else (
                7 * TAG if einheit.startswith("woche") else 30 * TAG)
            return time.time() + n * faktor
        m = _FRIST_TAG_RE.search(text or "")
        if m:
            ziel = _WOCHENTAGE[m.group(1).lower()]
            heute = time.localtime().tm_wday
            tage = (ziel - heute) % 7 or 7
            return time.time() + tage * TAG
        return 0.0

    @staticmethod
    def _lies_grund(rest):
        """Alles ohne Ping, Betrag und Frist ist der Grund."""
        text = re.sub(r"<@!?\d+>", " ", rest or "")
        text = _FRIST_RE.sub(" ", text)
        text = _FRIST_TAG_RE.sub(" ", text)
        # Das Befehlswort selbst ist nie ein Grund - 'pay ... als leihgabe'
        # reicht den ganzen Rest herein, inklusive des 'pay'.
        woerter = [w for w in text.split()
                   if economy.parse_amount(w) is None
                   and w.lower() not in ("fuer", "für", "wegen", "als", "leihgabe",
                                         "kredit", "darlehen", "leihweise", "an",
                                         "coins", "bitte", "mal", "pay", "zahl",
                                         "zahle", "überweis", "überweise",
                                         "ueberweis", "ueberweise", "leih",
                                         "leihe", "schuldschein", "geliehen",
                                         "pump", "auf")]
        return " ".join(woerter).strip(" ,.-")[:100]

    def _darf_anlegen(self, glaeubiger, schuldner, betrag):
        """(ok, Fehlertext). Alle Grenzen an EINER Stelle."""
        if int(glaeubiger) == int(schuldner):
            return False, "Bei dir selbst geht das nicht. 😄"
        if betrag < MIN_POSTEN:
            return False, (f"Unter **{fmt(MIN_POSTEN)}** {economy.COIN} lohnt die "
                           f"Buchführung nicht – das ist ein Trinkgeld, kein Kredit.")
        gesperrt, warum = self.score.gesperrt(schuldner)
        if gesperrt:
            return False, f"Geht gerade nicht: {warum}"
        offen_paar = [p for p in self.buch.zwischen(glaeubiger, schuldner)
                      if p.glaeubiger == int(glaeubiger)]
        if len(offen_paar) >= MAX_POSTEN_JE_PAAR:
            return False, (f"Zwischen euch stehen schon **{MAX_POSTEN_JE_PAAR}** offene "
                           f"Posten. Erst tilgen, dann neu leihen.")
        if len(self.buch.posten_von(schuldner)) >= MAX_POSTEN_JE_PERSON:
            return False, (f"Diese Person hat schon **{MAX_POSTEN_JE_PERSON}** offene "
                           f"Posten – mehr wird niemand mehr los.")
        _h, soll, _n = self.buch.summen(schuldner)
        vermoegen = max(0, economy.get_coins(schuldner))
        deckel = int(max(vermoegen, MIN_POSTEN * 10) * MAX_SCHULD_FAKTOR)
        if soll + betrag > deckel:
            return False, (f"Das sprengt die Grenze: mehr als das "
                           f"**{int(MAX_SCHULD_FAKTOR)}-fache** des eigenen Vermögens "
                           f"darf niemand schulden (Deckel gerade "
                           f"**{fmt(deckel)}** {economy.COIN}, offen schon "
                           f"**{fmt(soll)}**).")
        limit = self.score.leih_limit(glaeubiger, schuldner)
        if betrag > limit:
            return False, (f"So viel würde ich dir nicht leihen lassen. Bei einer "
                           f"Kreditwürdigkeit von **{self.score.score(schuldner)}** "
                           f"{self.score.ampel(schuldner)} sind höchstens "
                           f"**{fmt(limit)}** {economy.COIN} drin.")
        return True, ""

    async def _cmd_leih(self, message, rest):
        """'Flo leih @wer 5k [Grund] [bis freitag]' - Kredit ANBIETEN."""
        ziel = _echtes_ziel(message)
        if ziel is None:
            return (f"Wem willst du etwas leihen? "
                    f"`{self._bot_name} leih @jemand 5k [Grund] [bis freitag]`")
        betrag = self._lies_betrag(message, rest)
        if betrag is None:
            return (f"Wie viel denn? "
                    f"`{self._bot_name} leih @{ziel.display_name} 5k`")
        if economy.get_coins(message.author.id) < betrag:
            return (f"Du hast nicht genug. Kontostand: "
                    f"**{fmt(economy.get_coins(message.author.id))}** {economy.COIN}.")
        ok, fehler = self._darf_anlegen(message.author.id, ziel.id, betrag)
        if not ok:
            return fehler
        return await self._anfrage_stellen(
            message, ziel, betrag, grund=self._lies_grund(rest),
            faellig=self._lies_frist(rest), mit_geld=True)

    async def _cmd_schuldschein(self, message, rest):
        """'Flo schuldschein @wer 5k [Grund]' - Schuld OHNE Geldfluss."""
        ziel = _echtes_ziel(message)
        if ziel is None:
            return (f"Wer schuldet dir was? "
                    f"`{self._bot_name} schuldschein @jemand 5k [Grund]`")
        betrag = self._lies_betrag(message, rest)
        if betrag is None:
            return f"Wie viel denn? `{self._bot_name} schuldschein @{ziel.display_name} 5k`"
        ok, fehler = self._darf_anlegen(message.author.id, ziel.id, betrag)
        if not ok:
            return fehler
        return await self._anfrage_stellen(
            message, ziel, betrag, grund=self._lies_grund(rest),
            faellig=self._lies_frist(rest), mit_geld=False)

    async def _anfrage_stellen(self, message, ziel, betrag, *, grund, faellig,
                               mit_geld):
        """Postet die Anfrage mit den zwei Knoepfen.

        Bestaetigen muss IMMER der, dem daraus eine Schuld entsteht - also der
        Empfaenger beim Kredit und der Schuldner beim Schuldschein. Erst der
        Klick bewegt Geld und legt den Posten an; ohne Klick passiert nichts."""
        if getattr(ziel, "bot", False):
            return "Bots leihen sich nichts. 🤖"
        view = _AnfrageView(self, message.author, ziel, betrag, grund=grund,
                            faellig=faellig, mit_geld=mit_geld)
        emb = discord.Embed(
            title="🤝 Leih-Angebot" if mit_geld else "🧾 Schuldschein",
            description=(
                (f"**{message.author.display_name}** bietet **{ziel.display_name}** "
                 f"**{fmt(betrag)}** {economy.COIN} als **Leihgabe** an."
                 if mit_geld else
                 f"**{message.author.display_name}** sagt, **{ziel.display_name}** "
                 f"schuldet ihm noch **{fmt(betrag)}** {economy.COIN}.")
                + f"\n\n<@{ziel.id}> – nur du kannst das bestätigen."),
            color=FARBE)
        if grund:
            emb.add_field(name="Wofür", value=grund, inline=True)
        if faellig:
            emb.add_field(name="Fällig", value=f"<t:{int(faellig)}:D>", inline=True)
        emb.add_field(
            name="Kreditwürdigkeit",
            value=f"{self.score.ampel(ziel.id)} **{self.score.score(ziel.id)}**/100",
            inline=True)
        emb.set_footer(text=("Ohne Bestätigung passiert nichts. Das Angebot läuft "
                             f"nach {ANFRAGE_TIMEOUT // 60} Minuten ab."))
        try:
            view.message = await message.reply(embed=emb, view=view,
                                               mention_author=False)
        except discord.HTTPException:
            log.exception("Anfrage konnte nicht gesendet werden")
            return "Das Angebot ließ sich gerade nicht posten – versuch's nochmal."
        return HANDLED

    async def annehmen(self, besteller, ziel, betrag, *, grund, faellig, mit_geld):
        """Der Klick auf 'Annehmen'. (ok, Text)."""
        glaeubiger = besteller.id
        schuldner = ziel.id
        ok, fehler = self._darf_anlegen(glaeubiger, schuldner, betrag)
        if not ok:
            return False, fehler
        if mit_geld:
            # Erst JETZT fliesst Geld - vorher stand nur ein Angebot im Raum.
            if economy.get_coins(glaeubiger) < betrag:
                return False, (f"**{besteller.display_name}** hat inzwischen nicht "
                               f"mehr genug auf dem Konto.")
            economy.add_coins(glaeubiger, -betrag, reason="pay")
            economy.add_coins(schuldner, betrag, reason="pay")
            await economy.flush()
        posten = self.buch.anlegen(glaeubiger, schuldner, betrag, grund=grund,
                                   faellig=faellig)
        await self._save()
        log.info("Neuer Posten #%d: %s schuldet %s %d Coins (%s).",
                 posten.id, schuldner, glaeubiger, betrag, grund or "ohne Grund")
        termin = f" – fällig <t:{int(faellig)}:D>" if faellig else ""
        wie = "geliehen" if mit_geld else "als Schuldschein notiert"
        return True, (f"🤝 **{fmt(betrag)}** {economy.COIN} {wie}{termin}.\n"
                      f"<@{schuldner}> steht damit bei <@{glaeubiger}> in der Kreide "
                      f"(Posten **#{posten.id}**).")

    # --- Befehl: freiwillig tilgen ----------------------------------------
    async def _cmd_tilgen(self, message, rest):
        """'Flo tilg @wer [betrag]' - Sondertilgung ohne Umweg ueber pay."""
        ziel = _echtes_ziel(message)
        if ziel is None:
            return (f"Bei wem willst du tilgen? "
                    f"`{self._bot_name} tilg @jemand [betrag]`")
        offen = self.buch.saldo(ziel.id, message.author.id)
        if offen <= 0:
            return f"Bei **{ziel.display_name}** hast du nichts offen. ✨"
        betrag = self._lies_betrag(message, rest) or offen
        betrag = min(betrag, offen, max(0, economy.get_coins(message.author.id)))
        if betrag <= 0:
            return (f"Dein Konto ist leer – gerade geht keine Tilgung. "
                    f"Offen sind **{fmt(offen)}** {economy.COIN}.")
        economy.add_coins(message.author.id, -betrag, reason="schulden-tilgung")
        economy.add_coins(ziel.id, betrag, reason="schulden-tilgung")
        await economy.flush()
        self._anrechnen(message.author.id, ziel.id, betrag, art="sondertilgung")
        st = self.buch.stats(message.author.id)
        st["getilgt"] = _zahl(st.get("getilgt")) + betrag
        await self._save()
        rest_offen = self.buch.saldo(ziel.id, message.author.id)
        if rest_offen > 0:
            return (f"✅ **{fmt(betrag)}** {economy.COIN} getilgt – bei "
                    f"**{ziel.display_name}** bleiben **{fmt(rest_offen)}** offen.")
        return (f"✅ **{fmt(betrag)}** {economy.COIN} getilgt. Bei "
                f"**{ziel.display_name}** bist du wieder blitzsauber. ✨")

    # --- Befehl: Privatinsolvenz ------------------------------------------
    async def _cmd_insolvenz(self, message):
        _h, soll, _n = self.buch.summen(message.author.id)
        if soll <= 0:
            return "Du hast gar keine Schulden. Genieß es. ✨"
        vermoegen = max(0, economy.get_coins(message.author.id))
        zahlung = int(vermoegen * INSOLVENZ_QUOTE)
        emb = discord.Embed(
            title="⚖️ Privatinsolvenz?",
            description=(
                f"Offen sind **{fmt(soll)}** {economy.COIN}.\n\n"
                f"Bei einer Insolvenz gehen **{fmt(zahlung)}** {economy.COIN} "
                f"(50 % deines Vermögens) anteilig an deine Gläubiger, der Rest "
                f"wird **erlassen**.\n"
                f"Dein Score fällt auf **{Kreditwuerdigkeit.INSOLVENZ_SCORE}**, und "
                f"**{INSOLVENZ_SPERRE_TAGE} Tage** lang gibt es keine neuen "
                f"Schulden.\n\n_Ein ehrlicher Neuanfang – aber er kostet._"),
            color=FARBE_SCHULDEN)
        view = _InsolvenzView(self, message.author)
        try:
            view.message = await message.reply(embed=emb, view=view,
                                               mention_author=False)
        except discord.HTTPException:
            return "Die Rückfrage ließ sich nicht posten – versuch's nochmal."
        return HANDLED

    async def insolvenz_durchfuehren(self, uid):
        """Zahlt anteilig aus, erlaesst den Rest, setzt Score und Sperre."""
        offene = self.buch.posten_von(uid)
        soll = sum(p.offen for p in offene)
        if soll <= 0:
            return "Du hast gar keine Schulden mehr. ✨"
        vermoegen = max(0, economy.get_coins(uid))
        budget = int(vermoegen * INSOLVENZ_QUOTE)
        gezahlt = 0
        self._tilgung_laeuft = True
        try:
            for posten, betrag in self.plan.verteilen(uid, budget):
                echt = self._abbuchen(posten, betrag, art="insolvenz")
                if echt <= 0:
                    continue
                economy.add_coins(uid, -echt, reason="schulden-tilgung")
                economy.add_coins(posten.glaeubiger, echt, reason="schulden-tilgung")
                gezahlt += echt
            for posten in self.buch.posten_von(uid):
                posten.notiere("insolvenz-erlass", posten.offen)
                posten.offen = 0
                posten.status = "erlassen"
        finally:
            self._tilgung_laeuft = False
        # Score hart auf den Insolvenz-Wert setzen: die Historie bleibt stehen,
        # aber der Neuanfang beginnt sichtbar unten.
        self.buch.score_daten()[str(int(uid))] = [{
            "t": time.time(),
            "d": Kreditwuerdigkeit.INSOLVENZ_SCORE - Kreditwuerdigkeit.START,
            "g": "Privatinsolvenz"}]
        self.buch.stats(uid)["insolvenz_bis"] = time.time() + INSOLVENZ_SPERRE_TAGE * TAG
        await economy.flush()
        await self._save()
        log.info("Privatinsolvenz %s: %d von %d Coins verteilt, Rest erlassen.",
                 uid, gezahlt, soll)
        return (f"⚖️ **Privatinsolvenz durchgeführt.**\n"
                f"**{fmt(gezahlt)}** {economy.COIN} sind anteilig an deine Gläubiger "
                f"gegangen, **{fmt(soll - gezahlt)}** wurden erlassen.\n"
                f"Kreditwürdigkeit: **{Kreditwuerdigkeit.INSOLVENZ_SCORE}**/100 · "
                f"{INSOLVENZ_SPERRE_TAGE} Tage keine neuen Schulden.")

    # --- Befehl: erlassen -------------------------------------------------
    def erlassen(self, glaeubiger, schuldner, betrag=None):
        """Streicht (einen Teil) dessen, was 'schuldner' bei 'glaeubiger' offen
        hat. Rueckgabe: (erlassen, rest). Nur der Glaeubiger darf das."""
        if not self._enabled:
            return 0, 0
        offene = [p for p in self.buch.zwischen(glaeubiger, schuldner)
                  if p.glaeubiger == int(glaeubiger)]
        gesamt = sum(p.offen for p in offene)
        if gesamt <= 0:
            return 0, 0
        weg = gesamt if betrag is None else max(0, min(int(betrag), gesamt))
        if weg <= 0:
            return 0, gesamt
        rest = weg
        for p in offene:                      # aeltester zuerst
            if rest <= 0:
                break
            nimm = min(rest, p.offen)
            p.offen -= nimm
            p.notiere("erlassen", nimm)
            rest -= nimm
            if p.offen <= 0:
                p.status = "erlassen"
        return weg, gesamt - weg

    async def _erlassen(self, message):
        ziel = _echtes_ziel(message)
        if ziel is None:
            return (f"Wem willst du was erlassen? "
                    f"`{self._bot_name} schulden erlassen @jemand [betrag]`")
        if ziel.id == message.author.id:
            return "Dir selbst etwas erlassen ist... kreativ. 😄"
        offen = self.buch.saldo(message.author.id, ziel.id)
        if offen < 0:
            return (f"Erlassen kann nur, wem etwas zusteht – du hast bei "
                    f"**{ziel.display_name}** selbst noch **{fmt(-offen)}** "
                    f"{economy.COIN} offen. Das kann nur "
                    f"**{ziel.display_name}** streichen.")
        if offen == 0:
            return f"**{ziel.display_name}** hat bei dir nichts offen."
        rest_text = " ".join(message.content.split()[1:])
        betrag = None
        if not any(w in rest_text.lower() for w in ("alles", "komplett", "ganz", "all")):
            betrag = self._lies_betrag(message, rest_text)
        weg, rest = self.erlassen(message.author.id, ziel.id, betrag)
        if weg <= 0:
            return f"**{ziel.display_name}** hat bei dir nichts offen."
        await self._save()
        if rest > 0:
            return (f"📒 Erlassen: **{fmt(weg)}** {economy.COIN} für "
                    f"**{ziel.display_name}** – offen bleiben **{fmt(rest)}**.")
        return (f"📒 Großzügig: **{fmt(weg)}** {economy.COIN} erlassen. "
                f"**{ziel.display_name}** ist bei dir wieder blitzsauber. ✨")

    # --- Anzeige ----------------------------------------------------------
    def _posten_zeile(self, p, *, aus_sicht_von=None):
        wer = (f"<@{p.schuldner}>" if aus_sicht_von != p.schuldner
               else f"<@{p.glaeubiger}>")
        teil = ""
        if p.offen < p.urspruenglich:
            teil = f" _(von {fmt(p.urspruenglich)})_"
        termin = ""
        if p.faellig:
            termin = (" · ⏰ **überfällig**" if p.ist_ueberfaellig()
                      else f" · fällig <t:{int(p.faellig)}:R>")
        grund = f" · _{p.grund}_" if p.grund else ""
        return f"`#{p.id}` {wer} — **{fmt(p.offen)}**{teil}{termin}{grund}"

    def _eigene_embed(self, autor, guild=None):
        forderungen = self.buch.posten_gegen(autor.id)
        schulden_ = self.buch.posten_von(autor.id)
        haben, soll, netto = self.buch.summen(autor.id)
        if soll > 0:
            kopf = (f"🔴 Du hast **{fmt(soll)}** {economy.COIN} offen. "
                    f"**{int(TILGUNG_PCT * 100)} %** deiner Einnahmen (Daily, Casino, "
                    f"Spiele, Gewinne) gehen automatisch dorthin, bis es weg ist.")
            farbe = FARBE_SCHULDEN
        elif forderungen:
            kopf = "🟡 Du hast nichts offen – aber andere bei dir."
            farbe = FARBE
        else:
            kopf = "🟢 Blitzsauber: du hast mit niemandem etwas offen. ✨"
            farbe = FARBE_SAUBER
        e = discord.Embed(title="🧾 Dein Schuldbuch", description=kopf, color=farbe)
        if forderungen:
            e.add_field(
                name=f"⬅️ Du bekommst noch · {fmt(haben)} {economy.COIN}",
                value="\n".join(self._posten_zeile(p, aus_sicht_von=autor.id)
                                for p in forderungen[:8]),
                inline=False)
        if schulden_:
            e.add_field(
                name=f"➡️ Du hast noch offen · {fmt(soll)} {economy.COIN}",
                value="\n".join(self._posten_zeile(p, aus_sicht_von=autor.id)
                                for p in schulden_[:8]),
                inline=False)
        if forderungen or schulden_:
            zeichen = "＋" if netto >= 0 else "−"
            e.add_field(name="Unterm Strich",
                        value=f"**{zeichen}{fmt(abs(netto))}** {economy.COIN}",
                        inline=True)
            getilgt = self.getilgt_summe(autor.id)
            if getilgt:
                e.add_field(name="Automatisch getilgt",
                            value=f"**{fmt(getilgt)}** {economy.COIN}", inline=True)
        e.add_field(name="Kreditwürdigkeit",
                    value=f"{self.score.ampel(autor.id)} "
                          f"**{self.score.score(autor.id)}**/100", inline=True)
        e.set_footer(text=f"{self._bot_name} leih @wer 5k · "
                          f"{self._bot_name} tilg @wer · "
                          f"{self._bot_name} schulden erlassen @wer")
        try:
            e.set_thumbnail(url=autor.display_avatar.url)
        except Exception:  # noqa: BLE001 - Bild ist Deko
            pass
        return e

    def _paar_embed(self, autor, ziel):
        posten = self.buch.zwischen(autor.id, ziel.id, nur_offen=False)
        saldo = self.buch.saldo(autor.id, ziel.id)
        farbe = FARBE_SCHULDEN if saldo < 0 else (FARBE if saldo > 0 else FARBE_SAUBER)
        e = discord.Embed(title=f"🧾 Schuldbuch · {ziel.display_name}", color=farbe)
        if not posten:
            e.description = f"Zwischen dir und <@{ziel.id}> steht nichts im Buch."
            return e
        if saldo > 0:
            e.description = (f"<@{ziel.id}> steht mit **{fmt(saldo)}** {economy.COIN} "
                             f"bei dir in der Kreide.")
        elif saldo < 0:
            e.description = (f"🔴 Du hast bei <@{ziel.id}> noch **{fmt(-saldo)}** "
                             f"{economy.COIN} offen.\n"
                             f"_{int(TILGUNG_PCT * 100)} % deiner Einnahmen gehen "
                             f"automatisch dorthin – `{self._bot_name} tilg "
                             f"@{ziel.display_name}` geht schneller._")
        else:
            e.description = f"Mit <@{ziel.id}> ist alles **ausgeglichen**. ✨"
        offen = [p for p in posten if p.ist_offen()]
        if offen:
            e.add_field(name="Offene Posten",
                        value="\n".join(self._posten_zeile(p, aus_sicht_von=autor.id)
                                        for p in offen[:8]),
                        inline=False)
        erledigt = [p for p in posten if not p.ist_offen()][-5:]
        if erledigt:
            e.add_field(
                name="Erledigt",
                value="\n".join(
                    f"`#{p.id}` {fmt(p.urspruenglich)} — {p.status}"
                    + (f" · _{p.grund}_" if p.grund else "")
                    for p in erledigt),
                inline=False)
        try:
            e.set_thumbnail(url=ziel.display_avatar.url)
        except Exception:  # noqa: BLE001
            pass
        return e

    def _top_embed(self, guild=None):
        posten = self.buch.top(10)
        e = discord.Embed(title="🧾 Die größten offenen Posten", color=FARBE_SCHULDEN)
        if not posten:
            e.description = "Auf dem ganzen Server ist nichts offen. Vorbildlich. ✨"
            return e
        zeilen = []
        for i, (glaeubiger, schuldner, betrag) in enumerate(posten, 1):
            marke = "🥇🥈🥉"[i - 1] if i <= 3 else f"`{i}.`"
            zeilen.append(f"{marke} <@{schuldner}> → <@{glaeubiger}> · "
                          f"**{fmt(betrag)}** {economy.COIN}")
        e.description = "\n".join(zeilen)
        e.set_footer(text="Wer zuerst steht, hat am meisten offen. Rein informativ.")
        return e

    def _mahn_embed(self, uid, soll):
        glaeubiger = self.buch.glaeubiger_von(uid)
        zeilen = "\n".join(f"• <@{u}> — **{fmt(b)}** {economy.COIN}"
                           for u, b in glaeubiger[:6])
        e = discord.Embed(
            title="🧾 Da ist noch was offen",
            description=(f"In deinem Schuldbuch stehen **{fmt(soll)}** "
                         f"{economy.COIN}:\n{zeilen}"),
            color=FARBE_SCHULDEN)
        e.add_field(
            name="Wie das kleiner wird",
            value=(f"**{int(TILGUNG_PCT * 100)} %** von jeder Einnahme (Daily, "
                   f"Casino, Spiele, Gewinne) wandern automatisch dorthin – "
                   f"anteilig an alle, überfällige Posten zuerst.\n"
                   f"Schneller gehts mit `{self._bot_name} tilg @wer`."),
            inline=False)
        if self.getilgt_summe(uid):
            e.set_footer(text=f"Bisher automatisch getilgt: "
                              f"{fmt(self.getilgt_summe(uid))} Coins")
        return e

    def _mahn_embed_posten(self, p, stufe):
        tage = int((time.time() - p.faellig) / TAG) if p.faellig else 0
        titel = {1: "🧾 Fällig geworden", 2: "🧾 Seit einer Woche überfällig",
                 3: "🧾 Seit zwei Wochen überfällig"}[stufe]
        e = discord.Embed(
            title=titel,
            description=(f"Posten **#{p.id}** bei <@{p.glaeubiger}>: "
                         f"**{fmt(p.offen)}** {economy.COIN} offen"
                         + (f" (seit {tage} Tagen fällig)" if tage else "")
                         + (f"\n_{p.grund}_" if p.grund else "")),
            color=FARBE_SCHULDEN)
        if stufe >= 2:
            vorschlag = max(TILGUNG_MIN_JE_GLAEUBIGER, p.offen // 4)
            e.add_field(name="Vorschlag",
                        value=(f"Zahl in vier Schritten ab: "
                               f"`{self._bot_name} tilg <@{p.glaeubiger}> "
                               f"{fmt(vorschlag)}`"),
                        inline=False)
        if stufe >= 3:
            e.set_footer(text="Ab hier darf der Server das (ohne Beträge) öffentlich "
                              "notieren, wenn er das eingeschaltet hat.")
        return e

    def _mahn_embed_glaeubiger(self, p):
        return discord.Embed(
            title="🧾 Dein Posten ist überfällig",
            description=(f"<@{p.schuldner}> hat Posten **#{p.id}** über "
                         f"**{fmt(p.offen)}** {economy.COIN} noch offen.\n"
                         f"Du kannst jederzeit `{self._bot_name} schulden erlassen "
                         f"<@{p.schuldner}>` – oder einfach warten."),
            color=FARBE)

    def _verfall_warnung_embed(self, p, tage):
        return discord.Embed(
            title="🧾 Ein Posten verfällt bald",
            description=(f"Posten **#{p.id}** gegen <@{p.schuldner}> über "
                         f"**{fmt(p.offen)}** {economy.COIN} hatte seit "
                         f"{VERFALL_TAGE - tage} Tagen keine Bewegung.\n"
                         f"In **{tage} Tag(en)** verfällt er."),
            color=FARBE)

    def _verfall_embed(self, p):
        return discord.Embed(
            title="🧾 Posten verfallen",
            description=(f"Posten **#{p.id}** gegen <@{p.schuldner}> über "
                         f"**{fmt(p.offen)}** {economy.COIN} ist nach "
                         f"{VERFALL_TAGE} Tagen ohne Bewegung **verfallen**.\n"
                         f"_Kein ewiges Druckmittel – die Historie bleibt sichtbar._"),
            color=FARBE)

    def _hilfe_embed(self):
        return discord.Embed(
            title="📒 Das Schuldbuch",
            description=(
                "**Eine Schuld entsteht nur, wenn beide es so wollen.** Wer "
                "einfach `pay` benutzt, verschenkt sein Geld – daraus wird keine "
                "Forderung.\n\n"
                f"`{self._bot_name} leih @wer 5k [Grund] [bis freitag]`\n"
                "→ Kredit anbieten. Erst der Klick des anderen bewegt Geld.\n"
                f"`{self._bot_name} pay @wer 5k als leihgabe`\n"
                "→ dasselbe, nur aus der Zahlung heraus.\n"
                f"`{self._bot_name} schuldschein @wer 5k [Grund]`\n"
                "→ Schuld ohne Geldfluss. Bestätigen muss der **Schuldner**.\n\n"
                f"`{self._bot_name} schulden` · deine Übersicht\n"
                f"`{self._bot_name} schulden @wer` · Posten mit einer Person\n"
                f"`{self._bot_name} tilg @wer [betrag]` · freiwillig abzahlen\n"
                f"`{self._bot_name} schulden erlassen @wer [betrag]` · streichen\n"
                f"`{self._bot_name} insolvenz` · Neuanfang (kostet 50 % + Score)\n\n"
                f"**Automatisch:** {int(TILGUNG_PCT * 100)} % jeder echten Einnahme "
                "gehen an die Gläubiger – anteilig an alle, überfällige Posten "
                "zuerst, innerhalb einer Person der älteste zuerst.\n"
                f"**Kreditwürdigkeit** (0–100) steigt mit pünktlichem Zahlen und "
                f"begrenzt, wie viel dir jemand leihen kann.\n"
                f"**Verfall:** ohne jede Bewegung ist ein Posten nach "
                f"{VERFALL_TAGE} Tagen weg."),
            color=FARBE)


# --- Views -------------------------------------------------------------------
class _AnfrageView(discord.ui.View):
    """Die zwei Knoepfe unter einem Leih-Angebot bzw. Schuldschein.

    Klicken darf NUR die Person, der daraus eine Schuld entsteht."""

    def __init__(self, modul, besteller, ziel, betrag, *, grund, faellig, mit_geld):
        super().__init__(timeout=ANFRAGE_TIMEOUT)
        self._modul = modul
        self.besteller = besteller
        self.ziel = ziel
        self.betrag = betrag
        self.grund = grund
        self.faellig = faellig
        self.mit_geld = mit_geld
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ziel.id:
            await interaction.response.send_message(
                "Das kann nur die angesprochene Person bestätigen.", ephemeral=True)
            return False
        return True

    async def _abschliessen(self, interaction, text):
        for ch in self.children:
            ch.disabled = True
        try:
            await interaction.response.edit_message(content=text, embed=None, view=self)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="Annehmen", emoji="🤝", style=discord.ButtonStyle.success)
    async def _ja(self, interaction, _b):
        ok, text = await self._modul.annehmen(
            self.besteller, self.ziel, self.betrag, grund=self.grund,
            faellig=self.faellig, mit_geld=self.mit_geld)
        await self._abschliessen(interaction, text if ok else f"❌ {text}")

    @discord.ui.button(label="Ablehnen", emoji="✋", style=discord.ButtonStyle.secondary)
    async def _nein(self, interaction, _b):
        await self._abschliessen(
            interaction, f"✋ <@{self.ziel.id}> hat abgelehnt – es ist nichts passiert.")

    async def on_timeout(self):
        if self.message is None:
            return
        try:
            for ch in self.children:
                ch.disabled = True
            await self.message.edit(
                content="⌛ Das Angebot ist abgelaufen – es ist nichts passiert.",
                embed=None, view=self)
        except discord.HTTPException:
            pass


class _InsolvenzView(discord.ui.View):
    """Sicherheitsabfrage: eine Insolvenz laesst sich nicht rueckgaengig machen."""

    def __init__(self, modul, autor):
        super().__init__(timeout=60)
        self._modul = modul
        self.autor = autor
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id != self.autor.id:
            await interaction.response.send_message(
                "Das ist nicht deine Rückfrage.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Ja, Insolvenz anmelden", emoji="⚖️",
                       style=discord.ButtonStyle.danger)
    async def _ja(self, interaction, _b):
        text = await self._modul.insolvenz_durchfuehren(self.autor.id)
        for ch in self.children:
            ch.disabled = True
        try:
            await interaction.response.edit_message(content=text, embed=None, view=self)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def _nein(self, interaction, _b):
        for ch in self.children:
            ch.disabled = True
        try:
            await interaction.response.edit_message(
                content="Abgebrochen – es bleibt alles, wie es ist.",
                embed=None, view=self)
        except discord.HTTPException:
            pass
        self.stop()


# --- Singleton + Modul-API ---------------------------------------------------
instance = Schulden()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
pay_block = instance.pay_block
note_pay = instance.note_pay
note_pay_block = instance.note_pay_block
saldo = instance.saldo
posten = instance.posten
summen = instance.summen
erlassen = instance.erlassen
top = instance.top
tilgen_von_einnahme = instance.tilgen_von_einnahme
getilgt_summe = instance.getilgt_summe
kredit_score = instance.kredit_score
mahn_tick = instance.mahn_tick
# Fuer economy._pay: erkennt 'als leihgabe' und stellt die Anfrage.
leih_anfrage = instance._anfrage_stellen
