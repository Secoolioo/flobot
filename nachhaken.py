"""Er haelt nach: wer "morgen hab ich Pruefung" schreibt, wird hinterher gefragt.

Wie es laeuft
=============
  1. note_message()  billige Vorpruefung jeder Nachricht (synchron): kommt ein
     Zeitwort vor (morgen, Freitag, 12.03. ...), spricht die Person von sich
     (ich, hab, muss, bin ...), ist es keine Frage und nichts Heikles? Dann
     rechnet Python den Zeitpunkt aus (zeitpunkt) und legt einen KANDIDATEN
     ab - nur im Speicher, hoechstens 30 je Server, nach 6 Stunden weg.
  2. tick()          ordnet die Kandidaten gebuendelt ein: je Server hoechstens
     alle 10 Minuten EIN KI-Aufruf mit bis zu 10 Zeilen (hintergrund=True,
     Grenze im System-Prompt). Die KI sagt je Zeile, ob da wirklich etwas
     angekuendigt wird, worum es geht ('die Mathe-Pruefung') und ob es heikel
     ist. Gespeichert wird NUR das Thema - nie die Nachricht.
  3. vielleicht_nachfragen()  schreibt die Person nach dem Termin wieder etwas
     (nicht an Flo), fragt Flo EINMAL nach - als Antwort auf diese Nachricht,
     hoechstens einmal je Person und Tag und dreimal je Server und Tag. Nicht
     in einem Aufraeum-Kanal (dort waere die Frage nach einer Minute weg),
     nicht, wo Flo nicht schreiben darf, und nicht vor Leuten, die die
     Ankuendigung nie lesen konnten (sie kam aus einem privaten Kanal oder
     Thread, einem Mod- oder NSFW-Kanal - basis.privat) - dann fragt die
     naechste Nachricht in einem passenden Kanal. Die Frage landet im
     Chatverlauf der KI: wer darauf
     antwortet ('war gut, ne 2'), redet mit einem Flo, der weiss, was er fragte.

Wann
====
    morgen, am Freitag, 12.03.   der Tag, 17 Uhr
    uebermorgen, in 3 Tagen      der Tag, 17 Uhr
    heute abend / heute nacht    am naechsten Tag, 10 Uhr
    spaeter, nachher             in drei Stunden
    naechste Woche               Mittwoch der naechsten Woche, 17 Uhr
    am Wochenende                der kommende Samstag (am Wochenende selbst: Sonntag)
    naechsten Freitag            Freitag der NAECHSTEN Kalenderwoche
    naechste Woche Freitag       ebenso (der Wochentag nah bei 'naechste Woche')
    diesen Freitag               dieser Freitag - auch heute, wenn heute Freitag ist
    am Freitag (an einem Freitag) der Freitag in einer Woche
Sonntag ab 15 Uhr heisst 'am Wochenende' das naechste. Ein Datum ohne Jahr,
das dieses Jahr schon vorbei ist, gilt fuers naechste. Stehen mehrere
Ausdruecke da, gilt der genaueste (Datum vor Tag vor 'naechste Woche' vor
'spaeter'), unter gleich genauen der spaeteste Termin: 'am Freitag, den
13.11.' ist der 13.11., nicht dieser Freitag - lieber zu spaet fragen als
vor dem Ereignis.
Frueher als zwei Stunden nach der Ankuendigung fragt Flo nie, mehr als 60
Tage voraus merkt er sich nichts, und drei Tage nach dem Termin verfaellt es.

Was bewusst nicht passiert
==========================
  - Nichts bei Notlagen (ai.FloAI._NOTLAGE_RE): nicht merken, nicht fragen -
    auch nicht, wenn im Kanal gerade jemand am Boden ist. Und wer selbst ein
    Notsignal geschrieben hat, egal wo: alles Offene von ihm ist weg, und 72
    Stunden lang merkt Flo sich nichts von ihm und fragt nichts (notlage()).
  - Ist der KI-Chat auf dem Server aus, geht keine Chatzeile an die KI.
  - Nichts Heikles: Krankheit, Arzt, Tod, Trennung, Polizei, Schulden ... -
    schon die Vorpruefung laesst es liegen, die KI sortiert nach, und ihr
    Thema wird noch einmal geprueft (gehirn._heikel, fun.ist_hetze).
  - Keine Rohnachrichten auf der Platte. Kandidaten leben nur im Speicher.
  - Nicht in der Sendepause, nicht bei Ansprache, nie ungefragt mit Ping.
  - Wer nicht will: 'flo nachhaken aus' (je Person und Server) - das raeumt
    auch alles Offene weg. 'flo vergiss mich' (gehirn) raeumt es ebenfalls.
    Beides gilt auch, wo Nachhaken oder das Gedaechtnis aus ist (datenschutz).

Wo was aufgerufen wird:
    handle(message)              Handler-Kette, nach umfrage
                                 ('flo nachhaken aus|an' je Person)
    datenschutz(message)         Handler-Kette, nach gehirn, OHNE Schalter:
                                 'flo nachhaken aus' und - wenn das
                                 Gedaechtnis es nicht nimmt - 'flo vergiss
                                 mich' gelten auch, wo Nachhaken oder das
                                 Gedaechtnis aus ist
    note_message(msg)            bot.on_message, SYNCHRON, direkt nach dem
                                 Gedaechtnis-Haken - nie bei Notlagen
    vielleicht_nachfragen(msg)   bot.on_message, im Hintergrund, fuer jede
                                 Nachricht, die NICHT an Flo geht - nie bei
                                 Notlagen und nie in der Sendepause (beides
                                 prueft bot.py). True = Flo hat nachgefragt.
    tick(client)                 bot.alltag_loop, jede Minute (hoechstens
                                 bot.ALLTAG_FRIST, dann abgebrochen - die
                                 gebuendelten KI-Aufrufe haben deshalb eine
                                 eigene, kuerzere Frist). Die Sendepause prueft
                                 bot.py hier NICHT - tick fragt selbst
    notlage(gid, uid)            bot.on_message, SYNCHRON, fuer JEDE Nachricht
                                 mit Notsignal (auch wo Nachhaken aus ist)
    vergiss(gid, uid) -> int     gehirn 'vergiss mich': wie viele offene Sachen
                                 weg sind (Kandidaten gehen mit, zaehlen nicht)
    zeilen_fuer(gid, uid)        gehirn 'gedaechtnis': worauf Flo noch wartet

Daten: data/nachhaken.json - {"guilds": {"<gid>": {"offen": [...], "gefragt":
{"<uid>": "2026-10-21"}, "heute": {"tag": "2026-10-21", "n": 2}, "aus":
["<uid>"]}}}. 'heute' ist der Server-Deckel ohne Namen. Beim Import passiert
nichts.
"""

import asyncio
import json
import logging
import random
import re
import secrets
import time
from datetime import datetime, time as tageszeit, timedelta

import discord

