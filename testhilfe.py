"""Gemeinsame Grundlage aller Testdateien - Datenordner, Attrappen, Helfer.

Frueher stand das alles im Kopf von test_games_logic.py, zusammen mit 333
Tests in einer Datei mit 14.900 Zeilen. Beim Aufteilen in Themendateien darf
dieser Kopf NICHT mitkopiert werden: er biegt DATA_DIR auf einen frischen
Temp-Ordner um, und store.DATA_DIR wird beim BAU jedes JsonStore gelesen.
Zwanzig Kopien haetten zwanzig Ordner angelegt, und Stores, die vorher
entstanden sind, zeigten danach woandershin.

Also steht er genau einmal hier, und jede Testdatei importiert ihn als
allererste Zeile. Kein test_*.py fasst os.environ oder store.DATA_DIR selbst an -
test_tests_fassen_die_echten_daten_nicht_an haelt das wach.
"""

import asyncio
import inspect
import io
import json
import os
import random
import re
import shlex
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

import store                                    # noqa: E402


import admin

import casino

import cmdnorm

import economy

import floaktie

import gehirn

import luxus

import render

import words



# --- Ende-zu-Ende-Rauchtest -------------------------------------------------
# Die Nachrichten-Attrappe liegt in werkzeug/attrappe.py - dort holt sie sich
# auch das Inventar (werkzeug/inventar.py), das jedes Modul fragt, auf welche
# Woerter es reagiert. Eine Attrappe, zwei Nutzer, eine Datei.
from werkzeug.attrappe import RauchKanal as _RauchKanal          # noqa: E402

from werkzeug.attrappe import rauch_nachricht as _rauch_nachricht  # noqa: E402



# --- Admin-Befehle (nur Besitzer) -----------------------------------------------------
def _fake_msg(uid, content):
    return SimpleNamespace(
        author=SimpleNamespace(id=uid, bot=False, display_name="Tester"),
        content=content, mentions=[], guild=None)



class _FakeStore:
    """Minimaler JsonStore-Ersatz fuer die Tests (kein Datei-IO).

    Muss ALLES koennen, was store.JsonStore oeffentlich anbietet - sonst faellt
    eine Attrappe erst auf, wenn der echte Store eine Methode dazubekommt und
    elf Tests gleichzeitig mit AttributeError umkippen. Genau das ist beim
    Einbau von save_soon passiert. test_attrappe_kann_alles_was_der_store_kann
    haelt die beiden ab jetzt zusammen.
    """

    def __init__(self, data):
        self.data = data
        self.gespeichert = 0
        self.angemeldet = 0

    async def save(self):
        self.gespeichert += 1
        return True

    def save_soon(self):
        self.angemeldet += 1

    async def flush_now(self):
        self.angemeldet = 0
        return await self.save()



def _embed_text(antwort):
    """Alles Lesbare aus einer Bot-Antwort (Embed ODER Text) als ein String.
    Viele Antworten sind von Fliesstext auf Embeds umgestellt worden - die Tests
    pruefen den INHALT, nicht die Verpackung."""
    import discord
    if not isinstance(antwort, discord.Embed):
        return str(antwort)
    teile = [antwort.title or "", antwort.description or ""]
    for f in antwort.fields:
        teile.append(f.name or "")
        teile.append(f.value or "")
    if antwort.footer is not None:
        teile.append(antwort.footer.text or "")
    return "\n".join(teile)



def _with_economy(coins_by_uid=None):
    """Aktiviert economy mit Fake-Store; gibt (restore_fn) zurueck."""
    alt = (economy.instance._store, economy.instance._enabled)
    economy.instance._store = _FakeStore({"users": {}})
    economy.instance._enabled = True
    for uid, coins in (coins_by_uid or {}).items():
        economy.instance._profile(uid)["coins"] = coins

    def restore():
        economy.instance._store, economy.instance._enabled = alt
    return restore



