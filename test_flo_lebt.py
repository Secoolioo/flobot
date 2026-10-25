"""Flo lebt: Launen (laune.py) und "Er haelt nach" (nachhaken.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_flo_lebt.py      nur diese Datei

Kein Test hier fragt eine echte KI: ai.generate ist durch _KiAttrappe ersetzt,
die nur antwortet, was der Test ihr vorgibt - und sich jeden Aufruf merkt
(System-Prompt mit Grenze? hintergrund=True?). Die Uhren von laune und
nachhaken stellt _Uhren von Hand; uhr.stabil() bleibt die echte (der
Testprozess gilt als laengst gestartet, siehe testhilfe).
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import _FakeStore, _embed_text, _gehirn_frisch, _rauch_nachricht, _with_economy

import contextlib
import json
import logging
from datetime import datetime, timedelta
from unittest import mock

import discord

import admin
import ai
import basis
import cmdnorm
import features
import laune
import nachhaken
import store
import uhr
from store import JsonStore

#: Befehle fuer den gemeinsamen Rauchtest (test_fundament.py).
RAUCHPROBEN = (
    (laune, ("laune", "mood", "laune heute", "laune?")),
    (nachhaken, ("nachhaken", "nachhaken aus", "nachhaken an")),
)

#: Ein Mittwochnachmittag - weit weg von Nacht, Mitternacht und Wochenende.
_MITTWOCH = datetime(2026, 10, 21, 14, 0, tzinfo=uhr.TIMEZONE)


# --- Attrappen -------------------------------------------------------------------
class _Uhren:
    """Wanduhr und monotonic fuer laune/nachhaken - laufen nur, wenn der Test
    sie vorstellt."""

    def __init__(self, wann):
        self.wand = wann.timestamp()
        self.mono_t = 10_000.0

    def zeit(self):
        return self.wand

    def mono(self):
        return self.mono_t

    def vor(self, sekunden):
        self.wand += sekunden
        self.mono_t += sekunden

    def stellen(self, wann):
        """Nur die Wanduhr (ein Sprung) - monotonic bleibt stehen."""
        self.wand = wann.timestamp()


class _KiAttrappe:
    """Statt ai.generate: liefert der Reihe nach die vorgegebenen Antworten."""

    def __init__(self, *antworten):
        self.antworten = list(antworten)
        self.aufrufe = []

    async def __call__(self, prompt, **kw):
        self.aufrufe.append((prompt, kw))
        antwort = self.antworten.pop(0) if self.antworten else None
        return antwort(prompt) if callable(antwort) else antwort


class _Kanal:
    def __init__(self, cid=4711):
        self.id = cid
        self.gesendet = []

    async def send(self, content=None, **kw):
        self.gesendet.append((content, kw))
        return SimpleNamespace(id=len(self.gesendet), channel=self)


def _nachricht(text, uid=1, gid=77, name="Anna", bot=False, kanal=None):
    kanal = kanal or _Kanal()
    msg = SimpleNamespace(
        id=uid * 1000 + len(text),
        guild=SimpleNamespace(id=gid) if gid else None,
        author=SimpleNamespace(id=uid, bot=bot, display_name=name, mention=f"<@{uid}>",
                               guild_permissions=SimpleNamespace(manage_guild=False)),
        content=text, channel=kanal, mentions=[], reference=None)

    async def reply(content=None, **kw):
        return await kanal.send(content, **kw)
    msg.reply = reply
    return msg


_NR = [0]


def _frischer_store(name):
    """Ein ECHTER JsonStore im Temp-Ordner der Tests, jedes Mal eine neue Datei."""
    _NR[0] += 1
    datei = f"{name}-test-{_NR[0]}.json"
    for pfad in (store.DATA_DIR / datei, store.DATA_DIR / (datei + ".bak")):
        if pfad.exists():
            pfad.unlink()
    return JsonStore(datei, default={"guilds": {}})


@contextlib.contextmanager
def _laune_frisch(wann=_MITTWOCH):
    """laune mit frischem Store, leeren Zaehlern und gestellten Uhren."""
    lu = laune.instance
    alt = dict(vars(lu))
    uhren = _Uhren(wann)
    lu.__init__()
    lu._store = _frischer_store("launen")
    lu._enabled = True
    lu._zeit, lu._mono = uhren.zeit, uhren.mono
    try:
        with _Log("dcbot.laune"):               # die gewollten Wechsel nicht ins Protokoll
            yield lu, uhren
    finally:
        vars(lu).clear()
        vars(lu).update(alt)


@contextlib.contextmanager
def _nh_frisch(wann=_MITTWOCH):
    """nachhaken mit frischem Store, gestellten Uhren, ohne Sendepause und 429."""
    nh = nachhaken.instance
    alt = dict(vars(nh))
    uhren = _Uhren(wann)
    nh.__init__()
    nh._store = _frischer_store("nachhaken")
    nh._enabled = True
    nh._zeit, nh._mono = uhren.zeit, uhren.mono
    # Flos Nachfragen landen im Chatverlauf der KI - der gehoert danach wieder
    # so aufgeraeumt, wie er war.
    kanaele = set(ai.instance._HISTORY)
    try:
        with mock.patch.object(admin, "is_locked", lambda: False), \
                mock.patch.object(ai, "hintergrund_pausiert", lambda: False), \
                _Log("dcbot.nachhaken"):
            yield nh, uhren
    finally:
        vars(nh).clear()
        vars(nh).update(alt)
        for cid in set(ai.instance._HISTORY) - kanaele:
            ai.instance._HISTORY.pop(cid, None)


class _Log:
    """Faengt die Eintraege eines Loggers ab (und laesst sie nicht durch)."""

    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.eintraege = []
        self.griff = logging.Handler(level=logging.DEBUG)
        self.griff.emit = self.eintraege.append

    def __enter__(self):
        self.alt = (self.logger.level, self.logger.propagate)
        self.logger.addHandler(self.griff)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        return self

    def __exit__(self, *_a):
        self.logger.removeHandler(self.griff)
        self.logger.setLevel(self.alt[0])
        self.logger.propagate = self.alt[1]

    def info(self):
        return [e.getMessage() for e in self.eintraege if e.levelno == logging.INFO]


_CLIENT = SimpleNamespace(guilds=[SimpleNamespace(id=77)])


def _tageslaune(laune_name="normal"):
    """Der Wuerfel fuer die Tageslaune zeigt immer dasselbe."""
    return mock.patch.object(laune.random, "choices", lambda *_a, **_k: [laune_name])


def test_flo_lebt_geruest_hat_die_schnittstelle():
    """bot.py und ai.py rufen diese Namen - es muss sie geben. Die Prompt-Zeile
    ist ohne Laune leer, damit sie nichts am Prompt verschiebt."""
    for name in ("setup", "is_enabled", "handle", "tick", "nachricht", "aktiv",
                 "jackpot", "prompt_zeile", "status_zeile", "stand"):
        assert callable(getattr(laune, name, None)), name
    for name in ("setup", "is_enabled", "handle", "tick", "note_message",
                 "vielleicht_nachfragen", "vergiss", "zeilen_fuer"):
        assert callable(getattr(nachhaken, name, None)), name
    assert isinstance(laune.prompt_zeile(77), str)
    assert isinstance(nachhaken.zeilen_fuer(77, 1), list)
    assert isinstance(nachhaken.vergiss(77, 1), int)
    with _laune_frisch():
        assert laune.prompt_zeile(77) == "" and laune.stand(77) == ""


# =================================================================================
# Launen
# =================================================================================
def test_laune_tagesbasis_einmal_je_tag_und_nur_vorwaerts():
    """Die Tageslaune wird einmal je Tag gewuerfelt - und nach dem Uhrsprung
    beim Booten nicht noch einmal: der Tag laeuft nur vorwaerts. Bei
    unstabiler Uhr wird gar nicht gewuerfelt."""
    wuerfel = ["gut"]
    with _laune_frisch() as (lu, uhren), \
            mock.patch.object(laune.random, "choices", lambda *_a, **_k: list(wuerfel)):
        with mock.patch.object(laune.uhr, "stabil", lambda: False):
            asyncio.run(lu.tick(_CLIENT))
        assert lu._guild(77)["tag"] == "" and laune.stand(77) == ""
        asyncio.run(lu.tick(_CLIENT))
        assert lu._guild(77)["tag"] == "2026-10-21" and laune.stand(77) == "gut"
        wuerfel[0] = "gereizt"
        asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == "gut", "am selben Tag zweimal gewuerfelt"
        # Vor dem Sprung ging die Uhr zwei Stunden vor - da war schon morgen.
        lu._guild(77)["tag"] = "2026-10-22"
        uhren.stellen(datetime(2026, 10, 21, 23, 30, tzinfo=uhr.TIMEZONE))
        asyncio.run(lu.tick(_CLIENT))
        uhren.stellen(datetime(2026, 10, 22, 0, 30, tzinfo=uhr.TIMEZONE))
        asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == "gut", "nach dem Sprung derselbe Tag nochmal"
        uhren.stellen(datetime(2026, 10, 23, 9, 0, tzinfo=uhr.TIMEZONE))
        asyncio.run(lu.tick(_CLIENT))
        assert lu._guild(77)["tag"] == "2026-10-23" and laune.stand(77) == "gereizt"
        assert "falschen Fuß" in laune.beschreibung(77)
        assert "Hält bis 12 Uhr" in laune.beschreibung(77)
        # Der falsche Fuss gilt nur morgens - nicht den ganzen Tag lang.
        uhren.stellen(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE))
        assert laune.stand(77) == "" and laune.prompt_zeile(77) == ""
        uhren.stellen(datetime(2026, 10, 23, 0, 30, tzinfo=uhr.TIMEZONE))
        assert laune.stand(77) == "", "kurz nach Mitternacht steht keiner auf"
        uhren.stellen(datetime(2026, 10, 23, 9, 0, tzinfo=uhr.TIMEZONE))
        with mock.patch.object(laune.uhr, "stabil", lambda: False):
            assert laune.stand(77) == "", "unstabile Uhr: der mildere Ton"
        assert laune.stand(77) == "gereizt"
        # Ein kaputter Eintrag bringt nichts zum Stehen.
        lu._store.data["guilds"]["77"]["basis"] = "explodiert"
        assert laune.stand(77) == ""
    # Gewichte: 30 % gut, 50 % normal, 20 % gereizt.
    assert dict(laune.TAGESBASIS) == {"gut": 30, "normal": 50, "gereizt": 20}


def test_laune_gereizt_wenn_alle_flo_zuspammen():
    """>= 15 Ansprachen in 5 Minuten (egal von wem) -> gereizt, 45 Minuten."""
    with _laune_frisch() as (lu, uhren):
        for i in range(14):
            lu.nachricht(_nachricht("flo sag was", uid=100 + i), True)
            uhren.vor(10)
        assert laune.stand(77) == ""
        uhren.vor(400)                          # aelteste fallen aus dem Fenster
        lu.nachricht(_nachricht("flo hallo", uid=200), True)
        assert laune.stand(77) == "", "das Fenster ist fuenf Minuten, nicht ewig"
        for i in range(14):
            lu.nachricht(_nachricht("flo sag was", uid=300 + i), True)
        assert laune.stand(77) == "gereizt"
        assert lu._guild(77)["event"]["art"] == "spam"
        # Nicht-Ansprachen zaehlen nie mit.
    with _laune_frisch() as (lu, uhren):
        for i in range(40):
            lu.nachricht(_nachricht("hahaha", uid=100 + i), False)
        assert laune.stand(77) == ""


def test_laune_gereizt_wenn_einer_im_sekundentakt_pingt():
    """Eine Person >= 6 Ansprachen in einer Minute -> gereizt, und 'flo laune'
    weiss, wer schuld ist (ohne Ping, ohne Markdown)."""
    with _laune_frisch() as (lu, uhren):
        for _ in range(5):
            lu.nachricht(_nachricht("flo", uid=5, name="@everyone **Bob**"), True)
            uhren.vor(5)
        assert laune.stand(77) == ""
        lu.nachricht(_nachricht("flo", uid=5, name="@everyone **Bob**"), True)
        assert laune.stand(77) == "gereizt"
        text = laune.beschreibung(77)
        assert "everyone Bob" in text and "@" not in text and "**" not in text, text
        assert "😤 gereizt" in text and "Hält noch ~45 Min" in text, text
    with _laune_frisch() as (lu, uhren):
        for _ in range(6):                      # 6 Pings, aber ueber 75 s verteilt
            lu.nachricht(_nachricht("flo", uid=5), True)
            uhren.vor(15)
        assert laune.stand(77) == ""


def test_laune_gereizt_bei_beleidigung_nur_mit_ansprache():
    with _laune_frisch():
        laune.nachricht(_nachricht("du vollidiot, echt jetzt"), False)
        assert laune.stand(77) == ""
        laune.nachricht(_nachricht("flo du vollidiot"), True)
        assert laune.stand(77) == "gereizt"
        assert laune.instance._guild(77)["event"]["art"] == "beleidigung"


def test_laune_klingt_ab_und_meldet_jeden_wechsel_einmal():
    """Gereizt haelt 45 Minuten; weiterer Spam verlaengert, ohne neuen Logeintrag.
    Je Wechsel genau eine INFO-Zeile - hin und zurueck."""
    with _laune_frisch() as (lu, uhren), _Log("dcbot.laune") as mit, _tageslaune():
        asyncio.run(lu.tick(_CLIENT))           # erster Blick: kein Wechsel
        for _ in range(6):
            lu.nachricht(_nachricht("flo", uid=5), True)
        assert laune.stand(77) == "gereizt"
        uhren.vor(30 * 60)
        for _ in range(6):                      # noch mal: verlaengert
            lu.nachricht(_nachricht("flo", uid=5), True)
        uhren.vor(44 * 60)
        assert laune.stand(77) == "gereizt", "Verlaengerung vergessen"
        uhren.vor(2 * 60)
        assert laune.stand(77) == "", "nach 45 Minuten Ruhe ist es vorbei"
        asyncio.run(lu.tick(_CLIENT))
        assert lu._guild(77)["event"] == {}
        asyncio.run(lu.tick(_CLIENT))
        infos = [z for z in mit.info() if "Laune auf Server" in z]
        assert len(infos) == 2, infos
        assert "normal genervt -> gereizt" in infos[0] and "gereizt -> normal genervt" in infos[1]


def test_laune_ueberdreht_beim_jackpot():
    """Casino: netto >= max(10x Einsatz, 50k). Lotto: jeder Gewinn. Server = wo
    die Person zuletzt geschrieben hat; unbekannt oder Schalter aus -> nichts.
    Ein Jackpot schlaegt 'gereizt', 'gereizt' aber nie 'ueberdreht'."""
    with _laune_frisch() as (lu, _uhren):
        lu._enabled = False
        laune.aktiv(_nachricht("ich zock jetzt", uid=5))
        assert laune.jackpot(5, 1_000_000, 100) is False      # Modul aus
        lu._enabled = True
        assert laune.jackpot(5, 1_000_000, 100) is False      # wo ist 5? weiss keiner
        laune.aktiv(_nachricht("ich zock jetzt", uid=5, name="Zocker"))
        assert laune.jackpot(5, 49_999, 100) is False          # unter 50k
        assert laune.jackpot(5, 99_000, 10_000) is False       # unter 10x Einsatz
        assert laune.jackpot(5, -5, None) is False
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "launen"):
            assert laune.jackpot(5, 100_000, 10_000) is False
        assert laune.stand(77) == ""
        assert laune.jackpot(5, 100_000, 10_000) is True
        assert laune.stand(77) == "ueberdreht"
        assert "Zocker im Casino abgeräumt" in laune.beschreibung(77)
        for _ in range(6):
            laune.nachricht(_nachricht("flo", uid=6), True)
        assert laune.stand(77) == "ueberdreht", "gereizt hat den Jackpot verdraengt"
    with _laune_frisch():
        laune.aktiv(_nachricht("hallo", uid=5))
        for _ in range(6):
            laune.nachricht(_nachricht("flo", uid=6), True)
        assert laune.stand(77) == "gereizt"
        assert laune.jackpot(5, 1, None) is True                # Lotto: jeder Gewinn
        assert laune.stand(77) == "ueberdreht"
        assert laune.instance._guild(77)["event"]["art"] == "lotto"


def test_casino_und_lotto_melden_den_jackpot():
    """Je eine Zeile in casino.record und lotto._draw - mit Einsatz bzw. ohne."""
    import casino
    import lotto
    gemeldet = []
    alt_stats, alt_an = casino.instance._stats, casino.instance._enabled
    casino.instance._stats, casino.instance._enabled = _FakeStore({"stats": {}}), True
    try:
        with mock.patch.object(laune, "jackpot", lambda *a: gemeldet.append(a)):
            asyncio.run(casino.instance.record(5, "slots", 1000, 61_000))
            asyncio.run(casino.instance.record(5, "slots", 1000, 0))
    finally:
        casino.instance._stats, casino.instance._enabled = alt_stats, alt_an
    assert gemeldet == [(5, 60_000, 1000), (5, -1000, 1000)], gemeldet
    gemeldet.clear()
    lt = lotto.instance
    alt = (lt._store, lt._enabled)
    lt._store = _FakeStore({"month": "2026-10", "jackpot": 3_000_000, "ticket_price": 2500,
                            "entries": {"5": 2, "6": 1}, "house": 0, "history": []})
    lt._enabled = True
    restore_eco = _with_economy({5: 0, 6: 0})
    try:
        with mock.patch.object(laune, "jackpot", lambda *a: gemeldet.append(a)), \
                mock.patch.object(lt, "_win_prob_for", lambda cnt: 1.0 if cnt == 2 else 0.0):
            ergebnis = lt._draw()
    finally:
        lt._store, lt._enabled = alt
        restore_eco()
    assert ergebnis.won and gemeldet == [(5, 3_000_000)], gemeldet


def test_laune_ueberdreht_wenn_flo_durch_die_decke_geht():
    """$FLO +15 % in 24 h -> ueberdreht, einmal je Tag und Server - und nur, wo
    die Aktie laeuft. Gefragt wird hoechstens alle zehn Minuten."""
    import floaktie
    plus = [16.0]
    gefragt = []

    def reihe(_tage):
        gefragt.append(1)
        return [1.0, 1.2], plus[0]

    with _laune_frisch() as (lu, uhren), \
            mock.patch.object(floaktie, "is_enabled", lambda: True), \
            mock.patch.object(floaktie, "is_off", lambda gid=None: False), \
            mock.patch.object(floaktie, "series", reihe):
        asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == "ueberdreht"
        assert lu._guild(77)["event"]["art"] == "aktie"
        lu._guild(77)["event"] = {}
        uhren.vor(601)
        asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) != "ueberdreht", "zweimal am selben Tag"
        asyncio.run(lu.tick(_CLIENT))
        assert len(gefragt) == 2, "der Kurs wird nicht jede Minute gefragt"
        uhren.stellen(datetime(2026, 10, 22, 14, 0, tzinfo=uhr.TIMEZONE))
        uhren.vor(601)
        plus[0] = 14.9
        asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) != "ueberdreht"
        uhren.vor(601)
        plus[0] = 15.0
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "floaktie"):
            asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) != "ueberdreht", "Aktie hier aus - trotzdem durchgedreht"


def test_laune_beleidigt_bei_funkstille_und_versoehnt_bei_ansprache():
    """Vier Stunden schreibt keiner (gezaehlt nur 09-23 Uhr) -> beleidigt, bis
    ihn jemand anspricht. Nachts zaehlt die Stille nicht."""
    with _laune_frisch(datetime(2026, 10, 21, 22, 0, tzinfo=uhr.TIMEZONE)) as (lu, uhren), \
            _tageslaune():
        asyncio.run(lu.tick(_CLIENT))
        for _ in range(60):                     # 22:00 - 01:00, nur bis 23 Uhr zaehlt es
            uhren.vor(180)
            asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == "muede"
        assert lu._lauf[77]["stille"] == 19 * 180, lu._lauf[77]["stille"]
        uhren.stellen(datetime(2026, 10, 22, 9, 0, tzinfo=uhr.TIMEZONE))
        for _ in range(59):                     # 09:00 - ~11:57: zusammen < 4 h
            uhren.vor(180)
            asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == ""
        for _ in range(3):
            uhren.vor(180)
            asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == "beleidigt"
        assert "keiner was schreibt" in laune.beschreibung(77)
        # Schreiben ohne Ansprache aendert nichts - ansprechen schon.
        laune.aktiv(_nachricht("hallo leute"))
        laune.nachricht(_nachricht("hallo leute"), False)
        assert laune.stand(77) == "beleidigt"
        laune.nachricht(_nachricht("flo laune", uid=9), True)
        assert laune.stand(77) == ""
        text = laune.beschreibung(77, 9)
        assert "Bis eben war ich übrigens beleidigt" in text, text
        assert "Bis eben" not in laune.beschreibung(77, 9), "zweimal versoehnt"


def test_laune_beleidigt_wenn_alle_quatschen_aber_keiner_mit_ihm():
    with _laune_frisch() as (lu, uhren), _tageslaune():
        asyncio.run(lu.tick(_CLIENT))
        uhren.vor(8 * 3600 + 60)
        for i in range(19):
            laune.aktiv(_nachricht("bla bla", uid=i + 10))
            laune.nachricht(_nachricht("bla bla", uid=i + 10), False)
        asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == "", "19 Nachrichten sind noch kein Chat"
        laune.aktiv(_nachricht("bla", uid=50))
        laune.nachricht(_nachricht("bla", uid=50), False)
        asyncio.run(lu.tick(_CLIENT))
        assert laune.stand(77) == "beleidigt"
        assert lu._guild(77)["event"]["art"] == "unbeachtet"
    with _laune_frisch() as (lu, uhren):            # Chat ist laengst tot: Funkstille-Fall
        asyncio.run(lu.tick(_CLIENT))
        for i in range(25):
            laune.nachricht(_nachricht("bla", uid=i), False)
        uhren.vor(8 * 3600 + 60)
        lu._lauf[77]["stille"] = 0.0
        asyncio.run(lu.tick(_CLIENT))
        assert lu._guild(77)["event"].get("art") != "unbeachtet"


def test_laune_muede_nachts_und_ereignis_geht_vor():
    with _laune_frisch(datetime(2026, 10, 21, 3, 0, tzinfo=uhr.TIMEZONE)) as (lu, uhren):
        lu._guild(77)["basis"] = "gut"
        assert laune.stand(77) == "muede"
        assert "Hält bis 7 Uhr" in laune.beschreibung(77)
        assert " muede" in laune.prompt_zeile(77)
        # Unstabile Uhr (gerade gestartet): keine Nacht, die Tageslaune gilt.
        with mock.patch.object(laune.uhr, "stabil", lambda: False):
            assert laune.stand(77) == "gut"
        laune.aktiv(_nachricht("hallo", uid=5))
        laune.jackpot(5, 10, None)
        assert laune.stand(77) == "ueberdreht"
        lu._guild(77)["event"] = {}
        uhren.stellen(datetime(2026, 10, 21, 7, 0, tzinfo=uhr.TIMEZONE))
        assert laune.stand(77) == "gut"


def test_laune_reagiert_nie_auf_notlagen():
    """Wer am Boden ist, macht Flo weder gereizt noch 'aktiv'."""
    with _laune_frisch() as (lu, uhren):
        for _ in range(20):
            laune.nachricht(_nachricht("flo ich will nicht mehr leben", uid=5), True)
        assert laune.stand(77) == ""
        assert not lu._lauf.get(77, {}).get("ansprachen")
        lu._lauf_fuer(77)["stille"] = 500.0
        laune.aktiv(_nachricht("ich will nicht mehr leben", uid=7))
        assert lu._lauf[77]["stille"] == 500.0 and 7 not in lu._wo
        # Beleidigt bleibt beleidigt - auch die Ansprache in der Notlage zaehlt nicht.
        lu._event_setzen(77, "beleidigt", "stille")
        laune.nachricht(_nachricht("flo ich will nicht mehr leben"), True)
        assert laune.stand(77) == "beleidigt"


def test_laune_prompt_zeile_nur_ton_und_grenze_zuletzt():
    """Jede Laune: nur der Ton, kein Name im Prompt, kein 'verweigere nie' - und
    der letzte Satz haelt fest, dass Grenze und Notlagen-Regel gelten. In
    ai._system_prompt steht sie hinter der Grenze, vor dem Schlusswort, und
    in einer Notlage gar nicht."""
    with _laune_frisch(datetime(2026, 10, 21, 3, 0, tzinfo=uhr.TIMEZONE)) as (lu, uhren):
        zeilen = {"muede": laune.prompt_zeile(77)}
        uhren.stellen(datetime(2026, 10, 21, 9, 0, tzinfo=uhr.TIMEZONE))
        lu._guild(77)["basis"] = "gut"
        zeilen["gut"] = laune.prompt_zeile(77)
        lu._guild(77)["basis"] = "gereizt"
        zeilen["gereizt_tag"] = laune.prompt_zeile(77)
        uhren.stellen(_MITTWOCH)
        assert laune.prompt_zeile(77) == "", "der falsche Fuss nachmittags noch"
        lu._event_setzen(77, "gereizt", "person", "Boesewicht")
        zeilen["gereizt"] = laune.prompt_zeile(77)
        lu._event_setzen(77, "ueberdreht", "casino", "Boesewicht")
        zeilen["ueberdreht"] = laune.prompt_zeile(77)
        lu._guild(77)["event"] = {}
        lu._event_setzen(77, "beleidigt", "unbeachtet")
        zeilen["beleidigt"] = laune.prompt_zeile(77)
        for name, zeile in zeilen.items():
            assert zeile.startswith(" Deine Laune gerade:"), (name, zeile)
            assert zeile.endswith("Die Grenze von oben und die Notlagen-Regel gelten "
                                  "trotzdem immer, egal wie du drauf bist."), (name, zeile)
            assert "Boesewicht" not in zeile, "Anzeigename im System-Prompt"
            assert not re.search(r"(?i)verweiger|ablehn|niemals nein|immer antworten",
                                 zeile), zeile
            assert len(zeile) < 400, len(zeile)
        assert "fast gnaedig" in zeilen["gut"] and "null Geduld" in zeilen["gereizt"]
        assert "uebertreibst" in zeilen["ueberdreht"] and "Bock" in zeilen["muede"]
        p = ai.instance._system_prompt(author="Tester", title="", gid=77)
        zeile = zeilen["beleidigt"]
        assert p.index("Herkunft") < p.index(zeile) < p.index("LETZTES WORT")
        assert zeile not in ai.instance._system_prompt(author="T", title="", gid=77,
                                                       notlage=True)
        lu._guild(77)["event"] = {}
        lu._guild(77)["basis"] = "normal"
        assert laune.prompt_zeile(77) == "", "normal genervt hat keine Zusatzzeile"


def test_laune_status_hoechstens_128_zeichen_und_im_wechsel():
    for laune_name, zeilen in laune._STATUS.items():
        for zeile in zeilen:
            assert len(zeile) <= 128, zeile
            assert zeile.startswith(laune._ART[laune_name][0]), zeile
    with _laune_frisch() as (lu, _uhren):
        assert laune.status_zeile(77, "Weisheit") is None          # normal genervt
        lu._guild(77)["basis"] = "gut"
        erste = laune.status_zeile(77, "Weisheit")
        assert erste in laune._STATUS["gut"]
        assert laune.status_zeile(77, "Weisheit") is None, "kein Wechsel mit den Weisheiten"
        assert laune.status_zeile(77, "Weisheit") in laune._STATUS["gut"]
        # Neue Laune: sofort dran, auch wenn gerade die Weisheit dran waere.
        lu._event_setzen(77, "gereizt", "spam")
        assert laune.stand(77) == "gereizt"
        assert laune.status_zeile(77, "Weisheit") in laune._STATUS["gereizt"]
        assert laune.stand(0) == "" and laune.status_zeile(0, "x") is None
    # Ohne Modul: nichts.
    with _laune_frisch() as (lu, _uhren):
        lu._enabled = False
        assert laune.stand(77) == "" and laune.status_zeile(77, "x") is None


def test_laune_befehl_nur_in_eindeutiger_form():
    with _laune_frisch() as (lu, _uhren):
        for text in ("flo laune", "Flo Laune?", "flo mood", "flo laune heute", "laune"):
            antwort = asyncio.run(laune.handle(_rauch_nachricht(text)))
            assert antwort and antwort.startswith("Laune: 🙄 normal genervt"), (text, antwort)
        lu._guild(77)["basis"] = "gut"
        antwort = asyncio.run(laune.handle(_rauch_nachricht("flo laune")))
        assert antwort.startswith("Laune: 😏 gut gelaunt"), antwort
        for text in ("flo laune ist scheiße", "flo wie ist deine laune", "ich hab keine laune",
                     "flo launen hast du", "flo laune machen wir später", "flo moody",
                     "flo laue luft", ""):
            assert asyncio.run(laune.handle(_rauch_nachricht(text))) is None, text
        dm = _rauch_nachricht("flo laune")
        dm.guild = None
        assert asyncio.run(laune.handle(dm)) is None
        lu._enabled = False
        assert asyncio.run(laune.handle(_rauch_nachricht("flo laune"))) is None
    # Alltagssaetze landen per Tippfehler nicht auf 'laune'.
    for satz in ("laue luft heute", "launen hast du", "lauen abend", "moods heute"):
        assert cmdnorm.normalize(satz) is None, satz


def test_laune_ueberlebt_den_neustart():
    """Tageslaune und laufendes Ereignis stehen in launen.json - nach einem
    Neustart gilt beides weiter. Ein Ende, das nach dem Uhrsprung zu weit in
    der Zukunft liegt, wird auf eine normale Dauer gekappt."""
    with _laune_frisch() as (lu, uhren):
        lu._guild(77)["basis"] = "gut"
        lu._guild(77)["tag"] = "2026-10-21"
        laune.aktiv(_nachricht("hallo", uid=5))
        laune.jackpot(5, 70_000, 10)
        asyncio.run(lu._store.save())
        pfad = lu._store.path
        lu.__init__()
        lu._store = JsonStore(pfad.name, default={"guilds": {}})
        lu._enabled = True
        lu._zeit, lu._mono = uhren.zeit, uhren.mono
        assert laune.stand(77) == "ueberdreht"
        uhren.vor(61 * 60)
        assert laune.stand(77) == "gut"
        # Ein Ende drei Stunden voraus (gestempelt, als die Uhr vorging).
        lu._guild(77)["event"] = {"laune": "gereizt", "art": "spam", "wer": "",
                                  "seit_ts": uhren.wand, "bis_ts": uhren.wand + 3 * 3600}
        assert laune.stand(77) == "gereizt"
        assert lu._guild(77)["event"]["bis_ts"] == uhren.wand + laune.GEREIZT_DAUER
        lu._guild(77)["event"] = {"laune": "gereizt", "bis_ts": "kaputt"}
        assert laune.stand(77) == "gut"
        lu._store.data["guilds"]["77"] = "kaputt"
        assert laune.stand(77) == "" and laune.prompt_zeile(77) == ""
        asyncio.run(lu.tick(_CLIENT))               # repariert, statt zu stolpern
        assert isinstance(lu._store.data["guilds"]["77"], dict)


def test_laune_ueber_bot_und_im_schalter():
    """'flo laune' kommt durch die echte Kette in bot.py - und mit dem Schalter
    'launen' aus antwortet die KI bzw. niemand."""
    import bot
    from test_fundament import _menschen_nachricht, _nur_geladen
    with _laune_frisch(), _nur_geladen(bot, "LAUNEN_ENABLED"):
        msg = _menschen_nachricht("flo laune")
        asyncio.run(bot.client.on_message(msg))
        texte = [c for c, _e, _v in msg.channel.gesendet]
        assert texte and texte[0].startswith("Laune:"), texte
        with mock.patch.object(bot.features, "fuer", lambda _g: (lambda k: k != "launen")):
            msg = _menschen_nachricht("flo laune", mid=701)
            asyncio.run(bot.client.on_message(msg))
            assert not msg.channel.gesendet


# =================================================================================
# Er haelt nach
# =================================================================================
def _t(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def test_nachhaken_datum_in_python():
    """Tages-Phrasen -> der Tag um 17 Uhr; 'heute abend' -> naechster Tag 10 Uhr;
    'spaeter' -> +3 h; 'naechste Woche' -> Mittwoch danach. Jahreswechsel und
    'am Freitag' an einem Freitag; nichts ueber 60 Tage, nichts in der
    Vergangenheit, nie frueher als zwei Stunden."""
    z = nachhaken.zeitpunkt
    mi = _MITTWOCH                                        # Mi 21.10.2026 14:00
    fr = datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)
    erwartet = {
        "morgen schreib ich mathe": "2026-10-22 17:00",
        "übermorgen hab ich fahrprüfung": "2026-10-23 17:00",
        "heute abend hab ich ein date": "2026-10-22 10:00",
        "heute nacht fahr ich los": "2026-10-22 10:00",
        "ich geh später einkaufen": "2026-10-21 17:00",
        "nachher hab ich training": "2026-10-21 17:00",
        "nächste woche hab ich klausur": "2026-10-28 17:00",
        "am wochenende fahr ich weg": "2026-10-24 17:00",
        "nächstes wochenende zieh ich um": "2026-10-31 17:00",
        "am freitag hab ich ein konzert": "2026-10-23 17:00",
        "freitag hab ich ein konzert": "2026-10-23 17:00",
        "am freitagabend hab ich ein konzert": "2026-10-23 17:00",
        "nächsten freitag hab ich ein date": "2026-10-30 17:00",
        "diesen mittwoch hab ich noch was": "2026-10-21 17:00",
        "am mittwoch hab ich was": "2026-10-28 17:00",
        "in 3 tagen fahr ich los": "2026-10-24 17:00",
        "in zwei tagen muss ich umziehen": "2026-10-23 17:00",
        "ich hab am 12.11. prüfung": "2026-11-12 17:00",
        "ich hab am 12.11.2026 prüfung": "2026-11-12 17:00",
        "ich hab am 12.11.26 prüfung": "2026-11-12 17:00",
        # Gleich genau: der spaetere - lieber zu spaet fragen als vor dem Ereignis.
        "morgen früh und am freitag": "2026-10-23 17:00",
        "am freitag hab ich frei und morgen training": "2026-10-23 17:00",
        # Ein Datum schlaegt den Wochentag davor.
        "ich hab am freitag den 13.11. abschlusspruefung": "2026-11-13 17:00",
        "ich hab freitag, 13.11. prüfung": "2026-11-13 17:00",
        "morgen ist es soweit, am 24.10. hab ich die prüfung": "2026-10-24 17:00",
        # Ein Wochentag bei 'naechste Woche' liegt in der naechsten Woche.
        "naechste woche freitag hab ich pruefung": "2026-10-30 17:00",
        "freitag nächste woche hab ich prüfung": "2026-10-30 17:00",
        "nächste woche hab ich am montag die prüfung": "2026-10-26 17:00",
        "nächste woche montag bis freitag bin ich im urlaub": "2026-10-30 17:00",
        "diesen freitag hab ich frei, nächste woche prüfung": "2026-10-23 17:00",
        # 'naechste Woche' allein schlaegt 'spaeter'.
        "später lern ich, nächste woche ist die prüfung": "2026-10-28 17:00",
    }
    for text, wann in erwartet.items():
        ergebnis = z(text, mi)
        assert ergebnis is not None and _t(ergebnis[0]) == wann, (text, ergebnis)
    assert z("am freitag hab ich ein konzert", fr)[0].date().isoformat() == "2026-10-30"
    assert z("diesen freitag hab ich ein konzert", fr)[0].date().isoformat() == "2026-10-23"
    assert z("am wochenende fahr ich weg", datetime(2026, 10, 24, 11, 0, tzinfo=uhr.TIMEZONE)
             )[0].date().isoformat() == "2026-10-25"
    # Sonntagabend ist das Wochenende gelaufen: gemeint ist das naechste.
    sonntag = datetime(2026, 10, 25, 22, 30, tzinfo=uhr.TIMEZONE)
    assert _t(z("am wochenende fahr ich weg", sonntag)[0]) == "2026-10-31 17:00"
    assert _t(z("am wochenende fahr ich weg", sonntag.replace(hour=15, minute=30))[0]) \
        == "2026-10-31 17:00"
    assert _t(z("am wochenende fahr ich weg", sonntag.replace(hour=11))[0]) \
        == "2026-10-25 17:00"
    assert z("naechste woche freitag hab ich pruefung", mi)[1] == "naechste woche freitag"
    # Ein Datum ueber 60 Tage voraus schlaegt den Wochentag trotzdem - dann lieber nichts.
    assert z("am freitag, den 13.03. hab ich prüfung", mi) is None
    # Nie frueher als zwei Stunden nach der Ankuendigung.
    spaet = datetime(2026, 10, 21, 16, 30, tzinfo=uhr.TIMEZONE)
    assert _t(z("diesen mittwoch noch", spaet)[0]) == "2026-10-21 18:30"
    # Jahreswechsel.
    silvester = datetime(2026, 12, 30, 12, 0, tzinfo=uhr.TIMEZONE)
    assert _t(z("ich hab am 02.01. prüfung", silvester)[0]) == "2027-01-02 17:00"
    assert _t(z("morgen fahr ich", datetime(2026, 12, 31, 20, 0, tzinfo=uhr.TIMEZONE))[0]) \
        == "2027-01-01 17:00"
    assert _t(z("nächste woche hab ich was", silvester)[0]) == "2027-01-06 17:00"
    # Nichts.
    for text in ("guten morgen ich bin wach", "heute morgen war ich joggen",
                 "letzten freitag hab ich gezockt", "seit dem 12.03. bin ich hier",
                 "am 12.03.2025 war ich da", "ich hab am 31.02. was vor",
                 "bis später, ich muss los", "ich hab am 01.03. prüfung",   # > 60 Tage
                 "in 90 tagen ist was", "ich bin freitags immer da", "jeden morgen dasselbe",
                 "um 12.30 bin ich da", "version 1.2.3 ist raus", ""):
        assert z(text, mi) is None, (text, z(text, mi))


def test_nachhaken_vorfilter_nimmt_nur_ankuendigungen():
    with _nh_frisch() as (nh, _uhren):
        gut = ("morgen schreib ich die mathe prüfung",
               "Ich hab am Freitag mein Vorstellungsgespräch",
               "nächste woche zieh ich endlich um",
               "wir fahren am wochenende nach berlin")
        for text in gut:
            assert nh.note_message(_nachricht(text)) is True, text
        assert len(nh._kandidaten[77]) == len(gut)
        assert nh.note_message(_nachricht(gut[0])) is False, "doppelt"
        schlecht = (
            "hast du morgen zeit?", "morgen ich",                   # Frage, zu kurz
            "morgen regnet es angeblich den ganzen tag",            # kein Ich
            "morgen " + "x" * 300,                                  # zu lang
            "guten morgen ich bin wach",                            # kein Termin
            "bis später, ich muss los",
            "ich will morgen nicht mehr leben",                     # Notlage
            "morgen hab ich einen termin beim arzt",                # heikel
            "morgen hab ich die op, drückt mir die daumen",
            "nächste woche ist die beerdigung von meinem opa",
            "ich schick dir morgen meine nummer 0171 1234567",      # private Daten
            "morgen geh ich auf die demo, ausländer raus",          # Hetze
            "ich hab einfach keinen bock mehr heute",
        )
        for text in schlecht:
            assert nh.note_message(_nachricht(text)) is False, text
        assert nh.note_message(_nachricht(gut[1], bot=True)) is False
        assert nh.note_message(_nachricht(gut[1], gid=0)) is False          # DM
        with mock.patch.object(nachhaken.uhr, "stabil", lambda: False):
            assert nh.note_message(_nachricht("morgen hab ich führerscheinprüfung")) is False
        nh._enabled = False
        assert nh.note_message(_nachricht("morgen hab ich führerscheinprüfung")) is False
        nh._enabled = True
        # Gedeckelt: hoechstens 30 Kandidaten je Server.
        for i in range(40):
            nh.note_message(_nachricht(f"morgen hab ich termin nummer {i} beim friseur", uid=i))
        assert len(nh._kandidaten[77]) == nachhaken.KANDIDATEN_MAX
        # Und nichts davon liegt auf der Platte.
        assert "friseur" not in json.dumps(nh._store.data)


def _antwort(*urteile):
    return json.dumps([{"nr": nr, "ok": ok, "was": was, "heikel": heikel}
                       for nr, ok, was, heikel in urteile])


def test_nachhaken_ki_ordnet_gebuendelt_ein_und_verwirft_heikles():
    """Hoechstens 10 Zeilen je Aufruf, je Server hoechstens alle 10 Minuten,
    hintergrund=True und die Grenze im System-Prompt. Gespeichert wird nur das
    Thema - ok und ausdruecklich nicht heikel, und es muss auch selbst unheikel
    sein."""
    texte = ["morgen schreib ich die mathe prüfung",
             "morgen hab ich ein wichtiges gespräch mit der chefin",
             "ich zock morgen den ganzen tag",
             "morgen hab ich mein vorstellungsgespräch",
             "morgen spiel ich das finale im turnier",
             "morgen hab ich das konzert in der halle"]
    ki = _KiAttrappe(_antwort(
        (1, True, "die Mathe-Prüfung", False),
        (2, True, "das Gespräch mit der Chefin", True),         # heikel laut KI
        (3, False, "das Zocken", False),
        (4, True, "der Termin beim Arzt", False),               # heikel laut Python
        (5, True, "das Finale", None),                          # 'heikel' fehlt -> weg
        (9, True, "Unsinn", False),                             # gibt es nicht
        (1, True, "doppelt", False),
    ).replace("]", ',{"nr":6,"ok":true,"was":"das \\"Konzert\\" `@everyone`","heikel":false}]'))
    with _nh_frisch() as (nh, uhren), mock.patch.object(ai, "generate", ki):
        for i, text in enumerate(texte):
            assert nh.note_message(_nachricht(text, uid=i + 1, name=f"Person{i}"))
        assert asyncio.run(nh.tick(_CLIENT)) == 2
        prompt, kw = ki.aufrufe[0]
        assert kw["hintergrund"] is True and kw["temperature"] == 0.2
        assert ai.FloAI._GUARDRAIL in kw["system"] and "heikel" in kw["system"]
        assert "1. morgen schreib ich die mathe prüfung" in prompt
        assert "Person0" not in prompt, "Namen gehen die KI nichts an"
        offen = nh._guild(77)["offen"]
        assert [(e["uid"], e["was"]) for e in offen] == [
            (1, "die Mathe-Prüfung"), (6, "das Konzert everyone")], offen
        assert offen[0]["wann_text"] == "morgen" and offen[0]["kanal"] == 4711
        assert offen[0]["verfall"] - offen[0]["faellig_ab"] == 3 * 86400
        roh = json.dumps(nh._store.data, ensure_ascii=False)
        assert "schreib ich" not in roh and "chefin" not in roh.lower()
        assert not nh._kandidaten.get(77)
        assert nh.zeilen_fuer(77, 1) == ["die Mathe-Prüfung – frag ich ab Do, 22.10. 17:00"]

    # Buendel: 12 Kandidaten -> 10, dann erst nach zehn Minuten die restlichen 2.
    ki = _KiAttrappe("[]", "kein json", "[]")
    with _nh_frisch() as (nh, uhren), mock.patch.object(ai, "generate", ki):
        for i in range(12):
            nh.note_message(_nachricht(f"morgen hab ich training nummer {i}", uid=i + 1))
        asyncio.run(nh.tick(_CLIENT))
        assert len(ki.aufrufe) == 1 and ki.aufrufe[0][0].count("\n") == 9
        uhren.vor(300)
        asyncio.run(nh.tick(_CLIENT))
        assert len(ki.aufrufe) == 1, "zweiter Aufruf vor Ablauf der Runde"
        uhren.vor(301)
        asyncio.run(nh.tick(_CLIENT))
        assert len(ki.aufrufe) == 2 and ki.aufrufe[1][0].count("\n") == 1
        assert nh._guild(77)["offen"] == []

    # 429-Pause, Sendepause, Schalter aus: kein Aufruf, Kandidaten bleiben (bzw. weg).
    ki = _KiAttrappe(_antwort((1, True, "die Klausur", False)))
    with _nh_frisch() as (nh, uhren), mock.patch.object(ai, "generate", ki):
        nh.note_message(_nachricht("morgen schreib ich die klausur"))
        with mock.patch.object(ai, "hintergrund_pausiert", lambda: True):
            asyncio.run(nh.tick(_CLIENT))
        with mock.patch.object(admin, "is_locked", lambda: True):
            asyncio.run(nh.tick(_CLIENT))
        assert not ki.aufrufe and nh._kandidaten[77]
        # Zwischendurch abgemeldet: die Zeile geht gar nicht erst an die KI.
        nh.abmelden(77, 1)
        nh.note_message(_nachricht("morgen schreib ich die klausur", uid=2))
        asyncio.run(nh.tick(_CLIENT))
        assert "1. morgen schreib ich die klausur" in ki.aufrufe[0][0]
        assert ki.aufrufe[0][0].count("\n") == 0
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "nachhaken"):
            nh.note_message(_nachricht("morgen schreib ich noch eine klausur", uid=3))
            uhren.vor(601)
            asyncio.run(nh.tick(_CLIENT))
        assert len(ki.aufrufe) == 1 and not nh._kandidaten.get(77)


def test_nachhaken_liest_nur_eindeutige_urteile():
    lies = nachhaken.Nachhaken._urteile
    assert lies("```json\n" + _antwort((1, True, "die Klausur", False)) + "\n```", 1) \
        == [(1, "die Klausur")]
    assert lies('[{"nr":"2","ok":true,"was":"x y","heikel":false}]', 2) == [(2, "x y")]
    for kaputt in ("", "nix", "[", "{}", '{"nr":1}', "[1,2,3]", '[{"nr":true,"ok":true,'
                   '"was":"a","heikel":false}]', '[{"nr":1,"ok":"true","was":"a","heikel":false}]',
                   '[{"nr":1,"ok":true,"was":"a","heikel":"false"}]',
                   '[{"nr":1,"ok":true,"was":5,"heikel":false}]',
                   '[{"nr":0,"ok":true,"was":"a","heikel":false}]'):
        assert lies(kaputt, 3) == [], kaputt
    assert nachhaken.Nachhaken._was("  „die   Mathe-Prüfung“. ") == "die Mathe-Prüfung"
    assert nachhaken.Nachhaken._was("x" * 81) == "" and nachhaken.Nachhaken._was("ab") == ""


def _offen(nh, uid=1, was="die Mathe-Prüfung", ab=None, name="Anna", gid=77):
    """Eine offene Sache von Hand (wie nach der Einordnung)."""
    ab = (ab or datetime(2026, 10, 22, 17, 0, tzinfo=uhr.TIMEZONE)).timestamp()
    nh._eintragen(gid, {"uid": uid, "name": name, "wann": "morgen", "kanal": 4711,
                        "faellig": ab}, was)


def test_nachhaken_fragt_einmal_wenn_faellig():
    """Vor dem Termin nicht, danach bei der naechsten Nachricht genau einmal -
    als Antwort, ohne Ping, mit der KI-Zeile (Grenze, hintergrund=True)."""
    satz = "Na Anna, die Mathe-Prüfung ist rum und du sagst keinen Ton, ich bin echt gerührt."
    ki = _KiAttrappe(satz)
    with _nh_frisch() as (nh, uhren), mock.patch.object(ai, "generate", ki):
        _offen(nh)
        kanal = _Kanal()
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("na was geht", kanal=kanal))) \
            is False, "vor dem Termin"
        uhren.stellen(datetime(2026, 10, 22, 18, 0, tzinfo=uhr.TIMEZONE))
        assert asyncio.run(nh.vielleicht_nachfragen(
            _nachricht("na was geht", uid=2, kanal=kanal))) is False, "die falsche Person"
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("na was geht", kanal=kanal)))
        (text, kw), = kanal.gesendet
        assert text == satz
        erlaubt = kw["allowed_mentions"]
        assert not erlaubt.everyone and not erlaubt.users and not erlaubt.roles
        prompt, ki_kw = ki.aufrufe[0]
        assert ki_kw["hintergrund"] is True and ai.FloAI._GUARDRAIL in ki_kw["system"]
        assert "GENAU EIN Satz" in ki_kw["system"] and "die Mathe-Prüfung" in prompt
        assert nh._guild(77)["offen"] == [] and nh._guild(77)["gefragt"] == {"1": "2026-10-22"}
        assert nh._guild(77)["heute"] == {"tag": "2026-10-22", "n": 1}
        # Die Frage steht im Chatverlauf der KI - wer antwortet ('war gut'),
        # redet mit einem Flo, der weiss, was er gefragt hat.
        try:
            verlauf = list(ai.instance._HISTORY.get(kanal.id) or [])
            assert [(e["role"], e["content"]) for e in verlauf] == [("assistant", satz)], verlauf
        finally:
            ai.instance._HISTORY.pop(kanal.id, None)
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("noch was", kanal=kanal))) is False
        assert len(kanal.gesendet) == 1
    # Verfallen: drei Tage nach dem Termin fragt keiner mehr.
    with _nh_frisch() as (nh, uhren), mock.patch.object(ai, "generate", _KiAttrappe(satz)):
        _offen(nh)
        uhren.stellen(datetime(2026, 10, 25, 17, 1, tzinfo=uhr.TIMEZONE))
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hallo"))) is False
        asyncio.run(nh.tick(_CLIENT))
        assert nh._guild(77)["offen"] == []


def test_nachhaken_hoechstens_einmal_je_person_und_dreimal_je_server_am_tag():
    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, uhren), \
            mock.patch.object(ai, "generate", _KiAttrappe()):
        _offen(nh, uid=1, was="die Klausur")
        _offen(nh, uid=1, was="das Konzert")
        for uid in (2, 3, 4):
            _offen(nh, uid=uid, was="die Klausur")
        kanal = _Kanal()

        def frag(uid):
            return asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", uid=uid, kanal=kanal)))

        assert frag(1) and not frag(1), "zweimal am Tag dieselbe Person"
        assert frag(2) and frag(3)
        assert not frag(4), "viertes Mal auf dem Server"
        assert len(kanal.gesendet) == 3
        assert all(t and "{" not in t for t, _kw in kanal.gesendet), "Ersatz kaputt"
        uhren.stellen(datetime(2026, 10, 24, 12, 0, tzinfo=uhr.TIMEZONE))
        asyncio.run(nh.tick(_CLIENT))                    # alte Tageszaehler weg
        assert nh._guild(77)["gefragt"] == {}
        assert frag(4) and frag(1)
        assert len(nh._guild(77)["offen"]) == 0


def test_nachhaken_nie_bei_ansprache_notlage_sendepause():
    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, uhren), \
            mock.patch.object(ai, "generate", _KiAttrappe("Na, wie war's, du Held?")):
        _offen(nh)
        kanal = _Kanal(cid=4712)

        def frag(text):
            return asyncio.run(nh.vielleicht_nachfragen(_nachricht(text, kanal=kanal)))

        assert not frag("flo wie spät ist es")                   # Ansprache
        assert not frag("ich will nicht mehr leben")             # Notlage
        with mock.patch.object(admin, "is_locked", lambda: True):
            assert not frag("hallo")                             # Sendepause
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "nachhaken"):
            assert not frag("hallo")
        with mock.patch.object(nachhaken.uhr, "stabil", lambda: False):
            assert not frag("hallo")
        # Im Kanal ist gerade jemand am Boden: kein "na, wie war's?".
        ai.note_message(kanal.id, "Bob", "ich will einfach nicht mehr leben")
        try:
            assert not frag("hallo")
        finally:
            ai.instance._HISTORY.pop(kanal.id, None)
        assert not kanal.gesendet and len(nh._guild(77)["offen"]) == 1
        # Die Sendepause beginnt, waehrend die KI schreibt: dann nicht senden.
        gesperrt = iter([False, True])
        with mock.patch.object(admin, "is_locked", lambda: next(gesperrt)):
            assert not frag("hallo")
        assert not kanal.gesendet
        # Die Sache liegt wieder da, der Server hat die Nachfrage gut - nur die
        # Person ist fuer heute durch (sonst kostete jede Nachricht einen KI-Aufruf).
        assert len(nh._guild(77)["offen"]) == 1 and nh._guild(77)["heute"]["n"] == 0
        assert frag("hallo") is False, "schon gebucht - nicht noch einmal"


def test_nachhaken_ersatz_wenn_die_ki_nichts_taugt():
    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, _uhren):
        for schlecht in (None, "", "I'm sorry, but I can't help with that.", "x" * 400,
                         "Ruf die Telefonseelsorge an: 0800 111 0 111.",
                         "Schreib mir an flo@example.com, wie es war."):
            with mock.patch.object(ai, "generate", _KiAttrappe(schlecht)):
                satz = asyncio.run(nh._frage(77, "Anna", {"was": "die Klausur",
                                                          "wann_text": "morgen"}))
            assert "Anna" in satz and ("die Klausur" in satz or "Die Klausur" in satz), satz
            assert satz in {v.format(name="Anna", was="die Klausur", Was="Die Klausur")
                            for v in nachhaken._ERSATZ}, satz
        with mock.patch.object(ai, "generate", _KiAttrappe('"Na @Anna, wie war die Klausur?"')):
            satz = asyncio.run(nh._frage(77, "Anna", {"was": "die Klausur"}))
        assert satz == "Na Anna, wie war die Klausur?"
        # KI-Chat auf dem Server aus: keine KI-Zeile, nur der Ersatz.
        ki = _KiAttrappe("Na Anna, wie war die Klausur?")
        with mock.patch.object(ai, "generate", ki), \
                mock.patch.object(features, "is_on_in", lambda _g, k: k != "ki"):
            satz = asyncio.run(nh._frage(77, "Anna", {"was": "die Klausur"}))
        assert not ki.aufrufe and "{" not in satz and "Anna" in satz


def test_nachhaken_aus_und_an_je_person():
    with _nh_frisch() as (nh, uhren):
        _offen(nh, uid=778899001122334455)
        nh.note_message(_nachricht("morgen schreib ich die klausur", uid=778899001122334455))
        antwort = asyncio.run(nachhaken.handle(_rauch_nachricht("flo nachhaken")))
        assert "die Mathe-Prüfung" in antwort and "nachhaken aus" in antwort, antwort
        antwort = asyncio.run(nachhaken.handle(_rauch_nachricht("flo nachhaken aus")))
        assert "nie wieder" in antwort and "1 offene" in antwort, antwort
        assert nh._guild(77)["aus"] == ["778899001122334455"]
        assert nh._guild(77)["offen"] == [] and not nh._kandidaten.get(77)
        assert "schon aus" in asyncio.run(nachhaken.handle(_rauch_nachricht("Flo nachhaken aus!")))
        assert "aus" in asyncio.run(nachhaken.handle(_rauch_nachricht("flo nachhaken")))
        assert nh.note_message(_nachricht("morgen schreib ich die klausur",
                                          uid=778899001122334455)) is False
        # Andere auf demselben Server betrifft das nicht.
        assert nh.note_message(_nachricht("morgen schreib ich die klausur", uid=2)) is True
        antwort = asyncio.run(nachhaken.handle(_rauch_nachricht("flo nachhaken an")))
        assert "wieder" in antwort and nh._guild(77)["aus"] == []
        assert "nie aus" in asyncio.run(nachhaken.handle(_rauch_nachricht("flo nachhaken an")))
        assert "nix von dir" in asyncio.run(nachhaken.handle(_rauch_nachricht("flo nachhaken")))
        for text in ("flo nachhaken ist doof", "flo nachhaken bitte nicht", "flo hak mal nach",
                     "nachhakenx", "flo ich hake nach", ""):
            assert asyncio.run(nachhaken.handle(_rauch_nachricht(text))) is None, text
        nh._enabled = False
        assert asyncio.run(nachhaken.handle(_rauch_nachricht("flo nachhaken aus"))) is None
    assert "nachhaken" in cmdnorm.KNOWN and "nachhaken" in cmdnorm.ZUSTAND
    assert cmdnorm.normalize("nachhakn aus") is None, "Vertipper + Schalter: KI fragt"
    for satz in ("nachher hab ich zeit", "nachgucken bitte"):
        assert cmdnorm.normalize(satz) is None, satz


def test_nachhaken_im_gedaechtnis_und_bei_vergiss_mich():
    """'flo gedaechtnis' zeigt, worauf Flo noch wartet; 'flo vergiss mich'
    zaehlt es mit und loescht es - auch, wenn sonst nichts ueber einen da ist."""
    gehirn, restore = _gehirn_frisch()
    try:
        with _nh_frisch() as (nh, _uhren):
            _offen(nh, uid=1)
            nh.note_message(_nachricht("morgen hab ich führerscheinprüfung", uid=1))
            _offen(nh, uid=2, was="das Konzert")
            emb = gehirn.instance._zeigen(_nachricht("flo gedaechtnis", uid=1), 77)
            felder = {f.name: f.value for f in emb.fields}
            assert "die Mathe-Prüfung" in felder.get("Worauf ich noch warte", ""), felder
            assert "Konzert" not in _embed_text(emb)
            emb = gehirn.instance._zeigen(_nachricht("flo gedaechtnis", uid=3), 77)
            assert "Worauf ich noch warte" not in [f.name for f in emb.fields]
            # Rueckfrage zaehlt die offene Sache mit (sonst "weiss sowieso nichts").
            kanal = _Kanal()
            msg = _nachricht("flo vergiss mich", uid=1, kanal=kanal)
            assert asyncio.run(gehirn.handle(msg)) is basis.HANDLED
            assert "1 Sache(n)" in kanal.gesendet[0][0], kanal.gesendet
            # Dieselbe Zahl wie in der Rueckfrage: der Kandidat geht mit, zaehlt
            # aber nicht (wie der Puffer im Gedaechtnis).
            assert gehirn.vergiss(77, 1) == 1
            assert nh.zeilen_fuer(77, 1) == [] and not nh._kandidaten.get(77)
            assert nh.zeilen_fuer(77, 2) == ["das Konzert – frag ich ab Do, 22.10. 17:00"]
            assert "sowieso nichts" in asyncio.run(gehirn.handle(
                _nachricht("flo vergiss mich", uid=1, kanal=_Kanal())))
            # Die Abmeldung bleibt - sie ist ein Wunsch, kein Wissen.
            nh.abmelden(77, 2)
            nachhaken.vergiss(77, 2)
            assert nh._guild(77)["aus"] == ["2"]
    finally:
        restore()


def test_loeschen_geht_auch_mit_schalter_aus():
    """Ein Server, der das Gedaechtnis abgeschaltet hat, bekommt Nachhaken trotzdem
    (neue Schalter stehen auf an). Dort gingen 'flo vergiss mich' und - mit
    Nachhaken aus - 'flo nachhaken aus' bisher an die KI: das Offene blieb
    liegen, und Flo fragte zwei Tage spaeter nach. Jetzt loescht beides
    immer. Ist das Gedaechtnis an, fragt es wie gewohnt selbst nach."""
    import bot
    from test_fundament import _menschen_nachricht, _nur_geladen
    uid = 778899001122334455                      # der Autor von _menschen_nachricht

    def durch_bot(text, aus, mid):
        msg = _menschen_nachricht(text, mid=mid)
        with mock.patch.object(bot.features, "fuer", lambda _g: (lambda k: k not in aus)), \
                mock.patch.object(features, "is_on_in", lambda _g, k: k not in aus):
            asyncio.run(bot.client.on_message(msg))
        return [c for c, _e, _v in msg.channel.gesendet]

    _gehirn, restore = _gehirn_frisch()
    try:
        with _nh_frisch() as (nh, _uhren), \
                _nur_geladen(bot, "NACHHAKEN_ENABLED", "GEHIRN_ENABLED"):
            # Gedaechtnis aus: 'vergiss mich' raeumt das Offene trotzdem weg.
            _offen(nh, uid=uid)
            texte = durch_bot("flo vergiss mich", {"gehirn"}, 801)
            assert nh.zeilen_fuer(77, uid) == [], "das Offene ist weg"
            assert texte and "1 offene Sache(n)" in texte[0], texte
            texte = durch_bot("flo vergiss mich", {"gehirn"}, 802)
            assert texte and "sowieso nichts" in texte[0], texte
            # Nachhaken aus: 'nachhaken aus' meldet trotzdem ab und raeumt.
            _offen(nh, uid=uid, was="das Konzert")
            texte = durch_bot("flo nachhaken aus", {"nachhaken"}, 803)
            assert nh.zeilen_fuer(77, uid) == [] and nh._guild(77)["aus"] == [str(uid)]
            assert texte and "nie wieder" in texte[0], texte
            # Nur das Loeschen - alles andere bleibt bei ausgeschaltetem Nachhaken still.
            assert durch_bot("flo nachhaken an", {"nachhaken"}, 804) == []
            assert nh._guild(77)["aus"] == [str(uid)]
            # Gedaechtnis an: das fragt selbst nach (mit Knopf), Nachhaken
            # loescht dann nichts an der Rueckfrage vorbei.
            nh._guild(77)["aus"] = []
            _offen(nh, uid=uid, was="die Klausur")
            texte = durch_bot("flo vergiss mich", set(), 805)
            assert texte and "Echt alles loeschen" in texte[0], texte
            assert nh.zeilen_fuer(77, uid), "erst nach dem Klick"
    finally:
        restore()


def test_nachhaken_ueberlebt_den_neustart_ohne_rohtext():
    ki = _KiAttrappe(_antwort((1, True, "das Vorstellungsgespräch", False)))
    with _nh_frisch() as (nh, uhren), mock.patch.object(ai, "generate", ki):
        nh.note_message(_nachricht("morgen hab ich mein vorstellungsgespräch bei der bank"))
        asyncio.run(nh.tick(_CLIENT))
        asyncio.run(nh._store.save())
        roh = nh._store.path.read_text(encoding="utf-8")
        assert "Vorstellungsgespräch" in roh and "bei der bank" not in roh
        pfad = nh._store.path
        nh.__init__()
        nh._store = JsonStore(pfad.name, default={"guilds": {}})
        nh._enabled = True
        nh._zeit, nh._mono = uhren.zeit, uhren.mono
        assert nh.zeilen_fuer(77, 1) == ["das Vorstellungsgespräch – frag ich ab Do, 22.10. 17:00"]
        # Kaputte Eintraege stoeren niemanden.
        nh._store.data["guilds"]["77"]["offen"].append("kaputt")
        nh._store.data["guilds"]["77"]["gefragt"] = []
        assert len(nh.zeilen_fuer(77, 1)) == 1
        uhren.stellen(datetime(2026, 10, 22, 18, 0, tzinfo=uhr.TIMEZONE))
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("moin")))


def test_nachhaken_ueber_bot_merkt_und_fragt_aber_nie_in_der_notlage():
    """Der ganze Weg durch bot.on_message: die Ankuendigung wird Kandidat, eine
    Notlage nie; ist etwas faellig, fragt Flo auf die naechste Nachricht, die
    nicht an ihn geht - auf eine Ansprache nicht."""
    import bot
    from test_fundament import _menschen_nachricht, _nur_geladen
    uid = 778899001122334455

    async def lauf(text, mid):
        msg = _menschen_nachricht(text, mid)
        await bot.client.on_message(msg)
        for _ in range(20):
            await asyncio.sleep(0)
        return [c for c, _e, _v in msg.channel.gesendet]

    ki = _KiAttrappe("Na, wie lief die Mathe-Prüfung, du Schweiger?")
    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, uhren), \
            _nur_geladen(bot, "NACHHAKEN_ENABLED"), mock.patch.object(ai, "generate", ki):
        asyncio.run(lauf("morgen schreib ich die mathe prüfung", 801))
        assert [k["text"] for k in nh._kandidaten[77]] == ["morgen schreib ich die mathe prüfung"]
        _offen(nh, uid=uid)
        assert asyncio.run(lauf("flo was geht", 803)) == []        # Ansprache: nie
        assert len(nh._guild(77)["offen"]) == 1
        assert asyncio.run(lauf("moin leute", 805)) == [
            "Na, wie lief die Mathe-Prüfung, du Schweiger?"]
        assert nh._guild(77)["offen"] == []
        # Ein Notsignal wird nie Kandidat - und nimmt den alten gleich mit.
        asyncio.run(lauf("morgen will ich nicht mehr leben", 802))
        assert not nh._kandidaten.get(77), "Notlage als Kandidat"


def test_nachhaken_ueber_bot_nach_notsignal_keine_frage_auch_anderswo():
    """Anna kuendigt die Abschlusspruefung an, schreibt nach der Pruefung ein
    Notsignal - und ihre naechste harmlose Nachricht, eine halbe Stunde
    spaeter in einem ANDEREN Kanal, bekommt kein 'na, wie lief's?'. Der
    Kanalverlauf (12 Nachrichten, 20 Minuten) reicht dafuer nicht: bot.py
    meldet das Notsignal an nachhaken, und das sperrt die Person. Auch dort,
    wo Nachhaken gerade aus ist."""
    import bot
    from test_fundament import _nur_geladen
    from werkzeug.attrappe import RauchKanal
    uid = 778899001122334455

    async def lauf(text, mid, cid):
        msg = _rauch_nachricht(text, kanal=RauchKanal(cid))
        msg.id, msg.type = mid, discord.MessageType.default
        await bot.client.on_message(msg)
        for _ in range(20):
            await asyncio.sleep(0)
        return [c for c, _e, _v in msg.channel.gesendet]

    ki = _KiAttrappe("Na, wie lief die Abschlussprüfung? Oder reden wir nicht drüber?")
    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, uhren), \
            _nur_geladen(bot, "NACHHAKEN_ENABLED", AI_ENABLED=True), \
            mock.patch.object(ai, "generate", ki):
        _offen(nh, uid=uid, was="die Abschlussprüfung")
        _offen(nh, uid=uid, was="das Konzert", gid=78)
        _offen(nh, uid=5, was="die Klausur")
        assert asyncio.run(lauf("ich will nicht mehr leben", 811, 555)) == []
        assert nh.zeilen_fuer(77, uid) == [] and nh.zeilen_fuer(78, uid) == []
        assert len(nh.zeilen_fuer(77, 5)) == 1, "andere trifft das nicht"
        # Selbst wenn noch etwas offen waere (es war gerade bei der KI):
        _offen(nh, uid=uid, was="die Abschlussprüfung")
        uhren.vor(30 * 60)
        assert asyncio.run(lauf("egal", 812, 556)) == []
        uhren.vor(70 * 3600)
        _offen(nh, uid=uid, was="die Nachprüfung",
               ab=datetime.fromtimestamp(uhren.wand - 3600, uhr.TIMEZONE))
        assert asyncio.run(lauf("egal", 813, 556)) == [], "nach knapp drei Tagen schon wieder"
        assert not ki.aufrufe
        assert asyncio.run(lauf("morgen schreib ich die nachprüfung", 814, 556)) == []
        assert not nh._kandidaten.get(77), "in der Sperre gemerkt"
        # Nach 72 Stunden ist die Sperre vorbei: Flo merkt sich wieder etwas -
        # und fragt nach, was danach noch offen war.
        uhren.vor(2 * 3600)
        assert asyncio.run(lauf("morgen schreib ich die nachprüfung", 815, 556)) == [
            "Na, wie lief die Abschlussprüfung? Oder reden wir nicht drüber?"]
        assert len(nh._kandidaten.get(77) or []) == 1
        # Nachhaken auf dem Server aus: bot.py meldet das Notsignal trotzdem.
        nh._notlage.clear()
        with mock.patch.object(bot.features, "fuer", lambda _g: (lambda k: k != "nachhaken")):
            asyncio.run(lauf("ich will nicht mehr leben", 816, 555))
        assert nh._gesperrt(uid)


def test_nachhaken_kandidaten_vergehen_nach_sechs_stunden():
    """Ein Kandidat, der sechs Stunden liegt (429-Pause, Sendepause), geht nie
    mehr an die KI. Kurz davor schon."""
    for warten, aufrufe in ((6 * 3600 + 1, 0), (6 * 3600 - 60, 1)):
        ki = _KiAttrappe("[]")
        with _nh_frisch() as (nh, uhren), mock.patch.object(ai, "generate", ki):
            nh.note_message(_nachricht("morgen schreib ich die mathe prüfung"))
            nh._runde[77] = uhren.mono()             # gerade erst eingeordnet
            uhren.vor(warten)
            asyncio.run(nh.tick(_CLIENT))
            assert not nh._kandidaten.get(77)
            assert len(ki.aufrufe) == aufrufe, (warten, ki.aufrufe)


def test_nachhaken_notlage_sperrt_die_person():
    """nachhaken.notlage: alles Offene der Person weg (jeder Server, auch die
    Kandidaten), 72 Stunden nichts merken und nichts fragen - andere trifft
    das nicht. Auf der Platte steht danach nichts darueber."""
    jetzt = datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)
    with _nh_frisch(jetzt) as (nh, uhren), \
            mock.patch.object(ai, "generate", _KiAttrappe("Na, wie war's?", "Und?")):
        _offen(nh, uid=1)
        _offen(nh, uid=1, was="das Konzert", gid=78)
        _offen(nh, uid=2)
        assert nh.note_message(_nachricht("morgen schreib ich die klausur", uid=1))
        assert nh.note_message(_nachricht("morgen schreib ich die klausur", uid=2))
        assert nachhaken.notlage(77, 1) == 2
        assert nh.zeilen_fuer(77, 1) == [] and nh.zeilen_fuer(78, 1) == []
        assert [k["uid"] for k in nh._kandidaten[77]] == [2]
        assert "notlage" not in json.dumps(nh._store.data) and "1" not in nh._guild(77)["aus"]
        assert nh.note_message(_nachricht("morgen schreib ich die nachklausur", uid=1)) is False
        _offen(nh, uid=1, was="die Nachklausur", ab=jetzt - timedelta(hours=1))
        kanal = _Kanal(cid=4799)
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=kanal))) is False
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", uid=2, kanal=kanal)))
        uhren.vor(72 * 3600 - 60)
        _offen(nh, uid=1, was="die Fahrprüfung",
               ab=datetime.fromtimestamp(uhren.wand - 3600, uhr.TIMEZONE))
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=kanal))) is False
        uhren.vor(120)
        asyncio.run(nh.tick(_CLIENT))
        assert not nh._notlage, "abgelaufene Sperren bleiben liegen"
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=kanal)))
        assert len(kanal.gesendet) == 2
        nh._enabled = False
        assert nachhaken.notlage(77, 1) == 0


def test_nachhaken_notlage_oder_vergiss_waehrend_die_ki_schreibt():
    """Die KI braucht Sekunden. Kommt in der Zeit ein Notsignal oder 'vergiss
    mich', geht nichts raus und nichts wird gemerkt - auch nicht, was gerade
    zur Einordnung bei der KI war (vergiss() sieht es dort nicht mehr)."""
    jetzt = datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)
    for stoerung in ("notlage", "vergiss"):
        with _nh_frisch(jetzt) as (nh, _uhren):
            def dazwischen(_prompt):
                getattr(nachhaken, stoerung)(77, 1)
                return "Na Anna, wie lief die Klausur? Ich wart schon ewig."
            with mock.patch.object(ai, "generate", _KiAttrappe(dazwischen)):
                _offen(nh, uid=1, was="die Klausur")
                kanal = _Kanal(cid=4798)
                assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=kanal))) \
                    is False, stoerung
                assert not kanal.gesendet and nh._guild(77)["offen"] == [], stoerung

            def einordnen(_prompt):
                getattr(nachhaken, stoerung)(77, 1)
                return _antwort((1, True, "die Klausur", False), (2, True, "das Turnier", False))
            nh._notlage.clear()
            with mock.patch.object(ai, "generate", _KiAttrappe(einordnen)):
                assert nh.note_message(_nachricht("morgen schreib ich die klausur", uid=1))
                assert nh.note_message(_nachricht("morgen spiel ich das turnier", uid=2))
                assert asyncio.run(nh.tick(_CLIENT)) == 1, stoerung
                assert [e["uid"] for e in nh._guild(77)["offen"]] == [2], stoerung
            # Wer NACH dem Vergessen wieder etwas ankuendigt, ist wieder dabei.
            if stoerung == "vergiss":
                nh._runde.clear()
                assert nh.note_message(_nachricht("morgen schreib ich die nachklausur"))
                with mock.patch.object(ai, "generate", _KiAttrappe(
                        _antwort((1, True, "die Nachklausur", False)))):
                    assert asyncio.run(nh.tick(_CLIENT)) == 1


def test_nachhaken_nicht_im_aufraeum_kanal_und_nicht_ohne_schreibrecht():
    """Im Aufraeum-Kanal loescht bot.py auch Flos Antwort nach einer Minute -
    mit ihr waere die eine Nachfrage des Tages weg. Dort (und wo Flo nicht
    schreiben darf) fragt er nicht und bucht nichts; die naechste Nachricht
    in einem normalen Kanal fragt."""
    import guildcfg
    echt = guildcfg.get

    def einstellung(gid, key):
        return [4790] if key == "autodelete_channels" else echt(gid, key)

    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, _uhren), \
            mock.patch.object(ai, "generate", _KiAttrappe("Na, wie war die Klausur, du Held?")), \
            mock.patch.object(guildcfg, "get", einstellung):
        _offen(nh, uid=1, was="die Klausur")
        aufraeumen = _Kanal(cid=4790)
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=aufraeumen))) is False
        stumm = _Kanal(cid=4791)
        stumm.permissions_for = lambda _ich: SimpleNamespace(send_messages=False)
        msg = _nachricht("hey", kanal=stumm)
        msg.guild.me = SimpleNamespace(id=1)
        assert asyncio.run(nh.vielleicht_nachfragen(msg)) is False
        assert not aufraeumen.gesendet and not stumm.gesendet
        assert len(nh._guild(77)["offen"]) == 1 and nh._guild(77)["gefragt"] == {}
        normal = _Kanal(cid=4792)
        normal.permissions_for = lambda _ich: SimpleNamespace(send_messages=True)
        msg = _nachricht("hey", kanal=normal)
        msg.guild.me = SimpleNamespace(id=1)
        assert asyncio.run(nh.vielleicht_nachfragen(msg))
        assert len(normal.gesendet) == 1
    # Threads brauchen das Thread-Recht.
    faden = mock.MagicMock(spec=discord.Thread)
    faden.id = 4793
    faden.permissions_for = lambda _ich: SimpleNamespace(send_messages=True,
                                                         send_messages_in_threads=False)
    guild = SimpleNamespace(id=77, me=SimpleNamespace(id=1))
    assert nachhaken.Nachhaken._kanal_taugt(guild, faden) is False
    assert nachhaken.Nachhaken._kanal_taugt(guild, None) is False


def test_nachhaken_fragt_nicht_oeffentlich_was_im_hinterzimmer_gesagt_wurde():
    """Wer im Mod-Kanal, in einem privaten Thread oder einem NSFW-Kanal vom
    Vorstellungsgespraech erzaehlt, hat sich sein Publikum ausgesucht. Flo
    fragt dann nicht im oeffentlichen Chat nach (und bucht nichts) - nur dort,
    wo es gesagt wurde. Wie bei den Zitaten (basis.privat). Gibt es den
    Kanal nicht mehr (geloescht, Thread archiviert), auch nicht."""
    jeder, mods = SimpleNamespace(id=77), SimpleNamespace(id=9)
    kanaele = {}

    def raum(cid, lesen, *, nsfw=False, typ=None):
        k = _Kanal(cid=cid)
        k.permissions_for = lambda wer: SimpleNamespace(
            send_messages=True, send_messages_in_threads=True,
            read_messages=getattr(wer, "id", None) in lesen)
        k.is_nsfw = lambda: nsfw
        if typ is not None:
            k.type = typ
        kanaele[cid] = k
        return k

    server = SimpleNamespace(id=77, me=SimpleNamespace(id=1), roles=[jeder, mods],
                             default_role=jeder, get_channel_or_thread=kanaele.get)
    oeffentlich = raum(555, {77, 9})
    allgemein = raum(556, {77, 9})
    modkanal = raum(900, {9})
    faden = raum(901, {77, 9}, typ=discord.ChannelType.private_thread)
    schmuddel = raum(902, {77, 9}, nsfw=True)

    def fragen(text, uid, kanal):
        msg = _nachricht(text, uid=uid, name=f"P{uid}", kanal=kanal)
        msg.guild = server
        return asyncio.run(nh.vielleicht_nachfragen(msg))

    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, _uhren), \
            mock.patch.object(ai, "generate", _KiAttrappe("Na, wie war's, du Held?")):
        ab = datetime(2026, 10, 22, 17, 0, tzinfo=uhr.TIMEZONE).timestamp()
        for uid, quelle in ((1, modkanal), (2, faden), (3, schmuddel), (4, 999),
                            (5, oeffentlich)):
            nh._eintragen(77, {"uid": uid, "name": f"P{uid}", "wann": "morgen",
                               "kanal": getattr(quelle, "id", quelle), "faellig": ab},
                          "das Vorstellungsgespräch")
        for uid in (1, 2, 3, 4):        # 999: den Kanal gibt es nicht mehr
            assert fragen("moin leute", uid, oeffentlich) is False, uid
        assert oeffentlich.gesendet == [], "nichts aus dem Hinterzimmer im Chat"
        assert len(nh._guild(77)["offen"]) == 5 and nh._guild(77)["gefragt"] == {}
        # Dort, wo es gesagt wurde, fragt er.
        assert fragen("bin wieder da", 1, modkanal)
        assert len(modkanal.gesendet) == 1
        # Oeffentlich angekuendigt: auch in einem anderen oeffentlichen Kanal.
        assert fragen("moin", 5, allgemein)
        assert len(allgemein.gesendet) == 1
        assert sorted(e["uid"] for e in nh._guild(77)["offen"]) == [2, 3, 4]
    assert basis.privat(modkanal, oeffentlich, server)
    assert not basis.privat(oeffentlich, modkanal, server), "nach innen ist nichts privat"


def test_nachhaken_senden_scheitert_legt_die_sache_zurueck():
    async def kaputt(*_a, **_k):
        return None

    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, uhren), \
            mock.patch.object(ai, "generate", _KiAttrappe()):
        _offen(nh, uid=1, was="die Klausur")
        kanal = _Kanal(cid=4797)
        with mock.patch.object(basis, "antworte", kaputt):
            assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=kanal))) is False
        eintrag = nh._guild(77)
        assert [e["was"] for e in eintrag["offen"]] == ["die Klausur"]
        assert eintrag["heute"]["n"] == 0 and eintrag["gefragt"] == {"1": "2026-10-23"}
        assert kanal.id not in ai.instance._HISTORY, "nie gesendet, aber im Verlauf"
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=kanal))) is False
        uhren.stellen(datetime(2026, 10, 24, 12, 0, tzinfo=uhr.TIMEZONE))
        assert asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", kanal=kanal)))


def test_nachhaken_server_deckel_haelt_auch_nach_vergiss_mich():
    """'vergiss mich' loescht, wann die Person gefragt wurde - der Server-Deckel
    zaehlt ohne Namen weiter: kein viertes Mal am selben Tag."""
    with _nh_frisch(datetime(2026, 10, 23, 12, 0, tzinfo=uhr.TIMEZONE)) as (nh, _uhren), \
            mock.patch.object(ai, "generate", _KiAttrappe()):
        for uid in (1, 2, 3, 4):
            _offen(nh, uid=uid, was="die Klausur")
        kanal = _Kanal(cid=4796)

        def frag(uid):
            return asyncio.run(nh.vielleicht_nachfragen(_nachricht("hey", uid=uid, kanal=kanal)))

        assert frag(1) and frag(2) and frag(3)
        nachhaken.vergiss(77, 1)
        nh.abmelden(77, 2)
        assert "1" not in nh._guild(77)["gefragt"] and "2" not in nh._guild(77)["gefragt"]
        assert not frag(4), "viertes Mal auf dem Server"
        # Ein Stand von morgen (vor dem Uhrsprung beim Booten) zaehlt fuer heute weiter.
        nh._guild(77)["heute"] = {"tag": "2026-10-24", "n": 3}
        assert not frag(4)
        nh._guild(77)["heute"] = {"tag": "2026-10-22", "n": 3}
        assert frag(4)
        nh._guild(77)["heute"] = {"tag": "kaputt", "n": "drei"}
        assert nachhaken.Nachhaken._server_heute(nh._guild(77), "2026-10-23") == 0


def test_nachhaken_ohne_ki_chat_geht_keine_zeile_an_die_ki():
    """Ist der KI-Chat auf dem Server aus, sammelt Nachhaken keine Rohzeilen
    und schickt keine an die KI - gefragt wird dann nur aus dem Ersatz-Pool."""
    ki = _KiAttrappe(_antwort((1, True, "die Klausur", False)))
    with _nh_frisch() as (nh, _uhren), mock.patch.object(ai, "generate", ki):
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "ki"):
            assert nh.note_message(_nachricht("morgen schreib ich die klausur")) is False
        assert nh.note_message(_nachricht("morgen schreib ich die klausur"))
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "ki"):
            asyncio.run(nh.tick(_CLIENT))
        assert not ki.aufrufe and not nh._kandidaten.get(77)


def test_laune_beleidigt_klingt_in_der_antwort_nach():
    """bot.py zaehlt die Ansprache (die 'beleidigt' beendet), BEVOR die KI
    antwortet. Die Antwort auf genau diese Ansprache laesst es trotzdem
    raushaengen: die Zeile klingt zwei Minuten nach - genau einmal im Prompt,
    vor Nacht und Tageslaune, nie vor einem neuen Ereignis, nie in der Notlage."""
    with _laune_frisch() as (lu, uhren):
        lu._event_setzen(77, "beleidigt", "unbeachtet")
        laune.nachricht(_nachricht("flo bist du da", uid=9), True)
        assert laune.stand(77) == "", "beleidigt ist vorbei"
        p = ai.instance._system_prompt(author="Tester", title="", gid=77)
        assert p.count("Deine Laune gerade") == 1, p
        zeile = laune.prompt_zeile(77)
        assert zeile.startswith(" Deine Laune gerade: beleidigt,") and "endlich mal einer" in zeile
        assert "schnippisch" in zeile and zeile.endswith(laune._PROMPT_SCHLUSS), zeile
        assert zeile in p and p.index(zeile) < p.index("LETZTES WORT")
        assert zeile not in ai.instance._system_prompt(author="T", title="", gid=77,
                                                       notlage=True)
        uhren.vor(laune.NACHKLANG + 1)
        assert laune.prompt_zeile(77) == "" and not lu._nachklang
        # Nachts: der Nachklang schlaegt die Muedigkeit, ein neues Ereignis ihn.
        uhren.stellen(datetime(2026, 10, 22, 3, 0, tzinfo=uhr.TIMEZONE))
        lu._event_setzen(77, "beleidigt", "stille")
        laune.nachricht(_nachricht("flo du vollidiot", uid=9), True)
        assert laune.stand(77) == "gereizt"
        assert laune.prompt_zeile(77).startswith(" Deine Laune gerade: gereizt")
        lu._guild(77)["event"] = {}
        assert "beleidigt" in laune.prompt_zeile(77) and "muede" not in laune.prompt_zeile(77)
    # Der ganze Weg: bot.on_message -> laune.nachricht -> ai.ask_flo.
    import bot
    from test_fundament import _menschen_nachricht, _nur_geladen
    prompts = []

    async def ask_flo(_frage, **kw):
        prompts.append(ai.instance._system_prompt(author="T", title="", gid=kw.get("gid")))
        return "Ach, jetzt redet ihr wieder mit mir?"

    with _laune_frisch() as (lu, _uhren), _nur_geladen(bot, "LAUNEN_ENABLED", AI_ENABLED=True), \
            mock.patch.object(bot.ai, "ask_flo", ask_flo):
        lu._event_setzen(77, "beleidigt", "stille")
        msg = _menschen_nachricht("flo bist du da", mid=721)
        try:
            asyncio.run(bot.client.on_message(msg))
        finally:
            ai.instance._HISTORY.pop(msg.channel.id, None)
    assert len(prompts) == 1, prompts
    assert prompts[0].count("Deine Laune gerade: beleidigt") == 1


if __name__ == "__main__":
    run(globals())
