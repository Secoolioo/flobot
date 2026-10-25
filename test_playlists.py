"""Playlists je Server: playlists.py und musikquellen.py.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_playlists.py      nur diese Datei

Kein Test hier fasst das Netz an: musikquellen._holen und die Spotify-/yt-dlp-
Helfer der Musik sind in _pl_frisch() durch Attrappen ersetzt, die nur
antworten, was der Test ihnen vorgibt. Spotify wird aus Fixture-HTML gelesen,
das so aussieht wie die echte Einbettungsseite (siehe _SP_TRACK/_SP_ALBUM) -
ob die Seite WIRKLICH noch so aussieht, prueft tools_musik_check.py live.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (_Klick, _embed_text, _klick_im_voice, _rauch_nachricht,
                       _voice_kanal)

import contextlib
import io
import json
import logging
import os
import random
import re
from unittest import mock

import discord

import basis
import cmdnorm
import laufzeit
import music
import musikquellen
import playlists
import store
from store import JsonStore

#: Befehle, die der gemeinsame Rauchtest (test_fundament.py) durch das echte
#: handle() schickt - alle ohne Voice und ohne Netz.
RAUCHPROBEN = (
    (playlists, ("playlists", "playlist", "playlist alle", "playlist gibtsnicht",
                 "playlist neu", "queue speichern", "neue playlist",
                 "playlist https://youtu.be/dQw4w9WgXcQ")),
)


# --- Attrappen -------------------------------------------------------------------
class _Gesendet:
    """Eine Nachricht, die Flo geschickt hat - merkt sich jede Bearbeitung."""

    _nr = 5000

    def __init__(self, kanal, kw):
        _Gesendet._nr += 1
        self.id = _Gesendet._nr
        self.channel = kanal
        self.kw = dict(kw)
        self.edits = []
        self.geloescht = False

    @property
    def content(self):
        return self.kw.get("content")

    @property
    def view(self):
        for kw in reversed(self.edits):
            if "view" in kw:
                return kw["view"]
        return self.kw.get("view")

    async def edit(self, **kw):
        self.edits.append(kw)
        return self

    async def delete(self):
        self.geloescht = True


class _Kanal:
    def __init__(self, cid=555):
        self.id = cid
        self.name = "musik"
        self.gesendet = []

    async def send(self, content=None, **kw):
        kw["content"] = content
        msg = _Gesendet(self, kw)
        self.gesendet.append(msg)
        return msg

    def texte(self):
        return [m.content or "" for m in self.gesendet]

    async def _nichts(self, *_a, **_k):
        return None

    def permissions_for(self, _wer):
        return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True,
                               read_message_history=True, manage_messages=True,
                               attach_files=True)


_GILDE = SimpleNamespace(id=77, name="Testserver", me=SimpleNamespace(id=1))


def _person(uid=1, *, mod=False, voice=None, name=None):
    rechte = SimpleNamespace(administrator=mod, manage_guild=mod, manage_channels=mod,
                             manage_messages=mod, move_members=mod, manage_roles=mod)
    return SimpleNamespace(
        id=uid, bot=False, display_name=name or f"User{uid}", name=f"user{uid}",
        mention=f"<@{uid}>", guild_permissions=rechte,
        voice=SimpleNamespace(channel=voice) if voice is not None else None)


class _Nachricht:
    """Eine Nachricht von einem Menschen (ohne to_reference: basis.antworte
    nimmt dann reply)."""

    _nr = 100

    def __init__(self, text, autor, kanal, guild=_GILDE):
        _Nachricht._nr += 1
        self.id = _Nachricht._nr
        self.content = text
        self.author = autor
        self.channel = kanal
        self.guild = guild
        self.mentions = []
        self.reference = None
        self.reaktionen = []

    async def add_reaction(self, emoji):
        self.reaktionen.append(("+", emoji))

    async def remove_reaction(self, emoji, _wer):
        self.reaktionen.append(("-", emoji))

    async def reply(self, content=None, **kw):
        return await self.channel.send(content, **kw)

    def endreaktion(self):
        plus = [e for art, e in self.reaktionen if art == "+" and e != "🔎"]
        return plus[-1] if plus else None


def _view_text(view):
    if view is None:
        return ""
    return "\n".join(t.content for t in view.walk_children()
                     if isinstance(t, discord.ui.TextDisplay))


def _knopf_ids(view):
    return [k.custom_id for k in view.walk_children()
            if isinstance(k, discord.ui.DynamicItem)]


def _next_data(daten):
    return ('<!DOCTYPE html><html><head><title>Spotify</title></head><body>'
            '<div id="__next"></div><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(daten) + "</script></body></html>")


# So sieht die Einbettungsseite eines Songs aus (gekuerzt, Aufbau wie im
# Original: props.pageProps.state.data.entity).
_SP_TRACK = _next_data({"props": {"pageProps": {"state": {"data": {"entity": {
    "type": "track", "name": "Never Gonna Give You Up",
    "uri": "spotify:track:4cOdK2wGLETKBW3PvgPWqT", "id": "4cOdK2wGLETKBW3PvgPWqT",
    "title": "Never Gonna Give You Up",
    "artists": [{"name": "Rick Astley", "uri": "spotify:artist:0gxyHStUsqpMadRV0Di1Qt"}],
    "duration": 213573, "isPlayable": True, "isExplicit": False,
    "visualIdentity": {"image": []}}, "embeded_entity_uri": "spotify:track:x"},
    "settings": {"rtl": False}}}}, "page": "/track/[id]", "buildId": "abc"})

_SP_ALBUM = _next_data({"props": {"pageProps": {"state": {"data": {"entity": {
    "type": "album", "name": "Whenever You Need Somebody", "subtitle": "Rick Astley",
    "trackList": [
        {"uri": "spotify:track:1", "uid": "a", "title": "Never Gonna Give You Up",
         "subtitle": "Rick Astley", "duration": 213573},
        {"uri": "spotify:track:2", "uid": "b", "title": "Whenever You Need Somebody",
         "subtitle": "Rick Astley", "duration": 234000},
        {"uri": "spotify:track:3", "uid": "c", "title": "Together Forever",
         "subtitle": "Rick Astley, Stock Aitken Waterman",
         "duration": {"totalMilliseconds": 205000}},
        {"uri": "spotify:track:4", "uid": "d", "subtitle": "ohne Titel"},
        "Muell"]}}}}}})

_YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _oembed(titel):
    return 200, json.dumps({"title": titel, "author_name": "x", "type": "video"})


@contextlib.contextmanager
def _pl_frisch(netz=None, flach=None, sp_meta=None, sp_liste=None, kurz=None, ziel=None):
    """Playlists mit frischem, ECHTEM JsonStore im Temp-Ordner - und ohne Netz.

    netz     url-Anfang -> (status, text) fuer musikquellen._holen
    flach    url-Teil -> [(url, titel)] fuer music._flache_playlist
    ziel     url -> wohin die Weiterleitung zeigt (musikquellen._ziel)
    sp_meta / sp_liste / kurz: Antworten der Spotify-API-Helfer (sonst None)."""
    pl = playlists.instance
    alt = dict(vars(pl))
    _pl_frisch.nr += 1
    datei = f"playlists-test-{_pl_frisch.nr}.json"
    for pfad in (store.DATA_DIR / datei, store.DATA_DIR / (datei + ".bak")):
        if pfad.exists():
            pfad.unlink()
    pl._store = JsonStore(datei, default={"guilds": {}})
    pl._enabled = True
    pl._wizards = {}
    abrufe = []

    async def holen(url, *, frist=None):
        abrufe.append(url)
        for anfang, antwort in (netz or {}).items():
            if url.startswith(anfang):
                return antwort
        return 0, ""

    async def flache(url, *, quelle="Playlist", ende=None):
        abrufe.append(("flach", url, ende))
        for teil, eintraege in (flach or {}).items():
            if teil in url:
                return list(eintraege)[:ende] if ende else list(eintraege)
        return None

    async def meta(_url):
        return sp_meta

    async def liste(_url):
        return sp_liste

    async def kurzlink(url):
        return kurz(url) if callable(kurz) else kurz

    async def weiterleitung(url):
        abrufe.append(("ziel", url))
        return (ziel or {}).get(url)

    mi = music.instance
    try:
        with mock.patch.object(musikquellen, "_holen", holen), \
                mock.patch.object(mi, "_flache_playlist", flache), \
                mock.patch.object(mi, "_spotify_track_meta", meta), \
                mock.patch.object(mi, "_spotify_list_tracks", liste), \
                mock.patch.object(mi, "_spotify_kurzlink", kurzlink), \
                mock.patch.object(musikquellen, "_ziel", weiterleitung), \
                mock.patch.object(laufzeit, "protect_message", lambda m: None), \
                mock.patch.object(laufzeit, "release_message", lambda m, **k: None):
            pl.abrufe = abrufe
            yield pl
    finally:
        vars(pl).clear()
        vars(pl).update(alt)


_pl_frisch.nr = 0


async def _start(pl, kanal, autor, name="Zocken"):
    antwort = await pl.handle(_Nachricht(f"flo playlist neu {name}", autor, kanal))
    return antwort


async def _paste(pl, kanal, autor, text):
    msg = _Nachricht(text, autor, kanal)
    return await pl.on_message_passive(msg), msg


def _wizard(pl, kanal, autor):
    return pl._wizards.get((kanal.id, autor.id))


def _listen(pl, gid=77):
    return list(pl._listen(gid).values())


# --- Grammatik --------------------------------------------------------------------
def test_grammatik_erkennt_jede_form():
    """Jede Form aus dem Entwurf landet bei der richtigen Aktion - mit dem
    Namen in der Schreibweise, in der er getippt wurde."""
    p = playlists.instance._parse
    erwartet = {
        "playlist": ("liste", {}), "playlists": ("liste", {}),
        "playlist alle": ("liste", {}), "wiedergabeliste": ("liste", {}),
        "playlist neu Zocken": ("neu", {"name": "Zocken"}),
        "playlist erstellen Chill Abend": ("neu", {"name": "Chill Abend"}),
        "playlist anlegen x": ("neu", {"name": "x"}),
        "playlist bau x": ("neu", {"name": "x"}),
        "playlist mach x": ("neu", {"name": "x"}),
        "neue playlist Party Zeit": ("neu", {"name": "Party Zeit"}),
        "playlist zocken": ("spielen", {"name": "zocken", "mix": False, "nackt": True}),
        "playlist zocken mix": ("spielen", {"name": "zocken", "mix": True}),
        "playlist zocken gemischt": ("spielen", {"name": "zocken", "mix": True}),
        "playlist zocken shuffle": ("spielen", {"name": "zocken", "mix": True}),
        "playlist mix zocken": ("spielen", {"name": "zocken", "mix": True}),
        "playlist zocken dazu": ("dazu", {"name": "zocken"}),
        "playlist zocken erweitern": ("dazu", {"name": "zocken"}),
        "playlist dazu zocken": ("dazu", {"name": "zocken"}),
        "playlist zocken zeig": ("zeig", {"name": "zocken"}),
        "playlist zocken info": ("zeig", {"name": "zocken"}),
        "playlist zocken löschen": ("loeschen", {"name": "zocken"}),
        "playlist zocken delete": ("loeschen", {"name": "zocken"}),
        "playlist löschen zocken": ("loeschen", {"name": "zocken"}),
        "playlist zocken umbenennen in Party": ("umbenennen", {"name": "zocken", "neu": "Party"}),
        "playlist chill abend umbenennen party zeit": (
            "umbenennen", {"name": "chill abend", "neu": "party zeit"}),
        "playlist zocken raus 3": ("raus", {"name": "zocken", "nr": 3}),
        "playlist speichern Abend": ("speichern", {"name": "Abend"}),
        "queue speichern als Abend": ("speichern", {"name": "Abend"}),
        "warteschlange speichern Abend": ("speichern", {"name": "Abend"}),
        "queue speichern": ("speichern", {"name": ""}),
        "spiel playlist zocken": ("natur", {"name": "zocken", "mix": False}),
        "spiel mal die playlist zocken gemischt": ("natur", {"name": "zocken", "mix": True}),
        "play playlist zocken": ("natur", {"name": "zocken", "mix": False}),
        "leg die playlist zocken auf": ("natur", {"name": "zocken", "mix": False}),
        "starte meine playlist zocken": ("natur", {"name": "zocken", "mix": False}),
        "hau unsere playlist zocken rein": ("natur", {"name": "zocken", "mix": False}),
        "mach mal die playlist zocken an": ("natur", {"name": "zocken", "mix": False}),
        "pack playlist zocken auf": ("natur", {"name": "zocken", "mix": False}),
        "tu die playlist zocken an": ("natur", {"name": "zocken", "mix": False}),
        "playlist, zocken!": ("spielen", {"name": "zocken", "mix": False, "nackt": True}),
    }
    for text, soll in erwartet.items():
        assert p(text) == soll, (text, p(text))


def test_grammatik_laesst_saetze_der_ki():
    """Alles, was nach Satz aussieht, ist KEIN Befehl - die KI antwortet."""
    p = playlists.instance._parse
    for text in ("playlist ist doof", "playlist nervt", "playlist war gestern besser",
                 "playlist hat keiner gehört", "playlist finde ich gut",
                 "playlists von bob", "playlists sind cool",
                 "playlist zocken ist echt richtig richtig gut finde ich",
                 "mach die playlist zocken", "spiel playlist", "spiel die playlist",
                 "queue", "queue zeigen", "warteschlange leer machen", "neue songs",
                 "neue playlist mit ganz ganz vielen tollen songs drin",
                 "die playlist ist doof", "hahaha ja klar", "spiel mal was schönes",
                 "playlist zocken umbenennen", "playlist umbenennen zocken"):
        assert p(text) is None, (text, p(text))


def test_handle_unbekannte_namen_und_saetze():
    """'playlist <unbekannt>' sagt "gibt's nicht" (mit Vorschlag), aber nur,
    wenn der Name nicht nach Satz aussieht. 'spiel playlist <unbekannt>'
    bleibt eine YouTube-Suche (None -> music)."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        pl._anlegen(77, "Zocken", bob, [{"i": "ytsearch1:a", "t": "a", "h": None,
                                          "d": None, "q": "a"}])

        async def lauf():
            raus = {}
            for text in ("flo playlist zocke", "flo playlist abend", "flo spiel playlist abend",
                         "flo playlist von gestern war geil", "flo playlist mit freunden",
                         "flo mach mal playlist an", "flo spiel despacito",
                         "flo ich hab eine playlist gemacht", "flo playlist abend mix",
                         "flo playlist abend dazu"):
                raus[text] = await pl.handle(_Nachricht(text, bob, kanal))
            return raus
        raus = asyncio.run(lauf())
    assert "gibt's hier nicht" in raus["flo playlist zocke"]
    assert "Meintest du **Zocken**" in raus["flo playlist zocke"]
    assert "playlist neu abend" in raus["flo playlist abend"]
    for text in ("flo spiel playlist abend", "flo playlist von gestern war geil",
                 "flo playlist mit freunden", "flo mach mal playlist an",
                 "flo spiel despacito", "flo ich hab eine playlist gemacht"):
        assert raus[text] is None, (text, raus[text])
    assert "gibt's hier nicht" in raus["flo playlist abend mix"]
    assert "gibt's hier nicht" in raus["flo playlist abend dazu"]


