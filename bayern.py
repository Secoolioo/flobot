"""Bayrisch/Oesterreichisch fuer Flo.

- Begruessungen im Dialekt ('servus', 'griaß di', 'pfiat di' ...) -> Flo gruesst
  boarisch zurueck.
- Toggle 'Flo bayrisch [an|aus]' -> ab dann antwortet die KI komplett im Dialekt
  (bot.py gibt is_on() an ai.ask_flo/see_image weiter).

Reine Deko - faellt nie technisch aus.
"""

import logging
import random
import re

import discord

import ai

log = logging.getLogger("dcbot.bayern")

HANDLED = object()


class Bayern:
    # Begruessungen (erstes Wort / erste zwei Woerter).
    _GREET1 = {"servus", "servas", "sers", "seas", "habidere", "pfiadi", "pfiati",
               "griasdi", "griaßdi", "griasgod", "zefix", "griaseich"}
    _GREET2 = {("griaß", "di"), ("griass", "di"), ("griaß", "eich"),
               ("griass", "eich"), ("griaß", "gott"), ("griass", "gott"),
               ("pfiat", "di"), ("pfiad", "di"), ("pfiat", "eich")}

    _HELLO = [
        "Servus! Wia geht's da, oida? 🍺",
        "Griaß di! Host scho a Mass ghobt?",
        "Servas beinand, wos treibst?",
        "Habidere! Alles guad bei dir?",
        "Griaß di Gott, Spezl! 🥨",
        "Seas! Basd scho, dass d' vorbeischaugst.",
        "Servus du Hund, wos gehd?",
    ]
    _BYE = [
        "Pfiat di, Spezl! 👋",
        "Pfiat di Gott, mach's guad!",
        "Servus, hau di iber d'Heisa!",
        "Ba ba, bis boid amoi!",
    ]

    # 'Flo bayrisch [an|aus]'
    _TOGGLE_RE = re.compile(r"^(?:bo?a[iy]risch|boarisch|dialekt)\b\s*(an|ein|on|aus|off|weg)?", re.I)

    # Systemprompt-Zusatz, den ai bei aktivem Dialekt anhaengt.
    DIALECT_PROMPT = (
        " WICHTIG: Antworte AB JETZT komplett auf Bairisch/Boarisch (bayrisch-"
        "oesterreichischer Dialekt). Beispiele: 'i' statt ich, 'ned' statt nicht, "
        "'a'/'oa' statt ein, 'ma' statt man/wir, 'des' statt das, 'wos' statt was, "
        "'no' statt noch, 'oiso' statt also, 'gscheid', 'basd scho', 'oida', 'Spezl', "
        "'freili', 'moanst'. Bleib inhaltlich gleich - nur der Dialekt aendert sich."
    )

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"

        # Server-IDs, in denen die KI gerade boarisch antwortet.
        self._on = set()

    def setup(self):
        self._bot_name = ai.bot_name()
        self._enabled = True
        log.info("Bayrisch-Feature aktiv (Begruessungen + Dialekt-Toggle).")
        return self._enabled

    def is_enabled(self):
        return self._enabled

    def is_on(self, guild_id):
        """True, wenn die KI in diesem Server gerade boarisch antworten soll."""
        return bool(guild_id) and guild_id in self._on

    async def handle(self, message):
        if not self._enabled or message.guild is None:
            return None
        cleaned = ai.strip_lead(message.content or "")
        if not cleaned:
            return None
        words = cleaned.lower().split()
        first = words[0].strip(".,!?") if words else ""
        two = (words[0].strip(".,!?"), words[1].strip(".,!?")) if len(words) >= 2 else None

        # Dialekt an/aus?
        tm = self._TOGGLE_RE.match(cleaned)
        if tm:
            off = (tm.group(1) or "").lower() in ("aus", "off", "weg")
            if off:
                self._on.discard(message.guild.id)
                return "Oiso guad, i red wieda normal. 🙂"
            self._on.add(message.guild.id)
            return "Basd scho! Ab jetzt red i boarisch mit eich, oida. 🥨"

        # Begruessung?
        if first in self._GREET1 or (two is not None and two in self._GREET2):
            # Abschieds-Gruesse als solche erkennen.
            if first.startswith("pfiat") or first.startswith("pfiad") or first in ("pfiadi", "pfiati") \
                    or (two is not None and two[0].startswith("pfiat")):
                return random.choice(self._BYE)
            return random.choice(self._HELLO)

        return None


instance = Bayern()

# Modul-Aliase, damit die bisherige Modul-API weiter funktioniert.
DIALECT_PROMPT = Bayern.DIALECT_PROMPT
setup = instance.setup
is_enabled = instance.is_enabled
is_on = instance.is_on
handle = instance.handle
