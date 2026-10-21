"""Geburtstage: einmal eintragen, Flo denkt dran ('flo geburtstag 12.03.').

GERUEST. Die Schnittstelle unten ist die endgueltige - bot.py ruft sie schon an
den richtigen Stellen, nur tut sie noch nichts: setup() liefert False, jeder
Haken ist ein No-op. Der Teil "Server-Leben" fuellt nur diese Datei.

Wo was aufgerufen wird:
    handle(message)   Handler-Kette, nach umfrage
    tick(client)      bot.alltag_loop, jede Minute (hoechstens
                      bot.ALLTAG_FRIST, dann abgebrochen). Die Sendepause
                      prueft bot.py hier NICHT - selbst admin.is_locked()
                      fragen und den Glueckwunsch danach nachholen

Beim Import passiert nichts.
"""

import logging

import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.geburtstage")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("geburtstag", "geburtstage", "birthday", "bday")


class Geburtstage(FeatureBasis):
    """Geburtstage je Server - noch ohne Inhalt."""

    def __init__(self):
        self._enabled = False

    def setup(self):
        """False, bis der Teil "Server-Leben" das Modul fuellt."""
        return False

    def is_enabled(self):
        return self._enabled

    async def handle(self, message):
        """'geburtstag 12.03.', 'geburtstage' -> Antwort; alles andere None."""
        if not self._enabled:
            return None
        return None

    async def tick(self, client):
        """Ab 9 Uhr gratulieren - auch nachtraeglich, wenn Flo offline war."""
        return None


instance = Geburtstage()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
tick = instance.tick