def test_playlist_hilfe_ist_die_hilfe_seite():
    """'flo hilfe playlist' oeffnete die Hilfe - das genauso natuerliche 'flo
    playlist hilfe' hielt playlists fuer die Liste 'hilfe' ("gibt's hier
    nicht. Bau sie halt"). Jetzt ist beides die Hilfe-Seite, und 'hilfe'
    taugt nicht mehr als Name."""
    import bot
    from test_fundament import _nur_geladen
    with _nur_geladen(bot, "MUSIC_ENABLED", "PLAYLISTS_ENABLED", "ZITATE_ENABLED"):
        for text, seite in (("flo playlist hilfe", "playlist"),
                            ("Flo playlist help?", "playlist"),
                            ("flo leben hilfe", "leben"), ("flo musik anleitung", "musik")):
            assert bot._help_category_key(text) == seite, text
        for text in ("flo playlist", "flo spiel hilfe", "flo playlist zocken hilfe",
                     "flo hilfe", "flo hilfe hilfe"):
            assert bot._help_category_key(text) is None, text
    with _pl_frisch() as pl:
        for name in ("hilfe", "Help", "anleitung"):
            assert pl._name_pruefen(name)[0] is None, name
        assert pl._name_pruefen("Hilfsbereit")[0] == "Hilfsbereit"


def test_handle_links_und_saetze_mit_befehlswort_hinten():
    """'flo playlist <link>' ist kein Name: ein Hinweis, wie es geht - nicht
    "gibt's nicht, bau sie: playlist neu <link>" (das lehnt die Namenspruefung
    ab). Und ein Satz bleibt ein Satz, auch wenn er auf 'weg'/'dazu'/'zeig'
    endet - solange es die Liste nicht gibt."""
    kanal, bob = _Kanal(), _person(1)
    yt = "https://www.youtube.com/playlist?list=PLabc123"
    with _pl_frisch() as pl:
        pl._anlegen(77, "Zocken", bob, _songs("A"))

        async def lauf():
            raus = {}
            for text in (f"flo playlist {yt}", f"flo playlist {yt} mix",
                         "flo playlist https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
                         f"flo spiel playlist {yt}", "flo playlist www.youtube.com/watch",
                         f"flo playlist neu {yt}", f"flo neue playlist {yt}",
                         "flo playlist zocken ist weg", "flo playlist von gestern dazu",
                         "flo playlist die alte zeig", "flo playlist mit freunden löschen",
                         "flo playlist abend weg", "flo playlist zocken zeig"):
                raus[text] = await pl.handle(_Nachricht(text, bob, kanal))
            return raus
        raus = asyncio.run(lauf())
    for text in (f"flo playlist {yt}", f"flo playlist {yt} mix",
                 "flo playlist https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
                 "flo playlist www.youtube.com/watch", f"flo playlist neu {yt}",
                 f"flo neue playlist {yt}"):
        assert "Das ist ein Link, kein Playlist-Name" in raus[text], (text, raus[text])
        assert "spiel <link>" in raus[text] and "playlist neu <name>" in raus[text]
    for text in (f"flo spiel playlist {yt}", "flo playlist zocken ist weg",
                 "flo playlist von gestern dazu", "flo playlist die alte zeig",
                 "flo playlist mit freunden löschen"):
        assert raus[text] is None, (text, raus[text])
    assert "gibt's hier nicht" in raus["flo playlist abend weg"]
    assert raus["flo playlist zocken zeig"] is playlists.HANDLED


def test_handle_ist_schnell_raus_und_ohne_server_still():
    """Fremde erste Woerter kommen gar nicht erst bis zur Grammatik - und ohne
    Server (DM) oder ausgeschaltet antwortet das Modul nie."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        with mock.patch.object(pl, "_parse", side_effect=AssertionError("geparst")):
            for text in ("flo hahaha ja klar", "flo level", "flo spiel despacito",
                         "flo queue", "flo mach mal musik an", "flo neue frisur"):
                assert asyncio.run(pl.handle(_Nachricht(text, bob, kanal))) is None, text
        dm = _Nachricht("flo playlists", bob, kanal, guild=None)
        assert asyncio.run(pl.handle(dm)) is None
        pl._enabled = False
        assert asyncio.run(pl.handle(_Nachricht("flo playlists", bob, kanal))) is None


def test_cmdnorm_laesst_die_playlist_woerter_stehen():
    """Die Befehlswoerter sind bekannt (KNOWN) und werden nicht verbogen; das
    neue Vorwort 'neue' steht in KNOWN und ZUSTAND - aus 'neuer rekord' wird
    deshalb trotzdem nichts."""
    for wort in playlists._CMDS + ("neue",):
        assert wort in cmdnorm.KNOWN, wort
    assert "neue" in cmdnorm.ZUSTAND
    for text in ("neue playlist zocken", "playlist neu zocken", "playlists",
                 "queue speichern als zocken", "warteschlange speichern zocken",
                 "spiel playlist zocken", "neuer rekord heute", "neues spiel",
                 "neuen song gehört", "neu hier"):
        assert cmdnorm.normalize(text) is None, (text, cmdnorm.normalize(text))


# --- Uebersicht, Namen, Grenzen ------------------------------------------------------
def test_uebersicht_zeigt_name_songs_ersteller_und_gespielt():
    kanal, bob, anna = _Kanal(), _person(1, name="Bob"), _person(2, name="Anna")
    with _pl_frisch() as pl:
        leer = asyncio.run(pl.handle(_Nachricht("flo playlists", bob, kanal)))
        assert isinstance(leer, discord.Embed) and "Armselig" in leer.description
        a = pl._anlegen(77, "Zocken", bob, [{"i": "x", "t": "x"}] * 3)
        b = pl._anlegen(77, "Chill", anna, [{"i": "y", "t": "y"}])
        b["gespielt"] = 5
        e = asyncio.run(pl.handle(_Nachricht("flo playlist alle", bob, kanal)))
    text = _embed_text(e)
    assert text.index("**Chill**") < text.index("**Zocken**"), "meistgespielte zuerst"
    assert "3 Songs · von Bob · 0× gespielt" in text
    assert "1 Song · von Anna · 5× gespielt" in text
    assert "playlist neu <name>" in text and "queue speichern als <name>" in text
    assert a["id"] != b["id"] and re.fullmatch(r"[0-9a-z]{6}", a["id"])


def test_namen_werden_geprueft():
    pl = playlists.instance
    gut, fehler = pl._name_pruefen("  „Zocken   Abend“ ")
    assert gut == "Zocken Abend" and fehler is None
    for name, stichwort in (("", "Ohne Namen"), ("a" * 41, "Aufsatz"),
                            ("eins zwei drei vier fünf sechs", "Aufsatz"),
                            ("<@123> spam", "Zeichen"), ("@everyone", "Zeichen"),
                            ("https://x.y", "Zeichen"), ("zocken mix", "Befehl"),
                            ("neu", "Befehl"), ("alle", "Befehl"), ("ist doof", "Befehl"),
                            ("hits dazu", "Befehl"), ("lieder raus 3", "Befehl"),
                            ("x umbenennen y", "Befehl")):
        name_ok, grund = pl._name_pruefen(name)
        assert name_ok is None and stichwort in grund, (name, grund)


def test_grenzen_beim_anlegen():
    """Hoechstens MAX_LISTEN je Server und MAX_PRO_PERSON je Person - und
    einen Namen nur einmal."""
    kanal, bob, anna = _Kanal(), _person(1), _person(2)
    with _pl_frisch() as pl, mock.patch.object(playlists, "MAX_LISTEN", 3), \
            mock.patch.object(playlists, "MAX_PRO_PERSON", 2):
        pl._anlegen(77, "Eins", bob, [])
        pl._anlegen(77, "Zwei", bob, [])

        async def lauf():
            return [await pl.handle(_Nachricht(t, wer, kanal)) for t, wer in (
                ("flo playlist neu Drei", bob), ("flo playlist neu eins", anna),
                ("flo playlist neu Drei", anna), ("flo playlist neu Vier", anna),
                ("flo playlist neu", anna))]
        antworten = asyncio.run(lauf())
    assert "schon 2 Playlists" in antworten[0]
    assert "gibt's schon" in antworten[1] and "Eins" in antworten[1]
    assert antworten[2] is playlists.HANDLED
    # Annas Assistent fuer 'Drei' ist offen, aber noch nicht gespeichert -
    # 'Vier' darf (2 Listen auf dem Server), der Assistent fuer 'Drei' faellt weg.
    assert antworten[3] is playlists.HANDLED
    assert "Wie soll das Ding heißen" in antworten[4]


# --- Der Assistent ------------------------------------------------------------------
def test_assistent_mehrzeilig_mit_aufzaehlung_dubletten_und_chat():
    """Mehrere Zeilen auf einmal, Spiegelstriche/Nummern weg, Dubletten nur
    einmal, Chat-Zeilen bleiben draussen - und EINE Statusnachricht."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf():
            assert await _start(pl, kanal, bob) is playlists.HANDLED
            w = _wizard(pl, kanal, bob)
            ok, msg = await _paste(pl, kanal, bob,
                                   "1. Never Gonna Give You Up\n- Darude Sandstorm\n"
                                   "• never gonna give you up\nlol\nwelcher ist besser?\n"
                                   "(4) Toto Africa")
            return w, ok, msg
        w, ok, msg = asyncio.run(lauf())
        abrufe = list(pl.abrufe)
    assert ok is True
    assert [s["t"] for s in w["songs"]] == ["Never Gonna Give You Up", "Darude Sandstorm",
                                            "Toto Africa"]
    assert w["songs"][0]["i"] == "ytsearch1:Never Gonna Give You Up"
    assert w["songs"][0]["q"] == "Never Gonna Give You Up" and w["songs"][0]["h"] is None
    assert msg.endreaktion() == "⚠️", msg.reaktionen      # eine Dublette
    assert any("Goldfisch" in h for h in w["hinweise"])
    assert len(kanal.gesendet) == 1, "nur EINE Statusnachricht"
    assert "neue Playlist" in _view_text(kanal.gesendet[0].view)
    assert abrufe == [], "Textzeilen brauchen kein Netz"


def test_assistent_status_wird_gedrosselt_und_im_takt_nachgezogen():
    """Hoechstens alle EDIT_ABSTAND Sekunden ein Edit; was liegen bleibt,
    zieht der Takt nach. Dabei steht die Songliste mit Zaehler drin."""
    kanal, bob = _Kanal(), _person(1)
    uhr = [1000.0]
    with _pl_frisch() as pl, mock.patch.object(playlists, "_uhr", lambda: uhr[0]):
        async def lauf():
            await _start(pl, kanal, bob)
            status = kanal.gesendet[0]
            uhr[0] += 5
            await _paste(pl, kanal, bob, "Song A")
            nach_erstem = len(status.edits)
            uhr[0] += 0.5
            await _paste(pl, kanal, bob, "Song B")
            nach_zweitem = len(status.edits)
            w = _wizard(pl, kanal, bob)
            faellig = w["edit_faellig"]
            uhr[0] += 3
            await pl.tick(None)
            return status, nach_erstem, nach_zweitem, faellig, w
        status, eins, zwei, faellig, w = asyncio.run(lauf())
    assert eins == 1 and zwei == 1 and faellig is True
    assert len(status.edits) == 2 and w["edit_faellig"] is False
    text = _view_text(status.view)
    assert "**2 Songs** drin" in text and "`1.` Song A" in text and "`2.` Song B" in text


