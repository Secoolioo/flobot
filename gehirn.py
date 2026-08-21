"""Flos Langzeitgedaechtnis: was er ueber Leute und Server weiss.

Bisher reichte Flos Erinnerung genau 12 Nachrichten und 20 Minuten weit
(ai._HIST_MAX/_HIST_TTL). Danach war alles weg - jeden Tag dieselben Fragen,
kein einziger Rueckbezug auf gestern. Hier baut er sich statt dessen ein
Gedaechtnis auf, das ueber Neustarts hinweg haelt.

WIE ES LAEUFT
  1. note_message()  haengt jede oeffentliche Nachricht in einen kleinen Puffer
     je Server. Billig und synchron - reine Listenarbeit.
  2. tick()          gibt den Puffer ab und zwar gesammelt an die KI, die daraus
     kurze, dauerhafte Fakten zieht ("spielt Terraria", "hasst Montage").
  3. kontext_fuer()  gibt die passenden Fakten an den System-Prompt weiter.

WAS BEWUSST NICHT PASSIERT
  - Keine DMs. Nur was oeffentlich im Server steht.
  - Nichts wandert zwischen Servern. Was im einen Server gesagt wurde, weiss
    Flo im anderen nicht.
  - Keine privaten Daten. Die KI wird ausdruecklich darauf verpflichtet, und
    danach filtert _heikel() noch einmal hart nach (Mail, Telefon, IBAN,
    Adresse, Links). Ein Bot, der heimlich Dossiers anlegt, waere genau das
    Gegenteil von einem Spass-Feature.
  - Nichts Unsichtbares. Jeder kann sehen, was Flo ueber ihn weiss, und es
    loeschen:  'Flo gedaechtnis'  und  'Flo vergiss mich'.

Persistenz: data/gehirn.json ueber JsonStore, Speichern debounced.
"""

import asyncio
import logging
import re
import time

import discord

import ai
from basis import FeatureBasis
from store import JsonStore

log = logging.getLogger("dcbot.gehirn")

TAG = 86400.0

# --- Grenzen ----------------------------------------------------------------
# Ein Gedaechtnis ohne Deckel waechst, bis die Datei nicht mehr zu laden ist -
# und ein System-Prompt mit 300 Fakten kostet bei JEDER Antwort Token.
MAX_FAKTEN_PERSON = 25
MAX_FAKTEN_SERVER = 20
FAKT_MAX_LAENGE = 120
# So viele Nachrichten sammeln, bevor die KI daraus Fakten zieht. Kleiner waere
# teurer (mehr Aufrufe) und schlechter (weniger Zusammenhang).
PUFFER_ZIEL = 40
PUFFER_MAX = 120
# Kuerzere Nachrichten sagen ueber einen Menschen praktisch nie etwas aus.
MIN_LAENGE = 12
# Was 120 Tage lang niemand mehr bestaetigt hat, fliegt raus, sobald Platz
# gebraucht wird. Ohne das steht in drei Jahren noch drin, wer 2026 mal Lust
# auf Pizza hatte.
VERGESSEN_NACH = 120 * TAG

FARBE = 0x8E7CC3

# Solche Zeichenfolgen speichert Flo NIE, egal was die KI vorschlaegt.
_HEIKEL = (
    re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I),          # Mail
    re.compile(r"\+?\d[\d\s/()-]{7,}\d"),                      # Telefon
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,}\b"),             # IBAN
    re.compile(r"https?://|www\.", re.I),                      # Links
    re.compile(r"\b\d{5}\s+[A-Z][a-z]+", ),                    # PLZ + Ort
    # KEIN \b vor 'strasse': in "Hauptstrasse" steht kein Wortanfang davor -
    # genau die zusammengesetzten Formen sind aber die echten Adressen.
    re.compile(r"(stra(?:ss|ß)e\b|\bstr\.|\b(passwort|password|kreditkarte|"
               r"personalausweis|geburtstag am)\b)", re.I),
)

