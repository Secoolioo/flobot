"""Musik-Plus: Endlos-Radio, Abstimmung zum Ueberspringen, die Warteschlange,
die einen Neustart ueberlebt (musikzustand.py), und Musik-Wrapped (musikstats.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_musikplus.py      nur diese Datei

Das Fundament legt hier nur das Geruest fest - der Musik-Teil fuellt die
Datei mit seinen Tests.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _StallVoice, _dynamisch_klicken, _embed_text, _fake_person, _http_fehler,
    _klick_im_voice, _musik_umgebung, _track, _verlauf_frisch, _voice_kanal,
    _with_economy)

import contextlib
import logging
from dataclasses import replace
from unittest import mock

import discord

import ai
import features
import guildcfg
import laufzeit
import music
import musikstats
import musikzustand
import render
import uhr

#: Befehle fuer den gemeinsamen Rauchtest (test_fundament.py). musikzustand
#: hat keinen Befehl und steht deshalb nicht hier.
RAUCHPROBEN = (
    (musikstats, ("wrapped", "wrapped letzter monat", "musikwrapped", "wrapped posten")),
)


def test_musikplus_geruest_hat_die_schnittstelle():
    """music.py und bot.py rufen diese Namen - es muss sie geben, auch bevor
    sie etwas tun. merken/vergessen/notieren laufen mitten in der Wiedergabe
    und sind deshalb synchron."""
    import inspect
    for name in ("setup", "is_enabled", "merken", "vergessen", "takt", "sichern",
                 "wiederherstellen"):
        assert callable(getattr(musikzustand, name, None)), name
    for name in ("setup", "is_enabled", "handle", "notieren", "tick"):
        assert callable(getattr(musikstats, name, None)), name
    for synchron in (musikzustand.merken, musikzustand.vergessen, musikstats.notieren):
        assert not inspect.iscoroutinefunction(synchron), synchron.__name__


# =============================================================================
# Endlos-Radio und Abstimmung zum Ueberspringen (music.py)
# =============================================================================
class _VoiceImKanal(_StallVoice):
    """_StallVoice, deren Kanal Leute hat - Radio und Abstimmung zaehlen sie."""

    def __init__(self, kanal):
        super().__init__()
        self._kanal = kanal
        self.verbunden = True       # False = die Verbindung ist gerade gerissen

    def is_connected(self):
        return self.verbunden

    @property
    def channel(self):
        return self._kanal


def _vid(n):
    """Eine elfstellige Video-ID, wie YouTube sie vergibt."""
    return f"vid{n:08d}"


def _yt(n):
    return f"https://www.youtube.com/watch?v={_vid(n)}"


def _mix(*eintraege):
    """Flache Mix-Eintraege, wie _flache_playlist sie liefert: (adresse, titel)."""
    return [(_yt(n), titel) for n, titel in eintraege]


def _wunsch(titel="Wunsch", n=0, wer=5, name="Anna"):
    """Ein Song, den ein Mensch gewuenscht hat - ohne Laenge, damit ein
    Songende im Test nicht als Abbruch gilt."""
    return music.Track(title=titel, stream_url="http://stream/w", webpage_url=_yt(n),
                       requested_by=name, requested_by_id=wer)


def _texte(view):
    return "\n".join(k.content for k in view.walk_children()
                     if isinstance(k, discord.ui.TextDisplay))


def _knopf(view, aktion):
    return next(k for k in view.walk_children()
                if isinstance(k, music.MusikKnopf) and k.aktion == aktion)


@contextlib.contextmanager
def _radio_umgebung(menschen=3, *, mixe=None, gid=77):
    """Player in einem Kanal mit 'menschen' Menschen (und einem Bot), ein
    gestubbtes _flache_playlist und ein mitgeschriebenes _sag.

    mixe: die Antworten des Abrufs der Reihe nach (None = Fehlschlag) - oder eine
    async-Funktion (url, ende) fuer Sonderfaelle. Liefert (player, abrufe, gesagt).

    Mit eigenem, leerem Verlauf: das Radio filtert gegen den dauerhaften Verlauf,
    und was ein frueherer Test dort hinterlassen hat, waere sonst "lief schon"."""
    _modul, verlauf_zurueck = _verlauf_frisch()
    player, _voice, aufraeumen = _musik_umgebung()
    player.voice = _VoiceImKanal(_voice_kanal(menschen, 1))
    player.guild_id = gid
    abrufe, gesagt = [], []
    antworten = [] if callable(mixe) else list(mixe or [])

    async def flach(url, *, quelle="Playlist", ende=None):
        abrufe.append((url, quelle, ende))
        if callable(mixe):
            return await mixe(url, ende)
        return antworten.pop(0) if antworten else None

    async def sag(text):
        gesagt.append(text)

    alt = music._flache_playlist
    music._flache_playlist = flach
    player._sag = sag
    try:
        yield player, abrufe, gesagt
    finally:
        music._flache_playlist = alt
        aufraeumen()
        verlauf_zurueck()


def _rd(n):
    """Der Abruf, den das Radio fuer den Mix zu Song n machen muss."""
    return (f"{_yt(n)}&list=RD{_vid(n)}", "Radio", music.RADIO_MIX_ENDE)


# --- Endlos-Radio: Befehle -------------------------------------------------------
def test_radio_befehle_und_was_keine_sind():
    """'flo radio', 'radio an/aus', 'endlos', 'autoplay off' und 'mach mal das
    radio an' sind Befehle. Ein Satz hinter dem Wort ist keiner - den
    beantwortet die KI. 'mach das radio aus' stoppt wie eh und je die Musik."""
    p = music.instance.parse_command
    for text, erwartet in (
            ("flo radio", ("radio", "")), ("flo radio an", ("radio", "an")),
            ("flo radio aus", ("radio", "aus")), ("flo Radio AUS!", ("radio", "aus")),
            ("flo radio bitte an", ("radio", "an")), ("flo radio wieder an", ("radio", "an")),
            ("flo endlos", ("radio", "")), ("flo endlosradio an", ("radio", "an")),
            ("flo autoplay", ("radio", "")), ("flo autoplay off", ("radio", "off")),
            ("flo radio stop", ("radio", "stop")),
            ("flo radio einschalten", ("radio", "einschalten")),
            ("flo mach mal das radio an", ("radio", "an")),
            ("flo mach radio an", ("radio", "an")),
            ("flo schalt das radio ein", ("radio", "an")),
            ("flo mach das radio aus", ("stop", "")),
            ("flo endlosschleife", ("loop", "")),
            ("flo spiel radio gaga", ("search", "radio gaga"))):
        assert p(text) == erwartet, (text, p(text))
    for satz in ("flo radio ist langweilig", "flo endlos langweilig hier",
                 "flo radio an der wand", "flo radio gaga", "flo autoplay nervt",
                 "flo ich hör radio", "flo das radio", "flo radios sind out",
                 "flo dreh das radio auf", "flo radio an, ich will nicht mehr leben",
                 "flo skip, ich will nicht mehr leben"):
        assert p(satz) is None, (satz, p(satz))
    # Eine Notlage im Satz macht nie einen Befehl daraus - die KI antwortet
    # mit ihrer Notlagen-Regel, die Musik fasst nichts an.
    assert ai.FloAI._NOTLAGE_RE.search("flo radio an, ich will nicht mehr leben")
    # Die Woerter sind Befehle (Tippfehler-Ziel, nichts Zustandsaenderndes
    # aus einem Vertipper) - Alltagssaetze bleiben unangetastet.
    for wort in ("radio", "endlosradio", "autoplay", "endlos"):
        assert wort in cmdnorm.KNOWN and wort in cmdnorm.ZUSTAND, wort
        assert cmdnorm.normalize(f"{wort} an") is None, wort
    for satz in ("endlich ruhe hier", "endet bald", "radio ist laut", "ende los"):
        assert cmdnorm.normalize(satz) is None, (satz, cmdnorm.normalize(satz))
    # Gemessene Fehlgriffe (inventar.py --cmdnorm): 'Flo, adio!' ist ein
    # Tschuess, 'radi' ein Rettich - beides schaltete sonst das Radio.
    for wort in ("adio", "Adio!", "radi", "radi?", "rado", "endlose", "endlose!"):
        assert cmdnorm.normalize(wort) is None, (wort, cmdnorm.normalize(wort))
    # Ein echter Vertipper bleibt einer.
    assert cmdnorm.normalize("raido") == "radio"


def test_radio_befehl_im_chat_schaltet_um():
    """Im Voice (oder als Mod) schaltet man das Radio per Text - nacktes Wort
    = umschalten. Wer draussen sitzt, bekommt eine Abfuhr. Jede Aenderung
    geht an musikzustand (das Radio soll einen Neustart ueberleben)."""
    mi = music.instance
    player, _voice, aufraeumen = _musik_umgebung()
    kanal = _voice_kanal(2, 1)
    player.voice = _VoiceImKanal(kanal)
    alt = (mi._enabled, mi._players.get(1))
    mi._enabled = True
    mi._players[1] = player
    drin = _klick_im_voice(1001, kanal).user
    fremd = _klick_im_voice(2000, _voice_kanal(cid=99)).user
    mod = _klick_im_voice(2001, _voice_kanal(cid=99), mod=True).user
    gemerkt = []

    def sag(text, wer=drin):
        msg = SimpleNamespace(content=f"flo {text}", guild=SimpleNamespace(id=1),
                              channel=SimpleNamespace(id=5), author=wer, mentions=[])
        return asyncio.run(mi.handle(msg))

    try:
        player.start(_wunsch())
        with mock.patch.object(musikzustand, "merken", lambda p: gemerkt.append(p)):
            e = sag("radio an")
            assert player.radio and "Radio an" in e.title, _embed_text(e)
            assert gemerkt == [player]
            assert "Läuft doch schon" in _embed_text(sag("radio an"))
            assert len(gemerkt) == 1, "ohne Aenderung nichts zu merken"
            e = sag("radio")
            assert not player.radio and "Radio aus" in e.title
            assert "doch aus" in _embed_text(sag("autoplay off"))
            sag("endlos")
            assert player.radio
            sag("mach mal das radio an")
            assert player.radio
            e = sag("radio aus", fremd)
            assert e.description in music._ZAUNGAST and player.radio, "Zaungast hat gedreht"
            sag("radio aus", mod)
            assert not player.radio, "Mods duerfen auch von draussen"
            assert len(gemerkt) == 4, gemerkt
    finally:
        mi._enabled = alt[0]
        if alt[1] is None:
            mi._players.pop(1, None)
        else:
            mi._players[1] = alt[1]
        aufraeumen()


def test_radio_an_ohne_musik_startet_aus_dem_verlauf():
    """'flo radio an', und es laeuft nichts: Flo kommt in den Kanal der Person
    und legt gleich los - der Mix geht vom neuesten Song im Verlauf aus. Ohne
    Voice oder ganz ohne Verlauf sagt er, was fehlt."""
    music_mod, restore = _verlauf_frisch()
    mi = music_mod.instance
    kanal = _voice_kanal(2, 1)
    drin = _klick_im_voice(1001, kanal).user
    draussen = _fake_person(1002)
    alt = (mi._enabled, mi._players.get(1))
    mi._players.pop(1, None)
    try:
        with _radio_umgebung(2, mixe=[_mix((1, "Eins"), (2, "Zwei"))], gid=1) as (
                player, abrufe, gesagt):
            player.voice, player.active_channel_id = None, None
            mi._players[1] = player
            verbunden = []

            async def verbinden(k):
                verbunden.append(k.id)
                player.voice = _VoiceImKanal(k)
                player.active_channel_id = k.id
                return player.voice
            player.connect = verbinden

            def sag(text, wer=drin):
                msg = SimpleNamespace(content=f"flo {text}", guild=SimpleNamespace(id=1),
                                      channel=SimpleNamespace(id=5), author=wer, mentions=[])
                return asyncio.run(mi.handle(msg))

            e = sag("radio an")
            assert "noch nie was" in _embed_text(e), _embed_text(e)
            assert not player.radio and not verbunden and not abrufe

            mi.verlauf_notieren(1, _wunsch("Alt", 0))
            mi.verlauf_notieren(1, _wunsch("Neu", 9))          # neuester zuerst
            e = sag("radio an", draussen)
            assert "Sprachkanal" in _embed_text(e) and not verbunden

            e = sag("radio an")
            assert e.title == "📻  Radio an" and "Radio läuft" in _embed_text(e), _embed_text(e)
            assert verbunden == [kanal.id]
            assert abrufe == [_rd(9)], abrufe
            assert player.radio and player.current.title == "Eins" and player.current.radio
            assert [t.title for t in player.queue] == ["Zwei"]
            # Die Antwort auf den Befehl IST die Ansage - kein zweiter Hinweis.
            assert gesagt == []
    finally:
        mi._enabled = alt[0]
        if alt[1] is None:
            mi._players.pop(1, None)
        else:
            mi._players[1] = alt[1]
        restore()


@contextlib.contextmanager
def _radio_start(mixe, *, drin_vorher=False, verzoegerung=0.0):
    """'flo radio an', und es laeuft nichts: Server 1, Verlauf mit 'Neu' (Song
    9), Flo noch draussen (oder mit drin_vorher schon im Kanal). connect
    braucht 'verzoegerung' Sekunden - wie ein echter Voice-Beitritt.
    Liefert ein Objekt mit player, abrufe, gesagt, kanal, verbunden, sag(text,
    wer=None) und leute (zwei Menschen im Kanal)."""
    _modul, restore = _verlauf_frisch()
    mi = music.instance
    kanal = _voice_kanal(2, 1)
    alt = (mi._enabled, mi._players.get(1))
    mi._players.pop(1, None)
    try:
        with _radio_umgebung(2, mixe=mixe, gid=1) as (player, abrufe, gesagt):
            if not drin_vorher:
                player.voice, player.active_channel_id = None, None
            else:
                player.voice = _VoiceImKanal(kanal)
                player.active_channel_id = kanal.id
            mi._players[1] = player
            mi.verlauf_notieren(1, _wunsch("Neu", 9))
            verbunden = []

            async def verbinden(k):
                verbunden.append(k.id)
                if verzoegerung:
                    await asyncio.sleep(verzoegerung)
                if player.voice is None:
                    player.voice = _VoiceImKanal(k)
                player.active_channel_id = k.id
                return player.voice
            player.connect = verbinden
            leute = [_klick_im_voice(uid, kanal).user for uid in (1000, 1001)]

            def nachricht(text, wer=None):
                return SimpleNamespace(content=f"flo {text}", guild=SimpleNamespace(id=1),
                                       channel=SimpleNamespace(id=5),
                                       author=wer or leute[0], mentions=[])

            def sag(text, wer=None):
                return asyncio.run(mi.handle(nachricht(text, wer)))

            yield SimpleNamespace(player=player, abrufe=abrufe, gesagt=gesagt, kanal=kanal,
                                  verbunden=verbunden, sag=sag, nachricht=nachricht,
                                  leute=leute)
    finally:
        mi._enabled = alt[0]
        if alt[1] is None:
            mi._players.pop(1, None)
        else:
            mi._players[1] = alt[1]
        restore()


def test_radio_an_doppelt_startet_nichts_doppelt():
    """Zwei 'flo radio an' im selben Moment (Doppelpost, zwei Leute), waehrend
    Flo noch in den Kanal kommt: nur EINER startet. Vorher holte das zweite
    _advance dem laufenden Radio-Song den naechsten weg, start() scheiterte an
    'Already playing audio.', und nach zwei solchen Fehlern gab die
    Warteschlange auf - mit falscher Netz-Warnung und verlorenem Song."""
    async def mix(url, ende):
        await asyncio.sleep(0.02)          # YouTube braucht einen Moment
        return _mix((1, "Eins"), (2, "Zwei"), (3, "Drei"))

    with _radio_start(mix, verzoegerung=0.02) as r:
        mi = music.instance

        async def beide():
            return await asyncio.gather(mi.handle(r.nachricht("radio an", r.leute[0])),
                                        mi.handle(r.nachricht("radio an", r.leute[1])))
        antworten = asyncio.run(beide())
        texte = sorted(_embed_text(e) for e in antworten)
        assert any("Radio läuft" in t for t in texte), texte
        assert any("Läuft doch schon" in t for t in texte), texte
        p = r.player
        assert p.current is not None and p.current.title == "Eins", p.current
        assert [t.title for t in p.queue] == ["Zwei", "Drei"]
        assert not p._advance_aufgegeben and p.voice.is_playing()
        assert len(r.abrufe) == 1 and r.gesagt == [], (r.abrufe, r.gesagt)

    # Sucht gerade ein 'spiel ...', kommt Musik ohnehin: nur der Schalter.
    with _radio_start([_mix((1, "Eins"))]) as r:
        r.player._laufende_anfragen = 1
        e = r.sag("radio an")
        assert e.title == "📻  Radio an" and "Läuft die Schlange leer" in _embed_text(e)
        assert r.player.radio and r.player.current is None
        assert r.abrufe == [] and r.verbunden == []


def test_radio_an_wenn_der_start_scheitert():
    """Was 'radio an' sagt, wenn nichts anlaeuft:
    - nichts Neues im Mix: das Radio gibt auf (Ansage im Kanal), Flo geht wieder
      raus, wenn er nur dafuer reingekommen war;
    - Mix nicht ladbar oder Discord wackelt: Flo bleibt drin, der Watchdog
      holt es nach - und die Antwort sagt genau das (nicht 'versuch's nochmal');
    - Beitritt scheitert: die gemeinsame Voice-Antwort, Radio bleibt aus."""
    nichts_neues = _mix((9, "Neu"))
    with _radio_start([nichts_neues, nichts_neues]) as r:
        assert r.sag("radio an") is music.HANDLED
        assert r.gesagt == [music._RADIO_AUFGEGEBEN]
        assert not r.player.radio and r.player.voice is None, "stumm im Kanal geblieben"

    with _radio_start([nichts_neues, nichts_neues], drin_vorher=True) as r:
        assert r.sag("radio an") is music.HANDLED
        assert r.player.voice is not None, "war schon drin - bleibt drin"

    with _radio_start([None, None]) as r, mock.patch.object(music.log, "warning"):
        e = r.sag("radio an")
        assert "versuch's gleich selbst nochmal" in _embed_text(e), _embed_text(e)
        assert r.player.radio and r.player._radio_verschoben and r.player._radio_fehler == 1
        assert r.player.voice is not None and r.gesagt == []

    with _radio_start([_mix((1, "Eins"))]) as r, \
            mock.patch.object(laufzeit, "gateway_stabil", lambda gnade=None: False):
        e = r.sag("radio an")
        assert "versuch's gleich selbst nochmal" in _embed_text(e)
        assert r.abrufe == [] and r.player._radio_verschoben and r.player.voice is not None

    with _radio_start([_mix((1, "Eins"))]) as r, \
            mock.patch.object(music, "_radio_darf", lambda gid: False):
        e = r.sag("radio an")
        assert "Versuch's gleich nochmal" in _embed_text(e) and r.player.voice is None

    with _radio_start([_mix((1, "Eins"))]) as r:
        async def kaputt(k):
            raise asyncio.TimeoutError()
        r.player.connect = kaputt
        e = r.sag("radio an")
        assert e.description == music.VOICE_KAPUTT and not r.player.radio
        assert r.abrufe == []


# --- Endlos-Radio: Nachschub ------------------------------------------------------
def test_radio_legt_nach_wenn_die_schlange_leer_ist():
    """Schlange leer, Radio an: der YouTube-Mix zum letzten Wunsch kommt dran -
    drei Songs, lazy, als Radio markiert, ohne den Ausgangssong, ohne was
    schon lief oder das Radio schon hatte, ohne Stunden-Loops, Nightcore und
    tote Eintraege. Beim ersten Mal sagt Flo es an, danach nicht mehr."""
    mix = _mix((0, "Wunsch"),                        # der Ausgangssong selbst
               (11, "Alt (Official Video)"),          # lief schon - anderes Upload
               (12, "Song 10 Hours"), (13, "Song (Nightcore)"),
               (14, "Andere"),                        # hatte das Radio schon (ID)
               (15, "[Private video]"),
               (1, "Eins"), (2, "Zwei"), (16, "Eins (Lyrics)"), (3, "Drei"),
               (4, "Vier"))
    zweiter = _mix((4, "Vier"), (5, "Fünf"), (6, "Sechs"), (7, "Sieben"))
    with _radio_umgebung(3, mixe=[mix, zweiter]) as (player, abrufe, gesagt):
        player.history.append(_track("Alt"))
        player.start(_wunsch())
        player._radio_gesehen.append(_vid(14))
        player.radio = True
        asyncio.run(player.skip())
        assert abrufe == [_rd(0)], abrufe
        assert player.current.title == "Eins"
        assert [t.title for t in player.queue] == ["Zwei", "Drei"]
        for t in [player.current] + player.queue:
            assert t.radio and t.requested_by == music.RADIO_WUNSCH and t.requested_by_id == 0
            assert t.query.startswith("https://www.youtube.com/watch?v=")
        assert player._radio_gesehen == [_vid(14), _vid(1), _vid(2), _vid(3)]
        assert gesagt == [music._RADIO_HINWEIS]
        assert (player._radio_zahl, player._radio_runden) == (3, 1)

        # Song zu Ende, Schlange abgespielt: wieder vom Wunsch aus, ohne Ansage.
        player.queue.clear()
        player.voice.spielt = False
        asyncio.run(player._advance(player._play_gen))
        assert abrufe[-1] == _rd(0), abrufe
        assert player.current.title == "Vier" and [t.title for t in player.queue] == [
            "Fünf", "Sechs"]
        assert gesagt == [music._RADIO_HINWEIS], "die Ansage kommt nur einmal je Sitzung"


def test_radio_saat_wunsch_zuerst_jeder_dritte_vom_radio():
    """Ausgangspunkt ist der letzte Song, den ein Mensch gewuenscht hat; jeder
    dritte Nachschub geht vom letzten Radio-Song aus (sonst immer derselbe
    Mix). Ohne Wunsch-ID der letzte Nicht-Radio-Song; ohne Verlauf im Speicher
    der dauerhafte Verlauf."""
    music_mod, restore = _verlauf_frisch()
    try:
        player = music.GuildPlayer(loop=None, guild_id=78)
        wunsch = _wunsch("Wunsch", 0)
        ohne_id = music.Track(title="Ohne ID", stream_url="x", webpage_url=_yt(1))
        funk = music.Track(title="Funk", stream_url="x", webpage_url=_yt(2), radio=True)
        player.history[:] = [wunsch, ohne_id, funk]
        assert player._radio_saaten() == [(_yt(0), "Wunsch"), (_yt(2), "Funk")]
        player._radio_runden = 2              # der dritte Nachschub
        assert player._radio_saaten() == [(_yt(2), "Funk"), (_yt(0), "Wunsch")]
        player._radio_runden = 3
        assert player._radio_saaten()[0] == (_yt(0), "Wunsch")
        player.history[:] = [ohne_id, funk]
        assert player._radio_saaten()[0] == (_yt(1), "Ohne ID")
        # Lazy Track ohne Adresse: die Query (Link) zaehlt.
        lazy = music.Track(title="Lazy", stream_url="", query=_yt(3), requested_by_id=4)
        player.history[:] = [lazy]
        assert player._radio_saaten() == [(_yt(3), "Lazy")]
        # Frisch nach einem Neustart: der dauerhafte Verlauf, neuester zuerst.
        player.history.clear()
        assert player._radio_saaten() == []
        music_mod.instance.verlauf_notieren(78, _wunsch("Alt", 5))
        music_mod.instance.verlauf_notieren(78, _wunsch("Neu", 6))
        assert player._radio_saaten() == [(_yt(6), "Neu"), (_yt(5), "Alt")]
    finally:
        restore()


def test_radio_soundcloud_saat_sucht_erst_auf_youtube():
    """Ein SoundCloud-Song hat keine YouTube-ID - dann erst eine flache Suche
    nach dem Titel, und vom Treffer aus der Mix."""
    async def antwort(url, ende):
        if url.startswith("ytsearch1:"):
            return _mix((8, "Treffer"))
        return _mix((8, "Treffer"), (1, "Eins"))
    with _radio_umgebung(3, mixe=antwort) as (player, abrufe, gesagt):
        sc = music.Track(title="Wolke", stream_url="http://s",
                         webpage_url="https://soundcloud.com/wer/wolke", requested_by_id=5)
        player.start(sc)
        player.radio = True
        asyncio.run(player.skip())
        assert abrufe == [("ytsearch1:Wolke", "Radio", 1), _rd(8)], abrufe
        assert player.current.title == "Eins"


def test_radio_filter_laesst_covers_beim_cover_durch():
    """Wer ein Cover gewuenscht hat, bekommt auch Covers - das Wort im
    Ausgangssong zaehlt nicht als Muell. Der Verlauf (letzte 50) zaehlt als
    'lief schon', und mehr als 'anzahl' kommt nie."""
    music_mod, restore = _verlauf_frisch()
    try:
        player = music.GuildPlayer(loop=None, guild_id=79)
        music_mod.instance.verlauf_notieren(79, music.Track(title="Gestern Lief", stream_url=""))
        eintraege = _mix((1, "Anderes (Cover)"), (2, "Gestern lief [HD]"), (3, "Remix B"),
                         (4, "Mega Mix 2024"), (5, "C"), (6, "D"), (7, "Hours"),
                         (8, "Song 3 Stunden Version"))
        raus = player._radio_filtern(eintraege, _vid(0), "Song (Cover)", 2)
        assert [t for _a, t, _v in raus] == ["Anderes (Cover)", "Remix B"], raus
        # 'Hours' ist ein Songtitel, '3 Stunden' ein Loop.
        raus = player._radio_filtern(eintraege, _vid(0), "Song", 5)
        assert [t for _a, t, _v in raus] == ["Remix B", "C", "D", "Hours"], raus
        assert music._yt_id("https://youtu.be/dQw4w9WgXcQ?t=3") == "dQw4w9WgXcQ"
        assert music._yt_id("https://soundcloud.com/a/b") == ""
    finally:
        restore()


def test_radio_stop_oder_aus_waehrend_des_abrufs_reiht_nichts_ein():
    """YouTube braucht ein paar Sekunden. Sagt in der Zeit jemand 'flo stop'
    (neue Sitzung) oder 'radio aus', landet nichts mehr in der Schlange - sonst
    spielte das Radio beim naechsten 'spiel' als Geist an."""
    halter = {}

    async def stop_dazwischen(url, ende):
        await halter["p"].disconnect()
        return _mix((1, "Eins"), (2, "Zwei"))
    with _radio_umgebung(3, mixe=stop_dazwischen) as (player, abrufe, gesagt):
        halter["p"] = player
        player.start(_wunsch())
        player.radio = True
        asyncio.run(player.skip())
        assert len(abrufe) == 1 and not player.queue and player.current is None
        assert not player.radio and gesagt == []

    async def aus_dazwischen(url, ende):
        halter["p"].radio_setzen(False)
        return _mix((1, "Eins"))
    with _radio_umgebung(3, mixe=aus_dazwischen) as (player, abrufe, gesagt):
        halter["p"] = player
        player.start(_wunsch())
        player.radio = True
        asyncio.run(player.skip())
        assert len(abrufe) == 1 and not player.queue and player.current is None
        assert gesagt == []


def _heilen(player):
    """Ein Watchdog-Takt (heal) fuer den Player im Testkanal."""
    kanal = player.voice.channel
    gilde = SimpleNamespace(get_channel=lambda cid: kanal, voice_client=player.voice)
    player.active_channel_id = kanal.id
    asyncio.run(player.heal(gilde))


def test_radio_zwei_fehlschlaege_am_stueck_schalten_es_ab():
    """Je Nachschub zwei Versuche - der zweite vom anderen Ausgangssong aus.
    Bringen beide nichts Neues, ist das Radio aus, mit Ansage (noch mal fragen
    braechte dasselbe). Liess sich der Mix gar nicht LADEN (kurzer yt-dlp-/
    DNS-Aussetzer), schaltet das allein nichts ab: der Watchdog holt den
    Nachschub einmal nach, und erst wenn auch der scheitert, ist Schluss."""
    nichts_neues = _mix((0, "Wunsch"))
    with _radio_umgebung(3, mixe=[nichts_neues, nichts_neues]) as (player, abrufe, gesagt), \
            mock.patch.object(music.log, "warning") as warnung:
        player.history.append(music.Track(title="Funk", stream_url="x", webpage_url=_yt(7),
                                          radio=True))
        player.start(_wunsch())
        player.radio = True
        asyncio.run(player.skip())
        assert abrufe == [_rd(0), _rd(7)], abrufe
        assert not player.radio and player.current is None
        assert gesagt == [music._RADIO_AUFGEGEBEN] and not player._radio_verschoben
        assert warnung.called, "ins Log gehoert, warum das Radio aus ist"

    # Ladefehler: erst still nachholen, beim zweiten Mal aus.
    with _radio_umgebung(3, mixe=[None, None, None, None]) as (player, abrufe, gesagt), \
            mock.patch.object(music.log, "warning"):
        player.start(_wunsch())
        player.radio = True
        asyncio.run(player.skip())
        assert len(abrufe) == 2 and player.radio and player.current is None
        assert gesagt == [] and player._radio_verschoben and player._radio_fehler == 1
        player.voice.spielt = False
        _heilen(player)
        assert len(abrufe) == 4 and not player.radio
        assert gesagt == [music._RADIO_AUFGEGEBEN]
        assert (player._radio_fehler, player._radio_verschoben) == (0, False)

    # Nichts Neues, dann ein Ladefehler: auch das ist vielleicht nur das Netz.
    with _radio_umgebung(3, mixe=[nichts_neues, None, _mix((1, "Eins"))]) as (
            player, abrufe, gesagt), mock.patch.object(music.log, "warning"):
        player.start(_wunsch())
        player.radio = True
        asyncio.run(player.skip())
        assert len(abrufe) == 2 and player.radio and player._radio_verschoben
        player.voice.spielt = False
        _heilen(player)
        assert len(abrufe) == 3 and player.current.title == "Eins"
        assert player._radio_fehler == 0, "ein Erfolg setzt die Fehlerserie zurueck"
        assert gesagt == [music._RADIO_HINWEIS]

    # Ein Ladefehler, dann klappt der zweite Versuch im selben Nachschub.
    with _radio_umgebung(3, mixe=[None, _mix((1, "Eins"))]) as (player, abrufe, gesagt):
        player.start(_wunsch())
        player.radio = True
        asyncio.run(player.skip())
        assert len(abrufe) == 2 and player.radio and player.current.title == "Eins"
        assert player._radio_fehler == 0 and not player._radio_verschoben

    # Die Fehlerserie gilt nur am Stueck: Radio aus und wieder an faengt neu an.
    player = music.GuildPlayer(loop=None, guild_id=80)
    player.radio, player._radio_fehler = True, 1
    with mock.patch.object(musikzustand, "merken", lambda p: None):
        player.radio_setzen(False)
        player.radio_setzen(True)
    assert player._radio_fehler == 0


def test_radio_legt_nicht_nach_ohne_zuhoerer_geparkt_oder_in_der_sendepause():
    """Sitzen nur noch Bots im Kanal, ist die Schlange geparkt, ist Sendepause
    oder die Musik auf dem Server aus: kein Nachschub. Das Radio bleibt dabei
    an - es ist nur nichts zu tun."""
    mix = [_mix((1, "Eins"))]
    with _radio_umgebung(0, mixe=mix) as (player, abrufe, gesagt):
        player.start(_wunsch())
        player.radio = True
        asyncio.run(player.skip())
        assert abrufe == [] and player.current is None and player.radio

    with _radio_umgebung(3, mixe=mix) as (player, abrufe, gesagt):
        player.radio = True
        player._geparkt = True
        player.history.append(_wunsch())
        assert not asyncio.run(player._radio_nachlegen(player._session_gen))
        assert abrufe == []

    alt_lock = admin.instance._locked
    try:
        admin.instance._locked = True
        with _radio_umgebung(3, mixe=mix) as (player, abrufe, gesagt):
            player.start(_wunsch())
            player.radio = True
            asyncio.run(player.skip())
            assert abrufe == [] and player.current is None
    finally:
        admin.instance._locked = alt_lock

    with mock.patch.object(features, "is_on_in", lambda gid, key: key != "music"):
        with _radio_umgebung(3, mixe=mix) as (player, abrufe, gesagt):
            player.start(_wunsch())
            player.radio = True
            asyncio.run(player.skip())
            assert abrufe == [] and player.current is None


def test_radio_wartet_auf_discord_und_der_waechter_holt_nach():
    """Wackelt die Verbindung zu Discord, legt das Radio nichts nach - der
    Watchdog holt das EINMAL nach, sobald sie wieder steht."""
    with _radio_umgebung(3, mixe=[_mix((1, "Eins"))]) as (player, abrufe, gesagt):
        player.start(_wunsch())
        player.radio = True
        with mock.patch.object(laufzeit, "gateway_stabil", lambda gnade=None: False):
            asyncio.run(player.skip())
        assert abrufe == [] and player.current is None and player._radio_verschoben
        kanal = player.voice.channel
        gilde = SimpleNamespace(get_channel=lambda cid: kanal, voice_client=player.voice)
        player.voice.spielt = False
        asyncio.run(player.heal(gilde))
        assert abrufe == [_rd(0)] and player.current.title == "Eins"
        assert not player._radio_verschoben
        assert not asyncio.run(player._radio_nachholen(kanal)), "nur einmal"


def test_radio_nachholen_wartet_auf_die_voice_verbindung():
    """Ist nach dem Discord-Wackler auch die Voice-Verbindung noch weg, darf der
    nachzuholende Nachschub nicht verfallen: der Merker bleibt, die
    Leerlauf-Uhr laesst den Reconnect durch, und sobald Flo wieder verbunden
    ist, legt der naechste Takt nach. Radio aus oder Musik, die wieder laeuft,
    loeschen ihn - dann gibt es nichts mehr nachzuholen."""
    with _radio_umgebung(3, mixe=[_mix((1, "Eins"))]) as (player, abrufe, gesagt):
        player.start(_wunsch())
        player.radio = True
        with mock.patch.object(laufzeit, "gateway_stabil", lambda gnade=None: False):
            asyncio.run(player.skip())
        kanal = player.voice.channel
        player.voice.verbunden = False                 # die Voice ist mit weggefallen
        assert not asyncio.run(player._radio_nachholen(kanal))
        assert player._radio_verschoben, "Merker verfallen, obwohl nichts nachgeholt wurde"
        assert not asyncio.run(player._leerlauf(kanal)), "Leerlauf blockt den Reconnect"
        # Nur Bots im Kanal: auch dann bleibt der Merker (und die Uhr laeuft).
        player.voice.verbunden = True
        bots = _voice_kanal(0, 2)
        assert not asyncio.run(player._radio_nachholen(bots)) and player._radio_verschoben
        assert abrufe == []
        assert asyncio.run(player._radio_nachholen(kanal))
        assert abrufe == [_rd(0)] and player.current.title == "Eins"
        assert not player._radio_verschoben

    with _radio_umgebung(3) as (player, abrufe, gesagt):
        kanal = player.voice.channel
        player.radio, player._radio_verschoben = False, True
        assert not asyncio.run(player._radio_nachholen(kanal)) and not player._radio_verschoben
        player.radio, player._radio_verschoben = True, True
        player.start(_wunsch())                        # es laeuft wieder etwas
        assert not asyncio.run(player._radio_nachholen(kanal)) and not player._radio_verschoben
        assert abrufe == []


def test_radio_deckel_je_sitzung():
    """Hoechstens MUSIC_RADIO_MAX Radio-Songs je Sitzung - der letzte
    Nachschub wird gekappt, danach ist das Radio aus."""
    with _radio_umgebung(3, mixe=[_mix((1, "Eins"), (2, "Zwei"), (3, "Drei"))]) as (
            player, abrufe, gesagt):
        player.start(_wunsch())
        player.radio = True
        player._radio_hinweis = True
        player._radio_zahl = music.MUSIC_RADIO_MAX - 2
        asyncio.run(player.skip())
        assert player.current.title == "Eins" and [t.title for t in player.queue] == ["Zwei"]
        assert player._radio_zahl == music.MUSIC_RADIO_MAX
        player.queue.clear()
        asyncio.run(player.skip())
        assert len(abrufe) == 1 and not player.radio and player.current is None
        assert gesagt and "Radio aus" in gesagt[-1] and str(music.MUSIC_RADIO_MAX) in gesagt[-1]


def test_stop_setzt_radio_und_abstimmung_zurueck():
    """Das Radio gehoert zur Sitzung: nach 'flo stop' (oder dem Leerlauf) ist
    es aus, das Gedaechtnis leer, die naechste Sitzung kuendigt es neu an."""
    player, _voice, aufraeumen = _musik_umgebung()
    try:
        player.start(_wunsch())
        player.radio, player._radio_hinweis = True, True
        player._radio_gesehen.extend([_vid(1), _vid(2)])
        player._radio_zahl, player._radio_runden, player._radio_verschoben = 9, 3, True
        player._skip_stimmen.add(1001)
        player._abgewaehlt = player.current
        asyncio.run(player.disconnect())
        assert (player.radio, player._radio_hinweis, player._radio_gesehen,
                player._radio_zahl, player._radio_runden, player._radio_verschoben,
                player._skip_stimmen, player._abgewaehlt) == (
            False, False, [], 0, 0, False, set(), None)
    finally:
        aufraeumen()


# --- Endlos-Radio: Panel ------------------------------------------------------------
def test_radio_knopf_am_panel():
    """Zweite Reihe [Loop, Radio]: 📻 gruen, wenn an. Ein Klick (ueber den
    echten DynamicItem-Weg, wie nach einem Neustart) schaltet um, zeigt das
    Panel neu und merkt es sich. Die Meta-Zeile sagt '📻 Radio', und in 'Als
    Naechstes' tragen Radio-Songs ein 📻. Zaungaeste duerfen nicht."""
    mi = music.instance
    player, _voice, aufraeumen = _musik_umgebung()
    kanal = _voice_kanal(3, 1)
    player.voice = _VoiceImKanal(kanal)
    alt = mi._players.get(1)
    mi._players[1] = player
    gemerkt = []
    try:
        player.start(_wunsch())
        funk = _track("Vom Radio")
        funk.radio = True
        player.queue.extend([_track("Gewuenscht"), funk])
        view = music.MusikPanel(player)
        knopf = _knopf(view, "radio")
        assert knopf.item.custom_id == "flo:musik:radio" and len(knopf.item.custom_id) <= 100
        assert knopf.item.style == discord.ButtonStyle.secondary
        assert str(knopf.item.emoji) == "📻"
        assert "📻 Radio" not in _texte(view)
        assert "📻 Vom Radio" in _texte(view) and "📻 Gewuenscht" not in _texte(view)

        klick = _klick_im_voice(1001, kanal)
        with mock.patch.object(musikzustand, "merken", lambda p: gemerkt.append(p)):
            # Ueber das Notweg-Panel (klassische View): dort haengen die Knoepfe
            # direkt an der View - die custom_id ist dieselbe.
            treffer = _dynamisch_klicken(music.DYNAMISCHE_KNOEPFE,
                                         music._klassisches_panel(player),
                                         "flo:musik:radio", klick)
        assert treffer == 1 and player.radio and gemerkt == [player]
        (art, kw), = klick.protokoll
        assert art == "edit"
        neu = kw["view"]
        assert "📻 Radio" in _texte(neu)
        assert _knopf(neu, "radio").item.style == discord.ButtonStyle.success
        # Notweg-Panel (Embed): zwei Knoepfe in Reihe 1.
        reihe1 = [k for k in music._klassisches_panel(player).children if k.row == 1]
        assert [k.aktion for k in reihe1] == ["loop", "radio"]

        fremd = _klick_im_voice(2000, _voice_kanal(cid=99))
        asyncio.run(mi._panel_klick(fremd, "radio"))
        (art, text, kw), = fremd.protokoll
        assert art == "send" and text in music._ZAUNGAST and kw.get("ephemeral")
        assert player.radio, "Zaungast hat das Radio gedreht"
        # Und die Warteschlange zeigt es auch.
        emb = music._queue_embed(player)
        assert "📻 Vom Radio" in emb.fields[-1].value
    finally:
        if alt is None:
            mi._players.pop(1, None)
        else:
            mi._players[1] = alt
        aufraeumen()


def test_radio_meta_zeile_und_notweg_panel():
    """Die Meta-Zeile sagt jedes Ding EINMAL: ein Radio-Song hat keinen
    Wuenscher ('🙋 📻 Radio  ·  📻 Radio' stand vorher da), bei Radio aus
    steht '📻 vom Radio'. Das Notweg-Panel (Embed) zeigt Radio und
    Abstimmung auch. Ein Radio-Klick, waehrend kein Song laeuft (Schlange
    steht), antwortet mit einem Zettel statt am Embed zu scheitern."""
    def meta(player):
        return _texte(music.MusikPanel(player)).splitlines()[2]

    with _radio_umgebung(3, mixe=[_mix((1, "Eins"), (2, "Zwei"))]) as (player, abrufe, gesagt):
        player.start(_wunsch())
        player.radio = True
        assert meta(player) == "-# 🙋 Anna  ·  📻 Radio", meta(player)
        asyncio.run(player.skip())
        assert player.current.radio
        assert meta(player) == "-# 📻 Radio", meta(player)
        player.radio = False
        assert meta(player) == "-# 📻 vom Radio", meta(player)

        emb = music._now_playing_embed(player.current, 1, radio=True, stand=(2, 3))
        felder = {f.name: f.value for f in emb.fields}
        assert felder["Radio"] == "📻 an" and felder["Abstimmung"] == "⏭️ 2/3 wollen weg"
        ohne = music._now_playing_embed(player.current, 1)
        assert not {"Radio", "Abstimmung"} & {f.name for f in ohne.fields}

        # Notweg-Panel bekommt beim Auffrischen denselben Stand.
        alt = SimpleNamespace(embeds=[object()])
        player.radio = True
        neu = music.instance._panel_neu(player, alt)
        assert "Radio" in {f.name for f in neu["embed"].fields}

    mi = music.instance
    player, _voice, aufraeumen = _musik_umgebung()
    kanal = _voice_kanal(2, 1)
    player.voice = _VoiceImKanal(kanal)
    alt = mi._players.get(1)
    mi._players[1] = player
    try:
        player.queue.append(_track("Steht"))
        player._advance_aufgegeben = True
        klick = _klick_im_voice(1001, kanal)
        with mock.patch.object(musikzustand, "merken", lambda p: None):
            asyncio.run(mi._panel_klick(klick, "radio"))
        assert klick.protokoll == [("send", "📻 Radio an.", {"ephemeral": True})]
        assert player.radio
    finally:
        if alt is None:
            mi._players.pop(1, None)
        else:
            mi._players[1] = alt
        aufraeumen()


def test_hilfe_zeigt_radio_und_abstimmung_mit_der_musik():
    """Radio und die Abstimmung beim Skip gehoeren zur Musik selbst: die Seite
    'Playlists & Radio' steht in der Hilfe, sobald die Musik laeuft - auch
    ohne Playlists und Wrapped (dann nur mit diesen beiden Zeilen)."""
    import bot
    schalter = {name: False for name in dir(bot)
                if name.endswith("_ENABLED") and isinstance(getattr(bot, name), bool)}
    with contextlib.ExitStack() as stapel:
        for name, wert in dict(schalter, MUSIC_ENABLED=True).items():
            stapel.enter_context(mock.patch.object(bot, name, wert))
        seiten = [k for k, _e, _l in bot.client._help_categories()]
        assert "playlist" in seiten, seiten
        zeilen = [b for b, _ in bot._hilfe_eintraege("playlist")]
        assert zeilen == ["flo radio · radio aus", "flo skip"], zeilen
    with contextlib.ExitStack() as stapel:
        for name, wert in schalter.items():
            stapel.enter_context(mock.patch.object(bot, name, wert))
        assert "playlist" not in [k for k, _e, _l in bot.client._help_categories()]


# --- Abstimmung zum Ueberspringen ----------------------------------------------------
@contextlib.contextmanager
def _abstimmung(menschen, *, wunsch_id=1000):
    """Player in Kanal 42 mit 'menschen' Menschen (IDs ab 1000) und einem Bot.
    Es laeuft 'Schlager', gewuenscht von Mensch0, dahinter B und C.
    Liefert (player, kanal, gesagt)."""
    mi = music.instance
    player, _voice, aufraeumen = _musik_umgebung()
    kanal = _voice_kanal(menschen, 1)
    player.voice = _VoiceImKanal(kanal)
    gesagt = []

    async def sag(text):
        gesagt.append(text)
    player._sag = sag
    alt = (mi._enabled, mi._players.get(1))
    mi._enabled = True
    mi._players[1] = player
    try:
        player.start(music.Track(title="Schlager", stream_url="http://s",
                                 requested_by="Mensch0", requested_by_id=wunsch_id))
        player.queue.extend([_track("B"), _track("C")])
        yield player, kanal, gesagt
    finally:
        mi._enabled = alt[0]
        if alt[1] is None:
            mi._players.pop(1, None)
        else:
            mi._players[1] = alt[1]
        aufraeumen()


def _skip_klick(uid, kanal, **kw):
    klick = _klick_im_voice(uid, kanal, **kw)
    asyncio.run(music.instance._panel_klick(klick, "skip"))
    return klick


def test_skip_bis_zwei_leute_direkt():
    """Zu zweit wird nicht abgestimmt: Skip ist Skip, wie bisher (erst defer,
    dann wechseln, kein Kommentar). Auch wenn Discords Liste leer oder nicht
    lesbar ist - im Zweifel direkt."""
    with _abstimmung(2) as (player, kanal, gesagt):
        klick = _skip_klick(1001, kanal)
        assert klick.arten() == ["defer"] and player.current.title == "B"
        assert gesagt == [] and not player._skip_stimmen
    with _abstimmung(5) as (player, kanal, gesagt):
        player.voice = _StallVoice()          # Kanal ohne lesbare Mitglieder
        player.voice.spielt = True
        klick = _skip_klick(1001, SimpleNamespace(id=42))
        assert klick.arten() == ["defer"] and player.current.title == "B"


def test_skip_abstimmung_bei_vier_leuten_braucht_drei():
    """Vier Menschen: drei Stimmen (len//2 + 1). Jede Stimme zeigt das Panel
    mit 'x/3 wollen weg' (die Antwort) und einen Zettel 'Noch n'. Zweimal
    zaehlt nicht. Die dritte wirft den Song raus - mit oeffentlichem Urteil."""
    with _abstimmung(4) as (player, kanal, gesagt):
        klick = _skip_klick(1001, kanal)
        assert klick.arten() == ["edit", "followup"], klick.protokoll
        assert "⏭️ 1/3 wollen weg" in _texte(klick.protokoll[0][1]["view"])
        assert klick.protokoll[1][1] == "Stimme gezählt. Noch 2."
        assert klick.protokoll[1][2].get("ephemeral")
        assert player.current.title == "Schlager"

        klick = _skip_klick(1001, kanal)
        assert klick.protokoll == [("send", music._SKIP_DOPPELT, {"ephemeral": True})]

        klick = _skip_klick(1002, kanal)
        assert "⏭️ 2/3 wollen weg" in _texte(klick.protokoll[0][1]["view"])
        assert klick.protokoll[1][1] == "Stimme gezählt. Noch 1."
        assert player.current.title == "Schlager"

        klick = _skip_klick(1003, kanal)
        assert klick.arten() == ["defer"]
        assert player.current.title == "B"
        assert gesagt == ["⏭️ Demokratie hat gesprochen: **Schlager** ist raus. Mensch0, "
                          "dein Geschmack wurde gerade öffentlich abgewählt."], gesagt
        assert not player._skip_stimmen, "die Stimmen gelten nur fuer den alten Song"
        assert "wollen weg" not in _texte(music.MusikPanel(player))


def test_skip_wuenscher_und_mod_brauchen_keine_abstimmung():
    """Den eigenen Wunsch skippt man direkt (peinlich, aber ehrlich), ein Mod
    auch - von drinnen wie von draussen."""
    with _abstimmung(4) as (player, kanal, gesagt):
        klick = _skip_klick(1000, kanal)
        assert klick.arten() == ["defer"] and player.current.title == "B"
        assert gesagt == ["⏭️ U1000 skippt den eigenen Wunsch. Peinlich, aber ehrlich."]
    with _abstimmung(4) as (player, kanal, gesagt):
        _skip_klick(2001, _voice_kanal(cid=99), mod=True)
        assert player.current.title == "B"
        assert gesagt == ["⏭️ Mod-Hammer. **Schlager** ist weg, Diskussion beendet."]


def test_skip_aussenstehende_zaehlen_nicht():
    """Wer nicht im Voice sitzt, stimmt nicht mit - weder per Knopf noch per
    Text. Auf 'Ich spiele gerade nichts' bleibt es fuer alle, wenn nichts laeuft."""
    with _abstimmung(4) as (player, kanal, gesagt):
        klick = _skip_klick(2000, _voice_kanal(cid=99))
        (art, text, kw), = klick.protokoll
        assert art == "send" and text in music._ZAUNGAST and kw.get("ephemeral")
        assert not player._skip_stimmen and player.current.title == "Schlager"
        msg = SimpleNamespace(content="flo skip", guild=SimpleNamespace(id=1),
                              channel=SimpleNamespace(id=5), author=_fake_person(2000),
                              mentions=[])
        e = asyncio.run(music.instance.handle(msg))
        assert e.description in music._ZAUNGAST and player.current.title == "Schlager"
        asyncio.run(player.disconnect())
        e = asyncio.run(music.instance.handle(msg))
        assert e.description == "Ich spiele gerade nichts."


def test_skip_stimmen_gelten_nur_fuer_den_laufenden_song():
    """Ein neuer Song leert die Stimmen - ein Tempo-Neustart DESSELBEN Songs
    (keep_speed) nicht."""
    with _abstimmung(4) as (player, kanal, gesagt):
        _skip_klick(1001, kanal)
        assert player._skip_stimmen == {1001}
        assert asyncio.run(player.apply_speed(1.25))
        assert player._skip_stimmen == {1001} and player.current.title == "Schlager"
        player.voice.stop()
        player.start(_track("Neu"))
        assert player._skip_stimmen == set()


def test_skip_doppelklick_wirft_nicht_zwei_songs_raus():
    """Klickt jemand, waehrend der abgewaehlte Song noch rausfliegt, zaehlt das
    nicht fuer den naechsten: 'fliegt doch schon raus'."""
    mi = music.instance
    with _abstimmung(3) as (player, kanal, gesagt):
        person = {uid: _klick_im_voice(uid, kanal).user for uid in (1001, 1002)}
        assert mi._skip_abstimmen(player, person[1001])[:1] == ("stimme",)
        assert mi._skip_abstimmen(player, person[1002]) == ("erreicht", 2, 2)
        assert mi._skip_abstimmen(player, person[1001])[0] == "laeuft"
        klick = _skip_klick(1001, kanal)
        assert klick.protokoll == [("send", music._SKIP_LAEUFT, {"ephemeral": True})]
        assert player.current.title == "Schlager"


def test_skip_knopf_ohne_defer_skippt_trotzdem_und_nichts_haengt():
    """Scheitert das defer (3-Sekunden-Frist verpasst, Discord hustet), gilt
    der Skip trotzdem - entschieden ist entschieden. Vorher blieb der Song als
    'abgewaehlt' markiert, ohne dass geskippt wurde: jeder weitere Skip hiess
    'fliegt doch schon raus' - fuer den Wuenscher, Mods, sogar zu zweit."""
    with _abstimmung(4) as (player, kanal, gesagt):
        klick = _klick_im_voice(2001, _voice_kanal(cid=99), mod=True)
        klick.kaputt = {"defer"}
        asyncio.run(music.instance._panel_klick(klick, "skip"))
        assert player.current.title == "B" and player._abgewaehlt is None
        assert gesagt == ["⏭️ Mod-Hammer. **Schlager** ist weg, Diskussion beendet."]

    # Und wenn der Skip selbst scheitert: der Merker faellt wieder, der
    # naechste Mod-Klick wirkt.
    with _abstimmung(4) as (player, kanal, gesagt):
        echt = player.skip

        async def kaputt():
            raise RuntimeError("Discord hustet")
        player.skip = kaputt
        try:
            asyncio.run(music.instance._panel_klick(
                _klick_im_voice(2001, _voice_kanal(cid=99), mod=True), "skip"))
        except RuntimeError:
            pass
        assert player.current.title == "Schlager" and player._abgewaehlt is None
        player.skip = echt
        klick = _skip_klick(1000, kanal)                     # der Wuenscher selbst
        assert klick.arten() == ["defer"] and player.current.title == "B"

    # Dasselbe beim Textbefehl.
    with _abstimmung(4) as (player, kanal, gesagt):
        async def kaputt():
            raise RuntimeError("Discord hustet")
        player.skip = kaputt
        msg = SimpleNamespace(content="flo skip", guild=SimpleNamespace(id=1),
                              channel=SimpleNamespace(id=5),
                              author=_klick_im_voice(1000, kanal).user, mentions=[])
        try:
            asyncio.run(music.instance.handle(msg))
        except RuntimeError:
            pass
        assert player._abgewaehlt is None


def test_skip_schwelle_sinkt_wenn_leute_gehen():
    """Vier Leute, zwei Stimmen - dann geht einer, der nicht abgestimmt hat:
    drei Leute brauchen zwei Stimmen, die sind schon da. Klickt ein Waehler
    noch mal, fliegt der Song (vorher: 'Du hast schon abgestimmt', und das
    Panel zeigte '2/2 wollen weg', ohne dass etwas passierte)."""
    with _abstimmung(4) as (player, kanal, gesagt):
        _skip_klick(1001, kanal)
        _skip_klick(1002, kanal)
        assert player.current.title == "Schlager"
        kanal._leute = [m for m in kanal._leute if m.id != 1003]
        assert music.instance._skip_stand(player) == (2, 2)
        klick = _skip_klick(1001, kanal)
        assert klick.arten() == ["defer"] and player.current.title == "B"
        assert gesagt and gesagt[-1].startswith("⏭️ Demokratie hat gesprochen")

    # Gehen dagegen Waehler, zaehlen ihre Stimmen nicht mehr - doppelt bleibt doppelt.
    with _abstimmung(5) as (player, kanal, gesagt):
        _skip_klick(1001, kanal)
        _skip_klick(1002, kanal)
        kanal._leute = [m for m in kanal._leute if m.id != 1002]
        klick = _skip_klick(1001, kanal)
        assert klick.protokoll == [("send", music._SKIP_DOPPELT, {"ephemeral": True})]
        assert player.current.title == "Schlager"


def test_skip_per_text_wuenscher_und_mod():
    """'flo skip' vom Wuenscher oder einem Mod (auch von draussen) skippt ohne
    Abstimmung - und die Antwort ist das Urteil, nicht 'uebersprungen'."""
    def skip(wer):
        msg = SimpleNamespace(content="flo skip", guild=SimpleNamespace(id=1),
                              channel=SimpleNamespace(id=5), author=wer, mentions=[])
        return asyncio.run(music.instance.handle(msg))

    with _abstimmung(4) as (player, kanal, gesagt):
        e = skip(_klick_im_voice(1000, kanal).user)
        assert "skippt den eigenen Wunsch" in _embed_text(e) and player.current.title == "B"
        assert gesagt == [] and player._abgewaehlt is None
    with _abstimmung(4) as (player, kanal, gesagt):
        e = skip(_klick_im_voice(2001, _voice_kanal(cid=99), mod=True).user)
        assert "Mod-Hammer" in _embed_text(e) and player.current.title == "B"


def test_skip_per_text_stimmt_genauso_ab():
    """'flo skip' zaehlt wie der Knopf: Stimme mit Stand, doppelt, das Urteil.
    Ein Radio-Song wird anders abgewaehlt (keiner hat ihn gewuenscht). Mit
    hoechstens zwei Leuten bleibt es beim alten 'uebersprungen'."""
    def skip(wer):
        msg = SimpleNamespace(content="flo skip", guild=SimpleNamespace(id=1),
                              channel=SimpleNamespace(id=5), author=wer, mentions=[])
        return asyncio.run(music.instance.handle(msg))

    with _abstimmung(3) as (player, kanal, gesagt):
        player.current.radio = True
        leute = {uid: _klick_im_voice(uid, kanal).user for uid in (1001, 1002)}
        e = skip(leute[1001])
        assert "1/2" in _embed_text(e) and "Noch 1." in _embed_text(e)
        assert player.current.title == "Schlager"
        assert skip(leute[1001]).description == music._SKIP_DOPPELT
        e = skip(leute[1002])
        assert "Demokratie hat gesprochen" in _embed_text(e)
        assert "Sogar mein Radio wird abgewählt" in _embed_text(e)
        assert player.current.title == "B"

    with _abstimmung(2) as (player, kanal, gesagt):
        e = skip(_klick_im_voice(1001, kanal).user)
        assert e.description == "**Schlager** übersprungen." and player.current.title == "B"


# =============================================================================
# Die Warteschlange, die einen Neustart ueberlebt (musikzustand.py)
# =============================================================================
class _Protokoll:
    """Was Flo beim Zurueckholen tut - der Reihe nach."""

    def __init__(self):
        self.connect, self.resolve, self.panel, self.gesagt, self.gesendet = [], [], [], [], []
        self.gezaehlt = []
        self.log = []


class _Logfang(logging.Handler):
    """Faengt die Logzeilen eines Moduls (statt sie ins Testprotokoll zu kippen)."""

    def __init__(self, name):
        super().__init__(level=logging.DEBUG)
        self.name_ = name
        self.zeilen = []

    def emit(self, record):
        self.zeilen.append(record.getMessage())

    def __enter__(self):
        lg = logging.getLogger(self.name_)
        self.alt = (lg.level, lg.propagate)
        lg.addHandler(self)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        return self

    def __exit__(self, *_a):
        lg = logging.getLogger(self.name_)
        lg.removeHandler(self)
        lg.setLevel(self.alt[0])
        lg.propagate = self.alt[1]
        return False


@contextlib.contextmanager
def _zustand_frisch(daten=None):
    """musikzustand.setup() mit einem ECHTEN JsonStore im Test-Datenordner -
    leer oder mit 'daten' als Datei von vorher. Danach alles wie vorher."""
    kern = musikzustand.instance
    alt = dict(vars(kern))
    pfad = store.DATA_DIR / "musikzustand.json"
    for weg in (pfad, pfad.with_name(pfad.name + ".bak")):
        if weg.exists():
            weg.unlink()
    if daten is not None:
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(json.dumps(daten), encoding="utf-8")
    try:
        with _Logfang("dcbot.musikzustand"):
            assert musikzustand.setup() is True
        yield kern
    finally:
        vars(kern).clear()
        vars(kern).update(alt)


def _lied(titel, n=0, **kw):
    """Ein Song, wie er in musikzustand.json steht."""
    eintrag = {"t": titel, "q": _yt(n), "u": _yt(n), "d": 200, "w": "Anna",
               "wid": 1000, "th": "", "h": None, "r": False}
    eintrag.update(kw)
    return eintrag


def _stand(**aenderungen):
    """Ein Stand von vor dem Neustart: 'Jetzt' bei 1:23, dahinter ein Wunsch
    und ein Radio-Song, Radio an."""
    eintrag = {"v": 1, "ts": time.time() - 30, "voice": 42, "text": 5,
               "current": _lied("Jetzt", 1), "pos": 83.4, "pausiert": False,
               "radio": True, "radio_gesehen": [_vid(9)], "radio_zahl": 4,
               "geparkt": False,
               "queue": [_lied("Danach", 2, q="ytsearch1:Danach", u="", wid=1001, w="Bob"),
                         _lied("Funk", 3, w=music.RADIO_WUNSCH, wid=0, r=True)]}
    eintrag.update(aenderungen)
    return eintrag


async def _haken(haken, player):
    """Ein Test-Haken - gern auch einer, der eine Coroutine liefert."""
    if haken is not None:
        erg = haken(player)
        if asyncio.iscoroutine(erg):
            await erg


def _im_chat(text, gid=77, uid=1000):
    """Eine Chatnachricht an music.handle (Coroutine) - der ECHTE Befehlsweg."""
    msg = SimpleNamespace(content=text, guild=SimpleNamespace(id=gid),
                          channel=SimpleNamespace(id=5, name="musik"), mentions=[],
                          author=SimpleNamespace(id=uid, bot=False, display_name="Anna",
                                                 mention=f"<@{uid}>", voice=None))
    return music.instance.handle(msg)


@contextlib.contextmanager
def _zurueck_umgebung(eintrag, *, menschen=2, gid=77, kaputt=(), connect_kaputt=False,
                      beim_aufloesen=None, beim_verbinden=None, beim_sagen=None):
    """Ein Server mit Sprachkanal 42 ('menschen' Menschen und Flo), Textkanal 5
    und einem frischen Player; musikzustand mit 'eintrag' als Stand von vor dem
    Neustart. Verbinden, Aufloesen, Panel und Ansagen sind gestubbt und landen
    im Protokoll. Liefert (player, protokoll, client).

    kaputt: Titel, die sich nicht aufloesen lassen. Die Haken bekommen den
    Player (und duerfen eine Coroutine liefern, etwa _im_chat('flo stop')):
    beim_aufloesen - waehrend Flo 'Jetzt' aufloest; beim_verbinden - waehrend
    Flo verbindet, BEVOR die Verbindung steht; beim_sagen - bei der ersten
    Ansage (Flo sitzt schon im Kanal)."""
    prot = _Protokoll()
    _modul, verlauf_zurueck = _verlauf_frisch()
    player, _voice, aufraeumen = _musik_umgebung()
    player.voice = None
    player.active_channel_id = None
    player.guild_id = gid
    kanal = _voice_kanal(menschen, 1, cid=42)
    text = SimpleNamespace(id=5, name="musik")

    async def senden(inhalt=None, **_kw):
        prot.gesendet.append(inhalt)
    text.send = senden
    guild = SimpleNamespace(id=gid, name="Server", voice_client=None,
                            get_channel=lambda cid: {42: kanal, 5: text}.get(cid))
    kanal.guild = guild
    client = SimpleNamespace(get_guild=lambda g: guild if g == gid else None)

    async def verbinden(ziel):
        prot.connect.append((ziel.id, player._advancing))
        await _haken(beim_verbinden, player)
        if connect_kaputt:
            raise discord.ClientException("Discord will nicht")
        player.voice = _VoiceImKanal(ziel)
        player.active_channel_id = ziel.id
        return player.voice

    async def aufloesen(track):
        prot.resolve.append((track.title, player._advancing, track.query))
        if track.title == "Jetzt":
            await _haken(beim_aufloesen, player)
        if track.title in kaputt:
            raise RuntimeError("nicht ladbar")
        return replace(track, stream_url=f"http://stream/{track.title}")

    async def panel(_player, track, *, reply_to=None, extra=""):
        prot.panel.append((track.title, extra))
        prot.log.append(f"panel im Kontext {ai.aktuelle_guild()}")

    async def sag(inhalt):
        prot.gesagt.append(inhalt)
        if len(prot.gesagt) == 1:
            await _haken(beim_sagen, player)

    player.connect = verbinden
    player._sag = sag
    mi = music.instance
    alt_spieler = mi._players.get(gid)
    mi._players[gid] = player
    try:
        with _zustand_frisch({"guilds": {str(gid): eintrag}}), \
                mock.patch.object(musikzustand, "START_VERZOEGERUNG", 0), \
                mock.patch.object(music, "_resolve_track", aufloesen), \
                mock.patch.object(music, "_send_panel", panel), \
                mock.patch.object(musikstats, "notieren",
                                  lambda _g, t: prot.gezaehlt.append(t.title)), \
                _Logfang("dcbot.musikzustand") as fang:
            prot.log = fang.zeilen
            yield player, prot, client
    finally:
        if alt_spieler is None:
            mi._players.pop(gid, None)
        else:
            mi._players[gid] = alt_spieler
        aufraeumen()
        verlauf_zurueck()


def _zurueckholen(client):
    asyncio.run(musikzustand.wiederherstellen(client))


def test_zustand_schnappschuss_ist_json_und_kommt_gleich_zurueck():
    """Der Schnappschuss ist reines JSON: kein Track-Objekt, keine Stream-
    Adresse (laeuft ab), kein Loop und kein Tempo. Zurueck kommt jeder Song
    so, wie er war - lazy, mit Wunsch-ID, Radio-Marke, Spotify-Hinweis."""
    player, _voice, aufraeumen = _musik_umgebung()
    try:
        player.guild_id = 77
        hinweis = {"query": "A - B", "dur": 200, "title": "B", "artist": "A",
                   "liste": [1, 2]}
        player.start(music.Track(
            title="Jetzt", stream_url="http://geheim/stream", webpage_url=_yt(1),
            duration=200, requested_by="Anna", query=_yt(1), thumbnail="http://bild",
            match_hint=hinweis, requested_by_id=1000), seek=12.0)
        player.queue.extend([
            music.Track(title="Funk", stream_url="", query=_yt(3), requested_by="📻 Radio",
                        radio=True),
            music.Track(title="Geparkt", stream_url="http://s", query="ytsearch1:x",
                        weiter_ab=55.5, wiederhergestellt=True, live=True)])
        player.radio = True
        player._radio_gesehen.extend([_vid(9), _vid(8)])
        player.speed, player.loop_rest = 1.5, 3
        player.pausieren()
        eintrag = musikzustand.eintrag_bauen(player, 1_800_000_000.0)
        assert json.loads(json.dumps(eintrag)) == eintrag
        text = json.dumps(eintrag)
        assert "geheim" not in text and "loop" not in text and "speed" not in text
        assert eintrag["ts"] == 1_800_000_000.0 and eintrag["voice"] == 42
        assert eintrag["pausiert"] and eintrag["radio"] and not eintrag["geparkt"]
        assert 11.5 <= eintrag["pos"] <= 13.0, eintrag["pos"]
        assert eintrag["radio_gesehen"] == [_vid(9), _vid(8)]
        assert eintrag["current"]["h"] == {"query": "A - B", "dur": 200, "title": "B",
                                           "artist": "A", "liste": "[1, 2]"}
        zurueck = [musikzustand._song_aus(e, music) for e in eintrag["queue"]]
        assert zurueck[0].radio and zurueck[0].stream_url == "" and zurueck[0].query == _yt(3)
        assert zurueck[1].weiter_ab == 55.5 and zurueck[1].wiederhergestellt and zurueck[1].live
        jetzt = musikzustand._song_aus(eintrag["current"], music)
        assert (jetzt.title, jetzt.webpage_url, jetzt.duration, jetzt.requested_by,
                jetzt.requested_by_id, jetzt.thumbnail) == (
            "Jetzt", _yt(1), 200, "Anna", 1000, "http://bild")
        assert jetzt.stream_url == "" and jetzt.query == _yt(1)
        # Ohne Suche, aber mit Seite: die Seite wird die Eingabe. Ohne beides: weg.
        assert musikzustand._song_aus({"t": "x", "u": _yt(5)}, music).query == _yt(5)
        assert musikzustand._song_aus({"t": "x"}, music) is None
        assert musikzustand._song_aus("kaputt", music) is None
        # Nichts zu merken: kein Song, keine Schlange.
        player.current, player.queue = None, []
        assert musikzustand.eintrag_bauen(player) is None
    finally:
        aufraeumen()


def test_zustand_merken_vermerkt_nur_und_takt_baut():
    """merken() kommt in Schueben (je eingereihtem Song) - es vermerkt nur.
    Gebaut wird im Takt, einmal je Server; jede Minute frischt der Takt die
    Stellen aller laufenden Player auf (Absturz, kill -9)."""
    player, _voice, aufraeumen = _musik_umgebung()
    player.guild_id = 77
    mi = music.instance
    gebaut = []
    echt = musikzustand.eintrag_bauen
    uhr_ = [1000.0]
    try:
        with _zustand_frisch() as kern, \
                mock.patch.object(musikzustand, "eintrag_bauen",
                                  lambda p, j=None: gebaut.append(p) or echt(p, j)), \
                mock.patch.object(musikzustand, "_mono", lambda: uhr_[0]), \
                mock.patch.dict(mi._players, {77: player}, clear=True):
            asyncio.run(musikzustand.takt())         # die erste Runde frischt auf
            assert gebaut == [] and kern._guilds() == {}
            player.start(_track("A"))
            for i in range(500):
                mi._einreihen(player, _track(f"S{i}"))
            assert gebaut == [], "merken baut nichts"
            uhr_[0] += 15
            asyncio.run(musikzustand.takt())
            assert len(gebaut) == 1 and len(kern._guilds()["77"]["queue"]) == 500
            uhr_[0] += 15
            asyncio.run(musikzustand.takt())         # nichts geaendert, keine Minute um
            assert len(gebaut) == 1
            uhr_[0] += 45
            stand = kern._guilds()["77"]
            asyncio.run(musikzustand.takt())         # eine Minute um: auffrischen
            assert len(gebaut) == 2 and kern._guilds()["77"] is not stand
            # Radio-Schalter (Endlos-Radio) geht mit in den Stand.
            player.radio_setzen(True)
            uhr_[0] += 15
            asyncio.run(musikzustand.takt())
            assert kern._guilds()["77"]["radio"] is True
            # Die Schlange laeuft leer und es kommt nichts mehr: kein Stand mehr.
            player.queue.clear()
            player.current = None
            musikzustand.merken(player)
            asyncio.run(musikzustand.takt())
            assert kern._guilds() == {}
    finally:
        aufraeumen()


def test_zustand_vergessen_und_nach_dem_sichern_nichts_mehr():
    """'flo stop' (disconnect) loescht den Stand. Nach sichern() aendern weder
    merken noch vergessen etwas - beim Abmelden trennt discord.py die Voice, und
    der Song, der lief, muss im Stand bleiben. sichern() ist idempotent, und der
    naechste Start findet den Stand in der Datei."""
    player, _voice, aufraeumen = _musik_umgebung()
    player.guild_id = 77
    pfad = store.DATA_DIR / "musikzustand.json"
    try:
        with _zustand_frisch() as kern, \
                mock.patch.dict(music.instance._players, {77: player}, clear=True):
            player.start(_track("A"))
            asyncio.run(musikzustand.takt())
            assert "77" in kern._guilds()
            asyncio.run(player.disconnect())
            assert "77" not in kern._guilds(), "stop heisst: nicht zurueckkommen"
            player.voice = _voice
            player.active_channel_id = 42
            _voice.spielt = False
            player.start(_track("B"))
            music.instance._einreihen(player, _track("C"))
            with _Logfang("dcbot.musikzustand"):
                asyncio.run(musikzustand.sichern())
            vorher = pfad.read_text(encoding="utf-8")
            assert json.loads(vorher)["guilds"]["77"]["current"]["t"] == "B"
            asyncio.run(player.disconnect())          # das Abmelden
            musikzustand.merken(player)
            asyncio.run(musikzustand.takt())
            asyncio.run(musikzustand.sichern())
            asyncio.run(store.alle_sichern())
            assert pfad.read_text(encoding="utf-8") == vorher
        with _zustand_frisch(json.loads(vorher)) as kern:
            assert list(kern._alt) == ["77"]
            assert kern._alt["77"]["queue"][0]["t"] == "C"
            # Takt und Sichern lassen einen Stand, der aufs Zurueckholen
            # wartet, in Ruhe - auch wenn der Player dort leer ist.
            leer, _v, weg = _musik_umgebung()
            try:
                leer.guild_id = 77
                with mock.patch.dict(music.instance._players, {77: leer}, clear=True), \
                        _Logfang("dcbot.musikzustand"):
                    asyncio.run(musikzustand.takt())
                    asyncio.run(musikzustand.sichern())
                assert kern._guilds()["77"]["current"]["t"] == "B"
            finally:
                weg()
    finally:
        aufraeumen()


def test_zustand_kommt_mit_menschen_im_kanal_zurueck():
    """Nach dem Start: rein in den alten Kanal, den Song frisch aufloesen und an
    seiner Stelle weiterspielen, die Schlange dahinter, Radio wie vorher. Der
    Watchdog bleibt draussen (_advancing), solange das laeuft. Der Song lief
    schon - fuers Wrapped zaehlt er nicht noch einmal. Einmal je Prozess."""
    with _zurueck_umgebung(_stand()) as (player, prot, client):
        _zurueckholen(client)
        assert prot.resolve[0][:2] == ("Jetzt", True), prot.resolve
        assert prot.connect == [(42, True)]
        assert player.current.title == "Jetzt" and player.current.wiederhergestellt
        assert 83.0 <= player.position() <= 85.0, player.position()
        assert [t.title for t in player.queue] == ["Danach", "Funk"]
        assert all(t.stream_url == "" for t in player.queue), "lazy - erst beim Abspielen"
        assert player.queue[1].radio and player.queue[0].requested_by_id == 1001
        assert player.radio and player._radio_gesehen == [_vid(9)]
        assert player._radio_zahl == 4 and player._radio_hinweis
        assert player.text_channel.id == 5 and not player._advancing
        assert prot.panel == [("Jetzt", "Wieder da – weiter bei 1:23")]
        assert prot.gesagt == ["🔁 Bin wieder da. Hab eure Warteschlange mitgebracht – "
                               "3 Songs, ihr Undankbaren."]
        assert prot.gezaehlt == [], "lief schon einmal - kein zweites Mal im Wrapped"
        kern = musikzustand.instance
        assert kern._alt == {} and 77 in kern._geaendert
        # Der Song ist zu Ende - der naechste zaehlt ganz normal.
        player.voice.spielt = False
        player._played, player._seg_start = 200.0, None
        asyncio.run(player._advance(player._play_gen))
        assert player.current.title == "Danach" and prot.gezaehlt == ["Danach"]
        # Ein zweites on_ready (Reconnect) ist kein Neustart.
        _zurueckholen(client)
        assert len(prot.connect) == 1


def test_zustand_kommt_pausiert_zurueck():
    with _zurueck_umgebung(_stand(pausiert=True, radio=False)) as (player, prot, client):
        _zurueckholen(client)
        assert player.ist_pausiert() and player.voice.is_paused()
        assert not player.radio and not player._radio_hinweis
        assert prot.panel == [("Jetzt", "Wieder da – pausiert bei 1:23")]
        assert "auf Pause" in prot.gesagt[0] and "flo weiter" in prot.gesagt[0].lower()


def test_zustand_wird_verworfen_wenn_keiner_da_ist_oder_er_zu_alt_ist():
    """Kein Mensch im Kanal (Bots zaehlen nicht), aelter als 20 Minuten,
    Kanal oder Server weg, Musik dort aus, nichts drin: Flo bleibt draussen
    und verwirft den Stand - je eine Logzeile, warum."""
    faelle = (
        ({}, {"menschen": 0}, "keiner in 'Musik'"),
        ({"ts": time.time() - 21 * 60}, {}, "21 Minuten alt"),
        ({"ts": time.time() + 3 * 3600}, {}, "-180 Minuten alt"),
        ({"voice": 999}, {}, "Sprachkanal weg"),
        ({}, {"fremd": True}, "Server nicht mehr da"),
        ({"current": None, "queue": []}, {}, "nichts drin"),
        ({"current": {"t": "ohne alles"}, "queue": ["kaputt"]}, {}, "nichts drin"),
    )
    for aenderung, umgebung, grund in faelle:
        eintrag = _stand(**aenderung)
        fremd = umgebung.pop("fremd", False)
        with _zurueck_umgebung(eintrag, **umgebung) as (player, prot, client):
            if fremd:
                client.get_guild = lambda _g: None
            _zurueckholen(client)
            assert prot.connect == [] and player.current is None and not player.queue, grund
            assert musikzustand.instance._guilds() == {} and musikzustand.instance._alt == {}
            assert any(grund in z for z in prot.log), (grund, prot.log)
    with _zurueck_umgebung(_stand()) as (player, prot, client), \
            mock.patch.object(features, "is_on_in", lambda _g, k: k != "music"):
        _zurueckholen(client)
        assert prot.connect == [] and any("Musik dort aus" in z for z in prot.log)
    # Einen Stand aus der Zukunft gibt es nach dem Uhrsprung beim Booten: bis
    # zu zweieinhalb Stunden ist das kein Fehler.
    with _zurueck_umgebung(_stand(ts=time.time() + 2 * 3600)) as (player, prot, client):
        _zurueckholen(client)
        assert prot.connect == [(42, True)] and player.current.title == "Jetzt"


def test_zustand_wartet_auf_den_uhrsprung_beim_booten():
    """Startet Flo, solange die Uhr noch zwei Stunden vorgeht, sieht ein
    frischer Stand zwei Stunden alt aus. Solange die Uhr nicht stabil ist,
    schaut er deshalb nach, ob sie zurueckspringt - erst dann verwirft er."""
    wand = [time.time() + 7200]
    takte = []

    def jetzt():
        return wand[0]

    async def schlafen(sekunden):
        takte.append(sekunden)
        if len(takte) == 3:
            wand[0] -= 7200                   # NTP stellt die Uhr zurueck

    with _zurueck_umgebung(_stand(ts=time.time() - 60)) as (player, prot, client), \
            mock.patch.object(musikzustand, "_jetzt", jetzt), \
            mock.patch.object(musikzustand, "_schlafen", schlafen), \
            mock.patch.object(musikzustand.uhr, "stabil", lambda: False):
        _zurueckholen(client)
        assert player.current.title == "Jetzt" and prot.connect == [(42, True)]
        assert len(takte) == 3, takte
    # Springt sie nicht, ist der Stand nach UHR_NACHHOLEN wirklich zu alt.
    mono = [0.0]

    def monotonic():
        mono[0] += 50.0
        return mono[0]

    async def warten(_s):
        return None

    with _zurueck_umgebung(_stand(ts=time.time() - 7200)) as (player, prot, client), \
            mock.patch.object(musikzustand, "_mono", monotonic), \
            mock.patch.object(musikzustand, "_schlafen", warten), \
            mock.patch.object(musikzustand.uhr, "stabil", lambda: False):
        _zurueckholen(client)
        assert prot.connect == [] and any("120 Minuten alt" in z for z in prot.log)


def test_zustand_geparkt_kommt_geparkt_zurueck_und_weiter_setzt_fort():
    """Eine geparkte Schlange (der Watchdog kam nicht mehr in den Voice) kommt
    GEPARKT zurueck: kein Beitritt, kein Post. 'flo weiter' setzt fort - mit
    dem Song vorn an seiner Stelle, der fuers Wrapped nicht noch mal zaehlt."""
    geparkt = _stand(current=None, geparkt=True, voice=0, pos=0.0,
                     queue=[_lied("Jetzt", 1, ab=83.4, wh=True), _lied("Danach", 2)])
    with _zurueck_umgebung(geparkt) as (player, prot, client):
        _zurueckholen(client)
        assert prot.connect == [] and prot.gesagt == [] and prot.panel == []
        assert player._geparkt and player.current is None
        assert [t.title for t in player.queue] == ["Jetzt", "Danach"]
        assert player.queue[0].weiter_ab == 83.4 and player.queue[0].wiederhergestellt
        assert not music.instance.is_voice_busy(77), "geparkt belegt den Kanal nicht"
        kanal = _voice_kanal(1, 0, cid=43)
        msg = SimpleNamespace(author=SimpleNamespace(voice=SimpleNamespace(channel=kanal)))
        antwort = asyncio.run(music.instance._schlange_fortsetzen(player, msg))
        assert antwort is music.HANDLED and prot.connect == [(43, False)]
        assert player.current.title == "Jetzt" and 83.0 <= player.position() <= 85.0
        assert not player._geparkt and prot.gezaehlt == []


def test_zustand_in_der_sendepause_geparkt_und_still():
    """Die Sendepause prueft bot.py beim Zurueckholen nicht - musikzustand selbst:
    kein Beitritt, kein Post, die Schlange wartet geparkt auf 'weiter'."""
    with _zurueck_umgebung(_stand()) as (player, prot, client), \
            mock.patch.object(musikzustand, "_sendepause", lambda: True):
        _zurueckholen(client)
        assert prot.connect == [] and prot.gesagt == [] and prot.gesendet == []
        assert player._geparkt and [t.title for t in player.queue] == ["Jetzt", "Danach", "Funk"]
        assert player.queue[0].weiter_ab == 83.4 and player.queue[0].wiederhergestellt
        assert player.radio and player.text_channel.id == 5
        assert any("Sendepause" in z for z in prot.log)


def test_zustand_live_ohne_spulen_und_durchgelaufen_ist_durch():
    live = _stand(current=_lied("Jetzt", 1, lv=True, d=None))
    with _zurueck_umgebung(live) as (player, prot, client):
        _zurueckholen(client)
        assert player.current.title == "Jetzt" and player.position() < 2.0
        assert prot.panel == [("Jetzt", "Wieder da – weiter bei 0:00")]
    # Stand bei 3:19 von 3:20: der Song war durch - weiter mit dem naechsten.
    with _zurueck_umgebung(_stand(pos=199.0)) as (player, prot, client):
        _zurueckholen(client)
        assert [r[0] for r in prot.resolve] == ["Danach"]
        assert player.current.title == "Danach" and prot.gezaehlt == ["Danach"]
        assert "2 Songs" in prot.gesagt[0]


def test_zustand_song_nicht_ladbar_naechster_kommt():
    with _zurueck_umgebung(_stand(), kaputt=("Jetzt",)) as (player, prot, client):
        _zurueckholen(client)
        assert player.current.title == "Danach" and [t.title for t in player.queue] == ["Funk"]
        assert prot.panel == [("Danach", "")] and len(prot.gesagt) == 1
        assert not player._advancing
    # Nur der eine Song, und der laedt nicht: Flo setzt sich nicht fuer nichts rein.
    with _zurueck_umgebung(_stand(queue=[]), kaputt=("Jetzt",)) as (player, prot, client):
        _zurueckholen(client)
        assert prot.connect == [] and prot.gesagt == [] and player.current is None


def test_zustand_flo_stop_waehrend_flo_zurueckkommt():
    """'flo stop' - ueber den ECHTEN Befehlsweg - in den Sekunden, in denen
    Flo zurueckkommt: vor dem Zurueckholen, waehrend er aufloest, waehrend er
    verbindet, waehrend er sich meldet. Jedes Mal startet nichts, nichts
    bleibt als Geisterschlange im Player, Flo sitzt hinterher in keinem Kanal,
    und der Stand ist weg (kommt auch beim naechsten Neustart nicht wieder)."""
    mi = music.instance
    antworten = []

    async def stopp(_player):
        antworten.append(await _im_chat("flo stop"))

    def geraeumt(player, prot, client):
        assert "Musik gestoppt" in _embed_text(antworten[-1]), _embed_text(antworten[-1])
        assert player.current is None and not player.queue and not player._geparkt
        assert player.voice is None and player.active_channel_id is None
        assert not player._advancing and not player.radio
        assert not musikzustand.ausstehend(77)
        asyncio.run(musikzustand.takt())
        assert musikzustand.instance._guilds() == {}, "kein Stand fuer den naechsten Neustart"
        assert prot.panel == [] and not any("Bin wieder da" in t for t in prot.gesagt[1:])

    with mock.patch.object(mi, "_enabled", True):
        # Vor dem Zurueckholen (Flo wartet noch auf Discord): der Stopp meint
        # den Stand, der gleich kaeme - nicht "Ich bin in keinem Sprachkanal".
        with _zurueck_umgebung(_stand()) as (player, prot, client):
            assert musikzustand.ausstehend(77) and not musikzustand.ausstehend(78)
            asyncio.run(stopp(player))
            _zurueckholen(client)
            assert prot.connect == [] and prot.resolve == [] and prot.gesagt == []
            geraeumt(player, prot, client)
        # Waehrend des Aufloesens (noch kein Kanal, keine Schlange).
        with _zurueck_umgebung(_stand(), beim_aufloesen=stopp) as (player, prot, client):
            _zurueckholen(client)
            assert prot.connect == [] and prot.gesagt == []
            geraeumt(player, prot, client)
        # Waehrend des Verbindens - die Verbindung kommt erst NACH dem Stopp
        # zustande: Flo geht wieder raus, statt ohne Sitzung im Kanal zu haengen.
        with _zurueck_umgebung(_stand(), beim_verbinden=stopp) as (player, prot, client):
            _zurueckholen(client)
            assert prot.connect == [(42, True)] and prot.gesagt == []
            geraeumt(player, prot, client)
        # Waehrend der Ansage (Flo sitzt schon drin): kein Start, kein Einreihen.
        with _zurueck_umgebung(_stand(), beim_sagen=stopp) as (player, prot, client):
            _zurueckholen(client)
            assert len(prot.gesagt) == 1
            geraeumt(player, prot, client)
        # 'Flo hau ab' (Alltagswort) meint den Stand genauso.
        with _zurueck_umgebung(_stand()) as (player, prot, client):
            antworten.append(asyncio.run(_im_chat("flo hau ab")))
            _zurueckholen(client)
            assert prot.connect == []
            geraeumt(player, prot, client)
            # Ohne Stand, der zurueckkommt, bleibt alles wie immer: 'hau ab'
            # beantwortet die KI, 'stop' kennt keinen Sprachkanal.
            assert asyncio.run(_im_chat("flo hau ab")) is None
            assert "keinem Sprachkanal" in _embed_text(asyncio.run(_im_chat("flo stop")))
    # Nach dem Zurueckholen ist nichts mehr ausstehend - und ohne das Modul nie.
    with _zurueck_umgebung(_stand()) as (player, prot, client):
        _zurueckholen(client)
        assert player.current.title == "Jetzt" and not musikzustand.ausstehend(77)
        musikzustand.instance._enabled = False
        assert not musikzustand.ausstehend(77)


def test_zustand_neue_musik_vorher_oder_waehrenddessen_alter_stand_hinten_an():
    """Spielt jemand gleich nach dem Start selbst etwas - bevor Flo zurueckholt,
    waehrend er aufloest oder waehrend er sich meldet -, stellt sich der alte
    Stand jedes Mal hinten an (soweit der Deckel reicht). Flo wechselt nicht in
    den alten Kanal, Radio bleibt, wie es ist, und er sagt es."""
    def neu(player):
        player.voice = _VoiceImKanal(_voice_kanal(1, 1, cid=50))
        player.active_channel_id = 50
        player.text_channel = SimpleNamespace(id=6)
        player.start(_track("Neu"))

    def hinten_an(player, prot):
        assert player.current.title == "Neu"
        assert [t.title for t in player.queue] == ["Jetzt", "Danach", "Funk"]
        assert player.queue[0].weiter_ab == 83.4 and player.queue[0].wiederhergestellt
        assert player.queue[0].stream_url == "", "lazy - die Adresse liefe in der Schlange ab"
        assert not player.radio, "Radio bleibt, wie es ist"
        assert player.active_channel_id == 50 and player.text_channel.id == 6
        assert any("hinten drangehängt" in t and "3 Songs" in t for t in prot.gesagt), prot.gesagt
        assert musikzustand.instance._alt == {} and not player._advancing

    # Vorher: das 'spiel' war schneller als das Zurueckholen.
    with _zurueck_umgebung(_stand()) as (player, prot, client):
        neu(player)
        _zurueckholen(client)
        assert prot.connect == [] and prot.resolve == []
        hinten_an(player, prot)
    # Waehrend des Aufloesens.
    with _zurueck_umgebung(_stand(), beim_aufloesen=neu) as (player, prot, client):
        _zurueckholen(client)
        assert prot.connect == []
        hinten_an(player, prot)
    # Waehrend der Ansage (Flo ist schon drin): nicht drueberbuegeln.
    with _zurueck_umgebung(_stand(), beim_sagen=neu) as (player, prot, client):
        _zurueckholen(client)
        assert player.current.title == "Neu"
        assert [t.title for t in player.queue] == ["Jetzt", "Danach", "Funk"]
    # Der Deckel des Servers gilt auch hier.
    with _zurueck_umgebung(_stand()) as (player, prot, client), \
            mock.patch.object(music, "max_queue", lambda _g=0: 2):
        neu(player)
        player.queue.append(_track("Wunsch"))
        _zurueckholen(client)
        assert [t.title for t in player.queue] == ["Wunsch", "Jetzt"]
        assert "1 Song" in prot.gesagt[0]
    # Flo sass schon im Kanal ('flo komm'), aber es lief nichts: dann geht es
    # mit dem alten Stand los - an seiner Stelle, ohne Wrapped-Zaehlung.
    with _zurueck_umgebung(_stand()) as (player, prot, client):
        player.voice = _VoiceImKanal(_voice_kanal(1, 1, cid=50))
        player.active_channel_id = 50
        _zurueckholen(client)
        assert player.current.title == "Jetzt" and 83.0 <= player.position() <= 85.0
        assert [t.title for t in player.queue] == ["Danach", "Funk"]
        assert prot.gezaehlt == [] and prot.connect == []


def test_zustand_ansagen_mit_dem_namen_des_servers():
    """Das Zurueckholen laeuft in einem eigenen Task (aus on_ready) - trotzdem
    heissen Ansagen und Panel nach dem Praefix DIESES Servers, nicht nach der
    .env: dort hoert Flo auf 'Bob', und '`Flo weiter`' ginge ins Leere."""
    echt = guildcfg.get

    def get(gid, key):
        return "Bob" if key == "praefix" and int(gid) == 77 else echt(gid, key)

    with mock.patch.object(guildcfg, "get", get):
        with _zurueck_umgebung(_stand(pausiert=True)) as (player, prot, client):
            _zurueckholen(client)
            assert "`Bob weiter`" in prot.gesagt[0] and "Flo weiter" not in prot.gesagt[0]
            assert "panel im Kontext 77" in prot.log, prot.log
        with _zurueck_umgebung(_stand(), connect_kaputt=True) as (player, prot, client):
            _zurueckholen(client)
            assert "`Bob weiter`" in prot.gesagt[0], prot.gesagt
        assert ai.aktuelle_guild() == 0, "der Kontext bleibt nicht haengen"


def test_zustand_parkt_wenn_discord_nicht_reinlaesst():
    with _zurueck_umgebung(_stand(), connect_kaputt=True) as (player, prot, client):
        _zurueckholen(client)
        assert prot.connect == [(42, True)] and player.current is None
        assert player._geparkt and player.active_channel_id is None
        assert [t.title for t in player.queue] == ["Jetzt", "Danach", "Funk"]
        assert "geparkt" in prot.gesagt[0] and not player._advancing

# =============================================================================
# Musik-Wrapped (musikstats.py, render.musik_wrapped)
# =============================================================================
@contextlib.contextmanager
def _stats_frisch(daten=None):
    """musikstats.setup() mit einem ECHTEN JsonStore im Test-Datenordner - leer
    oder mit 'daten' als Datei. Danach alles wie vorher (auch die Datei weg:
    der Rauchtest soll kein fremdes Wrapped finden)."""
    kern = musikstats.instance
    alt = dict(vars(kern))
    pfad = store.DATA_DIR / "musikstats.json"

    def wegraeumen():
        for weg in (pfad, pfad.with_name(pfad.name + ".bak")):
            if weg.exists():
                weg.unlink()
    wegraeumen()
    if daten is not None:
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(json.dumps(daten), encoding="utf-8")
    try:
        with _Logfang("dcbot.musikstats"):
            assert musikstats.setup() is True
        yield kern
    finally:
        vars(kern).clear()
        vars(kern).update(alt)
        wegraeumen()


@contextlib.contextmanager
def _uhr_gestellt(wand, mono):
    """Eine eigene Uhr (laengst gestartet) hinter allem, was musikstats fragt.
    wand/mono sind Listen mit einem Wert - der Test stellt sie."""
    u = uhr.Uhr(zeit=lambda: wand[0], mono=lambda: mono[0])
    u._start = float("-inf")
    with mock.patch.multiple(uhr, jetzt=u.jetzt, stabil=u.stabil, monat_key=u.monat_key,
                             vormonat_key=u.vormonat_key, glaubhaft=u.glaubhaft), \
            _Logfang("dcbot.uhr"):
        yield u


def _wann(tag, stunde, minute=0, monat=11, jahr=2026):
    from datetime import datetime
    return datetime(jahr, monat, tag, stunde, minute, tzinfo=uhr.TIMEZONE).timestamp()


def _lauf(titel, n=0, *, wer=0, name="", dauer=200, radio=False, url=None, **kw):
    """Ein gestarteter Song, wie music.py ihn an notieren gibt."""
    return music.Track(title=titel, stream_url="http://s", duration=dauer,
                       webpage_url=_yt(n) if url is None else url, requested_by=name,
                       requested_by_id=wer, radio=radio, **kw)


def _monat_mit(gesamt=12, leute=((1000, 8, "Anna"), (1001, 4, "Bob")), radio=3):
    """Zaehler eines Monats: 'gesamt' Starts auf drei Songs verteilt."""
    songs = {"yt:a": {"t": "Darude - Sandstorm", "u": _yt(1), "n": gesamt - 4, "sek": 900},
             "yt:b": {"t": "Rick Astley - Never Gonna Give You Up", "u": _yt(2), "n": 3,
                      "sek": 600},
             "yt:c": {"t": "Nickelback - Photograph", "u": _yt(3), "n": 1, "sek": 250}}
    return {"songs": songs,
            "wer": {str(uid): {"n": n, "sek": n * 200, "name": name} for uid, n, name in leute},
            "gesamt": gesamt, "sek": gesamt * 200, "radio": radio}


class _WrappedKanal:
    def __init__(self, cid=600, *, ohne_bild=False):
        self.id = cid
        self.gesendet = []
        self.ohne_bild = ohne_bild

    def permissions_for(self, _wer):
        return SimpleNamespace(send_messages=True)

    async def send(self, content=None, **kw):
        if self.ohne_bild and "file" in kw:
            raise _http_fehler("Missing Permissions")
        self.gesendet.append((content, kw))
        return SimpleNamespace(id=len(self.gesendet))


class _WrappedServer:
    """Ein Server mit Rollen und Mitgliedern, die man holen muss (fetch_member)."""

    def __init__(self, gid=77, *, rollen_recht=True):
        self.id = gid
        self.name = "Testserver"
        self.roles = []
        self.me = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(
            manage_roles=rollen_recht))
        self.kanal = _WrappedKanal()
        self.mitglieder = {}
        self.protokoll = []

    def get_channel(self, cid):
        return self.kanal if cid == self.kanal.id else None

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def create_role(self, **kw):
        rolle = SimpleNamespace(id=555, name=kw["name"])
        self.roles.append(rolle)
        self.protokoll.append(("rolle_neu", kw["name"]))
        return rolle

    def mitglied(self, uid, *rollen):
        server = self
        person = SimpleNamespace(id=uid, roles=list(rollen))

        async def geben(rolle, reason=None):
            person.roles.append(rolle)
            server.protokoll.append(("geben", uid))

        async def nehmen(rolle, reason=None):
            person.roles.remove(rolle)
            server.protokoll.append(("nehmen", uid))
        person.add_roles, person.remove_roles = geben, nehmen
        self.mitglieder[uid] = person
        return person

    async def fetch_member(self, uid):
        if uid not in self.mitglieder:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "weg")
        return self.mitglieder[uid]


