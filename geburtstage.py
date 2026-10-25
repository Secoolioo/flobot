"""Geburtstage: einmal eintragen, Flo denkt dran ('flo geburtstag 12.03.').

Wie es sich anfuehlt
====================
    flo geburtstag 12.03.     eintragen (auch 12.3, 12. März, 29.02.; ein Jahr
                              dahinter wird ignoriert - "Das Jahr will ich gar
                              nicht wissen.")
    flo geburtstag            den eigenen zeigen
    flo geburtstag löschen    austragen (nur eindeutig: 'Geburtstag vergessen?'
                              oder 'geburtstag löschen?' loeschen nichts)
    flo geburtstage           die naechsten zehn auf diesem Server

Nur fuer sich selbst - wer jemand anderen eintragen will, bekommt eine Abfuhr.
Je Server getrennt: wer auf zwei Servern gefeiert werden will, traegt sich auf
beiden ein.

Am Geburtstag ab 9 Uhr (Ortszeit) gratuliert Flo im Geburtstags-Kanal (guildcfg
'geburtstag_channel', sonst die Ansagen) - mit Ping NUR fuer das
Geburtstagskind, einer Zeile von der KI (Hintergrund, mit Grenze) oder aus dem
Ersatz-Pool, dem Geschenk (guildcfg 'geburtstag_coins') und, wenn
eingeschaltet, der Rolle "🎂 Geburtstagskind" fuer den Tag (guildcfg
'geburtstag_rolle').

Coins gelten auf ALLEN Servern - deshalb darf man sie sich nicht auf Zuruf
holen:
  * Bezahlt wird nur, wenn das Datum mindestens VORLAUF_TAGE vor dem
    gefeierten Tag eingetragen war. Wer es zwei Tage vorher eintraegt, bekommt
    den Glueckwunsch - mit einer Bemerkung statt Coins.
  * Das Datum laesst sich hoechstens alle AENDERN_ABSTAND Tage aendern (der
    erste Eintrag ist frei, damit ein Tippfehler gleich korrigiert werden kann).
  * Gefeiert wird hoechstens einmal je Jahr und Server, egal wie oft das
    Datum wandert. Coins gibt es hoechstens einmal je Jahr UEBER ALLE SERVER
    (Kassenbuch 'bezahlt'): wer auf fuenf Servern eingetragen ist, wird
    fuenfmal gefeiert, aber einmal bezahlt - sonst druckte sich jeder mit ein
    paar Privatservern Geld.
  * Loeschen und neu eintragen hilft nicht: 'gefeiert' und die Aenderungs-
    Sperre bleiben stehen, und ein neuer Eintrag nach dem Loeschen zaehlt als
    Aenderung (mit neuem Vorlauf).
  * Der Server-Admin kann hoechstens 10.000 Coins einstellen (guildcfg).

Wo was aufgerufen wird:
    handle(message)   Handler-Kette, nach umfrage
    tick(client)      bot.alltag_loop, jede Minute (hoechstens
                      bot.ALLTAG_FRIST, dann abgebrochen). Die Sendepause
                      prueft bot.py hier NICHT - selbst admin.is_locked()
                      fragen und den Glueckwunsch danach nachholen

Die Uhr (uhr.py): gratuliert wird nur bei stabiler Uhr und ab 9 Uhr. 'geprueft'
(der letzte Tag, der ganz durch ist) laeuft nur vorwaerts; geprueft wird jeder
Tag danach bis heute, hoechstens NACHHOL_TAGE zurueck ("nachtraeglich, weil ich
offline war"). Der heutige Tag wird bei jedem Takt angesehen - wer sich heute
eintraegt und heute Geburtstag hat, bekommt den Glueckwunsch noch (ohne Coins:
zu knapp). Der 29.02. wird in Nicht-Schaltjahren am 28.02. gefeiert.

Bezahlt wird nach "lock-before-pay" (wie Giveaway und Musik-Wrapped): erst
'gefeiert' auf die Platte, DANN die Coins - in asyncio.shield, damit ein
abgebrochener Takt nie das eine ohne das andere laesst. Der Post selbst steht
in 'offen' und wird nachgeholt, wenn Discord ihn ablehnt (VERSUCHE, nur im
Nachhol-Fenster) - bezahlt wird dabei nie noch einmal. Die Rolle nimmt Flo am
Tag danach wieder (rolle_bis + fetch_member, ueberlebt Neustarts).

Wer den Server verlassen hat, wird am Geburtstag nicht gefeiert und fliegt
aus der Liste ('gefeiert' und die Aenderungs-Sperre bleiben). Ist es der erste
Eintrag auf einem Server, gilt bis gestern als geprueft - sonst kaeme ein
"nachtraeglich, weil ich offline war", obwohl Flo gar nicht offline war.

Nichts davon reagiert auf eine Notlage (ai.FloAI._NOTLAGE_RE): ein Name mit
Notsignal geht nicht an die KI, eine KI-Zeile mit Notsignal fliegt raus, und
wer gerade ein Notsignal geschrieben hat (basis merkt sich das im
Speicher), bekommt einen Glueckwunsch ohne Spott.

Daten: data/geburtstage.json
    {"bezahlt": {"<uid>": "2026"},                 (ueber alle Server)
     "guilds": {"<gid>": {
        "leute": {"<uid>": {"t": 12, "m": 3, "name": "Bob", "seit": "2026-02-01"}},
        "gefeiert": {"<uid>": "2026"},
        "geaendert": {"<uid>": "2026-02-01"},      ("" = eingetragen, nie geaendert)
        "offen": {"<uid>": {"tag": "2026-03-12", "betrag": 2500, "bezahlt": true,
                            "grund": ""|"zu_knapp"|"anderswo", "versuche": 0}},
        "rolle_id": 0, "rolle_bis": {"<uid>": "2026-03-13"},
        "geprueft": "2026-03-11"}}}

Beim Import passiert nichts.
"""

import asyncio
import calendar
import logging
import os
import random
import re
import time
from datetime import date, timedelta

import discord

import ai
import basis
from basis import FeatureBasis
import economy
import features
import guildcfg
from numfmt import fmt
import uhr
from store import JsonStore