import admin
import ai
import basis
from basis import FeatureBasis
import features
import fun
import gehirn
import guildcfg
import uhr
from store import JsonStore

log = logging.getLogger("dcbot.nachhaken")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("nachhaken",)
_ERSTE = frozenset(_CMDS)
_BEFEHL_RE = re.compile(r"nachhaken(?: (aus|an|ein|stop|stopp|off|on))?")
_AUS = frozenset(("aus", "stop", "stopp", "off"))
#: Wie im Gedaechtnis (gehirn._VERGESSEN_RE): 'vergiss' und 'vergiss mich'.
_VERGISS_RE = re.compile(r"vergiss(?: mich)?")

# --- Grenzen -------------------------------------------------------------------
MIN_LAENGE, MAX_LAENGE = 12, 300
KANDIDATEN_MAX = 30                 # je Server, nur im Speicher
KANDIDAT_TTL = 6 * 3600.0
RUNDE = 600.0                       # je Server hoechstens ein KI-Aufruf so oft
BUENDEL = 10                        # Zeilen je KI-Aufruf
SERVER_JE_TAKT = 2                  # so viele Server ordnet ein Takt hoechstens ein
KI_FRIST = 45.0                     # unter bot.ALLTAG_FRIST - der Takt bricht sonst ab
OFFEN_MAX = 100                     # je Server
PERSON_OFFEN_MAX = 5                # je Person und Server
WAS_MAX = 80
SATZ_MAX = 220
PERSON_PRO_TAG = 1
SERVER_PRO_TAG = 3
MIN_ABSTAND = timedelta(hours=2)
VORAUS_MAX = timedelta(days=60)
VERFALL = 3 * 86400.0
SPAETER = timedelta(hours=3)
#: Nach einem Notsignal fragt Flo die Person so lange nirgends nach etwas.
NOTLAGE_SPERRE = 72 * 3600.0
EREIGNIS_UHR = 17                   # Tages-Phrasen: an dem Tag um 17 Uhr
DANACH_UHR = 10                     # 'heute abend': am naechsten Tag um 10 Uhr

# --- Vorpruefung -----------------------------------------------------------------
#: Teilstrings, ohne die keiner der Zeit-Ausdruecke unten passen kann. Die
#: allermeisten Nachrichten sind hier nach ein paar 'in'-Vergleichen durch.
_SCHNELL = ("morgen", "woche", "montag", "dienstag", "mittwoch", "donnerstag",
            "freitag", "samstag", "sonntag", "abend", "nacht", "nachher",
            "später", "spaeter", "tag")
_ICH_RE = re.compile(
    r"\b(?:ich|i|mein|meine|meinen|meinem|meiner|wir|unser|unsere|hab|habe|hab's|"
    r"muss|muß|werd|werde|bin|geh|gehe|schreib|schreibe|fahr|fahre|flieg|fliege|"
    r"zieh|ziehe|hol|hole|treff|treffe|mach|mache|spiel|spiele|kauf|kaufe|komm|"
    r"komme|darf|soll|will|starte|fang|lern|lerne)\b")
#: Darueber redet Flo nicht hinterher - egal was die KI meint.
_SENSIBEL_RE = re.compile(
    r"arzt|ärzt|aerzt|krankenhaus|klinik|notaufnahme|\bop\b|operation|operiert|"
    r"therapie|therapeut|psycholog|psychiat|\bkrank|krebs|chemo|diagnose|befund|"
    r"\btod\b|\btot\b|gestorben|stirbt|sterben|beerdigung|beisetzung|trauerfeier|"
    r"beileid|trennung|getrennt|schluss gemacht|scheidung|schwanger|abtreibung|"
    r"polizei|anzeige erstattet|anzeige gegen|\bgericht|anwalt|verhandlung|"
    r"schulden|insolvenz|kündigung|gekündigt|kuendigung|gekuendigt|arbeitslos|"
    r"jobverlust|entlassen|\bsex|drogen|kiffen|entzug|depress|panikattack|\bangst|"
    r"schlecht geht|mies geht|beschissen geht|\bimpf|blutabnahme")

# --- Zeitpunkt -------------------------------------------------------------------
_WOCHENTAGE = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
               "freitag": 4, "samstag": 5, "sonntag": 6}
_KURZTAG = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_ZAHLWORT = {"ein": 1, "einem": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4,
             "fünf": 5, "fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
             "zehn": 10, "elf": 11, "zwölf": 12, "zwoelf": 12, "vierzehn": 14}
_DATUM_RE = re.compile(r"(?<![0-9.])([0-9]{1,2})\.([0-9]{1,2})\.(?:([0-9]{4}|[0-9]{2})(?![0-9]))?")
_MARKER = (
    ("datum", _DATUM_RE),
    ("uebermorgen", re.compile(r"\b(?:übermorgen|uebermorgen)\b")),
    ("morgen", re.compile(r"\bmorgen\b")),
    ("heute_abend", re.compile(r"\bheut(?:e)? (?:am )?(?:abend|nacht)\b")),
    ("in_tagen", re.compile(r"\bin ([0-9]{1,2}|" + "|".join(_ZAHLWORT) + r") tag(?:en)?\b")),
    ("naechste_woche", re.compile(r"\b(?:nächste|naechste|kommende) woche\b")),
    ("wochenende", re.compile(
        r"\b(am|dieses|diesem|übers|uebers|nächstes|naechstes|kommendes) wochenende\b")),
    ("wochentag", re.compile(
        r"\b(?:(am|diesen|diese|diesem|nächsten|naechsten|nächste|naechste|kommenden) )?"
        r"(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)"
        r"(?:abend|nachmittag|vormittag|mittag|morgen|früh)?\b")),
    ("spaeter", re.compile(r"\b(?:später|spaeter|nachher)\b")),
)
#: Davor heisst 'morgen' nicht 'am naechsten Tag' ('guten Morgen', 'heute morgen').
_MORGEN_NICHT_RE = re.compile(
    r"\b(?:heute|heut|guten|gestern|jeden|am|diesen|ganzen|einen|schönen|schoenen)\s$")
#: Davor ist ein Tag schon vorbei ('letzten Freitag', 'seit dem 12.03.').
_VERGANGEN_RE = re.compile(
    r"\b(?:letzt\w*|vergangen\w*|vorig\w*|seit|vom)\s(?:dem\s|den\s)?$")
_BIS_RE = re.compile(r"\bbis\s$")
#: Wie genau ein Ausdruck den Tag trifft - kleiner ist genauer (siehe zeitpunkt).
_GENAU = {"datum": 0, "uebermorgen": 1, "morgen": 1, "heute_abend": 1, "in_tagen": 1,
          "wochentag": 1, "naechste_woche": 2, "wochenende": 2, "spaeter": 3}
#: So nah (Zeichen dazwischen) muss ein Wochentag bei 'naechste Woche' stehen,
#: damit er in der naechsten Woche liegt.
_NAH = 25