@contextlib.contextmanager
def _wrapped_umgebung(daten=None, *, coins=None):
    """musikstats frisch, economy mit Fake-Store, ein Server im Client. Liefert
    (kern, server, client)."""
    zurueck = _with_economy(coins or {})
    server = _WrappedServer()
    client = SimpleNamespace(guilds=[server], _event_channel=lambda g: g.kanal)
    try:
        with _stats_frisch(daten) as kern, \
                mock.patch.object(ai, "is_enabled", lambda: False), \
                _Logfang("dcbot.musikstats") as fang:
            kern.log = fang.zeilen
            yield kern, server, client
    finally:
        zurueck()


def _ticken(client):
    asyncio.run(musikstats.tick(client))


def test_wrapped_zaehlt_starts_und_radio_nur_als_zahl():
    """Jeder neu gestartete Song zaehlt - je Song (YouTube-ID ohne Tracking,
    sonst die Adresse ohne '?', sonst der Titel) und je Wuenscher. Radio-Songs
    zaehlen nur als 'radio', zurueckgeholte (Neustart, Parken) gar nicht."""
    wand, mono = [_wann(15, 20, monat=10)], [1000.0]
    with _stats_frisch() as kern, _uhr_gestellt(wand, mono):
        n = musikstats.notieren
        n(77, _lauf("Sandstorm", url=f"{_yt(1)}&t=30&list=RDxyz&si=abc", wer=5, name="Anna"))
        n(77, _lauf("Sandstorm (anders)", url=f"https://youtu.be/{_vid(1)}?si=q", wer=6,
                    name="Bob", dauer=180))
        n(77, _lauf("SC", url="https://soundcloud.com/a/b?si=123&utm=x", wer=5, name="Anna"))
        n(77, _lauf("Nur Text", url="", query="ytsearch1:nur text", wer=5, name="Anna2"))
        n(77, _lauf("Funk", 9, radio=True, name=music.RADIO_WUNSCH))
        n(77, _lauf("Zurueck", 8, wer=5, wiederhergestellt=True))
        n(77, _lauf("Ohne Wunsch", 7, wer=0, dauer=None))
        m = kern.daten(77, "2026-10")
        assert set(m["songs"]) == {f"yt:{_vid(1)}", "https://soundcloud.com/a/b", "nur text",
                                   f"yt:{_vid(7)}"}, m["songs"]
        sand = m["songs"][f"yt:{_vid(1)}"]
        assert (sand["n"], sand["sek"], sand["t"]) == (2, 380, "Sandstorm (anders)")
        assert m["wer"]["5"] == {"n": 3, "sek": 600, "name": "Anna2"}
        assert m["wer"]["6"]["n"] == 1 and "0" not in m["wer"]
        assert (m["gesamt"], m["sek"], m["radio"]) == (5, 780, 1)
        assert all("Funk" != e["t"] for e in m["songs"].values()), "Radio ist kein Wunsch"
        # Andere Server zaehlen fuer sich, und kaputte Tracks werfen nie.
        n(78, _lauf("Woanders", 3))
        assert kern.daten(78, "2026-10")["gesamt"] == 1 and kern.daten(77, "2026-10")["gesamt"] == 5
        n(77, SimpleNamespace(title=None, duration="lang", requested_by_id="x"))
        n(0, _lauf("ohne Server"))
        n(77, None)
        # Aus ist aus.
        kern._enabled = False
        n(77, _lauf("Aus", 4, wer=5))
        assert kern.daten(77, "2026-10")["gesamt"] == 6


