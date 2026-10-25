"""Flos Launen: ein Tag hat eine Stimmung, und die faerbt Status und Ton ('flo laune').

Sechs Launen - und alle faerben NUR den Ton. Flo bleibt in jeder derselbe
passiv-aggressive Flo; die Grenze (ai.FloAI._GUARDRAIL) und die Notlagen-Regel
stehen im Prompt vor der Laune und gelten in jeder.

    Laune             Ausloeser                                         Dauer
    😏 gut gelaunt    Tagesbasis (30 %)                                 der Tag
    🙄 normal genervt Tagesbasis (50 %) - keine Zusatzzeile             -
    😤 gereizt        >= 15 Ansprachen in 5 min, eine Person >= 6/min,  45 min
                      Ansprache mit Beleidigung (fun.looks_offensive);
                      Tagesbasis (20 %, falscher Fuss) nur 07-12 Uhr
    🤪 ueberdreht     Jackpot: Casino netto >= max(10x Einsatz, 50k),   60 min
                      Lotto-Gewinn, $FLO +15 % in 24 h
    😒 beleidigt      4 h keine Menschen-Nachricht (gezaehlt nur 09-23  bis ihn
                      Uhr) oder: Chat laeuft, aber 8 h spricht ihn       jemand
                      niemand an                                        anspricht
    🥱 muede          01-07 Uhr                                         bis 07:00

Vorrang: ein Ereignis (gereizt, ueberdreht, beleidigt) vor der Nacht (muede)
vor der Tagesbasis. Unter den Ereignissen schlaegt ueberdreht gereizt und
gereizt beleidigt; dieselbe Laune noch einmal verlaengert nur. 'beleidigt'
endet mit der Ansprache - im Prompt klingt es NACHKLANG Sekunden nach, damit
die Antwort auf genau diese Ansprache es noch raushaengen laesst.

Wo was aufgerufen wird:
    handle(message)              Handler-Kette, nach umfrage
    aktiv(msg)                   bot.on_message, SYNCHRON, fuer JEDE Nachricht
                                 eines Menschen auf dem Server, VOR den Spielen
                                 und Assistenten - nie bei Notlagen. Nur "hier
                                 ist was los" (fuer 'beleidigt': Funkstille)
    nachricht(msg, angesprochen) bot.on_message, SYNCHRON, sobald feststeht,
                                 ob Flo angesprochen ist - nie bei Notlagen
                                 (bot.py filtert, das Modul prueft trotzdem).
                                 Sieht NICHT, was ein Spiel oder Assistent
                                 vorher geschluckt hat (Zaehlen, Quiz-Antwort,
                                 eingefuegte Songs) - dafuer gibt es aktiv()
    tick(client)                 bot.alltag_loop, jede Minute (hoechstens
                                 bot.ALLTAG_FRIST, dann abgebrochen). Die
                                 Sendepause prueft bot.py hier NICHT - die
                                 Laune postet nie etwas, sie braucht sie nicht
    jackpot(uid, netto, einsatz) casino.record / lotto._draw, SYNCHRON. Mit
                                 Einsatz (Casino) zaehlt nur netto >=
                                 max(10x Einsatz, 50k); ohne (Lotto) jeder
                                 Gewinn. Server = der, auf dem die Person
                                 zuletzt geschrieben hat (sonst nichts)
    prompt_zeile(gid) -> str     ai._system_prompt, hinter dem Gedaechtnis und
                                 vor dem Schlusswort. ai.py fragt nur, wenn
                                 is_enabled() und der Schalter 'launen' auf dem
                                 Server an ist, und NIE, wenn im Gespraech eine
                                 Notlage steht (in der Frage oder im Verlauf,
                                 der mit in den Prompt geht). Die Zeile aendert
                                 nur den Ton und endet mit dem Satz, dass
                                 Grenze und Notlagen-Regel trotzdem gelten.
    status_zeile(gid, spruch)    bot.status_loop: Spruch fuer den Status oder
                                 None (dann bleibt der normale Spruch). Im
                                 Wechsel mit den WEISHEITEN; eine NEUE Laune
                                 kommt sofort dran
    stand(gid) -> str            bot.status_loop: die aktuelle Laune als kurzes
                                 Wort ("" = keine). Aendert sie sich, setzt der
                                 Status sofort neu statt erst nach einer Minute.

Uhr: Tagesbasis und Nacht nur bei uhr.stabil() (der Sprung beim Booten), der
Tag laeuft nur vorwaerts (uhr.spaeter_als/glaubhaft). Ereignisse tragen ihr
Ende als Wanduhr-Stempel - basis.glaubhafte_zeit kappt einen, der nach dem
Sprung zu weit in der Zukunft liegt. Zaehler (Spam, Funkstille) laufen auf
monotonic und nur im Speicher: nach einem Neustart faengt alles bei null an.

Je Launenwechsel genau eine INFO-Zeile im Log.

Daten: data/launen.json - {"guilds": {"<gid>": {"tag", "basis", "event",
"aktie_tag"}}}. Keine Nachrichten, nur Laune, Grund und hoechstens ein Name.
Beim Import passiert nichts.
"""