class _FakeChannel:
    """Kanal-Attrappe: merkt sich, was Flo gesendet hat."""

    def __init__(self, cid=99):
        self.id = cid
        self.sent = []

    async def send(self, content=None, embed=None, view=None, embeds=None, **_kw):
        # embeds (Plural) MUSS mit erfasst werden: Module, die mehrere Embeds
        # auf einmal schicken (Profil-Lookup), waren hier sonst unsichtbar.
        self.sent.append({"content": content, "embed": embed, "view": view,
                          "embeds": embeds or ([embed] if embed is not None else [])})
        return SimpleNamespace(id=len(self.sent), edit=self._edit, channel=self)

    async def _edit(self, **_kw):
        return None

    async def fetch_message(self, _mid):
        raise RuntimeError("keine echte Nachricht im Test")



def _giveaway_msg(host=1, text="", channel=None):
    """Nachrichten-Attrappe fuer den Giveaway-Assistenten."""
    ch = channel or _GW_CHANNEL
    return SimpleNamespace(
        content=text, channel=ch,
        author=SimpleNamespace(id=host, bot=False, display_name=f"User{host}",
                               display_avatar=SimpleNamespace(url="http://x/y.png")),
        guild=SimpleNamespace(id=1))



def _giveaway_setup(coins_by_uid):
    """giveaway + economy mit Fake-Stores. Rueckgabe: (restore, gw)"""
    import giveaway
    restore_eco = _with_economy(coins_by_uid)
    gw = giveaway.instance
    alt = (gw._store, gw._enabled, dict(gw._wizards), gw._client)
    gw._store = _FakeStore({"active": {}, "next_id": 1, "done": []})
    gw._enabled = True
    gw._wizards = {}
    gw._client = None
    # WICHTIG: _protect macht 'import bot' - das wuerde im Test alle setup()s neu
    # fahren und die Fake-Stores (und damit die Kontostaende) austauschen.
    alt_protect = gw._protect
    # Signatur wie das Original (das kennt 'slot' seit jeher) - eine zu enge
    # Attrappe faellt sonst um, sobald der Aufrufer den Slot mitgibt.
    gw._protect = lambda _msg, **_k: None

    def restore():
        gw._store, gw._enabled, gw._wizards, gw._client = alt
        gw._protect = alt_protect
        restore_eco()
    return restore, gw



def _schulden_setup(coins_by_uid=None):
    """schulden + economy mit Fake-Stores. Rueckgabe: (restore, sch)"""
    import schulden
    restore_eco = _with_economy(coins_by_uid or {})
    sch = schulden.instance
    alt = (sch._store, sch._enabled, sch.buch._store, sch.buch._posten)
    sch._store = _FakeStore({"posten": [], "next_id": 1, "score": {}, "stats": {},
                             "archiv": {}, "pairs": {}})
    sch.buch.laden(sch._store)
    sch._enabled = True

    def restore():
        (sch._store, sch._enabled, sch.buch._store, sch.buch._posten) = alt
        restore_eco()
    return restore, sch



def _schuld(sch, glaeubiger, schuldner, betrag, **kw):
    """Legt einen Posten direkt an (die Zustimmung ist woanders getestet)."""
    return sch.buch.anlegen(glaeubiger, schuldner, betrag, **kw)



# --- Mehrere Server ---------------------------------------------------------
def _cfg_frisch():
    """guildcfg mit leerem Speicher - gibt eine Aufraeum-Funktion zurueck."""
    import ai
    import guildcfg
    alt = (guildcfg.instance._enabled, guildcfg.instance._store,
           guildcfg.instance._owner_id)
    guildcfg.instance._enabled = True
    guildcfg.instance._store = _FakeStore({"guilds": {}})
    guildcfg.instance._owner_id = 0
    # Den Praefix-Cache in ai mitleeren. Im Betrieb macht das der Hoerer
    # (guildcfg.horcht_auf("praefix", ai.praefix_geaendert)); hier wird der
    # Speicher aber direkt getauscht, ohne jemandem Bescheid zu sagen. Ohne das
    # gewinnt ein Regex, den ein frueherer Test fuer denselben Server gebaut
    # hat - in gemischter Reihenfolge reproduzierbar, alphabetisch nie.
    ai.instance._RE_CACHE.clear()
    # Den Hoerer anmelden, den sonst guildcfg.setup() anmeldet. Der Helfer
    # setzt _store/_enabled direkt und ruft setup() bewusst NICHT - ohne diese
    # Zeile wirkt eine Praefix-Aenderung im Test nicht sofort, weil niemand den
    # Regex-Cache leert. Doppelte Anmeldungen ignoriert horcht_auf.
    guildcfg.horcht_auf("praefix", ai.praefix_geaendert)

    def zurueck():
        (guildcfg.instance._enabled, guildcfg.instance._store,
         guildcfg.instance._owner_id) = alt
        ai.instance._RE_CACHE.clear()
    return guildcfg, zurueck



