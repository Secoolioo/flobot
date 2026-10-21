"""Das Fundament fuer Playlists, Musik-Plus, Flo lebt und Server-Leben.

Die Uhr, die Spruenge uebersteht (uhr.py), die Gerueste der neuen Module und
ihre Verdrahtung in bot.py, ai.py und music.py - alles, was die Teile danach
nicht mehr anfassen muessen. Dazu der gemeinsame Rauchtest (RAUCHPROBEN).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_fundament.py      nur diese Datei
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _Klick, _klick_im_voice, _ki_frisch, _musik_umgebung,
    _rauch_nachricht, _track, _verlauf_frisch, _voice_kanal, _VoiceChannelStub,
    _with_economy)

import contextlib
import logging
from datetime import datetime, time as dtime, timedelta, timezone
from unittest import mock

import discord

import geburtstage
import laune
import musikstats
import musikzustand
import nachhaken
import playlists
import uhr
import zitate

#: Die Module aus dem Fundament mit ihrem Funktionsschalter. Playlists und
#: Wrapped haengen am Musik-Schalter.
NEUE_MODULE = {
    playlists: "music", musikstats: "music", laune: "launen",
    nachhaken: "nachhaken", zitate: "zitate", geburtstage: "geburtstage",
}


class _Mitschnitt:
    """Faengt Log-Eintraege eines Loggers mit (als Kontext)."""

    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.eintraege = []
        self.griff = logging.Handler(level=logging.DEBUG)
        self.griff.emit = self.eintraege.append

    def __enter__(self):
        self.alt_stufe = self.logger.level
        self.logger.addHandler(self.griff)
        self.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *_a):
        self.logger.removeHandler(self.griff)
        self.logger.setLevel(self.alt_stufe)
        return False

    def ab(self, stufe):
        return [e for e in self.eintraege if e.levelno >= stufe]


@contextlib.contextmanager
def _mit_setup(modul):
    """modul.setup() fahren und danach den Zustand der Instanz zuruecklegen -
    sobald ein Teil sein Modul fuellt, legt setup() Speicher und Schalter an."""
    kern = getattr(modul, "instance", None)
    alt = dict(vars(kern)) if kern is not None else None
    try:
        yield modul.setup()
    finally:
        if alt is not None:
            vars(kern).clear()
            vars(kern).update(alt)


@contextlib.contextmanager
def _nur_geladen(bot, *namen, **werte):
    """Alle *_ENABLED-Schalter in bot.py aus - ausser den genannten (die an).
    Sonst laufen in on_message die Haken aller Module mit, die der Test gar
    nicht meint (XP, Wortzaehler, Aktie ...), und ihr Zustand faerbt ab."""
    with contextlib.ExitStack() as stapel:
        for name in dir(bot):
            if name.endswith("_ENABLED") and isinstance(getattr(bot, name), bool):
                stapel.enter_context(mock.patch.object(bot, name, name in namen))
        for name, wert in werte.items():
            stapel.enter_context(mock.patch.object(bot, name, wert))
        stapel.enter_context(mock.patch.object(bot.admin, "is_locked", lambda: False))
        yield


# --- Die Uhr -------------------------------------------------------------------
def test_uhr_schluessel_laufen_nur_vorwaerts():
    """Die Schluessel sind so gebaut, dass die Textreihenfolge die zeitliche ist -
    und verglichen wird NUR mit spaeter_als, nie mit '!='."""
    dt = datetime(2026, 1, 3, 12, 0, tzinfo=uhr.TIMEZONE)
    assert uhr.tag_key(dt) == "2026-01-03"
    assert uhr.monat_key(dt) == "2026-01"
    assert uhr.vormonat_key(dt) == "2025-12"            # Jahreswechsel rueckwaerts
    assert uhr.vormonat_key(datetime(2026, 3, 31, tzinfo=uhr.TIMEZONE)) == "2026-02"
    assert uhr.woche_key(dt) == "2026-W01"
    # ISO-Jahr, nicht Kalenderjahr: der 29.12.2025 gehoert zu 2026-W01.
    assert uhr.woche_key(datetime(2025, 12, 29, tzinfo=uhr.TIMEZONE)) == "2026-W01"
    assert uhr.woche_key(datetime(2026, 10, 21, tzinfo=uhr.TIMEZONE)) == "2026-W43"

    sp = uhr.spaeter_als
    assert sp("2026-10-22", "2026-10-21") and not sp("2026-10-21", "2026-10-21")
    assert not sp("2026-10-21", "2026-10-22"), "zurueck ist NIE neu"
    assert sp("2027-01", "2026-12") and sp("2027-W01", "2026-W53")
    assert sp("2026-10-21", "") and sp("2026-10", None), "noch nie = alles ist neu"
    assert not sp("", "2026-10") and not sp(None, None)
    # Die Ortszeit kommt von derselben Uhr wie stabil() - im Test stellbar.
    assert uhr.jetzt().tzinfo is not None and uhr.tag_key() == uhr.jetzt().strftime("%Y-%m-%d")


def test_uhr_stabil_erst_nach_der_startgnade_und_nicht_nach_einem_sprung():
    """Der Server-Sprung: ~15 s nach dem Booten zwei Stunden zurueck. Wer nach
    der Wanduhr plant, wartet die Startgnade ab - und nach jedem Sprung noch
    einmal SPRUNG_GNADE lang."""
    wand, mono = [1_800_000_000.0], [1000.0]
    u = uhr.Uhr(zeit=lambda: wand[0], mono=lambda: mono[0])

    def weiter(sek, sprung=0.0):
        mono[0] += sek
        wand[0] += sek + sprung

    assert u.stabil() is False                         # gerade gestartet
    weiter(uhr.START_GNADE - 1)
    assert u.stabil() is False
    weiter(2)
    assert u.stabil() is True

    # Kleines Nachstellen (NTP-Sekunden) ist kein Sprung.
    weiter(10, sprung=+3)
    assert u.stabil() is True

    # Zwei Stunden zurueck: ab jetzt SPRUNG_GNADE lang nicht stabil ...
    with _Mitschnitt("dcbot.uhr") as mit:
        weiter(10, sprung=-7200)
        assert u.stabil() is False
        weiter(uhr.SPRUNG_GNADE - 1)
        assert u.stabil() is False
        weiter(2)
        assert u.stabil() is True
    # ... und der Sprung steht EINMAL im Log, nicht bei jedem Aufruf.
    assert len(mit.ab(logging.WARNING)) == 1, [e.getMessage() for e in mit.eintraege]
    # Die Schluessel folgen der gestellten Uhr.
    assert u.tag_key() == datetime.fromtimestamp(wand[0], uhr.TIMEZONE).strftime("%Y-%m-%d")

    # Vorwaerts zaehlt genauso.
    weiter(10, sprung=+7200)
    assert u.stabil() is False


def test_relative_loops_planen_nach_dem_uhrsprung_ab_jetzt():
    """discord.py plant relative Loops als 'letzte Soll-Zeit + Takt' in
    WANDUHR-Zeit. Nach dem Sprung zurueck lag die naechste Runde zwei Stunden
    in der Zukunft: Giveaways, Voice-Watchdog, Status & Co. schliefen so lange.
    bot.py flickt das beim Import (uhr.zeitsprungfest_machen)."""
    from discord.ext import tasks
    import bot  # noqa: F401 - flickt beim Import

    async def koerper():
        return None

    schleife = tasks.loop(seconds=15)(koerper)
    jetzt = datetime.now(timezone.utc)
    schleife._last_iteration = jetzt + timedelta(hours=2)    # vor dem Sprung geplant
    naechste = schleife._get_next_sleep_time()
    assert naechste - jetzt <= timedelta(seconds=15.5), naechste - jetzt

    # Feste Uhrzeiten bleiben discord.py's eigene Sache: von 01:00 aus ist die
    # naechste 02:00-Runde heute, von 03:00 aus morgen.
    fest = tasks.loop(time=dtime(hour=2, tzinfo=uhr.TIMEZONE))(koerper)
    nachts = datetime(2026, 10, 21, 1, 0, tzinfo=uhr.TIMEZONE)
    assert fest._get_next_sleep_time(nachts) == datetime(2026, 10, 21, 2, 0,
                                                         tzinfo=uhr.TIMEZONE)
    morgens = datetime(2026, 10, 21, 3, 0, tzinfo=uhr.TIMEZONE)
    assert fest._get_next_sleep_time(morgens) == datetime(2026, 10, 22, 2, 0,
                                                          tzinfo=uhr.TIMEZONE)


def test_relativer_loop_ueberlebt_den_sprung_mitten_im_takt():
    """Faellt der Sprung genau in den Rumpf eines Loops, ist die naechste Runde
    schon geplant - zwei Stunden 'in der Zukunft'. Auch dann schlaeft ein
    relativer Loop hoechstens einen Takt."""
    from discord.ext import tasks
    import bot  # noqa: F401 - flickt beim Import
    runden = []

    async def koerper():
        runden.append(time.monotonic())
        if len(runden) == 1:
            schleife._next_iteration += timedelta(hours=2)   # der Sprung
        else:
            schleife.stop()

    schleife = tasks.loop(seconds=0.05)(koerper)

    async def lauf():
        schleife.start()
        await asyncio.wait_for(schleife._task, 3)

    asyncio.run(lauf())
    assert len(runden) == 2 and runden[1] - runden[0] < 1.0, runden


def test_zeitsprung_flicken_nur_bei_bekanntem_innenleben():
    """Das Flicken haengt an discord.py-Innenleben, und requirements.txt laesst
    alles bis <3 zu. Passt es nicht mehr: EINE Warnung, nichts angefasst - nie
    ein Absturz beim Start."""
    from discord.ext import tasks
    import bot  # noqa: F401 - flickt beim Import
    frisch = uhr.Uhr()

    with _Mitschnitt("dcbot.uhr") as mit:
        # 1) Die Methode gibt es nicht mehr.
        with mock.patch.object(tasks.Loop, "_get_next_sleep_time", None):
            assert frisch.zeitsprungfest_machen() is False
            assert tasks.Loop._get_next_sleep_time is None
        # 2) Ein Loop sieht innen anders aus (kein _sleep mehr).
        def fremd(_self, now=None):
            return None
        with mock.patch.object(tasks.Loop, "_get_next_sleep_time", fremd), \
                mock.patch.object(uhr.Uhr, "_probe_loop",
                                  staticmethod(lambda: SimpleNamespace())):
            assert frisch.zeitsprungfest_machen() is False
            assert tasks.Loop._get_next_sleep_time is fremd
    assert len(mit.ab(logging.WARNING)) == 2, [e.getMessage() for e in mit.eintraege]

    # Schon geflickt: True, und es wird nicht doppelt gewickelt.
    vorher = (tasks.Loop._get_next_sleep_time, tasks.Loop._try_sleep_until)
    assert frisch.zeitsprungfest_machen() is True
    assert (tasks.Loop._get_next_sleep_time, tasks.Loop._try_sleep_until) == vorher


# --- Anmeldung der neuen Module -----------------------------------------------
def test_neue_module_sind_ueberall_angemeldet():
    """Jedes neue Modul: im Panel-Katalog, in FEATURE_LOADED, in der Kette und
    den Sentinels, seine Befehle in cmdnorm.KNOWN (und dort nicht verbogen),
    eine Zeile in der Hilfe. Beim Import tut keins etwas - setup() ist ein
    bool, und ohne setup() antwortet keins."""
    import bot
    import cmdnorm
    import features
    katalog = {f["key"] for f in features.CATALOG}
    for key in ("launen", "nachhaken", "zitate", "geburtstage"):
        assert key in katalog and key in bot.FEATURE_LOADED, key
    quelle = open("bot.py", encoding="utf-8").read()
    hilfe = " ".join(b for _t, _f, zeilen in bot._HELP_DATA.values() for b, _ in zeilen)
    for modul, schalter in NEUE_MODULE.items():
        assert f'_on("{schalter}")' in quelle, schalter
        assert f"{modul.__name__}.handle)" in quelle, modul.__name__
        assert modul.HANDLED is bot.basis.HANDLED
        for wort in modul._CMDS:
            assert wort in cmdnorm.KNOWN, wort
            assert cmdnorm.normalize(f"{wort} x") is None, wort
        assert any(f"flo {wort}" in hilfe for wort in modul._CMDS), (
            f"{modul.__name__}: kein Befehl steht in der Hilfe")
        with _mit_setup(modul) as an:
            assert isinstance(an, bool)
            antwort = asyncio.run(modul.handle(_rauch_nachricht("hahaha ja klar")))
        assert antwort is None, modul.__name__
    for wort in ("radio", "endlosradio", "autoplay", "geburtstag", "playlist"):
        assert wort in cmdnorm.ZUSTAND, wort
    # Die neuen Hilfe-Seiten sind eigene Kategorien - und kein Alias, denn
    # 'playlist', 'zitate', 'geburtstage' sind selbst Befehle.
    assert {"playlist", "leben"} <= set(bot._HELP_DATA) and {"playlist", "leben"} <= set(bot._HELP_HINTS)
    assert not ({"playlist", "playlists", "leben", "zitate", "geburtstage"}
                & set(bot._HELP_CATEGORY_ALIASES))
    # Dauerhafte Knoepfe: die Liste in bot.py kennt die neuen Module.
    assert playlists in bot.DYNAMISCHE_MODULE and zitate in bot.DYNAMISCHE_MODULE
    # Und die neuen Einstellungen sind eingeordnet.
    import einstellungen
    import guildcfg
    for key in ("wrapped_channel", "dj_belohnung", "dj_rolle", "zitat_channel",
                "geburtstag_channel", "geburtstag_coins", "geburtstag_rolle"):
        assert guildcfg.instance.defn(key) is not None, key
        assert any(key in werte for werte in einstellungen.ZUORDNUNG.values()), key


def test_rechtsklick_als_zitat_speichern():
    """Dritter Eintrag im Apps-Menue. Dieselben Schranken wie die anderen
    (_rk_darf) - und eine Interaktion bekommt immer ein Wort, auch wenn das
    Zitate-Modul stolpert."""
    import bot
    bot.client._rechtsklick_zuletzt.clear()
    gerufen = []

    async def rechtsklick(interaction, nachricht):
        gerufen.append((interaction.user.id, nachricht.id))
        await interaction.response.send_message("Gespeichert.", ephemeral=True)

    nachricht = SimpleNamespace(id=9, content="Ich bin ein Genie", author=SimpleNamespace(id=2))
    # Aus: abgeschaltet, das Modul wird nicht gefragt.
    with mock.patch.object(bot, "ZITATE_ENABLED", False), \
            mock.patch.object(bot.admin, "is_locked", lambda: False), \
            mock.patch.object(bot.zitate, "rechtsklick", rechtsklick):
        klick = _Klick(uid=31, gid=77)
        asyncio.run(bot.client._rk_zitat(klick, nachricht))
    assert not gerufen and "abgeschaltet" in klick.protokoll[0][1]

    with mock.patch.object(bot, "ZITATE_ENABLED", True), \
            mock.patch.object(bot.admin, "is_locked", lambda: False), \
            mock.patch.object(bot.features, "is_on_in", lambda _g, _k: True), \
            mock.patch.object(bot.zitate, "rechtsklick", rechtsklick):
        klick = _Klick(uid=32, gid=77)
        asyncio.run(bot.client._rk_zitat(klick, nachricht))
        assert gerufen == [(32, 9)] and klick.arten() == ["send"]

        async def kaputt(_interaction, _nachricht):
            raise RuntimeError("absichtlich")

        with mock.patch.object(bot.zitate, "rechtsklick", kaputt):
            klick = _Klick(uid=33, gid=77)
            asyncio.run(bot.client._rk_zitat(klick, nachricht))
        assert klick.arten() == ["send"] and "schiefgelaufen" in klick.protokoll[0][1]
    bot.client._rechtsklick_zuletzt.clear()


# --- on_message: wo die neuen Haken sitzen ------------------------------------
def test_zitat_abstimmung_geht_an_zitate_statt_an_umfrage():
    """Flos eigene Zitat-Abstimmung gehoert dem Zitate-Modul. Andere Umfragen
    von Flo gehen wie bisher an umfrage, fremde Umfragen an niemanden."""
    import bot
    zitat, umfrage_ = [], []

    async def zitat_ergebnis(msg):
        zitat.append(msg.id)

    async def umfrage_ergebnis(msg):
        umfrage_.append(msg.id)

    alt_user = bot.client._connection.user
    bot.client._connection.user = SimpleNamespace(id=1)
    try:
        with _nur_geladen(bot, "ZITATE_ENABLED", "UMFRAGE_ENABLED"), \
                mock.patch.object(bot.zitate, "ist_eigene_umfrage", lambda m: m.id == 501), \
                mock.patch.object(bot.zitate, "ergebnis", zitat_ergebnis), \
                mock.patch.object(bot.umfrage, "ergebnis", umfrage_ergebnis):
            async def lauf():
                for autor_id, mid in ((1, 501), (1, 502), (2, 501), (2, 503)):
                    msg = _rauch_nachricht("")
                    msg.id = mid
                    msg.author = SimpleNamespace(id=autor_id, bot=True, display_name="x")
                    msg.type = discord.MessageType.poll_result
                    await bot.client.on_message(msg)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            asyncio.run(lauf())
    finally:
        bot.client._connection.user = alt_user
    assert zitat == [501], zitat
    assert umfrage_ == [502], umfrage_


def _menschen_nachricht(text, mid=700):
    msg = _rauch_nachricht(text)
    msg.id = mid
    msg.type = discord.MessageType.default
    return msg


def test_playlist_assistent_kommt_nach_giveaway_und_vor_der_ansprache():
    """Der Assistent sitzt direkt hinter dem Giveaway-Assistenten. Sagt er
    'meins', ist die Nachricht fertig - kein Befehl, keine KI, auch nicht bei
    'flo stop' (das beendet den Assistenten, nicht die Musik)."""
    import bot
    reihenfolge = []

    async def giveaway_haken(msg):
        reihenfolge.append("giveaway")
        return False

    async def playlist_haken(msg):
        reihenfolge.append("playlist")
        return msg.content.endswith("stop")

    async def musik(msg):
        reihenfolge.append("music")
        return None

    with _nur_geladen(bot, "GIVEAWAY_ENABLED", "MUSIC_ENABLED", "PLAYLISTS_ENABLED"), \
            mock.patch.object(bot.giveaway, "on_message_passive", giveaway_haken), \
            mock.patch.object(bot.playlists, "on_message_passive", playlist_haken), \
            mock.patch.object(bot.music, "handle", musik):
        asyncio.run(bot.client.on_message(_menschen_nachricht("flo stop")))
        assert reihenfolge == ["giveaway", "playlist"], reihenfolge
        reihenfolge.clear()
        asyncio.run(bot.client.on_message(_menschen_nachricht("flo spiel despacito")))
        assert reihenfolge == ["giveaway", "playlist", "music"], reihenfolge
        # Musik auf diesem Server aus: dann gibt es auch keinen Assistenten.
        reihenfolge.clear()
        with mock.patch.object(bot.features, "fuer",
                               lambda _g: (lambda key: key != "music")):
            asyncio.run(bot.client.on_message(_menschen_nachricht("flo stop")))
        assert "playlist" not in reihenfolge, reihenfolge


def test_launen_und_nachhaken_sehen_jede_nachricht_ausser_notlagen():
    """laune.nachricht sieht jede Nachricht (mit 'angesprochen'), nachhaken
    notiert vor der Ansprache und fragt bei NICHT angesprochenen nach - aber
    keins von beiden reagiert je auf eine Notlage. Das entscheidet bot.py
    zentral, nicht erst jedes Modul."""
    import bot
    gesehen = []

    def laune_haken(msg, angesprochen):
        gesehen.append(("laune", msg.id, angesprochen))

    def notiz(msg):
        gesehen.append(("notiz", msg.id))

    async def nachfragen(msg):
        gesehen.append(("nachfragen", msg.id))
        return False

    with _nur_geladen(bot, "LAUNEN_ENABLED", "NACHHAKEN_ENABLED"), \
            mock.patch.object(bot.laune, "nachricht", laune_haken), \
            mock.patch.object(bot.nachhaken, "note_message", notiz), \
            mock.patch.object(bot.nachhaken, "vielleicht_nachfragen", nachfragen):
        async def lauf(text, mid):
            await bot.client.on_message(_menschen_nachricht(text, mid))
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(lauf("morgen schreib ich die mathe pruefung", 1))
        assert gesehen == [("notiz", 1), ("laune", 1, False), ("nachfragen", 1)], gesehen
        gesehen.clear()
        asyncio.run(lauf("flo wie spaet ist es", 2))
        assert ("laune", 2, True) in gesehen and ("nachfragen", 2) not in gesehen, gesehen
        gesehen.clear()
        for nr, text in enumerate(("ich will nicht mehr leben",
                                   "flo ich will nicht mehr leben"), 3):
            asyncio.run(lauf(text, nr))
        assert gesehen == [], f"Automatik hat auf eine Notlage reagiert: {gesehen}"
        # Sendepause: kein Nachfragen (das waere Flo, der von sich aus redet).
        with mock.patch.object(bot, "ADMIN_ENABLED", True), \
                mock.patch.object(bot.admin, "is_locked", lambda: True):
            asyncio.run(lauf("morgen hab ich frei", 9))
        assert ("nachfragen", 9) not in gesehen, gesehen
        # Schalter aus: niemand sieht etwas.
        gesehen.clear()
        with mock.patch.object(bot.features, "fuer", lambda _g: (lambda key: False)):
            asyncio.run(lauf("morgen schreib ich die mathe pruefung", 10))
        assert gesehen == [], gesehen


# --- Takte und Herunterfahren ---------------------------------------------------
def test_alltag_takt_fragt_jedes_modul_einzeln():
    """Ein Modul, das stolpert, haelt die anderen nicht auf; was nicht geladen
    ist, wird nicht gefragt; ohne stabiles Gateway faellt die Runde aus."""
    import bot
    gerufen = []

    def tick_von(name, kaputt=False):
        async def tick(client):
            gerufen.append(name)
            if kaputt:
                raise RuntimeError("absichtlich")
        return tick

    alt_gw = (bot.client._gateway_weg_seit, bot.client._gateway_zurueck_um)
    try:
        with _nur_geladen(bot, "LAUNEN_ENABLED", "ZITATE_ENABLED", "GEBURTSTAGE_ENABLED",
                          "MUSIKSTATS_ENABLED"), \
                mock.patch.object(bot.laune, "tick", tick_von("laune", kaputt=True)), \
                mock.patch.object(bot.nachhaken, "tick", tick_von("nachhaken")), \
                mock.patch.object(bot.zitate, "tick", tick_von("zitate")), \
                mock.patch.object(bot.geburtstage, "tick", tick_von("geburtstage")), \
                mock.patch.object(bot.musikstats, "tick", tick_von("musikstats")), \
                _Mitschnitt("dcbot") as mit:
            bot.client._gateway_weg_seit, bot.client._gateway_zurueck_um = None, float("-inf")
            asyncio.run(bot.client.alltag_loop.coro(bot.client))
            assert gerufen == ["laune", "zitate", "geburtstage", "musikstats"], gerufen
            assert any("Launen-Takt" in e.getMessage() for e in mit.ab(logging.ERROR))
            gerufen.clear()
            bot.client._gateway_weg_seit = time.monotonic()           # Netz weg
            asyncio.run(bot.client.alltag_loop.coro(bot.client))
            assert gerufen == []
    finally:
        bot.client._gateway_weg_seit, bot.client._gateway_zurueck_um = alt_gw


def test_voice_takt_treibt_assistenten_und_musik_zustand():
    import bot
    gerufen = []

    async def pl_tick(client):
        gerufen.append("playlists")
        raise RuntimeError("absichtlich")

    async def takt():
        gerufen.append("musikzustand")

    async def heilen(_guild):
        gerufen.append("heal")

    with _nur_geladen(bot, "MUSIC_ENABLED", "PLAYLISTS_ENABLED", "MUSIKZUSTAND_ENABLED"), \
            mock.patch.object(bot.music, "heal_voice", heilen), \
            mock.patch.object(bot.playlists, "tick", pl_tick), \
            mock.patch.object(bot.musikzustand, "takt", takt):
        asyncio.run(bot.client.voice_heal_loop.coro(bot.client))
    gerufen = [g for g in gerufen if g != "heal"]
    assert gerufen == ["playlists", "musikzustand"], gerufen


def test_herunterfahren_sichert_warteschlange_und_assistenten():
    """Beides VOR store.alle_sichern - was sie dabei anstossen, schreibt der
    letzte Schritt gleich mit auf die Platte."""
    import bot
    schritte = []

    def schritt(name):
        async def lauf():
            schritte.append(name)
        return lauf

    with _nur_geladen(bot, "MUSIKZUSTAND_ENABLED", "PLAYLISTS_ENABLED"), \
            mock.patch.object(bot, "schulden", SimpleNamespace(instance=None)), \
            mock.patch.object(bot, "handel", SimpleNamespace(instance=None)), \
            mock.patch.object(bot.musikzustand, "sichern", schritt("musikzustand")), \
            mock.patch.object(bot.playlists, "offene_sichern", schritt("playlists")), \
            mock.patch.object(bot.store, "alle_sichern", schritt("speicher")):
        asyncio.run(bot.client._alles_sichern())
    assert schritte == ["musikzustand", "playlists", "speicher"], schritte


def test_status_zeigt_flos_laune_und_wechselt_sofort():
    """Der Status ist global - er zeigt die Laune des Hauptservers, hoechstens
    128 Zeichen. Wechselt die Laune, kommt der neue Status im naechsten Takt
    und nicht erst nach der Minute."""
    import bot
    gesetzt = []
    stand = ["gereizt"]

    async def presence(**kw):
        gesetzt.append(kw["activity"].name)

    c = bot.client
    alt = (c._status_gewechselt, c._weisheit_index, c._laune_im_status)
    try:
        with _nur_geladen(bot, "LAUNEN_ENABLED", GUILD_ID=77), \
                mock.patch.object(c, "change_presence", presence), \
                mock.patch.object(bot.laune, "stand", lambda gid: stand[0]), \
                mock.patch.object(bot.laune, "status_zeile",
                                  lambda gid, spruch: f"😤 {stand[0]} " + "x" * 300):
            c._status_gewechselt = float("-inf")
            asyncio.run(c.status_loop.coro(c))
            asyncio.run(c.status_loop.coro(c))         # gleich danach: keine Aenderung
            assert len(gesetzt) == 1 and len(gesetzt[0]) == 128, gesetzt
            assert gesetzt[0].startswith("😤 gereizt")
            stand[0] = "müde"                          # neue Laune
            asyncio.run(c.status_loop.coro(c))
            assert len(gesetzt) == 2 and gesetzt[1].startswith("😤 müde"), gesetzt
        # Ohne Laune (Geruest/aus) bleibt es beim normalen Spruch.
        with _nur_geladen(bot), mock.patch.object(c, "change_presence", presence):
            c._status_gewechselt = float("-inf")
            asyncio.run(c.status_loop.coro(c))
        assert gesetzt[-1] in bot.WEISHEITEN
    finally:
        c._status_gewechselt, c._weisheit_index, c._laune_im_status = alt


# --- KI: die Laune faerbt den Ton, nie die Notlage ------------------------------
_LAUNE_ZEILE = (
    " Deine Laune heute: gereizt, weil dich drei Leute in fuenf Minuten zugespammt "
    "haben - kuerzer angebunden, null Geduld, jeder Satz eine Spur genervter. Die "
    "Grenze von oben und die Notlagen-Regel gelten trotzdem immer, egal wie du "
    "drauf bist.")


def test_laune_faerbt_den_prompt_aber_nie_in_der_notlage():
    """Die Zeile steht hinter der Grenze und vor dem Schlusswort, die Anteile
    aus test_ki (Grenze vorne, Schlusshieb hinten) halten - und in einer Notlage
    fragt ai.py die Laune gar nicht erst."""
    import ai
    with mock.patch.object(laune, "prompt_zeile", lambda gid: _LAUNE_ZEILE):
        p = ai.instance._system_prompt(author="Tester", title="", gid=77)
        assert _LAUNE_ZEILE in p
        assert p.index("Herkunft") < len(p) * 0.5
        assert p.index("LETZTES WORT") > len(p) * 0.8
        assert p.index("Herkunft") < p.index(_LAUNE_ZEILE) < p.index("LETZTES WORT")
        assert p.rstrip().endswith("Nur die eine Grenze von oben bleibt.")
        ohne = ai.instance._system_prompt(author="Tester", title="", gid=77, notlage=True)
        assert _LAUNE_ZEILE not in ohne
        # Ohne Server (DM) gibt es keine Laune.
        assert _LAUNE_ZEILE not in ai.instance._system_prompt(author="Tester", title="")

        # ask_flo und see_image reichen die Notlage durch.
        flo, _anbieter = _ki_frisch([])
        gesehen = []
        echt = flo._system_prompt

        def mitschreiben(*a, **kw):
            gesehen.append(kw.get("notlage"))
            return echt(*a, **kw)

        flo._system_prompt = mitschreiben
        asyncio.run(flo.ask_flo("ich will nicht mehr leben", author="T", gid=77, uid=1))
        asyncio.run(flo.ask_flo("wie gehts dir", author="T", gid=77, uid=1))
        asyncio.run(flo.see_image("ich will nicht mehr leben", "http://x/b.png",
                                  author="T", gid=77, uid=1))
        assert gesehen[0] is True and gesehen[1] is False and gesehen[2] is True, gesehen


def test_hintergrund_pause_steht_einmal_im_journal():
    """Nach einem 429 fragen Gedaechtnis, Analyst und die neuen Takte in der
    Pause weiter - frueher stand dann bei jedem Aufruf dieselbe INFO-Zeile da.
    Jetzt einmal je Pause, danach nur noch im Debug-Log."""
    flo, anbieter = _ki_frisch([])
    flo._hintergrund_pause_bis = time.monotonic() + 300
    with _Mitschnitt("dcbot.ai") as mit:
        for _ in range(4):
            assert asyncio.run(flo.generate("x", hintergrund=True)) is None
        info = [e for e in mit.eintraege if e.levelno == logging.INFO
                and "ausgelassen" in e.getMessage()]
        assert len(info) == 1, [e.getMessage() for e in mit.eintraege]
        # Neue Pause (neuer 429) -> wieder genau eine Zeile.
        flo._hintergrund_pause_bis = time.monotonic() + 600
        asyncio.run(flo.generate("x", hintergrund=True))
        info = [e for e in mit.eintraege if e.levelno == logging.INFO
                and "ausgelassen" in e.getMessage()]
        assert len(info) == 2
    assert anbieter.aufrufe == [], "in der Pause darf nichts zum Anbieter"


# --- Musik: wer, und die Haken fuer den Neustart ---------------------------------
def test_track_und_play_many_tragen_die_wunsch_id():
    """requested_by ist nur ein Name - fuer 'den eigenen Wunsch skippen' und den
    DJ des Monats braucht es die ID, und ein Aufloesen darf sie nicht verlieren."""
    import music
    mi = music.instance
    player, voice, aufraeumen = _musik_umgebung()

    async def resolve(inp, hint):
        return music.Track(title=inp, stream_url="http://stream/x", duration=100)

    alt = mi._resolve_input
    mi._resolve_input = resolve
    try:
        items = [("eins", "Eins", None), ("zwei", "Zwei", None), ("drei", "Drei", None)]
        antwort = asyncio.run(mi._play_many(player, _VoiceChannelStub(), items, "Bob",
                                            "aus der Playlist", requested_by_id=55))
        assert antwort is music.HANDLED, antwort
        assert player.current.requested_by_id == 55
        assert [t.requested_by_id for t in player.queue] == [55, 55]
        # Laeuft schon was: eingereiht, ebenfalls mit ID.
        asyncio.run(mi._play_many(player, _VoiceChannelStub(), [("vier", "Vier", None)],
                                  "Bob", "aus der Playlist", requested_by_id=56))
        assert player.queue[-1].requested_by_id == 56
        # Ohne Angabe: 0 (niemand Bestimmtes) - alte Aufrufer laufen weiter.
        assert mi._lazy_track("x", "X", "Bob").requested_by_id == 0
        # Aufloesen behaelt Wunsch-ID, Radio- und Wiederherstellungs-Merker.
        lazy = music.Track(title="R", stream_url="", query="ytsearch1:r",
                           requested_by_id=7, radio=True, wiederhergestellt=True)
        fertig = asyncio.run(mi._resolve_track(lazy))
        assert (fertig.requested_by_id, fertig.radio, fertig.wiederhergestellt) == (7, True, True)
        # Der Player kennt die neuen Felder mit leeren Standards.
        frisch = music.GuildPlayer(loop=None)
        assert frisch.radio is False and frisch._radio_gesehen == [] \
            and frisch._skip_stimmen == set() and frisch._radio_hinweis is False
        assert frisch._radio_gesehen is not music.GuildPlayer(loop=None)._radio_gesehen
    finally:
        mi._resolve_input = alt
        aufraeumen()


def test_verlauf_merkt_sich_die_id_und_zaehlt_fuer_wrapped():
    """Der Verlauf schreibt 'wid' mit, und jeder neu gestartete Song geht an die
    Musik-Statistik - ausser einem, der nach einem Neustart nur weiterlaeuft."""
    music_mod, restore = _verlauf_frisch()
    gezaehlt = []
    try:
        with mock.patch.object(musikstats, "notieren",
                               lambda gid, track: gezaehlt.append((gid, track.title))):
            m = music_mod.instance
            m.verlauf_notieren(77, music_mod.Track(title="A", stream_url="",
                                                   requested_by="Bob", requested_by_id=9))
            m.verlauf_notieren(77, music_mod.Track(title="B", stream_url="",
                                                   wiederhergestellt=True))
            assert m.verlauf(77)[0]["wid"] == 0 and m.verlauf(77)[1]["wid"] == 9
            assert gezaehlt == [(77, "A")], gezaehlt
            # Eine kaputte Statistik kippt die Musik nicht.
            with mock.patch.object(musikstats, "notieren",
                                   lambda *_a: (_ for _ in ()).throw(RuntimeError("x"))):
                m.verlauf_notieren(77, music_mod.Track(title="C", stream_url=""))
            assert m.verlauf(77)[0]["t"] == "C"
    finally:
        restore()


def test_flache_playlist_kennt_ein_ende():
    """Radio und Playlist-Assistent brauchen nur ein paar Eintraege - yt-dlp
    soll dann nicht die ganze Liste abklappern."""
    import music
    mi = music.instance
    gesehen = []

    class _YDL:
        def __init__(self, opts):
            gesehen.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def extract_info(self, url, download=False):
            return {"entries": [{"id": "abc", "title": "A"}, None,
                                {"url": "https://soundcloud.com/x/y", "title": "B"}]}

    async def direkt(work, _quelle="YouTube"):
        return work("client")

    with mock.patch.object(music, "yt_dlp", SimpleNamespace(YoutubeDL=_YDL)), \
            mock.patch.object(mi, "_mit_clientwechsel", direkt):
        kurz = asyncio.run(mi._flache_playlist("https://youtube.com/playlist?list=x",
                                               quelle="Radio", ende=3))
        lang = asyncio.run(mi._flache_playlist("https://youtube.com/playlist?list=x"))
    assert gesehen[0]["playlistend"] == 3 and gesehen[1]["playlistend"] == music.MAX_QUEUE
    assert kurz == lang == [("https://www.youtube.com/watch?v=abc", "A"),
                            ("https://soundcloud.com/x/y", "B")]


def test_musik_merkt_sich_den_stand_an_den_richtigen_stellen():
    """musikzustand.merken nach start, Pause, Weiter, Einreihen, jedem
    Songwechsel, Verschieben und Parken - vergessen nur bei 'stop'. Und ein
    kaputtes musikzustand haelt die Musik nie an."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    player.guild_id = 77
    gemerkt, vergessen = [], []
    try:
        with mock.patch.object(musikzustand, "merken",
                               lambda p: gemerkt.append(p.guild_id)), \
                mock.patch.object(musikzustand, "vergessen", vergessen.append):
            player.start(_track("A"))
            assert len(gemerkt) == 1
            player.pausieren()
            player.fortsetzen()
            assert len(gemerkt) == 3
            b, c = _track("B"), _track("C")
            music.instance._einreihen(player, b)
            music.instance._einreihen(player, c)
            assert len(gemerkt) == 5
            ansicht = music.QueuePositionView.__new__(music.QueuePositionView)
            ansicht.player, ansicht.track = player, c
            ansicht.apply_move(0)
            assert len(gemerkt) == 6 and player.queue[0] is c
            voice.stop()
            player.queue.clear()
            asyncio.run(player._advance())             # Schlange leer -> trotzdem merken
            assert len(gemerkt) == 7
            player.queue.append(_track("D"))
            asyncio.run(player._parken(_VoiceChannelStub()))
            assert len(gemerkt) == 8 and player._geparkt
            asyncio.run(player.disconnect())
            assert vergessen == [77]
        # Wirft musikzustand, laeuft die Musik trotzdem.
        with mock.patch.object(musikzustand, "merken",
                               lambda p: (_ for _ in ()).throw(RuntimeError("x"))):
            player.voice = voice
            player.active_channel_id = 42
            voice.stop()
            player.start(_track("E"))
            assert player.current.title == "E"
    finally:
        aufraeumen()