def test_wrapped_zaehlt_ueber_den_echten_musik_weg():
    """music.start -> verlauf_notieren -> notieren. Loop-Durchlauf und Tempo-
    Neustart (keep_speed) sind kein neuer Start, ein geparkter Song nach
    'weiter' auch nicht."""
    wand, mono = [_wann(15, 20, monat=10)], [1000.0]
    _modul, verlauf_zurueck = _verlauf_frisch()
    player, voice, aufraeumen = _musik_umgebung()
    player.guild_id = 77
    try:
        with _stats_frisch() as kern, _uhr_gestellt(wand, mono):
            song = _lauf("Wunsch", 1, wer=5, name="Anna")
            player.start(song)
            voice.spielt = False
            player.start(song, seek=30.0, keep_speed=True)
            voice.spielt = False
            player.start(replace(song, wiederhergestellt=True))
            m = kern.daten(77, "2026-10")
            assert m["gesamt"] == 1 and m["wer"]["5"]["n"] == 1
    finally:
        aufraeumen()
        verlauf_zurueck()


def test_wrapped_deckel_je_monat_und_dreizehn_monate():
    wand, mono = [_wann(15, 20, monat=1)], [1000.0]
    with _stats_frisch() as kern, _uhr_gestellt(wand, mono), \
            mock.patch.object(musikstats, "MAX_SONGS", 3):
        for i in range(3):
            musikstats.notieren(77, _lauf(f"S{i}", i))
        musikstats.notieren(77, _lauf("S0 nochmal", 0))
        musikstats.notieren(77, _lauf("S2 nochmal", 2))
        musikstats.notieren(77, _lauf("Neu", 9))
        songs = kern.daten(77, "2026-01")["songs"]
        assert set(songs) == {f"yt:{_vid(0)}", f"yt:{_vid(2)}", f"yt:{_vid(9)}"}, songs
        # 15 Monate gezaehlt - die letzten 13 bleiben.
        for monat in range(1, 16):
            jahr, mon = 2026 + (monat - 1) // 12, (monat - 1) % 12 + 1
            wand[0] = _wann(10, 12, monat=mon, jahr=jahr)
            musikstats.notieren(77, _lauf("x", 1))
        monate = sorted(kern._guild(77)["monate"])
        assert len(monate) == 13 and monate[0] == "2026-03" and monate[-1] == "2027-03", monate


