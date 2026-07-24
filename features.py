"""Laufzeit-Schalter fuer Flos Funktionen (An/Aus ueber das Web-Panel).

Jedes Feature-Modul wird beim Start geladen (z. B. MUSIC_ENABLED = music.setup()).
Dieses Modul legt EINE zusaetzliche Ebene darueber: der Besitzer kann einzelne
Funktionen zur Laufzeit AUS- und wieder ANschalten, ohne den Bot neu zu starten.

'is_on(key)' ist True, solange die Funktion nicht aktiv abgeschaltet wurde. bot.py
fragt das im Handler-Durchlauf und in den passiven Hooks ab
(z. B. `MUSIC_ENABLED and features.is_on("music")`). Ein Feature, das beim Start
gar nicht geladen wurde, laesst sich nicht per Schalter aktivieren - dafuer ist
ein Neustart noetig (das Panel zeigt es als 'nicht geladen').

Der Zustand (welche Keys AUS sind) liegt in data/features.json und ueberlebt
Neustarts.
"""

import logging
import os

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
    {"key": "merchant", "label": "Fahrender Händler", "emoji": "🛒", "desc": "Täglicher Händler mit exklusiven Titeln"},
    {"key": "steal",    "label": "Coin-Raub",         "emoji": "🥷", "desc": "flo steal @wer – Coins klauen"},
    {"key": "handel",   "label": "Handelsbuch",       "emoji": "📒", "desc": "Alle Coin-Transaktionen als Statistik"},
    {"key": "luxus",    "label": "Luxus & Thron",     "emoji": "👑", "desc": "Prestige-Shop bis 1 Milliarde & DER THRON"},
    {"key": "terraria", "label": "Terraria-Wiki",     "emoji": "🌳", "desc": "Terraria-Fragen mit echten Wiki-Daten"},
    {"key": "media",    "label": "Bilder",            "emoji": "🎨", "desc": "Bilder generieren, Quote-Memes, Bild-Analyse"},
    {"key": "food",     "label": "Kalorien",          "emoji": "🍕", "desc": "Essensfotos automatisch analysieren"},
    {"key": "words",    "label": "Wörter-Zähler",     "emoji": "📊", "desc": "Zählt & rankt gesagte Wörter"},
    {"key": "voice",    "label": "Voice-Gags",        "emoji": "🔊", "desc": "Soundboard, Text-to-Speech & Join-Sounds"},
    {"key": "chaos",    "label": "Chaos & Fun",       "emoji": "😈", "desc": "Roast, Hype, Reactions, DM-Konter, Bot-Hass"},
    {"key": "mod",      "label": "Moderation",        "emoji": "🛡️", "desc": "Löschen, Warnen, Timeout, Kick, Ban"},
    {"key": "bayern",   "label": "Bayrisch-Modus",    "emoji": "🥨", "desc": "Flo antwortet auf Boarisch"},
]
_KEYS = {f["key"] for f in CATALOG}


class Features:
    """Haelt die Menge der ABGESCHALTETEN Feature-Keys (persistent)."""

    def __init__(self):
        self._store = None
        self._disabled = set()

    def setup(self):
        self._store = JsonStore("features.json", default={"disabled": []})
        self._disabled = set(self._store.data.get("disabled", []) or [])
        if self._disabled:
            log.info("Laufzeit-Schalter: %d Funktion(en) aus (%s).",
                     len(self._disabled), ", ".join(sorted(self._disabled)))
        return True

    def is_on(self, key):
        """True, solange die Funktion nicht aktiv abgeschaltet wurde."""
        return key not in self._disabled

    async def set(self, key, on):
        """Schaltet eine Funktion an/aus und speichert. Rueckgabe: neuer Zustand."""
        if key not in _KEYS:
            return None
        if on:
            self._disabled.discard(key)
        else:
            self._disabled.add(key)
        if self._store is not None:
            self._store.data["disabled"] = sorted(self._disabled)
            try:
                await self._store.save()
            except Exception:  # noqa: BLE001
                log.exception("Feature-Zustand konnte nicht gespeichert werden")
        log.info("Funktion '%s' %s (via Panel).", key, "AN" if on else "AUS")
        return bool(on)

    def state(self, loaded):
        """Baut die Panel-Liste: je Feature key/label/emoji/desc + loaded + on.
        'loaded' ist ein Dict {key: bool} der Start-Flags aus bot.py."""
        out = []
        for f in CATALOG:
            ld = bool(loaded.get(f["key"], False))
            out.append({**f, "loaded": ld, "on": ld and self.is_on(f["key"])})
        return out


# --- Singleton + Modul-API ---------------------------------------------------
instance = Features()

setup = instance.setup
is_on = instance.is_on
set_feature = instance.set
state = instance.state