def _um(tag, stunde):
    return datetime.combine(tag, tageszeit(stunde, 0), tzinfo=uhr.TIMEZONE)


def _ziel(art, m, jetzt):
    """Der Termin zu EINEM Treffer - oder None (kein gueltiger Tag)."""
    heute = jetzt.date()
    naechste_woche = heute + timedelta(days=7 - heute.weekday())   # deren Montag
    if art == "datum":
        tag, monat, jahr = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            if jahr:
                jahr = int(jahr) + (2000 if len(jahr) == 2 else 0)
                ziel = heute.replace(year=jahr, month=monat, day=tag)
                if ziel < heute:
                    return None
            else:
                ziel = heute.replace(month=monat, day=tag)
                if ziel < heute:                        # schon vorbei -> naechstes Jahr
                    ziel = ziel.replace(year=heute.year + 1)
        except ValueError:                              # 31.02., 29.02. im Nicht-Schaltjahr
            return None
        return _um(ziel, EREIGNIS_UHR)
    if art == "uebermorgen":
        return _um(heute + timedelta(days=2), EREIGNIS_UHR)
    if art == "morgen":
        return _um(heute + timedelta(days=1), EREIGNIS_UHR)
    if art == "heute_abend":
        return _um(heute + timedelta(days=1), DANACH_UHR)
    if art == "in_tagen":
        wort = m.group(1)
        n = int(wort) if re.fullmatch(r"[0-9]+", wort) else _ZAHLWORT.get(wort, 0)
        if not 1 <= n <= VORAUS_MAX.days:
            return None
        return _um(heute + timedelta(days=n), EREIGNIS_UHR)
    if art == "naechste_woche":
        return _um(naechste_woche + timedelta(days=2), EREIGNIS_UHR)
    if art == "wochenende":
        if m.group(1) in ("nächstes", "naechstes", "kommendes"):
            return _um(naechste_woche + timedelta(days=5), EREIGNIS_UHR)
        if heute.weekday() >= 5:                        # mitten drin: Sonntag
            ziel = _um(heute + timedelta(days=6 - heute.weekday()), EREIGNIS_UHR)
            if ziel - jetzt >= MIN_ABSTAND:
                return ziel
            # Sonntagabend ist dieses Wochenende gelaufen - gemeint ist das naechste.
            return _um(naechste_woche + timedelta(days=5), EREIGNIS_UHR)
        return _um(heute + timedelta(days=5 - heute.weekday()), EREIGNIS_UHR)
    if art == "wochentag":
        vorne, wt = m.group(1) or "", _WOCHENTAGE[m.group(2)]
        if vorne in ("nächsten", "naechsten", "nächste", "naechste"):
            return _um(naechste_woche + timedelta(days=wt), EREIGNIS_UHR)
        tage = (wt - heute.weekday()) % 7
        if tage == 0 and not vorne.startswith("dies"):
            tage = 7                                    # 'am Freitag' an einem Freitag
        return _um(heute + timedelta(days=tage), EREIGNIS_UHR)
    if art == "spaeter":
        return jetzt + SPAETER
    return None


def _abstand(a, b):
    """Zeichen zwischen zwei Treffern (0, wenn sie sich beruehren)."""
    return max(0, b.start() - a.end(), a.start() - b.end())