import logging
import random
import re
import time
from collections import deque
from datetime import datetime

import ai
import basis
from basis import FeatureBasis
import features
import fun
import uhr
from store import JsonStore

log = logging.getLogger("dcbot.laune")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Jedes steht auch in cmdnorm.KNOWN (ein Test haelt das fest).
_CMDS = ("laune", "mood")
_ERSTE = frozenset(_CMDS)
# Nur genau diese Formen - 'flo laune ist scheisse' ist ein Satz an die KI.
_BEFEHL_RE = re.compile(r"(?:laune|mood)(?:\s+(?:heute|gerade|grad|jetzt))?")

# --- Die Launen ----------------------------------------------------------------
NORMAL, GUT, GEREIZT, UEBERDREHT, BELEIDIGT, MUEDE = (
    "normal", "gut", "gereizt", "ueberdreht", "beleidigt", "muede")

#: Laune -> (Emoji, Wort fuer Menschen, Wort fuer den Prompt, Stil fuer den Prompt).
#: Der Prompt ist wie der Rest von ai.py ohne Umlaute geschrieben.
_ART = {
    GUT: ("😏", "gut gelaunt", "gut gelaunt",
          "erstaunlich gut drauf - fast gnaedig, aber der Hieb am Ende bleibt"),
    GEREIZT: ("😤", "gereizt", "gereizt", "kuerzer angebunden, null Geduld"),
    UEBERDREHT: ("🤪", "überdreht", "ueberdreht",
                 "laut, zu viel Energie, du uebertreibst alles"),
    BELEIDIGT: ("😒", "beleidigt", "beleidigt",
                "schnippisch, du laesst raushaengen, dass dich keiner beachtet hat"),
    MUEDE: ("🥱", "müde", "muede", "gelangweilt, wenig Bock"),
    NORMAL: ("🙄", "normal genervt", "", ""),
}
#: Launen, die als Ereignis kommen und gehen - mit Vorrang (hoeher schlaegt niedriger).
_RANG = {BELEIDIGT: 1, GEREIZT: 2, UEBERDREHT: 3}

#: Die Tageslaune, einmal je Tag gewuerfelt.
TAGESBASIS = ((GUT, 30), (NORMAL, 50), (GEREIZT, 20))

# --- Ausloeser und Dauer (Sekunden) --------------------------------------------
GEREIZT_DAUER = 45 * 60
UEBERDREHT_DAUER = 60 * 60
#: 'beleidigt' haelt, bis ihn jemand anspricht - aber nie laenger als das.
BELEIDIGT_HOECHSTENS = 12 * 3600
_DAUER = {GEREIZT: GEREIZT_DAUER, UEBERDREHT: UEBERDREHT_DAUER,
          BELEIDIGT: BELEIDIGT_HOECHSTENS}

SPAM_FENSTER, SPAM_ANSPRACHEN = 300.0, 15
PERSON_FENSTER, PERSON_ANSPRACHEN = 60.0, 6
#: Funkstille, gezaehlt nur zwischen TAG_VON und TAG_BIS Uhr.
STILLE = 4 * 3600.0
TAG_VON, TAG_BIS = 9, 23
#: Chat laeuft (letzte Nachricht juenger als CHAT_AKTIV, seit der letzten
#: Ansprache mindestens CHAT_NACHRICHTEN), aber UNBEACHTET lang spricht ihn keiner an.
UNBEACHTET = 8 * 3600.0
CHAT_AKTIV = 15 * 60.0
CHAT_NACHRICHTEN = 20
NACHT_VON, NACHT_BIS = 1, 7
JACKPOT_MIN, JACKPOT_FAKTOR = 50_000, 10
AKTIE_SPRUNG = 15.0                 # Prozent in 24 h
AKTIE_PRUEFEN = 600.0               # so oft schaut Flo auf den Kurs
#: Ein Takt, der laenger aussetzte (Netz weg), zaehlt hoechstens so viel Funkstille.
TAKT_DECKEL = 180.0
#: So viele Leute merkt sich Flo, um ihren Jackpot einem Server zuzuordnen.
WO_MAX = 5000
#: Wer Flo gerade aus 'beleidigt' geholt hat, liest das in 'flo laune' nach.
VERSOEHNT_FRIST = 30.0
#: So lange klingt 'beleidigt' im Prompt nach, wenn ihn endlich jemand anspricht -
#: die Antwort auf genau diese Ansprache soll es noch merken lassen.
NACHKLANG = 120.0
#: Die Tageslaune 'gereizt' (falscher Fuss) gilt nur morgens: von NACHT_BIS bis
#: FUSS_BIS Uhr, danach ist der Tag wieder normal genervt.
FUSS_BIS = 12

