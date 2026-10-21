"""Zitat der Woche: Rechtsklick -> "Als Zitat speichern", sonntags wird abgestimmt.

GERUEST. Die Schnittstelle unten ist die endgueltige - bot.py ruft sie schon an
den richtigen Stellen, nur tut sie noch nichts: setup() liefert False, jeder
Haken ist ein No-op. Der Teil "Server-Leben" fuellt nur diese Datei.

Wo was aufgerufen wird:
    handle(message)                 Handler-Kette, nach umfrage und VOR media -
                                    sonst macht media aus 'flo zitat der woche'
                                    ein Quote-Meme
    rechtsklick(interaction, msg)   Apps-Menue "Als Zitat speichern", nachdem
                                    bot._rk_darf Server, Sendepause und Schalter
                                    geprueft hat
    ist_eigene_umfrage(msg) -> bool bot.on_message bei einem Umfrage-Ergebnis
                                    von Flo: gehoert es zur Zitat-Abstimmung?
    ergebnis(msg)                   ... dann landet es hier statt bei umfrage.
                                    Die Sendepause prueft bot.py hier NICHT
                                    (anders als bei normalen Umfragen, deren
                                    Ergebnis dann verfaellt) - der Sieger darf
                                    nicht verloren gehen: in der Sendepause
                                    nichts posten, tick() holt es nach
    tick(client)                    bot.alltag_loop, jede Minute (hoechstens
                                    bot.ALLTAG_FRIST, dann abgebrochen) - auch
                                    hier selbst admin.is_locked() fragen

Beim Import passiert nichts.

'zitate' ist ab dem Fundament ein eigener Befehl (cmdnorm.KNOWN). Vorher machte
die Tippfehler-Korrektur daraus 'zitat' und damit media's Quote-Meme; bis
dieses Modul gefuellt ist, faellt 'flo zitate' zur KI durch. Gewollt - das
Wort gehoert dem Zitat der Woche.
"""

import logging

import discord

import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.zitate")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
# 'zitat' allein gehoert weiter media (Quote-Meme) - zitate nimmt davon nur
# die eigenen Formen ('zitat der woche', 'zitat weg 3', 'zitat speichern').
_CMDS = ("zitate", "zitat")

#: Knoepfe, die einen Neustart ueberleben. Noch keine.
DYNAMISCHE_KNOEPFE = ()


class Zitate(FeatureBasis):
    """Zitat-Kandidaten und die Sonntags-Abstimmung - noch ohne Inhalt."""

    def __init__(self):
        self._enabled = False

    def setup(self):
        """False, bis der Teil "Server-Leben" das Modul fuellt."""
        return False

    def is_enabled(self):
        return self._enabled

    async def handle(self, message):
        """'zitate', 'zitat der woche', 'zitat weg 3' -> Antwort; sonst None."""
        if not self._enabled:
            return None
        return None

    async def rechtsklick(self, interaction, nachricht):
        """Eine Nachricht als Kandidaten speichern.

        Eine Interaktion, auf die keiner antwortet, zeigt Discord als
        "fehlgeschlagen" - deshalb antwortet auch das Geruest."""
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Zitate gibt's hier noch nicht. Merk's dir halt selber.",
                    ephemeral=True)
        except discord.HTTPException:
            pass

    def ist_eigene_umfrage(self, msg):
        """Ist dieses Umfrage-Ergebnis die Zitat-Abstimmung?"""
        return False

    async def ergebnis(self, msg):
        """Die Zitat-Abstimmung ist vorbei -> Sieger kueren."""
        return False

    async def tick(self, client):
        """Sonntags die Abstimmung starten, verpasste Ergebnisse nachholen."""
        return None


instance = Zitate()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
rechtsklick = instance.rechtsklick
ist_eigene_umfrage = instance.ist_eigene_umfrage
ergebnis = instance.ergebnis
tick = instance.tick