def test_wrapped_dj_und_top_songs():
    """DJ = die meisten Wuensche; Gleichstand: wessen Songs laenger liefen,
    dann die kleinere ID - fest, nicht zufaellig."""
    kern = musikstats.instance
    m = {"songs": {"a": {"t": "A", "n": 2, "sek": 100}, "b": {"t": "B", "n": 2, "sek": 300},
                   "c": {"t": "C", "n": 5, "sek": 10}, "x": "kaputt"},
         "wer": {"7": {"n": 4, "sek": 100, "name": "Sieben"},
                 "3": {"n": 4, "sek": 900, "name": "Drei"},
                 "2": {"n": 4, "sek": 900, "name": "Zwei"},
                 "0": {"n": 99, "name": "niemand"}, "9": {"n": 0}}}
    assert kern.top_songs(m) == [("C", 5), ("B", 2), ("A", 2)]
    assert [uid for uid, _e in kern.rangliste(m)] == [2, 3, 7]
    assert kern.dj(m)[0] == 2 and kern.dj({}) is None
    # Eine von Hand kaputt editierte Datei sprengt nichts.
    assert kern.top_songs({"songs": ["x"]}) == [] and kern.rangliste({"wer": "x"}) == []


def test_wrapped_monatspost_genau_einmal_auch_nach_uhrsprung():
    """Am 1. ab 10 Uhr, einmal je Server, nur bei stabiler Uhr. Springt die
    Uhr nach dem Booten zwei Stunden zurueck (vor 10 Uhr, oder in den Vormonat),
    feuert nichts doppelt - 'gepostet' laeuft nur vorwaerts. Der DJ wird einmal
    bezahlt und als Einziger gepingt."""
    import economy
    wand, mono = [_wann(1, 9, 55)], [1000.0]
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono):
        _ticken(client)
        assert server.kanal.gesendet == [], "vor 10 Uhr nicht"
        mono[0] += _wann(1, 10, 5) - wand[0]         # die Zeit vergeht, kein Sprung
        wand[0] = _wann(1, 10, 5)
        _ticken(client)
        assert len(server.kanal.gesendet) == 1
        text, kw = server.kanal.gesendet[0]
        assert "Musik-Wrapped Oktober" in text and "<@1000>" in text
        assert "+5.000 Flo Coins" in text and "8 Wünschen" in text
        assert kw["allowed_mentions"].users == [discord.Object(1000)]
        assert not kw["allowed_mentions"].everyone and not kw["allowed_mentions"].roles
        assert kw["file"].filename == "wrapped.png"
        assert economy.instance._profile(1000)["coins"] == 5000
        g = kern._guild(77)
        assert g["gepostet"] == "2026-10" and g["dj"]["uid"] == 1000 and g["dj"]["bezahlt"]
        def vergeht(sekunden):
            wand[0] += sekunden
            mono[0] += sekunden

        def springt(sekunden):
            wand[0] += sekunden
            mono[0] += 1

        # Weiter ticken; die Uhr springt zurueck (vor 10 Uhr, dann in den
        # Oktober) und wieder vor - nach jedem Sprung erst die Gnadenfrist.
        for wie, sekunden in ((vergeht, 60), (springt, -7200), (vergeht, 400),
                              (vergeht, 7200), (springt, -86400 * 2), (vergeht, 400),
                              (springt, 86400 * 2), (vergeht, 400)):
            wie(sekunden)
            _ticken(client)
        assert wand[0] > _wann(1, 10, 5) and len(server.kanal.gesendet) == 1, server.kanal.gesendet
        assert economy.instance._profile(1000)["coins"] == 5000
        # Ein gespeicherter Monat WEIT in der Zukunft (kaputte Datei) haelt nichts an.
        g["gepostet"] = "2099-01"
        vergeht(60)
        _ticken(client)
        assert len(server.kanal.gesendet) == 2
        assert economy.instance._profile(1000)["coins"] == 5000, "derselbe Monat zahlt nie zweimal"


