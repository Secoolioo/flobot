"""Musik-Wrapped: was lief diesen Monat, und wer ist DJ des Monats ('flo wrapped').

Wie es sich anfuehlt
====================
    flo wrapped                  Karte des laufenden Monats: Top-Songs, wer am
                                 meisten gewuenscht hat, dazu ein Spruch
    flo wrapped letzter monat    dasselbe fuer den Vormonat
    flo wrapped posten           nur der Besitzer: das Monats-Wrapped JETZT
                                 (Testausloeser - siehe _abschliessen)

Am 1. eines Monats ab 10 Uhr postet Flo das Wrapped des Vormonats in den
Wrapped-Kanal (guildcfg 'wrapped_channel', sonst die Ansagen, sonst der
System-Kanal - nie in einen Aufraeum-Kanal, dort waere es nach einer Minute
weg, siehe basis.kanal_ohne_aufraeumen) - einmal je Server. Wer die meisten
Songs gewuenscht hat, ist DJ des Monats: Coins (guildcfg 'dj_belohnung') und,
wenn eingeschaltet, die Rolle "🎧 DJ des Monats" (guildcfg 'dj_rolle', auf dem
Hauptserver von Haus aus an). Wer sie hat, merkt sich Flo erst, wenn Discord
sie wirklich vergeben hat - genommen wird sie genau denen wieder, im naechsten
Monat (und in einem Monat ohne DJ).

Die Coins gibt es je Person hoechstens EINMAL im Monat, ueber alle Server
(Kassenbuch 'bezahlt'): wer auf fuenf Servern DJ ist, wird fuenfmal
ausgerufen, aber einmal bezahlt - sonst druckte sich jeder mit ein paar
Privatservern Geld (der Deckel von 'dj_belohnung' gilt nur je Server).

Hat der DJ gerade ein Notsignal geschrieben (basis.kuerzlich_notlage), wird er
nicht vorgefuehrt: kein Ping, keine KI, kein Spott - nur eine neutrale Zeile.
Coins und Rolle bekommt er trotzdem.

Wo was aufgerufen wird:
    handle(message)        Handler-Kette, direkt VOR music.handle
    notieren(gid, track)   music.verlauf_notieren, SYNCHRON, bei jedem neu
                           gestarteten Song - nicht bei wiederhergestellten
                           (nach einem Neustart oder nach dem Parken: die
                           liefen schon einmal)
    zuruecknehmen(gid, track, gehoert)
                           music.GuildPlayer._start_zuruecknehmen (skip und
                           disconnect: 'flo stop', Leerlauf, Rauswurf,
                           Aufraeumen), SYNCHRON: der laufende Song fliegt
                           nach 'gehoert' Sekunden raus
    tick(client)           bot.alltag_loop, jede Minute (hoechstens
                           bot.ALLTAG_FRIST, dann abgebrochen). Die Sendepause
                           prueft bot.py hier NICHT - tick fragt admin.is_locked()
                           selbst und holt das Wrapped danach nach

Gezaehlt wird, was STARTET ("sek" ist die Laenge der Songs, nicht die
gehoerte Zeit) - ausser einem Song, der in den ersten MIN_GEHOERT Sekunden
weggeskippt oder weggestoppt wird (bei kurzen Songs: vor der Haelfte): den
hat keiner gehoert, sein Start wird zurueckgenommen (zuruecknehmen). Sonst
skippte sich jeder in ein paar Minuten zum DJ des Monats - eigene Wuensche
skippt man ohne Abstimmung, eine 200-Song-Playlist ist ein Klick, und
'flo spiel X', 'flo stop' geht genauso schnell. Radio-Songs
(Endlos-Radio) zaehlen nur in "radio" - keiner
hat sie gewuenscht, also gehoeren sie weder in die Top-Songs noch zum DJ. Loop-
Durchlaeufe, Tempo- und Watchdog-Neustarts sind kein neuer Start (music.py
ruft dort nicht).

Die Uhr (uhr.py): gepostet wird nur bei stabiler Uhr, und 'gepostet' laeuft nur
vorwaerts (spaeter_als) - springt die Uhr nach dem Booten zurueck, feuert
nichts doppelt. Wer am 1. offline war, bekommt das Wrapped bis zum 7. nach
(NACHHOL_TAGE); danach faellt der Monat aus. In der Sendepause wartet es.

Bezahlt wird nach "lock-before-pay" (wie beim Giveaway): erst 'gepostet' und
der DJ auf die Platte, DANN die Coins. Stirbt Flo dazwischen, fehlen dem DJ
schlimmstenfalls die Coins - doppelt bezahlt wird nie.

Der Post selbst und die Rolle sind davon getrennt ('verkuendet', 'rolle'):
bricht der Takt nach dem Bezahlen ab (ALLTAG_FRIST) oder lehnt Discord den Post
ab, holt der Takt nach, was fehlt - ohne noch einmal zu zahlen, hoechstens
VERSUCHE Mal im Abstand von NOCHMAL_NACH und nur im Nachhol-Fenster.

Daten: data/musikstats.json
    {"bezahlt": {"<uid>": "2026-09"},              (ueber alle Server)
     "guilds": {"<gid>": {"monate": {"2026-10": {
        "songs": {"<key>": {"t": Titel, "u": Seite, "n": 12, "sek": 2400}},
        "wer": {"<uid>": {"n": 30, "sek": 6000, "name": "Bob"}},
        "gesamt": 120, "sek": 26000, "radio": 14}},
      "gepostet": "2026-09",
      "dj": {"monat": "2026-09", "uid": 123, "betrag": 5000, "bezahlt": true,
             "grund": ""|"anderswo", "verkuendet": true, "rolle": true,
             "versuche": 1},
      "rolle": {"id": 555, "bei": [123]}}}}

Beim Import passiert nichts.
"""

import asyncio
import logging
import os
import random
import re
import time

import discord

import ai
import basis
from basis import FeatureBasis
import economy
import features
import guildcfg
import render
import uhr
from store import JsonStore

log = logging.getLogger("dcbot.musikstats")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("wrapped", "musikwrapped")
_ERSTE = frozenset(_CMDS)