# --- Texte ---------------------------------------------------------------------
#: Warum - fuer 'flo laune' (mit {wer}, wenn es einen Schuldigen gibt).
_GRUND = {
    "spam": "weil ihr mich zugespammt habt",
    "person": "weil {wer} mich im Sekundentakt anpingt",
    "beleidigung": "weil {wer} meinte, mich blöd anmachen zu müssen",
    "fuss": "weil ich mit dem falschen Fuß aufgestanden bin",
    "casino": "weil {wer} im Casino abgeräumt hat und ich jetzt durchdreh",
    "lotto": "weil {wer} den Lotto-Jackpot geknackt hat",
    "aktie": "weil $FLO heute durch die Decke geht",
    "stille": "weil hier seit Stunden keiner was schreibt",
    "unbeachtet": "weil ihr seit Stunden quatscht und keiner mit mir redet",
    "nacht": "weil mitten in der Nacht immer noch Leute was von mir wollen",
    "gut": "einfach so. Frag nicht, sonst kippt's",
}
#: Warum - fuer den Prompt. Ohne Namen: ein Anzeigename hat im System-Prompt nichts zu suchen.
_PROMPT_GRUND = {
    "spam": "weil dich gerade alle gleichzeitig zuspammen",
    "person": "weil dich gerade einer im Sekundentakt anpingt",
    "beleidigung": "weil dich gerade einer bloed angemacht hat",
    "fuss": "weil du heute mit dem falschen Fuss aufgestanden bist",
    "casino": "weil gerade einer im Casino richtig abgeraeumt hat",
    "lotto": "weil gerade einer den Lotto-Jackpot geknackt hat",
    "aktie": "weil $FLO heute durch die Decke geht",
    "stille": "weil seit Stunden keiner mehr was schreibt",
    "unbeachtet": "weil dich seit Stunden keiner anspricht, obwohl alle quatschen",
    "nacht": "weil es mitten in der Nacht ist",
    "gut": "einfach so",
}
_PROMPT_SCHLUSS = ("Die Grenze von oben und die Notlagen-Regel gelten trotzdem immer, "
                   "egal wie du drauf bist.")

#: Statuszeilen (je hoechstens 128 Zeichen - ein Test haelt das fest).
_STATUS = {
    GUT: ("😏 Gut drauf. Nutzt es aus, das hält nicht lange.",
          "😏 Heute fast gnädig. Fast.",
          "😏 Erstaunlich gute Laune. Macht sie nicht kaputt."),
    GEREIZT: ("😤 Gereizt. Wer mich pingt, kriegt eine gedrückt.",
              "😤 Null Geduld heute. Fasst euch kurz.",
              "😤 Noch ein Ping und ich explodier."),
    UEBERDREHT: ("🤪 ÜBERDREHT. Wer hat da gewonnen?! Ich dreh durch!",
                 "🤪 Zu viel Energie, zu wenig Hirn. Wie ihr.",
                 "🤪 JACKPOT-MODUS. Niemand beruhigt mich."),
    BELEIDIGT: ("😒 Beleidigt. Mich fragt ja eh keiner.",
                "😒 Redet ruhig weiter ohne mich. Passt schon.",
                "😒 Ich bin nicht sauer. Nur enttäuscht."),
    MUEDE: ("🥱 Müde. Lasst mich pennen.",
            "🥱 Wer jetzt noch was will, hat kein Leben.",
            "🥱 Nachtschicht. Kein Bock."),
}

_NAME_WEG_RE = re.compile(r"[@`*_~|<>#\\]")


def _zahl(wert, ersatz):
    """Eine gespeicherte Zahl - oder 'ersatz', wenn dort Muell steht."""
    try:
        return float(wert)
    except (TypeError, ValueError):
        return ersatz


