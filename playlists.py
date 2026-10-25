"""Playlists je Server: 'flo playlist neu zocken', Songs reinkippen, 'fertig'.

Wie es sich anfuehlt
====================
    flo playlist neu Zocken          Flo fragt nach Songs (eine Statusnachricht)
    <Titel, Links, ganze Alben>      einfach reinkippen, gern zehn Zeilen auf einmal
    fertig                           gespeichert -> Karte mit vier Knoepfen
                                     (▶ Abspielen · 🔀 Gemischt · ✏️ Songs dazu · 🗑 Löschen)
    flo playlist zocken              spielt sie ab (im Voice)
    flo playlists                    alle Playlists dieses Servers

Alles weitere (gemischt, dazu, zeig, löschen, umbenennen, raus <nr>,
'queue speichern als <name>') steht unten an der Grammatik (_parse) und im
README-Kapitel "Playlists".

Wo bot.py was aufruft
=====================
    handle(message)              Handler-Kette, direkt VOR music.handle
    on_message_passive(message)  passiver Haken direkt nach dem Giveaway-
                                 Assistenten, vor der Ansprache-Pruefung:
                                 True = die Nachricht gehoerte dem Assistenten.
                                 Laeuft VOR der Sendepause-Pruefung von bot.py -
                                 deshalb fragt der Assistent admin.is_locked()
                                 selbst (in der Sendepause steht er still)
    tick(client)                 im Voice-Watchdog-Takt (alle 15 s): Frist und
                                 faellige Status-Edits - auch hier prueft bot.py
                                 die Sendepause nicht, also tut es tick selbst
    offene_sichern()             beim Herunterfahren: EINMAL, vor dem Abmelden,
                                 vor store.alle_sichern. Idempotent: jeder
                                 gespeicherte Assistent ist danach zu
    DYNAMISCHE_KNOEPFE           werden in setup_hook angemeldet
                                 (bot.DYNAMISCHE_MODULE)

Der Assistent
=============
Wer 'playlist neu <name>' sagt, hat genau EINEN offenen Assistenten (ein neuer
verwirft den alten). Seine Nachrichten in DIESEM Kanal gehoeren dann dem
Assistenten - ausser:
  - sie sind an Flo gerichtet ('flo stop die musik' stoppt weiter die Musik).
    Nur Fertig-/Abbruch-Woerter mit Flo davor ('flo stop', 'flo fertig')
    schliessen den Assistenten - die Musik bleibt dabei an, music.handle sieht
    die Nachricht nie. Mehrzeilig mit Flo vorn ist ein Paste nur, wenn ohne
    die angesprochene erste Zeile (Rede an Flo, nur ihre Links zaehlen)
    mindestens zwei Songs bleiben oder die letzte Zeile abschliesst -
    'Flo\\nwie spät ist es?' geht an die KI;
  - sie sehen nach Chat aus (Frage, Ueberschrift mit ':', langer Satz, nur
    Emojis, 'lol') - die laufen ganz normal weiter;
  - sie enthalten ein Notsignal (ai.FloAI._NOTLAGE_RE): das wird nie als Song
    geschluckt.
Die LETZTE Zeile eines Pastes darf abschliessen oder abbrechen ('Song A\\n
flo fertig', '...\\nabbrechen') - ein Song wird aus ihr nie. Was erst nach
dem Schliessen dran ist (wartete hinter 'fertig' auf die Sperre), bekommt ❌
und einen Satz statt still zu verschwinden.
Fertig nach fuenf Minuten ohne Songs: mit Songs speichert Flo von selbst, ohne
verwirft er. Ist 'music' auf dem Server inzwischen aus, macht der Takt still
zu (speichern ja, posten und pingen nein). Beim Herunterfahren speichert
offene_sichern, was da ist - und sagt, wenn das nicht ging.

Abspielen: je Server laeuft hoechstens EIN Einreihen gleichzeitig (der erste
Song braucht Sekunden - ein Doppelklick reihte sonst alles zweimal ein).

'fertig' gewinnt gegen Blackjack: wer einen Assistenten offen hat und am
Blackjack-Tisch 'fertig' tippt, schliesst den Assistenten (siehe README).

Daten: data/playlists.json - {"guilds": {"<gid>": {"listen": {"<pid>": {...}}}}}
Beim Import passiert nichts - kein Speicher, kein Netz.
"""

import asyncio
import difflib
import logging
import os
import random
import re
import secrets
import time

import discord

import ai
import basis
from basis import FeatureBasis
import features
import music
import musikquellen
from store import JsonStore

log = logging.getLogger("dcbot.playlists")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("playlist", "playlists", "wiedergabeliste")
# Erste Woerter der natuerlichen Formen ('neue playlist zocken', 'queue
# speichern als zocken', 'spiel die playlist zocken', 'mach playlist zocken
# an'). Sie gehoeren anderen (music) - hier zaehlen sie nur, wenn im Satz
# auch 'playlist' bzw. 'speichern' steht (siehe handle).
_VORWOERTER = frozenset(("neue", "queue", "warteschlange", "spiel", "spiele", "play",
                         "leg", "starte", "start", "hau", "mach", "pack", "tu"))
_ERSTE = frozenset(_CMDS) | _VORWOERTER


def _env_int(name, vorgabe):
    try:
        wert = int(os.getenv(name, str(vorgabe)) or vorgabe)
    except ValueError:
        return vorgabe
    return wert if wert > 0 else vorgabe


# --- Grenzen (per .env justierbar) -------------------------------------------
MAX_LISTEN = _env_int("PLAYLIST_MAX_LISTEN", 50)          # je Server
MAX_PRO_PERSON = _env_int("PLAYLIST_MAX_PRO_PERSON", 10)  # je Person und Server
MAX_SONGS = _env_int("PLAYLIST_MAX_SONGS", 200)           # je Liste
MAX_NAME = 40
MAX_NAME_WOERTER = 5
#: Ganze Alben/Playlists je Assistent - und je davon hoechstens so viele Songs.
MAX_ERWEITERUNGEN = 5
ERWEITERUNG_SONGS = musikquellen.ERWEITERUNG_MAX
#: So viele Zeilen nimmt der Assistent aus EINER Nachricht.
MAX_ZEILEN = 50
#: Fuenf Minuten ohne neue Songs, dann speichert (oder verwirft) Flo.
WIZARD_TIMEOUT = _env_int("PLAYLIST_WIZARD_TIMEOUT", 300)
#: So lange darf beim Herunterfahren das Umbauen EINER Statusnachricht dauern.
STATUS_FRIST = 2.0
#: So viele eingefuegte Links loest der Assistent gleichzeitig auf.
AUFLOESEN_PARALLEL = 4
#: Die Statusnachricht wird hoechstens so oft bearbeitet (Rate-Limit).
EDIT_ABSTAND = 2.0
KARTE_SONGS = 8
ZEIG_SONGS = 25
STATUS_SONGS = 10
#: So lange bleibt eine Karte im Aufraeum-Kanal stehen.
KARTE_FREIGABE = 600
FARBE = 0x1ABC9C

# --- Wortschatz ---------------------------------------------------------------
_FERTIG = frozenset((
    "stop", "stopp", "fertig", "ende", "done", "das wars", "das war's", "das war es",
    "passt", "schluss", "feierabend", "speichern", "save", "bin fertig",
    "ich bin fertig", "fertig jetzt", "jetzt fertig", "ok fertig", "okay fertig",
    "passt so", "reicht", "das reicht"))
_ABBRUCH = frozenset((
    "abbrechen", "abbruch", "cancel", "verwerfen", "vergiss es", "lass es",
    "doch nicht", "nvm", "nevermind", "exit", "quit", "wegwerfen"))
_NEU = ("neu", "erstellen", "anlegen", "bau", "baue", "mach", "new", "create")
_MIX = ("mix", "gemischt", "shuffle", "durchgemischt", "zufällig", "zufaellig")
_DAZU = ("dazu", "hinzu", "erweitern", "add", "ergänzen", "ergaenzen")
_ZEIG = ("zeig", "zeigen", "songs", "info", "inhalt")
_LOESCHEN = ("löschen", "loeschen", "weg", "delete", "entfernen")
_RAUS = ("raus", "entferne")
_UMBENENNEN = ("umbenennen", "rename", "umtaufen")
_SPEICHERN = ("speichern", "sichern", "save")
_ALS = ("als", "unter", "in")
# Steht eines davon direkt hinter 'playlist', ist das ein Satz UEBER
# Playlists und kein Befehl ('flo playlist ist doof', 'flo playlist nervt').
# Dann antwortet die KI.
_SATZ_VORN = frozenset((
    "ist", "war", "sind", "waren", "wäre", "waere", "finde", "find", "findest",
    "nervt", "nerven", "macht", "machen", "hat", "hab", "habe", "hatte", "haben",
    "mag", "magst", "kann", "kannst", "will", "willst", "soll", "sollte", "wird",
    "klingt", "läuft", "laeuft", "geht", "suckt", "rockt", "braucht", "fehlt",
    "gefällt", "gefaellt", "gibt", "gab", "wie", "was", "wer", "wo", "warum",
    "weil", "dass", "oder", "aber", "ich", "du", "nicht", "nie", "kein", "keine"))
# Steht eines davon in einem Namen, den es NICHT gibt, war es auch ein Satz
# ('flo playlist von gestern war geil', 'flo playlist mit freunden').
_SATZ = _SATZ_VORN | frozenset((
    "von", "vom", "mit", "für", "fuer", "auch", "echt", "voll", "ziemlich", "der",
    "die", "das", "den", "dem", "ein", "eine", "einen", "meine", "deine", "mir",
    "mich", "dir", "dich", "uns", "euch", "jetzt", "heute", "gestern", "immer"))
