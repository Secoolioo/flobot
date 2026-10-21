"""Er haelt nach: wer "morgen hab ich Pruefung" schreibt, wird hinterher gefragt.

GERUEST. Die Schnittstelle unten ist die endgueltige - bot.py ruft sie schon an
den richtigen Stellen, nur tut sie noch nichts: setup() liefert False, jeder
Haken ist ein No-op. Der Teil "Flo lebt" fuellt nur diese Datei (und gehirn.py
fuer 'vergiss mich' und das Gedaechtnis).

Wo was aufgerufen wird:
    handle(message)              Handler-Kette, nach umfrage
                                 ('flo nachhaken aus|an' je Person)
    note_message(msg)            bot.on_message, SYNCHRON, direkt nach dem
                                 Gedaechtnis-Haken - nie bei Notlagen
    vielleicht_nachfragen(msg)   bot.on_message, im Hintergrund, fuer jede
                                 Nachricht, die NICHT an Flo geht - nie bei
                                 Notlagen und nie in der Sendepause (beides
                                 prueft bot.py). True = Flo hat nachgefragt.
    tick(client)                 bot.alltag_loop, jede Minute (hoechstens
                                 bot.ALLTAG_FRIST, dann abgebrochen - die
                                 gebuendelten KI-Aufrufe brauchen eine Frist,
                                 die darunter liegt). Die Sendepause prueft
                                 bot.py hier NICHT
    vergiss(gid, uid) -> int     gehirn 'vergiss mich': wie viel weg ist
    zeilen_fuer(gid, uid)        gehirn 'gedaechtnis': worauf Flo noch wartet

Beim Import passiert nichts.
"""

import logging

import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.nachhaken")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("nachhaken",)


class Nachhaken(FeatureBasis):
    """Offene Ankuendigungen je Server - noch ohne Inhalt."""

    def __init__(self):
        self._enabled = False

    def setup(self):
        """False, bis der Teil "Flo lebt" das Modul fuellt."""
        return False

    def is_enabled(self):
        return self._enabled

    async def handle(self, message):
        """'nachhaken aus|an' -> Bestaetigung; alles andere None."""
        if not self._enabled:
            return None
        return None

    def note_message(self, msg):
        """Billige Vorpruefung, ob hier etwas angekuendigt wird. Synchron."""
        return None

    async def vielleicht_nachfragen(self, msg):
        """Ist bei dieser Person etwas faellig? Dann hier nachfragen."""
        return False

    async def tick(self, client):
        """Gesammelte Kandidaten auswerten, Abgelaufenes wegwerfen."""
        return None

    def vergiss(self, gid, uid):
        """Alles zu dieser Person auf diesem Server loeschen. Wie viel war es?"""
        return 0

    def zeilen_fuer(self, gid, uid):
        """Worauf Flo bei dieser Person noch wartet (fuer 'gedaechtnis')."""
        return []


instance = Nachhaken()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
note_message = instance.note_message
vielleicht_nachfragen = instance.vielleicht_nachfragen
tick = instance.tick
vergiss = instance.vergiss
zeilen_fuer = instance.zeilen_fuer