def test_assistent_jede_link_art():
    """YouTube (Video, Kurzlink, Playlist), SoundCloud (Track, Set, Kurzlink
    auf Track und auf Set), Spotify (Song/Album/Playlist ohne Keys, Kurzlink),
    Audiodatei - und was nicht geht, bekommt ❌ und einen Satz."""
    kanal, bob = _Kanal(), _person(1)
    netz = {
        "https://www.youtube.com/oembed": _oembed("Rick Astley - Never Gonna Give You Up"),
        "https://soundcloud.com/oembed": _oembed("Sandstorm by Darude"),
        "https://open.spotify.com/embed/track/": (200, _SP_TRACK),
        "https://open.spotify.com/embed/album/": (200, _SP_ALBUM),
        "https://open.spotify.com/embed/playlist/": (200, _SP_ALBUM),
    }
    flach = {
        "list=PLabc": [("https://www.youtube.com/watch?v=aaaaaaaaaaa", "YT eins"),
                       ("https://www.youtube.com/watch?v=bbbbbbbbbbb", "YT zwei")],
        "/sets/": [("https://soundcloud.com/a/eins", "SC eins"),
                   ("https://soundcloud.com/a/zwei", "SC zwei")],
        "on.soundcloud.com/SET": [("https://soundcloud.com/b/x", "B x"),
                                  ("https://soundcloud.com/b/y", "B y"),
                                  ("https://soundcloud.com/b/z", "B z")],
        "on.soundcloud.com/TRK": [("https://soundcloud.com/c/einzeln?si=1", "C einzeln")],
    }
    with _pl_frisch(netz=netz, flach=flach,
                    kurz="https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT") as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            w = _wizard(pl, kanal, bob)
            ergebnis = {}
            for name, text in (
                    ("yt", f"<{_YT}&t=42s>"),
                    ("yt_kurz", "https://youtu.be/dQw4w9WgXcQ?si=abc"),
                    ("yt_liste", "https://www.youtube.com/playlist?list=PLabc"),
                    ("sc", "https://soundcloud.com/darude/sandstorm?utm_source=x"),
                    ("sc_set", "https://soundcloud.com/a/sets/meins"),
                    ("sc_kurz_set", "https://on.soundcloud.com/SET"),
                    ("sc_kurz_track", "https://on.soundcloud.com/TRK"),
                    ("sp_track", "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT?si=x"),
                    ("sp_album", "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3"),
                    ("sp_liste", "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"),
                    ("sp_kurz", "https://spotify.link/aBcDeF"),
                    ("datei", "https://example.com/musik/Mein%20Lied.mp3"),
                    ("fremd", "https://example.com/rezept"),
                    ("podcast", "https://open.spotify.com/episode/512ojhOuo1ktJprKbVcKyQ")):
                vorher = len(w["songs"])
                _ok, msg = await _paste(pl, kanal, bob, text)
                ergebnis[name] = (w["songs"][vorher:], msg.endreaktion(), list(msg.reaktionen))
            return w, ergebnis
        w, e = asyncio.run(lauf())

    songs, reaktion, alle = e["yt"]
    assert songs[0]["i"] == _YT and songs[0]["t"] == "Rick Astley - Never Gonna Give You Up"
    assert reaktion == "✅" and ("+", "🔎") in alle and ("-", "🔎") in alle
    assert e["yt_kurz"][0] == [] and e["yt_kurz"][1] == "⚠️", "dasselbe Video = Dublette"
    assert [s["t"] for s in e["yt_liste"][0]] == ["YT eins", "YT zwei"]
    assert e["sc"][0][0]["i"] == "https://soundcloud.com/darude/sandstorm"
    assert e["sc"][0][0]["t"] == "Sandstorm by Darude"
    assert [s["t"] for s in e["sc_set"][0]] == ["SC eins", "SC zwei"]
    assert [s["t"] for s in e["sc_kurz_set"][0]] == ["B x", "B y", "B z"]
    assert e["sc_kurz_track"][0][0]["i"] == "https://soundcloud.com/c/einzeln"
    sp = e["sp_track"][0][0]
    assert sp["i"] == "ytsearch1:Rick Astley Never Gonna Give You Up"
    assert sp["h"] == {"query": "Rick Astley Never Gonna Give You Up", "dur": 214,
                       "title": "Never Gonna Give You Up", "artist": "Rick Astley"}
    assert sp["d"] == 214 and sp["t"] == "Rick Astley - Never Gonna Give You Up"
    assert [s["t"] for s in e["sp_album"][0]] == [
        "Rick Astley - Whenever You Need Somebody",
        "Rick Astley, Stock Aitken Waterman - Together Forever"], \
        "der erste Albumsong war schon da (Dublette), der kaputte Eintrag fliegt raus"
    assert e["sp_album"][0][1]["d"] == 205
    assert e["sp_liste"][0] == [] and e["sp_liste"][1] == "⚠️", "alles schon drin"
    assert e["sp_kurz"][0] == [] and e["sp_kurz"][1] == "⚠️", "Kurzlink -> derselbe Song"
    assert e["datei"][0][0]["t"] == "Mein Lied.mp3"
    for name in ("fremd", "podcast"):
        assert e[name][0] == [] and e[name][1] == "❌", name
    assert any("nix anfangen" in h for h in w["hinweise"])
    assert w["erweiterungen"] == 5      # yt_liste, sc_set, sc_kurz_set, sp_album, sp_liste


def test_assistent_privat_und_kaputt():
    """oEmbed sagt 'weg' (YouTube 404, SoundCloud 403) -> ❌ 'privat oder
    geloescht'. YouTubes 401 heisst nur 'Einbetten verboten' (Labels, VEVO) -
    das Video kommt rein. Ist das Netz weg, auch (mit Ersatztitel)."""
    kanal, bob = _Kanal(), _person(1)
    yt = ("https://www.youtube.com/oembed?format=json&url=https%3A%2F%2Fwww.youtube.com"
          "%2Fwatch%3Fv%3D")
    netz = {yt + "privatprivat": (404, "Not Found"),
            yt + "nichteinbet": (401, "Unauthorized"),
            "https://soundcloud.com/oembed": (403, "")}
    with _pl_frisch(netz=netz) as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            w = _wizard(pl, kanal, bob)
            r = []
            for text in ("https://youtu.be/privatprivat", "https://soundcloud.com/x/geheim",
                         "https://youtu.be/offlineoffl", "https://youtu.be/nichteinbet"):
                _ok, msg = await _paste(pl, kanal, bob, text)
                r.append(msg.endreaktion())
            return w, r
        w, r = asyncio.run(lauf())
    assert r == ["❌", "❌", "✅", "✅"], r
    assert [s["t"] for s in w["songs"]] == ["YouTube-Video offlineoffl",
                                            "YouTube-Video nichteinbet"]
    assert any("privat oder gelöscht" in h for h in w["hinweise"])


def test_soundcloud_kurzlink_auf_einen_track():
    """on.soundcloud.com auf EINEN Track: yt-dlp liefert keine Liste, also
    folgt Flo der Weiterleitung selbst und fragt oEmbed mit der echten Adresse.
    Geht auch das nicht, kommt der Kurzlink trotzdem rein - yt-dlp folgt ihm
    beim Abspielen, ein 'privat' waere geraten."""
    netz = {"https://soundcloud.com/oembed?format=json&url=https%3A%2F%2Fsoundcloud.com"
            "%2Fwer%2Fwas": _oembed("Was by Wer")}
    ziel = {"https://on.soundcloud.com/AbC": "https://soundcloud.com/wer/was?si=1&utm_x=2"}
    with _pl_frisch(netz=netz, ziel=ziel):
        songs, art, fehler = asyncio.run(musikquellen.eintraege("https://on.soundcloud.com/AbC"))
        unbekannt = asyncio.run(musikquellen.eintraege("https://on.soundcloud.com/XyZ?si=9"))
    assert art == "soundcloud" and fehler is None
    assert songs[0]["i"] == "https://soundcloud.com/wer/was" and songs[0]["t"] == "Was by Wer"
    assert unbekannt[0][0]["i"] == "https://on.soundcloud.com/XyZ" and unbekannt[2] is None


