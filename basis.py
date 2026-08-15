"""Gemeinsame Basis aller Feature-Module.

Bisher hielt sich JEDES Modul beim Start seine eigene Kopie des Botnamens:

    self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"

Einundzwanzig Mal dieselbe Zeile - und damit war ein eigener Praefix je Server
unmoeglich: der Name stand fest, sobald der Bot hochkam, und der Trigger-Regex
in bot.py wurde einmal beim Import gebaut.

Hier steht er nur noch EINMAL, und zwar als Eigenschaft, die zur LAUFZEIT
nachschaut, welcher Server gerade bedient wird. Die rund 180 Stellen im Code,
die `f"{self._bot_name} pay @wer"` schreiben, mussten dafuer nicht angefasst
werden - sie sind seitdem von allein serverrichtig. Wer den Namen fuer einen
BESTIMMTEN Server braucht (Hintergrund-Loops, DMs), nimmt `self.name_fuer(gid)`.

Ein Test haelt fest, dass kein Modul sich wieder eine eigene Kopie anlegt.
"""

import ai


class FeatureBasis:
    """Was jedes Feature-Modul koennen muss: seinen eigenen Namen kennen."""

    @property
    def _bot_name(self):
        """Wie Flo auf DIESEM Server heisst (in DMs: der Name aus der .env)."""
        return ai.bot_name()

    @_bot_name.setter
    def _bot_name(self, wert):
        """Zuweisungen werden bewusst geschluckt.

        Kein Modul soll den Namen mehr selbst halten - aber ein vergessenes
        `self._bot_name = ...` in einem alten Zweig darf auch nicht mit einem
        AttributeError den Bot-Start sprengen. Der Test
        test_kein_modul_haelt_den_botnamen_selbst haelt die Regel wach."""

    @staticmethod
    def name_fuer(gid):
        """Der Name fuer einen AUSDRUECKLICH genannten Server."""
        return ai.bot_name(gid)