# --- Kleinere Bausteine ---------------------------------------------------------
def test_an_flo_erkennt_die_ansprache_fuer_assistenten():
    """basis.an_flo: dieselbe Pruefung wie im Giveaway-Assistenten - Lead des
    Servers, 'hey flo', eine Erwaehnung von Flo oder seiner Rolle. Alles andere
    gehoert dem Assistenten."""
    import basis
    rolle = SimpleNamespace(id=4242)
    msg = SimpleNamespace(guild=SimpleNamespace(id=77, me=SimpleNamespace(id=1),
                                                self_role=rolle))
    for text in ("flo stop", "Flo, fertig", "hey flo fertig", "florian fertig",
                 "<@1> fertig", "<@!1> fertig", "<@&4242> fertig"):
        assert basis.an_flo(msg, text), text
    for text in ("despacito", "https://youtu.be/abc", "<@2> fertig", "<@&9> x",
                 "flohmarkt morgen", ""):
        assert not basis.an_flo(msg, text), text


def test_lotto_zieht_nach_uhrsprung_nicht_doppelt():
    """lotto.tick verglich den Monat mit '==': jede Abweichung war ein neuer
    Monat - auch einer aus der Zukunft. Mit dem Sprung beim Booten ueber einen
    Monatswechsel wurde derselbe Monat zwei- bis dreimal ausgelost."""
    import lotto
    lt = lotto.instance
    alt = (lt._store, lt._enabled, lt._win_chance)
    lt._store = _FakeStore({"month": "", "jackpot": 5_000_000, "ticket_price": 2500,
                            "entries": {}, "house": 0, "history": []})
    lt._enabled, lt._win_chance = True, 0.0
    monat = ["2026-11"]
    try:
        with mock.patch.object(lt, "_month_str", lambda: monat[0]), \
                mock.patch.object(lotto.uhr, "stabil", lambda: True):
            lt._store.data["month"] = "2026-10"
            assert asyncio.run(lt.tick()) is not None            # Oktober gezogen
            assert lt._state()["month"] == "2026-11"
            monat[0] = "2026-10"                                 # Uhr springt zurueck
            assert asyncio.run(lt.tick()) is None
            monat[0] = "2026-11"                                 # ... und wieder vor
            assert asyncio.run(lt.tick()) is None
            assert len(lt._state()["history"]) == 1
        # Monatswechsel, aber die Uhr ist noch nicht stabil: verschoben, nicht weg.
        monat[0] = "2026-12"
        with mock.patch.object(lt, "_month_str", lambda: monat[0]), \
                mock.patch.object(lotto.uhr, "stabil", lambda: False):
            assert asyncio.run(lt.tick()) is None
        assert lt._state()["month"] == "2026-11"
        with mock.patch.object(lt, "_month_str", lambda: monat[0]), \
                mock.patch.object(lotto.uhr, "stabil", lambda: True):
            assert asyncio.run(lt.tick()) is not None
        assert len(lt._state()["history"]) == 2
    finally:
        lt._store, lt._enabled, lt._win_chance = alt


