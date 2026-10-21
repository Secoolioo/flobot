"""Musik-Wrapped: was lief diesen Monat, und wer ist DJ des Monats ('flo wrapped').

GERUEST. Die Schnittstelle unten ist die endgueltige - music.py und bot.py rufen
sie schon an den richtigen Stellen, nur tut sie noch nichts: setup() liefert
False, und jeder Haken ist ein No-op. Der Musik-Teil fuellt nur diese Datei
(dazu eine Karte in render.py).

Wo was aufgerufen wird:
    handle(message)        Handler-Kette, direkt VOR music.handle
    notieren(gid, track)   music.verlauf_notieren, SYNCHRON, bei jedem neu
                           gestarteten Song - nicht bei wiederhergestellten
                           (nach einem Neustart oder nach dem Parken: die
                           liefen schon einmal)
    tick(client)           bot.alltag_loop, jede Minute (hoechstens
                           bot.ALLTAG_FRIST, dann abgebrochen). Die Sendepause
                           prueft bot.py hier NICHT - selbst admin.is_locked()
                           fragen und das Wrapped danach nachholen

Beim Import passiert nichts.
"""

import logging

import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.musikstats")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("wrapped", "musikwrapped")


class Musikstats(FeatureBasis):
    """Monats-Statistik der Musik je Server - noch ohne Inhalt."""

    def __init__(self):
        self._enabled = False

    def setup(self):
        """False, bis der Musik-Teil das Modul fuellt."""
        return False

    def is_enabled(self):
        return self._enabled

    async def handle(self, message):
        """'wrapped' -> Karte; alles andere None."""
        if not self._enabled:
            return None
        return None

    def notieren(self, gid, track):
        """Einen gestarteten Song zaehlen. Synchron, darf nie werfen."""
        return None

    async def tick(self, client):
        """Am Monatsanfang das Wrapped posten (einmal je Server)."""
        return None


instance = Musikstats()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
notieren = instance.notieren
tick = instance.tick