# Am Namensende waeren sie nicht mehr vom Befehl zu unterscheiden. 'hilfe'
# & Co.: 'flo playlist hilfe' oeffnet die Hilfe-Seite (bot._help_category_key).
_RESERVIERT_VORN = frozenset(_NEU + _MIX + _DAZU + _ZEIG + _LOESCHEN + _SPEICHERN
                             + _UMBENENNEN + ("alle", "hilfe", "help", "anleitung"))
_RESERVIERT_HINTEN = frozenset(_MIX + _DAZU + _ZEIG + _LOESCHEN)
# Im Assistenten: Zeilen, die so anfangen, sind ein Song, keine Ansprache.
_FLO_KUENSTLER = ("flo rida", "flo milli", "flo mega", "flo dalton", "flo nuggets")
# Im Assistenten: das ist Chat, kein Song.
_CHAT_WOERTER = frozenset((
    "lol", "lel", "haha", "hahaha", "hehe", "xd", "xdd", "ok", "okay", "oki", "ja",
    "jo", "jap", "nein", "ne", "nö", "hm", "hmm", "danke", "thx", "ty", "omg",
    "wtf", "bruh", "digga", "alter", "boah", "krass", "achso", "aha", "jaja",
    "gg", "rip", "+1", "brb"))
_AUFZAEHLUNG_RE = re.compile(r"^\s*(?:[-*•·–—>]+|\d{1,3}[.)\]:]|\(\d{1,3}\)|#\d{1,3}\b)\s*")
_RAND = " \t.,!?;:\"'„“”‚‘’»«`"
_ID_ZEICHEN = "0123456789abcdefghijklmnopqrstuvwxyz"
_NATUR_RE = re.compile(
    r"^(?P<verb>spiel|spiele|play|leg|starte|start|hau|mach|pack|tu)"
    r"(?:\s+(?:mal|mir|uns|doch|bitte|jetzt|ma))*"
    r"(?:\s+(?:die|meine|unsere|eure|deine|mein|unser|dein))?"
    r"\s+(?:playlist|wiedergabeliste)\s+(?P<rest>.+)$")
# Was hinter dem Namen einer natuerlichen Form stehen darf.
_NATUR_HINTEN = frozenset(("an", "auf", "ab", "rein", "mal", "bitte", "vor", "jetzt"))
# Ein Link, wo ein Name hingehoert ('flo playlist https://…').
_LINK_RE = re.compile(r"https?://|www\.", re.I)
# Zweimal Abspielen, waehrend der erste Song noch geladen wird.
_SCHON_DABEI = "Ich lad hier gerade schon eine Playlist rein. Geduld, du Specht – einmal reicht."


def _uhr():
    """Die Uhr fuer Fristen und Edit-Abstaende (monotonic - Tests stellen sie)."""
    return time.monotonic()


def _key(name):
    """Vergleichsform eines Namens: klein, Leerraum zusammengefasst."""
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def _wort(text):
    """Ein kurzer Text in Vergleichsform ('Fertig!' -> 'fertig')."""
    text = str(text or "").replace("’", "'").replace("´", "'")
    return re.sub(r"\s+", " ", text).strip(" .!?,;:-~*_\n\t").lower()


def _sauber(text, laenge=90):
    """Titel fuer die Anzeige: gekuerzt, ohne Markdown und ohne Ping."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > laenge:
        text = text[:laenge - 1].rstrip() + "…"
    return discord.utils.escape_markdown(text).replace("@", "@​")


def _code(text):
    """Ein Name in einem `Code`-Stueck: dort wirkt kein Markdown, nur ` stoert."""
    return re.sub(r"[`\n]", "", str(text or "")).strip()[:MAX_NAME + 10]


def _songs_text(n):
    return "1 Song" if n == 1 else f"{n} Songs"


def _dauer_text(songs):
    bekannt = [int(s["d"]) for s in songs
               if isinstance(s.get("d"), (int, float)) and not isinstance(s.get("d"), bool)
               and s["d"] > 0]
    if not bekannt:
        return "Dauer unbekannt"
    sek = sum(bekannt)
    text = (f"{sek // 3600} h {sek % 3600 // 60:02d} min" if sek >= 3600
            else f"{max(1, sek // 60)} min")
    return ("mind. " + text) if len(bekannt) < len(songs) else text


def _merkmal(i):
    """Woran ein Song als doppelt erkannt wird."""
    i = str(i or "").strip()
    if i.lower().startswith("ytsearch"):
        return re.sub(r"\s+", " ", i.lower())
    return i


def _darf(person, liste):
    """Ersteller oder wer den Server/Kanaele verwaltet."""
    if int(getattr(person, "id", 0) or 0) == int(liste.get("von", 0) or 0):
        return True
    rechte = getattr(person, "guild_permissions", None)
    return bool(rechte is not None and (getattr(rechte, "administrator", False)
                                        or getattr(rechte, "manage_guild", False)
                                        or getattr(rechte, "manage_channels", False)))


def _sendepause(uid=0):
    """Sendepause aktiv - und das ist nicht der Besitzer?"""
    try:
        import admin
        return bool(admin.is_locked()) and int(uid or 0) != int(admin.OWNER_ID)
    except Exception:  # noqa: BLE001 - ohne admin keine Sendepause
        return False


