"""Zitat der Woche: Rechtsklick -> "Als Zitat speichern", sonntags wird abgestimmt.

Wie es sich anfuehlt
====================
    Rechtsklick -> Apps -> Als Zitat speichern
    flo zitat speichern        (als Antwort auf die Nachricht) dasselbe
    flo zitate                 die Kandidaten, ueber die als Naechstes abgestimmt wird
    flo zitat der woche        der letzte Sieger
    flo zitat weg 3            Kandidat 3 raus - darf, wer ihn gespeichert hat,
                               wer ihn gesagt hat, und die Mods
    flo zitat umfrage jetzt    nur der Besitzer: die Abstimmung JETZT (Testausloeser)

Sonntags ab 12 Uhr (nachgeholt bis Montag 12 Uhr) startet Flo im Zitat-Kanal
(guildcfg 'zitat_channel', sonst die Ansagen, sonst der System-Kanal - nie ein
Aufraeum-Kanal: dort loescht bot.py auch Flos Posts nach einer Minute, die
Umfrage waere weg und Discord schickte nie ein Ergebnis; auf dem Hauptserver
ist der Ansagen-Kanal ab Werk genau so einer) eine echte Discord-Umfrage, 8
Stunden lang: bis zu zehn Kandidaten, hoechstens zwei je Person (bei mehr eine
feste Zufallsauswahl), jede Antwort nummeriert und auf Discords 55 Zeichen
gekuerzt ("3 · „…“ – Bob") - die vollen Zitate stehen im Text darueber. Gibt es
nur einen Kandidaten, gewinnt er konkurrenzlos; gibt es keinen, kommt hoechstens
eine Zeile (und nur, wenn hier in den letzten vier Wochen ueberhaupt etwas
gespeichert wurde). Ohne das Recht "Umfragen erstellen" entscheidet Flo allein.

Ist die Umfrage vorbei, postet Discord ein Ergebnis (poll_result) - bot.py
reicht es hierher (ist_eigene_umfrage / ergebnis), nicht an den normalen
Umfrage-Kommentar. Den Sieger liest Flo aus den Feldern des Ergebnisses
(victor_answer_id/-text); bei Gleichstand holt er die Umfrage
(fetch_message) und entscheidet unter den Besten per Zufall. War Flo offline,
als das Ergebnis kam, holt der Takt die Umfrage 30 Minuten nach dem Ende selbst
und wertet sie aus, sobald Discord sie abgeschlossen hat.

Blieb das Senden der Umfrage ohne Antwort (Netz weg, Zeitueberschreitung, Takt
abgebrochen), weiss keiner, ob sie drueben ist: 'umfrage' bleibt ohne mid
stehen. Kommt ihr Ergebnis, nimmt ergebnis() es an (gleiche Frage, gleicher
Kanal, keine der alten); vorher sieht der Takt nach UNKLAR_NACH im Kanal nach.
Gefunden -> laeuft normal weiter; sicher nicht da -> die Runde ist wieder frei;
nicht nachsehbar -> nach AUFGEBEN_NACH frei, die Kandidaten wandern weiter.

Der Sieger bekommt eine Zitat-Karte (render.quote_card mit Profilbild), einen
Kommentar (KI im Hintergrund, mit Grenze, sonst Ersatz-Pool) und als Einziger
einen Ping. Wer gerade ein Notsignal geschrieben hat (basis merkt sich das
72 Stunden im Speicher), kommt gar nicht erst zur Wahl - und gewinnt jemand,
der waehrend der Abstimmung eins schreibt: keine KI, kein Spott, kein Ping.

Was nicht gespeichert wird: Bot-Nachrichten, die eigene Nachricht ("Eigenlob
stinkt"), unter 3 oder ueber 300 Zeichen, doppelt, Notsignale
(ai.FloAI._NOTLAGE_RE - neutral abgelehnt, nie mit Spott), Hetze
(fun.ist_hetze), private Daten (gehirn._heikel) und alles aus dem
Hinterzimmer: aus einem NSFW-Kanal, einem privaten Thread oder einem Kanal,
den nicht jeder lesen kann, der den Zitat-Kanal liest (je Rolle gemessen,
Bot-Rollen zaehlen nicht - so geht es auch auf Servern, auf denen @everyone
nichts sieht). Dieselbe Pruefung beim Start der Umfrage und in 'flo zitate'
(dort fehlt so ein Kandidat, seine Nummer bleibt frei). Hoechstens 50 offene
Kandidaten je Server, 10 je Person.

Wo was aufgerufen wird:
    handle(message)                 Handler-Kette, nach umfrage und VOR media -
                                    sonst macht media aus 'flo zitat der woche'
                                    ein Quote-Meme
    rechtsklick(interaction, msg)   Apps-Menue "Als Zitat speichern", nachdem
                                    bot._rk_darf Server, Sendepause und Schalter
                                    geprueft hat
    ist_eigene_umfrage(msg) -> bool bot.on_message bei einem Umfrage-Ergebnis
                                    von Flo: gehoert es zur Zitat-Abstimmung?
    ergebnis(msg)                   ... dann landet es hier statt bei umfrage.
                                    Die Sendepause prueft bot.py hier NICHT
                                    (anders als bei normalen Umfragen, deren
                                    Ergebnis dann verfaellt) - der Sieger darf
                                    nicht verloren gehen: in der Sendepause
                                    nichts posten, tick() holt es nach
    tick(client)                    bot.alltag_loop, jede Minute (hoechstens
                                    bot.ALLTAG_FRIST, dann abgebrochen) - auch
                                    hier selbst admin.is_locked() fragen

Die Uhr (uhr.py): die Umfrage startet nur bei stabiler Uhr, und 'gestartet'
(ISO-Woche) laeuft nur vorwaerts - nach einem Uhrsprung kommt keine zweite.
'gestartet' steht auf der Platte, BEVOR die Umfrage rausgeht. Was nach dem
Start gespeichert wird, gehoert zur naechsten Runde; faellt ein Sonntag ganz
aus, wandern die Kandidaten in die naechste Abstimmung. Der Testausloeser
verbraucht ohne Kandidaten keine Runde.

'zitate' ist seit dem Fundament ein eigener Befehl (cmdnorm.KNOWN). Vorher
machte die Tippfehler-Korrektur daraus 'zitat' und damit media's Quote-Meme.
'zitat' allein gehoert weiter media: 'flo zitat <text>' und 'flo zitat' als
Antwort machen das Meme wie immer - zitate nimmt davon nur die eigenen Formen
(alle in befehl(), vollstaendig im README unter "Was sich fuer das Quote-Meme
geaendert hat").

Daten: data/zitate.json
    {"guilds": {"<gid>": {
        "wochen": {"2026-W43": [{"mid", "cid", "uid", "name", "text", "von",
                                 "von_name", "ts"}]},
        "umfrage": {"woche", "mid", "cid", "kandidaten": [...], "ende_ts", "art",
                    "sieger": null|Index|-1|-2, "stimmen", "gesamt", "verkuendet",
                    "versuche", "start_ts", "vorher"},
        "alte": [mid, ...], "gestartet": "2026-W42", "sieger": [...]}}}

Beim Import passiert nichts.
"""

import asyncio
import datetime as _dt
import logging
import os
import random
import re
import time

import discord

import ai
import basis
from basis import FeatureBasis
import features
import uhr
from store import JsonStore

log = logging.getLogger("dcbot.zitate")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
# 'zitat' allein gehoert weiter media (Quote-Meme) - zitate nimmt davon nur
# die eigenen Formen ('zitat der woche', 'zitat weg 3', 'zitat speichern').
_CMDS = ("zitate", "zitat")
_ERSTE = frozenset(_CMDS)

#: Knoepfe, die einen Neustart ueberleben. Keine - abgestimmt wird per Discord-Umfrage.
DYNAMISCHE_KNOEPFE = ()