def test_assistent_mehrere_links_in_einer_zeile():
    kanal, bob = _Kanal(), _person(1)
    netz = {"https://www.youtube.com/oembed": _oembed("Video")}
    with _pl_frisch(netz=netz) as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob,
                         "hier: https://youtu.be/aaaaaaaaaaa und https://youtu.be/bbbbbbbbbbb")
            return _wizard(pl, kanal, bob)
        w = asyncio.run(lauf())
    assert [s["i"][-11:] for s in w["songs"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_assistent_loest_gleichzeitig_auf_und_haelt_die_reihenfolge():
    """Viele Links auf einmal werden parallel aufgeloest (hoechstens
    AUFLOESEN_PARALLEL), die Songs stehen trotzdem in Zeilen-Reihenfolge - und
    eine Zeile, die beim Aufloesen umfaellt, kippt die anderen nicht."""
    kanal, bob = _Kanal(), _person(1)
    gleichzeitig = [0, 0]

    async def eintraege(zeile, platz=50):
        gleichzeitig[0] += 1
        gleichzeitig[1] = max(gleichzeitig[1], gleichzeitig[0])
        nr = int(zeile.rsplit("v", 1)[-1])
        await asyncio.sleep(0.02 * (8 - nr))          # die ersten brauchen am laengsten
        gleichzeitig[0] -= 1
        if nr == 3:
            raise RuntimeError("kaputt")
        return [{"i": zeile, "t": f"T{nr}", "h": None, "d": None, "q": zeile}], "youtube", None

    links = "\n".join(f"https://youtu.be/aaaaaaaaav{nr}" for nr in range(8))
    with _pl_frisch() as pl, mock.patch.object(musikquellen, "eintraege", eintraege), \
            _Stumm("dcbot.playlists"):
        async def lauf():
            await _start(pl, kanal, bob)
            _ok, msg = await _paste(pl, kanal, bob, links)
            return _wizard(pl, kanal, bob), msg
        w, msg = asyncio.run(lauf())
    assert [s["t"] for s in w["songs"]] == ["T0", "T1", "T2", "T4", "T5", "T6", "T7"]
    assert 1 < gleichzeitig[1] <= playlists.AUFLOESEN_PARALLEL, gleichzeitig
    assert msg.endreaktion() == "⚠️"
    assert any("schiefgelaufen" in h for h in w["hinweise"])


def test_assistent_kurzlinks_auf_alben_zaehlen_mit():
    """Ob ein spotify.link ein Album ist, zeigt erst das Aufloesen - die
    Grenze MAX_ERWEITERUNGEN gilt trotzdem. Und ist rechnerisch kein Platz mehr,
    wird gar nicht erst aufgeloest."""
    kanal, bob = _Kanal(), _person(1)
    netz = {"https://open.spotify.com/embed/album/": (200, _SP_ALBUM)}

    def kurz(url):
        return f"https://open.spotify.com/album/{url.rsplit('/', 1)[-1]}"
    with _pl_frisch(netz=netz, kurz=kurz) as pl, \
            mock.patch.object(playlists, "MAX_ERWEITERUNGEN", 1):
        async def lauf():
            await _start(pl, kanal, bob)
            _ok, msg = await _paste(pl, kanal, bob,
                                    "https://spotify.link/eins\nhttps://spotify.link/zwei")
            return _wizard(pl, kanal, bob), msg
        w, msg = asyncio.run(lauf())
    assert len(w["songs"]) == 3 and w["erweiterungen"] == 1
    assert msg.endreaktion() == "⚠️"
    assert any("Alben/Playlists reichen" in h for h in w["hinweise"])
    with _pl_frisch() as pl, mock.patch.object(playlists, "MAX_SONGS", 2):
        async def lauf2():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "Song A")
            _ok, msg = await _paste(pl, kanal, bob,
                                    "https://youtu.be/aaaaaaaaaaa\nhttps://youtu.be/bbbbbbbbbbb\n"
                                    "https://youtu.be/ccccccccccc")
            return _wizard(pl, kanal, bob), msg, list(pl.abrufe)
        w, msg, abrufe = asyncio.run(lauf2())
    assert len(w["songs"]) == 2 and msg.endreaktion() == "⚠️"
    assert len([a for a in abrufe if "oembed" in str(a)]) == 1, abrufe


def test_flo_rida_ist_ein_song_und_mehrzeiliges_ein_paste():
    """'Flo Rida - Low' faengt mit Flos Namen an, ist aber ein Song. Und wer
    mehrere Zeilen einkippt, meint den Assistenten - auch wenn die erste mit
    'Flo' anfaengt."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            eins = await _paste(pl, kanal, bob, "Flo Rida - Low")
            zwei = await _paste(pl, kanal, bob, "Flo Milli - Never Lose Me\nDarude - Sandstorm")
            drei = await _paste(pl, kanal, bob, "flo spiel was anderes")
            return _wizard(pl, kanal, bob), eins[0], zwei[0], drei[0]
        w, eins, zwei, drei = asyncio.run(lauf())
    assert (eins, zwei, drei) == (True, True, False)
    assert [s["t"] for s in w["songs"]] == ["Flo Rida - Low", "Flo Milli - Never Lose Me",
                                            "Darude - Sandstorm"]


def test_grenze_je_person_gilt_auch_beim_speichern():
    """Waehrend der Assistent offen war, kam eine andere Liste dazu ('queue
    speichern') - beim 'fertig' gilt die Grenze je Person trotzdem."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl, mock.patch.object(playlists, "MAX_PRO_PERSON", 1):
        async def lauf():
            await _start(pl, kanal, bob, "Eins")
            await _paste(pl, kanal, bob, "Song A")
            pl._anlegen(77, "Inzwischen", bob, [])
            await _paste(pl, kanal, bob, "fertig")
        asyncio.run(lauf())
        namen = [l["name"] for l in _listen(pl)]
    assert namen == ["Inzwischen"] and not pl._wizards
    assert any("Nix gespeichert. Du hast schon 1 Playlists" in t for t in kanal.texte()), \
        kanal.texte()


def test_status_edit_kommt_nie_nach_der_karte():
    """Ein Status-Edit, der noch unterwegs ist, wenn 'fertig' kommt: die Karte
    wartet auf ihn und landet als LETZTE - sonst ueberschriebe der alte Status
    die Karte, und deren Knoepfe waeren weg."""
    kanal, bob = _Kanal(), _person(1)
    uhr = [1000.0]
    with _pl_frisch() as pl, mock.patch.object(playlists, "_uhr", lambda: uhr[0]):
        async def lauf():
            await _start(pl, kanal, bob)
            status = kanal.gesendet[0]
            w = _wizard(pl, kanal, bob)
            tor = asyncio.Event()
            echt = status.edit

            async def langsam(**kw):
                if not status.edits:
                    await tor.wait()           # der erste Status-Edit haengt
                return await echt(**kw)
            status.edit = langsam
            w["songs"].append({"i": "ytsearch1:a", "t": "A", "h": None, "d": None, "q": "A"})
            w["edit_faellig"] = True
            uhr[0] += 10
            unterwegs = asyncio.ensure_future(pl._status_jetzt(w))
            await asyncio.sleep(0)
            fertig = asyncio.ensure_future(_paste(pl, kanal, bob, "fertig"))
            await asyncio.sleep(0.01)
            tor.set()
            await unterwegs
            await fertig
            return status
        status = asyncio.run(lauf())
    assert len(status.edits) == 2
    assert _knopf_ids(status.edits[-1]["view"]), "die Karte ist das Letzte"
    assert not _knopf_ids(status.edits[0]["view"])


def test_assistent_grenzen_songs_und_alben():
    """Deckel MAX_SONGS mit dem Hilferuf-Satz; hoechstens MAX_ERWEITERUNGEN
    Alben/Playlists; mehr als MAX_ZEILEN Zeilen werden abgeschnitten."""
    kanal, bob = _Kanal(), _person(1)
    flach = {"list=PL": [(f"https://www.youtube.com/watch?v=v{i:010d}", f"T{i}")
                         for i in range(10)]}
    with _pl_frisch(flach=flach) as pl, mock.patch.object(playlists, "MAX_SONGS", 4):
        async def lauf():
            await _start(pl, kanal, bob)
            w = _wizard(pl, kanal, bob)
            _ok, m1 = await _paste(pl, kanal, bob, "https://www.youtube.com/playlist?list=PL1")
            n1 = len(w["songs"])
            erste_platz = [a for a in pl.abrufe if a[0] == "flach"][0][2]
            _ok, m2 = await _paste(pl, kanal, bob, "a\nb\nc\nd\ne")
            return w, n1, erste_platz, m1, m2
        w, n1, platz, m1, m2 = asyncio.run(lauf())
    assert platz == 4, "yt-dlp holt nur so viele, wie noch reinpassen"
    assert n1 == 4 and len(w["songs"]) == 4
    assert any("Hilferuf" in h for h in w["hinweise"])
    assert m2.endreaktion() == "❌"
    with _pl_frisch(flach=flach) as pl, mock.patch.object(playlists, "MAX_ERWEITERUNGEN", 1):
        async def lauf2():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "https://www.youtube.com/playlist?list=PL1")
            _ok, m = await _paste(pl, kanal, bob, "https://www.youtube.com/playlist?list=PL2")
            return _wizard(pl, kanal, bob), m
        w, m = asyncio.run(lauf2())
    assert len(w["songs"]) == 10 and m.endreaktion() == "❌"
    assert any("Alben/Playlists reichen" in h for h in w["hinweise"])
    with _pl_frisch() as pl, mock.patch.object(playlists, "MAX_ZEILEN", 3):
        async def lauf3():
            await _start(pl, kanal, bob)
            _ok, m = await _paste(pl, kanal, bob, "a\nb\nc\nd\ne")
            return _wizard(pl, kanal, bob), m
        w, m = asyncio.run(lauf3())
    assert [s["t"] for s in w["songs"]] == ["a", "b", "c"] and m.endreaktion() == "⚠️"


def test_fertig_woerter_speichern_und_machen_die_karte():
    """Jedes Fertig-Wort (auch mit Flo davor) speichert; die Statusnachricht
    wird zur Karte, dazu 💾. 'fertig' als letzte Zeile nimmt die Zeilen davor mit."""
    for wort in ("fertig", "stop", "stopp", "Fertig!", "das wars", "das war's", "passt",
                 "schluss", "feierabend", "speichern", "save", "done", "ende",
                 "flo fertig", "Flo, stop", "hey flo fertig"):
        kanal, bob = _Kanal(), _person(1)
        with _pl_frisch() as pl:
            async def lauf():
                await _start(pl, kanal, bob)
                await _paste(pl, kanal, bob, "Song A")
                return await _paste(pl, kanal, bob, wort)
            (ok, _msg) = asyncio.run(lauf())
            listen = _listen(pl)
        assert ok is True, wort
        assert len(listen) == 1 and listen[0]["name"] == "Zocken", wort
        assert listen[0]["songs"][0]["t"] == "Song A"
        status = kanal.gesendet[0]
        assert _knopf_ids(status.view) == [f"flo:pl:{a}:{listen[0]['id']}"
                                           for a in ("play", "mix", "add", "del")], wort
        assert any("💾 **Zocken** gespeichert, 1 Song" in t for t in kanal.texte()), wort
        assert not pl._wizards
    # Ein Fertig-Wort als letzte Zeile - auch mit Flo davor. Es wird nie
    # selbst zum Song.
    for letzte in ("fertig", "flo fertig", "flo stop", "Flo, das wars!", "hey flo passt"):
        kanal, bob = _Kanal(), _person(1)
        with _pl_frisch() as pl:
            async def lauf():
                await _start(pl, kanal, bob)
                return await _paste(pl, kanal, bob, f"Song A\nSong B\n{letzte}")
            ok, _m = asyncio.run(lauf())
            listen = _listen(pl)
            offen = bool(pl._wizards)
        assert ok and not offen, letzte
        assert [s["t"] for s in listen[0]["songs"]] == ["Song A", "Song B"], letzte
        assert _knopf_ids(kanal.gesendet[0].view), letzte


def test_abbruch_wort_als_letzte_zeile_wirft_weg():
    """'Song A\\nabbrechen' - die letzte Zeile bricht ab (auch 'flo abbrechen'),
    nichts wird gespeichert, und 'abbrechen' landet nie als Song."""
    for letzte in ("abbrechen", "flo abbrechen", "Flo, vergiss es"):
        kanal, bob = _Kanal(), _person(1)
        with _pl_frisch() as pl:
            async def lauf():
                await _start(pl, kanal, bob)
                await _paste(pl, kanal, bob, "Song A")
                return await _paste(pl, kanal, bob, f"Song B\nSong C\n{letzte}")
            ok, _m = asyncio.run(lauf())
            assert ok is True and not _listen(pl) and not pl._wizards, letzte
        assert "Weggeworfen" in _view_text(kanal.gesendet[0].view), letzte
    # Steht das Wort NICHT am Ende, ist es eine normale Zeile.
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf2():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "Fertig\nSong A")
            return _wizard(pl, kanal, bob)
        w = asyncio.run(lauf2())
    assert w is not None and [s["t"] for s in w["songs"]] == ["Fertig", "Song A"]


def test_mehrzeilig_an_flo_erste_zeile_ist_kein_song():
    """Mehrzeilig mit Flo vorn: die angesprochene erste Zeile ist Rede, kein
    Song (nur ihre Links zaehlen). Bleibt danach weniger als zwei Songs
    uebrig, war es gar kein Paste - dann gehoert die Nachricht Befehlen und KI."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            raus = {}
            for text in ("Flo hier meine Songs:\nSong A\nSong B",
                         "Flo\nwie spät ist es?",
                         "flo spiel Despacito\nund dann was anderes",
                         "<@1> nimm die\nSong C\n- Song D",
                         "flo https://youtu.be/aaaaaaaaaaa\nSong E",
                         "Flo\nSong F\nfertig"):
                msg = _Nachricht(text, bob, kanal)
                raus[text] = (await pl.on_message_passive(msg), msg.reaktionen)
            return raus
        raus = asyncio.run(lauf())
        (liste,) = _listen(pl)
    assert raus["Flo hier meine Songs:\nSong A\nSong B"][0] is True
    for text in ("Flo\nwie spät ist es?", "flo spiel Despacito\nund dann was anderes"):
        assert raus[text] == (False, []), (text, raus[text])
    assert all(raus[t][0] is True for t in ("<@1> nimm die\nSong C\n- Song D",
                                             "flo https://youtu.be/aaaaaaaaaaa\nSong E",
                                             "Flo\nSong F\nfertig"))
    assert [s["t"] for s in liste["songs"]] == [
        "Song A", "Song B", "Song C", "Song D", "YouTube-Video aaaaaaaaaaa", "Song E",
        "Song F"]
    assert not any("Flo" in s["t"] or "wie spät" in s["t"] or "Despacito" in s["t"]
                   for s in liste["songs"])
    # Ohne Flo vorn bleibt eine Ueberschrift mit Doppelpunkt trotzdem draussen.
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf2():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "Meine Lieblingssongs:\nSong A")
            return _wizard(pl, kanal, bob)
        w = asyncio.run(lauf2())
    assert [s["t"] for s in w["songs"]] == ["Song A"]


def test_fertig_ohne_songs_und_abbrechen():
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "fertig")
        asyncio.run(lauf())
        assert not _listen(pl) and not pl._wizards
    assert any("du Clown" in t for t in kanal.texte())
    for wort in ("abbrechen", "cancel", "vergiss es", "lass es", "doch nicht", "nvm",
                 "exit", "quit", "verwerfen", "flo abbrechen"):
        kanal, bob = _Kanal(), _person(1)
        with _pl_frisch() as pl:
            async def lauf():
                await _start(pl, kanal, bob)
                await _paste(pl, kanal, bob, "Song A")
                return await _paste(pl, kanal, bob, wort)
            ok, _m = asyncio.run(lauf())
            assert ok is True and not _listen(pl) and not pl._wizards, wort
        assert any("Weggeworfen. War vermutlich besser so." in t for t in kanal.texte()), wort
        assert "Weggeworfen" in _view_text(kanal.gesendet[0].view)


def test_an_flo_gerichtet_geht_durch_und_fremde_auch():
    """Was an Flo geht (ausser Fertig/Abbruch), gehoert den Befehlen und der
    KI. Andere Leute im Kanal, Bots und DMs sieht der Assistent gar nicht."""
    kanal, bob, anna = _Kanal(), _person(1), _person(2)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            raus = []
            for text, wer, kn in (("flo wie gehts", bob, kanal),
                                  ("flo stop die musik", bob, kanal),
                                  ("<@1> spiel was", bob, kanal),
                                  ("Song von Anna", anna, kanal),
                                  ("Song im anderen Kanal", bob, _Kanal(556))):
                raus.append(await pl.on_message_passive(_Nachricht(text, wer, kn)))
            bot_msg = _Nachricht("Song vom Bot", _person(3), kanal)
            bot_msg.author.bot = True
            raus.append(await pl.on_message_passive(bot_msg))
            raus.append(await pl.on_message_passive(_Nachricht("x", bob, kanal, guild=None)))
            return raus
        raus = asyncio.run(lauf())
        assert raus == [False] * 7, raus
        assert _wizard(pl, kanal, bob)["songs"] == []


def test_flo_stop_beendet_den_assistenten_nicht_die_musik():
    """Durch das echte on_message und das ECHTE music.handle: 'flo stop' mit
    offenem Assistenten speichert - music.handle sieht die Nachricht nie, der
    Player wird nicht getrennt. Dasselbe fuer 'flo stop' als letzte Zeile
    eines Pastes. Ohne Assistent stoppt 'flo stop' wie immer die Musik."""
    import bot
    kanal = _Kanal(555)
    musik = []
    echt = music.instance.handle

    async def music_handle(msg):
        musik.append(msg.content)
        return await echt(msg)

    getrennt = []

    async def trennen():
        getrennt.append(1)
    spieler = SimpleNamespace(voice=object(), active_channel_id=9, queue=[], current=None,
                              text_channel=None, disconnect=trennen)
    with _pl_frisch() as pl, contextlib.ExitStack() as stapel:
        for name in dir(bot):
            if name.endswith("_ENABLED") and isinstance(getattr(bot, name), bool):
                stapel.enter_context(mock.patch.object(
                    bot, name, name in ("MUSIC_ENABLED", "PLAYLISTS_ENABLED")))
        stapel.enter_context(mock.patch.object(bot.admin, "is_locked", lambda: False))
        stapel.enter_context(mock.patch.object(bot.music, "handle", music_handle))
        stapel.enter_context(mock.patch.object(music.instance, "_enabled", True))
        stapel.enter_context(mock.patch.dict(music.instance._players, {77: spieler}))

        def nachricht(text):
            msg = _rauch_nachricht(text, kanal=kanal)
            msg.channel = kanal
            msg.type = discord.MessageType.default
            return msg

        async def lauf():
            stand = []
            assert await pl.handle(nachricht("flo playlist neu Zocken")) is playlists.HANDLED
            await bot.client.on_message(nachricht("Song A"))
            await bot.client.on_message(nachricht("flo stop"))
            stand.append(([l["name"] for l in _listen(pl)], list(getrennt), list(musik)))
            assert await pl.handle(nachricht("flo playlist neu Abend")) is playlists.HANDLED
            await bot.client.on_message(nachricht("Song B\nSong C\nflo stop"))
            stand.append(([l["name"] for l in _listen(pl)], list(getrennt), list(musik)))
            await bot.client.on_message(nachricht("flo stop"))
            return stand
        stand = asyncio.run(lauf())
        abend = next(l for l in _listen(pl) if l["name"] == "Abend")
    assert stand[0] == (["Zocken"], [], []), stand
    assert stand[1] == (["Zocken", "Abend"], [], []), stand
    assert [s["t"] for s in abend["songs"]] == ["Song B", "Song C"]
    assert musik == ["flo stop"], "nur das letzte 'flo stop' (ohne Assistent) geht an die Musik"
    assert getrennt == [1], "und das trennt wirklich - die Probe oben war nicht taub"


def test_notlage_wird_nie_als_song_geschluckt():
    """Ein Notsignal im Assistenten: nicht verbraucht, keine Reaktion, kein
    Song - die Nachricht laeuft normal weiter (bot.py kuemmert sich)."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            raus = []
            for text in ("ich will nicht mehr leben", "Song A\nich will nicht mehr leben",
                         "flo ich will nicht mehr leben", "fertig, ich will sterben"):
                msg = _Nachricht(text, bob, kanal)
                raus.append((await pl.on_message_passive(msg), msg.reaktionen))
            return raus
        raus = asyncio.run(lauf())
        w = _wizard(pl, kanal, bob)
    assert all(ok is False and reaktionen == [] for ok, reaktionen in raus), raus
    assert w is not None and w["songs"] == []


