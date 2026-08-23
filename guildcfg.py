"""Einstellungen JE SERVER (Guild).

Flo laeuft auf mehreren Servern gleichzeitig. Frueher stand jede Einstellung in
der .env - also fuer alle Server dieselbe: derselbe Level-Up-Kanal, dieselbe
Lautstaerke, dieselben Aufraeum-Kanaele. Auf einem zweiten Server zeigten die
Kanal-IDs ins Leere, und wer etwas anders wollte, musste den Bot neu starten.

Dieses Modul haelt
  * einen KATALOG aller einstellbaren Werte (Schluessel, Typ, Standard, Grenzen)
  * und je Server nur die ABWEICHUNGEN davon.

Wer nichts einstellt, bekommt den Standard aus der .env - nach dem Update
verhaelt sich der bestehende Server also exakt wie vorher. Erst ein 'setzen'
legt fuer diesen einen Server einen eigenen Wert an.

Befehle (nur fuer Server-Admins bzw. den Besitzer):
    Flo einstellungen                 -> alles anzeigen
    Flo einstellung lautstaerke 80    -> setzen
    Flo einstellung lautstaerke standard  -> wieder den Standard nehmen

Das Web-Panel nutzt dieselben Methoden (alle/setzen/loeschen) - es gibt nur
EINE Wahrheit ueber die Einstellungen, nicht zwei.
"""

import logging
import os
import re
from dataclasses import dataclass

import discord

import ai
from basis import FeatureBasis
import numfmt
from store import JsonStore

log = logging.getLogger("dcbot.guildcfg")

HANDLED = object()

# Wer will erfahren, dass sich eine Einstellung geaendert hat?
# {key: [funktion(gid), ...]}. Gefuellt ueber horcht_auf().
_HOERER = {}


def horcht_auf(key, fn):
    """Meldet eine Funktion an, die bei Aenderung von 'key' gerufen wird.

    Aufzurufen im setup() des Moduls, das den Wert zwischenspeichert. Die
    Funktion bekommt die Server-ID und muss selbst wissen, was zu tun ist.
    Doppelte Anmeldungen werden ignoriert - setup() laeuft in Tests mehrfach."""
    liste = _HOERER.setdefault(key, [])
    if fn not in liste:
        liste.append(fn)

# Der Server, auf dem Flo "zu Hause" ist. Nur dort sind Sachen wie das
# automatische Server-Icon von Haus aus an - ein frisch eingeladener Server
# soll nicht ungefragt sein Icon getauscht bekommen.
# Der gigachat des Heimatservers. Steht hier als Name statt als nackte Zahl
# mitten im Katalog - und an EINER Stelle, falls er sich mal aendert.
GIGACHAT_CHANNEL = 1453881901738889351

HAUPT_GUILD = int(os.getenv("GUILD_ID", "0") or "0")


@dataclass
class Einstellung:
    """Eine einstellbare Groesse: was sie ist, wie sie gelesen wird, was gilt,
    wenn der Server nichts eigenes gesetzt hat."""

    key: str
    label: str
    typ: str                  # an_aus | zahl | prozent | channel | channels | text
    fallback: object          # gilt, wenn weder Server noch .env etwas sagen
    env: str = ""             # .env-Variable, die den Standard vorgibt
    hinweis: str = ""
    gruppe: str = "Allgemein"
    # None = keine Grenze. Die Pruefung in setzen() fragt darauf ab
    # (guildcfg.py:207), die Deklaration sagt es jetzt auch.
    minimum: "float | None" = None
    maximum: "float | None" = None
    # Standard nur auf dem Hauptserver AN (z. B. Server-Icon, Aktien-Zaehlung).
    nur_haupt: bool = False

    def standard(self, gid=0):
        """Der Wert, der ohne eigene Server-Einstellung gilt."""
        if self.nur_haupt and not self.env:
            return bool(HAUPT_GUILD) and int(gid or 0) == HAUPT_GUILD
        roh = os.getenv(self.env) if self.env else None
        if roh is None:
            return self.fallback
        # AUSDRUECKLICH leer ('AUTODELETE_CHANNEL_IDS=') ist eine Aussage und
        # kein "nicht gesetzt": das hiess bisher 'Funktion aus' und muss es
        # weiter heissen. Deshalb geht auch der leere Text durch die Pruefung -
        # bei Kanaelen kommt dort 0 bzw. eine leere Liste heraus.
        ok, wert, _fehler = _pruefe(self, roh)
        return wert if ok else self.fallback