_CMD_ZEIGEN = ("gedaechtnis", "gedächtnis", "erinnerung", "was weisst du",
               "was weißt du")
_CMD_VERGESSEN = ("vergiss", "vergessen")

_EXTRAKT_SYSTEM = (
    "Du ziehst aus einem Discord-Chat DAUERHAFTE Fakten ueber die Leute heraus. "
    "Antworte AUSSCHLIESSLICH mit Zeilen der Form 'Name: Fakt' - eine je Zeile, "
    "hoechstens 12 Zeilen, jede Zeile hoechstens 100 Zeichen, auf Deutsch. "
    "Ein Fakt ist etwas, das morgen noch stimmt: Vorlieben, Spiele, Hobbys, "
    "Eigenarten, laufende Gags, Rivalitaeten, Koennen und Nicht-Koennen. "
    "KEINE Tagesereignisse ('hat gerade gegessen'), keine Wiederholung des "
    "Chats, keine Vermutungen. "
    "STRENG VERBOTEN: echte Namen, Adressen, Telefonnummern, Mailadressen, "
    "Arbeitgeber, Krankheiten, Geld, Passwoerter, Links oder irgendetwas, das "
    "jemandem schaden koennte - so etwas laesst du einfach weg. "
    "Gilt ein Fakt fuer den ganzen Server statt fuer eine Person, schreib "
    "'SERVER: Fakt'. Findest du nichts Brauchbares, antworte mit NICHTS."
)