def test_wrapped_holt_nach_und_wartet_auf_die_uhr():
    """Offline am 1.? Bis zum 7. kommt das Wrapped nach - am 8. nicht mehr.
    Gleich nach dem Start (Uhr nicht stabil) wartet es, und in der Sendepause
    auch: verschoben, nicht verloren."""
    import economy
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    with _wrapped_umgebung(daten) as (kern, server, client):
        with _uhr_gestellt([_wann(8, 12)], [1000.0]):
            _ticken(client)
        assert server.kanal.gesendet == [] and "gepostet" not in kern._guild(77)
        with _uhr_gestellt([_wann(5, 3)], [1000.0]):
            with mock.patch.object(uhr, "stabil", lambda: False):
                _ticken(client)
            assert server.kanal.gesendet == []
            with mock.patch.object(musikstats, "_sendepause", lambda: True):
                _ticken(client)
            assert server.kanal.gesendet == []
            _ticken(client)
        assert len(server.kanal.gesendet) == 1 and kern._guild(77)["gepostet"] == "2026-10"
        assert economy.instance._profile(1000)["coins"] == 5000


def test_wrapped_unter_zehn_songs_kein_post_und_keine_coins():
    import economy
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit(gesamt=9)}}}}
    wand, mono = [_wann(1, 11)], [1000.0]
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono):
        _ticken(client)
        assert server.kanal.gesendet == [] and kern._guild(77)["gepostet"] == "2026-10"
        assert economy.instance._profile(1000)["coins"] == 0
        assert any("nur 9 Songs" in z for z in kern.log)
    # Musik auf dem Server aus: gar nichts, auch kein 'gepostet'.
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(features, "is_on_in", lambda _g, k: k != "music"):
        _ticken(client)
        assert server.kanal.gesendet == [] and "gepostet" not in kern._guild(77)