# --- Profil-Lookup ('Flo check @wer') ---------------------------------------
class _FakeAsset:
    """Discord-Asset-Ersatz: kennt Groesse, Format und ob es animiert ist."""

    def __init__(self, url="https://cdn.discordapp.com/avatars/1/abc.webp",
                 animiert=False):
        self._url, self._animiert = url, animiert

    @property
    def url(self):
        return self._url

    def is_animated(self):
        return self._animiert

    def with_size(self, px):
        trenner = "&" if "?" in self._url else "?"
        return _FakeAsset(f"{self._url}{trenner}size={px}", self._animiert)

    def with_format(self, fmt):
        kopf = self._url.split("?")[0].rsplit(".", 1)[0]
        schwanz = self._url.split("?", 1)[1] if "?" in self._url else ""
        return _FakeAsset(f"{kopf}.{fmt}" + (f"?{schwanz}" if schwanz else ""),
                          self._animiert)



def _fake_person(uid=123456789012345678, *, name="secoolio", global_name="Secoolio",
                 nick=None, member=True, bot=False, avatar=True, animiert=False,
                 banner=False, server_avatar=False, flags=None, guild=None):
    """Ein Member (mit guild) oder ein blosser User (ohne)."""
    from datetime import datetime, timezone
    p = SimpleNamespace(
        id=uid, name=name, global_name=global_name, discriminator="0",
        display_name=nick or global_name or name, bot=bot, system=False,
        mention=f"<@{uid}>", nick=nick,
        created_at=datetime(2019, 4, 20, tzinfo=timezone.utc),
        avatar=_FakeAsset(animiert=animiert) if avatar else None,
        display_avatar=_FakeAsset(animiert=animiert),
        banner=_FakeAsset("https://cdn.discordapp.com/banners/1/b.webp") if banner else None,
        accent_colour=None, avatar_decoration=None, primary_guild=None,
        public_flags=flags or SimpleNamespace(),
        colour=SimpleNamespace(value=0),
    )
    if member:
        p.guild = guild or SimpleNamespace(id=999, name="Testserver", members=[])
        p.joined_at = datetime(2021, 6, 1, tzinfo=timezone.utc)
        p.premium_since = None
        p.roles = []
        p.guild_avatar = _FakeAsset("https://cdn.discordapp.com/guilds/9/u/1/a.webp") \
            if server_avatar else None
        p.timed_out_until = None
        p.is_timed_out = lambda: False
        p.pending = False
    return p



def _profil_frisch():
    """profil-Modul mit leerem Speicher; gibt eine Aufraeum-Funktion zurueck."""
    import profil
    alt = (profil.instance._enabled, profil.instance._store,
           dict(profil.instance._voll), dict(profil.instance._cooldown))
    profil.instance._enabled = True
    profil.instance._store = _FakeStore({"users": {}, "seit": 1_700_000_000})
    profil.instance._voll = {}
    profil.instance._cooldown = {}
    profil.instance._dirty = False

    def zurueck():
        (profil.instance._enabled, profil.instance._store,
         profil.instance._voll, profil.instance._cooldown) = alt
    return profil, zurueck