def test_sendepause_haelt_den_assistenten_an():
    """In der Sendepause schluckt der Assistent nichts und sagt nichts, der
    Takt laesst ihn stehen (auch abgelaufen) - danach geht es weiter."""
    import admin
    kanal, bob = _Kanal(), _person(1)
    uhr = [1000.0]
    with _pl_frisch() as pl, mock.patch.object(playlists, "_uhr", lambda: uhr[0]):
        async def lauf():
            await _start(pl, kanal, bob)
            with mock.patch.object(admin, "is_locked", lambda: True):
                ok, msg = await _paste(pl, kanal, bob, "Song A")
                uhr[0] += 10_000
                await pl.tick(None)
                offen = _wizard(pl, kanal, bob) is not None
            await pl.tick(None)
            return ok, msg, offen
        ok, msg, offen = asyncio.run(lauf())
        assert ok is False and msg.reaktionen == [] and offen
        assert not pl._wizards, "nach der Pause laeuft die Frist ab"
    assert any("Fünf Minuten nix" in t for t in kanal.texte())


def test_musik_aus_takt_und_statusknoepfe_schweigen():
    """Wird 'music' auf dem Server abgeschaltet, waehrend ein Assistent offen
    ist: der Takt zieht keinen Status nach und postet bei Fristende kein
    '⏰ Du pennst?' mit Ping - er speichert still und nimmt die Knoepfe weg.
    Die Knoepfe der Statusnachricht pruefen Schalter und Sendepause wie die
    der Karte."""
    import admin
    kanal, bob = _Kanal(), _person(1)
    uhr = [1000.0]
    aus = mock.patch.object(playlists.features, "is_on_in", lambda g, k: k != "music")
    with _pl_frisch() as pl, mock.patch.object(playlists, "_uhr", lambda: uhr[0]):
        async def lauf():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "Song A")
            w = _wizard(pl, kanal, bob)
            status = kanal.gesendet[0]
            w["edit_faellig"] = True
            vorher = len(status.edits)
            knoepfe = [k for k in w["view"].walk_children() if isinstance(k, discord.ui.Button)]
            with aus:
                uhr[0] += 10
                await pl.tick(None)
                edits_ohne_frist = len(status.edits) - vorher
                klick_aus = _Klick(1)
                await knoepfe[0].callback(klick_aus)
                with mock.patch.object(admin, "is_locked", lambda: True):
                    klick_pause = _Klick(1)
                    await knoepfe[1].callback(klick_pause)
                noch_offen = _wizard(pl, kanal, bob) is w
                uhr[0] += playlists.WIZARD_TIMEOUT
                await pl.tick(None)
            return (edits_ohne_frist, klick_aus, klick_pause, noch_offen, status)
        edits, klick_aus, klick_pause, noch_offen, status = asyncio.run(lauf())
        listen = _listen(pl)
        offen = bool(pl._wizards)
    assert edits == 0, "Musik aus: kein Status-Edit im Takt"
    assert klick_aus.arten() == ["send"] and "Musik ist auf diesem Server aus" \
        in klick_aus.protokoll[0][1]
    assert klick_pause.arten() == ["send"] and "Sendepause" in klick_pause.protokoll[0][1]
    assert noch_offen, "die Klicks haben nichts geschlossen"
    assert not offen and [l["name"] for l in listen] == ["Zocken"]
    assert len(kanal.gesendet) == 1, "keine neue Nachricht, kein Ping"
    assert not any("<@1>" in t or "Du pennst" in t for t in kanal.texte())
    text = _view_text(status.view)
    assert "Musik ist auf diesem Server aus" in text and "**Zocken** (1 Song)" in text
    assert not _knopf_ids(status.view)
    # Geht still speichern nicht (Grenze), steht das ehrlich da - weiter ohne Ping.
    kanal = _Kanal()
    with _pl_frisch() as pl, mock.patch.object(playlists, "_uhr", lambda: uhr[0]), \
            mock.patch.object(playlists, "MAX_PRO_PERSON", 1), aus:
        async def lauf2():
            await _start(pl, kanal, bob, "Zwei")
            w = _wizard(pl, kanal, bob)
            w["songs"].append({"i": "ytsearch1:a", "t": "A", "h": None, "d": None, "q": "A"})
            pl._anlegen(77, "Eins", bob, [])
            uhr[0] += playlists.WIZARD_TIMEOUT + 1
            await pl.tick(None)
        asyncio.run(lauf2())
        assert [l["name"] for l in _listen(pl)] == ["Eins"] and not pl._wizards
    text = _view_text(kanal.gesendet[0].view)
    assert "Speichern ging nicht: Du hast schon 1 Playlists" in text and "Futsch: 1 Song." in text
    assert len(kanal.gesendet) == 1


def test_frist_speichert_oder_verwirft():
    """Fuenf Minuten ohne Songs: mit Songs speichert Flo selbst (⏰ Du pennst?),
    ohne verwirft er. Jeder Paste schiebt die Frist nach hinten."""
    kanal, bob = _Kanal(), _person(1)
    uhr = [1000.0]
    with _pl_frisch() as pl, mock.patch.object(playlists, "_uhr", lambda: uhr[0]):
        async def lauf():
            await _start(pl, kanal, bob)
            uhr[0] += playlists.WIZARD_TIMEOUT - 10
            await _paste(pl, kanal, bob, "Song A")
            uhr[0] += 20
            await pl.tick(None)
            noch_offen = bool(pl._wizards)
            uhr[0] += playlists.WIZARD_TIMEOUT
            await pl.tick(None)
            return noch_offen
        noch_offen = asyncio.run(lauf())
        listen = _listen(pl)
    assert noch_offen, "der Paste hat die Frist verlaengert"
    assert [l["name"] for l in listen] == ["Zocken"] and not pl._wizards
    assert any("⏰ Du pennst?" in t and "**1 Song** in **Zocken**" in t and "<@1>" in t
               for t in kanal.texte())
    assert _knopf_ids(kanal.gesendet[0].view)[0].startswith("flo:pl:play:")
    kanal = _Kanal()
    with _pl_frisch() as pl, mock.patch.object(playlists, "_uhr", lambda: uhr[0]):
        async def lauf2():
            await _start(pl, kanal, bob, "Leer")
            uhr[0] += playlists.WIZARD_TIMEOUT + 1
            await pl.tick(None)
        asyncio.run(lauf2())
        assert not _listen(pl) and not pl._wizards
    assert any("⏰ Fünf Minuten nix. **Leer** gibt's nicht" in t for t in kanal.texte())


def test_zu_spaet_eingekippt_wird_nicht_still_geschluckt():
    """Songs, die hinter 'fertig' auf die Sperre warten (der Assistent loest
    gerade noch auf), kommen zu spaet: ❌ und ein Satz, wohin damit - statt
    still verschluckt. Dasselbe, wenn der Assistent waehrend des Aufloesens
    zugeht (neuer Assistent derselben Person)."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "Song A")
            w = _wizard(pl, kanal, bob)
            await w["lock"].acquire()          # ein Paste loest gerade auf
            fertig = asyncio.ensure_future(_paste(pl, kanal, bob, "fertig"))
            await asyncio.sleep(0)
            spaet = asyncio.ensure_future(_paste(pl, kanal, bob, "Song B\nSong C"))
            await asyncio.sleep(0)
            w["lock"].release()
            return await fertig, await spaet
        (ok_fertig, _m), (ok_spaet, msg) = asyncio.run(lauf())
        (liste,) = _listen(pl)
    assert ok_fertig is True and ok_spaet is True
    assert [s["t"] for s in liste["songs"]] == ["Song A"]
    assert msg.endreaktion() == "❌"
    assert any("Zu spät, **Zocken** ist schon gespeichert" in t
               and "playlist Zocken dazu" in t for t in kanal.texte()), kanal.texte()
    # Zu, waehrend die Links noch aufgeloest werden.
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf2():
            halt = asyncio.Event()
            echt = musikquellen.eintraege

            async def langsam(zeile, platz=musikquellen.ERWEITERUNG_MAX):
                await halt.wait()
                return await echt(zeile, platz=platz)
            with mock.patch.object(musikquellen, "eintraege", langsam):
                await _start(pl, kanal, bob, "Alt")
                unterwegs = asyncio.ensure_future(
                    _paste(pl, kanal, bob, "https://youtu.be/aaaaaaaaaaa"))
                await asyncio.sleep(0)
                assert await _start(pl, kanal, bob, "Frisch") is playlists.HANDLED
                halt.set()
                _ok, msg = await unterwegs
            return msg, pl._wizards[(kanal.id, bob.id)]
        msg, neu = asyncio.run(lauf2())
    assert msg.endreaktion() == "❌" and ("-", "🔎") in msg.reaktionen
    assert neu["name"] == "Frisch" and neu["songs"] == []


def test_ein_assistent_je_person():
    """Ein neuer Assistent verwirft den alten (mit Meldung), auch in einem
    anderen Kanal. Zwei Leute haben je ihren eigenen."""
    k1, k2, bob, anna = _Kanal(1), _Kanal(2), _person(1), _person(2)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, k1, bob, "Alt")
            await _paste(pl, k1, bob, "Song A")
            await _start(pl, k2, bob, "Frisch")
            await _start(pl, k1, anna, "Annas")
            return pl._wizards
        wizards = asyncio.run(lauf())
        assert sorted(wizards) == [(1, 2), (2, 1)]
        assert wizards[(2, 1)]["name"] == "Frisch"
        assert any("alter Assistent für **Alt**" in h for h in wizards[(2, 1)]["hinweise"])
        assert "Weggeworfen" in _view_text(k1.gesendet[0].view)
        assert not _listen(pl)


def test_offene_sichern_beim_herunterfahren_einmal():
    """offene_sichern speichert jeden Assistenten mit Songs (auch 'dazu') und
    schliesst ihn - ein zweiter Aufruf haengt nichts doppelt an."""
    kanal, bob, anna = _Kanal(), _person(1), _person(2)
    with _pl_frisch() as pl:
        bestand = pl._anlegen(77, "Alt", anna, [{"i": "ytsearch1:x", "t": "x"}])

        async def lauf():
            await _start(pl, kanal, bob, "Frisch")
            await _paste(pl, kanal, bob, "Song A\nSong B")
            await pl.handle(_Nachricht("flo playlist alt dazu", anna, kanal))
            await _paste(pl, kanal, anna, "Song C\nx")
            await _start(pl, _Kanal(9), _person(3), "LeerBleibt")
            erst = await pl.offene_sichern()
            zweit = await pl.offene_sichern()
            return erst, zweit
        erst, zweit = asyncio.run(lauf())
        namen = sorted(l["name"] for l in _listen(pl))
        datei = pl._store.path
    assert (erst, zweit) == (2, 0) and not pl._wizards
    assert namen == ["Alt", "Frisch"]
    # Die Statusnachrichten sind jetzt Karten (deren Knoepfe ueberleben den
    # Neustart) - die leere sagt, dass zu ist.
    status = [m for m in kanal.gesendet if m.edits]
    assert len(status) == 2 and all(_knopf_ids(m.view) for m in status)
    assert [s["t"] for s in bestand["songs"]] == ["x", "Song C"], "x war Dublette"
    # Und es steht wirklich auf der Platte (flush_now).
    gelesen = json.loads(datei.read_text(encoding="utf-8"))
    assert sorted(l["name"] for l in gelesen["guilds"]["77"]["listen"].values()) == namen


def test_offene_sichern_sagt_ehrlich_wenn_speichern_nicht_ging():
    """Beim Herunterfahren: HAT der Assistent Songs, aber speichern geht nicht
    (Grenze je Person), steht der Grund in der Statusnachricht - nicht "War
    eh nix drin". Der leere sagt weiter genau das."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl, mock.patch.object(playlists, "MAX_PRO_PERSON", 1):
        async def lauf():
            await _start(pl, kanal, bob, "Voll")
            await _paste(pl, kanal, bob, "Song A\nSong B")
            pl._anlegen(77, "Inzwischen", bob, [])
            await _start(pl, kanal, _person(2), "Leer")
            return await pl.offene_sichern()
        gesichert = asyncio.run(lauf())
        namen = [l["name"] for l in _listen(pl)]
    assert gesichert == 0 and namen == ["Inzwischen"]
    voll, leer = (_view_text(m.view) for m in kanal.gesendet[:2])
    assert "Speichern ging nicht: Du hast schon 1 Playlists" in voll, voll
    assert "Futsch: 2 Songs." in voll and "War eh nix drin" not in voll
    assert "War eh nix drin" in leer and "Speichern ging nicht" not in leer


