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
    _StallVoice, _dynamisch_klicken, _embed_text, _fake_person, _klick_im_voice,
    _musik_umgebung, _track, _verlauf_frisch, _voice_kanal)

import contextlib
from unittest import mock

import discord

import ai
import features
import laufzeit
import music
import musikstats
import musikzustand

#: Befehle fuer den gemeinsamen Rauchtest (test_fundament.py). musikzustand
#: hat keinen Befehl und steht deshalb nicht hier.
RAUCHPROBEN = (
    (musikstats, ()),
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


if __name__ == "__main__":
    run(globals())