class Gehirn(FeatureBasis):
    """Haelt fest, was Flo ueber Leute und Server gelernt hat."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._dirty = False
        self._save_task = None
        self._laeuft = set()        # Server, deren Auswertung gerade laeuft

    # --- Aufbau -------------------------------------------------------------
    def setup(self):
        """Aktiv nur mit KI - ohne sie kann niemand Fakten herausziehen."""
        if not ai.is_enabled():
            log.info("Gedaechtnis aus: KI ist nicht aktiv.")
            return False
        self._store = JsonStore("gehirn.json", default={"guilds": {}})
        self._enabled = True
        leute = sum(len((g or {}).get("leute") or {})
                    for g in (self._store.data.get("guilds") or {}).values())
        log.info("Gedaechtnis aktiv (%d Server, %d Leute im Kopf).",
                 len(self._store.data.get("guilds") or {}), leute)
        return True

    def is_enabled(self):
        return self._enabled and self._store is not None

    # --- Speichern (debounced wie beim Wort-Zaehler) ------------------------
    def _merken(self):
        self._dirty = True
        if self._save_task is None or self._save_task.done():
            try:
                self._save_task = asyncio.get_running_loop().create_task(
                    self._spaeter_speichern())
            except RuntimeError:
                pass        # kein Loop (Tests) - der Stand steht trotzdem im RAM

    async def _spaeter_speichern(self):
        await asyncio.sleep(20)
        await self.flush_now()

    async def flush_now(self):
        if self._store is not None and self._dirty:
            self._dirty = False
            await self._store.save()

    # --- Datenzugriff -------------------------------------------------------
    def _guild(self, gid):
        alle = self._store.data.setdefault("guilds", {})
        if not isinstance(alle, dict):
            alle = self._store.data["guilds"] = {}
        eintrag = alle.setdefault(str(int(gid)), {})
        eintrag.setdefault("leute", {})
        eintrag.setdefault("server", [])
        eintrag.setdefault("puffer", [])
        return eintrag

    def _person(self, gid, uid):
        leute = self._guild(gid)["leute"]
        eintrag = leute.setdefault(str(int(uid)), {})
        eintrag.setdefault("fakten", [])
        return eintrag

    # --- Schritt 1: mithoeren ----------------------------------------------
    def note_message(self, message):
        """Eine oeffentliche Nachricht in den Puffer. Billig und synchron.

        Bewusst NICHT jede Nachricht: zu kurze sagen nichts aus, Befehle an Flo
        sind Bedienung und kein Gespraech, und Bots merkt sich niemand."""
        if not self.is_enabled():
            return
        guild = getattr(message, "guild", None)
        if guild is None:                       # DMs bleiben aussen vor
            return
        autor = getattr(message, "author", None)
        if autor is None or getattr(autor, "bot", False):
            return
        text = (getattr(message, "content", "") or "").strip()
        if len(text) < MIN_LAENGE:
            return
        if ai.trigger_re().search(text):        # Befehl an Flo, kein Gespraech
            return
        puffer = self._guild(guild.id)["puffer"]
        puffer.append({
            "wer": int(getattr(autor, "id", 0) or 0),
            "name": str(getattr(autor, "display_name", "") or "?")[:32],
            "text": text[:280],
        })
        del puffer[:-PUFFER_MAX]
        self._merken()

    # --- Schritt 2: verstehen ----------------------------------------------
    async def tick(self, guilds):
        """Wertet volle Puffer aus. bot.py ruft das im Takt.

        Ein Server nach dem anderen und nie zweimal gleichzeitig derselbe -
        sonst wuerden zwei Auswertungen denselben Puffer lesen und dieselben
        Fakten doppelt anlegen."""
        if not self.is_enabled():
            return 0
        neue = 0
        for guild in guilds or []:
            gid = getattr(guild, "id", 0)
            if not gid or gid in self._laeuft:
                continue
            if len(self._guild(gid)["puffer"]) < PUFFER_ZIEL:
                continue
            self._laeuft.add(gid)
            try:
                neue += await self._auswerten(gid)
            except Exception:  # noqa: BLE001 - ein Server darf den Rest nicht kippen
                log.exception("Gedaechtnis: Auswertung fuer %s gescheitert", gid)
            finally:
                self._laeuft.discard(gid)
        if neue:
            self._merken()
        return neue

    async def _auswerten(self, gid):
        """Puffer -> Fakten. Gibt die Zahl der neuen Fakten zurueck."""
        eintrag = self._guild(gid)
        puffer = eintrag["puffer"]
        if not puffer:
            return 0
        # Puffer SOFORT leeren: geht der KI-Aufruf schief, sind die Nachrichten
        # zwar verloren - aber es kommen laufend neue. Andersherum wuerde ein
        # dauerhaft scheiternder Aufruf denselben Puffer ewig neu schicken.
        gespraech = list(puffer)
        del puffer[:]
        namen = {}
        zeilen = []
        for m in gespraech:
            namen.setdefault(m["name"], m["wer"])
            zeilen.append(f"{m['name']}: {m['text']}")
        antwort = await ai.generate(
            "Chat:\n" + "\n".join(zeilen[-PUFFER_MAX:]),
            system=_EXTRAKT_SYSTEM, temperature=0.3, max_tokens=400)
        if not antwort:
            return 0
        neue = 0
        for zeile in antwort.splitlines():
            wer, _, fakt = zeile.partition(":")
            wer, fakt = wer.strip(), fakt.strip(" -*·").strip()
            if not fakt or len(fakt) < 4:
                continue
            if self._heikel(fakt):
                log.info("Gedaechtnis: Fakt verworfen (sieht privat aus).")
                continue
            fakt = fakt[:FAKT_MAX_LAENGE]
            if wer.upper() == "SERVER":
                if self._merge(eintrag["server"], fakt, MAX_FAKTEN_SERVER):
                    neue += 1
                continue
            uid = namen.get(wer)
            if uid is None:
                continue
            person = self._person(gid, uid)
            person["name"] = wer[:32]
            if self._merge(person["fakten"], fakt, MAX_FAKTEN_PERSON):
                neue += 1
        if neue:
            log.info("Gedaechtnis: %d neue Fakten auf Server %s.", neue, gid)
        return neue

    @staticmethod
    def _heikel(text):
        """Sieht der Fakt nach privaten Daten aus? Dann NICHT speichern.

        Der KI ist das zwar verboten, aber eine Anweisung ist keine Zusicherung -
        und einmal gespeichert steht es in der Datei."""
        return any(p.search(text) for p in _HEIKEL)

    @staticmethod
    def _norm(text):
        """Vergleichsform eines Fakts: klein, ohne Satzzeichen, EIN Leerzeichen.

        Das Zusammenziehen der Leerzeichen ist nicht Kosmetik: ohne das galten
        'spielt Terraria' und 'Spielt  Terraria!' als zwei verschiedene Fakten
        und Flo haette denselben Satz doppelt im Kopf."""
        sauber = re.sub(r"[^a-z0-9äöüß ]+", "", (text or "").lower())
        return re.sub(r"\s+", " ", sauber).strip()

    @classmethod
    def _merge(cls, liste, fakt, deckel):
        """Fakt einsortieren. Gibt True zurueck, wenn er WIRKLICH neu war.

        Kennt Flo etwas schon, wird es nur bestaetigt (Zeitstempel neu, Zaehler
        hoch) - dadurch ueberlebt Bestaetigtes das Vergessen, Einmaliges nicht."""
        neu_norm = cls._norm(fakt)
        for eintrag in liste:
            if cls._norm(eintrag.get("t")) == neu_norm:
                eintrag["wann"] = time.time()
                eintrag["oft"] = int(eintrag.get("oft") or 1) + 1
                return False
        liste.append({"t": fakt, "wann": time.time(), "oft": 1})
        cls._aufraeumen(liste, deckel)
        return True

    @staticmethod
    def _aufraeumen(liste, deckel):
        """Ueber dem Deckel fliegt raus, was am laengsten niemand bestaetigt hat.

        Sortiert nach 'oft' und dann nach Alter: ein Fakt, den Flo dreimal
        gehoert hat, ist echter als einer aus einem Nebensatz."""
        jetzt = time.time()
        veraltet = [e for e in liste
                    if jetzt - float(e.get("wann") or 0) > VERGESSEN_NACH]
        if len(liste) > deckel and veraltet:
            for e in veraltet:
                liste.remove(e)
        if len(liste) > deckel:
            liste.sort(key=lambda e: (int(e.get("oft") or 1),
                                      float(e.get("wann") or 0)))
            del liste[:len(liste) - deckel]

    # --- Schritt 3: benutzen ------------------------------------------------
    def kontext_fuer(self, gid, uid=None, max_zeichen=700):
        """Der Gedaechtnis-Block fuer den System-Prompt - oder "".

        Absichtlich knapp: das haengt an JEDER Antwort und kostet jedes Mal
        Token. Die zuletzt bestaetigten Fakten zuerst."""
        if not self.is_enabled() or not gid:
            return ""
        eintrag = self._guild(gid)
        teile = []
        if uid:
            person = (eintrag["leute"].get(str(int(uid))) or {})
            fakten = self._sortiert(person.get("fakten") or [])
            if fakten:
                teile.append("Was du ueber ihn weisst: " + "; ".join(fakten))
        server = self._sortiert(eintrag.get("server") or [])
        if server:
            teile.append("Was auf diesem Server gilt: " + "; ".join(server))
        if not teile:
            return ""
        text = (" Du kennst die Leute hier laengst. " + " ".join(teile)
                + " Nutze das beilaeufig - als jemand, der dabei war, nicht als "
                  "Karteikasten. Zaehl es NIE einfach auf.")
        return text[:max_zeichen]

    @staticmethod
    def _sortiert(liste):
        gut = [e for e in liste if isinstance(e, dict) and e.get("t")]
        gut.sort(key=lambda e: float(e.get("wann") or 0), reverse=True)
        return [str(e["t"]) for e in gut]

    def weiss_ueber(self, gid, uid):
        """Alle Fakten zu einer Person - fuer die Anzeige."""
        if not self.is_enabled():
            return []
        return self._sortiert((self._person(gid, uid).get("fakten") or []))

    def vergiss(self, gid, uid):
        """Loescht alles ueber eine Person. Gibt zurueck, wie viel weg ist."""
        if not self.is_enabled():
            return 0
        leute = self._guild(gid)["leute"]
        weg = len((leute.get(str(int(uid))) or {}).get("fakten") or [])
        leute.pop(str(int(uid)), None)
        # Auch aus dem noch nicht ausgewerteten Puffer, sonst kommt die Person
        # beim naechsten Tick sofort wieder herein.
        puffer = self._guild(gid)["puffer"]
        self._guild(gid)["puffer"] = [m for m in puffer if m.get("wer") != int(uid)]
        if weg:
            self._merken()
        return weg

    # --- Befehle ------------------------------------------------------------
    async def handle(self, message):
        """'Flo gedaechtnis' und 'Flo vergiss mich'. None | Text | Embed."""
        if not self.is_enabled():
            return None
        gid = getattr(getattr(message, "guild", None), "id", 0)
        if not gid:
            return None
        text = ai.strip_lead(getattr(message, "content", "") or "").strip().lower()
        if not text:
            return None
        erstes = text.split()[0].strip(".,!?")
        if erstes in _CMD_VERGESSEN:
            return self._vergessen(message, gid, text)
        # 'was weisst du ueber mich' ist mehrteilig - deshalb startswith.
        if erstes in _CMD_ZEIGEN or any(text.startswith(w) for w in _CMD_ZEIGEN):
            return self._zeigen(message, gid)
        return None

    def _zeigen(self, message, gid):
        uid = message.author.id
        fakten = self.weiss_ueber(gid, uid)
        server = self._sortiert(self._guild(gid).get("server") or [])
        emb = discord.Embed(
            title="🧠 Was ich ueber dich weiss",
            description=("\n".join(f"• {f}" for f in fakten) if fakten else
                         "Noch gar nichts. Red halt mal was."),
            color=FARBE)
        if server:
            emb.add_field(name="Ueber diesen Server",
                          value="\n".join(f"• {f}" for f in server[:8]),
                          inline=False)
        emb.set_footer(text=f"{self._bot_name} vergiss mich · loescht alles davon")
        return emb

    def _vergessen(self, message, gid, text):
        ziel = message.author
        fremd = [m for m in (getattr(message, "mentions", None) or [])
                 if not getattr(m, "bot", False)]
        if fremd and fremd[0].id != message.author.id:
            # Fremde Erinnerungen loescht nur, wer den Server verwaltet.
            rechte = getattr(getattr(message.author, "guild_permissions", None),
                             "manage_guild", False)
            if not rechte:
                return ("Fremde Erinnerungen loescht hier nur die "
                        "Server-Verwaltung. Dein eigenes Zeug schon: "
                        f"`{self._bot_name} vergiss mich`.")
            ziel = fremd[0]
        weg = self.vergiss(gid, ziel.id)
        wen = "dich" if ziel.id == message.author.id else f"**{ziel.display_name}**"
        if not weg:
            return f"Ueber {wen} weiss ich sowieso nichts."
        return (f"Vergessen. {weg} Sache(n) ueber {wen} sind weg – "
                f"und der Puffer gleich mit.")


instance = Gehirn()

# Modul-Aliase: bot.py und Tests rufen die gewohnten Modul-Funktionen.
setup = instance.setup
is_enabled = instance.is_enabled
note_message = instance.note_message
tick = instance.tick
kontext_fuer = instance.kontext_fuer
weiss_ueber = instance.weiss_ueber
vergiss = instance.vergiss
handle = instance.handle
flush_now = instance.flush_now