class Playlists(FeatureBasis):
    """Gespeicherte Playlists, ihr Assistent, die Karte mit den Knoepfen."""

    def __init__(self):
        self._enabled = False
        self._store = None
        # Laufende Assistenten: (channel_id, user_id) -> dict (siehe _assistent_bauen)
        self._wizards = {}
        # Server, auf denen gerade eine Playlist eingereiht wird. Der erste
        # Song braucht 2-5 s (Aufloesen, Verbinden) - ein zweiter Klick in der
        # Zeit saehe noch keinen laufenden Song und reihte alles doppelt ein.
        self._am_laden = set()

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if os.getenv("PLAYLISTS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Playlists aus (PLAYLISTS_ENABLED=0).")
            return False
        self._store = JsonStore("playlists.json", default={"guilds": {}})
        self._enabled = True
        anzahl = sum(len((g or {}).get("listen") or {})
                     for g in self._store.data.get("guilds", {}).values()
                     if isinstance(g, dict))
        log.info("Playlists aktiv (%d gespeichert).", anzahl)
        return True

    def is_enabled(self):
        return self._enabled

    # --- Speicher ---------------------------------------------------------
    def _listen(self, gid):
        """Die Playlists eines Servers: pid -> Liste (legt den Server an)."""
        guilds = self._store.data.setdefault("guilds", {})
        g = guilds.get(str(gid))
        if not isinstance(g, dict):
            g = guilds[str(gid)] = {"listen": {}}
        listen = g.get("listen")
        if not isinstance(listen, dict):
            listen = g["listen"] = {}
        return listen

    def _speichern_bald(self):
        if self._store is not None:
            self._store.save_soon()

    def _finde(self, gid, name):
        key = _key(name)
        if not key:
            return None
        for liste in self._listen(gid).values():
            if isinstance(liste, dict) and _key(liste.get("key") or liste.get("name")) == key:
                return liste
        return None

    def _neue_id(self, gid):
        listen = self._listen(gid)
        while True:
            pid = "".join(secrets.choice(_ID_ZEICHEN) for _ in range(6))
            if pid not in listen:
                return pid

    def _anlegen(self, gid, name, person, songs):
        jetzt = time.time()
        pid = self._neue_id(gid)
        liste = {"id": pid, "name": name, "key": _key(name),
                 "von": int(getattr(person, "id", 0) or 0),
                 "von_name": str(getattr(person, "display_name", "") or "?")[:64],
                 "erstellt": jetzt, "geaendert": jetzt, "gespielt": 0, "zuletzt": 0.0,
                 "songs": list(songs)[:MAX_SONGS]}
        self._listen(gid)[pid] = liste
        self._speichern_bald()
        return liste

    def _platz_fuer_neue(self, gid, person):
        """None = es darf eine neue Liste her, sonst der Grund dagegen."""
        listen = [l for l in self._listen(gid).values() if isinstance(l, dict)]
        if len(listen) >= MAX_LISTEN:
            return (f"Dieser Server hat schon {MAX_LISTEN} Playlists. Löscht erst mal "
                    "was, ihr Messies.")
        uid = int(getattr(person, "id", 0) or 0)
        if sum(1 for l in listen if int(l.get("von", 0) or 0) == uid) >= MAX_PRO_PERSON:
            return (f"Du hast schon {MAX_PRO_PERSON} Playlists. Reicht. Lösch eine "
                    "oder hör einfach Radio.")
        return None

    def _name_pruefen(self, roh):
        """-> (name, None) oder (None, Abfuhr)."""
        name = re.sub(r"\s+", " ", str(roh or "")).strip().strip(_RAND + "*_~|")
        if not name:
            return None, (f"Ohne Namen? Wie soll ich die denn wiederfinden. "
                          f"`{self._bot_name} playlist neu <name>`")
        if len(name) > MAX_NAME or len(name.split()) > MAX_NAME_WOERTER:
            return None, (f"Höchstens {MAX_NAME} Zeichen und {MAX_NAME_WOERTER} Wörter – "
                          "das ist ein Name, kein Aufsatz.")
        if re.search(r"[<>@#`\\*_~|\n]|https?:|www\.", name):
            return None, ("Da sind Zeichen drin, die ich mir nicht antue. Buchstaben, "
                          "Zahlen, Leerzeichen – reicht doch.")
        woerter = name.lower().split()
        if (woerter[0] in _RESERVIERT_VORN or woerter[-1] in _RESERVIERT_HINTEN
                or woerter[0] in _SATZ_VORN or any(w in _UMBENENNEN for w in woerter)
                or (len(woerter) >= 2 and woerter[-2] in _RAUS
                    and re.fullmatch(r"[0-9]+", woerter[-1]))):
            return None, (f"„{name}“ als Name? Das verwechsel ich mit einem Befehl. "
                          "Nimm was anderes.")
        return name, None

    # --- Grammatik ----------------------------------------------------------
    @staticmethod
    def _zerlegen(roh):
        """-> (Originalwoerter, kleingeschriebene Woerter)."""
        text = re.sub(r"\s+", " ", str(roh or "")).strip().strip(_RAND)
        orig = [w.strip(",;:") for w in text.split()]
        orig = [w for w in orig if w]
        return orig, [w.lower() for w in orig]

    def _parse(self, roh):
        """Text (ohne Flo davor) -> (aktion, daten) oder None.

        None heisst: kein Playlist-Befehl - die Kette geht weiter, am Ende
        antwortet die KI. Nur was eindeutig ist, wird ein Befehl."""
        orig, w = self._zerlegen(roh)
        if not w:
            return None
        w0 = w[0]
        if w0 in _CMDS:
            rest, rest_o = w[1:], orig[1:]
            if not rest or rest == ["alle"]:
                return ("liste", {})
            if w0 == "playlists":
                return None             # die Mehrzahl ist nur die Uebersicht
            r0 = rest[0]
            if r0 in _SATZ_VORN:
                return None
            for i, wort in enumerate(rest):
                if wort in _UMBENENNEN:
                    alt, neu = rest_o[:i], rest_o[i + 1:]
                    if neu and neu[0].lower() in ("in", "zu", "nach", "auf"):
                        neu = neu[1:]
                    if not alt or not neu or len(alt) > MAX_NAME_WOERTER \
                            or len(neu) > MAX_NAME_WOERTER:
                        return None
                    return ("umbenennen", {"name": " ".join(alt), "neu": " ".join(neu)})
            if len(rest) > MAX_NAME_WOERTER + 1:
                return None             # ein Satz, kein Name
            if r0 in _NEU:
                return ("neu", {"name": " ".join(rest_o[1:])})
            if r0 in _MIX:
                return ("spielen", {"name": " ".join(rest_o[1:]), "mix": True})
            if r0 in _DAZU:
                return ("dazu", {"name": " ".join(rest_o[1:])})
            if r0 in _LOESCHEN:
                return ("loeschen", {"name": " ".join(rest_o[1:])})
            if r0 in _ZEIG:
                return ("zeig", {"name": " ".join(rest_o[1:])})
            if r0 in _SPEICHERN:
                name = rest_o[1:]
                if name and name[0].lower() in _ALS:
                    name = name[1:]
                return ("speichern", {"name": " ".join(name)})
            if (len(rest) >= 3 and rest[-2] in _RAUS
                    and re.fullmatch(r"[0-9]{1,4}", rest[-1])):
                return ("raus", {"name": " ".join(rest_o[:-2]), "nr": int(rest[-1])})
            if len(rest) >= 2:
                letztes, name = rest[-1], " ".join(rest_o[:-1])
                if letztes in _MIX:
                    return ("spielen", {"name": name, "mix": True})
                if letztes in _DAZU:
                    return ("dazu", {"name": name})
                if letztes in _ZEIG:
                    return ("zeig", {"name": name})
                if letztes in _LOESCHEN:
                    return ("loeschen", {"name": name})
            if len(rest) > MAX_NAME_WOERTER:
                return None
            return ("spielen", {"name": " ".join(rest_o), "mix": False, "nackt": True})
        if w0 == "neue":
            if len(w) >= 2 and w[1] in ("playlist", "wiedergabeliste") \
                    and len(w) - 2 <= MAX_NAME_WOERTER:
                return ("neu", {"name": " ".join(orig[2:])})
            return None
        if w0 in ("queue", "warteschlange"):
            if len(w) >= 2 and w[1] in _SPEICHERN:
                name = orig[2:]
                if name and name[0].lower() in _ALS:
                    name = name[1:]
                if len(name) <= MAX_NAME_WOERTER:
                    return ("speichern", {"name": " ".join(name)})
            return None
        m = _NATUR_RE.match(" ".join(w))
        if not m:
            return None
        n_vorn = len(w) - len(m.group("rest").split())
        name_o = orig[n_vorn:]
        name_w = w[n_vorn:]
        mix = an_auf = False
        while name_w and (name_w[-1] in _NATUR_HINTEN or name_w[-1] in _MIX):
            letztes = name_w.pop()
            name_o = name_o[:-1]
            mix = mix or letztes in _MIX
            an_auf = an_auf or letztes in ("an", "auf", "rein")
        if m.group("verb") in ("mach", "pack", "tu") and not an_auf:
            return None
        if not name_w or len(name_w) > MAX_NAME_WOERTER:
            return None
        return ("natur", {"name": " ".join(name_o), "mix": mix})

    # --- Befehle ------------------------------------------------------------
    async def handle(self, message):
        """'playlist ...' -> Antwort; alles andere None (die Kette geht weiter)."""
        if not self._enabled or message.guild is None:
            return None
        roh = ai.strip_lead(message.content or "")
        if not roh:
            return None
        erstes = roh.split(None, 1)[0].lower().strip(_RAND)
        if erstes not in _ERSTE:
            return None
        if erstes in _VORWOERTER:
            low = roh.lower()
            if ("playlist" not in low and "wiedergabeliste" not in low
                    and "speicher" not in low and "sicher" not in low and "save" not in low):
                return None
        befehl = self._parse(roh)
        if befehl is None:
            return None
        aktion, d = befehl
        gid = message.guild.id
        if aktion == "liste":
            return self._uebersicht(message.guild)
        if aktion == "neu":
            return await self._neu(message, d["name"])
        if aktion == "speichern":
            return await self._queue_speichern(message, d["name"])
        name = d.get("name", "")
        if not name.strip():
            return (f"Welche denn? `{self._bot_name} playlists` zeigt alle, "
                    f"`{self._bot_name} playlist <name>` spielt eine.")
        if _LINK_RE.search(name):
            if aktion == "natur":
                return None             # 'spiel playlist <link>' - Sache der Musik
            return self._link_statt_name()
        liste = self._finde(gid, name)
        if liste is None:
            if aktion == "natur":
                return None             # dann ist es eine YouTube-Suche (music)
            if any(wort in _SATZ for wort in _key(name).split()):
                # 'flo playlist zocken ist cool', 'flo playlist zocken ist weg',
                # 'flo playlist von gestern dazu' - ein Satz, kein Name.
                return None
            return self._gibts_nicht(gid, name)
        if aktion in ("spielen", "natur"):
            return await self._spielen_text(message, liste, d.get("mix", False))
        if aktion == "zeig":
            return await self._zeigen(message, liste)
        if not _darf(message.author, liste):
            return (f"Nicht deine Liste. Bau dir eine eigene: "
                    f"`{self._bot_name} playlist neu <name>`")
        if aktion == "dazu":
            return await self._assistent_starten(
                message.channel, message.author, gid, "dazu", liste=liste, antwort_auf=message)
        if aktion == "loeschen":
            frage = _LoeschFrage(gid, liste["id"], message.author.id)
            msg = await basis.antworte(
                message, f"**{_sauber(liste['name'])}** wirklich löschen? "
                         f"{_songs_text(len(liste.get('songs') or []))}, weg für immer.",
                view=frage)
            frage.message = msg
            return HANDLED if msg is not None else "Ging gerade nicht. Discord zickt."
        if aktion == "umbenennen":
            return self._umbenennen(gid, liste, d["neu"])
        if aktion == "raus":
            return self._raus(liste, d["nr"])
        return None

    def _link_statt_name(self):
        """'flo playlist https://…' / 'playlist neu https://…': ein Link ist
        kein Name (_name_pruefen lehnt ihn ab) - sagen, wie es geht."""
        b = self._bot_name
        return (f"Das ist ein Link, kein Playlist-Name. Abspielen: `{b} spiel <link>`. "
                f"Als Playlist speichern: `{b} playlist neu <name>` – und den Link "
                "dann reinkippen. So schwer ist das nicht.")

    def _gibts_nicht(self, gid, name):
        name = name.strip(_RAND)
        namen = {_key(l.get("name")): l.get("name") for l in self._listen(gid).values()
                 if isinstance(l, dict)}
        nah = difflib.get_close_matches(_key(name), list(namen), n=1, cutoff=0.6)
        vorschlag = f" Meintest du **{_sauber(namen[nah[0]])}**?" if nah else ""
        return (f"Eine Playlist **{_sauber(name, 40)}** gibt's hier nicht.{vorschlag} "
                f"Bau sie halt: `{self._bot_name} playlist neu {_code(name)}`")

    def _uebersicht(self, guild):
        listen = [l for l in self._listen(guild.id).values() if isinstance(l, dict)]
        e = discord.Embed(title=f"🎶 Playlists auf {getattr(guild, 'name', 'diesem Server')}",
                          color=FARBE)
        if not listen:
            e.description = ("Hier gibt's noch keine einzige Playlist. Armselig.\n"
                             f"Bau eine: `{self._bot_name} playlist neu <name>`")
            return e
        listen.sort(key=lambda l: (-int(l.get("gespielt", 0) or 0), _key(l.get("name"))))
        zeilen = [f"**{_sauber(l.get('name'), 40)}** · "
                  f"{_songs_text(len(l.get('songs') or []))} · "
                  f"von {_sauber(l.get('von_name'), 30)} · "
                  f"{int(l.get('gespielt', 0) or 0)}× gespielt" for l in listen[:25]]
        if len(listen) > 25:
            zeilen.append(f"-# …und {len(listen) - 25} weitere")
        e.description = "\n".join(zeilen)
        b = self._bot_name
        e.add_field(name="Bedienung", inline=False, value=(
            f"`{b} playlist <name>` spielt · `{b} playlist <name> mix` gemischt\n"
            f"`{b} playlist <name> zeig` · `{b} playlist <name> dazu` · "
            f"`{b} playlist <name> löschen`\n"
            f"`{b} playlist neu <name>` baut eine · "
            f"`{b} queue speichern als <name>` sichert die Warteschlange"))
        return e

    def _umbenennen(self, gid, liste, neu):
        alt = liste.get("name", "")
        neu, fehler = self._name_pruefen(neu)
        if fehler:
            return fehler
        andere = self._finde(gid, neu)
        if andere is not None and andere is not liste:
            return f"**{_sauber(neu)}** gibt's schon. Kreativität ist nicht deins, oder?"
        liste["name"], liste["key"], liste["geaendert"] = neu, _key(neu), time.time()
        self._speichern_bald()
        return (f"✏️ **{_sauber(alt)}** heißt jetzt **{_sauber(neu)}**. Besser wird sie "
                "davon auch nicht.")

    def _raus(self, liste, nr):
        songs = liste.setdefault("songs", [])
        if not 1 <= nr <= len(songs):
            return (f"**{_sauber(liste.get('name'))}** hat {_songs_text(len(songs))}. "
                    "Zählen üben.")
        weg = songs.pop(nr - 1)
        liste["geaendert"] = time.time()
        self._speichern_bald()
        return (f"➖ **{_sauber(weg.get('t'), 80)}** fliegt aus "
                f"**{_sauber(liste.get('name'))}**. Hat eh keiner vermisst.")

    async def _zeigen(self, message, liste):
        msg = await basis.antworte(message, view=self._karte(liste, songs_max=ZEIG_SONGS),
                                   allowed_mentions=discord.AllowedMentions.none())
        if msg is None:
            return "Die Karte ging gerade nicht raus. Discord zickt."
        self._karte_schuetzen(msg)
        return HANDLED

    # --- Abspielen ----------------------------------------------------------
    async def _abspielen(self, gid, person, voice_kanal, text_kanal, liste, gemischt,
                         reply_to=None):
        """-> (Antwort von music._play_many, Zusatzzeile oder "")."""
        items = [(s["i"], s.get("t") or s["i"], s.get("h"))
                 for s in liste.get("songs") or [] if isinstance(s, dict) and s.get("i")]
        if gemischt:
            items = random.sample(items, len(items))
        player = music._player_for(gid)
        player.text_channel = text_kanal
        deckel = music.max_queue(gid)
        frei = deckel - len(player.queue)
        res = await music._play_many(
            player, voice_kanal, items, getattr(person, "display_name", "?"),
            f"aus „{liste.get('name')}“", reply_to=reply_to,
            requested_by_id=int(getattr(person, "id", 0) or 0))
        erfolg = res is HANDLED or (isinstance(res, discord.Embed)
                                    and getattr(res.colour, "value", None) != music._COL_ERR)
        hinweis = ""
        if erfolg:
            liste["gespielt"] = int(liste.get("gespielt", 0) or 0) + 1
            liste["zuletzt"] = time.time()
            self._speichern_bald()
            if 0 < frei < len(items):
                hinweis = (f"Nur {frei} von {len(items)} passen rein (Deckel {deckel}). "
                           "Der Rest bleibt in der Liste.")
        return res, hinweis

    @staticmethod
    def _voice_von(person):
        return getattr(getattr(person, "voice", None), "channel", None)

    def _laden_beginnen(self, gid):
        """Darf dieser Aufruf abspielen? False = auf diesem Server wird gerade
        schon eine Playlist eingereiht (Doppelklick, zweimal getippt).
        Synchron - zwischen Pruefen und Eintragen kommt niemand dazwischen."""
        if gid in self._am_laden:
            return False
        self._am_laden.add(gid)
        return True

    async def _spielen_text(self, message, liste, gemischt):
        kanal = self._voice_von(message.author)
        if kanal is None:
            return "Geh erst in einen Sprachkanal, du Trockenschwimmer."
        if not liste.get("songs"):
            return (f"**{_sauber(liste.get('name'))}** ist leer. Kipp erst was rein: "
                    f"`{self._bot_name} playlist {_code(liste.get('name'))} dazu`")
        gid = message.guild.id
        if not self._laden_beginnen(gid):
            return _SCHON_DABEI
        try:
            res, hinweis = await self._abspielen(gid, message.author, kanal,
                                                 message.channel, liste, gemischt,
                                                 reply_to=message)
        finally:
            self._am_laden.discard(gid)
        if isinstance(res, discord.Embed):
            if hinweis:
                res.description = f"{res.description or ''}\n{hinweis}".strip()
            return res
        if hinweis:
            await basis.antworte(message, hinweis)
        return HANDLED

    # --- Warteschlange speichern --------------------------------------------
    @staticmethod
    def _song_aus_track(track):
        quelle = track.webpage_url if str(track.webpage_url or "").startswith("http") else ""
        i = quelle or str(track.query or "")
        if not i:
            return None
        dauer = track.duration if isinstance(track.duration, (int, float)) else None
        return {"i": i, "t": str(track.title or i)[:100],
                "h": None if quelle else track.match_hint,
                "d": int(dauer) if dauer else None, "q": "aus der Warteschlange"}

    async def _queue_speichern(self, message, name):
        gid = message.guild.id
        if not name.strip():
            return (f"Und wie soll sie heißen? `{self._bot_name} queue speichern als <name>`")
        name, fehler = self._name_pruefen(name)
        if fehler:
            return fehler
        if self._finde(gid, name) is not None:
            return (f"**{_sauber(name)}** gibt's schon. Anderer Name – oder häng was dran: "
                    f"`{self._bot_name} playlist {_code(name)} dazu`")
        voll = self._platz_fuer_neue(gid, message.author)
        if voll:
            return voll
        player = music.instance._players.get(gid)
        tracks = ([player.current] + list(player.queue)) if player is not None else []
        songs, gesehen = [], set()
        for track in tracks:
            if track is None:
                continue
            song = self._song_aus_track(track)
            if song is None or _merkmal(song["i"]) in gesehen:
                continue
            gesehen.add(_merkmal(song["i"]))
            songs.append(song)
        if not songs:
            return "Da läuft nix. Was soll ich speichern, die Stille?"
        liste = self._anlegen(gid, name, message.author, songs)
        msg = await basis.antworte(
            message, view=self._karte(liste, extra=(
                f"💾 Warteschlange gespeichert – {_songs_text(len(liste['songs']))}. "
                "Ob das jemand nochmal hören will, ist eine andere Frage.")),
            allowed_mentions=discord.AllowedMentions.none())
        if msg is None:
            return f"💾 **{_sauber(name)}** gespeichert, {_songs_text(len(liste['songs']))}."
        self._karte_schuetzen(msg)
        return HANDLED

    # --- Karte ----------------------------------------------------------------
    def _karte(self, liste, *, songs_max=KARTE_SONGS, extra=""):
        """Die Karte einer Playlist: Kopf, Songs, vier neustartfeste Knoepfe.
        timeout=None und NIE stop() - siehe PlaylistKnopf."""
        songs = [s for s in liste.get("songs") or [] if isinstance(s, dict)]
        kopf = (f"### 🎶 {_sauber(liste.get('name'), 60)}\n"
                f"-# von {_sauber(liste.get('von_name'), 40)} · {_songs_text(len(songs))} · "
                f"{_dauer_text(songs)} · {int(liste.get('gespielt', 0) or 0)}× gespielt")
        if extra:
            kopf += f"\n{extra}"
        zeilen = []
        for nr, s in enumerate(songs[:songs_max], start=1):
            d = s.get("d")
            dauer = (music._fmt_dur(int(d)) if isinstance(d, (int, float))
                     and not isinstance(d, bool) and d > 0 else "")
            zeilen.append(f"`{nr}.` {_sauber(s.get('t'), 70)}" + (f" · `{dauer}`" if dauer else ""))
        if len(songs) > songs_max:
            zeilen.append(f"-# …und {len(songs) - songs_max} weitere")
        if not zeilen:
            zeilen.append("_Leer. Gähnende Leere._")
        pid = liste["id"]
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay(kopf),
            discord.ui.TextDisplay("\n".join(zeilen)),
            discord.ui.ActionRow(
                PlaylistKnopf("play", pid, label="Abspielen", emoji="▶️",
                              style=discord.ButtonStyle.success),
                PlaylistKnopf("mix", pid, label="Gemischt", emoji="🔀",
                              style=discord.ButtonStyle.primary),
                PlaylistKnopf("add", pid, label="Songs dazu", emoji="✏️",
                              style=discord.ButtonStyle.secondary),
                PlaylistKnopf("del", pid, label="Löschen", emoji="🗑️",
                              style=discord.ButtonStyle.danger)),
            accent_colour=FARBE))
        return view

    @staticmethod
    def _text_view(text):
        """Eine Nachricht ohne Knoepfe (geschlossener Assistent, RIP)."""
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_colour=FARBE))
        return view

    def _karte_schuetzen(self, msg):
        """Im Aufraeum-Kanal: zehn Minuten stehen lassen, dann darf sie weg."""
        self.schuetzen(msg)
        self.freigeben(msg, delay=KARTE_FREIGABE)

    # --- Knoepfe der Karte ------------------------------------------------------
    async def _ephemeral(self, interaction, text, **kw):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True, **kw)
            else:
                await interaction.response.send_message(text, ephemeral=True, **kw)
        except discord.HTTPException as exc:
            log.warning("Playlist-Antwort nicht zustellbar: %s", exc)

    async def klick(self, interaction, aktion, pid):
        """Ein Knopf an einer Playlist-Karte (auch nach einem Neustart)."""
        if not self._enabled:
            await self._ephemeral(interaction, "Playlists sind gerade aus.")
            return
        gid = getattr(interaction, "guild_id", None)
        person = interaction.user
        if not gid:
            await self._ephemeral(interaction, "Das geht nur auf einem Server.")
            return
        if _sendepause(person.id):
            await self._ephemeral(interaction, "Sendepause. Ich red gerade mit keinem – "
                                               "mit dir schon gar nicht.")
            return
        if not features.is_on_in(gid, "music"):
            await self._ephemeral(interaction, "Musik ist auf diesem Server aus. Pech gehabt.")
            return
        liste = self._listen(gid).get(pid)
        if not isinstance(liste, dict):
            await self._ephemeral(interaction, "Die Liste gibt's nicht mehr. Weg ist weg.")
            return
        with ai.guild_kontext(gid):
            if aktion in ("play", "mix"):
                await self._klick_spielen(interaction, liste, aktion == "mix")
            elif not _darf(person, liste):
                await self._ephemeral(interaction, (
                    f"Nicht deine Liste. Bau dir eine eigene: "
                    f"`{self._bot_name} playlist neu <name>`"))
            elif aktion == "add":
                await interaction.response.defer()
                antwort = await self._assistent_starten(
                    interaction.channel, person, gid, "dazu", liste=liste)
                if isinstance(antwort, str):
                    await self._ephemeral(interaction, antwort)
            elif aktion == "del":
                frage = _LoeschFrage(gid, pid, person.id, karte=interaction.message)
                await self._ephemeral(
                    interaction, f"**{_sauber(liste.get('name'))}** wirklich löschen? "
                                 f"{_songs_text(len(liste.get('songs') or []))}, weg für immer.",
                    view=frage)

    async def _klick_spielen(self, interaction, liste, gemischt):
        kanal = self._voice_von(interaction.user)
        if kanal is None:
            await self._ephemeral(interaction, "Geh erst in einen Sprachkanal, du Trockenschwimmer.")
            return
        if not liste.get("songs"):
            await self._ephemeral(interaction, f"**{_sauber(liste.get('name'))}** ist leer. "
                                               "Erst was reinkippen.")
            return
        gid = interaction.guild_id
        if not self._laden_beginnen(gid):
            await self._ephemeral(interaction, _SCHON_DABEI)
            return
        try:
            # Aufloesen + Verbinden dauert laenger als Discords 3 Sekunden.
            await interaction.response.defer()
            res, hinweis = await self._abspielen(gid, interaction.user, kanal,
                                                 interaction.channel, liste, gemischt)
        finally:
            self._am_laden.discard(gid)
        try:
            if isinstance(res, discord.Embed):
                if hinweis:
                    res.description = f"{res.description or ''}\n{hinweis}".strip()
                await interaction.followup.send(embed=res)
            elif hinweis:
                await interaction.followup.send(hinweis)
        except discord.HTTPException as exc:
            log.warning("Playlist-Abspielantwort nicht zustellbar: %s", exc)

    async def loeschen_bestaetigt(self, interaction, gid, pid, karte=None):
        """'Ja, weg damit' - aus der Rueckfrage (Text oder Karte)."""
        liste = self._listen(gid).get(pid)
        if not isinstance(liste, dict):
            await interaction.response.edit_message(content="Schon weg. Zu langsam.", view=None)
            return
        if not _darf(interaction.user, liste):
            await interaction.response.edit_message(content="Nicht deine Liste.", view=None)
            return
        self._listen(gid).pop(pid, None)
        self._speichern_bald()
        wer = _sauber(getattr(interaction.user, "display_name", "?"), 40)
        rip = f"🗑️ **{_sauber(liste.get('name'))}** gelöscht von {wer}. RIP."
        log.info("Playlist '%s' (%s) auf Server %s geloescht.", liste.get("name"), pid, gid)
        if karte is None:
            await interaction.response.edit_message(content=rip, view=None)
            return
        await interaction.response.edit_message(content="Erledigt. 🪦", view=None)
        try:
            await karte.edit(view=self._text_view(rip))
        except discord.HTTPException as exc:
            log.info("Playlist-Karte nicht mehr bearbeitbar: %s", exc)

    # --- Der Assistent --------------------------------------------------------
    async def _neu(self, message, name):
        if not name.strip():
            return (f"Wie soll das Ding heißen? `{self._bot_name} playlist neu <name>` – "
                    "zum Beispiel `zocken`.")
        if _LINK_RE.search(name):
            return self._link_statt_name()
        name, fehler = self._name_pruefen(name)
        if fehler:
            return fehler
        gid = message.guild.id
        vorhanden = self._finde(gid, name)
        if vorhanden is not None:
            return (f"**{_sauber(vorhanden['name'])}** gibt's schon (von "
                    f"{_sauber(vorhanden.get('von_name'), 40)}). Was dazu? "
                    f"`{self._bot_name} playlist {_code(vorhanden['name'])} dazu`")
        voll = self._platz_fuer_neue(gid, message.author)
        if voll:
            return voll
        return await self._assistent_starten(message.channel, message.author, gid, "neu",
                                             name=name, antwort_auf=message)

    async def _assistent_starten(self, kanal, person, gid, modus, *, name="", liste=None,
                                 antwort_auf=None):
        """Neuen Assistenten oeffnen. -> HANDLED oder ein Text (Abfuhr)."""
        uid = int(person.id)
        alt_hinweis = ""
        for key in [k for k in self._wizards if k[1] == uid]:
            alt = self._wizards.pop(key)
            await self._schliessen(alt, self._text_view(
                f"### 🎶 {_sauber(alt['name'], 60)}\nWeggeworfen – du hast einen neuen "
                "Assistenten aufgemacht. Einer reicht."))
            alt_hinweis = (f"Dein alter Assistent für **{_sauber(alt['name'], 40)}** ist weg "
                           f"({_songs_text(len(alt['songs']))} futsch). Einer reicht.")
        bestand = list((liste or {}).get("songs") or [])
        w = {
            "key": (kanal.id, uid), "modus": modus, "gid": int(gid), "uid": uid,
            "person": person, "kanal": kanal,
            "name": liste["name"] if liste else name,
            "pid": liste["id"] if liste else None,
            "songs": [], "bestand": len(bestand),
            "gesehen": {_merkmal(s.get("i")) for s in bestand if isinstance(s, dict)},
            "frist": _uhr() + WIZARD_TIMEOUT, "status": None, "view": None,
            "lock": asyncio.Lock(), "edit_lock": asyncio.Lock(),
            "edit_faellig": False, "letzter_edit": float("-inf"),
            "edit_task": None, "erweiterungen": 0, "hinweise": [], "zu": False,
        }
        if alt_hinweis:
            w["hinweise"].append(alt_hinweis)
        w["view"] = _StatusAnsicht(w)
        self._wizards[w["key"]] = w
        try:
            if antwort_auf is not None:
                msg = await basis.antworte(antwort_auf, view=w["view"],
                                           allowed_mentions=discord.AllowedMentions.none())
            else:
                msg = await kanal.send(view=w["view"],
                                       allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("Playlist-Assistent: Status ging nicht raus: %s", exc)
            msg = None
        if msg is None:
            self._wizards.pop(w["key"], None)
            w["view"].stop()
            return "Ich krieg hier nichts gepostet. Rechte? Discord? Such's dir aus."
        w["status"] = msg
        w["letzter_edit"] = _uhr()
        self.schuetzen(msg)
        log.info("Playlist-Assistent (%s) fuer '%s' geoeffnet (Server %s).",
                 modus, w["name"], gid)
        return HANDLED

    def _status_text(self, w):
        dazu = w["modus"] == "dazu"
        teile = [f"### 🎶 {_sauber(w['name'], 60)} – " + ("Songs dazu" if dazu else "neue Playlist"),
                 "Kipp Titel oder Links rein – YouTube, SoundCloud, Spotify, ganze Alben, "
                 "gern zehn Zeilen auf einmal. Fertig? `fertig`. Doch keinen Bock? `abbrechen`."]
        n = len(w["songs"])
        zahl = f"**{_songs_text(n)}** drin"
        if dazu:
            zahl = f"**{n}** neu · {w['bestand']} schon drin"
        teile.append(zahl)
        if w["songs"]:
            start = max(0, n - STATUS_SONGS)
            zeilen = [f"`{w['bestand'] + nr}.` {_sauber(s.get('t'), 70)}"
                      for nr, s in enumerate(w["songs"][start:], start=start + 1)]
            if start:
                zeilen.insert(0, f"-# …und {start} davor")
            teile.append("\n".join(zeilen))
        for h in w["hinweise"][-3:]:
            teile.append(f"-# ⚠️ {h}")
        rest = max(0, int(w["frist"] - _uhr()))
        teile.append(f"-# Ich warte bis <t:{int(time.time() + rest)}:R>, dann speicher ich, "
                     "was da ist.")
        return "\n".join(teile)

    def _hinweis(self, w, text):
        if text and text not in w["hinweise"][-3:]:
            w["hinweise"].append(text)
            del w["hinweise"][:-3]

    async def on_message_passive(self, message):
        """Antwort an einen laufenden Assistenten? True = hier verarbeitet, die
        Nachricht geht nicht weiter (auch nicht an die Musik)."""
        if not self._wizards or not self._enabled:
            return False
        if getattr(message, "guild", None) is None:
            return False
        autor = message.author
        if getattr(autor, "bot", False):
            return False
        w = self._wizards.get((message.channel.id, autor.id))
        if w is None:
            return False
        text = (message.content or "").strip()
        if not text:
            return False
        if _sendepause(autor.id):
            return False                # steht still, bot.py schweigt sowieso
        if ai.FloAI._NOTLAGE_RE.search(text):
            return False                # nie als Song schlucken
        try:
            return await self._verarbeiten(w, message, text)
        except Exception:  # noqa: BLE001 - der Assistent darf nie den Bot mitnehmen
            log.exception("Playlist-Assistent fehlgeschlagen")
            return True

    def _kern(self, message, text):
        """(an Flo gerichtet?, Text ohne Anrede/Flo davor).

        Kuenstler, die selbst Flo heissen ('Flo Rida - Low'), sind ein Song und
        keine Ansprache."""
        an = basis.an_flo(message, text)
        if an and text.lower().startswith(_FLO_KUENSTLER):
            an = False
        if not an:
            return False, text
        ohne = basis._ANREDE_VORNE_RE.sub("", text)
        return True, ai.strip_lead(ohne)

    def _schlusswort(self, message, zeile):
        """'fertig' / 'abbruch' / None - fuer eine einzelne Zeile, auch mit Flo
        davor ('flo fertig', 'Flo, stop')."""
        wort = _wort(self._kern(message, zeile)[1])
        if wort in _FERTIG:
            return "fertig"
        if wort in _ABBRUCH:
            return "abbruch"
        return None

    async def _verarbeiten(self, w, message, text):
        an, kern = self._kern(message, text)
        wort = _wort(kern)
        if wort in _FERTIG:
            await self._fertig(w, nachricht=message)
            return True
        if wort in _ABBRUCH:
            await self._abbrechen(w, nachricht=message)
            return True
        zeilen = [z.strip() for z in text.splitlines() if z.strip()]
        if an and len(zeilen) < 2:
            return False                # Befehle und KI kuemmern sich
        # Die letzte Zeile darf abschliessen ('Song A\nSong B\nflo fertig')
        # oder abbrechen - ein Song wird aus ihr nie.
        schluss = self._schlusswort(message, zeilen[-1]) if len(zeilen) > 1 else None
        if schluss is not None:
            zeilen = zeilen[:-1]
        if schluss == "abbruch":
            await self._abbrechen(w, nachricht=message)
            return True
        links_vorn = []
        if an:
            # 'Flo, hier meine Songs:\nSong A\nSong B' - die angesprochene
            # erste Zeile ist Rede an Flo, kein Song. Nur ihre Links zaehlen.
            links_vorn = music._URL_RE.findall(zeilen[0])
            zeilen = zeilen[1:]
        zu_viele = len(zeilen) > MAX_ZEILEN
        einheiten = links_vorn + self._einheiten(zeilen[:MAX_ZEILEN])
        if an and schluss is None and len(einheiten) < 2:
            # 'Flo\nwie spät ist es?', 'flo spiel X\nund dann Y': an Flo, nicht
            # an den Assistenten - Befehle und KI kuemmern sich.
            return False
        if not einheiten and schluss is None:
            return False                # alles Chat - laeuft normal weiter
        if einheiten:
            await self._einfuegen(w, message, einheiten, zu_viele=zu_viele)
        if schluss == "fertig":
            await self._fertig(w, nachricht=message)
        return True

    @staticmethod
    def _ist_chat(zeile):
        if zeile.endswith(("?", ":")) or len(zeile) > 120 or len(zeile.split()) > 12:
            return True                 # Frage, Ueberschrift ('Meine Songs:'), Aufsatz
        if re.search(r"<a?:\w+:\d+>|<[@#][!&]?\d+>|@everyone|@here", zeile):
            return True
        if not re.search(r"[^\W_]", zeile):
            return True                 # nur Emojis/Satzzeichen
        return _wort(zeile) in _CHAT_WOERTER

    def _einheiten(self, zeilen):
        """Zeilen -> was aufgeloest wird: je Link ein Eintrag, sonst die Zeile.
        Chat-Zeilen fallen raus."""
        raus = []
        for zeile in zeilen:
            zeile = _AUFZAEHLUNG_RE.sub("", zeile).strip()
            if not zeile:
                continue
            links = music._URL_RE.findall(zeile)
            if links:
                raus.extend(links)
                continue
            if self._ist_chat(zeile):
                continue
            raus.append(zeile)
        return raus

    _zu_viele_alben = (f"{MAX_ERWEITERUNGEN} ganze Alben/Playlists reichen. "
                       "Den Rest tippst du gefälligst einzeln.")

    def _frei(self, w):
        return MAX_SONGS - w["bestand"] - len(w["songs"])

    async def _reaktion(self, message, emoji, weg=False):
        try:
            if weg:
                ich = getattr(getattr(message, "guild", None), "me", None)
                if ich is not None and hasattr(message, "remove_reaction"):
                    await message.remove_reaction(emoji, ich)
            else:
                await message.add_reaction(emoji)
        except Exception:  # noqa: BLE001 - reine Deko
            pass

    async def _einfuegen(self, w, message, einheiten, *, zu_viele=False):
        """Eingefuegte Zeilen aufloesen und anhaengen (Reaktion je Nachricht)."""
        deckel = (f"Mehr als {MAX_SONGS} Songs? Das ist keine Playlist mehr, das ist "
                  "ein Hilferuf.")
        async with w["lock"]:
            if w["zu"] or self._wizards.get(w["key"]) is not w:
                # Kam hinter 'fertig', dem ✅-Knopf oder der Frist an (die
                # warteten schon auf die Sperre): nicht still schlucken.
                await self._zu_spaet(w, message)
                return
            mit_netz = any(music._URL_RE.search(e) for e in einheiten)
            if mit_netz:
                await self._reaktion(message, "🔎")
            # Erst planen (ohne Netz): Deckel und Alben-Grenze. Jede Zeile
            # bringt mindestens einen Song - ist rechnerisch kein Platz mehr,
            # wird sie gar nicht erst aufgeloest.
            frei = self._frei(w)
            erweiterungen = w["erweiterungen"]
            plan = []
            for einheit in einheiten:
                grund, platz = None, min(max(frei, 0), ERWEITERUNG_SONGS)
                if frei <= 0:
                    grund = deckel
                elif musikquellen.ist_erweiterung(einheit):
                    if erweiterungen >= MAX_ERWEITERUNGEN:
                        grund = self._zu_viele_alben
                    else:
                        erweiterungen += 1
                if grund is None:
                    frei -= 1
                plan.append((einheit, grund, platz))
            # Dann gleichzeitig aufloesen - fuenfzig Links nacheinander (oEmbed,
            # yt-dlp, Spotify) hielten den Assistenten sonst eine Minute fest.
            # Die Reihenfolge bleibt die der Zeilen.
            sperre = asyncio.Semaphore(AUFLOESEN_PARALLEL)

            async def holen(einheit, platz):
                async with sperre:
                    return await musikquellen.eintraege(einheit, platz=platz)
            antworten = iter(await asyncio.gather(
                *(holen(e, platz) for e, grund, platz in plan if grund is None),
                return_exceptions=True))
            if w["zu"]:
                # Waehrenddessen zu (neuer Assistent derselben Person,
                # Herunterfahren) - die geholten Songs haben kein Zuhause mehr.
                if mit_netz:
                    await self._reaktion(message, "🔎", weg=True)
                await self._reaktion(message, "❌")
                return
            voll = teil = nix = 0
            for einheit, grund, _platz in plan:
                if grund is not None:
                    self._hinweis(w, grund)
                    nix += 1
                    continue
                antwort = next(antworten)
                if isinstance(antwort, BaseException):
                    log.warning("Playlist-Zeile nicht aufloesbar (%s): %s",
                                type(antwort).__name__, einheit[:120])
                    self._hinweis(w, "Da ist was schiefgelaufen. Die Zeile bleibt draußen.")
                    nix += 1
                    continue
                songs, art, fehler = antwort
                if art in musikquellen.ERWEITERUNGEN and songs:
                    # Kurzlinks (spotify.link, on.soundcloud.com) zeigen erst nach
                    # dem Aufloesen, ob sie ein Album sind - deshalb hier nochmal.
                    if w["erweiterungen"] >= MAX_ERWEITERUNGEN:
                        self._hinweis(w, self._zu_viele_alben)
                        nix += 1
                        continue
                    w["erweiterungen"] += 1
                neu = doppelt = 0
                gedeckelt = False
                for song in songs:
                    merkmal = _merkmal(song.get("i"))
                    if merkmal in w["gesehen"]:
                        doppelt += 1
                        continue
                    if self._frei(w) <= 0:
                        self._hinweis(w, deckel)
                        gedeckelt = True
                        break
                    w["gesehen"].add(merkmal)
                    w["songs"].append(song)
                    neu += 1
                if fehler:
                    self._hinweis(w, fehler)
                if doppelt:
                    self._hinweis(w, f"{_songs_text(doppelt)} hatte ich schon. "
                                     "Doppelt hilft auch nicht, du Goldfisch.")
                if neu and not (fehler or doppelt or gedeckelt):
                    voll += 1
                elif neu or doppelt:
                    teil += 1           # teilweise - oder alles schon drin
                else:
                    nix += 1
            if zu_viele:
                self._hinweis(w, f"Mehr als {MAX_ZEILEN} Zeilen auf einmal lese ich nicht. "
                                 "Der Rest ist weg.")
                teil += 1
            if mit_netz:
                await self._reaktion(message, "🔎", weg=True)
            await self._reaktion(message, "✅" if voll and not (teil or nix)
                                 else ("⚠️" if voll or teil else "❌"))
            if not w["zu"]:
                w["frist"] = _uhr() + WIZARD_TIMEOUT
                await self._status_planen(w)

    async def _zu_spaet(self, w, message):
        """Songs fuer einen Assistenten, der gerade zugemacht hat: ❌ und ein
        Satz, wo sie jetzt hinmuessen."""
        await self._reaktion(message, "❌")
        name = w.get("gespeichert")
        if name:
            text = (f"Zu spät, **{_sauber(name)}** ist schon gespeichert. Das hier hab ich "
                    f"nicht mehr genommen – häng's dran: "
                    f"`{self._bot_name} playlist {_code(name)} dazu`")
        else:
            text = "Zu spät, der Assistent ist schon zu. Das hier hab ich nicht mehr genommen."
        await self._sagen(w, text, nachricht=message)

    # --- Statusnachricht ----------------------------------------------------------
    async def _status_planen(self, w):
        """Status nachziehen - sofort, wenn der letzte Edit lange genug her ist,
        sonst einmal gesammelt (hoechstens alle EDIT_ABSTAND Sekunden)."""
        w["edit_faellig"] = True
        warten = w["letzter_edit"] + EDIT_ABSTAND - _uhr()
        if warten <= 0:
            await self._status_jetzt(w)
            return
        task = w.get("edit_task")
        if task is not None and not task.done():
            return
        w["edit_task"] = asyncio.ensure_future(self._status_spaeter(w, warten))

    async def _status_spaeter(self, w, warten):
        await asyncio.sleep(warten)
        await self._status_jetzt(w)

    async def _status_jetzt(self, w):
        if not w["edit_faellig"] or w["zu"]:
            return
        # Unter der Edit-Sperre: ein Status-Edit, der noch unterwegs ist, darf
        # nie NACH der Karte ankommen (die Knoepfe waeren weg) - _schliessen
        # wartet auf ihn, und wer danach kommt, sieht 'zu'.
        async with w["edit_lock"]:
            if w["zu"]:
                return
            await self._status_senden(w)

    async def _status_senden(self, w):
        w["edit_faellig"] = False
        w["letzter_edit"] = _uhr()
        w["view"].aufbauen()
        msg = w["status"]
        if msg is not None:
            try:
                await msg.edit(view=w["view"], allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.NotFound:
                w["status"] = None      # weggeraeumt -> einmal neu posten
            except discord.HTTPException as exc:
                log.info("Playlist-Status nicht bearbeitbar: %s", exc)
                return
        try:
            w["status"] = await w["kanal"].send(
                view=w["view"], allowed_mentions=discord.AllowedMentions.none())
            self.schuetzen(w["status"])
        except discord.HTTPException as exc:
            log.info("Playlist-Status nicht postbar: %s", exc)

    async def _schliessen(self, w, view, *, interaction=None):
        """Assistent zu: Status-Knoepfe weg (Karte oder Abschlusstext rein)."""
        w["zu"] = True
        self._wizards.pop(w["key"], None)
        task = w.get("edit_task")
        if task is not None and not task.done():
            task.cancel()
        if w.get("view") is not None:
            w["view"].stop()            # nur normale Knoepfe drin - stop() ist hier richtig
        async with w["edit_lock"]:
            return await self._abschluss_zeigen(w, view, interaction)

    async def _abschluss_zeigen(self, w, view, interaction):
        msg = w.get("status")
        if interaction is not None:
            try:
                if interaction.response.is_done():
                    await interaction.edit_original_response(view=view)
                else:
                    await interaction.response.edit_message(view=view)
                msg = interaction.message or msg
                view = None
            except discord.HTTPException as exc:
                log.info("Playlist-Status (Klick) nicht bearbeitbar: %s", exc)
        if view is not None:
            gesendet = None
            if msg is not None:
                try:
                    await msg.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
                    gesendet = msg
                except discord.HTTPException:
                    gesendet = None
            if gesendet is None:
                try:
                    gesendet = await w["kanal"].send(
                        view=view, allowed_mentions=discord.AllowedMentions.none())
                    self.schuetzen(gesendet)
                except discord.HTTPException as exc:
                    log.info("Playlist-Abschluss nicht postbar: %s", exc)
            msg = gesendet or msg
        if msg is not None:
            self.freigeben(msg, delay=KARTE_FREIGABE)
        return msg

    async def _sagen(self, w, text, *, nachricht=None, interaction=None):
        try:
            if nachricht is not None:
                await basis.antworte(nachricht, text)
            elif interaction is not None:
                await interaction.followup.send(text)
            else:
                await w["kanal"].send(f"<@{w['uid']}> {text}",
                                      allowed_mentions=discord.AllowedMentions(
                                          everyone=False, roles=False,
                                          users=[discord.Object(w["uid"])]))
        except discord.HTTPException as exc:
            log.info("Playlist-Meldung nicht zustellbar: %s", exc)

    # --- Abschluss ------------------------------------------------------------------
    def _speichern(self, w):
        """Die Songs des Assistenten in den Speicher. -> (liste, neu) oder (None, grund).
        Rein synchron - zwischen Pruefen und Schreiben kommt niemand dazwischen."""
        if not w["songs"]:
            return None, "leer"
        gid = w["gid"]
        if w["modus"] == "dazu":
            liste = self._listen(gid).get(w["pid"])
            if isinstance(liste, dict):
                vorhanden = {_merkmal(s.get("i")) for s in liste.get("songs") or []
                             if isinstance(s, dict)}
                neu = [s for s in w["songs"] if _merkmal(s.get("i")) not in vorhanden]
                platz = max(0, MAX_SONGS - len(liste.get("songs") or []))
                liste.setdefault("songs", []).extend(neu[:platz])
                liste["geaendert"] = time.time()
                self._speichern_bald()
                w["gespeichert"] = liste.get("name")
                return liste, len(neu[:platz])
            # In der Zwischenzeit geloescht: dann eben als neue Liste.
        name = w["name"]
        if self._finde(gid, name) is not None:
            for nr in range(2, 10):
                kandidat = f"{name} {nr}"
                if self._finde(gid, kandidat) is None:
                    name = kandidat
                    break
            else:
                return None, (f"**{_sauber(name)}** gibt's inzwischen – in jeder "
                              "Variante. Such dir was anderes aus.")
        # Die Grenzen nochmal: waehrend der Assistent offen war, kann eine
        # andere Liste dazugekommen sein ('queue speichern', ein Zweiter).
        grund = self._platz_fuer_neue(gid, w["person"])
        if grund:
            return None, grund
        liste = self._anlegen(gid, name, w["person"], w["songs"])
        w["gespeichert"] = name
        log.info("Playlist '%s' gespeichert (%d Songs, Server %s).",
                 name, len(liste["songs"]), gid)
        return liste, len(liste["songs"])

    @staticmethod
    async def _erst_bestaetigen(w, interaction):
        """Ein Klick muss in 3 s beantwortet sein. Loest der Assistent gerade
        auf oder ist ein Status-Edit unterwegs, wird das knapp - dann erst
        bestaetigen (die Karte kommt danach per edit_original_response)."""
        if interaction is not None and (w["lock"].locked() or w["edit_lock"].locked()):
            await interaction.response.defer()

    async def _fertig(self, w, *, nachricht=None, interaction=None):
        """'fertig' (Text oder Knopf): speichern, Status wird zur Karte."""
        await self._erst_bestaetigen(w, interaction)
        async with w["lock"]:
            if w["zu"] or self._wizards.get(w["key"]) is not w:
                return
            liste, ergebnis = self._speichern(w)
            name = _sauber(w["name"])
            if liste is None:
                if ergebnis != "leer":
                    text = f"Nix gespeichert. {ergebnis}"
                elif w["modus"] == "dazu":
                    text = f"Nix dazu? Dann bleibt **{name}** halt, wie sie war."
                else:
                    text = ("Null Songs und dann 'fertig'? Ich speicher doch keine leere "
                            "Liste, du Clown.")
                await self._schliessen(w, self._text_view(f"### 🎶 {name}\n{text}"),
                                       interaction=interaction)
                await self._sagen(w, text, nachricht=nachricht, interaction=interaction)
                return
            await self._schliessen(w, self._karte(liste), interaction=interaction)
            if w["modus"] == "dazu" and liste["id"] == w["pid"]:
                text = (f"💾 **{_sauber(liste['name'])}** hat {ergebnis} neue, jetzt "
                        f"{_songs_text(len(liste['songs']))}. Ob das jemand hören will, "
                        "ist eine andere Frage.")
            else:
                text = (f"💾 **{_sauber(liste['name'])}** gespeichert, "
                        f"{_songs_text(len(liste['songs']))}. Ob das jemand hören will, "
                        "ist eine andere Frage.")
            await self._sagen(w, text, nachricht=nachricht, interaction=interaction)

    async def _abbrechen(self, w, *, nachricht=None, interaction=None):
        await self._erst_bestaetigen(w, interaction)
        async with w["lock"]:
            if w["zu"] or self._wizards.get(w["key"]) is not w:
                return
            if w["modus"] == "dazu":
                text = f"🗑️ Weggeworfen. **{_sauber(w['name'])}** bleibt, wie sie war."
            else:
                text = "🗑️ Weggeworfen. War vermutlich besser so."
            await self._schliessen(w, self._text_view(f"### 🎶 {_sauber(w['name'])}\n{text}"),
                                   interaction=interaction)
            if interaction is None:
                await self._sagen(w, text, nachricht=nachricht)

    async def knopf(self, interaction, w, aktion):
        """✅ Fertig / 🗑 Abbrechen an der Statusnachricht. Dieselben Schranken
        wie die Karte: Sendepause und der Schalter 'music' (bot.py prueft fuer
        Klicks nichts davon)."""
        if w["zu"] or self._wizards.get(w["key"]) is not w:
            await self._ephemeral(interaction, "Der Assistent ist schon zu.")
            return
        if _sendepause(interaction.user.id):
            await self._ephemeral(interaction, "Sendepause. Ich red gerade mit keinem – "
                                               "mit dir schon gar nicht.")
            return
        if not features.is_on_in(w["gid"], "music"):
            await self._ephemeral(interaction, "Musik ist auf diesem Server aus. Pech gehabt.")
            return
        if aktion == "fertig":
            await self._fertig(w, interaction=interaction)
        else:
            await self._abbrechen(w, interaction=interaction)

    async def tick(self, client):
        """Abgelaufene Assistenten speichern oder wegwerfen, Status nachziehen."""
        if not self._wizards or not self._enabled:
            return None
        if _sendepause():
            return None                 # Flo redet nicht - die Frist holt er danach nach
        jetzt = _uhr()
        for w in list(self._wizards.values()):
            try:
                if w["lock"].locked():
                    continue            # loest gerade auf - naechster Takt
                if not features.is_on_in(w["gid"], "music"):
                    # Musik ist hier inzwischen aus: kein Status-Edit, kein
                    # '⏰ Du pennst?' mit Ping - bei Fristende still zumachen.
                    if w["frist"] <= jetzt:
                        await self._ablauf_still(w)
                    continue
                if w["frist"] <= jetzt:
                    await self._ablauf(w)
                elif w["edit_faellig"] and jetzt - w["letzter_edit"] >= EDIT_ABSTAND:
                    await self._status_jetzt(w)
            except Exception:  # noqa: BLE001 - einer darf die anderen nicht aufhalten
                log.exception("Playlist-Takt: Assistent '%s' hakt", w.get("name"))
        return None

    async def _ablauf(self, w):
        async with w["lock"]:
            if w["zu"] or self._wizards.get(w["key"]) is not w:
                return
            name = _sauber(w["name"])
            with ai.guild_kontext(w["gid"]):
                liste, ergebnis = self._speichern(w)
                if liste is not None:
                    await self._schliessen(w, self._karte(liste))
                    await self._sagen(w, f"⏰ Du pennst? Ich hab gespeichert, was da war: "
                                         f"**{_songs_text(len(w['songs']))}** in "
                                         f"**{_sauber(liste['name'])}**. Gern geschehen.")
                    return
                if ergebnis != "leer":
                    text = f"⏰ Fünf Minuten nix – und speichern geht nicht. {ergebnis}"
                elif w["modus"] == "dazu":
                    text = f"⏰ Fünf Minuten nix. **{name}** bleibt, wie sie war."
                else:
                    text = f"⏰ Fünf Minuten nix. **{name}** gibt's nicht, war eh nichts drin."
                await self._schliessen(w, self._text_view(f"### 🎶 {name}\n{text}"))
                await self._sagen(w, text)

    async def _ablauf_still(self, w):
        """Frist um, aber Musik auf dem Server aus: speichern, was da ist (die
        Daten gehen nicht verloren), die Statusnachricht ohne Knoepfe - keine
        neue Nachricht, kein Ping."""
        async with w["lock"]:
            if w["zu"] or self._wizards.get(w["key"]) is not w:
                return
            liste, ergebnis = self._speichern(w)
            text = (f"### 🎶 {_sauber(w['name'], 60)}\nMusik ist auf diesem Server aus – "
                    "der Assistent ist zu.")
            if liste is not None:
                text += (f" Was drin war, liegt in **{_sauber(liste['name'])}** "
                         f"({_songs_text(len(liste.get('songs') or []))}).")
            elif w["songs"]:
                text += (f" Speichern ging nicht: {ergebnis} "
                         f"Futsch: {_songs_text(len(w['songs']))}.")
            log.info("Playlist-Assistent '%s' still geschlossen (Musik aus, Server %s).",
                     w["name"], w["gid"])
            await self._schliessen(w, self._text_view(text))

    async def offene_sichern(self):
        """Beim Herunterfahren: Assistenten mit Songs noch speichern - und
        schliessen, damit ein zweiter Aufruf nichts doppelt anhaengt.

        Erst wird gespeichert (synchron) und geschrieben, DANN - so weit es in
        STATUS_FRIST geht - die Statusnachricht zur Karte gemacht: deren Knoepfe
        ueberleben den Neustart, die des Assistenten nicht."""
        gesichert = 0
        aufraeumen = []
        for w in list(self._wizards.values()):
            self._wizards.pop(w["key"], None)
            w["zu"] = True
            task = w.get("edit_task")
            if task is not None and not task.done():
                task.cancel()
            liste, grund = None, "Playlists sind aus."
            try:
                if w.get("view") is not None:
                    w["view"].stop()
                if self._enabled and w["songs"]:
                    liste, grund = self._speichern(w)
                    gesichert += liste is not None
            except Exception:  # noqa: BLE001 - beim Beenden nie abbrechen
                log.exception("Playlist-Assistent '%s' nicht gesichert", w.get("name"))
                grund = "Beim Speichern ist was schiefgelaufen."
            aufraeumen.append((w, liste, grund))
        if gesichert and self._store is not None:
            log.info("%d offene Playlist-Assistenten beim Beenden gespeichert.", gesichert)
            await self._store.flush_now()
        for w, liste, grund in aufraeumen:
            msg = w.get("status")
            if msg is None:
                continue
            kopf = f"### 🎶 {_sauber(w['name'], 60)}\n🔄 Ich starte neu – der Assistent ist zu."
            if liste is not None:
                ansicht = self._karte(liste)
            elif w["songs"]:
                # Es WAR was drin - nur speichern ging nicht (Grenze, Name weg).
                ansicht = self._text_view(
                    f"{kopf} Speichern ging nicht: {grund} "
                    f"Futsch: {_songs_text(len(w['songs']))}.")
            else:
                ansicht = self._text_view(f"{kopf} War eh nix drin.")
            try:
                await asyncio.wait_for(self._beim_beenden_zeigen(w, msg, ansicht),
                                       STATUS_FRIST)
            except Exception:  # noqa: BLE001 - beim Beenden nie abbrechen
                log.info("Playlist-Status beim Beenden nicht mehr bearbeitbar.")
        return gesichert

    @staticmethod
    async def _beim_beenden_zeigen(w, msg, ansicht):
        async with w["edit_lock"]:
            await msg.edit(view=ansicht)


class _StatusAnsicht(discord.ui.LayoutView):
    """Die EINE Statusnachricht des Assistenten. Normale Knoepfe (nicht
    neustartfest - nach einem Neustart gibt es den Assistenten ja auch nicht
    mehr); beim Schliessen wird stop() gerufen, die View ist dann weg."""

    def __init__(self, w):
        super().__init__(timeout=None)
        self.w = w
        self._text = discord.ui.TextDisplay("…")
        fertig = discord.ui.Button(label="Fertig", emoji="✅", style=discord.ButtonStyle.success)
        weg = discord.ui.Button(label="Abbrechen", emoji="🗑️", style=discord.ButtonStyle.danger)
        fertig.callback = self._fertig
        weg.callback = self._weg
        self.add_item(discord.ui.Container(self._text, discord.ui.ActionRow(fertig, weg),
                                           accent_colour=FARBE))
        self.aufbauen()

    def aufbauen(self):
        self._text.content = instance._status_text(self.w)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.w["uid"]:
            return True
        await instance._ephemeral(interaction, "Finger weg. Das ist nicht dein Assistent.")
        return False

    async def _fertig(self, interaction):
        await instance.knopf(interaction, self.w, "fertig")

    async def _weg(self, interaction):
        await instance.knopf(interaction, self.w, "weg")


class _LoeschFrage(discord.ui.View):
    """'Wirklich loeschen?' - eine kurze Rueckfrage (kein DynamicItem, stop() ok)."""

    def __init__(self, gid, pid, uid, *, karte=None):
        super().__init__(timeout=60)
        self.gid, self.pid, self.uid, self.karte = gid, pid, uid, karte
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.uid:
            return True
        await instance._ephemeral(interaction, "Du wurdest nicht gefragt.")
        return False

    @discord.ui.button(label="Ja, weg damit", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def _ja(self, interaction, _knopf):
        self.stop()
        await instance.loeschen_bestaetigt(interaction, self.gid, self.pid, karte=self.karte)

    @discord.ui.button(label="Doch nicht", style=discord.ButtonStyle.secondary)
    async def _nein(self, interaction, _knopf):
        self.stop()
        await interaction.response.edit_message(content="Dann halt nicht. Feigling.", view=None)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


# --- Die Knoepfe der Karte (ueberleben Neustarts dank fester custom_id) -------
# 'flo:pl:play:k3f9a2' - Aktion und Listen-ID stecken in der custom_id, alles
# andere steht im Speicher. Die Karte ist eine LayoutView mit timeout=None und
# wird NIE gestoppt: stop() nimmt die Vorlage global aus discord.py, danach
# waeren die Knoepfe ALLER Karten tot.
class PlaylistKnopf(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"flo:pl:(?P<aktion>play|mix|add|del):(?P<pid>[0-9a-z]{6})"):

    def __init__(self, aktion, pid, *, label=None, emoji=None,
                 style=discord.ButtonStyle.secondary):
        self.aktion, self.pid = aktion, pid
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"flo:pl:{aktion}:{pid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["aktion"], match["pid"], label=item.label, emoji=item.emoji,
                   style=item.style)

    async def callback(self, interaction):
        await instance.klick(interaction, self.aktion, self.pid)


#: Die Knoepfe der Playlist-Karte (Abspielen, Gemischt, Dazu, Loeschen), die
#: einen Neustart ueberleben - bot.setup_hook meldet sie an.
DYNAMISCHE_KNOEPFE = (PlaylistKnopf,)


instance = Playlists()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
on_message_passive = instance.on_message_passive
tick = instance.tick
offene_sichern = instance.offene_sichern