FRAGE = "🏆 Zitat der Woche – wer hat den größten Mist gelabert?"
#: Sonntag ab dieser Stunde; nachgeholt bis Montag zur selben Stunde.
START_STUNDE = 12
UMFRAGE_STUNDEN = 8
#: Discords Grenzen: hoechstens 10 Antworten mit je 55 Zeichen.
MAX_ANTWORTEN = 10
ANTWORT_MAX = 55
#: Hoechstens so viele Kandidaten derselben Person kommen in eine Umfrage.
JE_PERSON = 2
MIN_ZEICHEN = 3
MAX_ZEICHEN = 300
#: Offene Kandidaten je Server bzw. je Person, die speichert.
MAX_KANDIDATEN = 50
MAX_JE_SPEICHERER = 10
#: So lange nach dem Ende holt der Takt eine Umfrage selbst, falls das Ergebnis
#: nie ankam (Flo war offline) - und gibt nach AUFGEBEN_NACH auf.
NACHSEHEN_NACH = 30 * 60.0
AUFGEBEN_NACH = 3 * 86400.0
#: Blieb das Senden der Umfrage ohne Antwort (Netz weg, Zeitueberschreitung,
#: Takt abgebrochen), sieht Flo nach so langer Zeit im Kanal nach, ob sie drueben ist.
UNKLAR_NACH = 5 * 60.0
#: Ein Post, den Discord ablehnt: so oft nochmal, im Abstand von NOCHMAL_NACH.
VERSUCHE = 4
NOCHMAL_NACH = 15 * 60.0
#: So viele Wochen bleiben gespeichert, so viele Sieger, so viele alte Umfragen.
WOCHEN_BEHALTEN = 6
SIEGER_BEHALTEN = 20
ALTE_BEHALTEN = 10
#: Hoechstens so lange wartet ein Post auf die KI (Sekunden).
KI_FRIST = 30.0
PIN = "📌"

_RAND = " \t.,!?;:\"'„“”`"
_NUMMER_RE = re.compile(r"[0-9]{1,3}")
_ANTWORT_NR_RE = re.compile(r"^\s*([0-9]{1,2})\s*·")
_SPEICHERN = frozenset(("speichern", "merken", "speicher", "merk"))
_WEG = frozenset(("weg", "löschen", "loeschen", "entfernen", "raus", "delete"))
_DER_WOCHE = frozenset(("der woche", "dieser woche", "der letzten woche",
                        "letzte woche", "von letzter woche"))
_UMFRAGE_JETZT = frozenset(("umfrage jetzt", "abstimmung jetzt", "umfrage starten"))

# --- Texte ---------------------------------------------------------------------
_GESPEICHERT = f"{PIN} Gespeichert. Sonntag wird abgestimmt."
_ABGELEHNT = {
    "bot": "Bot-Nachrichten sind keine Zitate. Auch meine nicht – meine sind zu gut für euch.",
    "eigen": "Eigenlob stinkt. Lass das andere entscheiden.",
    "leer": "Da steht nix drin. Bilder und Sticker zitier ich nicht.",
    "kurz": "Unter drei Zeichen? Das ist kein Zitat, das ist ein Grunzen.",
    "lang": "Über 300 Zeichen? Das ist kein Zitat, das ist ein Roman. Kürzer, bitte.",
    # Notsignal: neutral, nie mit Spott - und ohne zu sagen, warum.
    "notlage": "Das speicher ich nicht. Such dir was anderes aus.",
    "hetze": "Menschenfeindlichen Müll stell ich nicht zur Wahl.",
    "heikel": "Da stehen Links oder private Sachen drin – das häng ich nicht öffentlich aus.",
    "doppelt": "Hat schon wer gespeichert. Ihr seid euch ja einig.",
    "voll": "Die Runde ist voll – 50 Kandidaten reichen, ihr Quasselstrippen.",
    "zu_viele": "Du hast für diese Runde schon 10 gespeichert. Lass auch mal andere ran.",
    "fremd": "Nur Nachrichten von diesem Server, du Grenzgänger.",
    # Privater Kanal, Mod-Kanal, NSFW: die zitierte Person hat sich ihr
    # Publikum nicht ausgesucht - ohne Spott ueber sie.
    "privat": ("Das kommt aus einem Kanal, den nicht jeder lesen kann. Was im Hinterzimmer "
               "gesagt wird, bleibt im Hinterzimmer."),
}
_NICHT_ALS_ANTWORT = ("Worauf denn? Antworte mit `{name} zitat speichern` auf die Nachricht – "
                      "oder Rechtsklick → Apps → Als Zitat speichern.")
_NICHT_GEFUNDEN = "Die Nachricht find ich nicht mehr. Zu langsam gewesen."
_KEINE_KANDIDATEN = ("Noch keine Kandidaten. Rechtsklick auf den dümmsten Satz → Apps → "
                     "Als Zitat speichern. Sonntag wird abgestimmt.")
_KEIN_SIEGER = ("Gab noch kein Zitat der Woche. Speichert halt mal was: Rechtsklick → "
                "Apps → Als Zitat speichern.")
_WELCHE_NUMMER = "Welche Nummer? `{name} zitate` zeigt die Liste."
_GIBTS_NICHT = "Nummer {nr} gibt's nicht. `{name} zitate` zeigt die Liste."
_DARFST_NICHT = ("Das darfst du nicht. Rausnehmen darf, wer's gespeichert hat, wer's "
                 "gesagt hat – und die Mods.")
_WEG_TEXT = ("Weg. War eh nicht lustig.", "Rausgeflogen. Die Welt wird's verkraften.")
_NUR_CHEF = "Den Knopf drückt nur der Chef. Abgestimmt wird sonntags, wie immer."

_UMFRAGE_KOPF = ("🏆 **Zitat der Woche** – stimmt ab, wer diese Woche den größten Mist "
                 "gelabert hat. Ihr habt {stunden} Stunden.")
_LEER_ZEILE = (
    "🏆 Zitat der Woche? Diese Woche hat keiner was gespeichert. Entweder habt ihr nur "
    "langweiligen Müll gelabert, oder ihr seid zu faul zum Rechtsklicken.",
    "🏆 Kein einziges Zitat diese Woche. Sieben Tage Chat, und nichts davon war der Rede "
    "wert. Respekt.",
)
_KEINE_STIMMEN = ("🏆 **Zitat der Woche** – null Stimmen. Dann gewinnt halt keiner. "
                  "Ihr wart es nicht wert.")
_KOPF = {
    "umfrage": "🏆 **Zitat der Woche** – <@{uid}> gewinnt mit {stimmen} von {gesamt} Stimmen.",
    "gleichstand": "🏆 **Zitat der Woche** – Gleichstand, ich entscheide: <@{uid}>.",
    "konkurrenzlos": ("🏆 **Zitat der Woche** – konkurrenzlos, weil ihr nur eins gespeichert "
                      "habt: <@{uid}>."),
    "ohne_umfrage": ("🏆 **Zitat der Woche** – ich darf hier keine Umfragen machen (Recht "
                     "„Umfragen erstellen“ fehlt), also entscheide ich allein: <@{uid}>."),
}
_ERSATZ = (
    "Von allem Unsinn, der diese Woche hier gelabert wurde, war das der lauteste. "
    "Glückwunsch, {name} – das ist kein Kompliment.",
    "{name}, der Server hält diesen Satz für deinen besten. Lass das mal sacken.",
    "Eine ganze Woche Chat, und das ist das Highlight. Ich mach mir Sorgen um euch. "
    "Und um dich, {name}.",
    "Gerahmt und für die Ewigkeit, {name}. Deine Enkel werden sich schämen.",
)
#: Statt des Kommentars, wenn der Sieger gerade ein Notsignal geschrieben hat.
_SANFT = "Glückwunsch. Heute ausnahmsweise ohne Spruch."


def _sendepause():
    try:
        import admin
        return bool(admin.is_locked())
    except Exception:  # noqa: BLE001 - ohne admin keine Sendepause
        return False


def _besitzer(uid):
    try:
        import admin
        return int(uid or 0) == int(admin.OWNER_ID)
    except Exception:  # noqa: BLE001
        return False


def _mono():
    return time.monotonic()


def _zeit():
    """Die Wanduhr als Zahl (Tests stellen sie)."""
    return time.time()


def _ganz(wert):
    try:
        return int(wert or 0)
    except (TypeError, ValueError):
        return 0