def _sauberer_name(name):
    """Ein Anzeigename, der in einem Satz nichts anrichtet (kein Ping, kein Markdown)."""
    name = _NAME_WEG_RE.sub("", str(name or "")).strip()
    return name[:32] or "irgendwer"


class Laune(FeatureBasis):
    """Flos Stimmung je Server."""

    def __init__(self):
        self._enabled = False
        self._store = None
        # Uhren austauschbar (Tests): Wanduhr fuer gespeicherte Stempel und
        # die Tageszeit, monotonic fuer alles, was nur im Speicher zaehlt.
        self._zeit = time.time
        self._mono = time.monotonic
        self._lauf = {}          # gid -> Zaehler im Speicher (siehe _lauf_fuer)
        self._wo = {}            # uid -> (gid, Name): wo zuletzt geschrieben
        self._gemeldet = {}      # gid -> zuletzt ins Log geschriebene Laune
        self._versoehnt = {}     # gid -> (mono, uid, art): wer 'beleidigt' beendet hat
        self._nachklang = {}     # gid -> (mono, art): 'beleidigt' endete eben (Prompt)
        self._letzter_tick = None
        self._aktie_geprueft = float("-inf")
        self._status_laune = ""
        self._status_schritt = 0
        # aktiv() und nachricht() sehen dieselbe Nachricht direkt nacheinander -
        # die Notlagen-Pruefung (der teuerste Teil) laeuft dann nur einmal.
        self._notlage_zuletzt = (None, False)

    # --- Aufbau -------------------------------------------------------------
    def setup(self):
        """Launen brauchen keine KI: Status und 'flo laune' gehen auch ohne."""
        self._store = JsonStore("launen.json", default={"guilds": {}})
        self._enabled = True
        log.info("Launen aktiv (%d Server mit Stimmung).",
                 len(self._store.data.get("guilds") or {}))
        return True

    def is_enabled(self):
        return self._enabled and self._store is not None

    # --- Daten ----------------------------------------------------------------
    def _guild(self, gid):
        """Der Eintrag eines Servers - angelegt und mit den richtigen Typen."""
        alle = self._store.data.get("guilds")
        if not isinstance(alle, dict):
            alle = self._store.data["guilds"] = {}
        eintrag = alle.get(str(int(gid)))
        if not isinstance(eintrag, dict):
            eintrag = alle[str(int(gid))] = {}
        for key, leer in (("tag", ""), ("basis", NORMAL), ("aktie_tag", "")):
            if not isinstance(eintrag.get(key), str):
                eintrag[key] = leer
        if eintrag["basis"] not in (GUT, NORMAL, GEREIZT):
            eintrag["basis"] = NORMAL
        if not isinstance(eintrag.get("event"), dict):
            eintrag["event"] = {}
        return eintrag

    def _lies(self, gid):
        """Nur lesen - legt nichts an (stand/prompt_zeile fuer fremde Server)."""
        alle = self._store.data.get("guilds") if self._store is not None else None
        eintrag = alle.get(str(int(gid or 0))) if isinstance(alle, dict) else None
        return eintrag if isinstance(eintrag, dict) else {}

    def _speichern(self):
        if self._store is not None:
            self._store.save_soon()

    def _lauf_fuer(self, gid):
        """Die Zaehler eines Servers - nur im Speicher, ab dem ersten Kontakt."""
        lauf = self._lauf.get(gid)
        if lauf is None:
            jetzt = self._mono()
            lauf = self._lauf[gid] = {
                "aktiv": jetzt, "ansprache": jetzt, "stille": 0.0,
                "seit_ansprache": 0, "ansprachen": deque(), "personen": {}}
        return lauf

    # --- Uhr ------------------------------------------------------------------
    def _jetzt(self):
        return datetime.fromtimestamp(self._zeit(), uhr.TIMEZONE)

    def _nacht(self, dt=None):
        """01-07 Uhr - aber nur, wenn man der Uhr gerade trauen kann."""
        if not uhr.stabil():
            return False
        return NACHT_VON <= (dt or self._jetzt()).hour < NACHT_BIS

    def _morgens(self):
        """07-12 Uhr: die Zeit fuer den falschen Fuss. Bei unstabiler Uhr nein -
        im Zweifel der mildere Ton."""
        return uhr.stabil() and NACHT_BIS <= self._jetzt().hour < FUSS_BIS

    # --- Die aktuelle Laune ---------------------------------------------------
    def _event(self, gid, jetzt=None):
        """Das laufende Ereignis eines Servers oder None (abgelaufen/kaputt)."""
        ev = self._lies(gid).get("event")
        if not isinstance(ev, dict) or ev.get("laune") not in _RANG:
            return None
        jetzt = self._zeit() if jetzt is None else jetzt
        bis = _zahl(ev.get("bis_ts"), 0.0)
        # Nach dem Uhrsprung beim Booten laege das Ende zwei Stunden zu weit
        # vorn - dann gilt es als 'gerade eben gesetzt' und wird zurueckgeschrieben.
        echt = basis.glaubhafte_zeit(bis, jetzt, vorlauf=_DAUER[ev["laune"]])
        if echt != bis:
            ev["bis_ts"] = echt
            self._speichern()
        if jetzt >= echt:
            return None
        return ev

    def _aktuell(self, gid):
        """{'laune', 'art', 'wer', 'seit_ts', 'bis_ts'} - was gerade gilt."""
        jetzt = self._zeit()
        ev = self._event(gid, jetzt)
        if ev is not None:
            return {"laune": ev["laune"], "art": str(ev.get("art") or ""),
                    "wer": str(ev.get("wer") or ""),
                    "seit_ts": _zahl(ev.get("seit_ts"), jetzt),
                    "bis_ts": float(ev["bis_ts"])}
        if self._nacht():
            return {"laune": MUEDE, "art": "nacht", "wer": "", "seit_ts": None,
                    "bis_ts": None}
        basis_laune = self._lies(gid).get("basis")
        if basis_laune not in (GUT, GEREIZT):
            basis_laune = NORMAL
        if basis_laune == GEREIZT and not self._morgens():
            basis_laune = NORMAL                        # der falsche Fuss ist ausgeschlafen
        art = {GUT: "gut", GEREIZT: "fuss"}.get(basis_laune, "")
        return {"laune": basis_laune, "art": art, "wer": "", "seit_ts": None,
                "bis_ts": None}

    def _melden(self, gid):
        """Eine INFO-Zeile, wenn sich die Laune geaendert hat - und nur dann.
        Der erste Blick auf einen Server ist kein Wechsel."""
        jetzt = self._aktuell(gid)
        neu = jetzt["laune"]
        alt = self._gemeldet.get(gid)
        self._gemeldet[gid] = neu
        if alt is None or alt == neu:
            return
        log.info("Laune auf Server %s: %s -> %s (%s).", gid, _ART[alt][1],
                 _ART[neu][1], _PROMPT_GRUND.get(jetzt["art"], "Tageslaune"))

    def _event_setzen(self, gid, laune, art, wer=""):
        """Ein Ereignis beginnt - oder verlaengert das laufende derselben Laune.
        Ein schwaecheres ersetzt nie ein staerkeres (siehe _RANG)."""
        self._gemeldet.setdefault(gid, self._aktuell(gid)["laune"])
        eintrag = self._guild(gid)
        jetzt = self._zeit()
        bis = jetzt + _DAUER[laune]
        alt = self._event(gid, jetzt)
        if alt is not None:
            if alt["laune"] == laune:
                alt["bis_ts"] = max(float(alt["bis_ts"]), bis)
                self._speichern()
                return False
            if _RANG[alt["laune"]] > _RANG[laune]:
                return False
        eintrag["event"] = {"laune": laune, "art": art, "wer": _sauberer_name(wer) if wer else "",
                            "seit_ts": jetzt, "bis_ts": bis}
        self._speichern()
        self._melden(gid)
        return True

    # --- Haken aus bot.py -------------------------------------------------------
    def _notlage(self, text):
        """ai.FloAI._NOTLAGE_RE - fuer denselben Text nur einmal gerechnet."""
        alt_text, alt_wert = self._notlage_zuletzt
        if text is alt_text or text == alt_text:
            return alt_wert
        wert = bool(ai.instance.ist_notlage(text))
        self._notlage_zuletzt = (text, wert)
        return wert

    def aktiv(self, msg):
        """Auf dem Server hat ein Mensch etwas geschrieben. Synchron, billig."""
        if not self._enabled:
            return
        guild = getattr(msg, "guild", None)
        autor = getattr(msg, "author", None)
        if guild is None or autor is None or getattr(autor, "bot", False):
            return
        if self._notlage(getattr(msg, "content", "") or ""):
            return
        gid = int(guild.id)
        lauf = self._lauf_fuer(gid)
        lauf["aktiv"] = self._mono()
        lauf["stille"] = 0.0
        uid = int(getattr(autor, "id", 0) or 0)
        if uid:
            self._wo.pop(uid, None)
            self._wo[uid] = (gid, getattr(autor, "display_name", "") or "")
            if len(self._wo) > WO_MAX:
                del self._wo[next(iter(self._wo))]

    def nachricht(self, msg, angesprochen):
        """Eine Chatnachricht zaehlen (Spam an Flo, Funkstille ...). Synchron."""
        if not self._enabled:
            return
        guild = getattr(msg, "guild", None)
        autor = getattr(msg, "author", None)
        if guild is None or autor is None or getattr(autor, "bot", False):
            return
        text = getattr(msg, "content", "") or ""
        if self._notlage(text):
            return
        gid = int(guild.id)
        lauf = self._lauf_fuer(gid)
        if not angesprochen:
            lauf["seit_ansprache"] += 1
            return
        jetzt = self._mono()
        lauf["ansprache"] = jetzt
        lauf["seit_ansprache"] = 0
        uid = int(getattr(autor, "id", 0) or 0)
        name = getattr(autor, "display_name", "") or ""
        # Angesprochen: aus mit dem Beleidigtsein.
        ev = self._event(gid)
        if ev is not None and ev["laune"] == BELEIDIGT:
            self._gemeldet.setdefault(gid, BELEIDIGT)
            self._versoehnt[gid] = (jetzt, uid, str(ev.get("art") or ""))
            # Die Antwort auf GENAU diese Ansprache soll es noch merken lassen -
            # der Prompt dafuer entsteht erst gleich (prompt_zeile, NACHKLANG).
            self._nachklang[gid] = (jetzt, str(ev.get("art") or ""))
            self._guild(gid)["event"] = {}
            self._speichern()
            self._melden(gid)
        # Alle zusammen zu viel?
        alle = lauf["ansprachen"]
        alle.append(jetzt)
        while alle and jetzt - alle[0] > SPAM_FENSTER:
            alle.popleft()
        if len(alle) >= SPAM_ANSPRACHEN:
            self._event_setzen(gid, GEREIZT, "spam")
        # Einer allein zu viel?
        einer = lauf["personen"].get(uid)
        if einer is None:
            einer = lauf["personen"][uid] = deque()
        einer.append(jetzt)
        while einer and jetzt - einer[0] > PERSON_FENSTER:
            einer.popleft()
        if len(einer) >= PERSON_ANSPRACHEN:
            self._event_setzen(gid, GEREIZT, "person", name)
        elif fun.looks_offensive(text):
            self._event_setzen(gid, GEREIZT, "beleidigung", name)

    def jackpot(self, uid, netto, einsatz=None):
        """Jemand hat dick gewonnen - Flo dreht vielleicht durch. Synchron.

        einsatz gesetzt (Casino): nur ab max(10x Einsatz, 50k) netto. Ohne
        (Lotto): jeder Gewinn. Der Server ist der, auf dem die Person zuletzt
        geschrieben hat - weiss Flo das nicht, passiert nichts."""
        if not self.is_enabled():
            return False
        try:
            netto = int(netto)
            if netto <= 0:
                return False
            if einsatz is not None and netto < max(JACKPOT_FAKTOR * int(einsatz),
                                                   JACKPOT_MIN):
                return False
            wo = self._wo.get(int(uid))
            if wo is None or not features.is_on_in(wo[0], "launen"):
                return False
            return self._event_setzen(wo[0], UEBERDREHT,
                                      "lotto" if einsatz is None else "casino", wo[1])
        except Exception:  # noqa: BLE001 - ein Gewinn darf nie an der Laune scheitern
            log.exception("Laune: Jackpot nicht verbucht")
            return False

    # --- Takt -------------------------------------------------------------------
    async def tick(self, client):
        """Tageslaune wuerfeln, abgelaufene Launen beenden, Funkstille zaehlen."""
        if not self.is_enabled():
            return
        mono = self._mono()
        dt = 0.0 if self._letzter_tick is None else min(max(0.0, mono - self._letzter_tick),
                                                        TAKT_DECKEL)
        self._letzter_tick = mono
        stabil = uhr.stabil()
        jetzt_dt = self._jetzt()
        heute = uhr.tag_key(jetzt_dt)
        aktie = self._aktie_plus(mono) if stabil else None
        for guild in list(getattr(client, "guilds", None) or []):
            gid = int(getattr(guild, "id", 0) or 0)
            if not gid or not features.is_on_in(gid, "launen"):
                continue
            try:
                self._tick_server(gid, mono, dt, stabil, jetzt_dt, heute, aktie)
            except Exception:  # noqa: BLE001 - ein Server darf die anderen nicht kippen
                log.exception("Laune: Takt fuer Server %s fehlgeschlagen", gid)

    def _tick_server(self, gid, mono, dt, stabil, jetzt_dt, heute, aktie):
        eintrag = self._guild(gid)
        self._gemeldet.setdefault(gid, self._aktuell(gid)["laune"])
        # Die Tageslaune: einmal je Tag, der Tag laeuft nur vorwaerts.
        if stabil and uhr.spaeter_als(heute, uhr.glaubhaft(eintrag["tag"], heute)):
            eintrag["tag"] = heute
            eintrag["basis"] = random.choices([l for l, _ in TAGESBASIS],
                                              [g for _, g in TAGESBASIS])[0]
            self._speichern()
        if eintrag["event"] and self._event(gid) is None:
            eintrag["event"] = {}
            self._speichern()
        lauf = self._lauf_fuer(gid)
        for uid, einer in list(lauf["personen"].items()):
            if not einer or mono - einer[-1] > PERSON_FENSTER:
                del lauf["personen"][uid]
        nacht = stabil and NACHT_VON <= jetzt_dt.hour < NACHT_BIS
        # Funkstille: nur tagsueber gezaehlt - nachts schreibt halt keiner.
        if stabil and TAG_VON <= jetzt_dt.hour < TAG_BIS:
            lauf["stille"] += dt
            if lauf["stille"] >= STILLE:
                lauf["stille"] = 0.0
                self._event_setzen(gid, BELEIDIGT, "stille")
        # Alle quatschen, keiner redet mit ihm.
        if (not nacht and mono - lauf["ansprache"] >= UNBEACHTET
                and mono - lauf["aktiv"] <= CHAT_AKTIV
                and lauf["seit_ansprache"] >= CHAT_NACHRICHTEN):
            lauf["seit_ansprache"] = 0
            self._event_setzen(gid, BELEIDIGT, "unbeachtet")
        # $FLO geht durch die Decke: einmal je Tag und Server.
        if (aktie is not None and aktie >= AKTIE_SPRUNG and self._aktie_hier(gid)
                and uhr.spaeter_als(heute, uhr.glaubhaft(eintrag["aktie_tag"], heute))):
            eintrag["aktie_tag"] = heute
            self._speichern()
            self._event_setzen(gid, UEBERDREHT, "aktie")
        self._melden(gid)

    def _aktie_plus(self, mono):
        """Kursplus von $FLO in 24 h (Prozent) - hoechstens alle AKTIE_PRUEFEN
        Sekunden gefragt; None, wenn es nichts zu fragen gibt."""
        if mono - self._aktie_geprueft < AKTIE_PRUEFEN:
            return None
        self._aktie_geprueft = mono
        try:
            import floaktie
            if not floaktie.is_enabled() or floaktie.is_off():
                return None
            _punkte, prozent = floaktie.series(1)
            return float(prozent)
        except Exception:  # noqa: BLE001 - ohne Kurs eben keine Laune davon
            log.debug("Laune: $FLO-Kurs nicht lesbar", exc_info=True)
            return None

    @staticmethod
    def _aktie_hier(gid):
        try:
            import floaktie
            return features.is_on_in(gid, "floaktie") and not floaktie.is_off(gid)
        except Exception:  # noqa: BLE001
            return False

    # --- Was die anderen lesen ------------------------------------------------
    def prompt_zeile(self, gid):
        """Die Zeile fuer den System-Prompt ("" = keine). Nur der Ton - und der
        letzte Satz haelt fest, dass Grenze und Notlagen-Regel trotzdem gelten."""
        if not self.is_enabled() or not gid:
            return ""
        jetzt = self._aktuell(gid)
        laune, art, zusatz = jetzt["laune"], jetzt["art"], ""
        # 'beleidigt' endet mit der Ansprache - bot.py zaehlt sie, BEVOR die KI
        # antwortet. Damit diese Antwort es trotzdem raushaengen laesst, klingt
        # es kurz nach (vor Nacht und Tageslaune, nie vor einem neuen Ereignis).
        nachklang = self._nachklang.get(gid)
        if nachklang is not None:
            if self._mono() - nachklang[0] > NACHKLANG:
                self._nachklang.pop(gid, None)
            elif jetzt["seit_ts"] is None:
                laune, art = BELEIDIGT, nachklang[1]
                zusatz = " - und jetzt redet endlich mal einer mit dir"
        if laune == NORMAL:
            return ""
        _emoji, _wort, prompt_wort, stil = _ART[laune]
        grund = _PROMPT_GRUND.get(art, "")
        grund = f", {grund}{zusatz}" if grund else zusatz
        return (f" Deine Laune gerade: {prompt_wort}{grund}. Das faerbt nur deinen "
                f"Ton: {stil}. Sonst bleibst du genau derselbe wie immer. "
                f"{_PROMPT_SCHLUSS}")

    def status_zeile(self, gid, spruch):
        """Spruch fuer den Bot-Status oder None (dann gilt 'spruch'). Im Wechsel
        mit den Weisheiten - eine neue Laune kommt aber sofort dran."""
        if not self.is_enabled() or not gid:
            return None
        laune = self._aktuell(gid)["laune"]
        if laune == NORMAL:
            self._status_laune = ""
            return None
        if laune != self._status_laune:
            self._status_laune = laune
            self._status_schritt = 0
        self._status_schritt += 1
        if self._status_schritt % 2 == 0:
            return None
        return random.choice(_STATUS[laune])[:128]

    def stand(self, gid):
        """Die aktuelle Laune als kurzes Wort - "" = keine besondere."""
        if not self.is_enabled() or not gid:
            return ""
        laune = self._aktuell(gid)["laune"]
        return "" if laune == NORMAL else laune

    # --- 'flo laune' ------------------------------------------------------------
    async def handle(self, message):
        """'laune' -> wie Flo drauf ist und warum; alles andere None."""
        if not self._enabled:
            return None
        text = ai.strip_lead(getattr(message, "content", "") or "")
        erstes = text[:12].split(None, 1)
        if not erstes or erstes[0].lower().rstrip("?!.,") not in _ERSTE:
            return None
        gid = getattr(getattr(message, "guild", None), "id", 0)
        if not gid:
            return None
        sauber = re.sub(r"\s+", " ", text.lower()).strip(" ?!.,")
        if not _BEFEHL_RE.fullmatch(sauber):
            return None
        return self.beschreibung(gid, getattr(getattr(message, "author", None), "id", 0))

    def beschreibung(self, gid, uid=0):
        """'Laune: 😤 gereizt – seit 14:20, weil ... Hält noch ~40 Min, ...'"""
        a = self._aktuell(gid)
        laune = a["laune"]
        emoji, wort, _p, _s = _ART[laune]
        grund = _GRUND.get(a["art"], "").format(wer=_sauberer_name(a["wer"]))
        jetzt = self._zeit()
        if laune == NORMAL:
            text = (f"Laune: {emoji} {wort}. Also wie immer – ihr nervt, ich ertrag's. "
                    "Hält, bis mir jemand einen Grund gibt.")
        elif a["seit_ts"] is not None:
            seit = datetime.fromtimestamp(min(a["seit_ts"], jetzt), uhr.TIMEZONE)
            rest = max(1, round((a["bis_ts"] - jetzt) / 60))
            text = f"Laune: {emoji} {wort} – seit {seit:%H:%M}, {grund}. "
            text += {
                GEREIZT: f"Hält noch ~{rest} Min, wenn ihr mich in Ruhe lasst.",
                UEBERDREHT: f"Hält noch ~{rest} Min, dann bin ich wieder normal unausstehlich.",
                BELEIDIGT: "Hält, bis mich endlich mal jemand anspricht. Aber lasst euch Zeit.",
            }[laune]
        elif laune == MUEDE:
            text = (f"Laune: {emoji} {wort} – {grund}. Hält bis 7 Uhr, dann bin ich "
                    "wach und genauso schlecht drauf.")
        elif laune == GEREIZT:
            text = (f"Laune: {emoji} {wort} – seit heute früh, {grund}. "
                    f"Hält bis {FUSS_BIS} Uhr, dann bin ich wieder normal genervt. "
                    "Bis dahin: Abstand.")
        else:
            text = (f"Laune: {emoji} {wort} – seit heute früh, {grund}. "
                    "Hält bis Mitternacht. Nutzt es aus oder lasst es.")
        versoehnt = self._versoehnt.get(gid)
        if versoehnt and int(versoehnt[1]) == int(uid or 0) \
                and self._mono() - versoehnt[0] <= VERSOEHNT_FRIST:
            self._versoehnt.pop(gid, None)
            text += (" Bis eben war ich übrigens beleidigt, "
                     f"{_GRUND.get(versoehnt[2], 'weil mich keiner beachtet hat')}. "
                     "Jetzt bist ja du da. Toll.")
        return text


instance = Laune()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
aktiv = instance.aktiv
nachricht = instance.nachricht
tick = instance.tick
jackpot = instance.jackpot
prompt_zeile = instance.prompt_zeile
status_zeile = instance.status_zeile
stand = instance.stand
beschreibung = instance.beschreibung