# Die frueheren fest verdrahteten Kanaele des Hauptservers. Sie stehen HIER als
# fallback, damit sich am bestehenden Server nach dem Update nichts verschiebt:
# vorher galt genau dieser Wert, wenn die .env nichts sagte. Auf einem fremden
# Server loesen diese IDs schlicht nichts auf - dort greift der System-Kanal,
# bis jemand einen eigenen einstellt.
_ALT_COMMANDS = 1512045750362837013
_ALT_KALORIEN = 1522294725116428329

# Alle einstellbaren Werte. Jeder Eintrag hier wird auch WIRKLICH irgendwo
# gelesen - lieber zwoelf echte Schalter als vierzig, die nichts tun.
KATALOG = [
    # --- Ansprache -------------------------------------------------------
    Einstellung("praefix", "Ansprache", "text", "", "BOT_NAME",
                "Wie Flo auf DIESEM Server angesprochen wird (2–32 Zeichen, "
                "ein Wort). Die Aliasse aus BOT_ALIASES gelten weiter überall.",
                "Ansprache"),

    # --- Kanaele ---------------------------------------------------------
    Einstellung("ansage_channel", "Ansagen-Kanal", "channel", _ALT_COMMANDS,
                "LEVELUP_CHANNEL_ID",
                "Wohin Flo von sich aus postet: Händler, Lotto, Shop-Highlights.",
                "Kanäle"),
    Einstellung("levelup_channel", "Level-Up-Kanal", "channel", _ALT_COMMANDS,
                "LEVELUP_CHANNEL_ID",
                "Wohin die Level-Up-Karten gehen. Aus = in den Kanal, in dem "
                "geschrieben wurde.", "Kanäle"),
    Einstellung("event_channel", "Event-Kanal", "channel", _ALT_COMMANDS,
                "GAMES_EVENT_CHANNEL_ID",
                "Wo die Zufalls-Events (Schnell-Tippen) auftauchen.", "Kanäle"),
    Einstellung("zaehl_channel", "Zählspiel-Kanal", "channel", 0, "COUNTING_CHANNEL_ID",
                "Kanal, in dem gemeinsam gezählt wird. Aus = kein Zählspiel.",
                "Kanäle"),
    Einstellung("kalorien_channel", "Kalorien-Kanal", "channel", _ALT_KALORIEN,
                "KALORIEN_CHANNEL_ID",
                "Essensfotos hier werden automatisch analysiert.", "Kanäle"),
    Einstellung("modlog_channel", "Mod-Log-Kanal", "channel", 0, "MOD_LOG_CHANNEL_ID",
                "Protokoll aller Moderations-Aktionen. Aus = kein Protokoll.",
                "Kanäle"),
    Einstellung("autodelete_channels", "Aufräum-Kanäle", "channels", [_ALT_COMMANDS],
                "AUTODELETE_CHANNEL_IDS",
                "In diesen Kanälen räumt Flo alte Nachrichten weg.", "Kanäle"),

    # --- Musik -----------------------------------------------------------
    Einstellung("lautstaerke", "Lautstärke", "prozent", 50, "MUSIC_DEFAULT_VOLUME",
                "Womit Flo hier zu spielen anfängt (0–200 %).", "Musik",
                minimum=0, maximum=200),
    Einstellung("musik_max_queue", "Warteschlange höchstens", "zahl", 50,
                "MUSIC_MAX_QUEUE",
                "So viele Songs dürfen hier gleichzeitig warten.", "Musik",
                minimum=1, maximum=500),

    # --- Arbeit & Wort des Tages -----------------------------------------
    # Standard ist der gigachat des Heimatservers - dort soll das Wort des
    # Tages hin, ohne dass jemand erst etwas einstellen muss. Auf einem FREMDEN
    # Server gibt es diesen Kanal nicht; arbeit.kanal_fuer faellt dann von
    # selbst auf die Namenssuche ('gigachat', 'wordle', ...) und danach auf den
    # Ansagen-Kanal zurueck. Ueberschreibbar per .env (WORDLE_CHANNEL_ID) und
    # je Server ueber 'Flo einstellung wordle_channel'.
    Einstellung("wordle_channel", "Wort-des-Tages-Kanal", "channel",
                GIGACHAT_CHANNEL, "WORDLE_CHANNEL_ID",
                "Wohin das tägliche Wordle geht. Standard ist der gigachat; "
                "kennt dieser Server ihn nicht, sucht Flo einen Kanal namens "
                "'gigachat' oder 'wordle' und nimmt sonst den Ansagen-Kanal.",
                "Arbeit"),
    Einstellung("wordle_min_voice", "Wordle ab … Leuten im Voice", "zahl", 3, "",
                "Flo legt das Wort des Tages erst raus, wenn so viele Leute in "
                "einem Sprachkanal sitzen — nicht nach der Uhr. Ist an einem "
                "Tag nie was los, fällt es aus.", "Arbeit",
                minimum=1, maximum=50),

    # --- Spiel & Wirtschaft ----------------------------------------------
    Einstellung("casino_max_einsatz", "Maximaleinsatz", "zahl", 0, "",
                "Obergrenze je Casino-Runde auf DIESEM Server. 0 = die globale "
                "Grenze aus der .env.", "Spiel", minimum=0),
    Einstellung("levelup_ansagen", "Level-Up-Karten", "an_aus", True, "",
                "Ob Flo hier Level-Aufstiege ansagt. Aus = XP zählt weiter, "
                "aber ohne Karte.", "Spiel"),
    Einstellung("daily_erinnerung", "An den Daily erinnern", "an_aus", False, "",
                "Einmal am Tag eine kurze Erinnerung an den Tagesbonus im "
                "Ansagen-Kanal.", "Spiel"),

    # --- Verhalten -------------------------------------------------------
    Einstellung("autodelete_sekunden", "Aufräumen nach", "zahl", 60,
                "AUTODELETE_SECONDS",
                "So alt darf eine Nachricht in den Aufräum-Kanälen werden "
                "(Sekunden).", "Verhalten", minimum=10, maximum=86_400),
    Einstellung("bayern", "Bayrisch-Modus", "an_aus", False, "",
                "Flo antwortet auf diesem Server boarisch.", "Verhalten"),
    Einstellung("icon_auto", "Server-Icon automatisch", "an_aus", False, "",
                "Flo tauscht das Server-Icon nach Tages- und Jahreszeit. "
                "Braucht das Recht 'Server verwalten'.", "Verhalten",
                nur_haupt=True),
    Einstellung("aktie_zaehlt", "Zählt für die $FLO-Aktie", "an_aus", False, "",
                "Ob Calls und Chat dieses Servers den Kurs bewegen. Die Aktie "
                "ist für alle Server dieselbe.", "Verhalten", nur_haupt=True),
    Einstellung("schulden_pranger", "Überfällige Posten ansagen", "an_aus", False, "",
                "Ab 14 Tagen Verzug eine neutrale Notiz im Ansagen-Kanal – "
                "ohne Beträge, nur „X hat einen überfälligen Posten bei Y“. "
                "Standard: aus.", "Verhalten"),

    # --- Voice -----------------------------------------------------------
    # Lagen bis eben in voicegags.py in einem EIGENEN Speicher und galten fuer
    # ALLE Server gleich - und im Web-Panel tauchten sie gar nicht auf. Wer sie
    # umstellen wollte, brauchte den Bot-Besitzer.
    Einstellung("soundboard", "Soundboard", "an_aus", True, "",
                "Ob es das Sound-Brett (`Flo sounds`) auf diesem Server gibt.",
                "Voice"),
    Einstellung("join_sounds", "Join-Sounds", "an_aus", False, "JOIN_SOUNDS",
                "Flo spielt einen kurzen Sound, wenn jemand einen Sprachkanal "
                "betritt. Standard: aus.", "Voice"),

    # --- Moderation ------------------------------------------------------
    # Standen nur in der .env, galten fuer alle Server gleich und brauchten
    # einen Neustart. Der env-Wert bleibt der Standard, damit sich ohne eigene
    # Einstellung nichts aendert.
    Einstellung("warn_limit", "Verwarnungen bis Timeout", "zahl", 3, "WARN_LIMIT",
                "Nach so vielen Verwarnungen setzt Flo automatisch einen "
                "Timeout.", "Moderation", minimum=1, maximum=20),
    Einstellung("warn_timeout", "Auto-Timeout (Sekunden)", "zahl", 3600,
                "WARN_TIMEOUT_SECONDS",
                "Wie lange der automatische Timeout dauert. 0 = keiner.",
                "Moderation", minimum=0, maximum=2419200),
    Einstellung("timeout_standard", "Timeout-Standarddauer (Sekunden)", "zahl", 600,
                "MOD_DEFAULT_TIMEOUT",
                "Gilt, wenn `Flo timeout @wer` keine Dauer nennt.",
                "Moderation", minimum=1, maximum=2419200),
    Einstellung("purge_max", "Löschen höchstens", "zahl", 1000, "PURGE_MAX",
                "Obergrenze für `Flo lösch N` – schützt vor einem Vertipper, "
                "der den halben Kanal leert.", "Moderation", minimum=1,
                maximum=5000),
]

