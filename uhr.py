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
  Ein gespeicherter Schluessel, der WEIT in der Zukunft liegt, hielte so
  alles an - glaubhaft() verwirft ihn (siehe dort).
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
import re
import time
from datetime import date, datetime, time as tageszeit, timedelta, timezone
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


# Wie die Schluessel aussehen - naechster_key rechnet von dort eine Einheit weiter.
_TAG_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_MONAT_RE = re.compile(r"([0-9]{4})-([0-9]{2})")
_WOCHE_RE = re.compile(r"([0-9]{4})-W([0-9]{2})")


def naechster_key(key):
    """Der Schluessel eine Einheit nach 'key': '2026-10-21' -> '2026-10-22',
    '2026-12' -> '2027-01', '2026-W53' -> '2027-W01'. "" fuer alles, was
    kein Schluessel ist (auch ein 31. Februar)."""
    key = str(key or "")
    try:
        m = _TAG_RE.fullmatch(key)
        if m:
            tag = date(int(m[1]), int(m[2]), int(m[3])) + timedelta(days=1)
            return tag.strftime("%Y-%m-%d")
        m = _MONAT_RE.fullmatch(key)
        if m:
            jahr, monat = int(m[1]), int(m[2])
            if not 1 <= monat <= 12:
                return ""
            jahr, monat = (jahr + 1, 1) if monat == 12 else (jahr, monat + 1)
            return f"{jahr:04d}-{monat:02d}"
        m = _WOCHE_RE.fullmatch(key)
        if m:
            montag = date.fromisocalendar(int(m[1]), int(m[2]), 1) + timedelta(days=7)
            jahr, woche, _tag = montag.isocalendar()
            return f"{jahr}-W{woche:02d}"
    except ValueError:
        return ""
    return ""


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
        # Gespeicherte Schluessel, die glaubhaft() schon verworfen hat - jeder
        # steht nur EINMAL im Log.
        self._verworfen = set()

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

    def glaubhaft(self, alt, jetzt_key):
        """Ein gespeicherter Schluessel so, wie spaeter_als ihn sehen soll.

        NUR VORWAERTS hat eine Kehrseite: ein Schluessel aus der Zukunft haelt
        den Plan an, bis die Uhr ihn eingeholt hat. Eine Einheit voraus ist
        normal - vor dem Sprung zurueck war schon morgen bzw. naechster Monat.
        Liegt er WEITER vorn (ein groesserer Sprung, eine Datei von einem
        anderen Rechner), stuende alles wochen- oder jahrelang still: kein
        Aktien-Tageschart, keine Lotto-Ziehung, waehrend sich die Lose stapeln.
        Das alte '!=' hatte sich da selbst geheilt. Deshalb gilt so ein
        Schluessel bei stabiler Uhr als kaputt: "" = "noch nie", und der Plan
        laeuft ab jetzt weiter. Bei unstabiler Uhr bleibt er stehen - dann ist
        nicht klar, welche der beiden Uhren luegt.

        jetzt_key ist der Schluessel, den der Aufrufer gerade rechnet (Tag,
        Monat oder Woche - dieselbe Art wie 'alt')."""
        alt = str(alt or "")
        grenze = naechster_key(jetzt_key)
        if not alt or not grenze or not spaeter_als(alt, grenze):
            return alt
        if not self.stabil():
            return alt
        if alt not in self._verworfen:
            self._verworfen.add(alt)
            log.warning("Uhr: gespeicherter Stand '%s' liegt mehr als einen Schritt "
                        "nach '%s' - gilt als kaputt, es geht ab jetzt weiter.",
                        alt, jetzt_key)
        return ""

    # --- discord.py-Loops ----------------------------------------------------
    @staticmethod
    def _probe_loop(**takt):
        """Ein nie gestarteter Loop - nur, um sein Innenleben anzusehen."""
        from discord.ext import tasks

        async def _nichts():
            return None
        return tasks.loop(**takt)(_nichts)

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
        wie sie sind. Wacht so einer nach dem Sprung zu FRUEH auf, legt
        discord.py ihn selbst bis zur Uhrzeit wieder hin. Was das nicht heilt:
        plant er seine erste Runde, solange die Uhr noch zwei Stunden vorgeht
        (Start zwischen 0 und 2 bzw. 16 und 18 Uhr), ist die Uhrzeit fuer ihn
        heute schon vorbei - er schlaeft bis morgen, und die Runde von heute
        faellt aus: 2-Uhr-Shop samt Vermoegenssteuer, 18-Uhr-Erinnerung. Das
        war vor dem Flicken genauso; den Shop wuerfelt der Start ohnehin neu
        (economy.refresh_shop), die Steuer fehlt dann einen Tag.

        discord.py ist nur als '>=2.7.1,<3' festgelegt, und beides ist
        Innenleben. Geprueft wird deshalb nicht nur, ob es die Teile gibt,
        sondern auch, ob sie noch dasselbe BEDEUTEN: der Flicken erkennt einen
        relativen Loop an '_sleep is not MISSING'. Speicherte eine neue Version
        fuer feste Uhrzeiten dort None oder 0, planten 2-Uhr-Shop und
        Erinnerung mit timedelta(seconds=None) - und stuerben. Passt es nicht,
        wird nichts geraten: eine Warnung, kein Flicken. Rueckgabe: True =
        geflickt (auch: war es schon)."""
        try:
            from discord.ext import tasks
            from discord.utils import MISSING
            klasse = tasks.Loop
            alt_plan = getattr(klasse, "_get_next_sleep_time", None)
            alt_schlaf = getattr(klasse, "_try_sleep_until", None)
            if getattr(alt_plan, "_flo_zeitsprungfest", False):
                return True
            relativ = self._probe_loop(seconds=1)
            fest = self._probe_loop(time=tageszeit(hour=2, tzinfo=timezone.utc))
            teile = ("_sleep", "_time", "_last_iteration", "_next_iteration")
            passt = (callable(alt_plan) and callable(alt_schlaf)
                     and isinstance(relativ, klasse) and isinstance(fest, klasse)
                     and all(hasattr(p, t) for p in (relativ, fest) for t in teile)
                     # ... und was der Flicken daraus liest: relativ = Takt in
                     # Sekunden und keine Uhrzeit, fest = umgekehrt.
                     and relativ._sleep == 1.0 and relativ._time is MISSING
                     and fest._sleep is MISSING and fest._time is not MISSING)
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
glaubhaft = instance.glaubhaft
zeitsprungfest_machen = instance.zeitsprungfest_machen
