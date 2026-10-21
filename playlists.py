"""Playlists je Server: 'flo playlist neu zocken', Songs reinkippen, 'fertig'.

GERUEST. Die Schnittstelle unten ist die endgueltige - bot.py ruft sie schon an
den richtigen Stellen, nur tut sie noch nichts: setup() liefert False, also
bleibt das Feature aus, und jeder Haken gibt "nicht zustaendig" zurueck. Der
Playlist-Teil fuellt nur noch diese Datei (dazu musikquellen.py und die
handle-Region in music.py) - bot.py muss dafuer niemand mehr anfassen.

Wo bot.py was aufruft:
    handle(message)              Handler-Kette, direkt VOR music.handle
    on_message_passive(message)  passiver Haken direkt nach dem Giveaway-
                                 Assistenten, vor der Ansprache-Pruefung:
                                 True = die Nachricht gehoerte dem Assistenten
    tick(client)                 im Voice-Watchdog-Takt (alle 15 s)
    offene_sichern()             beim Herunterfahren, vor store.alle_sichern
    DYNAMISCHE_KNOEPFE           werden in setup_hook angemeldet
                                 (bot.DYNAMISCHE_MODULE)

Beim Import passiert nichts - kein Speicher, kein Netz.
"""

import logging

import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.playlists")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("playlist", "playlists", "wiedergabeliste")

#: Die Knoepfe der Playlist-Karte (Abspielen, Gemischt, Dazu, Loeschen), die
#: einen Neustart ueberleben. Noch keine.
DYNAMISCHE_KNOEPFE = ()


class Playlists(FeatureBasis):
    """Gespeicherte Playlists und ihr Assistent - noch ohne Inhalt."""

    def __init__(self):
        self._enabled = False

    def setup(self):
        """False, bis der Playlist-Teil das Modul fuellt."""
        return False

    def is_enabled(self):
        return self._enabled

    async def handle(self, message):
        """'playlist ...' -> Antwort; alles andere None (die Kette geht weiter)."""
        if not self._enabled:
            return None
        return None

    async def on_message_passive(self, message):
        """Antwort an einen laufenden Assistenten? True = hier verarbeitet, die
        Nachricht geht nicht weiter (auch nicht an die Musik)."""
        return False

    async def tick(self, client):
        """Abgelaufene Assistenten speichern oder wegwerfen, Status nachziehen."""
        return None

    async def offene_sichern(self):
        """Beim Herunterfahren: Assistenten mit Songs noch speichern."""
        return None


instance = Playlists()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
on_message_passive = instance.on_message_passive
tick = instance.tick
offene_sichern = instance.offene_sichern