#: So viele verschiedene Songs merkt sich ein Monat - darueber fliegt der mit
#: den wenigsten Starts raus (eine Party mit 2000 Songs sprengt sonst die Datei).
MAX_SONGS = 500
#: So viele Monate bleiben stehen (ein Jahr plus der laufende).
MAX_MONATE = 13
#: Darunter gibt es kein Monats-Wrapped (und keinen DJ).
MIN_SONGS = 10
#: Wer einen Song frueher wegskippt (bei kurzen Songs: vor der Haelfte), hat
#: ihn nicht gehoert - sein Start zaehlt dann nicht.
MIN_GEHOERT = 30.0
#: Am 1. ab dieser Stunde (Ortszeit), nachgeholt wird bis zum NACHHOL_TAGE.
POST_STUNDE = 10
NACHHOL_TAGE = 7
#: 'flo wrapped' hoechstens so oft je Server (Bild + KI kosten).
ABKUEHLUNG = 60.0
#: So viele Songs und Leute zeigt die Karte.
TOP_SONGS = 5
TOP_LEUTE = 4
DJ_ROLLE = "🎧 DJ des Monats"
DJ_FARBE = 0xF1C40F
#: Bricht der Monats-Post ab (Takt-Frist, Discord lehnt ab, kein Kanal), so oft
#: versucht es der Takt - im Abstand von NOCHMAL_NACH Sekunden (bezahlt wird
#: dabei nie noch einmal).
VERSUCHE = 6
NOCHMAL_NACH = 30 * 60.0
#: So steht der DJ im Spruch, wenn sein Name ein Notsignal enthaelt.
DJ_OHNE_NAME = "DJ"

_MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember")

# Was hinter 'wrapped' stehen darf - alles andere ist ein Satz ('flo wrapped
# ist cool'), und den beantwortet die KI.
_RAND = " \t.,!?;:\"'„“”`"
_VORMONAT = frozenset((
    "letzter monat", "letzten monat", "letzte monat", "vormonat", "vorigen monat",
    "voriger monat", "vorheriger monat", "vorherigen monat", "vom letzten monat",
    "für letzten monat", "fuer letzten monat", "last month", "letzter", "letzten"))
_LAUFEND = frozenset(("", "diesen monat", "dieser monat", "aktuell", "jetzt",
                      "this month", "bisher"))
_POSTEN = frozenset(("posten", "jetzt posten", "post", "test"))

_ZU_FRUEH = ("Hab ich dir doch grad gezeigt. Scroll hoch, du Goldfisch.",
             "Nochmal? Die Zahlen haben sich in der Minute nicht geändert, Genie.",
             "Eine Minute Pause. So spannend ist euer Musikgeschmack echt nicht.")
_NICHTS = ("Im {monat} lief hier noch gar nix. Stille ist auch ein Geschmack – "
           "ein schlechter.")
_NUR_CHEF = "Den Knopf drückt nur der Chef. Du kriegst dein Wrapped am Ersten wie alle anderen."

# Wenn die KI nicht kann (aus, Sendepause, 429, Verweigerung): derselbe Ton.
_ERSATZ = (
    "{song} lief hier öfter als jeder vernünftige Song. {dj}, du bist offiziell schuld daran.",
    "Ein ganzer Monat Musik, und das Highlight war {song}? {dj} hat gewünscht, als gäb's "
    "kein Morgen – Geschmack hat das trotzdem nicht ersetzt.",
    "Ich hab {song} so oft gespielt, dass ich's im Schlaf summe. Danke für nichts, {dj}.",
    "Platz eins: {song}. Mehr muss man über euch nicht wissen. {dj}, nimm die Krone und "
    "schäm dich ein bisschen.",
)
_ERSATZ_OHNE_DJ = (
    "{song} auf Platz eins, und gewünscht hat sich keiner was – das Radio hat euren Monat "
    "gerettet. Traurig.",
    "Keiner hat sich was gewünscht, aber {song} lief trotzdem dauernd. Ihr seid wirklich "
    "pflegeleicht. Und langweilig.",
)
# Kein Song, den man nennen kann (nur Radio - oder nichts, was Flo zitieren will).
_ERSATZ_OHNE_SONG = (
    "Was hier lief, sag ich lieber nicht laut. {dj}, du weißt, was du getan hast.",
    "Einen ganzen Monat Musik, und nichts davon war der Rede wert. {dj}, du warst "
    "trotzdem der Lauteste.",
)
#: Statt des Spruchs, wenn der DJ gerade ein Notsignal geschrieben hat.
_SANFT = "Glückwunsch an den DJ. Heute ausnahmsweise ohne Spruch."
#: Der DJ hat diesen Monat schon auf einem anderen Server kassiert.
_ANDERSWO = ("Coins gibt's hier keine – die hat sich der DJ diesen Monat schon auf "
             "einem anderen Server abgeholt. Einmal reicht, du Raffzahn.")
_ERSATZ_LEER = (
    "Nur Radio, kein einziger Wunsch. Ihr lasst euch echt alles vorsetzen.",
    "Keiner hat sich was gewünscht. Ich bin hier DJ, Jukebox und Geschmack in einem – "
    "gern geschehen.",
)


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


def _ganz(wert):
    try:
        return int(wert or 0)
    except (TypeError, ValueError):
        return 0


def _monatsname(key, *, mit_jahr=False):
    """'2026-10' -> 'Oktober' (bzw. 'Oktober 2026')."""
    m = re.fullmatch(r"([0-9]{4})-([0-9]{2})", str(key or ""))
    if not m or not 1 <= int(m[2]) <= 12:
        return str(key or "?")
    name = _MONATE[int(m[2]) - 1]
    return f"{name} {m[1]}" if mit_jahr else name