def test_wrapped_bezahlt_erst_nach_dem_festhalten_und_nie_halb():
    """Lock-before-pay: 'gepostet' und der DJ stehen auf der Platte, BEVOR die
    Coins fliessen. Bricht der Takt mittendrin ab (ALLTAG_FRIST), wird trotzdem
    zu Ende bezahlt - und der naechste Takt zahlt nicht noch einmal."""
    import economy
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    wand, mono = [_wann(1, 11)], [1000.0]
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono):
        pfad = store.DATA_DIR / "musikstats.json"
        gesehen = []
        echt = kern._store.save
        frei = []

        async def langsam():
            gesehen.append((json.loads(pfad.read_text(encoding="utf-8"))["guilds"]["77"]
                            .get("gepostet") if pfad.exists() else None,
                            economy.instance._profile(1000)["coins"]))
            ok = await echt()
            await frei[0].wait()
            return ok

        async def lauf():
            frei.append(asyncio.Event())
            with mock.patch.object(kern._store, "save", langsam):
                takt = asyncio.ensure_future(musikstats.tick(client))
                for _ in range(20):
                    await asyncio.sleep(0)
                takt.cancel()               # ALLTAG_FRIST schlaegt zu
                frei[0].set()
                with contextlib.suppress(asyncio.CancelledError):
                    await takt
                for _ in range(500):
                    if kern._guild(77).get("dj", {}).get("bezahlt"):
                        break
                    await asyncio.sleep(0.01)
        asyncio.run(lauf())
        assert gesehen[0] == (None, 0), "vor dem Festhalten ist noch nichts bezahlt"
        assert json.loads(pfad.read_text(encoding="utf-8"))["guilds"]["77"]["gepostet"] == "2026-10"
        assert economy.instance._profile(1000)["coins"] == 5000
        assert kern._guild(77)["dj"]["bezahlt"]
        assert server.kanal.gesendet == [] and not kern._guild(77)["dj"]["verkuendet"]
        # Der naechste Takt zahlt nicht noch einmal - holt aber den Post nach
        # (der Abbruch kam vor dem ersten Versuch, also ohne Pause).
        _ticken(client)
        assert economy.instance._profile(1000)["coins"] == 5000
        assert len(server.kanal.gesendet) == 1 and "<@1000>" in server.kanal.gesendet[0][0]
        assert "+5.000 Flo Coins" in server.kanal.gesendet[0][0]