_NACH_KEY = {e.key: e for e in KATALOG}

_JA = ("an", "ein", "on", "1", "true", "ja", "yes", "j", "y", "aktiv")
_NEIN = ("aus", "off", "0", "false", "nein", "no", "n", "inaktiv", "weg", "keiner",
         "keine", "nichts", "-")
_STANDARD_WOERTER = ("standard", "default", "zurück", "zurueck", "reset", "auto")

_CHANNEL_RE = re.compile(r"<#(\d+)>")


# _pruefe und _kanal_id stehen BEWUSST auf Modulebene und nicht in GuildConfig:
# Einstellung.standard() braucht sie, und ein Dataclass-Feld darf nicht am
# Singleton haengen (sonst haette der Katalog eine Abhaengigkeit auf die
# Instanz, die ihn selbst benutzt).
def _pruefe(defn, roh, guild=None):
    """Wandelt eine Eingabe in den Typ der Einstellung. (ok, wert, fehler)."""
    if defn.typ == "an_aus":
        w = str(roh).strip().lower()
        if w in _JA:
            return True, True, ""
        if w in _NEIN:
            return True, False, ""
        return False, None, "Sag „an“ oder „aus“."

    if defn.typ in ("zahl", "prozent"):
        w = str(roh).strip().rstrip("%").replace(",", ".").strip()
        try:
            # OverflowError faengt 'inf' ab: float('inf') geht durch, int() davon
            # nicht - ohne das kippt schon 'Flo einstellung lautstaerke inf'.
            zahl = int(round(float(w)))
        except (TypeError, ValueError, OverflowError):
            return False, None, "Das ist keine Zahl."
        if zahl != zahl:        # NaN - kommt durch float('nan') herein
            return False, None, "Das ist keine Zahl."
        if defn.minimum is not None and zahl < defn.minimum:
            return False, None, f"Mindestens {int(defn.minimum)}."
        if defn.maximum is not None and zahl > defn.maximum:
            return False, None, f"Höchstens {int(defn.maximum)}."
        return True, zahl, ""

    if defn.typ == "channel":
        cid, fehler = _kanal_id(roh, guild)
        if fehler:
            return False, None, fehler
        return True, cid, ""

    if defn.typ == "channels":
        # Eine LISTE muss hier nativ ankommen duerfen. get() schickt den
        # gespeicherten Wert zur Sicherheit nochmal durch diese Pruefung -
        # und str([123, 456]) ergab die Tokens '[123,' und '456]', die an der
        # Kanal-Erkennung scheiterten. Folge: get() lieferte IMMER den
        # Standard, die Aufraeum-Kanaele waren faktisch nicht einstellbar.
        if isinstance(roh, (list, tuple, set)):
            teile = [str(t) for t in roh]
        else:
            teile = re.split(r"[,\s]+", str(roh).strip())
        raus = []
        for teil in teile:
            teil = teil.strip()
            if not teil:
                continue
            if teil.lower() in _NEIN:
                continue
            cid, fehler = _kanal_id(teil, guild)
            if fehler:
                return False, None, fehler
            if cid and cid not in raus:
                raus.append(cid)
        return True, raus, ""

    # text
    wert = str(roh).strip()
    if defn.key == "praefix" and wert:
        # Die Ansprache ist kein Fliesstext: sie steht am Satzanfang und wird
        # zum Regex. Mehrere Woerter, Regex-Sonderzeichen oder ein einzelner
        # Buchstabe wuerden entweder nie oder staendig treffen.
        if len(wert) < 2 or len(wert) > 32:
            return False, None, "Die Ansprache braucht 2 bis 32 Zeichen."
        if re.search(r"\s", wert):
            return False, None, "Die Ansprache muss EIN Wort sein (ohne Leerzeichen)."
        if not re.match(r"^[\w'-]+$", wert, re.UNICODE):
            return False, None, ("Nur Buchstaben, Ziffern, Unterstrich, "
                                 "Bindestrich und Apostroph.")
    return True, wert[:200], ""


