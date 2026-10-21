"""Flos Launen: ein Tag hat eine Stimmung, und die faerbt Status und Ton ('flo laune').

GERUEST. Die Schnittstelle unten ist die endgueltige - bot.py und ai.py rufen
sie schon an den richtigen Stellen, nur tut sie noch nichts: setup() liefert
False, jeder Haken ist ein No-op, prompt_zeile() ist leer. Der Teil "Flo lebt"
fuellt nur diese Datei (plus je eine Zeile in casino.record und lotto._draw
fuer jackpot()).

Wo was aufgerufen wird:
    handle(message)              Handler-Kette, nach umfrage
    nachricht(msg, angesprochen) bot.on_message, SYNCHRON, sobald feststeht,
                                 ob Flo angesprochen ist - nie bei Notlagen
                                 (bot.py filtert, das Modul prueft trotzdem)
    tick(client)                 bot.alltag_loop, jede Minute
    jackpot(uid, netto)          casino/lotto, SYNCHRON, nach einem dicken Gewinn
    prompt_zeile(gid) -> str     ai._system_prompt, hinter dem Gedaechtnis und
                                 vor dem Schlusswort - NIE in einer Notlage. Die
                                 Zeile aendert nur den Ton und endet mit dem
                                 Satz, dass Grenze und Notlagen-Regel trotzdem
                                 gelten.
    status_zeile(gid, spruch)    bot.status_loop: Spruch fuer den Status oder
                                 None (dann bleibt der normale Spruch)
    stand(gid) -> str            bot.status_loop: die aktuelle Laune als kurzes
                                 Wort ("" = keine). Aendert sie sich, setzt der
                                 Status sofort neu statt erst nach einer Minute.

Beim Import passiert nichts.
"""

import logging

import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.laune")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("laune", "mood")


class Laune(FeatureBasis):
    """Flos Stimmung je Server - noch ohne Inhalt."""

    def __init__(self):
        self._enabled = False

    def setup(self):
        """False, bis der Teil "Flo lebt" das Modul fuellt."""
        return False

    def is_enabled(self):
        return self._enabled

    async def handle(self, message):
        """'laune' -> wie Flo drauf ist und warum; alles andere None."""
        if not self._enabled:
            return None
        return None

    def nachricht(self, msg, angesprochen):
        """Eine Chatnachricht zaehlen (Spam an Flo, Funkstille ...). Synchron."""
        return None

    async def tick(self, client):
        """Tageslaune wuerfeln, abgelaufene Launen beenden."""
        return None

    def jackpot(self, uid, netto):
        """Jemand hat dick gewonnen - Flo dreht vielleicht durch. Synchron."""
        return None

    def prompt_zeile(self, gid):
        """Die Zeile fuer den System-Prompt ("" = keine)."""
        return ""

    def status_zeile(self, gid, spruch):
        """Spruch fuer den Bot-Status oder None (dann gilt 'spruch')."""
        return None

    def stand(self, gid):
        """Die aktuelle Laune als kurzes Wort - "" = keine besondere."""
        return ""


instance = Laune()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
nachricht = instance.nachricht
tick = instance.tick
jackpot = instance.jackpot
prompt_zeile = instance.prompt_zeile
status_zeile = instance.status_zeile
stand = instance.stand
