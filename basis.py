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

import logging
import re

import ai
import laufzeit

log = logging.getLogger("dcbot.basis")

# Eine getippte Erwaehnung sieht im Text so aus. Alles andere in
# message.mentions steht dort, ohne dass jemand es geschrieben hat.
_ERWAEHNUNG_RE = re.compile(r"<@!?(\d+)>")


def echte_erwaehnungen(message):
    """Die Erwaehnungen, die WIRKLICH im Text stehen - in Text-Reihenfolge.

    message.mentions ist unsortiert UND enthaelt bei einer Antwort-mit-Ping den
    Autor der beantworteten Nachricht, obwohl niemand ihn getippt hat. Wer die
    Liste roh nimmt, trifft den Falschen:

        (Antwort auf Bobs Nachricht) "Flo ban spam"   -> bannt Bob
        (Antwort auf Bobs Nachricht) "Flo klau"       -> beklaut Bob

    Discord haengt den beantworteten Autor genau dann an, wenn die Antwort ihn
    anpingt - und das ist die Voreinstellung im Client. Beides ohne ein
    einziges getipptes @.

    Deshalb wird hier gegen den geschriebenen Text abgeglichen. Bots und - falls
    'ohne' gesetzt ist - bestimmte IDs fliegen raus."""
    nach_id = {}
    for benutzer in (getattr(message, "mentions", None) or []):
        uid = getattr(benutzer, "id", None)
        if uid is not None:
            nach_id[int(uid)] = benutzer
    raus, gesehen = [], set()
    for token in _ERWAEHNUNG_RE.findall(getattr(message, "content", "") or ""):
        uid = int(token)
        if uid in gesehen:
            continue
        benutzer = nach_id.get(uid)
        if benutzer is not None:
            gesehen.add(uid)
            raus.append(benutzer)
    return raus


def erstes_ziel(message, *, ohne_bots=True, ohne=()):
    """Die erste getippte Erwaehnung, die als Ziel taugt - oder None."""
    verboten = {int(x) for x in ohne if x is not None}
    for benutzer in echte_erwaehnungen(message):
        if ohne_bots and getattr(benutzer, "bot", False):
            continue
        if int(getattr(benutzer, "id", 0)) in verboten:
            continue
        return benutzer
    return None


#: "Ich habe selbst geantwortet" - EIN Objekt fuer den ganzen Bot.
#:
#: Jedes Modul, das seine Antwort selbst in den Kanal schickt, gibt statt eines
#: Textes dieses Objekt zurueck. bot.on_message erkennt daran, dass es nichts
#: mehr senden muss - und zwar per IDENTITAET (`ist es DIESES Objekt?`), nicht
#: per Vergleich.
#:
#: Genau darum steht es hier und nicht 21 Mal einzeln in den Modulen. Vorher
#: hatte jedes Modul sein eigenes `HANDLED = object()`, und bot.py sammelte sie
#: aus einer von Hand gepflegten Liste ein. Beim Aufteilen einer Datei waere das
#: eine Falle mit Ansage: die neue Datei macht sich ihr eigenes object(), das
#: ist ein ANDERES, bot.py erkennt es nicht - und die Antwort landet in
#: str(antwort)[:80]. Das ist dann kein Schweigen, sondern ein Fehler auf einem
#: nackten object().
#:
#: Mit einem gemeinsamen Sentinel ist Objekt-Identitaet keine Frage der Datei
#: mehr. Die Module behalten ihren Namen `HANDLED` - er zeigt nur alle auf
#: dasselbe Objekt.
HANDLED = object()


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

    # --- Loesch-Schutz -------------------------------------------------------
    # Zehn Module hatten dafuer dasselbe Fuenfzeiler-Paar: ein lazy 'import bot'
    # in einem try/except, einmal zum Anmelden und einmal zum Freigeben. Zehn
    # Kopien derselben Zeilen sind nicht nur Ballast - sie sind zehn Stellen,
    # an denen jemand das try/except vergessen kann, und jede davon ist ein
    # echter Import von bot.py mit allen Nebenwirkungen (siehe laufzeit.py).
    #
    # Jetzt einmal hier. Die Module behalten ihre Namen _protect/_release als
    # duenne Weiterleitung, damit keine Aufrufstelle angefasst werden musste.

    @staticmethod
    def schuetzen(message):
        """Nachricht vor dem Auto-Loeschen schuetzen (Spielrunde laeuft)."""
        if message is None:
            return
        try:
            laufzeit.protect_message(message)
        except Exception:  # noqa: BLE001 - Komfort, kein Grund zum Abbrechen
            log.debug("Loesch-Schutz anmelden fehlgeschlagen", exc_info=True)

    @staticmethod
    def freigeben(message, **wie):
        """Schutz nach der Gnadenfrist wieder aufheben."""
        if message is None:
            return
        try:
            laufzeit.release_message(message, **wie)
        except Exception:  # noqa: BLE001
            log.debug("Loesch-Schutz freigeben fehlgeschlagen", exc_info=True)

    @property
    def client(self):
        """Der laufende discord.Client - oder None, wenn keiner laeuft."""
        return laufzeit.client