def zeitpunkt(text, jetzt):
    """(faellig_ab, wann_text) fuer eine Ankuendigung - oder None.

    jetzt ist eine Ortszeit mit Zeitzone. Es gilt der GENAUESTE Ausdruck
    (_GENAU: ein Datum schlaegt 'am Freitag', 'am Freitag' schlaegt 'naechste
    Woche'), unter gleich genauen der SPAETESTE Termin - lieber einen Tag zu
    spaet fragen als vor dem Ereignis. Ein Wochentag direkt bei 'naechste
    Woche' liegt in der naechsten Woche. Frueher als MIN_ABSTAND nach jetzt
    wird es nie, weiter als VORAUS_MAX voraus gibt es nicht."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    treffer = []                                        # [genau, ziel, wann, art, m]
    for art, muster in _MARKER:
        for m in muster.finditer(low):
            davor = low[:m.start()]
            if art == "morgen" and _MORGEN_NICHT_RE.search(davor):
                continue
            if art in ("datum", "wochentag", "morgen") and _VERGANGEN_RE.search(davor):
                continue
            if art == "spaeter" and _BIS_RE.search(davor):
                continue                                # 'bis später' ist ein Gruss
            ziel = _ziel(art, m, jetzt)
            if ziel is not None:
                treffer.append([_GENAU[art], ziel, m.group(0), art, m])
    if not treffer:
        return None
    # 'naechste Woche Freitag', 'Freitag naechste Woche', 'naechste Woche hab
    # ich am Freitag ...': der Freitag der naechsten Woche, nicht dieser.
    wochen = [t[4] for t in treffer if t[3] == "naechste_woche"]
    if wochen:
        montag = jetzt.date() + timedelta(days=7 - jetzt.weekday())
        for t in treffer:
            m = t[4]
            if t[3] != "wochentag" or (m.group(1) or "").startswith("dies"):
                continue
            nah = [w for w in wochen if _abstand(w, m) <= _NAH]
            if nah:
                t[1] = _um(montag + timedelta(days=_WOCHENTAGE[m.group(2)]), EREIGNIS_UHR)
                t[2] = f"{nah[0].group(0)} {m.group(2)}"
    bester = min(treffer, key=lambda t: (t[0], -t[1].timestamp()))
    faellig = max(bester[1], jetzt + MIN_ABSTAND)
    if faellig - jetzt > VORAUS_MAX:
        return None
    return faellig, bester[2]


# --- KI ---------------------------------------------------------------------------
_EXTRAKT_PROMPT = (
    "Du sortierst Discord-Nachrichten, in denen jemand etwas ueber die naechsten Tage "
    "sagt. Zu JEDER nummerierten Zeile entscheidest du: Kuendigt die Person ein "
    "konkretes eigenes Vorhaben oder Ereignis an, nach dem man sie hinterher fragen "
    "kann, wie es gelaufen ist (Pruefung, Klausur, Vorstellungsgespraech, "
    "Fuehrerschein, Umzug, Konzert, Turnier, Date, Reise)? Dann ok=true, und 'was' "
    "ist das Thema in hoechstens sechs Woertern mit Artikel, so wie man es in einer "
    "Frage nennt ('die Mathe-Pruefung', 'das Vorstellungsgespraech') - ohne "
    "Zeitangabe, ohne Namen, auf Deutsch. ok=false bei Witz, Ironie, Zitat, Frage, "
    "Spielzug, Erinnerung an andere, Alltagskram (essen, schlafen, zocken, duschen) "
    "und bei allem, was schon vorbei ist. heikel=true bei allem rund um Krankheit, "
    "Arzt, Krankenhaus, OP, Therapie, Tod, Beerdigung, Trennung, Schwangerschaft, "
    "Polizei, Gericht, Schulden, Jobverlust, Sex, Drogen oder wenn sich jemand "
    "schlecht fuehlt - im Zweifel heikel=true. Antworte NUR mit einem JSON-Array, ein "
    "Objekt je Zeile, zum Beispiel "
    '[{"nr":1,"ok":true,"was":"die Mathe-Pruefung","heikel":false}] - kein Text '
    "davor oder danach."
)
_FRAGE_PROMPT = (
    "Du bist {name}, das passiv-aggressive Grossmaul aus einem deutschen Discord. "
    "Jemand hat vor ein paar Tagen angekuendigt, dass etwas ansteht - jetzt ist es "
    "vorbei, und von selbst hat die Person natuerlich nichts erzaehlt. Du fragst "
    "nach: GENAU EIN Satz, passiv-aggressiv, als haettest du tagelang auf den "
    "Bericht gewartet. Sprich die Person mit du an und nenn das Thema; kein Emoji, "
    "keine Anfuehrungszeichen, keine @-Erwaehnung. Kein Spott ueber Krankheit, Tod "
    "oder echte Probleme. Gib NUR den Satz aus."
)
#: Wenn die KI nichts Brauchbares liefert. {Was} = mit grossem Anfangsbuchstaben.
_ERSATZ = (
    "Na {name}, wie lief {was}? Oder reden wir nicht drüber, weil's wieder nix war?",
    "{name}, {was} ist durch. Ich warte immer noch auf den Bericht – aber klar, "
    "bin ja nur der Bot.",
    "{name}, ich hab extra nicht nachgefragt, wie {was} war. Bis jetzt. Also?",
    "Und, {name}? {Was} ist vorbei, und ich erfahr's mal wieder als Letzter.",
)
_NAME_WEG_RE = re.compile(r"[@`*_~|<>#\\]")
_WAS_WEG_RE = re.compile(r"[\"'`*_~|<>#@\\„“”‚‘’]")


def _sauber(text, grenze=32):
    return re.sub(r"\s+", " ", _NAME_WEG_RE.sub("", str(text or ""))).strip()[:grenze]


def _zahl(wert, ersatz=0.0):
    try:
        return float(wert)
    except (TypeError, ValueError):
        return ersatz


def _norm(text):
    return re.sub(r"[^a-z0-9äöüß]+", "", str(text or "").lower())


class Nachhaken(FeatureBasis):
    """Offene Ankuendigungen je Server - und die Nachfrage danach."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._zeit = time.time          # austauschbar (Tests)
        self._mono = time.monotonic
        self._kandidaten = {}           # gid -> [Kandidat] (nur im Speicher)
        self._runde = {}                # gid -> monotonic der letzten Einordnung
        # uid -> monotonic des letzten Notsignals (nur im Speicher, siehe notlage).
        self._notlage = {}
        # (gid, uid) -> wie oft schon 'vergiss'. Wer waehrend eines KI-Aufrufs
        # vergessen werden will, ist hinterher an der Zahl zu erkennen.
        self._vergessen = {}

    # --- Aufbau -------------------------------------------------------------
    def setup(self):
        """Nur mit KI: ob da wirklich etwas angekuendigt wird, entscheidet sie."""
        if not ai.is_enabled():
            log.info("Nachhaken aus: KI ist nicht aktiv.")
            return False
        self._store = JsonStore("nachhaken.json", default={"guilds": {}})
        self._enabled = True
        offen = sum(len((g or {}).get("offen") or [])
                    for g in (self._store.data.get("guilds") or {}).values()
                    if isinstance(g, dict))
        log.info("Nachhaken aktiv (%d offene Sache(n)).", offen)
        return True

    def is_enabled(self):
        return self._enabled and self._store is not None

    # --- Daten ----------------------------------------------------------------
    def _guild(self, gid):
        alle = self._store.data.get("guilds")
        if not isinstance(alle, dict):
            alle = self._store.data["guilds"] = {}
        eintrag = alle.get(str(int(gid)))
        if not isinstance(eintrag, dict):
            eintrag = alle[str(int(gid))] = {}
        for key, typ in (("offen", list), ("gefragt", dict), ("aus", list),
                         ("heute", dict)):
            if not isinstance(eintrag.get(key), typ):
                eintrag[key] = typ()
        if not all(isinstance(e, dict) for e in eintrag["offen"]):
            eintrag["offen"] = [e for e in eintrag["offen"] if isinstance(e, dict)]
        return eintrag

    @staticmethod
    def _server_heute(eintrag, heute):
        """So oft hat Flo auf dem Server heute schon nachgefragt. Ein eigener
        Zaehler ohne Namen ('heute': {'tag', 'n'}) - 'gefragt' je Person loescht
        'vergiss mich', der Server-Deckel muss trotzdem halten."""
        zaehler = eintrag["heute"]
        if uhr.spaeter_als(heute, uhr.glaubhaft(zaehler.get("tag"), heute)):
            return 0                                    # ein neuer Tag
        n = zaehler.get("n")
        return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0

    def _lies(self, gid):
        alle = self._store.data.get("guilds") if self._store is not None else None
        eintrag = alle.get(str(int(gid or 0))) if isinstance(alle, dict) else None
        return eintrag if isinstance(eintrag, dict) else {}

    def _speichern(self):
        if self._store is not None:
            self._store.save_soon()

    def _jetzt(self):
        return datetime.fromtimestamp(self._zeit(), uhr.TIMEZONE)

    def _ist_aus(self, gid, uid):
        aus = self._lies(gid).get("aus")
        return isinstance(aus, list) and str(int(uid)) in aus

    def _gesperrt(self, uid):
        """Hat diese Person in den letzten NOTLAGE_SPERRE Sekunden ein
        Notsignal geschrieben (egal wo)? Dann nichts merken, nichts fragen."""
        seit = self._notlage.get(int(uid or 0))
        return seit is not None and self._mono() - seit < NOTLAGE_SPERRE

    def _vergessen_nr(self, gid, uid):
        return self._vergessen.get((int(gid or 0), int(uid or 0)), 0)

    @staticmethod
    def _kanal_taugt(guild, kanal):
        """Darf die Nachfrage hier hin? Nicht in einen Aufraeum-Kanal - dort
        loescht bot.py auch Flos Antwort nach einer Minute, und mit ihr die
        eine Nachfrage des Tages. Und nicht, wo Flo nicht schreiben darf."""
        if kanal is None:
            return False
        if getattr(kanal, "id", 0) in set(
                guildcfg.get(guild.id, "autodelete_channels") or ()):
            return False
        ich = getattr(guild, "me", None)
        pruefen = getattr(kanal, "permissions_for", None)
        if ich is None or pruefen is None:
            return True                 # nicht pruefbar - Discord sagt es beim Senden
        try:
            rechte = pruefen(ich)
        except Exception:  # noqa: BLE001
            return True
        if isinstance(kanal, discord.Thread):
            return bool(getattr(rechte, "send_messages_in_threads", False))
        return bool(getattr(rechte, "send_messages", False))

    @staticmethod
    def _hinterzimmer(guild, sache, kanal):
        """Wurde 'sache' in einem Kanal angekuendigt, den nicht jeder lesen
        kann, der 'kanal' liest (privater Kanal oder Thread, Mod-Kanal, NSFW)?
        Dann fragt Flo hier nicht nach: wer im Hinterzimmer vom
        Vorstellungsgespraech erzaehlt, hat sich sein Publikum ausgesucht. Im
        selben Kanal (oder einem mit hoechstens demselben Publikum) schon.

        Gibt es den Kanal nicht mehr (geloescht, Thread archiviert), weiss Flo
        nicht, wer ihn lesen konnte - dann auch nicht. Nur eine Attrappe ohne
        Kanal-Suche (oder ein Eintrag ohne Kanal) ist nicht pruefbar."""
        try:
            cid = int(sache.get("kanal") or 0)
        except (TypeError, ValueError):
            cid = 0
        if not cid or cid == getattr(kanal, "id", None):
            return False
        holen = (getattr(guild, "get_channel_or_thread", None)
                 or getattr(guild, "get_channel", None))
        if holen is None:
            return False
        try:
            quelle = holen(cid)
        except Exception:  # noqa: BLE001
            quelle = None
        if quelle is None:
            return True
        return basis.privat(quelle, kanal, guild)

    @staticmethod
    def _heikel(text):
        """Notlage, private Daten, Hetze oder ein Thema, ueber das Flo nicht
        hinterher redet? Dann gar nicht erst."""
        if not text:
            return True
        return bool(ai.instance.ist_notlage(text)
                    or _SENSIBEL_RE.search(text.lower())
                    or gehirn.Gehirn._heikel(text)
                    or fun.ist_hetze(text))

    # --- Schritt 1: mithoeren ----------------------------------------------
    def note_message(self, msg):
        """Billige Vorpruefung, ob hier etwas angekuendigt wird. Synchron."""
        if not self._enabled:
            return False
        guild = getattr(msg, "guild", None)
        autor = getattr(msg, "author", None)
        if guild is None or autor is None or getattr(autor, "bot", False):
            return False
        text = (getattr(msg, "content", "") or "").strip()
        if not MIN_LAENGE <= len(text) <= MAX_LAENGE or "?" in text:
            return False
        low = text.lower()
        if not any(wort in low for wort in _SCHNELL) and not (
                "." in low and _DATUM_RE.search(low)):
            return False
        if not _ICH_RE.search(low):
            return False
        gid, uid = int(guild.id), int(getattr(autor, "id", 0) or 0)
        if not uid or self._ist_aus(gid, uid) or self._gesperrt(uid):
            return False
        # Ohne KI-Chat auf dem Server geht keine Zeile an die KI (siehe tick).
        if not features.is_on_in(gid, "ki"):
            return False
        # Der Termin haengt an der Wanduhr - kurz nach dem Start oder einem
        # Sprung weiss Flo nicht, welcher Tag 'morgen' ist.
        if not uhr.stabil():
            return False
        termin = zeitpunkt(text, self._jetzt())
        if termin is None or self._heikel(text):
            return False
        liste = self._kandidaten.setdefault(gid, [])
        if any(k["uid"] == uid and k["text"] == text for k in liste):
            return False
        liste.append({
            "uid": uid, "name": _sauber(getattr(autor, "display_name", "")),
            "text": text, "kanal": int(getattr(getattr(msg, "channel", None), "id", 0) or 0),
            "faellig": termin[0].timestamp(), "wann": termin[1], "mono": self._mono()})
        del liste[:-KANDIDATEN_MAX]
        return True

    # --- Schritt 2: einordnen ----------------------------------------------
    async def tick(self, client):
        """Gesammelte Kandidaten auswerten, Abgelaufenes wegwerfen."""
        if not self.is_enabled():
            return 0
        mono = self._mono()
        self._aufraeumen(mono)
        neu = 0
        # In der 429-Pause und in der Sendepause bleiben die Kandidaten liegen.
        if ai.hintergrund_pausiert() or admin.is_locked():
            return 0
        dran = []
        for gid, liste in list(self._kandidaten.items()):
            if not liste:
                continue
            # Nachhaken oder KI-Chat hier aus: dann geht auch keine Rohzeile
            # dieses Servers an die KI - die Kandidaten sind einfach weg.
            if not (features.is_on_in(gid, "nachhaken") and features.is_on_in(gid, "ki")):
                self._kandidaten.pop(gid, None)
                continue
            if mono - self._runde.get(gid, float("-inf")) >= RUNDE:
                dran.append(gid)
        dran.sort(key=lambda g: self._runde.get(g, float("-inf")))
        for gid in dran[:SERVER_JE_TAKT]:
            if ai.hintergrund_pausiert() or admin.is_locked():
                break
            self._runde[gid] = mono
            try:
                neu += await self._einordnen(gid)
            except Exception:  # noqa: BLE001 - ein Server darf die anderen nicht kippen
                log.exception("Nachhaken: Einordnen auf Server %s gescheitert", gid)
        return neu

    def _aufraeumen(self, mono):
        """Alte Kandidaten raus; Verfallenes und alte Tageszaehler (nur bei
        stabiler Uhr - sie haengen an der Wanduhr)."""
        for gid, liste in list(self._kandidaten.items()):
            liste[:] = [k for k in liste if mono - k["mono"] < KANDIDAT_TTL]
            if not liste:
                self._kandidaten.pop(gid, None)
        for uid, seit in list(self._notlage.items()):
            if mono - seit >= NOTLAGE_SPERRE:
                del self._notlage[uid]
        if not uhr.stabil():
            return
        jetzt = self._zeit()
        heute = uhr.tag_key(self._jetzt())
        geaendert = False
        for gid_text in list((self._store.data.get("guilds") or {}).keys()):
            try:
                eintrag = self._guild(int(gid_text))
            except (TypeError, ValueError):
                continue
            vorher = len(eintrag["offen"])
            eintrag["offen"] = [e for e in eintrag["offen"]
                                if _zahl(e.get("verfall")) > jetzt]
            alt = [u for u, tag in eintrag["gefragt"].items()
                   if uhr.spaeter_als(heute, uhr.glaubhaft(tag, heute))]
            for uid in alt:
                del eintrag["gefragt"][uid]
            geaendert |= bool(alt) or len(eintrag["offen"]) != vorher
        if geaendert:
            self._speichern()

    async def _einordnen(self, gid):
        """Bis zu BUENDEL Kandidaten -> KI -> offene Sachen. Rueckgabe: wie viele neu."""
        liste = self._kandidaten.get(gid) or []
        # SOFORT aus der Liste: scheitert der Aufruf, sind sie weg (wie beim
        # Gedaechtnis) - ein dauerhaft kaputter Aufruf schickte sonst ewig dieselben.
        paket = [k for k in liste[:BUENDEL]
                 if not self._ist_aus(gid, k["uid"]) and not self._gesperrt(k["uid"])]
        del liste[:BUENDEL]
        if not paket:
            return 0
        # Wer waehrend des Aufrufs 'vergiss mich' sagt, ist hinterher an der
        # Zahl zu erkennen - seine Zeilen sind dann schon aus _kandidaten raus,
        # vergiss() saehe sie nicht.
        vergessen = {k["uid"]: self._vergessen_nr(gid, k["uid"]) for k in paket}
        zeilen = "\n".join(f"{nr}. {k['text']}" for nr, k in enumerate(paket, 1))
        system = f"{_EXTRAKT_PROMPT} {ai.FloAI._GUARDRAIL}"
        with ai.guild_kontext(gid):
            try:
                antwort = await asyncio.wait_for(ai.generate(
                    zeilen, system=system, temperature=0.2, max_tokens=300,
                    hintergrund=True), KI_FRIST)
            except asyncio.TimeoutError:
                log.warning("Nachhaken: Einordnen auf Server %s brauchte zu lange.", gid)
                return 0
        if not antwort:
            return 0
        neu = 0
        for nr, was in self._urteile(antwort, len(paket)):
            kandidat = paket[nr - 1]
            uid = kandidat["uid"]
            # Inzwischen abgemeldet, vergessen oder am Boden: nicht merken.
            if (self._ist_aus(gid, uid) or self._gesperrt(uid)
                    or self._vergessen_nr(gid, uid) != vergessen[uid]):
                continue
            was = self._was(was)
            if not was or self._heikel(was):
                continue
            if self._eintragen(gid, kandidat, was):
                neu += 1
        if neu:
            self._speichern()
            log.info("Nachhaken: %d neue Sache(n) auf Server %s.", neu, gid)
        return neu

    @staticmethod
    def _urteile(antwort, anzahl):
        """[(nr, was)] aus der KI-Antwort - nur ok=true UND heikel=false.
        Fehlt 'heikel' oder ist es kein klares false, zaehlt es als heikel."""
        text = str(antwort or "")
        start, ende = text.find("["), text.rfind("]")
        if start < 0 or ende <= start:
            return []
        try:
            daten = json.loads(text[start:ende + 1])
        except ValueError:
            return []
        if not isinstance(daten, list):
            return []
        raus, gesehen = [], set()
        for d in daten:
            if not isinstance(d, dict):
                continue
            nr = d.get("nr")
            if isinstance(nr, str) and re.fullmatch(r"[0-9]{1,3}", nr.strip()):
                nr = int(nr.strip())
            if isinstance(nr, bool) or not isinstance(nr, int) or not 1 <= nr <= anzahl:
                continue
            if nr in gesehen or d.get("ok") is not True or d.get("heikel") is not False:
                continue
            if not isinstance(d.get("was"), str):
                continue
            gesehen.add(nr)
            raus.append((nr, d["was"]))
        return raus

    @staticmethod
    def _was(was):
        """Das Thema, so wie Flo es spaeter in den Mund nimmt - oder ""."""
        was = re.sub(r"\s+", " ", _WAS_WEG_RE.sub("", str(was or ""))).strip(" .,;:!-")
        if not 3 <= len(was) <= WAS_MAX:
            return ""
        return was

    def _eintragen(self, gid, kandidat, was):
        eintrag = self._guild(gid)
        offen = eintrag["offen"]
        uid = int(kandidat["uid"])
        meine = [e for e in offen if e.get("uid") == uid]
        if len(meine) >= PERSON_OFFEN_MAX:
            return False
        if any(_norm(e.get("was")) == _norm(was) for e in meine):
            return False
        offen.append({
            "id": secrets.token_hex(4), "uid": uid, "name": kandidat["name"],
            "was": was, "wann_text": kandidat["wann"], "kanal": kandidat["kanal"],
            "faellig_ab": kandidat["faellig"],
            "verfall": kandidat["faellig"] + VERFALL})
        del offen[:-OFFEN_MAX]
        return True

    # --- Schritt 3: nachfragen ---------------------------------------------
    async def vielleicht_nachfragen(self, msg):
        """Ist bei dieser Person etwas faellig? Dann hier nachfragen."""
        if not self.is_enabled():
            return False
        guild = getattr(msg, "guild", None)
        autor = getattr(msg, "author", None)
        if guild is None or autor is None or getattr(autor, "bot", False):
            return False
        gid, uid = int(guild.id), int(getattr(autor, "id", 0) or 0)
        offen = self._lies(gid).get("offen")
        if not offen or not any(isinstance(e, dict) and e.get("uid") == uid for e in offen):
            return False
        text = getattr(msg, "content", "") or ""
        # Wer in den letzten Tagen ein Notsignal geschrieben hat - egal in
        # welchem Kanal, egal wie lange der Verlauf dort reicht -, wird nicht
        # gefragt, wie die Pruefung lief (siehe notlage).
        if (not features.is_on_in(gid, "nachhaken") or admin.is_locked()
                or ai.instance.ist_notlage(text) or ai.trigger_re().search(text)
                or self._ist_aus(gid, uid) or self._gesperrt(uid) or not uhr.stabil()):
            return False
        jetzt = self._zeit()
        faellig = [e for e in offen if isinstance(e, dict) and e.get("uid") == uid
                   and _zahl(e.get("faellig_ab"), jetzt + 1) <= jetzt < _zahl(e.get("verfall"))]
        if not faellig:
            return False
        # Nicht im Aufraeum-Kanal, nicht ohne Schreibrecht - und dann OHNE zu
        # buchen: die naechste Nachricht in einem normalen Kanal fragt.
        kanal = getattr(msg, "channel", None)
        if not self._kanal_taugt(guild, kanal):
            return False
        # Was im Hinterzimmer angekuendigt wurde, fragt Flo nicht vor allen
        # anderen nach - auch ohne zu buchen: im passenden Kanal fragt er.
        faellig = [e for e in faellig if not self._hinterzimmer(guild, e, kanal)]
        if not faellig:
            return False
        eintrag = self._guild(gid)
        heute = uhr.tag_key(self._jetzt())
        gefragt = eintrag["gefragt"]
        if not uhr.spaeter_als(heute, uhr.glaubhaft(gefragt.get(str(uid)), heute)):
            return False                                # heute schon gefragt
        n_heute = self._server_heute(eintrag, heute)
        if n_heute >= SERVER_PRO_TAG:
            return False
        # Ist im Kanal gerade jemand am Boden, platzt Flo nicht mit "na, wie
        # war's?" dazwischen.
        if ai.instance._notlage_im_verlauf(ai.instance._recent(getattr(kanal, "id", 0))):
            return False
        # ERST buchen, dann fragen - alles bis hierher lief ohne await, eine
        # zweite Nachricht derselben Person findet den Eintrag nicht mehr.
        sache = min(faellig, key=lambda e: _zahl(e.get("faellig_ab")))
        eintrag["offen"] = [e for e in eintrag["offen"] if e is not sache]
        gefragt[str(uid)] = heute
        eintrag["heute"] = {"tag": heute, "n": n_heute + 1}
        self._speichern()
        vergessen = self._vergessen_nr(gid, uid)
        name = _sauber(getattr(autor, "display_name", "") or sache.get("name"))
        satz = await self._frage(gid, name, sache)
        # Die KI braucht ein paar Sekunden. Inzwischen 'vergiss mich' oder ein
        # Notsignal: dann ist die Sache weg und Flo sagt nichts.
        if self._gesperrt(uid) or self._vergessen_nr(gid, uid) != vergessen:
            return False
        # Inzwischen Sendepause oder Nachhaken aus: die Sache wieder hinlegen.
        if admin.is_locked() or not features.is_on_in(gid, "nachhaken"):
            self._zuruecklegen(gid, uid, sache, heute, vergessen)
            return False
        gesendet = await basis.antworte(msg, satz,
                                        allowed_mentions=discord.AllowedMentions.none())
        if gesendet is None:
            self._zuruecklegen(gid, uid, sache, heute, vergessen)
            return False
        # Die Antwort darauf ('war gut, ne 2') geht meist an Flo - die KI muss
        # dann wissen, was er gefragt hat (wie bei jeder KI-Antwort in bot.py).
        ai.note_message(getattr(kanal, "id", 0), ai.bot_name(gid), satz, is_bot=True)
        log.info("Nachhaken: auf Server %s nachgefragt.", gid)
        return True

    def _zuruecklegen(self, gid, uid, sache, tag, vergessen):
        """Die Nachfrage ging nicht raus: die Sache liegt wieder da, und der
        Server hat die Nachfrage gut. Die Person bleibt fuer heute gebucht -
        sonst kostete jede ihrer Nachrichten einen KI-Aufruf, solange das
        Senden scheitert. Abgemeldet, vergessen, Notsignal: weg ist weg."""
        eintrag = self._guild(gid)
        zaehler = eintrag["heute"]
        if zaehler.get("tag") == tag and isinstance(zaehler.get("n"), int) and zaehler["n"] > 0:
            zaehler["n"] -= 1
        if (not self._ist_aus(gid, uid) and not self._gesperrt(uid)
                and self._vergessen_nr(gid, uid) == vergessen
                and not any(e.get("id") == sache.get("id") for e in eintrag["offen"])):
            eintrag["offen"].append(sache)
        self._speichern()

    async def _frage(self, gid, name, sache):
        """Die Nachfrage: ein Satz von der KI - oder einer aus dem Ersatz-Pool."""
        was = str(sache.get("was") or "das Ding")
        system = (f"{_FRAGE_PROMPT.format(name=self.name_fuer(gid))} "
                  f"{ai.FloAI._GUARDRAIL}")
        prompt = (f"Person: {name}\nThema: {was}\n"
                  f"Angekuendigt mit: {sache.get('wann_text') or 'bald'}\n"
                  "Schreib jetzt deine Nachfrage.")
        satz = None
        if not features.is_on_in(gid, "ki"):
            # KI-Chat hier aus: dann auch keine KI-Zeile, nur der Ersatz-Pool.
            return self._ersatz(name, was)
        with ai.guild_kontext(gid):
            try:
                satz = await asyncio.wait_for(ai.generate(
                    prompt, system=system, temperature=0.9, max_tokens=80,
                    hintergrund=True), KI_FRIST)
            except asyncio.TimeoutError:
                satz = None
        return self._satz(satz) or self._ersatz(name, was)

    @staticmethod
    def _satz(text):
        """Die KI-Antwort, wenn sie taugt - sonst None."""
        text = re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'„“”")
        if not 8 <= len(text) <= SATZ_MAX:
            return None
        if (ai.instance._ist_verweigerung(text) or ai.instance.ist_notlage(text, text)
                or gehirn.Gehirn._heikel(text) or fun.ist_hetze(text)):
            return None
        # Erst NACH der Pruefung: ohne '@' saehe eine Mailadresse harmlos aus.
        return text.replace("@", "")

    @staticmethod
    def _ersatz(name, was):
        vorlage = random.choice(_ERSATZ)
        return vorlage.format(name=name or "Du", was=was, Was=was[:1].upper() + was[1:])

    # --- Befehle ------------------------------------------------------------
    async def handle(self, message):
        """'nachhaken aus|an' -> Bestaetigung, 'nachhaken' -> Stand; sonst None."""
        if not self._enabled:
            return None
        text = ai.strip_lead(getattr(message, "content", "") or "")
        erstes = text[:16].split(None, 1)
        if not erstes or erstes[0].lower().rstrip("?!.,") not in _ERSTE:
            return None
        gid = getattr(getattr(message, "guild", None), "id", 0)
        uid = getattr(getattr(message, "author", None), "id", 0)
        if not gid or not uid:
            return None
        m = _BEFEHL_RE.fullmatch(re.sub(r"\s+", " ", text.lower()).strip(" ?!.,"))
        if not m:
            return None
        wahl = m.group(1)
        if wahl in _AUS:
            if self._ist_aus(gid, uid):
                return ("Ist doch schon aus. Ich frag dich nix, versprochen – "
                        "hat mich eh nie interessiert.")
            weg = self.abmelden(gid, uid)
            text = ("Gut. Dann frag ich dich halt nie wieder, wie's war. "
                    "Interessiert mich eh nicht.")
            if weg:
                text += f" Die {weg} offene(n) Sache(n) hab ich gleich mit weggeworfen."
            return text
        if wahl:
            if not self._ist_aus(gid, uid):
                return "War doch nie aus. Ich hör schon die ganze Zeit zu."
            aus = self._guild(gid)["aus"]
            aus[:] = [u for u in aus if u != str(int(uid))]
            self._speichern()
            return ("Na gut. Ab jetzt merk ich mir wieder, was du ankündigst – "
                    "und frag nach, ob's was geworden ist.")
        if self._ist_aus(gid, uid):
            return (f"Nachhaken ist für dich aus. Was du vorhast, ist mir also egal. "
                    f"Wieder an: `{self._bot_name} nachhaken an`.")
        zeilen = self.zeilen_fuer(gid, uid)
        if not zeilen:
            return ("Gerade wart ich auf nix von dir. Kündig halt mal was an – "
                    f"und wenn du das nicht willst: `{self._bot_name} nachhaken aus`.")
        return ("Ich wart noch auf:\n" + "\n".join(f"• {z}" for z in zeilen)
                + f"\nKeine Lust drauf? `{self._bot_name} nachhaken aus`.")

    async def datenschutz(self, message):
        """Was jemand zum Loeschen sagt, gilt IMMER - auch wo Nachhaken oder das
        Gedaechtnis auf dem Server aus ist. Sonst ging der Satz an die KI (die
        dann frei erfindet, sie haette vergessen), und das Offene blieb liegen,
        bis jemand den Schalter wieder anmacht:
            'flo nachhaken aus'   wie immer: abmelden, alles Offene weg
            'flo vergiss mich'    nur, wenn das Gedaechtnis es nicht nimmt
                                  (Modul oder Schalter aus): das Offene weg -
                                  und mit dem Schalter aus auch, was das
                                  Gedaechtnis von frueher noch ueber einen weiss
        bot.py ruft das NACH gehirn.handle, ohne Schalter. Sonst None."""
        if not self.is_enabled():
            return None
        gid = getattr(getattr(message, "guild", None), "id", 0)
        uid = getattr(getattr(message, "author", None), "id", 0)
        if not gid or not uid:
            return None
        text = re.sub(r"\s+", " ", ai.strip_lead(getattr(message, "content", "") or "")
                      .lower()).strip(" ?!.,")
        befehl = _BEFEHL_RE.fullmatch(text)
        if befehl and befehl.group(1) in _AUS:
            return await self.handle(message)
        if not _VERGISS_RE.fullmatch(text):
            return None
        if gehirn.is_enabled() and features.is_on_in(gid, "gehirn"):
            return None                 # das Gedaechtnis fragt selbst nach
        if basis.erstes_ziel(message, ohne=[uid]) is not None:
            return None                 # fremde Leute vergisst nur das Gedaechtnis
        if gehirn.is_enabled():
            if gehirn.weiss_ueber(gid, uid):
                # Schalter aus, aber die Fakten von damals liegen noch da: dann
                # die Rueckfrage des Gedaechtnisses wie sonst auch (ein Klick,
                # dann ist beides weg, gezaehlt wird beides). Vorher blieben
                # sie liegen, und Flo behauptete, er wisse nichts.
                return await gehirn.handle(message)
            # Das Gedaechtnis weiss nichts, was verloren gehen koennte: sein
            # Puffer geht ohne Rueckfrage mit, gezaehlt wird nur das Offene
            # (das loescht gehirn.vergiss gleich mit).
            weg = gehirn.vergiss(gid, uid)
        else:
            weg = self.vergiss(gid, uid)
        if not weg:
            return "Über dich weiß ich sowieso nichts. Nimm dich nicht so wichtig."
        return (f"Vergessen. {weg} offene Sache(n) über dich sind weg – ich frag dich "
                "nix mehr. Als ob's mich je interessiert hätte.")

    def abmelden(self, gid, uid):
        """Opt-out: merken, dass die Person nicht will, und alles Offene weg."""
        weg = self.vergiss(gid, uid)
        aus = self._guild(gid)["aus"]
        if str(int(uid)) not in aus:
            aus.append(str(int(uid)))
        self._speichern()
        log.debug("Nachhaken: jemand hat sich auf Server %s abgemeldet.", gid)
        return weg

    # --- Notlage ------------------------------------------------------------
    def notlage(self, gid, uid):
        """Diese Person hat gerade ein Notsignal geschrieben (bot.py, fuer jede
        solche Nachricht - auch wo Nachhaken aus ist). Ein "na, wie lief die
        Pruefung?" waere danach immer falsch: alles Offene von ihr ist weg (auf
        jedem Server, auch die Kandidaten), und NOTLAGE_SPERRE lang merkt Flo
        sich nichts Neues von ihr und fragt nichts. Die Sperre steht nur im
        Speicher - auf der Platte liegt nichts darueber, wer am Boden war. Nach
        einem Neustart gibt es von ihr eh nichts Offenes mehr. Rueckgabe: wie
        viele offene Sachen weg sind."""
        if not self.is_enabled():
            return 0
        uid = int(uid or 0)
        if not uid:
            return 0
        self._notlage[uid] = self._mono()
        for liste in self._kandidaten.values():
            liste[:] = [k for k in liste if k["uid"] != uid]
        weg = 0
        alle = self._store.data.get("guilds")
        for eintrag in (alle.values() if isinstance(alle, dict) else ()):
            offen = eintrag.get("offen") if isinstance(eintrag, dict) else None
            if not isinstance(offen, list):
                continue
            rest = [e for e in offen if not (isinstance(e, dict) and e.get("uid") == uid)]
            weg += len(offen) - len(rest)
            eintrag["offen"] = rest
        if weg:
            self._speichern()
        log.debug("Nachhaken: Notsignal auf Server %s - %d offene Sache(n) verworfen.",
                  gid, weg)
        return weg

    # --- Fuer das Gedaechtnis -----------------------------------------------
    def vergiss(self, gid, uid):
        """Alles zu dieser Person auf diesem Server loeschen. Rueckgabe: wie
        viele offene Sachen weg sind - genau die, die 'gedaechtnis' zeigt.
        Kandidaten (noch nicht eingeordnete Zeilen, nur im Speicher) raeumt es
        mit weg, zaehlt sie aber nicht - wie gehirn seinen Puffer. Die
        Abmeldung ('nachhaken aus') bleibt stehen - sie ist ein Wunsch, kein
        Wissen ueber die Person."""
        if not self.is_enabled() or not gid:
            return 0
        gid, uid = int(gid), int(uid)
        # Zeilen, die gerade bei der KI sind, erkennt _einordnen hieran.
        self._vergessen[(gid, uid)] = self._vergessen_nr(gid, uid) + 1
        liste = self._kandidaten.get(gid) or []
        liste[:] = [k for k in liste if k["uid"] != uid]
        weg = 0
        if self._lies(gid):
            eintrag = self._guild(gid)
            vorher = len(eintrag["offen"])
            eintrag["offen"] = [e for e in eintrag["offen"] if e.get("uid") != uid]
            weg = vorher - len(eintrag["offen"])
            # Wann die Person gefragt wurde, ist Wissen ueber sie - der
            # Server-Deckel zaehlt getrennt ('heute') und haelt trotzdem.
            eintrag["gefragt"].pop(str(uid), None)
            self._speichern()
        return weg

    def zeilen_fuer(self, gid, uid):
        """Worauf Flo bei dieser Person noch wartet (fuer 'gedaechtnis')."""
        if not self.is_enabled() or not gid:
            return []
        offen = [e for e in (self._lies(gid).get("offen") or [])
                 if isinstance(e, dict) and e.get("uid") == int(uid)]
        offen.sort(key=lambda e: _zahl(e.get("faellig_ab")))
        zeilen = []
        for e in offen:
            ab = datetime.fromtimestamp(_zahl(e.get("faellig_ab")), uhr.TIMEZONE)
            zeilen.append(f"{e.get('was')} – frag ich ab {_KURZTAG[ab.weekday()]}, "
                          f"{ab:%d.%m. %H:%M}")
        return zeilen


instance = Nachhaken()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
note_message = instance.note_message
vielleicht_nachfragen = instance.vielleicht_nachfragen
tick = instance.tick
vergiss = instance.vergiss
notlage = instance.notlage
zeilen_fuer = instance.zeilen_fuer
abmelden = instance.abmelden
datenschutz = instance.datenschutz