# --- Musik: Zuverlaessigkeit (der gemeldete Fehler) -------------------------
class _StallVoice:
    """Voice-Client-Attrappe mit der Semantik von discord.py 2.7.1.

    Kann den FFMPEG-STALL nachstellen: is_playing() meldet weiter True, der
    Block-Zaehler des Players (AudioPlayer.loops) steht aber still - genau der
    Zustand, in dem der Bot 'verbunden und spielend' aussieht, waehrend kein
    Ton mehr fliesst."""

    def __init__(self):
        self.spielt = False
        self.pausiert_ = False
        self.frames = 0          # wandert normalerweise mit jedem Block hoch
        self.stall = False       # True = Zaehler steht (kein Ton mehr)
        self.play_calls = []
        self.stops = 0
        self._player = SimpleNamespace(loops=0)

    # -- discord.py-API --
    def is_connected(self):
        return True

    def is_playing(self):
        return self.spielt and not self.pausiert_

    def is_paused(self):
        return self.pausiert_

    def play(self, _src, after=None):
        if self.spielt:
            import discord
            raise discord.ClientException("Already playing audio.")
        self.spielt = True
        self.stall = False
        self.play_calls.append(after)

    def stop(self):
        self.spielt = False
        self.stops += 1

    def pause(self):
        self.pausiert_ = True

    def resume(self):
        self.pausiert_ = False

    @property
    def channel(self):
        return SimpleNamespace(id=42)

    # -- Test-Steuerung --
    def takt(self, n=1):
        """Laesst n Bloecke laufen (oder eben nicht, wenn stall gesetzt ist)."""
        if self.spielt and not self.pausiert_ and not self.stall:
            self.frames += n
            self._player.loops = self.frames



def _musik_umgebung():
    """Patcht FFmpeg/Panels weg. Rueckgabe: (player, voice, aufraeumen)."""
    import discord
    import music
    alt = (discord.FFmpegPCMAudio, discord.PCMVolumeTransformer,
           music._send_panel, music._retire_panel, music._resolve_track)
    discord.FFmpegPCMAudio = lambda *a, **k: SimpleNamespace(cleanup=lambda: None)
    discord.PCMVolumeTransformer = lambda src, volume=1.0: src

    async def nix(*a, **k):
        return None

    music._send_panel = nix
    music._retire_panel = nix

    # Aufloesen wird AUSDRUECKLICH stillgelegt: der Track kommt unveraendert
    # zurueck. Vorher hat der Helfer _resolve_track zwar gesichert, aber nie
    # gesetzt - er erbte also, was ein frueherer Test hinterlassen hatte. Lief
    # zufaellig ein echtes (oder gefaketes) Aufloesen, wurde player.current
    # durch ein ANDERES Track-Objekt ersetzt und die Identitaetspruefung
    # 'player.current is lang' fiel um. In gemischter Reihenfolge war das
    # reproduzierbar, alphabetisch nie.
    async def unveraendert(track):
        return track

    music._resolve_track = unveraendert

    player = music.GuildPlayer(loop=asyncio.get_event_loop_policy().new_event_loop())
    voice = _StallVoice()
    player.voice = voice
    player.active_channel_id = 42

    def aufraeumen():
        (discord.FFmpegPCMAudio, discord.PCMVolumeTransformer,
         music._send_panel, music._retire_panel, music._resolve_track) = alt
        player.loop.close()

    return player, voice, aufraeumen



def _track(titel="A", url="http://stream/a"):
    import music
    return music.Track(title=titel, stream_url=url, query=f"ytsearch1:{titel}")



def _VoiceChannelStub():
    """Echter discord.VoiceChannel ohne __init__ - heal() prueft per isinstance,
    ein SimpleNamespace wuerde dort stillschweigend abgewiesen."""
    import discord
    ch = object.__new__(discord.VoiceChannel)
    ch.id = 42
    ch.name = "Musik"
    return ch



