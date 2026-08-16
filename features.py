"""Laufzeit-Schalter fuer Flos Funktionen (An/Aus ueber das Web-Panel).

Jedes Feature-Modul wird beim Start geladen (z. B. MUSIC_ENABLED = music.setup()).
Dieses Modul legt ZWEI zusaetzliche Ebenen darueber:

1. GLOBAL - der Besitzer schaltet eine Funktion auf allen Servern aus
   (`is_on(key)`). Das ist der Not-Aus und gewinnt immer.
2. JE SERVER - ein Server-Admin schaltet sie nur bei sich aus
   (`is_on_in(guild_id, key)`). Der Nachbarserver merkt davon nichts.

bot.py fragt im Handler-Durchlauf und in den passiven Hooks ueber
`features.fuer(guild_id)` ab - das ist dieselbe Pruefung, nur schon auf einen
Server festgelegt. Ein Feature, das beim Start gar nicht geladen wurde, laesst
sich nirgends anschalten - dafuer ist ein Neustart noetig (das Panel zeigt es
als 'nicht geladen').

Der Zustand liegt in data/features.json und ueberlebt Neustarts:
    {"disabled": [global aus], "guilds": {"<id>": [auf diesem Server aus]}}
"""

import logging
import os
from functools import partial

from store import JsonStore

log = logging.getLogger("dcbot.features")

# Katalog aller schaltbaren Funktionen (Reihenfolge = Anzeige im Panel).
CATALOG = [
    {"key": "ki",       "label": "KI-Chat",           "emoji": "💬", "desc": "Flo antwortet als KI (mit Kontext, Bildern & Wetter)"},
    {"key": "music",    "label": "Musik",             "emoji": "🎵", "desc": "YouTube/Spotify abspielen, Warteschlange, Lyrics"},
    {"key": "games",    "label": "Spiele",            "emoji": "🎮", "desc": "Quiz, Zahlenraten, Duelle & Zufalls-Events"},
    {"key": "casino",   "label": "Casino",            "emoji": "🎰", "desc": "Blackjack, Roulette, Slots & 10 weitere Spiele"},
    {"key": "economy",  "label": "Level & Coins",     "emoji": "📈", "desc": "XP, Level, Flo Coins, Tages-Shop & Titel"},
    {"key": "floaktie", "label": "FloCorp-Aktie",     "emoji": "💰", "desc": "$FLO handeln + Voice-Dividende"},
    {"key": "lotto",    "label": "Monats-Lotto",      "emoji": "🎟️", "desc": "Monatlicher Millionen-Jackpot"},
    {"key": "giveaway", "label": "Giveaways",         "emoji": "🎉", "desc": "Nutzer verlosen eigene Coins (Flo fragt alles ab)"},
    {"key": "merchant", "label": "Fahrender Händler", "emoji": "🛒", "desc": "Täglicher Händler mit exklusiven Titeln"},
    {"key": "steal",    "label": "Coin-Raub",         "emoji": "🥷", "desc": "flo steal @wer – Coins klauen"},
    {"key": "arbeit",   "label": "Arbeit & Wordle",   "emoji": "🧰", "desc": "flo work – Schichten mit Mini-Spielen, dazu das Wort des Tages"},
    {"key": "handel",   "label": "Handelsbuch",       "emoji": "📒", "desc": "Alle Coin-Transaktionen als Statistik"},
    {"key": "schulden", "label": "Kreide-Tafel",      "emoji": "🧾", "desc": "Merkt sich, wer wem was überwiesen hat (nur Anzeige)"},
    {"key": "luxus",    "label": "Luxus & Thron",     "emoji": "👑", "desc": "Prestige-Shop bis 1 Milliarde & DER THRON"},
    {"key": "terraria", "label": "Terraria-Wiki",     "emoji": "🌳", "desc": "Terraria-Fragen mit echten Wiki-Daten"},
    {"key": "media",    "label": "Bilder",            "emoji": "🎨", "desc": "Bilder generieren, Quote-Memes, Bild-Analyse"},
    {"key": "food",     "label": "Kalorien",          "emoji": "🍕", "desc": "Essensfotos automatisch analysieren"},
    {"key": "words",    "label": "Wörter-Zähler",     "emoji": "📊", "desc": "Zählt & rankt gesagte Wörter"},
    {"key": "voice",    "label": "Voice-Gags",        "emoji": "🔊", "desc": "Soundboard, Text-to-Speech & Join-Sounds"},
    {"key": "chaos",    "label": "Chaos & Fun",       "emoji": "😈", "desc": "Roast, Hype, Reactions, DM-Konter, Bot-Hass"},
    {"key": "mod",      "label": "Moderation",        "emoji": "🛡️", "desc": "Löschen, Warnen, Timeout, Kick, Ban"},
    {"key": "bayern",   "label": "Bayrisch-Modus",    "emoji": "🥨", "desc": "Flo antwortet auf Boarisch"},
    {"key": "profil",   "label": "Profil-Lookup",     "emoji": "🔎", "desc": "flo check @wer – Profilbild in 4096 px, Badges, Namensverlauf"},
]
_KEYS = {f["key"] for f in CATALOG}