def test_speicher_rundreise_und_schluesseltypen():
    """Echter JsonStore: nach dem Speichern neu laden ergibt dieselben Daten,
    alle Schluessel sind Strings (JSON) und die Felder wie im Entwurf."""
    kanal, bob = _Kanal(), _person(1, name="Bob")
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "Song A\nfertig")
            await pl._store.flush_now()
        asyncio.run(lauf())
        name = pl._store.path.name
        daten = pl._store.data
        neu = JsonStore(name, default={"guilds": {}})
    assert neu.data == json.loads(json.dumps(daten))
    gilde = neu.data["guilds"]["77"]
    (pid, liste), = gilde["listen"].items()
    assert pid == liste["id"] and isinstance(pid, str)
    assert set(liste) == {"id", "name", "key", "von", "von_name", "erstellt", "geaendert",
                          "gespielt", "zuletzt", "songs"}
    assert liste["von"] == 1 and liste["von_name"] == "Bob" and liste["key"] == "zocken"
    assert set(liste["songs"][0]) == {"i", "t", "h", "d", "q"}


def test_dazu_nur_ersteller_oder_mods_und_ohne_dubletten():
    kanal, bob, anna, mod = _Kanal(), _person(1), _person(2), _person(3, mod=True)
    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, [{"i": "ytsearch1:song a", "t": "Song A"}])

        async def lauf():
            fremd = await pl.handle(_Nachricht("flo playlist zocken dazu", anna, kanal))
            eigen = await pl.handle(_Nachricht("flo playlist zocken dazu", bob, kanal))
            await _paste(pl, kanal, bob, "song a\nSong B\nfertig")
            mods = await pl.handle(_Nachricht("flo playlist dazu zocken", mod, kanal))
            await _paste(pl, kanal, mod, "Song C")
            await _paste(pl, kanal, mod, "fertig")
            return fremd, eigen, mods
        fremd, eigen, mods = asyncio.run(lauf())
    assert "Nicht deine Liste" in fremd
    assert eigen is playlists.HANDLED and mods is playlists.HANDLED
    assert [s["t"] for s in liste["songs"]] == ["Song A", "Song B", "Song C"]
    assert any("hat 1 neue, jetzt 2 Songs" in t for t in kanal.texte())


# --- Abspielen ------------------------------------------------------------------------
def _mit_play_many(antwort=None):
    """Ersetzt music._play_many/_player_for und merkt sich jeden Aufruf."""
    aufrufe = []
    spieler = SimpleNamespace(queue=[], text_channel=None)

    async def play_many(player, channel, items, requested_by, label, reply_to=None,
                        requested_by_id=0):
        aufrufe.append({"items": list(items), "von": requested_by, "label": label,
                        "id": requested_by_id, "kanal": channel, "reply_to": reply_to})
        return playlists.HANDLED if antwort is None else antwort

    stapel = contextlib.ExitStack()
    stapel.enter_context(mock.patch.object(music, "_play_many", play_many))
    stapel.enter_context(mock.patch.object(music, "_player_for", lambda gid: spieler))
    return stapel, aufrufe, spieler


def _songs(*titel):
    return [{"i": f"ytsearch1:{t}", "t": t, "h": {"query": t} if t == "B" else None,
             "d": 100, "q": t} for t in titel]


def test_abspielen_per_text_in_reihenfolge_und_gemischt():
    voice = _voice_kanal(2, 1)
    kanal, bob = _Kanal(), _person(1, voice=voice, name="Bob")
    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, _songs("A", "B", "C", "D", "E"))
        stapel, aufrufe, spieler = _mit_play_many()
        with stapel:
            async def lauf():
                eins = await pl.handle(_Nachricht("flo playlist zocken", bob, kanal))
                random.seed(7)
                zwei = await pl.handle(_Nachricht("flo spiel die playlist Zocken gemischt",
                                                  bob, kanal))
                return eins, zwei
            eins, zwei = asyncio.run(lauf())
    assert eins is playlists.HANDLED and zwei is playlists.HANDLED
    assert [i[1] for i in aufrufe[0]["items"]] == ["A", "B", "C", "D", "E"]
    assert aufrufe[0]["items"][1] == ("ytsearch1:B", "B", {"query": "B"})
    assert aufrufe[0]["id"] == 1 and aufrufe[0]["von"] == "Bob"
    assert aufrufe[0]["label"] == "aus „Zocken“" and aufrufe[0]["kanal"] is voice
    assert aufrufe[0]["reply_to"] is not None and spieler.text_channel is kanal
    random.seed(7)
    soll = random.sample(aufrufe[0]["items"], 5)
    assert aufrufe[1]["items"] == soll and sorted(soll) == sorted(aufrufe[0]["items"])
    assert liste["gespielt"] == 2 and liste["zuletzt"] > 0


def test_doppelt_abspielen_reiht_nicht_doppelt_ein():
    """Zweimal Abspielen, waehrend der erste Song noch laedt (zweimal getippt,
    Doppelklick auf ▶/🔀) - durch das ECHTE music._play_many: die Liste steht
    nur einmal in der Schlange, der Zweite bekommt 'Geduld' statt der
    Fehlermeldung "Den ersten Song konnte ich gerade nicht abspielen"."""
    voice = _voice_kanal()
    kanal, bob = _Kanal(), _person(1, voice=voice)
    mi = music.instance

    async def lauf(pl, liste):
        spieler = music.GuildPlayer(loop=asyncio.get_running_loop(), guild_id=77)
        gestartet = []

        async def connect(_k):
            return None

        async def still():
            return None

        def start(track, **_k):
            if spieler.current is not None:
                raise RuntimeError("Already playing audio.")
            spieler.current = track
            gestartet.append(track.title)
        spieler.connect, spieler._warte_bis_still, spieler.start = connect, still, start
        spieler.is_active = lambda: spieler.current is not None

        async def aufloesen(inp, _hint=None):
            await asyncio.sleep(0.02)           # der erste Song braucht seine Zeit
            return music.Track(title=inp, stream_url="http://s")

        async def panel(*_a, **_k):
            return None
        with mock.patch.object(music, "_player_for", lambda gid: spieler), \
                mock.patch.object(mi, "_resolve_input", aufloesen), \
                mock.patch.object(mi, "_send_panel", panel), \
                mock.patch.object(playlists.features, "is_on_in", lambda g, k: True):
            text = await asyncio.gather(
                pl.handle(_Nachricht("flo playlist zocken", bob, kanal)),
                pl.handle(_Nachricht("flo playlist zocken", bob, kanal)))
            stand_text = (list(gestartet), [t.title for t in spieler.queue])
            # Dasselbe mit den Knoepfen der Karte (▶ und 🔀 kurz hintereinander).
            spieler.current = None
            spieler.queue.clear()
            gestartet.clear()
            k1, k2 = _klick_im_voice(5, voice, gid=77), _klick_im_voice(5, voice, gid=77)
            k1.channel = k2.channel = kanal
            await asyncio.gather(pl.klick(k1, "play", liste["id"]),
                                 pl.klick(k2, "mix", liste["id"]))
            stand_klick = (list(gestartet), [t.title for t in spieler.queue])
            frei = set(pl._am_laden)
            # Danach ist der Weg wieder frei: wer jetzt nochmal will, haengt an.
            danach = await pl.handle(_Nachricht("flo playlist zocken", bob, kanal))
        return text, stand_text, k1, k2, stand_klick, frei, danach

    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, _songs("A", "B", "C"))
        text, stand_text, k1, k2, stand_klick, frei, danach = asyncio.run(lauf(pl, liste))
        gespielt = liste["gespielt"]
    assert text[0] is playlists.HANDLED and text[1] == playlists._SCHON_DABEI, text
    assert stand_text == (["ytsearch1:A"], ["B", "C"]), stand_text
    assert k1.arten() == ["defer"], k1.protokoll
    assert k2.arten() == ["send"] and k2.protokoll[0][2]["ephemeral"]
    assert "Ich lad hier gerade schon eine Playlist rein" in k2.protokoll[0][1]
    assert stand_klick == (["ytsearch1:A"], ["B", "C"]), stand_klick
    assert frei == set() and isinstance(danach, discord.Embed)
    assert "eingereiht" in danach.description and gespielt == 3
    # Auch wenn _play_many platzt, bleibt der Server nicht gesperrt.
    stapel, _a, _sp = _mit_play_many()

    async def kaputt(*_a, **_k):
        raise RuntimeError("Voice weg")
    with _pl_frisch() as pl, stapel, mock.patch.object(music, "_play_many", kaputt):
        pl._anlegen(77, "Zocken", bob, _songs("A"))
        with contextlib.suppress(RuntimeError):
            asyncio.run(pl.handle(_Nachricht("flo playlist zocken", bob, kanal)))
        assert pl._am_laden == set()


def test_abspielen_braucht_voice_und_songs():
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        pl._anlegen(77, "Zocken", bob, _songs("A"))
        pl._anlegen(77, "Leer", bob, [])
        stapel, aufrufe, _sp = _mit_play_many()
        with stapel:
            ohne = asyncio.run(pl.handle(_Nachricht("flo playlist zocken", bob, kanal)))
            bob.voice = SimpleNamespace(channel=_voice_kanal())
            leer = asyncio.run(pl.handle(_Nachricht("flo playlist leer", bob, kanal)))
    assert "Trockenschwimmer" in ohne and "ist leer" in leer and aufrufe == []


def test_abspielen_meldet_den_deckel_der_warteschlange():
    """_play_many kappt still - Flo sagt dazu, wie viele reingepasst haben."""
    voice = _voice_kanal()
    kanal, bob = _Kanal(), _person(1, voice=voice)
    eingereiht = discord.Embed(description="**3** Songs eingereiht", color=0x5865F2)
    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, _songs("A", "B", "C", "D", "E"))
        stapel, aufrufe, spieler = _mit_play_many(eingereiht)
        spieler.queue = [object()] * 7
        with stapel, mock.patch.object(music, "max_queue", lambda gid: 10):
            antwort = asyncio.run(pl.handle(_Nachricht("flo playlist zocken", bob, kanal)))
        voll = discord.Embed(description="voll", color=music._COL_ERR)
        stapel, _a, spieler = _mit_play_many(voll)
        with stapel:
            fehler = asyncio.run(pl.handle(_Nachricht("flo playlist zocken", bob, kanal)))
    assert "Nur 3 von 5 passen rein (Deckel 10). Der Rest bleibt in der Liste." \
        in antwort.description
    assert fehler is voll and "Nur" not in fehler.description
    assert liste["gespielt"] == 1, "ein Fehler zaehlt nicht als gespielt"


def test_abspielen_setzt_eine_geparkte_schlange_fort():
    """Echtes music._play_many: ist die Warteschlange geparkt, haengen sich die
    Playlist-Songs hinten an und die alte Schlange laeuft weiter."""
    voice = _voice_kanal()
    kanal, bob = _Kanal(), _person(1, voice=voice)
    schleife = asyncio.new_event_loop()
    spieler = music.GuildPlayer(loop=schleife, guild_id=77)
    alt = music.Track(title="Alt", stream_url="", query="ytsearch1:Alt")
    spieler.queue.append(alt)
    spieler._geparkt = True
    weiter = []

    async def connect(_kanal):
        return None

    async def advance(gen=None):
        weiter.append(list(spieler.queue))
    spieler.connect = connect
    spieler._advance = advance
    try:
        with _pl_frisch() as pl, mock.patch.object(music, "_player_for", lambda gid: spieler):
            pl._anlegen(77, "Zocken", bob, _songs("A", "B"))
            antwort = asyncio.run(pl.handle(_Nachricht("flo playlist zocken", bob, kanal)))
    finally:
        schleife.close()
    assert isinstance(antwort, discord.Embed) and "eingereiht" in antwort.description
    assert len(weiter) == 1 and [t.title for t in weiter[0]] == ["Alt", "A", "B"]
    assert all(t.requested_by_id == 1 for t in weiter[0][1:])


def test_queue_speichern():
    kanal, bob = _Kanal(), _person(1)
    jetzt = music.Track(title="Jetzt", stream_url="http://s", query="ytsearch1:jetzt",
                        webpage_url="https://www.youtube.com/watch?v=jetztjetzt1", duration=200)
    spaeter = music.Track(title="Später", stream_url="", query="ytsearch1:Rick Astley x",
                          match_hint={"query": "Rick Astley x", "dur": 100})
    spieler = SimpleNamespace(current=jetzt, queue=[spaeter, spaeter])
    with _pl_frisch() as pl, mock.patch.dict(music.instance._players, {77: spieler}):
        async def lauf():
            ok = await pl.handle(_Nachricht("flo queue speichern als Abend", bob, kanal))
            doppelt = await pl.handle(_Nachricht("flo warteschlange speichern abend", bob, kanal))
            ohne = await pl.handle(_Nachricht("flo queue speichern", bob, kanal))
            return ok, doppelt, ohne
        ok, doppelt, ohne = asyncio.run(lauf())
        (liste,) = _listen(pl)
    assert ok is playlists.HANDLED
    assert [s["i"] for s in liste["songs"]] == ["https://www.youtube.com/watch?v=jetztjetzt1",
                                                "ytsearch1:Rick Astley x"]
    assert liste["songs"][0]["h"] is None and liste["songs"][0]["d"] == 200
    assert liste["songs"][1]["h"] == {"query": "Rick Astley x", "dur": 100}
    assert "Warteschlange gespeichert" in _view_text(kanal.gesendet[0].view)
    assert "gibt's schon" in doppelt and "heißen" in ohne
    with _pl_frisch() as pl, mock.patch.dict(music.instance._players, {}, clear=True):
        leer = asyncio.run(pl.handle(_Nachricht("flo queue speichern als x", bob, kanal)))
    assert "die Stille" in leer