# ---------------------------------------------------------------------------
# BotSicht: Discord aus Flos Blickwinkel (Web-Panel)
# ---------------------------------------------------------------------------
def _botsicht_umgebung():
    """Baut eine komplette Flo-Welt aus Attrappen: ein Server, ein offener und
    ein gesperrter Textkanal, ein Sprachkanal, zwei Leute, zwei Nachrichten.

    Die Kanaele sind MagicMock MIT spec - das ist hier kein Selbstzweck:
    _api_sicht_channels entscheidet per isinstance, was Text- und was
    Sprachkanal ist, und nur ein spec-Mock besteht diese Pruefung."""
    import unittest.mock as mock
    import discord
    import webpanel

    def rechte(**kw):
        p = mock.MagicMock(spec=discord.Permissions)
        for k in ("read_messages", "read_message_history", "send_messages",
                  "attach_files", "embed_links", "manage_messages", "add_reactions"):
            setattr(p, k, kw.get(k, True))
        return p

    def person(uid, name, bot=False):
        u = mock.MagicMock(spec=discord.Member)
        u.id, u.name, u.display_name, u.bot = uid, name, name, bot
        u.display_avatar = SimpleNamespace(url=f"https://cdn.test/{uid}.png")
        u.color = discord.Colour(0x7CA6FF)
        u.roles, u.status, u.voice = [], "online", None
        return u

    def nachricht(mid, text, autor, kanal, guild, t):
        m = mock.MagicMock(spec=discord.Message)
        m.id, m.content, m.author = mid, text, autor
        m.channel, m.guild = kanal, guild
        m.created_at = SimpleNamespace(timestamp=lambda t=t: t)
        m.edited_at, m.pinned = None, False
        m.attachments, m.embeds, m.reactions, m.reference = [], [], [], None
        m.type = discord.MessageType.default
        return m

    guild = mock.MagicMock(spec=discord.Guild)
    guild.id, guild.name, guild.icon = 10, "Testserver", None
    guild.member_count, guild.voice_client = 120, None
    alice, flo = person(1, "Alice"), person(99, "Flo", bot=True)
    guild.members, guild.me = [alice, flo], flo

    offen = mock.MagicMock(spec=discord.TextChannel)
    offen.id, offen.name, offen.topic, offen.position = 100, "allgemein", "Alles", 0
    offen.category, offen.guild = None, guild
    offen.is_nsfw = lambda: False
    offen.permissions_for = lambda _m: rechte()

    zu = mock.MagicMock(spec=discord.TextChannel)
    zu.id, zu.name, zu.topic, zu.position = 101, "geheim", None, 1
    zu.category, zu.guild = None, guild
    zu.is_nsfw = lambda: False
    zu.permissions_for = lambda _m: rechte(read_messages=False, read_message_history=False)

    voice = mock.MagicMock(spec=discord.VoiceChannel)
    voice.id, voice.name, voice.position = 102, "Lounge", 0
    voice.category, voice.guild, voice.members = None, guild, [alice]
    voice.permissions_for = lambda _m: rechte()
    guild.channels = [offen, zu, voice]

    # history() liefert NEUESTE zuerst - genau wie das Original.
    verlauf = [nachricht(9002, "zweite <@1> hi", alice, offen, guild, 1700000100.0),
               nachricht(9001, "erste `code`", flo, offen, guild, 1700000000.0)]

    async def hist(**kw):
        for m in verlauf[:kw.get("limit", 50)]:
            yield m
    offen.history = lambda **kw: hist(**kw)

    gesendet = []

    async def send(text, **kw):
        gesendet.append((text, kw))
        return nachricht(9100, text, flo, offen, guild, 1700000200.0)
    offen.send = send

    wp = webpanel.WebPanel()
    wp._enabled, wp._auth = True, False
    wp._client = SimpleNamespace(
        guilds=[guild], user=flo, latency=0.042,
        intents=SimpleNamespace(members=False, message_content=True,
                                presences=False, voice_states=True),
        get_guild=lambda gid: guild if gid == 10 else None,
        get_channel=lambda cid: {100: offen, 101: zu, 102: voice}.get(cid),
        is_closed=lambda: False)
    return wp, gesendet, offen, nachricht(9500, "live", alice, offen, guild, 1700000300.0)



# ---------------------------------------------------------------------------
# Arbeit: Schichten, Wordle und das Wort des Tages
# ---------------------------------------------------------------------------
def _arbeit_frisch(coins=None):
    """arbeit mit leerem Store und aktiver Economy. Gibt (modul, restore)."""
    import arbeit
    # bot ZUERST importieren, und zwar VOR dem Store-Tausch unten.
    # Grund: eine laufende Schicht meldet sich per lazy 'import bot' beim
    # Auto-Loesch-Schutz an. Ist bot in diesem Prozess noch nie importiert
    # worden, laeuft dabei bot.py komplett durch - samt 'ARBEIT_ENABLED =
    # arbeit.setup()', und das legt einen FRISCHEN Store an. Der Test haette
    # danach in einen Topf geschrieben, den keiner mehr liest.
    # (Im Betrieb kann das nicht passieren, siehe den sys.modules-Alias in
    # bot.py - dort ist bot beim ersten lazy Import laengst geladen.)
    import bot                                                    # noqa: F401
    restore_eco = _with_economy(coins or {1: 0, 2: 0})
    alt = (arbeit.instance._store, arbeit.instance._enabled)
    arbeit.instance._store = _FakeStore({"nutzer": {}, "tag": {}})
    arbeit.instance._enabled = True

    def restore():
        arbeit.instance._store, arbeit.instance._enabled = alt
        restore_eco()
    return arbeit, restore