def _kanal_id(roh, guild=None):
    """Kanal aus Erwähnung (<#123>), roher ID oder Name. (id, fehler)."""
    w = str(roh).strip()
    if not w or w.lower() in _NEIN:
        return 0, ""
    m = _CHANNEL_RE.search(w)
    if m:
        return int(m.group(1)), ""
    if numfmt.ist_zahl(w):
        cid = int(w)
        # Eine ID aus einem FREMDEN Server anzunehmen hilft niemandem - der
        # Kanal ist von hier aus nie erreichbar.
        if guild is not None and guild.get_channel(cid) is None:
            return 0, "Diesen Kanal gibt es auf diesem Server nicht."
        return cid, ""
    if guild is not None:
        name = w.lstrip("#").lower()
        for ch in getattr(guild, "text_channels", []) or []:
            if (ch.name or "").lower() == name:
                return ch.id, ""
    return 0, "Kanal nicht gefunden – nimm eine #Erwähnung."


class GuildConfig(FeatureBasis):
    """Haelt je Server die Abweichungen vom Standard - und den Befehl dazu."""

    _CMDS = ("einstellungen", "einstellung", "config", "konfig", "konfiguration",
             "servereinstellungen", "settings")
    # Die Funktions-Schalter (Casino, Musik, ...) liegen in features.json und
    # waren bisher NUR im Web-Panel erreichbar. Damit beides denselben Weg hat,
    # gibt es sie jetzt auch als Befehl - dieselben Rechte, dieselbe Wahrheit.
    _CMDS_FEATURES = ("funktionen", "funktion", "features", "feature", "module")

    def __init__(self):
        self._enabled = False
        self._owner_id = 0
        self._store = None

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        self._owner_id = int(os.getenv("OWNER_ID", "0") or "0")
        self._store = JsonStore("guildcfg.json", default={"guilds": {}})
        self._enabled = True
        # Die Ansprache steckt in fertig gebauten Regexen (ai._RE_CACHE) - ohne
        # diesen Anstoss haette 'Flo einstellung praefix Bob' erst nach einem
        # Neustart gewirkt. Steht hier und nicht in ai.setup(), weil ai die
        # guildcfg nur lazy importieren darf (sonst gaebe es einen Ring).
        horcht_auf("praefix", ai.praefix_geaendert)
        log.info("Server-Einstellungen aktiv (%d Server mit eigenen Werten, "
                 "%d Schalter im Katalog).", len(self._guilds()), len(KATALOG))
        return True

    def is_enabled(self):
        return self._enabled

    # --- Zugriff ----------------------------------------------------------
    def _guilds(self):
        if self._store is None:
            return {}
        return self._store.data.setdefault("guilds", {})

    def _eigene(self, gid):
        """Die selbst gesetzten Werte dieses Servers (leer, wenn keine)."""
        if not gid:
            return {}
        return self._guilds().get(str(int(gid)), {}) or {}

    @staticmethod
    def defn(key):
        return _NACH_KEY.get(key)

    def standard(self, key, gid=0):
        defn = _NACH_KEY.get(key)
        return None if defn is None else defn.standard(gid)

    def get(self, gid, key):
        """Der geltende Wert: eigener Server-Wert, sonst Standard.

        Robust auch VOR setup() (Tests, Import-Zeit): dann gilt der Standard."""
        defn = _NACH_KEY.get(key)
        if defn is None:
            return None
        eigen = self._eigene(gid)
        if key in eigen:
            ok, wert, _f = _pruefe(defn, eigen[key])
            if ok:
                return wert
            # Unbrauchbarer Eintrag (von Hand editiert) - lieber der Standard
            # als ein Absturz an der Lesestelle.
            log.warning("Server %s: Wert fuer '%s' unbrauchbar (%r) - nehme Standard.",
                        gid, key, eigen[key])
        return defn.standard(gid)

    def an(self, gid, key):
        """Kurzform fuer An/Aus-Schalter."""
        return bool(self.get(gid, key))

    def eigen(self, gid, key):
        """True, wenn dieser Server einen eigenen Wert gesetzt hat."""
        return key in self._eigene(gid)

    async def setzen(self, gid, key, roh, guild=None):
        """Setzt einen Wert fuer EINEN Server. (ok, wert, fehler)."""
        defn = _NACH_KEY.get(key)
        if defn is None:
            return False, None, "Diese Einstellung gibt es nicht."
        if not gid:
            return False, None, "Das geht nur auf einem Server."
        if str(roh).strip().lower() in _STANDARD_WOERTER:
            await self.loeschen(gid, key)
            return True, self.get(gid, key), ""
        ok, wert, fehler = _pruefe(defn, roh, guild)
        if not ok:
            return False, None, fehler
        if self._store is None:
            return False, None, "Die Einstellungen sind gerade nicht bereit."
        self._guilds().setdefault(str(int(gid)), {})[key] = wert
        await self._speichern()
        self._nachziehen(gid, key)
        log.info("Server %s: '%s' = %r", gid, key, wert)
        return True, wert, ""

    async def loeschen(self, gid, key):
        """Nimmt den eigenen Wert zurueck - ab dann gilt wieder der Standard."""
        eigen = self._guilds().get(str(int(gid or 0)), {})
        if key not in eigen:
            return False
        del eigen[key]
        if not eigen:
            self._guilds().pop(str(int(gid)), None)
        await self._speichern()
        self._nachziehen(gid, key)
        log.info("Server %s: '%s' auf Standard zurueck.", gid, key)
        return True

    async def vergessen(self, gid):
        """Alle eigenen Werte eines Servers loeschen (Bot wurde rausgeworfen)."""
        if self._guilds().pop(str(int(gid or 0)), None) is None:
            return False
        await self._speichern()
        self._nachziehen(gid, "praefix")
        log.info("Server %s: Einstellungen geloescht (Bot ist dort nicht mehr).", gid)
        return True

    @staticmethod
    def _nachziehen(gid, key):
        """Sagt allen Bescheid, die an dieser Einstellung haengen.

        Wer einen Wert nicht bei jedem Gebrauch neu liest, sondern sich ihn
        merkt, erfaehrt sonst NIE von einer Aenderung - im Panel klickt jemand,
        und bis zum Neustart passiert nichts. Genau das ist mit der Lautstaerke
        passiert.

        Frueher stand hier eine if-Kette, die mit jedem neuen Verbraucher haette
        wachsen muessen - und die niemand anfasst, der ein Modul aendert.
        Jetzt meldet sich jeder Verbraucher in seinem eigenen setup() selbst an
        (guildcfg.horcht_auf), und die Abhaengigkeit steht da, wo sie hingehoert."""
        for fn in list(_HOERER.get(key, ())):
            try:
                fn(gid)
            except Exception:  # noqa: BLE001 - eine Einstellung ist nie fatal
                log.warning("Einstellung '%s': ein Hoerer ist gescheitert.", key,
                            exc_info=True)

    async def _speichern(self):
        if self._store is None:
            return
        try:
            await self._store.save()
        except Exception:  # noqa: BLE001 - eine Einstellung darf nie fatal sein
            log.exception("Server-Einstellungen konnten nicht gespeichert werden")

    def alle(self, gid):
        """Alle Einstellungen dieses Servers fuer Panel und Embed."""
        raus = []
        for defn in KATALOG:
            raus.append({
                "key": defn.key,
                "label": defn.label,
                "typ": defn.typ,
                "gruppe": defn.gruppe,
                "hinweis": defn.hinweis,
                "wert": self.get(gid, defn.key),
                "standard": defn.standard(gid),
                "eigen": self.eigen(gid, defn.key),
                "min": defn.minimum,
                "max": defn.maximum,
            })
        return raus

    # --- Anzeige ----------------------------------------------------------
    def text(self, gid, key):
        """Der Wert als lesbarer Text ('an', '#allgemein', '80 %')."""
        defn = _NACH_KEY.get(key)
        if defn is None:
            return "?"
        return self._als_text(defn, self.get(gid, key))

    @staticmethod
    def _als_text(defn, wert):
        if defn.typ == "an_aus":
            return "an" if wert else "aus"
        if defn.typ == "prozent":
            return f"{int(wert)} %"
        if defn.typ == "zahl":
            return str(int(wert))
        if defn.typ == "channel":
            return f"<#{int(wert)}>" if wert else "aus"
        if defn.typ == "channels":
            liste = list(wert or [])
            return ", ".join(f"<#{int(c)}>" for c in liste) if liste else "keine"
        return str(wert or "–")

    def _embed(self, guild):
        gid = getattr(guild, "id", 0)
        emb = discord.Embed(
            title=f"⚙️ Einstellungen für {getattr(guild, 'name', 'diesen Server')}",
            description=(f"Jeder Server hat seine eigenen. Ändern mit "
                         f"`{self._bot_name} einstellung <name> <wert>`, "
                         f"zurücksetzen mit `… standard`."),
            color=discord.Color.blurple(),
        )
        gruppen = {}
        for defn in KATALOG:
            gruppen.setdefault(defn.gruppe, []).append(defn)
        for gruppe, defs in gruppen.items():
            zeilen = []
            for defn in defs:
                marke = "▸" if self.eigen(gid, defn.key) else "·"
                zeilen.append(f"{marke} `{defn.key}` — **{self._als_text(defn, self.get(gid, defn.key))}**")
            emb.add_field(name=gruppe, value="\n".join(zeilen), inline=False)
        eigene = len(self._eigene(gid))
        emb.set_footer(text=f"▸ = hier eigens gesetzt ({eigene}) · · = Standard")
        return emb

    def darf(self, message):
        """Darf diese Person Server-Einstellungen aendern? ('Server verwalten')

        Oeffentlich, damit JEDER Weg denselben Check benutzt - vorher hat
        bayern.py die Einstellung ganz ohne Pruefung umgelegt."""
        return self._darf(message)

    def _darf(self, message):
        """Nur Server-Admins (Server verwalten) und der Besitzer duerfen schalten."""
        autor = getattr(message, "author", None)
        if autor is not None and self._owner_id and getattr(autor, "id", 0) == self._owner_id:
            return True
        rechte = getattr(autor, "guild_permissions", None)
        return bool(getattr(rechte, "manage_guild", False))

    # --- Befehl -----------------------------------------------------------
    async def _funktionen(self, message, rest):
        """'Flo funktionen' / 'Flo funktion casino aus' - je Server.

        Bisher gab es diese Schalter NUR im Web-Panel. Wer kein Panel offen
        hatte, konnte auf seinem eigenen Server das Casino nicht abschalten."""
        import features
        gid = message.guild.id
        if not rest:
            zeilen = []
            for eintrag in features.CATALOG:
                an = features.is_on_in(gid, eintrag["key"])
                global_aus = not features.is_on(eintrag["key"])
                zeichen = "🟢" if an else "🔴"
                notiz = " _(global aus)_" if global_aus else ""
                zeilen.append(f"{zeichen} `{eintrag['key']}` – {eintrag['label']}{notiz}")
            emb = discord.Embed(
                title="🧩 Funktionen auf diesem Server",
                description="\n".join(zeilen) or "Keine Module geladen.",
                color=0x5865F2)
            emb.set_footer(text=f"{self._bot_name} funktion casino aus")
            await message.channel.send(embed=emb)
            return HANDLED
        if not self._darf(message):
            return ("Funktionen darf nur umschalten, wer hier **Server "
                    "verwalten** darf.")
        key = rest[0].lower().strip(".,!?:")
        schalter = (rest[1].lower().strip(".,!?") if len(rest) > 1 else "")
        if schalter not in _JA and schalter not in _NEIN:
            return (f"An oder aus? `{self._bot_name} funktion {key} aus`")
        an = schalter in _JA
        neu_wert = await features.set_guild(gid, key, an)
        if neu_wert is None:
            moeglich = ", ".join(f"`{e['key']}`" for e in features.CATALOG)
            return f"„{key}“ kenne ich nicht. Es gibt: {moeglich}"
        if an and not features.is_on(key):
            return (f"`{key}` ist **global** abgeschaltet – das hebt ein "
                    f"Server-Schalter nicht auf.")
        return (f"`{key}` ist auf diesem Server jetzt "
                f"**{'an' if neu_wert else 'aus'}**.")

    async def handle(self, message):
        if not self._enabled or getattr(message, "guild", None) is None:
            return None
        cleaned = ai.strip_lead(message.content or "")
        if not cleaned:
            return None
        teile = cleaned.split()
        erstes = teile[0].lower().strip(".,!?") if teile else ""
        if erstes in self._CMDS_FEATURES:
            return await self._funktionen(message, teile[1:])
        if erstes not in self._CMDS:
            return None
        rest = teile[1:]

        if not self._darf(message):
            return ("Die Server-Einstellungen darf nur ändern, wer hier "
                    "**Server verwalten** darf.")

        gid = message.guild.id
        if not rest:
            await message.channel.send(embed=self._embed(message.guild))
            return HANDLED

        key = rest[0].lower().strip(".,!?:")
        defn = _NACH_KEY.get(key)
        if defn is None:
            moeglich = ", ".join(f"`{e.key}`" for e in KATALOG)
            return f"„{key}“ kenne ich nicht. Es gibt: {moeglich}"

        if len(rest) == 1:
            hinweis = f" — {defn.hinweis}" if defn.hinweis else ""
            woher = "eigens gesetzt" if self.eigen(gid, key) else "Standard"
            return (f"**{defn.label}** (`{key}`): "
                    f"**{self._als_text(defn, self.get(gid, key))}** ({woher}){hinweis}")

        wert_roh = " ".join(rest[1:])
        ok, wert, fehler = await self.setzen(gid, key, wert_roh, message.guild)
        if not ok:
            return f"Geht nicht: {fehler}"
        if str(wert_roh).strip().lower() in _STANDARD_WOERTER:
            return (f"**{defn.label}** steht wieder auf dem Standard: "
                    f"**{self._als_text(defn, wert)}**")
        return f"**{defn.label}** ist jetzt **{self._als_text(defn, wert)}**. ✅"


# --- Singleton + Modul-API ---------------------------------------------------
instance = GuildConfig()

setup = instance.setup
is_enabled = instance.is_enabled
get = instance.get
an = instance.an
eigen = instance.eigen
setzen = instance.setzen
darf = instance.darf
loeschen = instance.loeschen
vergessen = instance.vergessen
alle = instance.alle
text = instance.text
handle = instance.handle