class Features:
    """Haelt die ABGESCHALTETEN Feature-Keys - global und je Server."""

    def __init__(self):
        self._store = None
        self._disabled = set()      # global aus (Not-Aus des Besitzers)
        self._per_guild = {}        # guild_id -> set(keys), nur hier aus

    def setup(self):
        self._store = JsonStore("features.json",
                                default={"disabled": [], "guilds": {}})
        self._disabled = set(self._store.data.get("disabled", []) or [])
        self._per_guild = {}
        for gid, keys in (self._store.data.get("guilds", {}) or {}).items():
            # Der Schluessel MUSS eine Zahl sein. Steht dort Text (von Hand
            # editiert), warf int() frueher hier - und weil setup() auf
            # Modulebene laeuft, kam der ganze Bot nicht mehr hoch.
            try:
                gid_zahl = int(gid)
                aus = {k for k in (keys or []) if k in _KEYS}
            except (TypeError, ValueError):
                log.error("features.json: Server-Eintrag %r ist unbrauchbar - "
                          "wird uebersprungen.", gid)
                continue
            if aus:
                self._per_guild[gid_zahl] = aus
        if self._disabled:
            log.info("Laufzeit-Schalter: %d Funktion(en) global aus (%s).",
                     len(self._disabled), ", ".join(sorted(self._disabled)))
        if self._per_guild:
            log.info("Laufzeit-Schalter: %d Server mit eigenen Abschaltungen.",
                     len(self._per_guild))
        return True

    def is_on(self, key):
        """True, solange die Funktion nicht GLOBAL abgeschaltet wurde."""
        return key not in self._disabled

    def is_on_in(self, guild_id, key):
        """True, wenn die Funktion global UND auf diesem Server an ist.

        Ohne Server (DM, guild_id=0) zaehlt nur der globale Schalter."""
        if key in self._disabled:
            return False
        gid = int(guild_id or 0)
        return key not in self._per_guild.get(gid, ())

    def fuer(self, guild_id):
        """Pruef-Funktion 'key -> bool', schon auf EINEN Server festgelegt.

        bot.py baut sich die einmal je Nachricht und reicht sie durch die
        Handler-Kette - so steht an jeder Abfrage derselbe kurze Aufruf wie
        vorher, nur eben serverbezogen."""
        return partial(self.is_on_in, int(guild_id or 0))

    async def set(self, key, on):
        """Schaltet eine Funktion GLOBAL an/aus. Rueckgabe: neuer Zustand."""
        if key not in _KEYS:
            return None
        if on:
            self._disabled.discard(key)
        else:
            self._disabled.add(key)
        await self._speichern()
        log.info("Funktion '%s' global %s (via Panel).", key, "AN" if on else "AUS")
        return bool(on)

    async def set_guild(self, guild_id, key, on):
        """Schaltet eine Funktion NUR auf diesem Server an/aus.

        Der globale Schalter bleibt davon unberuehrt: ist etwas global aus,
        holt es auch ein 'an' hier nicht zurueck."""
        if key not in _KEYS:
            return None
        gid = int(guild_id or 0)
        if not gid:
            return None
        aus = self._per_guild.setdefault(gid, set())
        if on:
            aus.discard(key)
        else:
            aus.add(key)
        if not aus:
            self._per_guild.pop(gid, None)
        await self._speichern()
        log.info("Funktion '%s' auf Server %s %s.", key, gid, "AN" if on else "AUS")
        return bool(on) and self.is_on(key)

    async def vergessen(self, guild_id):
        """Alle Server-Schalter vergessen (Bot wurde dort rausgeworfen)."""
        if self._per_guild.pop(int(guild_id or 0), None) is None:
            return False
        await self._speichern()
        return True

    async def _speichern(self):
        if self._store is None:
            return
        self._store.data["disabled"] = sorted(self._disabled)
        self._store.data["guilds"] = {str(g): sorted(k)
                                      for g, k in self._per_guild.items() if k}
        try:
            await self._store.save()
        except Exception:  # noqa: BLE001
            log.exception("Feature-Zustand konnte nicht gespeichert werden")

    def state(self, loaded, guild_id=0):
        """Baut die Panel-Liste: je Feature key/label/emoji/desc + loaded + on.
        'loaded' ist ein Dict {key: bool} der Start-Flags aus bot.py.

        Mit guild_id zeigt 'on' den Zustand AUF DIESEM SERVER; 'global_on' sagt
        daneben, ob der Besitzer die Funktion ueberhaupt zulaesst - ist die
        global aus, kann ein Server-Admin sie nicht anschalten."""
        gid = int(guild_id or 0)
        out = []
        for f in CATALOG:
            ld = bool(loaded.get(f["key"], False))
            glob = self.is_on(f["key"])
            an = self.is_on_in(gid, f["key"]) if gid else glob
            out.append({**f, "loaded": ld, "on": ld and an, "global_on": glob})
        return out


# --- Singleton + Modul-API ---------------------------------------------------
instance = Features()

setup = instance.setup
is_on = instance.is_on
is_on_in = instance.is_on_in
fuer = instance.fuer
set_feature = instance.set
set_guild = instance.set_guild
vergessen = instance.vergessen
state = instance.state