def _karriere_durchspielen(arbeit, A, uid, salat):
    stufen_bei = {}
    for i in range(1, 13):
        _betrag, info = A.abrechnen(uid, salat, 1.0)
        prof = A._nutzer(uid)
        assert prof["geschafft"] == i, (i, prof["geschafft"])
        assert prof["serie"] == i
        if info.get("aufgestiegen"):
            stufen_bei[i] = info["stufe"].titel
    # Genau EIN Aufstieg in den ersten zwoelf, und zwar bei zehn.
    assert stufen_bei == {10: "Aushilfe"}, stufen_bei
    # Die Serie ist bei 50 % gedeckelt (nach zehn Siegen) und laeuft nicht weiter.
    assert A._serie_bonus(A._nutzer(uid)) == arbeit.SERIE_MAX

    # Verlieren: die Serie ist weg, die Karriere NICHT.
    vorher = dict(A._nutzer(uid))
    A.abrechnen(uid, salat, 0.0)
    prof = A._nutzer(uid)
    assert prof["serie"] == 0, "Serie muss beim Verlieren zurueckgesetzt werden"
    assert prof["geschafft"] == vorher["geschafft"], (
        "die Karriere darf niemals rueckwaerts gehen")
    assert arbeit.stufe_fuer(prof["geschafft"]).titel == "Aushilfe"

    # Spass-Wordle zaehlt bewusst NICHT fuer die Karriere.
    vorher = dict(A._nutzer(uid))
    A.abrechnen(uid, arbeit.SPASS, 1.0)
    prof = A._nutzer(uid)
    assert prof["geschafft"] == vorher["geschafft"]
    assert prof["serie"] == vorher["serie"]
    assert prof.get("spass_siege") == 1 and prof.get("spass_gespielt") == 1

    # Die Schwellen selbst - damit sie nicht unbemerkt verrutschen.
    assert [(st.ab, st.titel) for st in arbeit.STUFEN] == [
        (0, "Praktikant"), (10, "Aushilfe"), (30, "Facharbeiter"),
        (75, "Vorarbeiter"), (150, "Schichtleiter"), (300, "Meister"),
        (600, "Werksleiter")]
    for n, erwartet in ((0, "Praktikant"), (9, "Praktikant"), (10, "Aushilfe"),
                        (29, "Aushilfe"), (30, "Facharbeiter"), (599, "Meister"),
                        (600, "Werksleiter"), (9999, "Werksleiter")):
        assert arbeit.stufe_fuer(n).titel == erwartet, n
    assert arbeit.naechste_stufe(600) is None



# --- KI-Ausfaelle: jede Ursache eigen behandeln -----------------------------
class _KiAntwort:
    """Antwort des Anbieters, so wie das openai-Paket sie liefert."""

    def __init__(self, text="ok"):
        nachricht = SimpleNamespace(content=text, tool_calls=None)
        self.choices = [SimpleNamespace(message=nachricht)]



class _KiFehler(Exception):
    """Fehler des Anbieters mit HTTP-Status und Wortlaut - genau die zwei Dinge,
    an denen ai._einordnen() sich orientiert."""

    def __init__(self, status, text=""):
        self.status_code = status
        self.response = SimpleNamespace(status_code=status, text=text)
        super().__init__(text or f"HTTP {status}")



class _KiAnbieter:
    """Falscher LLM-Anbieter: arbeitet eine vorgegebene Folge ab und schreibt
    mit, mit welchem Modell und welcher Signatur er angesprochen wurde."""

    def __init__(self, folge, modelle=(), signatur=""):
        self._folge = list(folge)
        self._modelle = list(modelle)
        self.signatur = signatur
        self.modelle_gefragt = 0
        self.aufrufe = []          # je Aufruf das benutzte Modell
        self.letzte_kwargs = {}    # womit zuletzt aufgerufen wurde
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._models)

    async def _create(self, **kw):
        self.aufrufe.append(kw.get("model"))
        self.letzte_kwargs = dict(kw)
        naechste = self._folge.pop(0) if self._folge else _KiAntwort()
        if isinstance(naechste, Exception):
            raise naechste
        return naechste

    async def _models(self):
        self.modelle_gefragt += 1
        return SimpleNamespace(data=[SimpleNamespace(id=m) for m in self._modelle])



