"""Die Uhr, die Spruenge uebersteht.

Auf dem Server springt die Wanduhr ~15 s nach dem Booten zwei Stunden ZURUECK:
die Hardware-Uhr der VM laeuft auf Ortszeit, das System haelt sie fuer UTC, und
erst NTP stellt es richtig (README "Die Uhr des Servers", basis.glaubhafte_zeit).
Flo kann vorher starten. Fuer alles, was nach der Wanduhr plant, heisst das:

* Ein Plan "heute schon gemacht?" mit '!=' feuert bei jedem Sprung erneut -
  vor dem Sprung ist schon morgen, danach wieder heute, um Mitternacht noch
  einmal morgen. Deshalb gibt es hier Schluessel ('2026-10-21', '2026-10',
  '2026-W43'), die NUR VORWAERTS verglichen werden (spaeter_als). Wer etwas
  erledigt hat, merkt sich den Schluessel; ein kleinerer ist dann nie "neu".
* stabil() sagt, ob man der Wanduhr gerade trauen kann: nicht in den ersten
  Minuten nach dem Start (UHR_START_GNADE) und nicht kurz nach einem Sprung.
  Wer nach der Uhr plant, wartet so lange - und holt danach auf (jeder Plan
  hat sein eigenes Nachhol-Fenster), damit kein Tag ausfaellt.
* zeitsprungfest_machen() flickt die relativen tasks.loop von discord.py, die
  nach einem Sprung zurueck sonst zwei Stunden schlafen (siehe dort).

Kein Zustand auf der Platte: der Prozess-Start und der letzte Sprung gelten
nur fuer diesen Lauf.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("dcbot.uhr")

# Dieselbe Zeitzone wie bot.TIMEZONE (dieselbe Variable aus der .env).
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

#: So lange nach dem Start gilt die Uhr als ungeprueft. Der Sprung kommt ~15 s
#: nach dem Booten; fuenf Minuten lassen NTP reichlich Luft.
START_GNADE = float(os.getenv("UHR_START_GNADE", "300") or "300")
#: Ab so viel Versatz zwischen Wanduhr und monotonic ist es ein Sprung. NTP
#: dreht die Uhr sonst nur sanft nach (das zieht monotonic mit), ein echter
#: Sprung ist eine Stufe - zwei Stunden sind keine Ungenauigkeit mehr.
SPRUNG_SCHWELLE = 60.0
#: So lange nach einem Sprung plant niemand nach der Wanduhr.
SPRUNG_GNADE = 300.0


def spaeter_als(neu, alt):
    """Liegt Schluessel 'neu' NACH 'alt'? Der einzige Vergleich fuer diese
    Schluessel - nie '!='.

    Die Schluessel sind so gebaut, dass die Textreihenfolge die zeitliche ist
    (Jahr vorne, alles mit fuehrenden Nullen). Leer oder None als 'alt' heisst
    "noch nie": dann ist jeder Schluessel spaeter."""
    return str(neu or "") > str(alt or "")


class Uhr:
    """Wanduhr mit Sprung-Erkennung.

    zeit/mono sind austauschbar (Tests). Gemerkt wird der Abstand zwischen
    Wanduhr und monotonic: solange niemand an der Uhr dreht, bleibt er stehen -
    NTP zieht beide Uhren gleich nach. Er aendert sich nur bei einem Sprung,
    und nur dann aendert stabil() auch etwas am Zustand."""

    def __init__(self, zeit=time.time, mono=time.monotonic):
        self._zeit = zeit
        self._mono = mono
        self._start = mono()
        self._versatz = zeit() - self._start
        self._sprung_um = float("-inf")     # monotonic des letzten Sprungs ('nie')

    # --- Jetzt und die Schluessel ------------------------------------------
    def jetzt(self):
        """Die Ortszeit (TIMEZONE) - ueber dieselbe Uhr wie stabil()."""
        return datetime.fromtimestamp(self._zeit(), TIMEZONE)

    def tag_key(self, dt=None):
        """'2026-10-21'"""
        return (dt or self.jetzt()).strftime("%Y-%m-%d")

    def monat_key(self, dt=None):
        """'2026-10'"""
        return (dt or self.jetzt()).strftime("%Y-%m")

    def vormonat_key(self, dt=None):
        """Der Monat davor: '2026-09' (im Januar der Dezember des Vorjahres)."""
        dt = dt or self.jetzt()
        return (dt.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    def woche_key(self, dt=None):
        """ISO-Woche: '2026-W43'. Das ISO-Jahr, nicht das Kalenderjahr - der
        29.12.2025 gehoert zur Woche '2026-W01'."""
        jahr, woche, _tag = (dt or self.jetzt()).isocalendar()
        return f"{jahr}-W{woche:02d}"

    # --- Kann man der Uhr trauen? -------------------------------------------
    def stabil(self):
        """Nein, solange START_GNADE seit dem Start nicht um ist, und nein,
        wenn die Wanduhr gesprungen ist - dann SPRUNG_GNADE lang nicht.

        Gemessen wird bei jedem Aufruf. Merkt den Sprung deshalb erst der
        naechste Aufruf; der Takt in bot.py fragt jede Minute."""
        mono = self._mono()
        sprung = (self._zeit() - mono) - self._versatz
        if abs(sprung) > SPRUNG_SCHWELLE:
            self._versatz += sprung
            self._sprung_um = mono
            log.warning("Uhr: die Systemzeit ist um %+.0f s gesprungen - wer nach "
                        "der Uhr plant, wartet %.0f s.", sprung, SPRUNG_GNADE)
        if mono - self._start < START_GNADE:
            return False
        return mono - self._sprung_um >= SPRUNG_GNADE

    # --- discord.py-Loops ----------------------------------------------------
    @staticmethod
    def _probe_loop():
        """Ein nie gestarteter Loop - nur, um sein Innenleben anzusehen."""
        from discord.ext import tasks

        async def _nichts():
            return None
        return tasks.loop(seconds=1)(_nichts)

    def zeitsprungfest_machen(self):
        """Relative tasks.loop (seconds=...) planen ab JETZT statt ab der
        letzten Soll-Zeit.

        discord.py 2.7.1 rechnet die naechste Runde als 'letzte Soll-Zeit +
        Takt' - in WANDUHR-Zeit. Springt die Uhr zwei Stunden zurueck, liegt
        diese Soll-Zeit zwei Stunden in der Zukunft, und der Loop schlaeft so
        lange: Giveaways, Voice-Watchdog, Status, Aufraeumen, $FLO, jeder neue
        Takt. Springt sie vor, holt er die verpassten Runden in einem Rutsch
        nach. Deshalb zwei Flicken:
          1. _get_next_sleep_time: relative Loops ab jetzt + Takt;
          2. _try_sleep_until: ein relativer Loop schlaeft nie laenger als
             einen Takt - fuer den Sprung, der genau in seinen Rumpf faellt
             (dort ist die naechste Runde schon geplant).
        Loops mit fester Uhrzeit (2-Uhr-Shop, 18-Uhr-Erinnerung) bleiben,
        wie sie sind: wacht so einer zu frueh auf, legt discord.py sich selbst
        bis zur Uhrzeit wieder hin.

        discord.py ist nur als '>=2.7.1,<3' festgelegt, und beides ist
        Innenleben. Passt es nicht mehr, wird nichts geraten: eine Warnung,
        kein Flicken. Rueckgabe: True = geflickt (auch: war es schon)."""
        try:
            from discord.ext import tasks
            from discord.utils import MISSING
            klasse = tasks.Loop
            alt_plan = getattr(klasse, "_get_next_sleep_time", None)
            alt_schlaf = getattr(klasse, "_try_sleep_until", None)
            if getattr(alt_plan, "_flo_zeitsprungfest", False):
                return True
            probe = self._probe_loop()
            passt = (callable(alt_plan) and callable(alt_schlaf)
                     and isinstance(probe, klasse)
                     and hasattr(probe, "_sleep") and hasattr(probe, "_last_iteration")
                     and hasattr(probe, "_next_iteration"))
        except Exception as exc:  # noqa: BLE001 - lieber ungeflickt als kein Start
            log.warning("Uhr: discord.py-Loops nicht pruefbar (%s: %s) - relative "
                        "Loops bleiben ungeflickt.", type(exc).__name__, exc)
            return False
        if not passt:
            import discord
            log.warning("Uhr: discord.py %s plant seine Loops anders als erwartet - "
                        "relative Loops bleiben ungeflickt (nach einem Sprung der "
                        "Uhr zurueck koennen sie bis zu zwei Stunden schlafen).",
                        getattr(discord, "__version__", "?"))
            return False

        def _naechste_runde(loop_selbst, now=MISSING):
            if loop_selbst._sleep is not MISSING:
                # relative Loops: ab JETZT, nicht ab der letzten Soll-Zeit
                return datetime.now(timezone.utc) + timedelta(seconds=loop_selbst._sleep)
            return alt_plan(loop_selbst, now)

        def _schlafen_bis(loop_selbst, dt):
            if loop_selbst._sleep is not MISSING:
                spaetestens = (datetime.now(timezone.utc)
                               + timedelta(seconds=loop_selbst._sleep))
                if dt > spaetestens:
                    dt = spaetestens
                    loop_selbst._next_iteration = dt
            return alt_schlaf(loop_selbst, dt)

        _naechste_runde._flo_zeitsprungfest = True
        _schlafen_bis._flo_zeitsprungfest = True
        klasse._get_next_sleep_time = _naechste_runde
        klasse._try_sleep_until = _schlafen_bis
        log.debug("Uhr: relative discord.py-Loops sind zeitsprungfest.")
        return True


# --- Singleton + Modul-API ---------------------------------------------------
instance = Uhr()

jetzt = instance.jetzt
tag_key = instance.tag_key
monat_key = instance.monat_key
vormonat_key = instance.vormonat_key
woche_key = instance.woche_key
stabil = instance.stabil
zeitsprungfest_machen = instance.zeitsprungfest_machen