def test_shop_und_aktienchart_laufen_nur_vorwaerts():
    """Der Shop wuerfelt beim Start nur neu, wenn der Shop-Tag SPAETER ist als
    der gespeicherte, und der Aktien-Chart schreibt einen Tag nur einmal - ein
    gespeicherter Tag aus der Zukunft (Uhrsprung) loest nichts mehr aus."""
    import floaktie
    restore = _with_economy({})
    e = economy.instance
    try:
        e._store.data["shop"] = {"date": "2999-01-01", "items": [{"n": 1, "x": "alt"}]}
        assert e.refresh_shop(force=False)["items"] == [{"n": 1, "x": "alt"}]
        e._store.data["shop"]["date"] = "2000-01-01"
        assert e.refresh_shop(force=False)["date"] == e._shop_tag()
        st = e.refresh_shop(force=True)
        assert st["date"] == e._shop_tag()
    finally:
        restore()

    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "", "act_ema": 0.0,
                            "msg_count": 0, "last_msg_count": 0, "holdings": {},
                            "history": [], "ticks": []})
    leer = SimpleNamespace(voice_channels=[], afk_channel=None, id=77)
    tag = ["2026-10-22"]
    try:
        with mock.patch.object(fa, "_today", lambda: tag[0]):
            asyncio.run(fa.sample_and_tick([leer], dt=20.0))
            tag[0] = "2026-10-21"                              # Uhr springt zurueck
            asyncio.run(fa.sample_and_tick([leer], dt=20.0))
            tag[0] = "2026-10-22"
            asyncio.run(fa.sample_and_tick([leer], dt=20.0))
            tag[0] = "2026-10-23"
            asyncio.run(fa.sample_and_tick([leer], dt=20.0))
        tage = [h["day"] for h in fa._store.data["history"]]
        assert tage == ["2026-10-22", "2026-10-23"], tage
    finally:
        fa._store, fa._enabled = alt