log = logging.getLogger("dcbot.geburtstage")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("geburtstag", "geburtstage", "birthday", "bday")
_ERSTE = frozenset(_CMDS)

#: Ab dieser Stunde (Ortszeit) wird gratuliert.
GLUECKWUNSCH_STUNDE = 9
#: So viele Tage holt Flo nach, wenn er offline war.
NACHHOL_TAGE = 2
#: So lange vor dem gefeierten Tag muss das Datum eingetragen sein, sonst
#: gibt es keine Coins (nur den Glueckwunsch).
VORLAUF_TAGE = 14
#: Abstand zwischen zwei Aenderungen des Datums (Tage).
AENDERN_ABSTAND = 30
#: So viele zeigt 'flo geburtstage'.
LISTE_MAX = 10
#: Hoechstens so viele Coins - dieselbe Grenze wie in guildcfg, doppelt haelt
#: besser (eine von Hand editierte Datei geht an guildcfg vorbei).
COINS_MAX = 10_000
ROLLE = "🎂 Geburtstagskind"
ROLLE_FARBE = 0xFF6FB5
#: Ein abgelehnter Glueckwunsch: so oft nochmal, im Abstand von NOCHMAL_NACH.
VERSUCHE = 4
NOCHMAL_NACH = 15 * 60.0
#: Rolle nicht abnehmbar (Recht fehlt, Discord zickt): so lange Pause.
ROLLE_NOCHMAL_NACH = 60 * 60.0
#: ... und nach so vielen Tagen wird es nicht mehr versucht.
ROLLE_AUFGEBEN_TAGE = 7
#: Hoechstens so lange wartet der Takt auf die KI (Sekunden).
KI_FRIST = 30.0
#: So steht ein Name im Glueckwunsch, wenn er ein Notsignal enthaelt.
OHNE_NAME = "Geburtstagskind"

_MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember")
_MONAT_NR = {
    "januar": 1, "jan": 1, "jänner": 1, "jaenner": 1, "februar": 2, "feb": 2,
    "märz": 3, "maerz": 3, "mär": 3, "maer": 3, "mrz": 3, "april": 4, "apr": 4,
    "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "oktober": 10, "okt": 10,
    "november": 11, "nov": 11, "dezember": 12, "dez": 12,
}
# [0-9] statt \d: \d nimmt auch '٣' und '²' - int() stirbt dann daran.
_ZAHL_DATUM_RE = re.compile(
    r"([0-9]{1,2})\.\s?([0-9]{1,2})\.?(?:\s?([0-9]{4}|[0-9]{2}))?")
_WORT_DATUM_RE = re.compile(
    r"([0-9]{1,2})\.?\s*(" + "|".join(sorted(_MONAT_NR, key=len, reverse=True))
    + r")\.?(?:\s*([0-9]{4}))?")
# Was vor dem Datum stehen darf: 'flo geburtstag am 12.03.', '... ist am 12.3.'
_VOR_DATUM_RE = re.compile(r"^(?:(?:setzen|eintragen|ist)\s+)?(?:am\s+)?")

_RAND = " \t.,!?;:\"'„“”`"
_ZEIGEN = frozenset(("", "zeigen", "anzeigen", "wann", "mein", "meiner", "meinen"))
# Nur eindeutige Woerter - 'Geburtstag vergessen?' ist ein Vorwurf, kein
# Auftrag. Und mit Fragezeichen loescht nichts ('geburtstag löschen?').
_LOESCHEN = frozenset(("löschen", "loeschen", "lösche", "loesche", "weg", "entfernen",
                       "austragen", "delete", "remove"))
_LISTE = frozenset(("", "liste", "anzeigen", "zeigen", "hier", "alle", "kommende",
                    "nächste", "naechste", "demnächst", "demnaechst"))

# --- Texte ---------------------------------------------------------------------
_FUER_ANDERE = (
    "Für andere trag ich nix ein. Soll {wer} das selber machen – oder hat der keine Finger?",
    "Nee. Jeder trägt seinen eigenen Geburtstag ein. {wer} kann das allein, glaub mir.",
)
_UNGUELTIG = ("Den {datum} gibt's nicht. Netter Versuch, Zeitreisender.",
              "{datum}? Im Kalender, den du benutzt, vielleicht. Bei mir nicht.")
_JAHR = "Das Jahr will ich gar nicht wissen. "
_KEINER = ("Hab ich nicht. Trag ihn ein: `{name} geburtstag 12.03.` – das Jahr kannst "
           "du dir sparen.")
_NIX_ZU_LOESCHEN = "Da steht nix. Was soll ich löschen, deine Erwartungen?"
_GELOESCHT = ("Gelöscht. Dann wirst du halt heimlich älter. Ein neues Datum zählt "
              "übrigens als Änderung – gefeiert wird trotzdem nur einmal im Jahr.")
_SCHON = "Steht doch schon drin: **{datum}**. Ich bin ja nicht dement."
_GESPERRT = ("Schon wieder ein neuer Geburtstag? So oft wird keiner geboren. "
             "Ändern frühestens am {ab}.")
_LISTE_LEER = ("Hier hat keiner seinen Geburtstag eingetragen. Entweder seid ihr alle "
               "unsterblich, oder es ist euch peinlich.")