def test_umbenennen_raus_zeigen_und_loeschen_per_text():
    kanal, bob, anna = _Kanal(), _person(1), _person(2)
    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, _songs(*[f"S{i}" for i in range(30)]))
        pl._anlegen(77, "Chill", anna, [])

        async def lauf():
            r = {}
            for text, wer in (("flo playlist zocken umbenennen in Chill", bob),
                              ("flo playlist zocken umbenennen in Party Zeit", anna),
                              ("flo playlist zocken umbenennen in Party Zeit", bob),
                              ("flo playlist party zeit raus 2", bob),
                              ("flo playlist party zeit raus 99", bob),
                              ("flo playlist party zeit zeig", anna),
                              ("flo playlist party zeit löschen", anna),
                              ("flo playlist party zeit löschen", bob)):
                r[(text, wer.id)] = await pl.handle(_Nachricht(text, wer, kanal))
            return r
        r = asyncio.run(lauf())
        assert "gibt's schon" in r[("flo playlist zocken umbenennen in Chill", 1)]
        assert "Nicht deine Liste" in r[("flo playlist zocken umbenennen in Party Zeit", 2)]
        assert "heißt jetzt **Party Zeit**" in r[("flo playlist zocken umbenennen in Party Zeit", 1)]
        assert liste["name"] == "Party Zeit" and liste["key"] == "party zeit"
        assert "**S1** fliegt aus" in r[("flo playlist party zeit raus 2", 1)]
        assert "Zählen üben" in r[("flo playlist party zeit raus 99", 1)]
        assert len(liste["songs"]) == 29
        karte = kanal.gesendet[0].view
        text = _view_text(karte)
        assert "`25.`" in text and "`26.`" not in text and "…und 4 weitere" in text
        assert "Nicht deine Liste" in r[("flo playlist party zeit löschen", 2)]
        frage_msg = kanal.gesendet[-1]
        frage = frage_msg.kw["view"]
        assert "wirklich löschen?" in frage_msg.content
        # Ein Fremder darf nicht bestaetigen.
        fremd = _Klick(2)
        assert asyncio.run(frage.interaction_check(fremd)) is False
        assert "nicht gefragt" in fremd.protokoll[0][1]
        klick = _Klick(1, gid=77)
        klick.user = bob
        asyncio.run(frage.children[0].callback(klick))
        assert liste["id"] not in pl._listen(77)
        assert klick.arten() == ["edit"]
        assert "gelöscht von User1. RIP." in klick.protokoll[0][1]["content"]
        nein = _Klick(1, gid=77)
        asyncio.run(frage.children[1].callback(nein))
        assert "Feigling" in nein.protokoll[0][1]["content"]


# --- Karte und Knoepfe ------------------------------------------------------------------
def _karte_klicken(view, custom_id, klick):
    """Ein Klick auf einen Knopf der Karte - ueber den echten Weg von discord.py
    nach einem Neustart (nur die Vorlagen sind bekannt, die View nicht), fuer
    eine Components-V2-Nachricht."""
    import discord.ui.view as ui_view
    from discord.components import _component_factory
    klick.message.components = [_component_factory(d) for d in view.to_components()]
    klick.message.flags = SimpleNamespace(components_v2=True)
    typ = next(k.type.value for k in view.walk_children()
               if getattr(k, "custom_id", None) == custom_id)
    klick.data = {"custom_id": custom_id, "component_type": typ, "values": []}
    lager = ui_view.ViewStore(SimpleNamespace())
    lager.add_dynamic_items(*playlists.DYNAMISCHE_KNOEPFE)

    async def lauf():
        treffer = 0
        for muster, fabrik in list(lager._dynamic_items.items()):
            passt = muster.fullmatch(custom_id)
            if passt is not None:
                treffer += 1
                await lager.schedule_dynamic_item_call(typ, fabrik, klick, custom_id, passt)
        return treffer
    return asyncio.run(lauf())


def test_karte_hat_vier_neustartfeste_knoepfe():
    bob = _person(1)
    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, _songs(*[f"S{i}" for i in range(12)]))
        karte = pl._karte(liste)
    assert karte.timeout is None
    ids = _knopf_ids(karte)
    assert ids == [f"flo:pl:{a}:{liste['id']}" for a in ("play", "mix", "add", "del")]
    assert all(len(i) <= 100 for i in ids)
    texte = [k.item.label for k in karte.walk_children() if isinstance(k, discord.ui.DynamicItem)]
    assert texte == ["Abspielen", "Gemischt", "Songs dazu", "Löschen"]
    text = _view_text(karte)
    assert "### 🎶 Zocken" in text and "12 Songs" in text and "20 min" in text
    assert "`8.` S7" in text and "`9.`" not in text and "…und 4 weitere" in text
    # Kein Muster eines anderen Moduls passt auf die Playlist-Knoepfe - und
    # umgekehrt.
    import bot
    for modul in bot.DYNAMISCHE_MODULE:
        for klasse in getattr(modul, "DYNAMISCHE_KNOEPFE", ()):
            if modul is playlists:
                continue
            for custom_id in ids:
                assert not klasse.__discord_ui_compiled_template__.fullmatch(custom_id), klasse
    assert playlists in bot.DYNAMISCHE_MODULE


def test_karte_klick_play_mix_add_del():
    voice = _voice_kanal(3, 0)
    kanal = _Kanal()
    bob = _person(1)
    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, _songs("A", "B", "C"))
        karte = pl._karte(liste)
        stapel, aufrufe, spieler = _mit_play_many()
        with stapel, mock.patch.object(playlists.features, "is_on_in", lambda g, k: True):
            # Abspielen: erst bestaetigen (defer), dann Musik.
            klick = _klick_im_voice(5, voice, gid=77)
            klick.channel = kanal
            assert _karte_klicken(karte, f"flo:pl:play:{liste['id']}", klick) == 1
            assert klick.arten() == ["defer"] and len(aufrufe) == 1
            assert aufrufe[0]["id"] == 5 and [i[1] for i in aufrufe[0]["items"]] == ["A", "B", "C"]
            assert spieler.text_channel is kanal and liste["gespielt"] == 1
            # Gemischt mit Fehler-Embed -> Followup.
            stapel2, aufrufe2, _sp = _mit_play_many(discord.Embed(description="kaputt",
                                                                  color=music._COL_ERR))
            with stapel2:
                klick = _klick_im_voice(5, voice, gid=77)
                klick.channel = kanal
                _karte_klicken(karte, f"flo:pl:mix:{liste['id']}", klick)
            assert klick.arten() == ["defer", "followup"] and len(aufrufe2[0]["items"]) == 3
            # Nicht im Voice.
            klick = _Klick(5, gid=77)
            _karte_klicken(karte, f"flo:pl:play:{liste['id']}", klick)
            assert klick.arten() == ["send"] and "Trockenschwimmer" in klick.protokoll[0][1]
            # Songs dazu: nur Ersteller/Mods.
            klick = _klick_im_voice(5, voice, gid=77)
            _karte_klicken(karte, f"flo:pl:add:{liste['id']}", klick)
            assert "Nicht deine Liste" in klick.protokoll[0][1] and not pl._wizards
            klick = _klick_im_voice(6, voice, gid=77, mod=True)
            klick.channel = kanal
            _karte_klicken(karte, f"flo:pl:add:{liste['id']}", klick)
            assert klick.arten() == ["defer"] and (kanal.id, 6) in pl._wizards
            assert pl._wizards[(kanal.id, 6)]["modus"] == "dazu"
            assert "Songs dazu" in _view_text(kanal.gesendet[-1].view)
            # Loeschen: Rueckfrage (ephemeral), dann Karte -> RIP.
            klick = _klick_im_voice(5, voice, gid=77)
            _karte_klicken(karte, f"flo:pl:del:{liste['id']}", klick)
            assert "Nicht deine Liste" in klick.protokoll[0][1]
            klick = _Klick(1, gid=77, member=bob)
            _karte_klicken(karte, f"flo:pl:del:{liste['id']}", klick)
            assert klick.arten() == ["send"] and klick.protokoll[0][2]["ephemeral"]
            frage = klick.protokoll[0][2]["view"]
            ja = _Klick(1, gid=77, member=bob)
            asyncio.run(frage.children[0].callback(ja))
            assert ja.protokoll[0][1]["content"] == "Erledigt. 🪦"
            assert liste["id"] not in pl._listen(77)
            rip = [e[1] for e in klick.protokoll if e[0] == "panel_edit"]
            assert rip and "RIP" in _view_text(rip[-1]["view"])
            # Veraltete Karte.
            klick = _klick_im_voice(5, voice, gid=77)
            _karte_klicken(karte, f"flo:pl:play:{liste['id']}", klick)
            assert "gibt's nicht mehr. Weg ist weg." in klick.protokoll[0][1]


def test_karte_klick_respektiert_schalter_und_sendepause():
    import admin
    bob = _person(1)
    with _pl_frisch() as pl:
        liste = pl._anlegen(77, "Zocken", bob, _songs("A"))
        karte = pl._karte(liste)
        with mock.patch.object(playlists.features, "is_on_in", lambda g, k: k != "music"):
            klick = _Klick(1, gid=77)
            _karte_klicken(karte, f"flo:pl:play:{liste['id']}", klick)
        assert "Musik ist auf diesem Server aus" in klick.protokoll[0][1]
        with mock.patch.object(admin, "is_locked", lambda: True):
            klick = _Klick(1, gid=77)
            _karte_klicken(karte, f"flo:pl:play:{liste['id']}", klick)
        assert "Sendepause" in klick.protokoll[0][1]
        pl._enabled = False
        klick = _Klick(1, gid=77)
        _karte_klicken(karte, f"flo:pl:play:{liste['id']}", klick)
        assert "gerade aus" in klick.protokoll[0][1]


def test_status_knoepfe_nur_fuer_den_besitzer():
    """✅ Fertig / 🗑 Abbrechen an der Statusnachricht: nur der Besitzer; Fertig
    macht die Statusnachricht in der Klick-Antwort zur Karte."""
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf():
            await _start(pl, kanal, bob)
            await _paste(pl, kanal, bob, "Song A")
            w = _wizard(pl, kanal, bob)
            ansicht = w["view"]
            fremd = _Klick(2)
            darf = await ansicht.interaction_check(fremd)
            klick = _Klick(1)
            knoepfe = [k for k in ansicht.walk_children() if isinstance(k, discord.ui.Button)]
            await knoepfe[0].callback(klick)
            return darf, fremd, klick, ansicht
        darf, fremd, klick, ansicht = asyncio.run(lauf())
        (liste,) = _listen(pl)
    assert darf is False and "nicht dein Assistent" in fremd.protokoll[0][1]
    assert klick.arten() == ["edit", "followup"]
    assert _knopf_ids(klick.protokoll[0][1]["view"])[0] == f"flo:pl:play:{liste['id']}"
    assert "gespeichert" in klick.protokoll[1][1]
    assert ansicht.is_finished(), "die Status-View (ohne DynamicItems) ist gestoppt"
    kanal, bob = _Kanal(), _person(1)
    with _pl_frisch() as pl:
        async def lauf2():
            await _start(pl, kanal, bob)
            ansicht = _wizard(pl, kanal, bob)["view"]
            klick = _Klick(1)
            knoepfe = [k for k in ansicht.walk_children() if isinstance(k, discord.ui.Button)]
            await knoepfe[1].callback(klick)
            nochmal = _Klick(1)
            await knoepfe[1].callback(nochmal)
            return klick, nochmal
        klick, nochmal = asyncio.run(lauf2())
        assert not pl._wizards and not _listen(pl)
    assert klick.arten() == ["edit"] and "Weggeworfen" in _view_text(klick.protokoll[0][1]["view"])
    assert "schon zu" in nochmal.protokoll[0][1]


def test_aufraeum_kanal_schutz():
    """Statusnachricht geschuetzt, solange der Assistent offen ist; die Karte
    danach zehn Minuten stehen lassen (freigeben mit delay=600)."""
    kanal, bob = _Kanal(), _person(1)
    geschuetzt, frei = [], []
    with _pl_frisch() as pl, \
            mock.patch.object(laufzeit, "protect_message", lambda m: geschuetzt.append(m.id)), \
            mock.patch.object(laufzeit, "release_message",
                              lambda m, **k: frei.append((m.id, k.get("delay")))):
        async def lauf():
            await _start(pl, kanal, bob)
            status = kanal.gesendet[0].id
            schutz_offen = list(geschuetzt)
            await _paste(pl, kanal, bob, "Song A\nfertig")
            await pl.handle(_Nachricht("flo playlist zocken zeig", bob, kanal))
            return status, schutz_offen
        status, schutz_offen = asyncio.run(lauf())
    karte = kanal.gesendet[-1].id
    assert schutz_offen == [status]
    assert (status, playlists.KARTE_FREIGABE) in frei
    assert karte in geschuetzt and (karte, 600) in frei


