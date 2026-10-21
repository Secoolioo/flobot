"""Die Warteschlange, die einen Neustart ueberlebt (musikzustand.json).

GERUEST. Die Schnittstelle unten ist die endgueltige - music.py und bot.py rufen
sie schon an den richtigen Stellen, nur tut sie noch nichts: setup() liefert
False, und jeder Haken ist ein No-op. Der Musik-Teil fuellt nur diese Datei.

Wo was aufgerufen wird:
    merken(player)          music.py, SYNCHRON: nach jedem erfolgreichen
                            start(), bei Pause/Weiter, beim Einreihen, am Ende
                            jedes Songwechsels, beim Verschieben in der
                            Warteschlange und wenn der Watchdog sie parkt
    vergessen(gid)          music.py, GuildPlayer.disconnect() ('flo stop')
    takt()                  bot.py, im Voice-Watchdog-Takt (alle 15 s)
    sichern()               bot.py, beim Herunterfahren vor store.alle_sichern
    wiederherstellen(client) bot.py, einmal je Prozess aus on_ready

merken() und vergessen() laufen mitten in der Wiedergabe - sie duerfen nie
werfen und nie warten. Beim Import passiert nichts.
"""

import logging

from basis import FeatureBasis

log = logging.getLogger("dcbot.musikzustand")


class Musikzustand(FeatureBasis):
    """Schnappschuss der Player je Server - noch ohne Inhalt."""

    def __init__(self):
        self._enabled = False

    def setup(self):
        """False, bis der Musik-Teil das Modul fuellt."""
        return False

    def is_enabled(self):
        return self._enabled

    def merken(self, player):
        """Stand dieses Players merken (gesammelt speichern). Synchron."""
        return None

    def vergessen(self, gid):
        """Der Server hat bewusst gestoppt - kein Stand mehr fuer ihn."""
        return None

    async def takt(self):
        """Regelmaessig die Positionen nachziehen (deckt Absturz und kill -9)."""
        return None

    async def sichern(self):
        """Beim Herunterfahren: Positionen festhalten und sofort schreiben."""
        return None

    async def wiederherstellen(self, client):
        """Nach dem Start: zurueck in die Kanaele und weiterspielen."""
        return None


instance = Musikzustand()

setup = instance.setup
is_enabled = instance.is_enabled
merken = instance.merken
vergessen = instance.vergessen
takt = instance.takt
sichern = instance.sichern
wiederherstellen = instance.wiederherstellen