def test_wrapped_dj_rolle_wandert_zum_neuen_dj():
    """Mit 'dj_rolle' an und 'Rollen verwalten': der neue DJ bekommt die Rolle,
    wer sie bisher hatte, verliert sie (beide per fetch_member - der Cache kennt
    sie ohne Mitglieder-Intent oft nicht). Wer sie hat, merkt sich Flo erst,
    wenn Discord sie wirklich vergeben hat: ein Monat ohne Schalter, ohne Recht
    oder mit einer Abfuhr von Discord laesst sie also nicht beim Falschen
    kleben. Ein Monat ohne DJ (zu wenig Songs) nimmt sie nur weg."""
    wand, mono = [_wann(1, 11)], [1000.0]
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()},
                               "gepostet": "2026-09",
                               "dj": {"monat": "2026-09", "uid": 1001, "bezahlt": True,
                                      "verkuendet": True, "rolle": True}}}}
    echt_an = guildcfg.an
    schalter = {"dj_rolle": True}

    def an(gid, key):
        return schalter[key] if key in schalter else echt_an(gid, key)

    def stellen(zeit):
        mono[0] += zeit - wand[0]
        wand[0] = zeit

    def monat(key, leute, gesamt=12):
        kern._monat(77, key).update(_monat_mit(gesamt=gesamt, leute=leute))

    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(guildcfg, "an", an):
        bob = server.mitglied(1001)
        anna = server.mitglied(1000)
        carl = server.mitglied(1002)
        _ticken(client)
        # Bob war DJ im September - aber die Rolle hat er nie bekommen (stand
        # nirgends): niemandem wird etwas genommen, Anna bekommt sie.
        assert server.protokoll == [("rolle_neu", musikstats.DJ_ROLLE), ("geben", 1000)]
        rolle = server.roles[0]
        assert kern._guild(77)["rolle"] == {"id": rolle.id, "bei": [1000]}
        assert rolle in anna.roles and kern._guild(77)["dj"]["rolle"]
        # November: Bob ist DJ - Anna gibt sie ab, keine neue Rolle.
        monat("2026-11", ((1001, 9, "Bob"), (1000, 2, "Anna")))
        stellen(_wann(1, 11, monat=12))
        _ticken(client)
        assert server.protokoll[2:] == [("nehmen", 1000), ("geben", 1001)], server.protokoll
        assert rolle in bob.roles and rolle not in anna.roles
        # Dezember: Schalter aus - Carl ist DJ, bekommt aber nichts, und Bob
        # behaelt sie (Flo fasst die Rollen nicht an).
        schalter["dj_rolle"] = False
        monat("2026-12", ((1002, 9, "Carl"),))
        stellen(_wann(1, 11, monat=1, jahr=2027))
        _ticken(client)
        assert len(server.protokoll) == 4 and kern._guild(77)["rolle"]["bei"] == [1001]
        # Januar: wieder an, Anna ist DJ - genommen wird sie BOB (der sie hat),
        # nicht Carl (dem DJ von davor, der sie nie bekam).
        schalter["dj_rolle"] = True
        monat("2027-01", ((1000, 9, "Anna"),))
        stellen(_wann(1, 11, monat=2, jahr=2027))
        _ticken(client)
        assert server.protokoll[4:] == [("nehmen", 1001), ("geben", 1000)], server.protokoll
        # Februar: zu wenig Songs - kein DJ, die Rolle geht nur weg.
        monat("2027-02", ((1001, 5, "Bob"),), gesamt=5)
        stellen(_wann(1, 11, monat=3, jahr=2027))
        _ticken(client)
        assert server.protokoll[6:] == [("nehmen", 1000)], server.protokoll
        assert kern._guild(77)["rolle"]["bei"] == [] and rolle not in anna.roles
        assert len(server.kanal.gesendet) == 4, "Februar: kein Post"
        # Maerz: Discord verweigert das Geben - Carl steht dann NICHT als Halter.
        monat("2027-03", ((1002, 9, "Carl"),))

        async def verweigert(_rolle, reason=None):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "nope")
        carl.add_roles = verweigert
        stellen(_wann(1, 11, monat=4, jahr=2027))
        _ticken(client)
        assert kern._guild(77)["rolle"]["bei"] == [] and rolle not in carl.roles
        assert any("DJ-Rolle fuer 1002 ging nicht" in z for z in kern.log)
        # April: Anna ist DJ, Bob hat die Rolle (von Hand gegeben, Flo weiss
        # es aus einem frueheren Monat) und laesst sie sich nicht nehmen: Bob
        # bleibt als Halter stehen - beim naechsten Mal wird es nochmal versucht.
        kern._guild(77)["rolle"]["bei"] = [1001]
        bob.roles.append(rolle)

        async def klebt(_rolle, reason=None):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "nope")
        bob.remove_roles = klebt
        monat("2027-04", ((1000, 9, "Anna"),))
        stellen(_wann(1, 11, monat=5, jahr=2027))
        _ticken(client)
        assert kern._guild(77)["rolle"]["bei"] == [1001, 1000]
        # Bob ist weg (NotFound): er faellt aus der Liste, Anna gibt ab, und
        # der neue DJ bekommt sie trotzdem.
        del server.mitglieder[1001]
        carl = server.mitglied(1002)
        monat("2027-05", ((1002, 9, "Carl"),))
        stellen(_wann(1, 11, monat=6, jahr=2027))
        _ticken(client)
        assert server.protokoll[-2:] == [("nehmen", 1000), ("geben", 1002)], server.protokoll
        assert kern._guild(77)["rolle"]["bei"] == [1002] and rolle in carl.roles
    # Ohne 'Rollen verwalten' - oder mit dj_rolle aus (Standard ausser auf dem
    # Hauptserver) - keine Rolle; gepostet wird trotzdem.
    for recht, wert in ((False, True), (True, False)):
        schalter["dj_rolle"] = wert
        with _wrapped_umgebung(daten) as (kern, server, client), \
                _uhr_gestellt([_wann(1, 11)], [1000.0]), \
                mock.patch.object(guildcfg, "an", an):
            server.me.guild_permissions.manage_roles = recht
            server.mitglied(1000)
            _ticken(client)
            assert server.protokoll == [] and len(server.kanal.gesendet) == 1
            assert "rolle" not in kern._guild(77)


def test_wrapped_post_haengt_oder_scheitert_kommt_nach_ohne_doppelt_zu_zahlen():
    """Bricht der Takt NACH dem Bezahlen ab (ALLTAG_FRIST - die KI, die Karte
    oder das Hochladen haengt) oder lehnt Discord den Post ab, ist der Monat
    nicht verloren: der Takt holt den Post nach NOCHMAL_NACH nach - mit Ping
    und Coin-Zeile, aber ohne noch einmal zu zahlen. Hoechstens VERSUCHE Mal
    und nur im Nachhol-Fenster. Der Besitzer-Ausloeser sagt ehrlich, wenn der
    Post nicht rausging, und kann ihn danach noch selbst nachholen."""
    import economy
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    wand, mono = [_wann(1, 11)], [1000.0]

    def vergeht(sekunden):
        wand[0] += sekunden
        mono[0] += sekunden

    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(musikstats, "_mono", lambda: mono[0]):
        echt = server.kanal.send
        drin = []

        async def haengt(content=None, **_kw):
            drin.append(content)
            await asyncio.sleep(3600)

        async def abbrechen():
            server.kanal.send = haengt
            takt = asyncio.ensure_future(musikstats.tick(client))
            for _ in range(500):
                if drin:
                    break
                await asyncio.sleep(0.01)
            assert drin, "der Post sollte haengen"
            takt.cancel()                   # ALLTAG_FRIST schlaegt zu
            with contextlib.suppress(asyncio.CancelledError):
                await takt
        asyncio.run(abbrechen())
        server.kanal.send = echt
        g = kern._guild(77)
        assert economy.instance._profile(1000)["coins"] == 5000
        assert g["gepostet"] == "2026-10" and g["dj"]["verkuendet"] is False
        assert server.kanal.gesendet == []
        _ticken(client)
        assert server.kanal.gesendet == [], "erst nach NOCHMAL_NACH"
        vergeht(musikstats.NOCHMAL_NACH)
        _ticken(client)
        assert len(server.kanal.gesendet) == 1
        text, kw = server.kanal.gesendet[0]
        assert "<@1000>" in text and "+5.000 Flo Coins" in text
        assert kw["allowed_mentions"].users == [discord.Object(1000)]
        assert g["dj"]["verkuendet"] is True and g["dj"]["versuche"] == 2
        for _ in range(3):
            vergeht(musikstats.NOCHMAL_NACH)
            _ticken(client)
        assert len(server.kanal.gesendet) == 1
        assert economy.instance._profile(1000)["coins"] == 5000, "nie doppelt bezahlt"

    # Discord lehnt jeden Post ab: der Besitzer hoert das (nicht "ist raus"),
    # der Takt versucht es VERSUCHE Mal und gibt dann auf.
    wand, mono = [_wann(1, 11)], [1000.0]
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(musikstats, "_mono", lambda: mono[0]), \
            mock.patch.object(laufzeit, "client", client):
        echt = server.kanal.send
        versucht = []

        async def abgelehnt(content=None, **_kw):
            versucht.append(content)
            raise _http_fehler("Missing Access")
        server.kanal.send = abgelehnt
        befehl = _wrapped_nachricht("flo wrapped posten", server, uid=admin_id())
        antwort = asyncio.run(musikstats.handle(befehl))
        assert "abgelehnt" in antwort and "ist raus" not in antwort, antwort
        assert economy.instance._profile(1000)["coins"] == 5000
        for _ in range(10):
            vergeht(musikstats.NOCHMAL_NACH)
            _ticken(client)
        g = kern._guild(77)
        assert g["dj"]["versuche"] == musikstats.VERSUCHE
        assert len(versucht) == 2 * musikstats.VERSUCHE, "je Versuch mit und ohne Bild"
        assert any(f"nach {musikstats.VERSUCHE} Versuchen" in z for z in kern.log)
        assert economy.instance._profile(1000)["coins"] == 5000
        # Von Hand geht es danach noch - als der eigentliche Monats-Post.
        server.kanal.send = echt
        antwort = asyncio.run(musikstats.handle(befehl))
        assert "ist raus" in antwort and len(server.kanal.gesendet) == 1
        assert "<@1000>" in server.kanal.gesendet[0][0] and g["dj"]["verkuendet"]
        assert economy.instance._profile(1000)["coins"] == 5000

    # Nach dem Nachhol-Fenster (ab dem 8.) wird nichts mehr nachgeholt.
    wand, mono = [_wann(7, 23, 50)], [1000.0]
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(musikstats, "_mono", lambda: mono[0]):
        echt = server.kanal.send

        async def kaputt(content=None, **_kw):
            raise _http_fehler("Service Unavailable")
        server.kanal.send = kaputt
        _ticken(client)
        assert kern._guild(77)["dj"]["verkuendet"] is False
        server.kanal.send = echt
        vergeht(musikstats.NOCHMAL_NACH)
        _ticken(client)
        assert server.kanal.gesendet == [] and kern._guild(77)["dj"]["versuche"] == 1