# Glueckwunsch, wenn die KI nicht kann (aus, Sendepause, 429, Verweigerung).
_ERSATZ = (
    "Alles Gute, {name}. Wieder ein Jahr älter und immer noch nix erreicht.",
    "{name} hat Geburtstag. Ein Jahr näher an der Rente und immer noch kein Plan – "
    "trotzdem: Glückwunsch.",
    "Happy Birthday, {name}. Ich hab dran gedacht – im Gegensatz zu deinen "
    "sogenannten Freunden.",
    "Glückwunsch, {name}. Noch ein Jahr, in dem du mich vollquatschen darfst. "
    "Ich freu mich. Nicht.",
)
# Wer gerade ein Notsignal geschrieben hat, bekommt keinen Spott ab.
_SANFT = (
    "Alles Gute zum Geburtstag, {name}. Heute ausnahmsweise ohne Spruch.",
    "Happy Birthday, {name}. Schön, dass du da bist – und das mein ich so.",
)
_COINS = (
    "Hier, {n} Flo Coins – kauf dir Würde.",
    "{n} Flo Coins von mir. Mehr bist du mir nicht wert.",
    "Dein Geschenk: {n} Flo Coins. Verzock sie nicht alle auf einmal. Doch, mach ruhig.",
)
_COINS_SANFT = "Dazu {n} Flo Coins."
_ZU_KNAPP = (
    "Coins gibt's keine – wer seinen Geburtstag erst kurz vorher einträgt, will nur "
    "abkassieren. Nächstes Jahr vielleicht.",
    "Geschenk? Nö. Wer sich zwei Tage vorher einträgt, kriegt nur den Glückwunsch. "
    "Abkassieren auf Zuruf läuft hier nicht.",
)
_ANDERSWO = ("Coins gab's dieses Jahr schon auf einem anderen Server – du wirst nur "
             "einmal im Jahr älter, auch wenn du dich überall einträgst.")
_NACHTRAEGLICH = "Nachträglich, weil ich offline war: "


def _sendepause():
    try:
        import admin
        return bool(admin.is_locked())
    except Exception:  # noqa: BLE001 - ohne admin keine Sendepause
        return False


def _mono():
    return time.monotonic()


def _ganz(wert):
    try:
        return int(wert or 0)
    except (TypeError, ValueError):
        return 0