def _ki_frisch(folge, modelle=(), modell="altes-modell-70b"):
    """Eine FloAI-Instanz mit falschem Anbieter und ohne Wartezeiten."""
    import ai
    flo = ai.FloAI()
    flo._model = modell
    flo._vision_model = "altes-vision-modell"
    flo._api_key = "gsk_test"
    flo._basis_url = "https://api.example.invalid/v1"
    flo.WARTEN = (0.0, 0.0, 0.0)     # Tests sollen nicht wirklich schlafen
    anbieter = _KiAnbieter(folge, modelle)
    flo._client = anbieter
    return flo, anbieter



# --- Die Aerzte (tools_*_check.py) ------------------------------------------
class _FalscherAnbieter:
    """Spielt einen LLM-Anbieter samt Cloudflare davor. Kein echtes Netz."""

    def __init__(self, fall="ok",
                 modelle=("openai/gpt-oss-120b", "qwen/qwen3.6-27b")):
        import http.server
        import threading
        self.fall = fall
        self.modelle = list(modelle)
        aussen = self

        class Griff(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _raus(self, code, text, typ="application/json"):
                roh = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", typ)
                if code == 403 and aussen.fall.startswith("cf"):
                    self.send_header("cf-ray", "abc123-FRA")
                self.send_header("Content-Length", str(len(roh)))
                self.end_headers()
                self.wfile.write(roh)

            def _antwort(self):
                f = aussen.fall
                ua = self.headers.get("User-Agent", "")
                if f == "cf_signatur" and "curl" in ua:
                    f = "ok"
                if f == "cf_ip" or f == "cf_signatur":
                    return self._raus(403, "<html>error code: 1010</html>", "text/html")
                if f == "ok":
                    if "/models" in self.path:
                        return self._raus(200, json.dumps({"data": [
                            {"id": m} for m in aussen.modelle]}))
                    return self._raus(200, json.dumps(
                        {"choices": [{"message": {"content": "ok"}}]}))
                code = {"401": 401, "404": 404, "429": 429, "500": 503}[f]
                return self._raus(code, json.dumps({"error": {"message": "kaputt"}}))

            do_GET = do_POST = _antwort

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Griff)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.server.shutdown()



def _ki_arzt_lauf(anbieter, schluessel="gsk_streng_geheim_1234567890"):
    """Laesst den KI-Arzt gegen den falschen Anbieter laufen. Gibt (text, arzt)."""
    import tools_ki_check
    merker = {k: os.environ.get(k) for k in
              ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_USER_AGENT",
               "HTTPS_PROXY", "https_proxy")}
    os.environ["LLM_BASE_URL"] = anbieter.url
    os.environ["LLM_API_KEY"] = schluessel
    os.environ.pop("LLM_USER_AGENT", None)
    for v in ("HTTPS_PROXY", "https_proxy"):
        os.environ.pop(v, None)
    puffer = io.StringIO()
    alt_aus = sys.stdout
    sys.stdout = puffer
    try:
        arzt = tools_ki_check.KiCheck()
        arzt.lauf()
    finally:
        sys.stdout = alt_aus
        for k, v in merker.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return re.sub(r"\x1b\[[0-9;]*m", "", puffer.getvalue()), arzt



def _gehirn_frisch():
    """gehirn mit Fake-Store. Gibt (modul, restore)."""
    import gehirn
    g = gehirn.instance
    alt = (g._store, g._enabled)
    g._store = _FakeStore({"guilds": {}})
    g._enabled = True

    def restore():
        g._store, g._enabled = alt
    return gehirn, restore



def _gehirn_msg(uid, text, gid=77, name="Anna", bot=False):
    return SimpleNamespace(
        guild=SimpleNamespace(id=gid),
        author=SimpleNamespace(id=uid, bot=bot, display_name=name),
        content=text, mentions=[])



def _verlauf_track(titel, url="", wer="", dauer=None, query=""):
    return SimpleNamespace(title=titel, webpage_url=url, requested_by=wer,
                           duration=dauer, query=query)