def test_wrapped_karte_liest_den_monat_nur_auf_dem_loop():
    """notieren() aendert den laufenden Monat auf dem Event-Loop, die Karte
    rendert im Thread. Der Thread bekommt deshalb nur fertige Listen und
    Zahlen - den Monat selbst liest nur der Loop. Sonst sprengte ein Song, der
    waehrend des Renderns startet, die Iteration, und die Karte fehlte still."""
    import io
    import threading
    wand, mono = [_wann(20, 18)], [1000.0]
    daten = {"guilds": {"77": {"monate": {"2026-11": _monat_mit()}}}}
    gesehen = []

    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono):
        echt_karte = kern._karte

        def karte(*a, **kw):
            gesehen.append(("karte", threading.current_thread() is threading.main_thread()))
            return echt_karte(*a, **kw)

        def malen(monat, songs, leute, **kw):
            gesehen.append(("malen", threading.current_thread() is threading.main_thread()))
            # Im Thread kommt ein Song dazu - die Listen hier aendert das nicht.
            m = kern.daten(77, "2026-11")
            m["songs"]["yt:neu"] = {"t": "Neu", "n": 1, "sek": 1}
            m["wer"]["1009"] = {"n": 1, "sek": 1, "name": "Neu"}
            assert isinstance(songs, list) and isinstance(leute, list)
            gesehen.append(("daten", monat, songs, leute, kw))
            return io.BytesIO(b"png")

        with mock.patch.object(kern, "_karte", karte), \
                mock.patch.object(render, "musik_wrapped", malen):
            msg = _wrapped_nachricht("flo wrapped", server)
            assert asyncio.run(musikstats.handle(msg)) is musikstats.HANDLED
        assert gesehen[0] == ("karte", True) and gesehen[1] == ("malen", False), gesehen
        _art, monat, songs, leute, kw = gesehen[2]
        assert monat == "November 2026" and songs[0] == ("Darude - Sandstorm", 8)
        assert leute == [("Anna", 8), ("Bob", 4)]
        assert kw == {"gesamt": 12, "sekunden": 2400, "radio": 3, "server": "Testserver",
                      "laufend": True}
        assert msg.gesendet[0][1]["file"].filename == "wrapped.png"


def test_wrapped_kanal_und_ohne_bild():
    """Der eingestellte Wrapped-Kanal geht vor, sonst die Ansagen. Darf Flo
    keine Dateien anhaengen, kommt es ohne Bild. Ohne Kanal: bezahlt wird
    trotzdem (einmal), gepostet erst, wenn es einen gibt (der Takt holt nach)."""
    import economy
    wand, mono = [_wann(1, 11)], [1000.0]
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    echt_get = guildcfg.get
    wrapped = _WrappedKanal(cid=700, ohne_bild=True)

    def get(gid, key):
        return 700 if key == "wrapped_channel" else echt_get(gid, key)

    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(guildcfg, "get", get):
        server.get_channel = lambda cid: wrapped if cid == 700 else None
        _ticken(client)
        assert server.kanal.gesendet == [] and len(wrapped.gesendet) == 1
        assert "file" not in wrapped.gesendet[0][1]
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(musikstats, "_mono", lambda: mono[0]):
        ansage = client._event_channel
        client._event_channel = lambda _g: None
        _ticken(client)
        assert kern._guild(77)["gepostet"] == "2026-10"
        assert economy.instance._profile(1000)["coins"] == 5000
        assert any("kein Kanal" in z for z in kern.log)
        client._event_channel = ansage
        wand[0] += musikstats.NOCHMAL_NACH
        mono[0] += musikstats.NOCHMAL_NACH
        _ticken(client)
        assert len(server.kanal.gesendet) == 1 and "<@1000>" in server.kanal.gesendet[0][0]
        assert economy.instance._profile(1000)["coins"] == 5000


def test_wrapped_ki_spruch_hintergrund_mit_grenze_und_ersatz():
    """Der Spruch kommt von der KI - im Takt als Hintergrund-Aufruf (niemand
    wartet), mit der festen Grenze im System-Prompt und im Kontext des Servers.
    Ohne KI, mit 'ki' aus, bei Verweigerung oder leerer Antwort: der Ersatz-Pool,
    im selben Ton."""
    wand, mono = [_wann(1, 11)], [1000.0]
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    aufrufe = []
    antworten = []

    async def generate(prompt, *, system=None, temperature=0.8, max_tokens=300,
                       hintergrund=False):
        aufrufe.append({"prompt": prompt, "system": system, "hintergrund": hintergrund,
                        "gid": ai.aktuelle_guild()})
        return antworten.pop(0) if antworten else None

    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate), \
            mock.patch.object(laufzeit, "client", client):
        antworten.append("Sandstorm, schon wieder. Anna hat echt keine Hobbys.")
        _ticken(client)
        assert len(aufrufe) == 1
        a = aufrufe[0]
        assert a["hintergrund"] is True and a["gid"] == 77
        assert ai.FloAI._GUARDRAIL in a["system"] and "GENAU ZWEI" in a["system"]
        assert "Darude - Sandstorm" in a["prompt"] and "Anna" in a["prompt"]
        assert server.kanal.gesendet[0][0].endswith("Anna hat echt keine Hobbys.")
        # 'flo wrapped posten' (Besitzer, schon gepostet): Wiederholung ohne
        # Coins und ohne Ping - KI normal (jemand wartet), Verweigerung -> Ersatz.
        antworten.append("I'm sorry, but I can't help with that.")
        msg = _wrapped_nachricht("flo wrapped posten", server, uid=admin_id())
        antwort = asyncio.run(musikstats.handle(msg))
        assert "nochmal gepostet" in antwort and len(server.kanal.gesendet) == 2
        text, kw = server.kanal.gesendet[1]
        assert aufrufe[1]["hintergrund"] is False
        assert "<@1000>" not in text and "Flo Coins" not in text and "**Anna**" in text
        assert kw["allowed_mentions"].users is False
        assert any(text.endswith(e.format(song="**Darude - Sandstorm**", dj="**Anna**"))
                   for e in musikstats._ERSATZ), text
    # 'ki' auf dem Server aus: keine KI, Ersatz.
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate), \
            mock.patch.object(features, "is_on_in", lambda _g, k: k != "ki"):
        aufrufe.clear()
        _ticken(client)
        assert aufrufe == [] and len(server.kanal.gesendet) == 1


def test_wrapped_reagiert_nie_auf_notsignale():
    """Was Flo von sich aus postet, reagiert nie auf ein Notsignal: ein Songtitel
    oder Name, auf den ai.FloAI._NOTLAGE_RE anspringt, geht weder an die KI noch
    in den Spruch - und eine Antwort mit Notsignal wird verworfen. Der DJ mit so
    einem Namen bleibt trotzdem DJ (der Kopf pingt ihn ja): im Spruch heisst er
    nur 'DJ', statt dass der Spruch behauptet, es gebe keinen."""
    wand, mono = [_wann(1, 11)], [1000.0]
    monat = _monat_mit(leute=((1000, 8, "will nicht mehr leben"), (1001, 4, "Bob")))
    monat["songs"]["yt:a"]["t"] = "Ich will sterben (Lyrics)"
    daten = {"guilds": {"77": {"monate": {"2026-10": monat}}}}
    aufrufe = []

    async def generate(prompt, **kw):
        aufrufe.append(prompt)
        return "Ich will sterben, so schlecht ist das."

    assert ai.FloAI._NOTLAGE_RE.search("Ich will sterben (Lyrics)")
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        _ticken(client)
        assert len(aufrufe) == 1
        assert "sterben" not in aufrufe[0] and "nicht mehr leben" not in aufrufe[0]
        assert "Einen DJ gibt es nicht" not in aufrufe[0]
        assert "lassen wir weg" in aufrufe[0] and "'DJ'" in aufrufe[0]
        text = server.kanal.gesendet[0][0]
        assert "<@1000>" in text and "nicht mehr leben" not in text
        spruch = text.split("Wünschen. ", 1)[1]
        assert not ai.FloAI._NOTLAGE_RE.search(spruch), spruch
        assert "Rick Astley" in spruch, "der Ersatz nennt den naechsten harmlosen Song"
        assert "gewünscht hat sich keiner" not in spruch and "Keiner hat sich" not in spruch
        song = f"**{musikstats._sauber('Rick Astley - Never Gonna Give You Up', 60)}**"
        assert any(spruch == e.format(song=song, dj="**DJ**")
                   for e in musikstats._ERSATZ), spruch


def admin_id():
    import admin
    return admin.OWNER_ID


def _wrapped_nachricht(text, server, uid=5):
    gesendet = []

    async def antworten(inhalt=None, **kw):
        gesendet.append((inhalt, kw))
        return SimpleNamespace(id=1)
    msg = SimpleNamespace(content=text, guild=server, mentions=[], reply=antworten,
                          author=SimpleNamespace(id=uid, bot=False, display_name="Tester",
                                                 mention=f"<@{uid}>"),
                          channel=SimpleNamespace(id=5, send=antworten))
    msg.gesendet = gesendet
    return msg


def test_flo_wrapped_zeigt_den_monat_mit_abkuehlung():
    """'flo wrapped' zeigt den laufenden Monat (ohne Ping, ohne Coins), 'flo
    wrapped letzter monat' den davor; eine Minute Abkuehlung je Server. Ein
    leerer Monat bekommt einen Satz statt einer leeren Karte."""
    wand, mono = [_wann(20, 18)], [1000.0]
    daten = {"guilds": {"77": {"monate": {"2026-11": _monat_mit(radio=0),
                                          "2026-10": _monat_mit(gesamt=30)}}}}
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(musikstats, "_mono", lambda: mono[0]):
        msg = _wrapped_nachricht("flo wrapped", server)
        assert asyncio.run(musikstats.handle(msg)) is musikstats.HANDLED
        text, kw = msg.gesendet[0]
        assert "Musik-Wrapped November** (läuft noch)" in text and "DJ bisher: **Anna**" in text
        assert "<@" not in text and "Coins" not in text
        assert kw["file"].filename == "wrapped.png"
        assert kw["allowed_mentions"].users is False
        assert asyncio.run(musikstats.handle(_wrapped_nachricht("flo wrapped", server))) \
            in musikstats._ZU_FRUEH
        mono[0] += 61
        msg = _wrapped_nachricht("Flo, wrapped letzter monat?", server)
        assert asyncio.run(musikstats.handle(msg)) is musikstats.HANDLED
        assert "Musik-Wrapped Oktober** –" in msg.gesendet[0][0] and "30 Songs" in msg.gesendet[0][0]
        assert "DJ des Monats: **Anna**" in msg.gesendet[0][0]
        mono[0] += 61
        wand[0] = _wann(3, 12, monat=1, jahr=2027)
        msg = _wrapped_nachricht("flo musikwrapped", server)
        assert asyncio.run(musikstats.handle(msg)) == musikstats._NICHTS.format(monat="Januar")
        assert kern._guild(77).get("gepostet") is None, "zeigen ist kein Monats-Post"


def test_flo_wrapped_nur_eindeutige_befehle():
    """Nur was eindeutig ist, wird ein Befehl - ein Satz mit 'wrapped' geht an
    die KI. 'posten' darf nur der Besitzer."""
    wand, mono = [_wann(20, 18)], [1000.0]
    with _wrapped_umgebung() as (kern, server, client), _uhr_gestellt(wand, mono):
        for satz in ("flo wrapped ist cool", "flo wrapped mir mal das geschenk",
                     "wrapped up the project", "flo hast du das wrapped gesehen",
                     "hahaha ja klar", "flo spiel wrapped", "flo wrapped posten bitte jetzt",
                     "flo wrapped 2025", ""):
            assert asyncio.run(musikstats.handle(_wrapped_nachricht(satz, server))) is None, satz
        assert asyncio.run(musikstats.handle(
            _wrapped_nachricht("flo wrapped posten", server))) == musikstats._NUR_CHEF
        dm = _wrapped_nachricht("flo wrapped", server)
        dm.guild = None
        assert "nur auf einem Server" in asyncio.run(musikstats.handle(dm))
        kern._enabled = False
        assert asyncio.run(musikstats.handle(_wrapped_nachricht("flo wrapped", server))) is None
    import cmdnorm
    for wort in musikstats._CMDS:
        assert wort in cmdnorm.KNOWN and wort not in cmdnorm.ZUSTAND


def test_flo_wrapped_posten_ist_der_monatspost():
    """Der Besitzer-Ausloeser: steht das Wrapped des Vormonats noch aus, IST
    'flo wrapped posten' der Monats-Post (DJ bezahlt) - der Takt macht ihn
    danach nicht noch einmal. Zu wenig Songs: kein Post."""
    import economy
    wand, mono = [_wann(2, 12)], [1000.0]
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit()}}}}
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(laufzeit, "client", client):
        antwort = asyncio.run(musikstats.handle(
            _wrapped_nachricht("flo wrapped posten", server, uid=admin_id())))
        assert "ist raus" in antwort and len(server.kanal.gesendet) == 1
        assert "<@1000>" in server.kanal.gesendet[0][0]
        assert economy.instance._profile(1000)["coins"] == 5000
        _ticken(client)
        assert len(server.kanal.gesendet) == 1 and economy.instance._profile(1000)["coins"] == 5000
    daten = {"guilds": {"77": {"monate": {"2026-10": _monat_mit(gesamt=5)}}}}
    with _wrapped_umgebung(daten) as (kern, server, client), _uhr_gestellt(wand, mono), \
            mock.patch.object(laufzeit, "client", client):
        antwort = asyncio.run(musikstats.handle(
            _wrapped_nachricht("flo wrapped posten", server, uid=admin_id())))
        assert "keine 10 Songs" in antwort and server.kanal.gesendet == []


def test_wrapped_karte_mit_langen_titeln_und_emojis():
    from PIL import Image
    lang = "Ein Titel, der wirklich nie aufhört 🎵🔥 " * 8
    png = render.musik_wrapped(
        "Oktober 2026", [(lang, 12), ("🔥🔥🔥", 3), ("Ä Ö Ü ß – „Zitat“", 2)],
        [("Bobby 🎧 " * 10, 30), ("", 2), ("Zalgo z̷̢a̶l̸g̵o̴", 1)],
        gesamt=1_234_567, sekunden=10 ** 9, radio=99_999, server="S" * 200, laufend=True)
    bild = Image.open(png)
    assert bild.size[0] == 900 and bild.format == "PNG"
    leer = Image.open(render.musik_wrapped("Januar 2027", [], []))
    assert leer.size[0] == 900 and leer.size[1] < bild.size[1]

if __name__ == "__main__":
    run(globals())