def _sauber(text, laenge=40):
    """Fuer den Chat: gekuerzt, ohne Markdown und ohne Ping."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > laenge:
        text = text[:laenge - 1].rstrip() + "…"
    return discord.utils.escape_markdown(text).replace("@", "@\u200b")


def _notlage(text):
    """Notsignal? Darauf reagiert nichts, was Flo von sich aus tut."""
    return bool(ai.FloAI._NOTLAGE_RE.search(str(text or "")))


def _datum_text(t, m):
    """(12, 3) -> '12. März'"""
    return f"{int(t)}. {_MONATE[int(m) - 1]}"


def _gueltig(t, m):
    """Gibt es diesen Tag (in einem Schaltjahr)?"""
    try:
        date(2000, int(m), int(t))
    except (TypeError, ValueError):
        return False
    return True


def feiertag(t, m, jahr):
    """Der Tag, an dem in 'jahr' gefeiert wird: der 29.02. ist in einem
    Nicht-Schaltjahr der 28.02."""
    if (int(m), int(t)) == (2, 29) and not calendar.isleap(jahr):
        return date(jahr, 2, 28)
    return date(jahr, int(m), int(t))


def naechster(t, m, ab):
    """Der naechste Feiertag am oder nach 'ab' (date)."""
    for jahr in (ab.year, ab.year + 1):
        tag = feiertag(t, m, jahr)
        if tag >= ab:
            return tag
    return feiertag(t, m, ab.year + 2)     # nie erreicht - der Form halber


def _als_datum(key):
    """'2026-03-12' -> date, sonst None."""
    m = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", str(key or ""))
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def zerlege_datum(text):
    """'12.03.', '12.3', '12. März', '12.03.1999' -> (tag, monat, mit_jahr) -
    ('ungueltig', text) fuer ein Datum, das es nicht gibt ('31.02.') - None fuer
    alles, was kein Datum ist."""
    text = " ".join(str(text or "").lower().split()).strip(" ,!?")
    for muster, wort in ((_ZAHL_DATUM_RE, False), (_WORT_DATUM_RE, True)):
        m = muster.fullmatch(text)
        if not m:
            continue
        t = int(m.group(1))
        mon = _MONAT_NR[m.group(2)] if wort else int(m.group(2))
        if not 1 <= mon <= 12 or not _gueltig(t, mon):
            return ("ungueltig", text)
        return (t, mon, bool(m.group(3)))
    return None


class Geburtstage(FeatureBasis):
    """Geburtstage je Server: eintragen, zeigen, gratulieren."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._lock = None
        # gid -> uid -> monotonic: ab wann ein abgelehnter Glueckwunsch nochmal darf
        self._nochmal_ab = {}
        # gid -> monotonic: ab wann das Abnehmen der Rolle nochmal versucht wird
        self._rolle_nochmal_ab = {}

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if os.getenv("GEBURTSTAGE_ENABLED", "1").strip().lower() in (
                "0", "false", "no", "off", "aus"):
            log.info("Geburtstage aus (GEBURTSTAGE_ENABLED=0).")
            return False
        self._store = JsonStore("geburtstage.json", default={"guilds": {}, "bezahlt": {}})
        self._lock = None
        self._nochmal_ab = {}
        self._rolle_nochmal_ab = {}
        self._enabled = True
        log.info("Geburtstage aktiv (%d Server mit Eintraegen).", len(self._guilds()))
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

    def _bezahlt(self):
        """uid -> Jahr, in dem diese Person zuletzt Geburtstags-Coins bekam -
        UEBER ALLE SERVER: Coins gelten ueberall, also gibt es sie auch nur
        einmal im Jahr, egal auf wie vielen Servern jemand gefeiert wird."""
        b = self._store.data.setdefault("bezahlt", {})
        if not isinstance(b, dict):
            b = self._store.data["bezahlt"] = {}
        return b

    def _guild(self, gid, *, anlegen=True):
        """Der Eintrag eines Servers - alle Felder in Ordnung gebracht. Ohne
        'anlegen' None, wenn es den Server (noch) nicht gibt."""
        guilds = self._guilds()
        g = guilds.get(str(int(gid)))
        if not isinstance(g, dict):
            if not anlegen:
                return None
            g = guilds[str(int(gid))] = {}
        for feld in ("leute", "gefeiert", "geaendert", "offen", "rolle_bis"):
            if not isinstance(g.get(feld), dict):
                g[feld] = {}
        if not isinstance(g.get("rolle_id"), int) or isinstance(g.get("rolle_id"), bool):
            g["rolle_id"] = 0
        if not isinstance(g.get("geprueft"), str):
            g["geprueft"] = ""
        return g

    @staticmethod
    def _person(g, uid):
        """Der gueltige Eintrag einer Person - oder None."""
        p = g["leute"].get(str(int(uid)))
        if not isinstance(p, dict) or not _gueltig(p.get("t"), p.get("m")):
            return None
        return p

    def eintrag(self, gid, uid):
        """(tag, monat) einer Person auf einem Server - oder None."""
        if self._store is None:
            return None
        g = self._guild(gid, anlegen=False)
        p = self._person(g, uid) if g else None
        return (int(p["t"]), int(p["m"])) if p else None

    # --- 'flo geburtstag ...' -------------------------------------------------
    @staticmethod
    def befehl(text):
        """Die Grammatik. ('zeigen',) | ('liste',) | ('loeschen',) |
        ('setzen', t, m, mit_jahr) | ('ungueltig', text) | None (= kein Befehl,
        die KI antwortet)."""
        teile = " ".join(str(text or "").split()).split(None, 1)
        if not teile:
            return None
        wort = teile[0].lower().strip(_RAND)
        if wort not in _ERSTE:
            return None
        rest = (teile[1] if len(teile) > 1 else "").lower().strip(" \t,!?;:\"'„“”`")
        if wort == "geburtstage":
            return ("liste",) if rest.rstrip(".") in _LISTE else None
        if rest.rstrip(".") in _ZEIGEN:
            return ("zeigen",)
        if rest.rstrip(".") in _LOESCHEN:
            return None if "?" in teile[1] else ("loeschen",)
        if rest.rstrip(".") in ("liste", "alle"):
            return ("liste",)
        datum = zerlege_datum(_VOR_DATUM_RE.sub("", rest, count=1))
        if datum is None:
            return None
        if datum[0] == "ungueltig":
            return datum
        return ("setzen",) + datum

    async def handle(self, message):
        """'geburtstag 12.03.', 'geburtstage' -> Antwort; alles andere None."""
        if not self._enabled:
            return None
        inhalt = getattr(message, "content", "") or ""
        klein = inhalt.lower()
        if "geburtstag" not in klein and "birthday" not in klein and "bday" not in klein:
            return None             # jede Chatnachricht kommt hier vorbei - schnell raus
        text = ai.strip_lead(inhalt)
        erstes = text[:14].split(None, 1)
        if not erstes or erstes[0].lower().strip(_RAND) not in _ERSTE:
            return None
        guild = getattr(message, "guild", None)
        autor = getattr(message, "author", None)
        if guild is None or autor is None:
            return None
        befehl = self.befehl(text)
        if befehl is None:
            return None
        # Nie fuer andere: ai.strip_lead hat die Erwaehnung schon weggeschnitten,
        # deshalb hier gegen den getippten Text.
        ziel = basis.erstes_ziel(message)
        if ziel is not None and int(ziel.id) != int(autor.id):
            return random.choice(_FUER_ANDERE).format(
                wer=_sauber(getattr(ziel, "display_name", "der"), 32))
        gid, uid = int(guild.id), int(autor.id)
        name = str(getattr(autor, "display_name", "") or "")
        art = befehl[0]
        if art == "liste":
            return self._liste(guild)
        if art == "zeigen":
            return self._zeigen(gid, uid, name)
        if art == "loeschen":
            return await self._loeschen(gid, uid)
        if art == "ungueltig":
            return random.choice(_UNGUELTIG).format(datum=_sauber(befehl[1], 20))
        _art, t, m, mit_jahr = befehl
        antwort = await self._setzen(gid, uid, name, t, m)
        return (_JAHR + antwort) if mit_jahr else antwort

    def _zeigen(self, gid, uid, name):
        g = self._guild(gid, anlegen=False)
        p = self._person(g, uid) if g else None
        if p is None:
            return _KEINER.format(name=self._bot_name)
        if name and p.get("name") != name[:64]:
            p["name"] = name[:64]
            self._store.save_soon()
        heute = uhr.jetzt().date()
        tage = (naechster(p["t"], p["m"], heute) - heute).days
        datum = _datum_text(p["t"], p["m"])
        if tage == 0:
            return f"Dein Geburtstag: **{datum}** – heute. Dachtest du, ich vergess das?"
        if tage == 1:
            return f"Dein Geburtstag: **{datum}** – morgen. Ich hab's nicht vergessen, keine Sorge."
        return (f"Dein Geburtstag: **{datum}** – noch {tage} Tage. Ich zähl nicht mit, "
                "versprochen.")

    def _frei_ab(self, g, k, heute_key):
        """Ab wann diese Person ihr Datum wieder aendern darf - None = jetzt."""
        zuletzt = _als_datum(uhr.glaubhaft(g["geaendert"].get(k), heute_key))
        return zuletzt + timedelta(days=AENDERN_ABSTAND) if zuletzt else None

    async def _setzen(self, gid, uid, name, t, m):
        """Eintragen oder aendern. Die Sperre und der Vorlauf stehen sofort auf
        der Platte - beides gilt fuers Geld."""
        async with self._sperre():
            g = self._guild(gid)
            k = str(uid)
            jetzt = uhr.jetzt()
            heute, heute_key = jetzt.date(), uhr.tag_key(jetzt)
            datum = _datum_text(t, m)
            p = self._person(g, uid)
            if p is not None and (int(p["t"]), int(p["m"])) == (t, m):
                if name and p.get("name") != name[:64]:
                    p["name"] = name[:64]
                    self._store.save_soon()
                return _SCHON.format(datum=datum)
            # Wer schon einmal ein Datum hatte (auch ein geloeschtes), aendert.
            aenderung = p is not None or k in g["geaendert"]
            if aenderung:
                frei = self._frei_ab(g, k, heute_key)
                if frei is not None and heute < frei:
                    return _GESPERRT.format(ab=frei.strftime("%d.%m.%Y"))
                g["geaendert"][k] = heute_key
            else:
                g["geaendert"][k] = ""
            if not g["leute"] and not g["geprueft"] and uhr.stabil():
                # Der erste Eintrag auf diesem Server: bis gestern ist nichts
                # nachzuholen - sonst gratulierte der erste Takt einem, der
                # gestern Geburtstag hatte, "nachtraeglich, weil ich offline war".
                g["geprueft"] = uhr.tag_key(jetzt - timedelta(days=1))
            g["leute"][k] = {"t": int(t), "m": int(m), "name": name[:64], "seit": heute_key}
            await self._store.save()
        log.info("Geburtstage: Eintrag auf Server %s %s.", gid,
                 "geaendert" if aenderung else "neu")
        if aenderung:
            text = (f"Geändert auf **{datum}**. Das nächste Mal frühestens in "
                    f"{AENDERN_ABSTAND} Tagen – so oft wird keiner geboren.")
        else:
            text = (f"Eingetragen: **{datum}**. Ich denk dran – im Gegensatz zu deinen "
                    "sogenannten Freunden.")
        naechstes = naechster(t, m, heute)
        if not uhr.spaeter_als(str(naechstes.year), g["gefeiert"].get(k)):
            text += " Gefeiert wurdest du dieses Jahr schon – nochmal gibt's nicht."
        elif self.betrag(gid) <= 0:
            pass                        # ohne Geschenk gibt es nichts zu erklaeren
        elif not uhr.spaeter_als(str(naechstes.year), self._bezahlt().get(k)):
            text += (" Coins hast du dieses Jahr schon auf einem anderen Server kassiert – "
                     "hier gibt's nur den Glückwunsch.")
        elif (naechstes - heute).days < VORLAUF_TAGE:
            text += (f" Für dieses Mal zu knapp: Coins gibt's nur, wenn das Datum "
                     f"mindestens {VORLAUF_TAGE} Tage vorher drinsteht. Glückwunsch "
                     "kriegst du trotzdem.")
        else:
            text += (f" Geschenk gibt's nur, wenn das Datum {VORLAUF_TAGE} Tage vorher "
                     "drinsteht – das hast du geschafft. Applaus.")
        return text

    async def _loeschen(self, gid, uid):
        """Austragen. 'gefeiert' und die Aenderungs-Sperre bleiben stehen -
        sonst waere Loeschen und neu Eintragen der Weg um beides herum."""
        g = self._guild(gid, anlegen=False)
        if g is None or self._person(g, uid) is None:
            return _NIX_ZU_LOESCHEN
        k = str(uid)
        g["leute"].pop(k, None)
        g["geaendert"].setdefault(k, "")
        await self._store.save()
        return _GELOESCHT

    def _liste(self, guild):
        g = self._guild(guild.id, anlegen=False)
        heute = uhr.jetzt().date()
        leute = []
        for k, p in ((g or {}).get("leute") or {}).items():
            if not isinstance(p, dict) or not _gueltig(p.get("t"), p.get("m")):
                continue
            tag = naechster(p["t"], p["m"], heute)
            mitglied = None
            try:
                mitglied = guild.get_member(int(k))
            except Exception:  # noqa: BLE001 - Attrappen, kaputter Schluessel
                mitglied = None
            name = getattr(mitglied, "display_name", None) or p.get("name") or f"#{k}"
            leute.append(((tag - heute).days, int(p["m"]), int(p["t"]), str(name)))
        if not leute:
            return _LISTE_LEER
        leute.sort()
        zeilen = []
        for tage, m, t, name in leute[:LISTE_MAX]:
            wann = ("heute 🎂" if tage == 0 else "morgen" if tage == 1
                    else f"in {tage} Tagen")
            zeilen.append(f"• **{_datum_text(t, m)}** – {_sauber(name)} ({wann})")
        return "🎂 **Die nächsten Geburtstage hier:**\n" + "\n".join(zeilen)

    # --- Der Takt -------------------------------------------------------------
    async def tick(self, client):
        """Ab 9 Uhr gratulieren - auch nachtraeglich, wenn Flo offline war."""
        if not self._enabled or self._store is None:
            return
        if not uhr.stabil():
            return                      # Startgnade oder Uhrsprung - spaeter
        jetzt = uhr.jetzt()
        guilds = list(getattr(client, "guilds", None) or ())
        # Rollen von gestern abnehmen - auch vor 9 Uhr und in der Sendepause
        # (das ist Aufraeumen, kein Post).
        for guild in guilds:
            try:
                await self._rollen_abnehmen(guild, uhr.tag_key(jetzt))
            except Exception:  # noqa: BLE001 - ein Server haelt die anderen nie auf
                log.exception("Geburtstags-Rolle auf '%s' nicht abgenommen",
                              getattr(guild, "name", "?"))
        if jetzt.hour < GLUECKWUNSCH_STUNDE:
            return
        if _sendepause():
            return                      # verschoben, nicht verloren (Nachholen)
        for guild in guilds:
            try:
                await self._server(client, guild, jetzt)
            except Exception:  # noqa: BLE001
                log.exception("Geburtstage auf '%s' fehlgeschlagen",
                              getattr(guild, "name", "?"))

    async def _server(self, client, guild, jetzt):
        """Einen Server durchgehen: liegengebliebene Glueckwuensche, dann jeder
        Tag ab 'geprueft' bis heute (hoechstens NACHHOL_TAGE zurueck)."""
        gid = int(guild.id)
        g = self._guild(gid, anlegen=False)
        if g is None or not features.is_on_in(gid, "geburtstage"):
            return
        heute, heute_key = jetzt.date(), uhr.tag_key(jetzt)
        await self._offene_nachholen(client, guild, g, heute)
        geprueft_key = uhr.glaubhaft(g.get("geprueft"), heute_key)
        geprueft = _als_datum(geprueft_key)
        # Noch nie geprueft (oder kaputt): dasselbe Fenster - ein Server, auf
        # dem der Takt bisher nie durchkam (Sendepause, Schalter aus), holt so
        # die letzten Tage nach wie jeder andere.
        start = heute - timedelta(days=NACHHOL_TAGE)
        if geprueft is not None and geprueft + timedelta(days=1) > start:
            start = geprueft + timedelta(days=1)
        tag = start
        while tag <= heute:
            for k, p in list(g["leute"].items()):
                if not isinstance(p, dict) or not _gueltig(p.get("t"), p.get("m")):
                    continue
                if feiertag(p["t"], p["m"], tag.year) != tag:
                    continue
                try:
                    await self._feiern(client, guild, g, int(k), p, tag, heute)
                except (TypeError, ValueError):
                    log.warning("Geburtstage: kaputter Eintrag %r auf Server %s.", k, gid)
            tag += timedelta(days=1)
        # Alles VOR heute ist durch; heute wird bei jedem Takt angesehen (wer
        # sich heute eintraegt und heute Geburtstag hat, wird noch gefeiert).
        gestern = uhr.tag_key(jetzt - timedelta(days=1))
        if uhr.spaeter_als(gestern, geprueft_key):
            g["geprueft"] = gestern
            self._store.save_soon()

    async def _feiern(self, client, guild, g, uid, p, tag, heute):
        """Eine Person an 'tag' feiern - einmal im Jahr, bezahlt nach dem
        Festhalten. Der Post kommt danach (und wird nachgeholt, wenn er
        scheitert)."""
        gid, k = int(guild.id), str(uid)
        if not uhr.spaeter_als(str(tag.year), g["gefeiert"].get(k)):
            return                      # dieses Jahr schon dran
        mitglied = await self._mitglied(guild, uid)
        if mitglied is False:
            return                      # Discord zickt - naechster Takt
        betrag, grund = self._geschenk(gid, p, tag)
        if mitglied is None:
            # Nicht mehr auf dem Server: kein Glueckwunsch, keine Coins - aber
            # 'gefeiert', sonst fragte jeder Takt Discord erneut.
            betrag, grund = 0, "weg"
        async with self._sperre():
            if not uhr.spaeter_als(str(tag.year), g["gefeiert"].get(k)):
                return
            # shield: bricht der Takt ab (ALLTAG_FRIST), wird trotzdem zu Ende
            # festgehalten und bezahlt - nie das eine ohne das andere.
            eintrag = await asyncio.shield(
                self._festhalten_und_zahlen(gid, g, uid, tag, betrag, grund))
        if mitglied is None:
            # Nicht mehr hier: auch aus der Liste ('flo geburtstage' zeigte
            # sonst Name und Datum fuer immer). 'gefeiert' und die Sperre
            # bleiben - wer wiederkommt, faengt nicht bei null an.
            g["offen"].pop(k, None)
            g["leute"].pop(k, None)
            self._store.save_soon()
            log.info("Geburtstage: %s ist nicht mehr auf Server %s - kein Glueckwunsch, "
                     "Eintrag weg.", uid, gid)
            return
        if tag == heute or tag + timedelta(days=1) == heute:
            await self._rolle_geben(guild, g, uid, heute + timedelta(days=1))
        await self._verkuenden(client, guild, g, uid, eintrag, heute, mitglied=mitglied)

    @staticmethod
    def betrag(gid):
        """Das Geschenk auf diesem Server - 0 ohne Wirtschaft (Modul oder
        Schalter aus) oder wenn der Server 0 eingestellt hat."""
        if not economy.is_enabled() or not features.is_on_in(gid, "economy"):
            return 0
        return max(0, min(COINS_MAX, _ganz(guildcfg.get(gid, "geburtstag_coins"))))

    def _geschenk(self, gid, p, tag):
        """(betrag, grund): was das Geburtstagskind bekommt - und warum nicht.
        grund: '' (bezahlt oder nichts eingestellt), 'zu_knapp' (zu kurz
        vorher eingetragen)."""
        betrag = self.betrag(gid)
        if betrag <= 0:
            return 0, ""
        seit = _als_datum(p.get("seit"))
        if seit is None or (tag - seit).days < VORLAUF_TAGE:
            return 0, "zu_knapp"
        return betrag, ""

    async def _festhalten_und_zahlen(self, gid, g, uid, tag, betrag, grund):
        """Lock-before-pay: erst 'gefeiert', das Kassenbuch (ueber alle Server)
        und den offenen Glueckwunsch auf die Platte, DANN die Coins. Kam das
        nicht auf die Platte (Datei nicht schreibbar - economy.json aber
        schon), wird NICHT gezahlt: sonst zahlte jeder Neustart im
        Nachhol-Fenster noch einmal."""
        k, jahr = str(uid), str(tag.year)
        g["gefeiert"][k] = jahr
        kasse_vorher = self._bezahlt().get(k)
        if betrag > 0:
            if uhr.spaeter_als(jahr, kasse_vorher):
                self._bezahlt()[k] = jahr
            else:
                # Dieses Jahr schon auf einem anderen Server bezahlt.
                betrag, grund = 0, "anderswo"
        eintrag = g["offen"][k] = {"tag": uhr.tag_key(tag), "betrag": betrag,
                                   "bezahlt": False, "grund": grund, "versuche": 0}
        if await self._store.save() is False and betrag > 0:
            log.error("Geburtstage: geburtstage.json nicht schreibbar - %s bekommt KEINE "
                      "Coins (sonst zahlte jeder Neustart noch mal).", uid)
            if kasse_vorher is None:
                self._bezahlt().pop(k, None)
            else:
                self._bezahlt()[k] = kasse_vorher
            betrag = eintrag["betrag"] = 0
        if betrag > 0:
            economy.add_coins(uid, betrag, reason="geburtstag")
            try:
                await economy.flush()
            except Exception:  # noqa: BLE001 - economy sichert ohnehin gesammelt
                log.debug("economy.flush nach Geburtstags-Geschenk fehlgeschlagen",
                          exc_info=True)
        eintrag["bezahlt"] = True
        self._store.save_soon()
        log.info("Geburtstage: %s auf Server %s gefeiert (%s, +%d Coins%s).", uid, gid,
                 eintrag["tag"], betrag, f", {grund}" if grund else "")
        return eintrag

    @staticmethod
    async def _mitglied(guild, uid):
        """Das Mitglied (Cache, sonst fetch_member) - None, wenn es nicht mehr
        auf dem Server ist, False, wenn Discord gerade nicht antwortet."""
        try:
            mitglied = guild.get_member(uid)
        except Exception:  # noqa: BLE001
            mitglied = None
        if mitglied is not None:
            return mitglied
        try:
            return await guild.fetch_member(uid)
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            log.warning("Geburtstage: Mitglied %s nicht abrufbar (%s) - spaeter.", uid, exc)
            return False

    async def _offene_nachholen(self, client, guild, g, heute):
        """Glueckwuensche, die Discord abgelehnt hat (oder die ein abgebrochener
        Takt nicht mehr geschafft hat): nochmal - ohne noch einmal zu zahlen."""
        for k, eintrag in list(g["offen"].items()):
            tag = _als_datum((eintrag or {}).get("tag")) if isinstance(eintrag, dict) else None
            if (tag is None or (heute - tag).days > NACHHOL_TAGE
                    or _ganz(eintrag.get("versuche")) >= VERSUCHE):
                g["offen"].pop(k, None)
                self._store.save_soon()
                log.warning("Geburtstage: Glueckwunsch fuer %s auf Server %s kam nicht "
                            "raus - ich lass es.", k, guild.id)
                continue
            ab = self._nochmal_ab.get(int(guild.id), {}).get(k)
            if ab is not None and _mono() < ab:
                continue
            mitglied = await self._mitglied(guild, int(k))
            if mitglied is False:
                continue
            if mitglied is None:
                g["offen"].pop(k, None)
                g["leute"].pop(k, None)
                self._store.save_soon()
                continue
            await self._verkuenden(client, guild, g, int(k), eintrag, heute,
                                   mitglied=mitglied)

    async def _verkuenden(self, client, guild, g, uid, eintrag, heute, *, mitglied):
        """Der Glueckwunsch in den Geburtstags-Kanal. Klappt er, ist 'offen' weg."""
        gid, k = int(guild.id), str(uid)
        eintrag["versuche"] = _ganz(eintrag.get("versuche")) + 1
        self._nochmal_ab.setdefault(gid, {})[k] = _mono() + NOCHMAL_NACH
        self._store.save_soon()
        kanal = self._kanal(client, guild)
        if kanal is None:
            log.warning("Geburtstage auf '%s': kein Kanal, in den Flo darf und der kein "
                        "Aufraeum-Kanal ist - geburtstag_channel einstellen.",
                        getattr(guild, "name", gid))
            return False
        text = await self.glueckwunsch(gid, uid, mitglied, eintrag, heute)
        erlaubt = discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                          users=[discord.Object(uid)])
        try:
            await kanal.send(text, allowed_mentions=erlaubt)
        except discord.HTTPException as exc:
            log.warning("Geburtstags-Glueckwunsch ging nicht raus: %s", exc)
            return False
        g["offen"].pop(k, None)
        self._nochmal_ab.get(gid, {}).pop(k, None)
        self._store.save_soon()
        return True

    async def glueckwunsch(self, gid, uid, mitglied, eintrag, heute):
        """Der ganze Post: Ping, eine Zeile, das Geschenk (oder warum nicht)."""
        name = str(getattr(mitglied, "display_name", "") or "")
        if not name or _notlage(name):
            name = OHNE_NAME
        sanft = self._kuerzlich_notlage(uid)
        if sanft:
            zeile = random.choice(_SANFT).format(name=_sauber(name))
        else:
            zeile = (await self._ki_zeile(gid, name)
                     or random.choice(_ERSATZ).format(name=_sauber(name)))
        betrag = _ganz(eintrag.get("betrag"))
        if betrag > 0 and eintrag.get("bezahlt"):
            zeile += " " + (_COINS_SANFT if sanft else random.choice(_COINS)).format(
                n=fmt(betrag))
        elif eintrag.get("grund") == "zu_knapp" and not sanft:
            zeile += " " + random.choice(_ZU_KNAPP)
        elif eintrag.get("grund") == "anderswo" and not sanft:
            zeile += " " + _ANDERSWO
        tag = _als_datum(eintrag.get("tag"))
        vorne = _NACHTRAEGLICH if tag is not None and tag < heute else ""
        return f"🎂 {vorne}<@{int(uid)}> {zeile}"[:2000]

    @staticmethod
    def _kuerzlich_notlage(uid):
        """Hat die Person gerade ein Notsignal geschrieben (basis)? Dann kein
        Spott - auch nicht zum Geburtstag."""
        return basis.kuerzlich_notlage(uid)

    async def _ki_zeile(self, gid, name):
        """Zwei Saetze von der KI - None, wenn sie nicht darf, nicht kann oder
        etwas liefert, das nicht in einen Glueckwunsch gehoert."""
        if not features.is_on_in(gid, "ki") or _sendepause():
            return None
        try:
            if not ai.is_enabled():
                return None
            with ai.guild_kontext(gid):
                system = (
                    f"Du bist {ai.bot_name(gid)}, ein frecher deutscher Discord-Bot mit losem "
                    "Mundwerk. Heute hat jemand im Server Geburtstag, und du gratulierst in "
                    "GENAU ZWEI Saetzen auf Deutsch: passiv-aggressiv, ein Seitenhieb aufs "
                    "Aelterwerden oder darauf, dass die Person nichts auf die Reihe kriegt - "
                    "am Ende aber doch ein Glueckwunsch. Kein Spott ueber Aussehen, Gewicht, "
                    "Krankheit oder Tod. Keine Emojis, kein Vorwort, keine Coins erwaehnen. "
                    f"{ai.FloAI._GUARDRAIL}")
                if name == OHNE_NAME:
                    prompt = ("Den Namen lassen wir weg - sprich die Person einfach mit "
                              "'du' an. Schreib jetzt deinen Glueckwunsch.")
                else:
                    prompt = f"Geburtstagskind: {name[:40]}\nSchreib jetzt deinen Glueckwunsch."
                out = await asyncio.wait_for(ai.generate(
                    prompt, system=system, temperature=1.0, max_tokens=120,
                    hintergrund=True), KI_FRIST)
        except Exception:  # noqa: BLE001 - dann eben der Ersatz (auch bei Frist)
            log.debug("Geburtstage: KI-Zeile gescheitert", exc_info=True)
            return None
        out = re.sub(r"\s+", " ", str(out or "")).strip().strip("\"'„“”").strip()
        if (not out or ai.instance._ist_verweigerung(out) or ai.instance.ist_notlage(out, out)
                or self._heikel(out)):
            return None
        return ai.instance._kuerzen(out).replace("@", "")

    @staticmethod
    def _heikel(text):
        try:
            import fun
            import gehirn
            return bool(gehirn.Gehirn._heikel(text) or fun.ist_hetze(text))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _kanal(client, guild):
        """Der Geburtstags-Kanal (guildcfg 'geburtstag_channel'), sonst die
        Ansagen, sonst der System-Kanal - aber nie ein Aufraeum-Kanal
        (autodelete_channels): dort loescht bot.py auch Flos Glueckwunsch nach
        einer Minute, und mit ihm den Ping. Auf dem Hauptserver ist der
        Ansagen-Kanal ab Werk genau so einer. None = keiner taugt."""
        return basis.kanal_ohne_aufraeumen(client, guild, "geburtstag_channel")

    # --- Die Rolle ------------------------------------------------------------
    @staticmethod
    def _darf_rollen(guild):
        ich = getattr(guild, "me", None)
        return bool(getattr(getattr(ich, "guild_permissions", None), "manage_roles", False))

    async def _rolle_geben(self, guild, g, uid, bis):
        """Die Rolle "🎂 Geburtstagskind" bis 'bis' (date) - nur mit guildcfg
        'geburtstag_rolle' und dem Recht "Rollen verwalten". 'rolle_bis' steht
        VOR dem Vergeben auf der Platte: stirbt Flo dazwischen, nimmt er sie
        trotzdem wieder."""
        if not guildcfg.an(guild.id, "geburtstag_rolle"):
            return False
        if not self._darf_rollen(guild):
            log.warning("Geburtstage: Rolle auf '%s' geht nicht - mir fehlt "
                        "'Rollen verwalten'.", getattr(guild, "name", guild.id))
            return False
        rid = _ganz(g.get("rolle_id"))
        rolle = (guild.get_role(rid) if rid else None) or discord.utils.get(
            guild.roles, name=ROLLE)
        if rolle is None:
            try:
                rolle = await guild.create_role(name=ROLLE, colour=discord.Colour(ROLLE_FARBE),
                                                mentionable=False,
                                                reason="Flo: Geburtstagskind")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Geburtstage: Rolle '%s' nicht anlegbar auf '%s': %s",
                            ROLLE, getattr(guild, "name", guild.id), exc)
                return False
        g["rolle_id"] = int(rolle.id)
        k = str(uid)
        g["rolle_bis"][k] = uhr.tag_key(bis) if hasattr(bis, "strftime") else str(bis)
        await self._store.save()
        try:
            mitglied = await guild.fetch_member(uid)
            if rolle not in mitglied.roles:
                await mitglied.add_roles(rolle, reason="Flo: Geburtstag")
        except discord.NotFound:
            g["rolle_bis"].pop(k, None)
            self._store.save_soon()
            return False
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Geburtstage: Rolle fuer %s ging nicht (%s) - steht Flos Rolle "
                        "ueber '%s'?", uid, exc, ROLLE)
            g["rolle_bis"].pop(k, None)
            self._store.save_soon()
            return False
        return True

    async def _rollen_abnehmen(self, guild, heute_key):
        """Wessen Tag vorbei ist (rolle_bis <= heute), dem nimmt Flo die Rolle
        wieder - per fetch_member, der Cache kennt die Leute ohne Mitglieder-
        Intent oft nicht. Klappt es nicht, bleibt der Eintrag stehen (Pause
        ROLLE_NOCHMAL_NACH, nach ROLLE_AUFGEBEN_TAGE aufgegeben)."""
        if self._store is None:
            return
        g = self._guild(guild.id, anlegen=False)
        if g is None or not g["rolle_bis"]:
            return
        faellig = [k for k, bis in g["rolle_bis"].items()
                   if not uhr.spaeter_als(uhr.glaubhaft(bis, heute_key), heute_key)]
        if not faellig:
            return
        gid = int(guild.id)
        ab = self._rolle_nochmal_ab.get(gid)
        if ab is not None and _mono() < ab:
            return
        heute = _als_datum(heute_key)

        def zu_alt(k):
            bis = _als_datum(g["rolle_bis"].get(k))
            return bis is None or heute is None or (heute - bis).days > ROLLE_AUFGEBEN_TAGE

        rid = _ganz(g.get("rolle_id"))
        rolle = (guild.get_role(rid) if rid else None) or discord.utils.get(
            getattr(guild, "roles", None) or [], name=ROLLE)
        if rolle is None:
            for k in faellig:           # Rolle weg - dann hat sie auch keiner mehr
                g["rolle_bis"].pop(k, None)
            self._store.save_soon()
            return
        if not self._darf_rollen(guild):
            self._rolle_nochmal_ab[gid] = _mono() + ROLLE_NOCHMAL_NACH
            for k in [k for k in faellig if zu_alt(k)]:
                g["rolle_bis"].pop(k, None)
                log.warning("Geburtstage: Rolle von %s auf '%s' nicht abnehmbar - "
                            "ich geb's auf.", k, getattr(guild, "name", gid))
            self._store.save_soon()
            return
        for k in faellig:
            try:
                mitglied = await guild.fetch_member(int(k))
                if rolle in mitglied.roles:
                    await mitglied.remove_roles(rolle, reason="Flo: Geburtstag vorbei")
            except discord.NotFound:
                pass                    # nicht mehr auf dem Server
            except (discord.Forbidden, discord.HTTPException) as exc:
                self._rolle_nochmal_ab[gid] = _mono() + ROLLE_NOCHMAL_NACH
                if not zu_alt(k):
                    log.warning("Geburtstage: Rolle von %s nicht genommen (%s) - spaeter "
                                "nochmal.", k, exc)
                    continue
            except (TypeError, ValueError):
                pass                    # kaputter Schluessel
            g["rolle_bis"].pop(k, None)
        self._store.save_soon()


instance = Geburtstage()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
tick = instance.tick