def _einzeilig(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _kappen(text, grenze):
    """Auf eine Laenge kuerzen - mit '…' statt mitten im Wort."""
    text = _einzeilig(text)
    if len(text) <= grenze:
        return text
    if grenze <= 1:
        return "…"[:grenze]
    kurz = text[:grenze - 1].rstrip()
    leer = kurz.rfind(" ")
    if leer > grenze // 2:
        kurz = kurz[:leer].rstrip(" ,;:-")
    return kurz + "…"


def _sauber(text, laenge=300):
    """Fuer den Chat: gekuerzt, ohne Markdown und ohne Ping."""
    return discord.utils.escape_markdown(_kappen(text, laenge)).replace("@", "@\u200b")


def _notlage(text):
    """Notsignal? Darauf reagiert nichts, was Flo von sich aus tut."""
    return bool(ai.FloAI._NOTLAGE_RE.search(str(text or "")))


def _hetze(text):
    try:
        import fun
        return bool(fun.ist_hetze(text))
    except Exception:  # noqa: BLE001 - ohne Erkennung entscheidet die KI-Grenze
        return False


def _heikel(text):
    try:
        import gehirn
        return bool(gehirn.Gehirn._heikel(text))
    except Exception:  # noqa: BLE001
        return False


# Die Regeln fuer alles, was Flo ungefragt postet, stehen in basis.py - hier
# nur unter den alten Namen (die Aufrufstellen und Tests kennen sie so).
#: Hat die Person gerade ein Notsignal geschrieben? Dann keine Abstimmung ueber
#: ihren "Mist" und kein Spott, wenn sie gewinnt.
_kuerzlich_notlage = basis.kuerzlich_notlage
_leser = basis.leser
#: Ein Zitat aus einem Kanal, den nicht jeder lesen kann (Mod-Kanal, privater
#: Thread, NSFW), haengt Flo nicht dort aus, wo es andere lesen.
_privat = basis.privat
_darf_schreiben = basis.darf_schreiben
kanal_ohne_aufraeumen = basis.kanal_ohne_aufraeumen


def _quelle(guild, k):
    """Der Kanal, aus dem ein gespeicherter Kandidat stammt - None, wenn es ihn
    nicht (mehr) gibt."""
    cid = _ganz(k.get("cid"))
    if not cid or guild is None:
        return None
    holen = getattr(guild, "get_channel_or_thread", None) or getattr(guild, "get_channel", None)
    try:
        return holen(cid) if holen is not None else None
    except Exception:  # noqa: BLE001
        return None


def antwort_text(nr, kandidat):
    """'3 · „Pizza ist ein Gemüse“ – Bob', hoechstens ANTWORT_MAX Zeichen. Das
    Zitat wird gekuerzt, nicht der Name (der wird nur kurz gehalten)."""
    name = _kappen(kandidat.get("name") or "?", 14)
    vorne, hinten = f"{nr} · „", f"“ – {name}"
    platz = ANTWORT_MAX - len(vorne) - len(hinten)
    return f"{vorne}{_kappen(kandidat.get('text') or '', max(1, platz))}{hinten}"[:ANTWORT_MAX]


def faellige_woche(jetzt):
    """Die ISO-Woche, deren Abstimmung gerade dran ist: Sonntag ab
    START_STUNDE - oder (nachgeholt) Montag bis START_STUNDE die Woche davor.
    Sonst None."""
    if jetzt.weekday() == 6 and jetzt.hour >= START_STUNDE:
        return uhr.woche_key(jetzt)
    if jetzt.weekday() == 0 and jetzt.hour < START_STUNDE:
        return uhr.woche_key(jetzt - _dt.timedelta(days=1))
    return None


def auswahl(kandidaten, seed):
    """Bis MAX_ANTWORTEN Kandidaten, hoechstens JE_PERSON derselben Person -
    bei mehr eine feste Zufallsauswahl (gleicher Seed, gleiches Ergebnis).
    Reihenfolge danach: wie gespeichert."""
    gemischt = list(kandidaten)
    random.Random(str(seed)).shuffle(gemischt)
    je_person, raus = {}, []
    for k in gemischt:
        uid = _ganz(k.get("uid"))
        if je_person.get(uid, 0) >= JE_PERSON:
            continue
        je_person[uid] = je_person.get(uid, 0) + 1
        raus.append(k)
        if len(raus) >= MAX_ANTWORTEN:
            break
    raus.sort(key=lambda k: (float(k.get("ts") or 0), _ganz(k.get("mid"))))
    return raus


class Zitate(FeatureBasis):
    """Zitat-Kandidaten und die Sonntags-Abstimmung."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._lock = None
        # gid -> monotonic: ab wann ein gescheiterter Post/Start nochmal darf
        self._nochmal_ab = {}
        # gid -> Woche, fuer die "kein Kanal" schon im Log steht
        self._ohne_kanal = {}
        # gids, deren Umfrage gerade gesendet wird (dann wartet _ausstehend)
        self._sendet = set()

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if os.getenv("ZITATE_ENABLED", "1").strip().lower() in (
                "0", "false", "no", "off", "aus"):
            log.info("Zitat der Woche aus (ZITATE_ENABLED=0).")
            return False
        self._store = JsonStore("zitate.json", default={"guilds": {}})
        self._lock = None
        self._nochmal_ab = {}
        self._ohne_kanal = {}
        self._sendet = set()
        self._enabled = True
        log.info("Zitat der Woche aktiv (%d Server mit Zitaten).", len(self._guilds()))
        return True

    def is_enabled(self):
        return self._enabled

    def _sperre(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # --- Speicher ---------------------------------------------------------
    def _guilds(self):
        guilds = self._store.data.setdefault("guilds", {})
        if not isinstance(guilds, dict):
            guilds = self._store.data["guilds"] = {}
        return guilds

    def _guild(self, gid, *, anlegen=True):
        guilds = self._guilds()
        g = guilds.get(str(int(gid)))
        if not isinstance(g, dict):
            if not anlegen:
                return None
            g = guilds[str(int(gid))] = {}
        if not isinstance(g.get("wochen"), dict):
            g["wochen"] = {}
        for feld in ("sieger", "alte"):
            if not isinstance(g.get(feld), list):
                g[feld] = []
        if not isinstance(g.get("gestartet"), str):
            g["gestartet"] = ""
        if not isinstance(g.get("umfrage"), dict):
            g["umfrage"] = {}
        return g

    def sammel_woche(self, g, jetzt=None):
        """In welche Runde ein Zitat, das JETZT gespeichert wird, gehoert: die
        laufende ISO-Woche - ausser ihre Abstimmung lief schon, dann die naechste."""
        woche = uhr.woche_key(jetzt or uhr.jetzt())
        gestartet = uhr.glaubhaft(g.get("gestartet"), woche)
        if uhr.spaeter_als(woche, gestartet):
            return woche
        return uhr.naechster_key(gestartet) or woche

    def offene(self, g, bis=None):
        """Alle Kandidaten, ueber die noch nicht abgestimmt wurde (Wochen nach
        'gestartet', hoechstens bis 'bis'), in der Reihenfolge des Speicherns."""
        gestartet = g.get("gestartet") or ""
        raus = []
        for woche, liste in g["wochen"].items():
            if not uhr.spaeter_als(woche, gestartet):
                continue
            if bis and uhr.spaeter_als(woche, bis):
                continue
            raus.extend(k for k in (liste if isinstance(liste, list) else ())
                        if isinstance(k, dict) and str(k.get("text") or "").strip())
        raus.sort(key=lambda k: (float(k.get("ts") or 0), _ganz(k.get("mid"))))
        return raus

    def _aufraeumen(self, g, jetzt):
        """Alte Wochen weg (WOCHEN_BEHALTEN)."""
        grenze = uhr.woche_key(jetzt - _dt.timedelta(weeks=WOCHEN_BEHALTEN))
        for woche in list(g["wochen"]):
            if not uhr.spaeter_als(woche, grenze) or not g["wochen"][woche]:
                del g["wochen"][woche]

    # --- Speichern -------------------------------------------------------------
    def pruefen(self, g, nachricht, von_uid, ziel=None):
        """Darf diese Nachricht Kandidat werden? None = ja, sonst der Grund
        (Schluessel in _ABGELEHNT). 'ziel' ist der Kanal, in dem abgestimmt
        wird (ohne: @everyone)."""
        autor = getattr(nachricht, "author", None)
        if autor is None or getattr(autor, "bot", False):
            return "bot"
        text = self._text(nachricht)
        # Ein Notsignal zuerst - darauf antwortet Flo nur neutral, egal was sonst
        # an der Nachricht ist. Auch im Namen: der stuende sonst in der Umfrage.
        if _notlage(text) or _notlage(getattr(autor, "display_name", "")):
            return "notlage"
        if _privat(getattr(nachricht, "channel", None), ziel, getattr(nachricht, "guild", None)):
            return "privat"
        if int(getattr(autor, "id", 0)) == int(von_uid):
            return "eigen"
        if not text:
            return "leer"
        if _hetze(text):
            return "hetze"
        if _heikel(text):
            return "heikel"
        if len(text) < MIN_ZEICHEN:
            return "kurz"
        if len(text) > MAX_ZEICHEN:
            return "lang"
        mid = _ganz(getattr(nachricht, "id", 0))
        laufend = (g.get("umfrage") or {}).get("kandidaten") or []
        for liste in list(g["wochen"].values()) + [laufend]:
            if any(isinstance(k, dict) and _ganz(k.get("mid")) == mid
                   for k in (liste if isinstance(liste, list) else ())):
                return "doppelt"
        offen = self.offene(g)
        if len(offen) >= MAX_KANDIDATEN:
            return "voll"
        if sum(1 for k in offen if _ganz(k.get("von")) == int(von_uid)) >= MAX_JE_SPEICHERER:
            return "zu_viele"
        return None

    @staticmethod
    def _text(nachricht):
        """Was zitiert wird: der Text mit Namen statt <@123> (clean_content)."""
        try:
            text = getattr(nachricht, "clean_content", None)
        except Exception:  # noqa: BLE001 - Attrappen ohne Cache
            text = None
        if not isinstance(text, str):
            text = getattr(nachricht, "content", "") or ""
        return text.strip()

    def speichern(self, gid, nachricht, von):
        """Kandidat anlegen. (True, Text) oder (False, Abfuhr)."""
        g = self._guild(gid)
        guild = getattr(nachricht, "guild", None)
        if guild is not None and _ganz(getattr(guild, "id", 0)) not in (0, int(gid)):
            return False, _ABGELEHNT["fremd"]
        ziel = None
        if guild is not None:
            import laufzeit
            ziel = self._kanal(laufzeit.client, guild)
        grund = self.pruefen(g, nachricht, von.id, ziel)
        if grund is not None:
            return False, _ABGELEHNT[grund]
        jetzt = uhr.jetzt()
        woche = self.sammel_woche(g, jetzt)
        autor = nachricht.author
        g["wochen"].setdefault(woche, []).append({
            "mid": int(nachricht.id),
            "cid": _ganz(getattr(getattr(nachricht, "channel", None), "id", 0)),
            "uid": int(autor.id),
            "name": str(getattr(autor, "display_name", "") or "?")[:64],
            "text": _einzeilig(self._text(nachricht))[:MAX_ZEICHEN],
            "von": int(von.id),
            "von_name": str(getattr(von, "display_name", "") or "?")[:64],
            "ts": _zeit(),
        })
        self._aufraeumen(g, jetzt)
        self._store.save_soon()
        log.info("Zitat gespeichert auf Server %s (Runde %s).", gid, woche)
        return True, _GESPEICHERT

    @staticmethod
    async def _anpinnen(nachricht):
        """📌 an die Nachricht - Deko, darf scheitern."""
        try:
            await nachricht.add_reaction(PIN)
        except Exception:  # noqa: BLE001 - fehlendes Recht, geloescht, Attrappe
            log.debug("Zitat: Reaktion ging nicht", exc_info=True)

    async def rechtsklick(self, interaction, nachricht):
        """Eine Nachricht als Kandidaten speichern - Antwort sofort (3-Sekunden-
        Frist), die Reaktion danach."""
        if not self._enabled or self._store is None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Zitate gibt's hier noch nicht. Merk's dir halt selber.",
                        ephemeral=True)
            except discord.HTTPException:
                pass
            return
        gid = _ganz(getattr(interaction, "guild_id", 0))
        ok, text = self.speichern(gid, nachricht, interaction.user)
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            log.debug("Zitat: Antwort auf den Rechtsklick ging nicht", exc_info=True)
        if ok:
            await self._anpinnen(nachricht)

    # --- Befehle ----------------------------------------------------------------
    @staticmethod
    def befehl(text):
        """('liste',) | ('sieger',) | ('speichern',) | ('weg', nr|None) |
        ('umfrage',) | None (= kein Befehl: media oder die KI ist dran)."""
        sauber = " ".join(str(text or "").lower().split()).strip(" \t,!?;:\"'„“”`")
        teile = sauber.split(None, 1)
        if not teile:
            return None
        wort = teile[0].strip(_RAND)
        if wort not in _ERSTE:
            return None
        rest = (teile[1] if len(teile) > 1 else "").strip(_RAND)
        if wort == "zitate" and rest in ("", "liste", "anzeigen", "zeigen", "kandidaten"):
            return ("liste",)
        if rest in _DER_WOCHE:
            return ("sieger",)
        if rest in _SPEICHERN:
            return ("speichern",)
        if rest in _UMFRAGE_JETZT:
            return ("umfrage",)
        weg = rest.split()
        if weg and weg[0] in _WEG:
            if len(weg) == 1:
                return ("weg", None)
            if len(weg) == 2:
                nr = weg[1].lstrip("#").strip(_RAND)
                if _NUMMER_RE.fullmatch(nr):
                    return ("weg", int(nr))
        return None

    async def handle(self, message):
        """'zitate', 'zitat der woche', 'zitat weg 3' -> Antwort; sonst None."""
        if not self._enabled:
            return None
        inhalt = getattr(message, "content", "") or ""
        if "zitat" not in inhalt.lower():
            return None             # jede Chatnachricht kommt hier vorbei - schnell raus
        text = ai.strip_lead(inhalt)
        erstes = text[:10].split(None, 1)
        if not erstes or erstes[0].lower().strip(_RAND) not in _ERSTE:
            return None
        guild = getattr(message, "guild", None)
        if guild is None or getattr(message, "author", None) is None:
            return None
        befehl = self.befehl(text)
        if befehl is None:
            return None
        art = befehl[0]
        g = self._guild(guild.id)
        if art == "liste":
            return self._liste(g, guild, getattr(message, "channel", None))
        if art == "sieger":
            return self._letzter_sieger(g)
        if art == "speichern":
            return await self._speichern_befehl(message)
        if art == "weg":
            return self._weg(g, message, befehl[1])
        return await self._jetzt_befehl(message, guild)

    def _liste(self, g, guild=None, hier=None):
        """Die offenen Kandidaten, nummeriert wie fuer 'zitat weg <nr>'. Was aus
        einem Kanal stammt, den hier nicht jeder lesen kann, fehlt (nur
        gezaehlt) - seine Nummer bleibt frei, damit 'weg <nr>' stimmt."""
        offen = self.offene(g)
        if not offen:
            return _KEINE_KANDIDATEN
        zeilen, laenge, versteckt, merker = [], 0, 0, {}
        for nr, k in enumerate(offen, 1):
            if _privat(_quelle(guild, k), hier, guild, merker):
                versteckt += 1
                continue
            zeile = (f"**{nr}** „{_sauber(k.get('text'), 120)}“ – "
                     f"{_sauber(k.get('name'), 32)}")
            if laenge + len(zeile) > 1700:
                zeilen.append(f"… und {len(offen) - nr + 1} weitere.")
                break
            zeilen.append(zeile)
            laenge += len(zeile) + 1
        if versteckt:
            zeilen.append(f"(+{versteckt} aus Kanälen, die hier nicht jeder lesen kann – "
                          "die zeig ich hier nicht.)")
        return (f"{PIN} **Kandidaten fürs Zitat der Woche** ({len(offen)}) – Sonntag ab "
                f"{START_STUNDE} Uhr wird abgestimmt:\n" + "\n".join(zeilen))

    def _letzter_sieger(self, g):
        sieger = next((s for s in g["sieger"] if isinstance(s, dict)), None)
        if sieger is None:
            return _KEIN_SIEGER
        woche = str(sieger.get("woche") or "")
        kw = woche.split("-W")[-1].lstrip("0") if "-W" in woche else "?"
        stimmen = ""
        if _ganz(sieger.get("gesamt")) > 0:
            stimmen = f" ({_ganz(sieger.get('stimmen'))} von {_ganz(sieger.get('gesamt'))} Stimmen)"
        return (f"🏆 Zitat der Woche (KW {kw}): „{_sauber(sieger.get('text'))}“ – "
                f"**{_sauber(sieger.get('name'), 40)}**{stimmen}. Ein Meisterwerk. Nicht.")

    async def _speichern_befehl(self, message):
        bezug = getattr(message, "reference", None)
        if bezug is None or not getattr(bezug, "message_id", None):
            return _NICHT_ALS_ANTWORT.format(name=self._bot_name)
        ziel = getattr(bezug, "resolved", None)
        if not isinstance(ziel, discord.Message):
            try:
                ziel = await message.channel.fetch_message(bezug.message_id)
            except Exception:  # noqa: BLE001 - geloescht, kein Leserecht, Attrappe
                return _NICHT_GEFUNDEN
        ok, text = self.speichern(message.guild.id, ziel, message.author)
        if ok:
            await self._anpinnen(ziel)
        return text

    def _weg(self, g, message, nr):
        if nr is None:
            return _WELCHE_NUMMER.format(name=self._bot_name)
        offen = self.offene(g)
        if not 1 <= nr <= len(offen):
            return _GIBTS_NICHT.format(nr=nr, name=self._bot_name)
        k = offen[nr - 1]
        autor = message.author
        uid = int(autor.id)
        rechte = getattr(autor, "guild_permissions", None)
        mod = bool(getattr(rechte, "manage_messages", False)
                   or getattr(rechte, "administrator", False))
        if uid not in (_ganz(k.get("von")), _ganz(k.get("uid"))) and not mod \
                and not _besitzer(uid):
            return _DARFST_NICHT
        for liste in g["wochen"].values():
            if isinstance(liste, list) and k in liste:
                liste.remove(k)
                break
        self._store.save_soon()
        return random.choice(_WEG_TEXT)

    async def _jetzt_befehl(self, message, guild):
        """'flo zitat umfrage jetzt' - nur der Besitzer: die Runde, in die gerade
        gespeichert wird, wird JETZT abgestimmt."""
        if not _besitzer(getattr(message.author, "id", 0)):
            return _NUR_CHEF
        import laufzeit
        g = self._guild(guild.id)
        ergebnis = await self._starten(laufzeit.client, guild, self.sammel_woche(g),
                                       manuell=True)
        return {
            "umfrage": "Läuft. Die Runde zählt – sonntags kommt für diese Woche keine zweite.",
            "konkurrenzlos": "Nur ein Kandidat – der hat gewonnen. Steht drüben.",
            "ohne_umfrage": "Mir fehlt „Umfragen erstellen“ – ich hab allein entschieden.",
            "leer": "Keine Kandidaten, keine Umfrage. Speichert erst mal was.",
            "kein_kanal": ("Kein Kanal, in den ich darf – und in einem Aufräum-Kanal wäre "
                           "die Umfrage nach einer Minute weg. Stell `zitat_channel` ein."),
            "nicht_raus": "Discord hat die Umfrage abgelehnt, steht im Log.",
            "unklar": ("Discord hat nicht geantwortet. Ob die Umfrage drüben ist, seh ich "
                       "in ein paar Minuten nach."),
            "aus": "Zitate sind hier aus.",
        }.get(ergebnis, "Ging nicht. Steht im Log.")

    # --- Die Abstimmung -------------------------------------------------------
    def _kanal(self, client, guild):
        """Der Zitat-Kanal (guildcfg 'zitat_channel'), sonst die Ansagen, sonst
        der System-Kanal - aber nie ein Aufraeum-Kanal (autodelete_channels):
        dort loescht bot.py auch Flos Posts nach einer Minute, die Umfrage waere
        weg, bevor einer abstimmt, und Discord schickte nie ein Ergebnis. Auf dem
        Hauptserver ist der Ansagen-Kanal ab Werk genau so einer. None = kein
        Kanal taugt (steht dann einmal im Log)."""
        return kanal_ohne_aufraeumen(client, guild, "zitat_channel")

    @staticmethod
    def _darf_umfragen(kanal, guild):
        try:
            rechte = kanal.permissions_for(guild.me)
        except Exception:  # noqa: BLE001 - nicht pruefbar: Discord sagt es beim Senden
            return True
        return bool(getattr(rechte, "send_polls", True))

    @staticmethod
    def umfrage_text(kandidaten):
        """Der Text ueber der Umfrage: alle Zitate in voller Laenge - so weit
        Discords 2000 Zeichen reichen, sonst gleichmaessig gekuerzt."""
        kopf = _UMFRAGE_KOPF.format(stunden=UMFRAGE_STUNDEN)
        grenze = MAX_ZEICHEN
        while True:
            zeilen = [f"**{nr}** „{_sauber(k.get('text'), grenze)}“ – "
                      f"{_sauber(k.get('name'), 32)}"
                      for nr, k in enumerate(kandidaten, 1)]
            text = kopf + "\n\n" + "\n".join(zeilen)
            if len(text) <= 1990 or grenze <= 40:
                return text[:2000]
            grenze -= 20

    async def tick(self, client):
        """Sonntags die Abstimmung starten, verpasste Ergebnisse nachholen."""
        if not self._enabled or self._store is None:
            return
        if not uhr.stabil():
            return                      # Startgnade oder Uhrsprung - spaeter
        guilds = list(getattr(client, "guilds", None) or ())
        for guild in guilds:
            try:
                await self._ausstehend(client, guild)
            except Exception:  # noqa: BLE001 - ein Server haelt die anderen nie auf
                log.exception("Zitat der Woche: Ergebnis auf '%s' fehlgeschlagen",
                              getattr(guild, "name", "?"))
        jetzt = uhr.jetzt()
        woche = faellige_woche(jetzt)
        if woche is None or _sendepause():
            return                      # Sendepause: verschoben (Fenster bis Montag)
        for guild in guilds:
            try:
                await self._starten(client, guild, woche)
            except Exception:  # noqa: BLE001
                log.exception("Zitat der Woche: Abstimmung auf '%s' fehlgeschlagen",
                              getattr(guild, "name", "?"))

    async def _starten(self, client, guild, woche, *, manuell=False):
        """Die Abstimmung der Runde 'woche' auf EINEM Server - einmal: 'gestartet'
        steht auf der Platte, bevor etwas rausgeht. Rueckgabe: 'umfrage',
        'konkurrenzlos', 'ohne_umfrage', 'leer', 'kein_kanal', 'nicht_raus',
        'erledigt', 'aus', 'warten'."""
        gid = int(guild.id)
        if not features.is_on_in(gid, "zitate"):
            return "aus"
        g = self._guild(gid, anlegen=manuell)
        if g is None:
            return "leer"               # hier wurde nie etwas gespeichert
        if not manuell:
            ab = self._nochmal_ab.get(gid)
            if ab is not None and _mono() < ab:
                return "warten"
        async with self._sperre():
            vorher = uhr.glaubhaft(g.get("gestartet"), woche)
            if not uhr.spaeter_als(woche, vorher):
                return "erledigt"
            # Nie zur Wahl: ein Notsignal im Zitat oder im Namen - und wer
            # gerade eins geschrieben hat (basis): "wer hat den groessten
            # Mist gelabert?" ist dann das Letzte, was die Person braucht.
            kandidaten = [k for k in self.offene(g, bis=woche)
                          if not (_notlage(k.get("text")) or _notlage(k.get("name"))
                                  or _kuerzlich_notlage(k.get("uid")))]
            kanal = None
            if kandidaten:
                kanal = self._kanal(client, guild)
                if kanal is None:
                    if self._ohne_kanal.get(gid) != woche:
                        self._ohne_kanal[gid] = woche
                        log.warning("Zitat der Woche auf '%s': kein Kanal, in den Flo darf "
                                    "und der kein Aufraeum-Kanal ist - zitat_channel "
                                    "einstellen.", getattr(guild, "name", gid))
                    return "kein_kanal"
                # Was im Hinterzimmer gesagt wurde, kommt nicht in einen
                # Kanal, den mehr Leute lesen (der Zitat-Kanal kann sich seit
                # dem Speichern geaendert haben).
                merker = {}
                kandidaten = [k for k in kandidaten
                              if not _privat(_quelle(guild, k), kanal, guild, merker)]
            if not kandidaten:
                if manuell:
                    # Der Testausloeser verbraucht ohne Kandidaten keine Runde -
                    # sonst fiele der naechste Sonntag aus.
                    return "leer"
                g["gestartet"] = woche
                self._store.save_soon()
                leer = True
            else:
                leer = False
                gewaehlt = auswahl(kandidaten, f"{gid}:{woche}")
                g["gestartet"] = woche
                alt = g.get("umfrage") or {}
                if _ganz(alt.get("mid")):
                    g["alte"] = ([m for m in g["alte"] if _ganz(m)] + [int(alt["mid"])])[
                        -ALTE_BEHALTEN:]
                art = ("konkurrenzlos" if len(gewaehlt) == 1
                       else "umfrage" if self._darf_umfragen(kanal, guild) else "ohne_umfrage")
                # 'start_ts' und 'vorher': geht beim Senden etwas schief, ohne
                # dass Discord antwortet, weiss _ausstehend damit, wann und
                # wohin es nachsehen muss - und auf welchen Stand es zurueckgeht.
                u = g["umfrage"] = {
                    "woche": woche, "mid": 0, "cid": _ganz(getattr(kanal, "id", 0)),
                    "kandidaten": gewaehlt, "ende_ts": 0.0, "art": art,
                    "sieger": None, "stimmen": 0, "gesamt": 0, "verkuendet": False,
                    "versuche": 0, "start_ts": _zeit(), "vorher": vorher}
                if art != "umfrage":
                    u["sieger"] = (0 if art == "konkurrenzlos"
                                   else random.Random(f"{gid}:{woche}").randrange(len(gewaehlt)))
            # Erst auf die Platte, DANN raus: springt die Uhr oder bricht der
            # Takt ab, kommt keine zweite Umfrage.
            await self._store.save()
        if leer:
            return await self._leere_woche(client, guild, g, woche)
        if art != "umfrage":
            await self._verkuenden(client, guild, g, hintergrund=not manuell)
            return art
        umfrage = discord.Poll(question=FRAGE,
                               duration=_dt.timedelta(hours=UMFRAGE_STUNDEN))
        for nr, k in enumerate(gewaehlt, 1):
            umfrage.add_answer(text=antwort_text(nr, k))
        self._sendet.add(gid)
        try:
            gesendet = await kanal.send(self.umfrage_text(gewaehlt), poll=umfrage,
                                        allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            # Discord hat geantwortet: sicher NICHT gepostet.
            log.warning("Zitat der Woche auf '%s': Umfrage ging nicht raus (%s) - "
                        "spaeter nochmal.", getattr(guild, "name", gid), exc)
            await self._freigeben(gid, g, u)
            return "nicht_raus"
        except Exception as exc:  # noqa: BLE001 - Netz weg, Zeitueberschreitung
            # Ob die Umfrage drueben ist, weiss keiner. 'umfrage' bleibt ohne
            # mid stehen (so steht es auch auf der Platte, wenn der Takt hier
            # abgebrochen wird oder Flo stirbt): kommt ihr Ergebnis, nimmt
            # ergebnis() es an; vorher sucht _ausstehend sie im Kanal und gibt
            # die Runde frei, wenn sie nicht da ist.
            log.warning("Zitat der Woche auf '%s': keine Antwort beim Senden der Umfrage "
                        "(%s: %s) - ich seh gleich nach, ob sie drueben ist.",
                        getattr(guild, "name", gid), type(exc).__name__, exc)
            return "unklar"
        finally:
            self._sendet.discard(gid)
        u["mid"] = _ganz(getattr(gesendet, "id", 0))
        u["ende_ts"] = _zeit() + UMFRAGE_STUNDEN * 3600
        self._store.save_soon()
        log.info("Zitat der Woche %s auf '%s': Umfrage mit %d Kandidaten.", woche,
                 getattr(guild, "name", gid), len(gewaehlt))
        return "umfrage"

    async def _freigeben(self, gid, g, u):
        """Die Umfrage ist sicher nicht drueben: zurueck auf den Stand davor. Der
        naechste Versuch (nach NOCHMAL_NACH) nimmt dieselben Kandidaten;
        gelingt es den ganzen Sonntag nicht, wandern sie in die naechste Runde."""
        async with self._sperre():
            if g.get("umfrage") is u and not _ganz(u.get("mid")):
                g["gestartet"] = str(u.get("vorher") or "")
                g["umfrage"] = {}
            self._nochmal_ab[gid] = _mono() + NOCHMAL_NACH
            await self._store.save()

    async def _suchen(self, guild, g, u):
        """Die Umfrage, deren Senden ohne Antwort blieb, im Kanal suchen: Flos
        Nachricht mit FRAGE, nicht aelter als der Start (mit Spielraum fuer die
        Uhr) und keine der alten. Die Nachricht - None = sicher nicht da -
        False = nicht nachsehbar (Recht fehlt, Discord zickt)."""
        cid = _ganz(u.get("cid"))
        kanal = guild.get_channel(cid) if cid else None
        if kanal is None or not hasattr(kanal, "history"):
            return False
        ich = _ganz(getattr(getattr(guild, "me", None), "id", 0))
        alte = {_ganz(m) for m in g.get("alte") or ()}
        ab = float(u.get("start_ts") or 0) - 6 * 3600
        try:
            async for m in kanal.history(limit=50):
                umfrage = getattr(m, "poll", None)
                if (umfrage is None or _ganz(getattr(m.author, "id", 0)) != ich
                        or _ganz(m.id) in alte or str(umfrage.question) != FRAGE):
                    continue
                erstellt = getattr(m, "created_at", None)
                if erstellt is not None and erstellt.timestamp() < ab:
                    continue
                return m
        except Exception as exc:  # noqa: BLE001 - Forbidden, HTTP, Netz
            log.warning("Zitat der Woche: Kanal nicht durchsuchbar (%s: %s).",
                        type(exc).__name__, exc)
            return False
        return None

    async def _unklar(self, guild, g, u):
        """Senden ohne Antwort: nach UNKLAR_NACH im Kanal nachsehen. Gefunden ->
        die Umfrage laeuft normal weiter; sicher nicht da -> Runde frei; nicht
        nachsehbar -> spaeter nochmal, nach AUFGEBEN_NACH frei (kam bis dahin
        kein Ergebnis, war auch keine Umfrage drueben)."""
        gid = int(guild.id)
        jetzt = _zeit()
        try:
            start = float(u.get("start_ts") or 0)
        except (TypeError, ValueError):
            start = 0.0
        glaubhaft = basis.glaubhafte_zeit(start, jetzt)
        if glaubhaft != start:
            u["start_ts"] = start = glaubhaft
            self._store.save_soon()
        if jetzt - start < UNKLAR_NACH:
            return
        gefunden = await self._suchen(guild, g, u)
        if gefunden is False:
            if jetzt - start > AUFGEBEN_NACH:
                log.warning("Zitat der Woche %s: Umfrage nicht auffindbar - die Kandidaten "
                            "kommen in die naechste Runde.", u.get("woche"))
                await self._freigeben(gid, g, u)
            else:
                self._nochmal_ab[gid] = _mono() + NOCHMAL_NACH
            return
        if gefunden is None:
            log.warning("Zitat der Woche %s: die Umfrage ist nicht drueben - Runde frei.",
                        u.get("woche"))
            await self._freigeben(gid, g, u)
            return
        async with self._sperre():
            if g.get("umfrage") is u and not _ganz(u.get("mid")):
                u["mid"] = _ganz(gefunden.id)
                erstellt = getattr(gefunden, "created_at", None)
                beginn = erstellt.timestamp() if erstellt is not None else start
                u["ende_ts"] = min(beginn, jetzt) + UMFRAGE_STUNDEN * 3600
                self._store.save_soon()
        log.info("Zitat der Woche %s: die Umfrage ist doch drueben.", u.get("woche"))

    async def _leere_woche(self, client, guild, g, woche):
        """Keine Kandidaten: eine derbe Zeile - aber nur, wenn hier in den
        letzten vier Wochen ueberhaupt etwas gespeichert wurde. Sonst Stille."""
        jetzt = uhr.jetzt()
        frueher = {uhr.woche_key(jetzt - _dt.timedelta(weeks=n)) for n in range(1, 5)}
        if not any(g["wochen"].get(w) for w in frueher):
            return "leer"
        kanal = self._kanal(client, guild)
        if kanal is not None:
            try:
                await kanal.send(random.choice(_LEER_ZEILE),
                                 allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                log.warning("Zitat der Woche: Leer-Zeile ging nicht raus (%s)", exc)
        return "leer"

    # --- Das Ergebnis ---------------------------------------------------------
    @staticmethod
    def _felder(msg):
        felder = {}
        for embed in getattr(msg, "embeds", None) or ():
            for feld in getattr(embed, "fields", None) or ():
                felder[str(getattr(feld, "name", ""))] = str(getattr(feld, "value", "") or "")
        return felder

    def ist_eigene_umfrage(self, msg):
        """Ist dieses Umfrage-Ergebnis die Zitat-Abstimmung? Die laufende, eine
        aeltere von Flo - oder eine mit genau dieser Frage."""
        if not self._enabled or self._store is None:
            return False
        guild = getattr(msg, "guild", None)
        g = self._guild(guild.id, anlegen=False) if guild is not None else None
        ref = _ganz(getattr(getattr(msg, "reference", None), "message_id", 0))
        if g is not None and ref:
            if ref == _ganz((g.get("umfrage") or {}).get("mid")) or ref in {
                    _ganz(m) for m in g["alte"]}:
                return True
        return self._felder(msg).get("poll_question_text", "") == FRAGE

    def _annehmen(self, g, u, msg, ref):
        """Gehoert dieses Ergebnis zu der Umfrage, deren Senden ohne Antwort
        blieb (mid 0)? Ja, wenn die Frage stimmt, der Kanal passt und es keine
        der alten ist."""
        if (not u or _ganz(u.get("mid")) or u.get("art") != "umfrage"
                or u.get("sieger") is not None or u.get("verkuendet")):
            return False
        if ref in {_ganz(m) for m in g.get("alte") or ()}:
            return False
        cid = _ganz(u.get("cid"))
        if cid and _ganz(getattr(getattr(msg, "channel", None), "id", 0)) != cid:
            return False
        return self._felder(msg).get("poll_question_text", "") == FRAGE

    @staticmethod
    def _index(u, antwort_id=0, antwort_text_=""):
        """Welcher Kandidat zu einer Antwort gehoert: die Nummer vorne im Text,
        sonst die Antwort-ID (Discord zaehlt ab 1, in Reihenfolge)."""
        kandidaten = u.get("kandidaten") or []
        m = _ANTWORT_NR_RE.match(str(antwort_text_ or ""))
        nr = int(m.group(1)) if m else _ganz(antwort_id)
        return nr - 1 if 1 <= nr <= len(kandidaten) else None

    def _aus_antworten(self, u, umfrage, seed):
        """(index, stimmen, gesamt, gleichstand) aus den Antworten einer geholten
        Umfrage - bei Gleichstand per Zufall unter den Besten. None = nichts
        Brauchbares."""
        antworten = list(getattr(umfrage, "answers", None) or ())
        zaehler = []
        for a in antworten:
            idx = self._index(u, getattr(a, "id", 0), getattr(a, "text", ""))
            if idx is not None:
                zaehler.append((idx, _ganz(getattr(a, "vote_count", 0))))
        if not zaehler:
            return None
        gesamt = sum(n for _i, n in zaehler)
        if gesamt <= 0:
            return (-1, 0, 0, False)
        beste = max(n for _i, n in zaehler)
        oben = sorted(i for i, n in zaehler if n == beste)
        return (random.Random(seed).choice(oben), beste, gesamt, len(oben) > 1)

    async def _holen(self, client, guild, u):
        """Die Umfrage-Nachricht von Discord. None = nicht (mehr) da,
        False = gerade nicht erreichbar."""
        cid, mid = _ganz(u.get("cid")), _ganz(u.get("mid"))
        kanal = guild.get_channel(cid) if cid else None
        if kanal is None and client is not None:
            try:
                kanal = client.get_channel(cid)
            except Exception:  # noqa: BLE001
                kanal = None
        if kanal is None or not mid:
            return None
        try:
            return await kanal.fetch_message(mid)
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            log.warning("Zitat der Woche: Umfrage nicht abrufbar (%s) - spaeter.", exc)
            return False

    async def ergebnis(self, msg):
        """Die Zitat-Abstimmung ist vorbei -> Sieger kueren. In der Sendepause
        (oder wenn es nicht rausgeht) haelt Flo den Sieger fest, und der Takt
        postet ihn spaeter."""
        if not self._enabled or self._store is None:
            return False
        guild = getattr(msg, "guild", None)
        g = self._guild(guild.id, anlegen=False) if guild is not None else None
        ref = _ganz(getattr(getattr(msg, "reference", None), "message_id", 0))
        u = (g or {}).get("umfrage") or {}
        if g and ref and self._annehmen(g, u, msg, ref):
            u["mid"] = ref              # das Senden blieb ohne Antwort - sie war drueben
            self._store.save_soon()
            log.info("Zitat der Woche %s: Ergebnis einer Umfrage ohne Antwort beim "
                     "Senden - angenommen.", u.get("woche"))
        if not g or not ref or ref != _ganz(u.get("mid")):
            log.info("Zitat der Woche: Ergebnis einer alten Abstimmung - ignoriert.")
            return False
        gid = int(guild.id)
        async with self._sperre():
            if u.get("sieger") is None:
                felder = self._felder(msg)
                gesamt = _ganz(felder.get("total_votes"))
                idx = self._index(u, felder.get("victor_answer_id"),
                                  felder.get("victor_answer_text"))
                if gesamt <= 0:
                    u.update(sieger=-1, stimmen=0, gesamt=0)
                elif idx is not None:
                    u.update(sieger=idx, gesamt=gesamt,
                             stimmen=_ganz(felder.get("victor_answer_votes")))
                else:
                    # Gleichstand - Discord nennt keinen Sieger. Die Umfrage
                    # selbst holen und unter den Besten auslosen.
                    try:
                        geholt = await msg.channel.fetch_message(ref)
                    except Exception:  # noqa: BLE001 - dann der Takt (NACHSEHEN_NACH)
                        log.warning("Zitat der Woche: Gleichstand, Umfrage nicht "
                                    "abrufbar - der Takt holt es nach.", exc_info=True)
                        return False
                    werte = self._aus_antworten(u, getattr(geholt, "poll", None),
                                                f"{gid}:{u.get('woche')}")
                    if werte is None:
                        return False
                    idx, stimmen, gesamt, gleich = werte
                    u.update(sieger=idx, stimmen=stimmen, gesamt=gesamt)
                    if gleich:
                        u["art"] = "gleichstand"
                self._store.save_soon()
        if not features.is_on_in(gid, "zitate"):
            u["verkuendet"] = True      # hier abgeschaltet: kein Post
            self._store.save_soon()
            return False
        if _sendepause():
            return False                # der Takt holt es nach
        import laufzeit
        return await self._verkuenden(laufzeit.client, guild, g, hintergrund=True)

    async def _ausstehend(self, client, guild):
        """Was an einer Abstimmung noch fehlt: das Ergebnis (Flo war offline, als
        es kam - 30 Minuten nach dem Ende selbst nachsehen) und der Post (in der
        Sendepause festgehalten, oder von Discord abgelehnt)."""
        g = self._guild(guild.id, anlegen=False)
        u = (g or {}).get("umfrage") or {}
        if not u or u.get("verkuendet"):
            return
        gid = int(guild.id)
        ab = self._nochmal_ab.get(gid)
        if ab is not None and _mono() < ab:
            return
        if u.get("sieger") is None:
            if not _ganz(u.get("mid")):
                # Gesendet, aber ohne Antwort - ob sie drueben ist, klaert _unklar.
                if u.get("art") == "umfrage" and gid not in self._sendet:
                    await self._unklar(guild, g, u)
                return
            jetzt = _zeit()
            try:
                gespeichert = float(u.get("ende_ts") or 0)
            except (TypeError, ValueError):
                gespeichert = 0.0
            # Gestempelt, bevor die Uhr beim Booten zurueckgesprungen ist? Dann
            # gilt "ab jetzt UMFRAGE_STUNDEN" - und zwar zurueckgeschrieben,
            # sonst wanderte das Ende bei jedem Takt mit (basis.glaubhafte_zeit).
            ende = basis.glaubhafte_zeit(gespeichert, jetzt, vorlauf=UMFRAGE_STUNDEN * 3600)
            if ende != gespeichert:
                u["ende_ts"] = ende
                self._store.save_soon()
            if jetzt < ende + NACHSEHEN_NACH:
                return
            if jetzt > ende + AUFGEBEN_NACH:
                u.update(sieger=-2, verkuendet=True)
                self._store.save_soon()
                log.warning("Zitat der Woche %s: kein Ergebnis zu bekommen - ich lass es.",
                            u.get("woche"))
                return
            geholt = await self._holen(client, guild, u)
            if geholt is None:
                u.update(sieger=-2, verkuendet=True)
                self._store.save_soon()
                log.warning("Zitat der Woche %s: die Umfrage ist weg.", u.get("woche"))
                return
            umfrage = getattr(geholt, "poll", None) if geholt is not False else None
            if umfrage is None or not umfrage.is_finalised():
                self._nochmal_ab[gid] = _mono() + NOCHMAL_NACH
                return
            werte = self._aus_antworten(u, umfrage, f"{gid}:{u.get('woche')}")
            async with self._sperre():
                if u.get("sieger") is None:
                    if werte is None:
                        u.update(sieger=-2, verkuendet=True)
                    else:
                        idx, stimmen, gesamt, gleich = werte
                        u.update(sieger=idx, stimmen=stimmen, gesamt=gesamt)
                        if gleich:
                            u["art"] = "gleichstand"
                    self._store.save_soon()
            log.info("Zitat der Woche %s: Ergebnis nachgeholt.", u.get("woche"))
        if u.get("verkuendet") or _sendepause():
            return
        if not features.is_on_in(gid, "zitate"):
            u["verkuendet"] = True
            self._store.save_soon()
            return
        await self._verkuenden(client, guild, g, hintergrund=True)

    # --- Der Sieger -----------------------------------------------------------
    async def _verkuenden(self, client, guild, g, *, hintergrund):
        """Den Sieger posten (Karte, Kommentar, ein Ping). Einmal: unter der
        Sperre, und 'verkuendet' steht danach. Scheitert der Post, versucht es
        der Takt nochmal (VERSUCHE, NOCHMAL_NACH)."""
        gid = int(guild.id)
        async with self._sperre():
            u = g.get("umfrage") or {}
            if not u or u.get("verkuendet") or u.get("sieger") is None:
                return False
            u["versuche"] = _ganz(u.get("versuche")) + 1
            self._nochmal_ab[gid] = _mono() + NOCHMAL_NACH
            if _ganz(u["versuche"]) > VERSUCHE:
                u["verkuendet"] = True
                self._store.save_soon()
                log.warning("Zitat der Woche %s: nach %d Versuchen nicht raus - ich lass es.",
                            u.get("woche"), VERSUCHE)
                return False
            kanal = self._kanal(client, guild)
            if kanal is None:
                log.warning("Zitat der Woche: kein Kanal fuer den Sieger auf '%s'.",
                            getattr(guild, "name", gid))
                self._store.save_soon()
                return False
            sieger = _ganz(u.get("sieger")) if u.get("sieger") is not None else -1
            kandidaten = u.get("kandidaten") or []
            if not 0 <= sieger < len(kandidaten):
                try:
                    await kanal.send(_KEINE_STIMMEN,
                                     allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException as exc:
                    log.warning("Zitat der Woche: Post ging nicht raus (%s)", exc)
                    self._store.save_soon()
                    return False
                u["verkuendet"] = True
                self._store.save_soon()
                return True
            k = kandidaten[sieger]
            uid = _ganz(k.get("uid"))
            kopf = _KOPF.get(u.get("art") or "umfrage", _KOPF["umfrage"]).format(
                uid=uid, stimmen=_ganz(u.get("stimmen")), gesamt=_ganz(u.get("gesamt")))
            # Hat der Sieger gerade ein Notsignal geschrieben (basis):
            # kein Spott, keine KI, kein Ping - nur die Karte und eine Zeile.
            sanft = _kuerzlich_notlage(uid)
            if sanft:
                kommentar = _SANFT
            else:
                kommentar = await self._kommentar(gid, k, hintergrund=hintergrund)
            bild = await self._karte(client, guild, k)
            text = f"{kopf}\n> „{_sauber(k.get('text'))}“\n{kommentar}"[:2000]
            erlaubt = discord.AllowedMentions(
                everyone=False, roles=False, replied_user=False,
                users=[discord.Object(uid)] if uid and not sanft else False)
            kw = {"allowed_mentions": erlaubt}
            if bild is not None:
                kw["file"] = discord.File(bild, filename="zitat_der_woche.png")
            try:
                await kanal.send(text, **kw)
            except discord.HTTPException as exc:
                if "file" not in kw:
                    log.warning("Zitat der Woche: Post ging nicht raus (%s)", exc)
                    self._store.save_soon()
                    return False
                kw.pop("file")          # ohne 'Dateien anhaengen' eben ohne Bild
                try:
                    await kanal.send(text, **kw)
                except discord.HTTPException as exc2:
                    log.warning("Zitat der Woche: Post ging nicht raus (%s)", exc2)
                    self._store.save_soon()
                    return False
            u["verkuendet"] = True
            self._nochmal_ab.pop(gid, None)
            g["sieger"].insert(0, {
                "woche": u.get("woche"), "uid": uid, "name": k.get("name"),
                "text": k.get("text"), "stimmen": _ganz(u.get("stimmen")),
                "gesamt": _ganz(u.get("gesamt")), "ts": _zeit()})
            del g["sieger"][SIEGER_BEHALTEN:]
            self._store.save_soon()
        log.info("Zitat der Woche %s auf '%s' verkuendet.", u.get("woche"),
                 getattr(guild, "name", gid))
        return True

    async def _karte(self, client, guild, k):
        """render.quote_card mit Profilbild - im Thread. None, wenn es nicht geht."""
        avatar = None
        uid = _ganz(k.get("uid"))
        try:
            person = guild.get_member(uid) if uid else None
            if person is None and uid and client is not None:
                person = await client.fetch_user(uid)
            if person is not None:
                import media
                avatar = await media.instance._fetch_avatar(person)
        except Exception:  # noqa: BLE001 - Profilbild ist nur Deko
            log.debug("Zitat der Woche: kein Profilbild", exc_info=True)
        try:
            import render
            return await asyncio.to_thread(render.quote_card, avatar, str(k.get("text") or ""),
                                           str(k.get("name") or "?"))
        except Exception:  # noqa: BLE001 - dann eben ohne Bild
            log.exception("Zitat der Woche: Karte nicht gerendert")
            return None

    async def _kommentar(self, gid, k, *, hintergrund):
        """Zwei Saetze zum Siegerzitat - von der KI, sonst aus dem Ersatz-Pool."""
        name = str(k.get("name") or "")
        if not name or _notlage(name):
            name = "du"
        text = await self._ki_kommentar(gid, k, name, hintergrund=hintergrund)
        return text or random.choice(_ERSATZ).format(name=_sauber(name, 40))

    async def _ki_kommentar(self, gid, k, name, *, hintergrund):
        zitat = str(k.get("text") or "")
        if (not features.is_on_in(gid, "ki") or _sendepause() or _notlage(zitat)
                or _hetze(zitat)):
            return None
        try:
            if not ai.is_enabled():
                return None
            with ai.guild_kontext(gid):
                system = (
                    f"Du bist {ai.bot_name(gid)}, ein frecher deutscher Discord-Bot mit losem "
                    "Mundwerk. Der Server hat das Zitat der Woche gewaehlt - den duemmsten "
                    "oder lustigsten Satz der Woche. Kommentiere es in GENAU ZWEI Saetzen auf "
                    "Deutsch: mach dich ueber den Satz lustig und ueber die Person, die ihn "
                    "gesagt hat. Keine Emojis, kein Vorwort, keine Aufzaehlung. "
                    f"{ai.FloAI._GUARDRAIL}")
                prompt = f"Zitat: „{zitat[:MAX_ZEICHEN]}“\nGesagt von: {name[:40]}"
                out = await asyncio.wait_for(ai.generate(
                    prompt, system=system, temperature=1.0, max_tokens=120,
                    hintergrund=hintergrund), KI_FRIST)
        except Exception:  # noqa: BLE001 - dann eben der Ersatz (auch bei Frist)
            log.debug("Zitat der Woche: KI-Kommentar gescheitert", exc_info=True)
            return None
        out = _einzeilig(out).strip("\"'„“”").strip()
        if (not out or ai.instance._ist_verweigerung(out) or ai.instance.ist_notlage(out, out)
                or _hetze(out) or _heikel(out)):
            return None
        return ai.instance._kuerzen(out).replace("@", "")


instance = Zitate()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
rechtsklick = instance.rechtsklick
ist_eigene_umfrage = instance.ist_eigene_umfrage
ergebnis = instance.ergebnis
tick = instance.tick