def _sauber(text, laenge=80):
    """Fuer den Chat: gekuerzt, ohne Markdown und ohne Ping."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > laenge:
        text = text[:laenge - 1].rstrip() + "…"
    return discord.utils.escape_markdown(text).replace("@", "@​")


def _notlage(text):
    """Notsignal? Darauf reagiert nichts, was Flo von sich aus tut."""
    return bool(ai.FloAI._NOTLAGE_RE.search(str(text or "")))


def _heikel(text):
    """Private Daten oder Hetze im KI-Spruch? Dann lieber der Ersatz - wie bei
    Zitaten und Geburtstagen."""
    try:
        import fun
        import gehirn
        return bool(gehirn.Gehirn._heikel(text) or fun.ist_hetze(text))
    except Exception:  # noqa: BLE001
        return False


def _song_key(track):
    """Woran ein Song wiedererkannt wird: die YouTube-ID (ohne Tracking- und
    Listen-Anhang), sonst die Adresse ohne '?...', sonst der Titel."""
    adresse = str(getattr(track, "webpage_url", "") or "")
    if not adresse:
        query = str(getattr(track, "query", "") or "")
        adresse = query if query.startswith("http") else ""
    if adresse:
        try:
            import music
            vid = music._yt_id(adresse)
        except Exception:  # noqa: BLE001
            vid = ""
        if vid:
            return f"yt:{vid}"
        return adresse.split("#", 1)[0].split("?", 1)[0].lower()[:200]
    return " ".join(str(getattr(track, "title", "") or "").lower().split())[:200]


def _dauer(track):
    d = getattr(track, "duration", None)
    return int(d) if isinstance(d, (int, float)) and 0 < d < 86400 else 0


class Musikstats(FeatureBasis):
    """Monats-Statistik der Musik je Server, das Wrapped und der DJ des Monats."""

    def __init__(self):
        self._enabled = False
        self._store = None
        # 'flo wrapped': monotonic der letzten Karte je Server.
        self._zuletzt = {}
        # Monats-Post (tick) und Testausloeser nie gleichzeitig - sonst zahlte
        # ein Doppelklick im falschen Moment den DJ zweimal.
        self._lock = None
        # Abgebrochener Monats-Post: gid -> monotonic, ab wann der Takt es
        # nochmal versucht (NOCHMAL_NACH).
        self._nochmal_ab = {}
        # Der zuletzt gezaehlte Start je Server (nur im Speicher), damit ein
        # Skip ihn zuruecknehmen kann: gid -> (track, monat, song_key, uid, sek).
        self._letzter = {}

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if os.getenv("MUSIKSTATS_ENABLED", "1").strip().lower() in (
                "0", "false", "no", "off", "aus"):
            log.info("Musik-Wrapped aus (MUSIKSTATS_ENABLED=0).")
            return False
        self._store = JsonStore("musikstats.json", default={"guilds": {}, "bezahlt": {}})
        self._zuletzt = {}
        self._lock = None
        self._nochmal_ab = {}
        self._letzter = {}
        self._enabled = True
        log.info("Musik-Wrapped aktiv (%d Server mit Statistik).",
                 len(self._guilds()))
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
        """Das Kassenbuch ueber alle Server: uid -> der letzte Monat, fuer den
        die Person DJ-Coins bekommen hat."""
        b = self._store.data.setdefault("bezahlt", {})
        if not isinstance(b, dict):
            b = self._store.data["bezahlt"] = {}
        return b

    def _guild(self, gid):
        guilds = self._guilds()
        g = guilds.get(str(int(gid)))
        if not isinstance(g, dict):
            g = guilds[str(int(gid))] = {}
        if not isinstance(g.get("monate"), dict):
            g["monate"] = {}
        return g

    def _monat(self, gid, key):
        """Die Zaehler eines Monats (legt ihn an - und raeumt alte Monate weg)."""
        monate = self._guild(gid)["monate"]
        m = monate.get(key)
        if not isinstance(m, dict):
            m = monate[key] = {"songs": {}, "wer": {}, "gesamt": 0, "sek": 0, "radio": 0}
            for alt in sorted(monate)[:-MAX_MONATE]:
                del monate[alt]
        for feld in ("songs", "wer"):
            if not isinstance(m.get(feld), dict):
                m[feld] = {}
        return m

    def daten(self, gid, key):
        """Die Zaehler eines Monats zum Lesen - None, wenn es keine gibt."""
        if self._store is None:
            return None
        g = self._guilds().get(str(int(gid or 0)))
        m = (g or {}).get("monate", {}).get(key) if isinstance(g, dict) else None
        return m if isinstance(m, dict) else None

    # --- Zaehlen ------------------------------------------------------------
    def notieren(self, gid, track):
        """Einen gestarteten Song zaehlen. Synchron, darf nie werfen."""
        if not self._enabled or self._store is None or not gid or track is None:
            return
        try:
            self._zaehlen(int(gid), track)
        except Exception:  # noqa: BLE001 - Zaehlen darf nie die Musik kippen
            log.debug("Musik-Wrapped: Song nicht gezaehlt", exc_info=True)

    def _zaehlen(self, gid, track):
        if getattr(track, "wiederhergestellt", False):
            return                      # lief schon einmal (Neustart, Parken)
        monat = uhr.monat_key()
        m = self._monat(gid, monat)
        if getattr(track, "radio", False):
            m["radio"] = _ganz(m.get("radio")) + 1
            self._letzter[gid] = (track, monat, None, 0, 0)
            self._store.save_soon()
            return
        dauer = _dauer(track)
        key = _song_key(track)
        if key:
            songs = m["songs"]
            e = songs.get(key)
            if not isinstance(e, dict):
                e = songs[key] = {"t": "", "u": "", "n": 0, "sek": 0}
            e["t"] = str(getattr(track, "title", "") or e.get("t") or "?")[:150]
            e["u"] = str(getattr(track, "webpage_url", "") or e.get("u") or "")[:300]
            e["n"] = _ganz(e.get("n")) + 1
            e["sek"] = _ganz(e.get("sek")) + dauer
            if len(songs) > MAX_SONGS:
                weg = min((k for k in songs if k != key),
                          key=lambda k: (_ganz(songs[k].get("n")), _ganz(songs[k].get("sek"))))
                del songs[weg]
        uid = _ganz(getattr(track, "requested_by_id", 0))
        if uid > 0:
            w = m["wer"].get(str(uid))
            if not isinstance(w, dict):
                w = m["wer"][str(uid)] = {"n": 0, "sek": 0, "name": ""}
            w["n"] = _ganz(w.get("n")) + 1
            w["sek"] = _ganz(w.get("sek")) + dauer
            w["name"] = str(getattr(track, "requested_by", "") or w.get("name") or "")[:64]
        m["gesamt"] = _ganz(m.get("gesamt")) + 1
        m["sek"] = _ganz(m.get("sek")) + dauer
        self._letzter[gid] = (track, monat, key, uid, dauer)
        self._store.save_soon()

    def zuruecknehmen(self, gid, track, gehoert):
        """Der laufende Song fliegt nach 'gehoert' Sekunden raus (Skip, Stopp). War
        das zu frueh (MIN_GEHOERT, bei kurzen Songs die Haelfte), zaehlt sein
        Start nicht - in dem Monat, in dem er gezaehlt wurde. Synchron, darf
        nie werfen."""
        if not self._enabled or self._store is None or not gid or track is None:
            return
        try:
            self._abziehen(int(gid), track, float(gehoert or 0.0))
        except Exception:  # noqa: BLE001 - Zaehlen darf nie die Musik kippen
            log.debug("Musik-Wrapped: Skip nicht verrechnet", exc_info=True)

    def _abziehen(self, gid, track, gehoert):
        letzter = self._letzter.get(gid)
        if letzter is None or letzter[0] is not track:
            return                      # nicht gezaehlt (zurueckgeholt) oder schon verrechnet
        _track, monat, key, uid, dauer = letzter
        if gehoert >= (min(MIN_GEHOERT, dauer / 2) if dauer else MIN_GEHOERT):
            return
        del self._letzter[gid]
        m = self.daten(gid, monat)
        if m is None:
            return

        def weniger(eintrag, feld, um):
            eintrag[feld] = max(0, _ganz(eintrag.get(feld)) - um)

        if key is None:                 # ein Radio-Song
            weniger(m, "radio", 1)
            self._store.save_soon()
            return
        song = m.get("songs", {}).get(key) if isinstance(m.get("songs"), dict) else None
        if isinstance(song, dict):
            weniger(song, "n", 1)
            weniger(song, "sek", dauer)
            if not _ganz(song.get("n")):
                del m["songs"][key]
        wer = m.get("wer", {}).get(str(uid)) if uid > 0 and isinstance(m.get("wer"), dict) else None
        if isinstance(wer, dict):
            weniger(wer, "n", 1)
            weniger(wer, "sek", dauer)
            if not _ganz(wer.get("n")):
                del m["wer"][str(uid)]
        weniger(m, "gesamt", 1)
        weniger(m, "sek", dauer)
        self._store.save_soon()

    # --- Auswerten ------------------------------------------------------------
    @staticmethod
    def top_songs(m, anzahl=TOP_SONGS):
        """[(titel, starts)], meistgespielt zuerst (Gleichstand: laenger gelaufen)."""
        songs = (m or {}).get("songs")
        songs = [e for e in (songs.values() if isinstance(songs, dict) else ())
                 if isinstance(e, dict)]
        songs.sort(key=lambda e: (-_ganz(e.get("n")), -_ganz(e.get("sek")),
                                  str(e.get("t") or "")))
        return [(str(e.get("t") or "?"), _ganz(e.get("n"))) for e in songs[:anzahl]]

    @staticmethod
    def rangliste(m):
        """[(uid, eintrag)] - wer am meisten gewuenscht hat. Gleichstand: wessen
        Songs laenger liefen, dann die kleinere ID (fest, nicht zufaellig)."""
        leute = []
        wer = (m or {}).get("wer")
        for uid, e in (wer.items() if isinstance(wer, dict) else ()):
            if isinstance(e, dict) and _ganz(e.get("n")) > 0 and _ganz(uid) > 0:
                leute.append((_ganz(uid), e))
        leute.sort(key=lambda p: (-_ganz(p[1].get("n")), -_ganz(p[1].get("sek")), p[0]))
        return leute

    def dj(self, m):
        """(uid, eintrag) des DJ des Monats - oder None."""
        leute = self.rangliste(m)
        return leute[0] if leute else None

    def _karte(self, m, key, server, *, laufend):
        """Was auf die Karte kommt: (args, kwargs) fuer render.musik_wrapped.

        Laeuft auf dem Event-Loop - der Thread bekommt nur fertige Listen und
        Zahlen. Den Monat selbst zu lesen, waehrend notieren() ihn auf dem Loop
        aendert, sprengte im laufenden Monat die Iteration ('dictionary changed
        size during iteration') - und die Karte fehlte still."""
        leute = [(str(e.get("name") or f"#{uid}"), _ganz(e.get("n")))
                 for uid, e in self.rangliste(m)[:TOP_LEUTE]]
        return ((_monatsname(key, mit_jahr=True), self.top_songs(m), leute),
                {"gesamt": _ganz(m.get("gesamt")), "sekunden": _ganz(m.get("sek")),
                 "radio": _ganz(m.get("radio")), "server": str(server or ""),
                 "laufend": laufend})

    async def _bild(self, m, key, server, *, laufend):
        """Die Karte als PNG (BytesIO) - gerendert im Thread, oder None."""
        try:
            args, kw = self._karte(m, key, server, laufend=laufend)
            return await asyncio.to_thread(render.musik_wrapped, *args, **kw)
        except Exception:  # noqa: BLE001 - dann eben ohne Bild
            log.exception("Musik-Wrapped: Karte nicht gerendert")
            return None

    # --- Der Spruch -----------------------------------------------------------
    async def _spruch(self, gid, m, key, *, hintergrund):
        """Zwei derbe Saetze zum Monat - von der KI, sonst aus dem Ersatz-Pool.

        Nie zu einem Notsignal: ein Songtitel oder Name, auf den
        ai.FloAI._NOTLAGE_RE anspringt, geht weder an die KI noch in den Spruch.
        Ein DJ mit so einem Namen bleibt trotzdem der DJ (der Kopf nennt ihn ja
        auch) - im Spruch heisst er nur DJ_OHNE_NAME. Hat der DJ gerade selbst
        ein Notsignal geschrieben, gibt es gar keinen Spruch (_SANFT)."""
        dj = self.dj(m)
        if dj and basis.kuerzlich_notlage(dj[0]):
            return _SANFT
        top = [(t, n) for t, n in self.top_songs(m) if not _notlage(t)]
        dj_name = str(dj[1].get("name") or "").strip() if dj else ""
        if dj and (not dj_name or _notlage(dj_name)):
            dj_name = DJ_OHNE_NAME
        text = await self._ki_spruch(gid, key, top, dj_name, hintergrund=hintergrund)
        if text:
            return text
        song = f"**{_sauber(top[0][0], 60)}**" if top else ""
        wer = f"**{_sauber(dj_name, 40)}**" if dj_name else ""
        if song and wer:
            return random.choice(_ERSATZ).format(song=song, dj=wer)
        if song:
            return random.choice(_ERSATZ_OHNE_DJ).format(song=song)
        if wer:
            return random.choice(_ERSATZ_OHNE_SONG).format(dj=wer)
        return random.choice(_ERSATZ_LEER)

    async def _ki_spruch(self, gid, key, top, dj_name, *, hintergrund):
        if not top or not features.is_on_in(gid, "ki") or _sendepause():
            return None
        try:
            if not ai.is_enabled():
                return None
            with ai.guild_kontext(gid):
                name = ai.bot_name(gid)
                system = (
                    f"Du bist {name}, ein frecher deutscher Discord-Bot mit losem Mundwerk. "
                    "Du kommentierst das Musik-Wrapped des Servers - was den ganzen Monat "
                    "lief - in GENAU ZWEI Saetzen auf Deutsch: zieh ueber den Musikgeschmack "
                    "her, nenn dabei einen der Songs, und mach dich ueber den DJ des Monats "
                    "lustig (der hat sich am meisten gewuenscht). Keine Emojis, kein Vorwort, "
                    f"keine Aufzaehlung. {ai.FloAI._GUARDRAIL}")
                zeilen = "\n".join(f"{i}. {t[:100]} ({n}x)" for i, (t, n) in enumerate(top, 1))
                if dj_name == DJ_OHNE_NAME:
                    dj_zeile = ("Den Namen des DJ des Monats lassen wir weg - nenn ihn "
                                f"einfach '{DJ_OHNE_NAME}'.")
                elif dj_name:
                    dj_zeile = f"DJ des Monats: {dj_name[:40]}."
                else:
                    dj_zeile = "Einen DJ gibt es nicht - gewuenscht hat sich keiner was."
                prompt = f"Musik-Wrapped {_monatsname(key)}. Meistgespielt:\n{zeilen}\n{dj_zeile}"
                out = await ai.generate(prompt, system=system, temperature=1.0,
                                        max_tokens=120, hintergrund=hintergrund)
        except Exception:  # noqa: BLE001 - dann eben der Ersatz
            log.debug("Musik-Wrapped: KI-Spruch gescheitert", exc_info=True)
            return None
        out = str(out or "").strip().strip('"').strip()
        if (not out or ai.instance._ist_verweigerung(out) or ai.instance.ist_notlage(out, out)
                or _heikel(out)):
            return None
        return ai.instance._kuerzen(out)

    # --- 'flo wrapped' --------------------------------------------------------
    def _befehl(self, message):
        """('zeigen', laufend) | ('posten', None) | None."""
        inhalt = message.content or ""
        if "wrapped" not in inhalt.lower():
            return None             # jede Chatnachricht kommt hier vorbei - schnell raus
        roh = ai.strip_lead(inhalt)
        if not roh:
            return None
        teile = roh.split(None, 1)
        if teile[0].lower().strip(_RAND) not in _ERSTE:
            return None
        rest = " ".join((teile[1] if len(teile) > 1 else "").lower().split()).strip(_RAND)
        if rest in _LAUFEND:
            return ("zeigen", True)
        if rest in _VORMONAT:
            return ("zeigen", False)
        if rest in _POSTEN:
            return ("posten", None)
        return None

    async def handle(self, message):
        """'wrapped' -> Karte; alles andere None."""
        if not self._enabled:
            return None
        befehl = self._befehl(message)
        if befehl is None:
            return None
        guild = getattr(message, "guild", None)
        if guild is None:
            return "Wrapped gibt's nur auf einem Server. Hier hörst du ja eh nur dich selbst."
        if befehl[0] == "posten":
            return await self._posten_befehl(message, guild)
        laufend = befehl[1]
        jetzt = _mono()
        zuletzt = self._zuletzt.get(guild.id)
        if zuletzt is not None and jetzt - zuletzt < ABKUEHLUNG:
            return random.choice(_ZU_FRUEH)
        self._zuletzt[guild.id] = jetzt
        key = uhr.monat_key() if laufend else uhr.vormonat_key()
        m = self.daten(guild.id, key)
        gesamt = _ganz((m or {}).get("gesamt"))
        if not m or (gesamt <= 0 and _ganz(m.get("radio")) <= 0):
            return _NICHTS.format(monat=_monatsname(key))
        text = self._kopf(m, key, laufend=laufend, mit_ping=False)
        text += " " + await self._spruch(guild.id, m, key, hintergrund=False)
        bild = await self._bild(m, key, getattr(guild, "name", ""), laufend=laufend)
        kw = {"allowed_mentions": discord.AllowedMentions.none()}
        if bild is not None:
            kw["file"] = discord.File(bild, filename="wrapped.png")
        msg = await basis.antworte(message, text, **kw)
        return HANDLED if msg is not None else text

    def _kopf(self, m, key, *, laufend, mit_ping, belohnung=0):
        """Die erste Zeile: Monat, Zahlen, DJ."""
        gesamt, radio = _ganz(m.get("gesamt")), _ganz(m.get("radio"))
        stunden = _ganz(m.get("sek")) / 3600
        zahlen = (f"{gesamt} Songs" + (f" + {radio} vom Radio" if radio else "")
                  + (f", {stunden:.1f} Stunden".replace(".", ",") if stunden >= 0.1 else ""))
        titel = f"🎧 **Musik-Wrapped {_monatsname(key)}**" + (" (läuft noch)" if laufend else "")
        dj = self.dj(m)
        if dj is None:
            return f"{titel} – {zahlen}. Einen DJ gibt's nicht, gewünscht hat sich keiner was."
        uid, e = dj
        wer = f"<@{uid}>" if mit_ping else f"**{_sauber(e.get('name') or uid, 40)}**"
        rolle = "DJ bisher" if laufend else "DJ des Monats"
        lohn = (f" (+{belohnung:,} Flo Coins)".replace(",", ".") if belohnung else "")
        return f"{titel} – {zahlen}. {rolle}: {wer}{lohn} mit {_ganz(e.get('n'))} Wünschen."

    # --- Der Monats-Post ------------------------------------------------------
    @staticmethod
    def im_fenster(jetzt):
        """Am 1. ab POST_STUNDE bis zum NACHHOL_TAGE (Ortszeit)."""
        return (1 <= jetzt.day <= NACHHOL_TAGE
                and (jetzt.day > 1 or jetzt.hour >= POST_STUNDE))

    async def tick(self, client):
        """Am Monatsanfang das Wrapped posten (einmal je Server) - und nachholen,
        was davon abgebrochen ist (Post, Rolle; siehe _amtlich)."""
        if not self._enabled or self._store is None:
            return
        if not uhr.stabil():
            return                      # Startgnade oder Uhrsprung - spaeter
        jetzt = uhr.jetzt()
        if not self.im_fenster(jetzt):
            return
        if _sendepause():
            return                      # verschoben, nicht verloren (Fenster)
        vormonat = uhr.vormonat_key(jetzt)
        for guild in list(getattr(client, "guilds", None) or ()):
            try:
                await self._abschliessen(client, guild, vormonat)
            except Exception:  # noqa: BLE001 - ein Server haelt die anderen nie auf
                log.exception("Musik-Wrapped auf '%s' fehlgeschlagen",
                              getattr(guild, "name", "?"))

    async def _posten_befehl(self, message, guild):
        """'flo wrapped posten' - nur der Besitzer. Steht das Wrapped des
        Vormonats noch aus (oder hing es mittendrin), IST das der Monats-Post
        (mit DJ-Coins und Rolle - bezahlt wird nie doppelt, und der Takt macht
        ihn danach nicht noch einmal). Sonst ein Wiederholungs-Post ohne Coins."""
        if not _besitzer(getattr(message.author, "id", 0)):
            return _NUR_CHEF
        import laufzeit
        ergebnis = await self._abschliessen(laufzeit.client, guild, uhr.vormonat_key(),
                                            manuell=True)
        monat = _monatsname(uhr.vormonat_key())
        return {
            "gepostet": f"Wrapped {monat} ist raus, DJ bezahlt. Der Takt lässt es jetzt in Ruhe.",
            "wiederholt": f"Wrapped {monat} nochmal gepostet – ohne Coins, die gibt's nur einmal.",
            "zu_wenig": f"Im {monat} liefen hier keine {MIN_SONGS} Songs. Dafür poste ich nix.",
            "kein_kanal": "Kein Kanal, in den ich darf. Stell `wrapped_channel` ein.",
            "nicht_raus": (f"Wrapped {monat}: Discord hat den Post abgelehnt, steht im Log. "
                           "Coins gibt's trotzdem nur einmal – und raus ist noch nix."),
            "aus": "Musik ist hier aus. Kein Wrapped.",
        }.get(ergebnis, "Ging nicht. Steht im Log.")

    async def _abschliessen(self, client, guild, monat, *, manuell=False):
        """Das Monats-Wrapped EINES Servers. Einmal je Monat: 'gepostet' laeuft
        nur vorwaerts, und der DJ wird erst bezahlt, wenn das auf der Platte
        steht. Was danach noch fehlt (der Post, die Rolle), holt der naechste
        Takt nach - ohne noch einmal zu zahlen (_amtlich). manuell
        (Testausloeser): auch ausserhalb des Fensters, ohne Pause zwischen den
        Versuchen, und schon erledigt heisst dann 'nochmal posten, ohne Coins'.

        Rueckgabe: 'gepostet', 'wiederholt', 'erledigt', 'zu_wenig',
        'kein_kanal', 'nicht_raus', 'aus'."""
        gid = int(guild.id)
        if not features.is_on_in(gid, "music"):
            return "aus"
        async with self._sperre():
            g = self._guild(gid)
            neu = uhr.spaeter_als(monat, uhr.glaubhaft(g.get("gepostet"), monat))
            info = None if neu else self._offen(g, monat)
            if info is None and not neu and not manuell:
                return "erledigt"
            if info is not None and not manuell and not self._wieder_dran(gid, info):
                return "erledigt"           # Pause zwischen zwei Versuchen - oder aufgegeben
            m = self.daten(gid, monat) or {}
            genug = _ganz(m.get("gesamt")) >= MIN_SONGS
            if neu:
                if not genug:
                    log.info("Musik-Wrapped %s auf '%s': nur %d Songs - kein Post.", monat,
                             getattr(guild, "name", gid), _ganz(m.get("gesamt")))
                # shield: bricht der Takt ab (ALLTAG_FRIST), wird trotzdem zu Ende
                # festgehalten und bezahlt - nie das eine ohne das andere.
                info = await asyncio.shield(self._festhalten_und_zahlen(
                    gid, g, monat, self.dj(m) if genug else None, genug=genug))
            if info is None:
                # Laengst erledigt - der Besitzer will es nur nochmal sehen.
                if not genug:
                    return "zu_wenig"
                ergebnis = await self._ausrufen(client, guild, m, monat, 0,
                                                hintergrund=False, wiederholung=True)
                return "wiederholt" if ergebnis == "gepostet" else ergebnis
            return await self._amtlich(client, guild, g, info, m, monat, genug=genug,
                                       manuell=manuell)

    @staticmethod
    def _offen(g, monat):
        """Der DJ-Eintrag von 'monat', wenn am Monats-Post noch etwas fehlt
        (der Post selbst oder die Rolle) - sonst None. Fehlt ein Feld ganz,
        gilt es als erledigt."""
        info = g.get("dj")
        if not isinstance(info, dict) or info.get("monat") != monat:
            return None
        if info.get("verkuendet", True) and info.get("rolle", True):
            return None
        return info

    def _wieder_dran(self, gid, info):
        """Ein Monats-Post, an dem noch etwas fehlt: jetzt nochmal versuchen?
        Hoechstens VERSUCHE Mal, mit NOCHMAL_NACH Abstand (nach einem Neustart
        gleich wieder - die Uhr dafuer lebt nur im Speicher)."""
        if _ganz(info.get("versuche")) >= VERSUCHE:
            return False
        ab = self._nochmal_ab.get(gid)
        return ab is None or _mono() >= ab

    async def _amtlich(self, client, guild, g, info, m, monat, *, genug, manuell):
        """Der Monats-Post mit allem, was dazugehoert und noch fehlt: der Post
        (mit Ping - nur bei genug Songs) und die Rolle. Bezahlt ist zu diesem
        Zeitpunkt schon, hier fliesst kein Coin."""
        gid = int(guild.id)
        info["versuche"] = _ganz(info.get("versuche")) + 1
        self._nochmal_ab[gid] = _mono() + NOCHMAL_NACH
        self._store.save_soon()
        ergebnis = "gepostet" if genug else "zu_wenig"
        if not genug:
            info["verkuendet"] = True      # ohne genug Songs gibt es nichts zu verkuenden
        elif not info.get("verkuendet"):
            ergebnis = await self._ausrufen(client, guild, m, monat, _ganz(info.get("betrag")),
                                            hintergrund=not manuell, wiederholung=False,
                                            grund=str(info.get("grund") or ""))
            if ergebnis == "gepostet":
                info["verkuendet"] = True
                self._store.save_soon()
        elif manuell:
            ergebnis = await self._ausrufen(client, guild, m, monat, 0,
                                            hintergrund=False, wiederholung=True)
            ergebnis = "wiederholt" if ergebnis == "gepostet" else ergebnis
        if not info.get("rolle"):
            # Ein Monat ohne DJ (zu wenig Songs) nimmt die Rolle nur weg.
            await self._dj_rolle(guild, g, _ganz(info.get("uid")))
            info["rolle"] = True
            self._store.save_soon()
        if not info.get("verkuendet") and _ganz(info.get("versuche")) >= VERSUCHE:
            log.warning("Musik-Wrapped %s auf '%s': nach %d Versuchen nicht raus - ich "
                        "lass es (bezahlt ist der DJ).", monat, getattr(guild, "name", gid),
                        VERSUCHE)
        return ergebnis

    async def _ausrufen(self, client, guild, m, monat, belohnung, *, hintergrund,
                        wiederholung, grund=""):
        """Den Post in den Wrapped-Kanal. 'gepostet', 'kein_kanal' oder 'nicht_raus'."""
        kanal = self._kanal(client, guild)
        if kanal is None:
            log.warning("Musik-Wrapped %s auf '%s': kein Kanal, in den Flo darf.",
                        monat, getattr(guild, "name", guild.id))
            return "kein_kanal"
        if await self._posten(kanal, guild, m, monat, belohnung, hintergrund=hintergrund,
                              wiederholung=wiederholung, grund=grund):
            return "gepostet"
        return "nicht_raus"

    async def _festhalten_und_zahlen(self, gid, g, monat, dj, *, genug):
        """Lock-before-pay: erst 'gepostet', den DJ und das Kassenbuch (ueber
        alle Server) auf die Platte, DANN die Coins. Rueckgabe: der DJ-Eintrag
        des Monats ('betrag' = was der DJ bekommen hat, 'grund' = 'anderswo',
        wenn er diesen Monat schon auf einem anderen Server kassiert hat). War
        derselbe Monat schon bezahlt (eine von Hand kaputt editierte Datei
        setzt 'gepostet' zurueck), fliesst nichts mehr. Kam der Lock gar nicht
        auf die Platte (Datei nicht schreibbar - economy.json aber schon), wird
        NICHT gezahlt: nach jedem Neustart saehe der Takt den Monat sonst als
        neu und zahlte noch einmal."""
        alt = g.get("dj") if isinstance(g.get("dj"), dict) else {}
        schon = alt.get("monat") == monat and bool(alt.get("bezahlt"))
        uid = dj[0] if dj else 0
        betrag, grund = 0, ""
        if not schon:
            betrag = _ganz(guildcfg.get(gid, "dj_belohnung"))
            if not (genug and uid and betrag > 0 and economy.is_enabled()
                    and features.is_on_in(gid, "economy")):
                betrag = 0
        k, kasse_vorher = str(int(uid or 0)), None
        if betrag > 0:
            kasse_vorher = self._bezahlt().get(k)
            if uhr.spaeter_als(monat, uhr.glaubhaft(kasse_vorher, monat)):
                self._bezahlt()[k] = monat
            else:
                betrag, grund = 0, "anderswo"   # schon auf einem anderen Server
        g["gepostet"] = monat
        info = g["dj"] = {"monat": monat, "uid": uid,
                          "betrag": _ganz(alt.get("betrag")) if schon else 0,
                          "bezahlt": schon,
                          "grund": str(alt.get("grund") or "") if schon else grund,
                          "verkuendet": False, "rolle": False, "versuche": 0}
        self._nochmal_ab.pop(gid, None)
        gespeichert = await self._store.save()
        if schon:
            return info
        if betrag > 0 and gespeichert is False:
            log.error("Musik-Wrapped %s: musikstats.json nicht schreibbar - der DJ "
                      "bekommt KEINE Coins (sonst zahlte jeder Neustart noch mal).", monat)
            if kasse_vorher is None:
                self._bezahlt().pop(k, None)
            else:
                self._bezahlt()[k] = kasse_vorher
            betrag = 0
        if betrag > 0:
            economy.add_coins(uid, betrag, reason="dj-des-monats")
            info["betrag"] = betrag
            try:
                await economy.flush()
            except Exception:  # noqa: BLE001 - economy sichert ohnehin gesammelt
                log.debug("economy.flush nach DJ-Lohn fehlgeschlagen", exc_info=True)
        info["bezahlt"] = True
        self._store.save_soon()
        log.info("Musik-Wrapped %s: DJ des Monats auf Server %s ist %s (+%d Coins%s).",
                 monat, gid, uid or "keiner", info["betrag"], f", {grund}" if grund else "")
        return info

    async def _dj_rolle(self, guild, g, uid):
        """Die Rolle "🎧 DJ des Monats" an 'uid' (0 = an niemanden) - nur mit
        guildcfg 'dj_rolle' und dem Recht "Rollen verwalten".

        Wer sie hat, steht in g['rolle']['bei'] - und zwar erst, wenn Discord
        sie wirklich vergeben hat. Genommen wird sie genau denen (klappt das
        nicht, bleiben sie fuers naechste Mal stehen). So behaelt sie keiner
        ewig, nur weil sie in einem Monat ohne Recht oder ohne Schalter nicht
        weiterwandern konnte. fetch_member: ohne Mitglieder-Intent kennt der
        Cache die Leute oft nicht."""
        if not guildcfg.an(guild.id, "dj_rolle"):
            return
        ich = getattr(guild, "me", None)
        if not getattr(getattr(ich, "guild_permissions", None), "manage_roles", False):
            log.warning("Musik-Wrapped: DJ-Rolle auf '%s' geht nicht - mir fehlt "
                        "'Rollen verwalten'.", guild.name)
            return
        info = g.get("rolle")
        if not isinstance(info, dict):
            info = g["rolle"] = {}
        roh = info.get("bei") if isinstance(info.get("bei"), list) else []
        bei = [u for u in (_ganz(x) for x in roh) if u > 0]
        rid = _ganz(info.get("id"))
        rolle = (guild.get_role(rid) if rid else None) or discord.utils.get(guild.roles,
                                                                           name=DJ_ROLLE)
        if rolle is None:
            if not uid:
                info["bei"] = []            # keiner hat sie, keiner bekommt sie
                return
            try:
                rolle = await guild.create_role(name=DJ_ROLLE, colour=discord.Colour(DJ_FARBE),
                                                mentionable=False,
                                                reason="Flo: DJ des Monats (Musik-Wrapped)")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Musik-Wrapped: Rolle '%s' nicht anlegbar auf '%s': %s",
                            DJ_ROLLE, guild.name, exc)
                return
        info["id"] = rolle.id
        behalten = []
        for wer in bei:
            if wer == uid:
                continue                    # wer sie hat und wieder DJ ist, behaelt sie
            try:
                mitglied = await guild.fetch_member(wer)
                if rolle in mitglied.roles:
                    await mitglied.remove_roles(rolle, reason="Flo: neuer DJ des Monats")
            except discord.NotFound:
                continue                    # nicht mehr auf dem Server
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Musik-Wrapped: DJ-Rolle von %s nicht genommen (%s) - steht "
                            "Flos Rolle ueber '%s'?", wer, exc, DJ_ROLLE)
                behalten.append(wer)        # beim naechsten Mal nochmal
        if uid:
            try:
                mitglied = await guild.fetch_member(uid)
                if rolle not in mitglied.roles:
                    await mitglied.add_roles(rolle, reason="Flo: DJ des Monats")
                behalten.append(uid)
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Musik-Wrapped: DJ-Rolle fuer %s ging nicht (%s) - steht "
                            "Flos Rolle ueber '%s'?", uid, exc, DJ_ROLLE)
                if uid in bei:
                    behalten.append(uid)
        info["bei"] = behalten[-20:]
        self._store.save_soon()

    @staticmethod
    def _kanal(client, guild):
        """Der Wrapped-Kanal (guildcfg 'wrapped_channel'), sonst die Ansagen,
        sonst der System-Kanal - aber nie ein Aufraeum-Kanal: dort loeschte
        bot.py das Wrapped samt DJ-Ping nach einer Minute, und 'verkuendet'
        stuende schon - es kaeme nie wieder. Auf dem Hauptserver ist der
        Ansagen-Kanal ab Werk genau so einer. None = keiner taugt (dann holt
        der Takt nach, siehe _amtlich)."""
        return basis.kanal_ohne_aufraeumen(client, guild, "wrapped_channel")

    async def _posten(self, kanal, guild, m, monat, belohnung, *, hintergrund, wiederholung,
                      grund=""):
        dj = self.dj(m)
        # Wer gerade ein Notsignal geschrieben hat, wird nicht angepingt (und
        # _spruch laesst den Spott weg) - Coins und Rolle bleiben.
        sanft = bool(dj) and basis.kuerzlich_notlage(dj[0])
        text = self._kopf(m, monat, laufend=False, mit_ping=not wiederholung,
                          belohnung=belohnung)
        if grund == "anderswo" and not wiederholung and not sanft:
            text += " " + _ANDERSWO
        text += " " + await self._spruch(guild.id, m, monat, hintergrund=hintergrund)
        erlaubt = discord.AllowedMentions(
            everyone=False, roles=False, replied_user=False,
            users=[discord.Object(dj[0])] if dj and not wiederholung and not sanft else False)
        bild = await self._bild(m, monat, getattr(guild, "name", ""), laufend=False)
        kw = {"allowed_mentions": erlaubt}
        if bild is not None:
            kw["file"] = discord.File(bild, filename="wrapped.png")
        try:
            await kanal.send(text, **kw)
        except discord.HTTPException as exc:
            if "file" not in kw:
                log.warning("Musik-Wrapped ging nicht raus: %s", exc)
                return False
            # Ohne 'Dateien anhaengen' eben ohne Bild.
            kw.pop("file")
            try:
                await kanal.send(text, **kw)
            except discord.HTTPException as exc2:
                log.warning("Musik-Wrapped ging nicht raus: %s", exc2)
                return False
        return True


instance = Musikstats()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
notieren = instance.notieren
zuruecknehmen = instance.zuruecknehmen
tick = instance.tick