def test_testhilfe_kennt_voice_mit_leuten():
    """_voice_kanal ist ein echter VoiceChannel (heal & Co. pruefen per
    isinstance) mit Menschen und Bots; _klick_im_voice sitzt darin."""
    import music
    kanal = _voice_kanal(3, 1)
    assert isinstance(kanal, discord.VoiceChannel)
    assert len(kanal.members) == 4 and sum(not m.bot for m in kanal.members) == 3
    assert music._nur_bots_im_kanal(_voice_kanal(0, 1))
    assert not music._nur_bots_im_kanal(_voice_kanal(1, 1))
    klick = _klick_im_voice(5, kanal)
    player = music.GuildPlayer(loop=None)
    player.active_channel_id = kanal.id
    assert music.Music._darf_steuern(klick, player)
    fremd = _klick_im_voice(6, _voice_kanal(cid=99))
    assert not music.Music._darf_steuern(fremd, player)
    assert music.Music._darf_steuern(_klick_im_voice(7, _voice_kanal(cid=99), mod=True),
                                     player)


# --- Der gemeinsame Rauchtest --------------------------------------------------
def _alle_rauchproben():
    """(Datei, Modul, Befehle) aus jeder Testdatei, die RAUCHPROBEN fuehrt."""
    import importlib
    import lauf
    raus = []
    for datei in lauf.TESTDATEIEN:
        for modul, befehle in getattr(importlib.import_module(datei), "RAUCHPROBEN", ()) or ():
            raus.append((datei, modul, tuple(befehle)))
    return raus