# --- Spotify ohne Keys und die Quellen direkt -------------------------------------------
def test_spotify_ohne_keys_aus_fixture():
    meta = musikquellen.track_aus_embed(_SP_TRACK)
    assert meta == {"query": "Rick Astley Never Gonna Give You Up",
                    "name": "Never Gonna Give You Up", "artist": "Rick Astley", "dur": 214,
                    "artists": "Rick Astley",
                    "display": "Rick Astley - Never Gonna Give You Up"}
    liste = musikquellen.liste_aus_embed(_SP_ALBUM)
    assert [m["name"] for m in liste] == ["Never Gonna Give You Up",
                                         "Whenever You Need Somebody", "Together Forever"]
    assert [m["dur"] for m in liste] == [214, 234, 205]
    assert liste[2]["artist"] == "Rick Astley" and "Stock Aitken" in liste[2]["artists"]
    assert len(musikquellen.liste_aus_embed(_SP_ALBUM, ende=2)) == 2
    # Andere Verschachtelung, Felder mit anderen Namen, ohne Dauer.
    anders = _next_data({"x": [{"y": {"entity": {"title": "Song", "subtitle": "Wer",
                                                 "duration": "kaputt"}}}]})
    assert musikquellen.track_aus_embed(anders)["query"] == "Wer Song"
    assert musikquellen.track_aus_embed(anders)["dur"] is None
    # Der Aufbau der Spotify-Hauptseiten: Base64 im initial-state.
    import base64 as b64
    roh = b64.b64encode(json.dumps({"entities": {"items": {"x": {
        "type": "track", "name": "Basis", "artists": [{"profile": {"name": "Vier"}}],
        "duration": {"totalMilliseconds": 61000}}}}}).encode()).decode()
    html = f'<script id="initial-state" type="text/plain">{roh}</script>'
    assert musikquellen.track_aus_embed(html)["query"] == "Vier Basis"
    # Muell darf nie werfen.
    for kaputt in ("", "<html></html>", '<script id="__NEXT_DATA__">{kaputt</script>',
                   _next_data({"entity": "text"}), _next_data([1, 2, 3]),
                   _next_data({"entity": {"name": ""}}), None):
        assert musikquellen.track_aus_embed(kaputt) is None, kaputt
        assert musikquellen.liste_aus_embed(kaputt) is None, kaputt


def test_spotify_ohne_keys_holt_und_faellt_auf_oembed_zurueck():
    with _pl_frisch(netz={"https://open.spotify.com/embed/track/": (200, _SP_TRACK)}) as pl:
        meta = asyncio.run(musikquellen.spotify_track_ohne_keys(
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=1"))
        assert pl.abrufe == ["https://open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT"]
    assert meta["query"] == "Rick Astley Never Gonna Give You Up" and meta["dur"] == 214
    netz = {"https://open.spotify.com/embed/track/": (200, "<html>umgebaut</html>"),
            "https://open.spotify.com/oembed": (200, json.dumps({"title": "Nur Titel"}))}
    with _pl_frisch(netz=netz), _Stumm("dcbot.musikquellen"):
        meta = asyncio.run(musikquellen.spotify_track_ohne_keys(
            "spotify:track:4cOdK2wGLETKBW3PvgPWqT"))
    assert meta["query"] == "Nur Titel" and meta["dur"] is None
    with _pl_frisch():
        assert asyncio.run(musikquellen.spotify_track_ohne_keys(
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")) is None
        assert asyncio.run(musikquellen.spotify_track_ohne_keys("https://x.de")) is None
    netz = {"https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M": (200, _SP_ALBUM)}
    with _pl_frisch(netz=netz):
        liste = asyncio.run(musikquellen.spotify_liste_ohne_keys(
            "https://open.spotify.com/user/spotify/playlist/37i9dQZF1DXcBWIGoYBM5M", ende=1))
    assert [m["name"] for m in liste] == ["Never Gonna Give You Up"]


class _Stumm:
    """Gewollte Warnungen nicht ins Testprotokoll lassen."""

    def __init__(self, name):
        self.logger = logging.getLogger(name)

    def __enter__(self):
        self.alt = self.logger.disabled
        self.logger.disabled = True

    def __exit__(self, *_a):
        self.logger.disabled = self.alt
        return False


def test_eintraege_textzeile_ist_lazy_und_ohne_netz():
    with _pl_frisch() as pl:
        songs, art, fehler = asyncio.run(musikquellen.eintraege("  Daft   Punk - One More Time "))
        assert pl.abrufe == []
    assert art == "text" and fehler is None
    assert songs == [{"i": "ytsearch1:Daft Punk - One More Time",
                      "t": "Daft Punk - One More Time", "h": None, "d": None,
                      "q": "Daft   Punk - One More Time"}]
    assert asyncio.run(musikquellen.eintraege("   ")) == ([], "", None)
    assert musikquellen.ist_erweiterung("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3")
    assert not musikquellen.ist_erweiterung(_YT)


def test_musikquellen_frist_aus_der_env():
    """MUSIKQUELLEN_FRIST: Zahl gilt, Tippfehler/0/leer fallen auf die Vorgabe."""
    for wert, soll in (("7.5", 7.5), ("abc", 5.0), ("0", 5.0), ("-3", 5.0), ("", 5.0)):
        with mock.patch.dict(os.environ, {"MUSIKQUELLEN_FRIST": wert}):
            assert musikquellen._env_sekunden("MUSIKQUELLEN_FRIST", 5.0) == soll, wert
    # Alben/Playlists duerfen so lange wie frueher music._spotify_playlist_via_embed.
    fristen = []

    async def holen(url, *, frist=None):
        fristen.append((url, frist))
        return 0, ""
    with mock.patch.object(musikquellen, "_holen", holen):
        asyncio.run(musikquellen.spotify_liste_ohne_keys(
            "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3"))
    assert fristen == [("https://open.spotify.com/embed/album/1DFixLWuPkv3KT3TnV35m3", 15.0)]


def test_music_spielt_spotify_links_ohne_keys():
    """music._einzeln_spielen: liefert die API nichts (keine Keys), kommt der
    Song aus der Einbettungsseite - mit Best-Match-Hinweis und YouTube-Suche
    als Erneuerungs-Quelle. Der Album-Zweig faellt genauso zurueck."""
    mi = music.instance
    gesehen = []

    async def resolve_input(inp, hint):
        gesehen.append((inp, hint))
        return music.Track(title="Rick", stream_url="http://s/x", duration=213)

    player = SimpleNamespace(fremden_ton_stoppen=lambda: None, guild_id=77, queue=[])
    msg = SimpleNamespace(author=SimpleNamespace(display_name="Bob", id=9))

    async def kein_meta(_url):
        return None

    class _Halt(Exception):
        """Nach dem Aufloesen anhalten - kein echter Voice-Fehler (der loggt)."""

    async def bis_hier(*_a, **_k):
        raise _Halt()

    netz = {"https://open.spotify.com/embed/track/": (200, _SP_TRACK)}
    with _pl_frisch(netz=netz), mock.patch.object(mi, "_resolve_input", resolve_input), \
            mock.patch.object(mi, "_spotify_track_meta", kein_meta):
        player.connect = bis_hier
        try:
            asyncio.run(mi._einzeln_spielen(
                player, msg, "play", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
                SimpleNamespace(channel=None)))
        except _Halt:
            pass
    assert gesehen == [("ytsearch1:Rick Astley Never Gonna Give You Up",
                        {"query": "Rick Astley Never Gonna Give You Up", "dur": 214,
                         "title": "Never Gonna Give You Up", "artist": "Rick Astley"})]
    # Und ohne Einbettung: ehrliche Meldung statt Absturz.
    with _pl_frisch(), mock.patch.object(mi, "_spotify_track_meta", kein_meta):
        antwort = asyncio.run(mi._einzeln_spielen(
            player, msg, "play", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
            SimpleNamespace(channel=None)))
    assert "Künstler und Titel" in _embed_text(antwort)
    # Album-Zweig in handle.
    abgespielt = []

    async def play_many(player, channel, items, requested_by, label, reply_to=None,
                        requested_by_id=0):
        abgespielt.append((items, label, requested_by_id))
        return music.HANDLED

    netz = {"https://open.spotify.com/embed/album/": (200, _SP_ALBUM)}
    nachricht = SimpleNamespace(
        content="flo spiel https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3",
        guild=SimpleNamespace(id=4243), channel=_Kanal(),
        author=_person(9, voice=_voice_kanal()), mentions=[])
    alt = (mi._enabled, dict(mi._players))
    try:
        mi._enabled = True
        with _pl_frisch(netz=netz), mock.patch.object(mi, "_play_many", play_many):
            antwort = asyncio.run(mi.handle(nachricht))
    finally:
        (mi._enabled, mi._players) = alt
    assert antwort is music.HANDLED
    items, label, wer = abgespielt[0]
    assert label == "aus dem Album" and wer == 9 and len(items) == 3
    # Gesucht wird wie bei music._spotify_track_meta mit dem Haupt-Kuenstler
    # (die Komma-Liste verwaessert die YouTube-Suche), angezeigt werden alle.
    assert items[2] == ("ytsearch1:Rick Astley Together Forever",
                        "Rick Astley, Stock Aitken Waterman - Together Forever",
                        {"query": "Rick Astley Together Forever",
                         "dur": 205, "title": "Together Forever", "artist": "Rick Astley"})


def test_music_liest_spotify_playlists_an_einer_stelle():
    """'flo spiel <Spotify-Playlist>' liest die Einbettung ueber musikquellen
    (dieselbe Stelle wie der Assistent) - mit Dauer fuer den Best-Match. Der
    alte Helfer _spotify_playlist_via_embed liefert weiter seine Textliste."""
    mi = music.instance
    abgespielt = []

    async def play_many(player, channel, items, requested_by, label, reply_to=None,
                        requested_by_id=0):
        abgespielt.append((items, label))
        return music.HANDLED

    url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
    netz = {"https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M": (200, _SP_ALBUM)}
    nachricht = SimpleNamespace(content=f"flo spiel {url}", guild=SimpleNamespace(id=4244),
                                channel=_Kanal(), author=_person(9, voice=_voice_kanal()),
                                mentions=[])
    alt = (mi._enabled, dict(mi._players))
    try:
        mi._enabled = True
        with _pl_frisch(netz=netz), mock.patch.object(mi, "_play_many", play_many):
            antwort = asyncio.run(mi.handle(nachricht))
            texte = asyncio.run(mi._spotify_playlist_via_embed(url))
    finally:
        (mi._enabled, mi._players) = alt
    assert antwort is music.HANDLED
    items, label = abgespielt[0]
    assert label == "aus der Playlist" and [i[2]["dur"] for i in items] == [214, 234, 205]
    assert texte[0] == "Rick Astley - Never Gonna Give You Up" and len(texte) == 3


def test_arzt_prueft_spotify_ohne_keys_live():
    """'bash k m' prueft die Einbettungsseite wirklich - ohne Bot-Import (arzt.py)."""
    import inspect as _inspect
    import tools_musik_check
    quelle = _inspect.getsource(tools_musik_check.MusikCheck.spotify_ohne_keys_pruefen)
    assert "open.spotify.com/embed/track" in quelle and "open.spotify.com/embed/album" in quelle
    assert "import music" not in open("tools_musik_check.py", encoding="utf-8").read()
    arzt = tools_musik_check.MusikCheck()
    antworten = {"embed/track": (200, _SP_TRACK, {}), "embed/album": (200, _SP_ALBUM, {}),
                 "oembed": (200, json.dumps({"title": "Never Gonna"}), {})}

    def anfrage(url, **_kw):
        return next(a for teil, a in antworten.items() if teil in url)
    with mock.patch.object(arzt, "anfrage", anfrage), \
            contextlib.redirect_stdout(io.StringIO()):
        arzt.spotify_ohne_keys_pruefen()
    text = "\n".join(arzt.zeilen)
    assert "Rick Astley - Never Gonna Give You Up (3:33)" in text, text
    assert "Album ohne Keys: 5 Songs" in text and arzt.probleme == []
    arzt = tools_musik_check.MusikCheck()
    antworten = {"embed/": (200, "<html>neu gebaut</html>", {}), "oembed": (404, "", {})}
    with mock.patch.object(arzt, "anfrage", anfrage), \
            contextlib.redirect_stdout(io.StringIO()):
        arzt.spotify_ohne_keys_pruefen()
    assert any("umgebaut" in p[0] for p in arzt.probleme), arzt.probleme


def test_playlists_geruest_hat_die_schnittstelle():
    """bot.py und music.py rufen diese Namen - es muss sie geben."""
    for name in ("setup", "is_enabled", "handle", "on_message_passive", "tick",
                 "offene_sichern"):
        assert callable(getattr(playlists, name, None)), name
    assert playlists.DYNAMISCHE_KNOEPFE == (playlists.PlaylistKnopf,)
    assert playlists.HANDLED is basis.HANDLED
    for name in ("eintraege", "spotify_track_ohne_keys", "spotify_liste_ohne_keys"):
        assert callable(getattr(musikquellen, name, None)), name


def test_setup_legt_den_speicher_an_und_ist_abschaltbar():
    pl = playlists.instance
    alt = dict(vars(pl))
    try:
        with mock.patch.dict(os.environ, {"PLAYLISTS_ENABLED": "0"}):
            assert pl.setup() is False
        assert pl.setup() is True and pl.is_enabled()
        assert isinstance(pl._store.data.get("guilds"), dict)
        assert pl._store.path.name == "playlists.json"
    finally:
        vars(pl).clear()
        vars(pl).update(alt)


if __name__ == "__main__":
    run(globals())