def _verlauf_frisch():
    """music mit leerem Verlaufs-Store. Gibt (modul, restore)."""
    import music
    m = music.instance
    alt = (m._store, m._enabled)
    m._store = _FakeStore({"guilds": {}})
    m._enabled = True

    def restore():
        m._store, m._enabled = alt
    return music, restore



def _als_coro(wert):
    """Kleiner Helfer: macht aus einem Wert etwas Awaitbares."""
    async def lauf():
        return wert
    return lauf()


# ---------------------------------------------------------------------------
# ZUERST den Datenordner umbiegen - VOR jedem Modul-Import, denn store.DATA_DIR
# wird beim Import festgelegt und jeder JsonStore haengt daran.
#
# Ohne das schreiben die Tests in die ECHTEN Daten. Nachgemessen: ein einziger
# Lauf legte in data/economy.json ein Testkonto an und schrieb in
# data/floaktie.json "holdings": {"1": 5} samt neuem Kurs und History. Die
# README schickt einen zum Testen ausdruecklich nach /opt/flobot - dort haette
# ein Testlauf also Depots und Aktienkurs des laufenden Servers veraendert.
# Zusaetzlich machte der uebriggebliebene Stand die Suite unzuverlaessig:
# test_floaktie_market ist an geerbten Kursdaten gescheitert.
# ---------------------------------------------------------------------------
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="flobot-tests-")

store.DATA_DIR = __import__("pathlib").Path(os.environ["DATA_DIR"])


# Die Kursbewegung der Aktie WUERFELT pro Minute zweimal: TICK_NOISE (Rauschen
# am Deckel) und VOL_SPREAD (echte Volatilitaet, +-80 % um den Trend). Die
# Aktien-Tests haben bisher nur TICK_NOISE genullt und damit in Wahrheit einen
# Zufallswert geprueft - test_floaktie_aktivitaet_treibt_den_kurs ist deshalb in
# 17 von 100 Laeufen grundlos gescheitert. Fuer die gesamte Suite ist die
# Volatilitaet daher AUS; wer sie braucht, schaltet sie im eigenen Test wieder
# ein (siehe test_floaktie_volatilitaet_ist_symmetrisch).
floaktie.VOL_SPREAD = 0.0



_GW_CHANNEL = _FakeChannel()



#: Eingaben, an denen Befehle erfahrungsgemaess zerbrechen. Bewusst gemein:
#: leer, Grenzwerte, falsche Zahlenformate, fremde Ziffernsysteme, Formatzeichen,
#: Einschleusungsversuche, Massen-Erwaehnungen, unsichtbare und ueberlange Texte.
_BOESE_EINGABEN = (
    "", " ", "   ", "\t", "\n",
    "0", "-1", "-999999999999", "999999999999999999999999", "1e308", "nan", "inf",
    "0.5", "1,5", "1_000", "0x10", "1e-5", "٣", "½", "²",
    "abc", "null", "None", "undefined", "%s", "{0}", "{{}}", "%%",
    "'; DROP TABLE x; --", "../../etc/passwd", "<script>alert(1)</script>",
    "@everyone", "@here", "<@0>", "<@999999999999999999>",
    "a" * 5000, "🎉" * 200, "\u200b" * 50, "\x00", "\ufeff",
    "-0", "+5", "5 5 5 5 5", "1 2 3 4 5 6 7 8 9 10 11 12",
    "999999999999 rot", "0 rot", "-100 rot", "abc rot", "100 abcdef",
    "<@123> -999999", "<@123> 0", "<@123> abc", "<@123>",
)


def run(raum=None):
    """Kleiner Runner fuer 'python test_<thema>.py'.

    Der eigentliche Weg ist lauf.py (sammelt alle Fehler, kann mischen). Das
    hier bleibt, damit eine einzelne Datei auch allein laeuft.

    Der Aufrufer gibt seinen eigenen globals()-Raum mit: sonst suchte diese
    Funktion die Tests in testhilfe statt in der Datei, die sie ruft.
    """
    raum = raum if raum is not None else globals()
    namen = sorted(n for n in raum if n.startswith("test_"))
    for name in namen:
        raum[name]()
        print(f"ok  {name}")
    print(f"\n{len(namen)} Tests bestanden.")