def test_rauchproben_der_neuen_module():
    """Jede Testdatei kann RAUCHPROBEN = ((modul, ("befehl", ...)), ...) fuehren -
    so traegt jeder Teil seine Rauch-Befehle in SEINE Datei ein, und
    test_rauchtest_deckt_die_kette_aus_bot_py_ab zaehlt sie mit.

    Hier laufen sie: jeder Befehl durch das echte handle(), und es muss eine
    Antwort kommen, ohne dass etwas fliegt. Ein Modul ohne Befehle ist nur
    erlaubt, solange es ein Geruest ist - schaltet setup() es ein, muss es hier
    auch Befehle nennen."""
    import ai
    proben = _alle_rauchproben()
    namen = {modul.__name__ for _d, modul, _b in proben}
    assert {m.__name__ for m in NEUE_MODULE} <= namen, namen
    alt_client = ai.instance._client
    ai.instance._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=None)))
    stumm, geflogen, ohne = [], [], []
    try:
        for datei, modul, befehle in proben:
            with _mit_setup(modul) as an:
                if an and not befehle:
                    ohne.append(f"{modul.__name__} ({datei}.RAUCHPROBEN)")
                for befehl in befehle:
                    try:
                        antwort = asyncio.run(modul.handle(_rauch_nachricht(befehl)))
                    except Exception as exc:  # noqa: BLE001 - genau das suchen wir
                        geflogen.append(f"{modul.__name__} '{befehl}': "
                                        f"{type(exc).__name__}: {str(exc)[:120]}")
                        continue
                    if antwort is None:
                        stumm.append(f"{modul.__name__} '{befehl}'")
    finally:
        ai.instance._client = alt_client
    assert not geflogen, "Befehle stuerzen ab:\n  " + "\n  ".join(geflogen)
    assert not stumm, ("Befehle werden nicht erkannt (handle gibt None):\n  "
                       + "\n  ".join(stumm))
    assert not ohne, ("Diese Module sind eingeschaltet, nennen aber keinen "
                      "Rauch-Befehl:\n  " + "\n  ".join(ohne))


if __name__ == "__main__":
    run(globals())
