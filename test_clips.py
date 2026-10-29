"""Voice-Clips (clips.py): Ansage, Mithoeren, Opt-out, Befehle, Dialog, Schnitt, Post.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

Die Welt hier ist aus Attrappen gebaut, aber an den Stellen, an denen es
zaehlt, echt: ein ECHTER discord.VoiceChannel (Unterklasse ohne __init__,
heal & Co. pruefen per isinstance), ein ECHTER voiceempfang.EmpfangsClient
(object.__new__, ohne Socket-Thread), ein ECHTER music.GuildPlayer und der
echte Empfaenger. Ende zu Ende laeuft echtes libopus, echtes PyNaCl, der echte
Schnitt mit ffmpeg - ohne libopus/ffmpeg wird nur dieser eine Test still
uebersprungen. Netz gibt es keins: Whisper und KI sind ueberall abgeklemmt.

    python test_clips.py      nur diese Datei
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _Klick, _dynamisch_klicken, _embed_text, _fake_person, _klick_im_voice,
    _rauch_nachricht)

import contextlib
import copy
from collections import deque
import logging
import math
import struct
from unittest import mock

import discord
import nacl.secret

import basis
import clips
import clipschnitt
import clipstext
import features
import guildcfg
import laufzeit
import music
import voiceempfang

#: Fuer den gemeinsamen Rauchtest (test_fundament.test_rauchproben_der_neuen_module).
RAUCHPROBEN = (
    (clips, ("clip", "clip nein", "clip ja", "clips", "clips monat", "clip status",
             "clip aus", "clip an")),
)

FLO = 4242
GID = 7700
VOICE = 880
ANNA, BOB, CEM, DORA = 501, 502, 503, 504
_SCHLUESSEL = bytes(range(32))
#: Ein Opus-Rahmen mit 20 ms "Stimme" (TOC 0xFC: CELT, 20 ms, ein Rahmen) - fuer
#: alles, was nur zaehlt und nicht dekodiert.
_STIMME = b"\xfc" + bytes(range(1, 61))


# =============================================================================
# Die Welt
# =============================================================================
class _Uhr:
    """Die Uhr fuer Puffer und Fenster (clips._uhr) - der Test stellt sie."""

    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def vor(self, sekunden):
        self.t += sekunden


class _Gesendet:
    """Eine Nachricht, die Flo geschickt hat - mit allem, was er dazu sagte."""

    _naechste = 900_000

    def __init__(self, kanal, content, kw):
        _Gesendet._naechste += 1
        self.id = _Gesendet._naechste
        self.channel = kanal
        self.content = content
        self.kw = kw
        self.view = kw.get("view")
        self.file = kw.get("file")
        self.embed = kw.get("embed")
        self.jump_url = f"https://discord.com/channels/{GID}/{kanal.id}/{self.id}"
        self.edits = []
        self.geloescht = False
        self.components = []
        if self.view is not None:
            from discord.components import _component_factory
            self.components = [_component_factory(d) for d in self.view.to_components()]

    async def edit(self, **kw):
        self.edits.append(kw)
        if "content" in kw:
            self.content = kw["content"]
        return self

    async def delete(self):
        self.geloescht = True


class _TextKanal:
    """Ein Textkanal: merkt sich alles, was Flo dort sendet."""

    def __init__(self, cid=600, *, schreiben=True, dateien=True, name="clips"):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self.gesendet = []
        self.schreiben = schreiben
        self.dateien = dateien
        self.kaputt = None

    def permissions_for(self, _wer):
        return SimpleNamespace(send_messages=self.schreiben, attach_files=self.dateien,
                               view_channel=True, read_messages=True)

    async def send(self, content=None, **kw):
        if self.kaputt is not None:
            raise self.kaputt
        msg = _Gesendet(self, content, kw)
        self.gesendet.append(msg)
        return msg

    def is_nsfw(self):
        return False


def _voice_kanal_mit_chat(cid=VOICE, leute=(), *, name="Zocken", schreiben=True,
                          dateien=True):
    """Ein ECHTER discord.VoiceChannel (ohne __init__) mit Leuten und eigenem
    Text-Chat (send, fetch_message, Rechte)."""

    class _VoiceMitChat(discord.VoiceChannel):
        @property
        def members(self):
            return list(self._leute)

        def permissions_for(self, wer):
            if getattr(wer, "_rolle", False):         # basis.leser fragt je Rolle
                return SimpleNamespace(read_messages=wer.id in self._lesen)
            return SimpleNamespace(view_channel=True, send_messages=self._schreiben,
                                   attach_files=self._dateien, read_messages=True)

        def is_nsfw(self):
            return False

        async def send(self, content=None, **kw):
            if self.kaputt is not None:
                raise self.kaputt
            msg = _Gesendet(self, content, kw)
            self.gesendet.append(msg)
            self.nachrichten[msg.id] = msg
            return msg

        async def fetch_message(self, mid):
            if mid in self.nachrichten:
                return self.nachrichten[mid]
            raise discord.NotFound(SimpleNamespace(status=404, reason="weg"), "Unknown Message")

    kanal = object.__new__(_VoiceMitChat)
    kanal.id = cid
    kanal.name = name
    kanal._leute = list(leute)
    kanal._schreiben = schreiben
    kanal._dateien = dateien
    kanal._lesen = set()
    kanal.gesendet = []
    kanal.nachrichten = {}
    kanal.kaputt = None
    kanal.nsfw = False
    return kanal


class _Zustand:
    """VoiceConnectionState-Attrappe: Schluessel, Modus, DAVE - und wer lauscht."""

    def __init__(self):
        self.secret_key = list(_SCHLUESSEL)
        self.mode = voiceempfang.MODUS
        self.dave_session = None
        self.dave_protocol_version = 0
        self.lauscher = []
        self.verbunden = True
        self.ws = None

    def add_socket_listener(self, cb):
        self.lauscher.append(cb)

    def remove_socket_listener(self, cb):
        if cb in self.lauscher:
            self.lauscher.remove(cb)

    def is_connected(self):
        return self.verbunden


class _Client(voiceempfang.EmpfangsClient):
    """Ein EmpfangsClient ohne Socket-Thread. aufwecken() zaehlt nur mit."""

    def ruestung(self, kanal):
        self._empfang_vorbereiten()
        self._connection = _Zustand()
        self.loop = None
        self._player = None
        self.channel = kanal
        self.geweckt = 0
        self.spielt_ = False
        return self

    def is_playing(self):
        return self.spielt_

    def is_paused(self):
        return False

    async def aufwecken(self):
        self.geweckt += 1
        return not self.spielt_


def _client(kanal):
    return object.__new__(_Client).ruestung(kanal)


class _Gilde:
    """Ein Server: Flo selbst (taub, wie die Musik ihn verbindet), sein
    Voice-Client, Kanaele, Leute - und jeder change_voice_state."""

    def __init__(self, gid, kanal, vc):
        self.id = gid
        self.me = SimpleNamespace(id=FLO, voice=SimpleNamespace(
            self_deaf=True, self_mute=False, deaf=False, mute=False, channel=kanal))
        self.voice_client = vc
        self.zustand = []
        self.kanaele = {kanal.id: kanal}
        self.leute = {}
        self.filesize_limit = 10 * 1024 * 1024
        self.roles = []
        self.default_role = None
        self.system_channel = None
        self.text_channels = []

    async def change_voice_state(self, *, channel, self_deaf=False, self_mute=False):
        self.zustand.append({"kanal": getattr(channel, "id", None), "self_deaf": self_deaf,
                             "self_mute": self_mute})
        self.me.voice.self_deaf = self_deaf

    def get_channel(self, cid):
        return self.kanaele.get(cid)

    def get_member(self, uid):
        return self.leute.get(uid)


def _person(uid, name, kanal=None, *, mod=False, bot=False):
    rechte = SimpleNamespace(administrator=mod, manage_guild=mod, manage_channels=mod,
                             move_members=mod, manage_messages=mod)
    return SimpleNamespace(id=uid, display_name=name, name=name.lower(), bot=bot,
                           mention=f"<@{uid}>", guild_permissions=rechte,
                           voice=SimpleNamespace(channel=kanal) if kanal is not None else None)


class _Bezug:
    pass


def _nachricht(welt, text, autor, *, erwaehnt=(), kanal=None):
    """Eine Nachricht, wie bot.on_message sie weiterreicht - mit Erwaehnungen
    im Text (basis.echte_erwaehnungen gleicht dagegen ab)."""
    kanal = kanal or welt.text
    for p in erwaehnt:
        text += f" <@{p.id}>"
    return SimpleNamespace(content=text, author=autor, guild=welt.guild, channel=kanal,
                           mentions=list(erwaehnt), id=777, reference=None,
                           to_reference=lambda **_kw: _Bezug())


@contextlib.contextmanager
def _welt(*, aufnahme=True, leute=None, verbunden=True, musik=True, transkript=False):
    """Ein Server mit Flo in einer Musik-Sitzung im Voice 'Zocken', in dem Anna
    und Bob sitzen. clips ist an (eigener Speicher), die Uhr steht, nichts geht
    ins Netz. Danach ist ALLES wie vorher - auch Register, Player, Notsignale."""
    inst = clips.instance
    alt = dict(vars(inst))
    alt_spieler = dict(music.instance._players)
    alt_register = dict(voiceempfang._REGISTER)
    alt_client = laufzeit.client
    alt_notlage = dict(basis._notlage_seit)
    alt_settle = clips.SETTLE_SEK
    alt_spaeter = clips.SPAETER_SEK
    alt_an = guildcfg.an
    original_an = guildcfg.instance.an
    try:
        frisch = clips.Clips()
        vars(inst).clear()
        vars(inst).update(vars(frisch))
        inst._store = _FakeStore(copy.deepcopy(clips._STANDARD))
        inst._enabled = True
        inst._kann_rendern = True
        uhr_ = _Uhr()
        inst._uhr = uhr_
        clips.SETTLE_SEK = 0
        clips.SPAETER_SEK = 0.01
        anna = _person(ANNA, "Anna")
        bob = _person(BOB, "Bob")
        kanal = _voice_kanal_mit_chat(leute=leute if leute is not None else [
            SimpleNamespace(id=ANNA, bot=False, voice=SimpleNamespace(self_mute=False,
                                                                       mute=False)),
            SimpleNamespace(id=BOB, bot=False, voice=SimpleNamespace(self_mute=False,
                                                                      mute=False))])
        anna.voice = SimpleNamespace(channel=kanal)
        bob.voice = SimpleNamespace(channel=kanal)
        vc = _client(kanal)
        vc._connection.verbunden = verbunden
        gilde = _Gilde(GID, kanal, vc)
        gilde.leute.update({ANNA: anna, BOB: bob})
        text = _TextKanal(cid=601, name="allgemein")
        ziel = _TextKanal(cid=guildcfg.CLIP_CHANNEL, name="clips")
        gilde.kanaele[text.id] = text
        gilde.kanaele[ziel.id] = ziel
        kanal.guild = gilde
        spieler = music.GuildPlayer(loop=None, guild_id=GID)
        if musik:
            spieler.voice = vc
            spieler.active_channel_id = kanal.id
        music.instance._players[GID] = spieler
        laufzeit.client = SimpleNamespace(
            get_guild=lambda g: gilde if g == GID else None, user=SimpleNamespace(id=FLO),
            guilds=[gilde])
        schalter = {"aufnahme": aufnahme}

        def an(gid, key):
            if key == "clip_aufnahme":
                return schalter["aufnahme"]
            return original_an(gid, key)

        guildcfg.an = an
        welt = SimpleNamespace(inst=inst, guild=gilde, kanal=kanal, vc=vc, spieler=spieler,
                               text=text, ziel=ziel, anna=anna, bob=bob, uhr=uhr_,
                               schalter=schalter)
        with mock.patch.object(clipstext.transkriber, "an", lambda: transkript), \
                mock.patch.object(clipstext.transkriber, "anbieter",
                                  lambda: "Groq" if transkript else ""):
            yield welt
    finally:
        for s in list(inst._sitzungen.values()):
            if s.empfaenger is not None:
                s.empfaenger.abhaengen()
        for task in list(inst._tasks):
            task.cancel()
        vars(inst).clear()
        vars(inst).update(alt)
        music.instance._players.clear()
        music.instance._players.update(alt_spieler)
        voiceempfang._REGISTER.clear()
        voiceempfang._REGISTER.update(alt_register)
        laufzeit.client = alt_client
        basis._notlage_seit.clear()
        basis._notlage_seit.update(alt_notlage)
        clips.SETTLE_SEK = alt_settle
        clips.SPAETER_SEK = alt_spaeter
        guildcfg.an = alt_an


def _sitzung(welt):
    return welt.inst._sitzung(GID)


def _frag(nachricht, wie=None):
    """clips.handle (oder 'wie', etwa clips.datenschutz) - und was Flo sagte:
    die Antwort selbst oder, wenn clips selbst geantwortet hat (HANDLED: wer
    aussteigt, steht nie im Befehls-Log von bot.py), das zuletzt Gesendete."""
    antwort = asyncio.run((wie or clips.handle)(nachricht))
    if antwort is clips.HANDLED:
        return nachricht.channel.gesendet[-1].content
    return antwort


def _reden(welt, uid, von, bis, *, strang=1, opus=_STIMME):
    """uid redet von 'von' bis 'bis' (Sekunden VOR jetzt) - direkt in den Puffer."""
    puffer = _sitzung(welt).puffer
    jetzt = welt.uhr()
    n = int(round((von - bis) / 0.02))
    for k in range(n):
        puffer.einfuegen(uid, strang, 100 + k, 5000 + k * 960, opus, jetzt - von + k * 0.02)


def _flo_spielt(welt, von, bis):
    puffer = _sitzung(welt).puffer
    jetzt = welt.uhr()
    for k in range(int(round((von - bis) / 0.02))):
        puffer.eigen_anhaengen(_STIMME, jetzt - von + k * 0.02)


async def _starten(welt):
    """Abgleichen und den Hoer-Task (Taubheit, Wecken) zu Ende laufen lassen."""
    await welt.inst.abgleichen(welt.guild)
    await _nebenher(welt)


async def _nebenher(welt):
    """Alles abwarten, was clips nebenher gestartet hat."""
    for _ in range(5):
        offen = [t for t in list(welt.inst._tasks) if not t.done()]
        if not offen:
            return
        await asyncio.wait(offen, timeout=5)


def _ergebnis(auftrag, *, whisper=None, sek=2.0):
    """Was ein (gefakter) Schnitt zurueckgibt."""
    return clipschnitt.Ergebnis(
        mp3=b"ID3\x03" + bytes(2000), kbps=192, dauer=auftrag.dauer,
        stimm_sek={sp.uid: sek for sp in auftrag.stimmen}, whisper=whisper or {})


@contextlib.contextmanager
def _schnitt(ergebnis=None, fehler=None):
    """clipschnitt.rendern gefakt - merkt sich jeden Auftrag."""
    auftraege = []

    def rendern(auftrag):
        auftraege.append(auftrag)
        if fehler is not None:
            raise fehler
        return (ergebnis or _ergebnis)(auftrag)

    with mock.patch.object(clipschnitt, "rendern", rendern):
        yield auftraege


async def _kein_kommentar(*_a, **_k):
    return None


# =============================================================================
# Verdrahtung
# =============================================================================
def test_clips_ist_ueberall_angemeldet():
    """Panel-Katalog, FEATURE_LOADED, Kette (samt datenschutz), Sentinel,
    dauerhafte Knoepfe, cmdnorm, Hilfe, Einstellungen, lauf.py - jede Stelle,
    die ein neues Modul braucht."""
    import bot
    import cmdnorm
    import einstellungen
    import lauf
    assert "clips" in {f["key"] for f in features.CATALOG}
    assert "clips" in bot.FEATURE_LOADED
    quelle = open("bot.py", encoding="utf-8").read()
    assert '(CLIPS_ENABLED and _on("clips"), clips.handle)' in quelle
    assert "(True, clips.datenschutz)" in quelle
    # Direkt hinter voicegags - und datenschutz direkt hinter handle.
    kette = quelle[quelle.index("for enabled, handler in ("):]
    assert (kette.index("voicegags.handle") < kette.index("clips.handle")
            < kette.index("clips.datenschutz") < kette.index("games.handle"))
    assert clips.HANDLED is basis.HANDLED
    assert "nachhaken, zitate, geburtstage, clips)" in quelle     # _HANDLED_SENTINELS
    assert clips in bot.DYNAMISCHE_MODULE
    for wort in clips._CMDS:
        assert wort in cmdnorm.KNOWN, wort
        assert cmdnorm.normalize(f"{wort} x") is None, wort
    assert "clip" in cmdnorm.ZUSTAND
    # Ein Vertipper mit Argument wird korrigiert, mit Fliesstext nicht.
    assert cmdnorm.normalize("cilp 1m") == "clip 1m"
    assert cmdnorm.normalize("cilp das war gut") is None
    assert einstellungen.ZUORDNUNG["clips"] == ("clip_channel", "clip_aufnahme")
    assert "clips" in lauf.WACHE and "test_clips" in lauf.TESTDATEIEN
    assert ("clips", "instance._store") in lauf.WACHE_TIEF
    assert ("voiceempfang", "_REGISTER") in lauf.WACHE_INHALT
    # Die Haken in bot.py.
    for stelle in ("clips.voice_state(member, before, after)", "await clips.tick(self)",
                   '("Clips", clips.herunterfahren)', "clips.ansage_behalten(m)",
                   "clips.ist_clip(m)", "clips.geloescht(payload.guild_id",
                   "CLIPS_ENABLED = clips.setup() if MUSIC_ENABLED else clips.nur_datenschutz()",
                   "if await clips.datenschutz_dm(message):",
                   "or MUSIKSTATS_ENABLED or CLIPS_ENABLED) and _ist_notlage(content)",
                   "VOICE_GAGS_ENABLED or ECONOMY_ENABLED or CLIPS_ENABLED:"):
        assert stelle in quelle, stelle


def test_clips_einstellungen_und_standard():
    """clip_channel zeigt ab Werk auf den Clip-Kanal des Heimatservers;
    Mithoeren ist nur dort von Haus aus erlaubt (nur_haupt)."""
    assert guildcfg.CLIP_CHANNEL == 1453880623893839913
    kanal = guildcfg.instance.defn("clip_channel")
    aufnahme = guildcfg.instance.defn("clip_aufnahme")
    assert kanal.typ == "channel" and kanal.fallback == guildcfg.CLIP_CHANNEL
    assert kanal.env == "CLIP_CHANNEL_ID" and kanal.gruppe == "Voice"
    assert aufnahme.typ == "an_aus" and aufnahme.nur_haupt and aufnahme.gruppe == "Voice"
    with mock.patch.object(guildcfg, "HAUPT_GUILD", 1453867645660303527):
        assert aufnahme.standard(1453867645660303527) is True
        assert aufnahme.standard(42) is False
    with mock.patch.dict(os.environ, {"CLIP_CHANNEL_ID": "123"}):
        assert kanal.standard(5) == 123


def test_clips_hilfe_zeilen_haengen_am_modul():
    """Die Voice-Seite traegt Soundboard UND Clips - jede Zeile nur mit ihrem
    Modul, die Seite schon mit einem davon."""
    import bot
    from test_fundament import _nur_geladen
    with _nur_geladen(bot, "CLIPS_ENABLED"):
        seiten = {k for k, _e, _l in bot.client._help_categories()}
        assert "voice" in seiten
        zeilen = [b for b, _ in bot._hilfe_eintraege("voice")]
        assert zeilen == ["flo clip", "flo clip 1m @wer", "flo clips", "flo clip nein · ja",
                          "flo clip an · aus"], zeilen
    with _nur_geladen(bot, "VOICE_GAGS_ENABLED"):
        zeilen = [b for b, _ in bot._hilfe_eintraege("voice")]
        assert zeilen == ["flo soundboard", "flo sound <name>", "flo sprich <text>"], zeilen
    with _nur_geladen(bot):
        assert "voice" not in {k for k, _e, _l in bot.client._help_categories()}
    assert "clip" in bot._HELP_HINTS["voice"]


def test_clips_hilfe_karte_schneidet_nichts_ab():
    """Acht Zeilen auf der Voice-Karte - rechts am Rand steht trotzdem nie Text."""
    import bot
    import render
    from PIL import Image
    from test_fundament import _nur_geladen
    with _nur_geladen(bot, "CLIPS_ENABLED", "VOICE_GAGS_ENABLED"):
        titel, farbe, _alle = bot._HELP_DATA["voice"]
        eintraege = bot._hilfe_eintraege("voice")
        assert len(eintraege) == 8
        bild = Image.open(render.help_card(titel, bot._rgb(farbe), eintraege)).convert("RGB")
    breite, hoehe = bild.size
    for y in range(92, hoehe - 24):
        for x in range(breite - 24, breite - 11):
            assert max(bild.getpixel((x, y))) < 100, (x, y)


def test_clips_knoepfe_ueberschneiden_sich_nie():
    """Jede custom_id der Ansage und eines Clips passt auf GENAU eine Vorlage
    aller dauerhaften Knoepfe - und ist hoechstens 100 Zeichen lang."""
    import bot
    alle = [bot.HilfeAuswahl]
    for modul in bot.DYNAMISCHE_MODULE:
        alle.extend(getattr(modul, "DYNAMISCHE_KNOEPFE", ()))
    assert len(set(alle)) == len(alle)
    for view in (clips.AnsageView(VOICE), clips.AnsageView(2 ** 63), clips.ClipView("ab12cd34", 7)):
        for teil in view.children:
            assert isinstance(teil, discord.ui.DynamicItem)
            assert type(teil) in clips.DYNAMISCHE_KNOEPFE
            assert len(teil.custom_id) <= 100
            passend = [k.__name__ for k in alle
                       if k.__discord_ui_compiled_template__.fullmatch(teil.custom_id)]
            assert passend == [type(teil).__name__], (teil.custom_id, passend)
    assert view.timeout is None


def test_clips_beim_import_passiert_nichts():
    """Ein frisches Clips-Objekt ist aus und antwortet nie; das Modul hat beim
    Import nichts angemeldet - weder im Empfangs-Register noch in guildcfg."""
    frisch = clips.Clips()
    assert frisch._enabled is False and frisch._store is None
    assert frisch.empfangs_klasse() is None
    assert asyncio.run(frisch.handle(_rauch_nachricht("clip"))) is None
    assert asyncio.run(frisch.datenschutz(_rauch_nachricht("clip nein"))) is None
    frisch.sitzung_geaendert(GID)                    # aus: tut nichts, wirft nichts
    assert voiceempfang.fuer(GID) is None


def test_clips_setup_ohne_empfang_oder_libopus_bleibt_aus():
    """discord.py passt nicht oder libopus fehlt: aus, mit Grund im Log. Ohne
    ffmpeg/MP3: die Befehle gehen, mitgehoert wird nicht."""
    from test_fundament import _Mitschnitt, _mit_setup
    with _Mitschnitt("dcbot.clips") as mit:
        with mock.patch.object(voiceempfang, "kompatibel", lambda: "kein hook mehr"), \
                _mit_setup(clips) as an:
            assert an is False
        with mock.patch.object(music, "_opus_da", lambda: False), _mit_setup(clips) as an:
            assert an is False
        with mock.patch.dict(os.environ, {"CLIPS_ENABLED": "0"}), _mit_setup(clips) as an:
            assert an is False
        with mock.patch.object(clipschnitt, "ffmpeg_pruefen", lambda neu=False: "ffmpeg fehlt"), \
                _mit_setup(clips) as an:
            assert an is True and clips.instance._kann_rendern is False
            nachricht = _rauch_nachricht("clip nein")
            assert asyncio.run(clips.handle(nachricht)) is clips.HANDLED
            assert "raus" in nachricht.channel.gesendet[-1][0]
    texte = " ".join(e.getMessage() for e in mit.eintraege)
    assert "kein hook mehr" in texte and "libopus" in texte and "ffmpeg fehlt" in texte


def test_clips_ausstieg_gilt_auch_wenn_clips_gar_nicht_laufen():
    """Kommen die Clips im ganzen Bot nicht hoch (aus, ohne Musik, discord.py
    passt nicht, libopus fehlt), gilt der Ausstieg trotzdem: 🙅 und 'flo clip
    nein' werden GEMERKT - laufen die Clips spaeter wieder, bleibt die Person
    raus. Und datenschutz() steht dafuer immer in der Kette."""
    from test_fundament import _Mitschnitt, _mit_setup
    import bot
    quelle = open("bot.py", encoding="utf-8").read()
    assert "(True, clips.datenschutz)" in quelle
    assert "CLIPS_ENABLED = clips.setup() if MUSIC_ENABLED else clips.nur_datenschutz()" in quelle
    @contextlib.contextmanager
    def ohne_musik():                               # bot.py, wenn die Musik aus ist
        yield clips.nur_datenschutz()

    def frischer_speicher(*_a, **_k):
        return _FakeStore(copy.deepcopy(clips._STANDARD))

    for wie in ("kompatibel", "musik"):
        alt = dict(vars(clips.instance))
        clips.instance._store = None                  # wie nach dem Start: noch nichts offen
        with _Mitschnitt("dcbot.clips"), \
                mock.patch.object(voiceempfang, "kompatibel", lambda: "kein hook mehr"), \
                mock.patch.object(clips, "JsonStore", frischer_speicher):
            kontext = _mit_setup(clips) if wie == "kompatibel" else ohne_musik()
            try:
                with kontext as an:
                    assert an is False and clips.instance.is_enabled() is False
                    nachricht = _rauch_nachricht("clip nein")
                    assert asyncio.run(clips.handle(nachricht)) is None      # Clips laufen nicht
                    assert asyncio.run(clips.datenschutz(nachricht)) is clips.HANDLED
                    assert "du bist raus" in nachricht.channel.gesendet[-1][0]
                    uid = nachricht.author.id
                    assert clips.instance.ist_raus(uid)
                    assert str(uid) in clips.instance._store.data["opt_out"]
                    klick = _Klick(CEM, gid=GID)
                    _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, clips.AnsageView(VOICE),
                                       clips.OHNE_ID, klick)
                    assert "du bist raus" in klick.protokoll[0][1]
                    assert str(CEM) in clips.instance._store.data["opt_out"], wie
            finally:
                vars(clips.instance).clear()
                vars(clips.instance).update(alt)
    assert bot.clips is clips


# =============================================================================
# Grammatik
# =============================================================================
def test_clips_grammatik():
    """Jede Form aus der Tabelle - und Gerede faellt zur KI durch."""
    with _welt() as welt:
        bot_bob = _person(BOB, "Bob", bot=True)
        flo = _person(FLO, "Flo", bot=True)
        anna = welt.anna

        def b(text, *erwaehnt):
            return welt.inst.befehl(_nachricht(welt, "flo " + text, anna, erwaehnt=erwaehnt))

        assert b("clips") == ("brett", "woche")
        assert b("clips monat") == ("brett", "monat")
        assert b("clips der woche!") == ("brett", "woche")
        assert b("clips alle") == ("brett", "alle") and b("clips top") == ("brett", "top")
        for raus in ("clip nein", "clip ohne mich", "clip raus", "clip opt-out", "clip optout",
                     "clip nicht mit mir"):
            assert b(raus) == ("raus", []), raus
        assert b("clip nein", welt.bob) == ("raus", [welt.bob])
        for rein in ("clip ja", "clip mit mir", "clip rein", "clip opt-in", "clip optin"):
            assert b(rein) == ("rein", []), rein
        for aus in ("clip aus", "clip stop", "clip stopp", "clip beenden"):
            assert b(aus) == ("aus",), aus
        for an in ("clip an", "clip ein", "clip start", "clip starten", "clip komm"):
            assert b(an) == ("an",), an
        for status in ("clip status", "clip info", "clip diagnose"):
            assert b(status) == ("status",), status
        for hilfe in ("clip hilfe", "clip help", "clip ?"):
            assert b(hilfe) == ("hilfe",), hilfe
        dauern = {
            "clip 30": 30, "clip 30s": 30, "clip 30 s": 30, "clip 30 sekunden": 30,
            "clip 90": 90, "clip 1m": 60, "clip 1 min": 60, "clip 2 minuten": 120,
            "clip 3": 180, "clip 5": 300, "clip 6": 10, "clip 0": 10,
            "clip halbe minute": 30, "clip eine minute": 60, "clip minute": 60,
            "clip fünf minuten": 300, "clip fuenf minuten": 300,
            "clip die letzten 30 sekunden bitte": 30, "clip mal kurz das jetzt eben 1m": 60,
        }
        for text, sek in dauern.items():
            assert b(text) == ("clip", sek, None), (text, b(text))
        assert b("clip") == ("clip", None, None)
        assert b("clip das mal bitte") == ("clip", None, None)
        assert b("clip 1m", welt.bob) == ("clip", 60, welt.bob)
        assert b("clip 1m nur", welt.bob) == ("clip", 60, welt.bob)
        assert b("clip 1m nur mich") == ("clip", 60, "ich")
        assert b("clip", welt.bob) == ("clip", None, welt.bob)
        # Flo selbst (die Ansprache per Erwaehnung) ist kein Ziel.
        assert b("clip 30", flo) == ("clip", 30, None)
        assert b("clip 1m", welt.bob, anna) == ("fehler", "mehrere")
        assert b("clip 1m alle", welt.bob) == ("fehler", "mehrere")
        assert b("clip 30", bot_bob) == ("fehler", "bot_ziel")
        assert b("clip 400") == ("fehler", "zu_lang")
        assert b("clip 6 minuten") == ("fehler", "zu_lang")
        for gerede in ("clip ist cool", "clips sind cool", "clip das war geil", "clip 30 40",
                       "clip 1234", "clipboard leeren", "clips 30", "clip nein danke bob",
                       "clips", "hahaha clip"):
            if gerede == "clips":
                continue
            assert b(gerede) is None, gerede
        assert welt.inst.befehl(_nachricht(welt, "hahaha ja klar", anna)) is None


def test_clips_grammatik_aussteigen_klappt_auch_hoeflich_und_vertippt():
    """Der Ausstieg ist die Einwilligung - er darf nicht an einem 'bitte',
    'danke' oder einem Buchstabendreher scheitern und bei der KI landen (die
    weiss von Clips nichts, und man glaubte sich raus). Zurueck geht es nur
    richtig geschrieben. Dazu ein paar natuerliche Laengen."""
    with _welt() as welt:
        def b(text, *erwaehnt):
            return welt.inst.befehl(_nachricht(welt, "flo " + text, welt.anna,
                                               erwaehnt=erwaehnt))

        for raus in ("clip nein danke", "clip nein bitte", "clip ohne mich bitte",
                     "clip bitte ohne mich", "clip mich bitte nicht", "clip raus bitte",
                     "clip ich will nicht", "clip nicht mich", "clip nee", "clip lieber ohne mich",
                     "cilp nein", "clpi nein", "clipp ohne mich", "cilp nein danke"):
            assert b(raus) == ("raus", []), raus
        assert b("cilp nein", welt.bob) == ("raus", [welt.bob])     # -> 'fremd_raus'
        assert b("clip ja bitte") == ("rein", []) and b("clip bitte wieder rein") == ("rein", [])
        assert b("clip aus bitte") == ("aus",) and b("clip an bitte") == ("an",)
        for nichts in ("cilp ja", "cilp das war gut", "clio nein", "cilp", "clip danke",
                       "clip nein danke bob", "clip bitte nicht"):
            assert b(nichts) is None, nichts
        for text, sek in {"clip eine halbe minute": 30, "clip ne halbe minute": 30,
                          "clip 1:30": 90, "clip 0:45": 45, "clip 1,5 min": 90,
                          "clip 1.5 minuten": 90, "clip 2,5": 150}.items():
            assert b(text) == ("clip", sek, None), (text, b(text))
        assert b("clip 5:01") == ("fehler", "zu_lang")
        # Und ueber den ganzen Weg: der Vertipper kommt an handle() vorbei.
        antwort = _frag(_nachricht(welt, "flo cilp nein danke", welt.anna))
        assert "du bist raus" in antwort and welt.inst.ist_raus(ANNA)
        assert asyncio.run(clips.handle(_nachricht(welt, "flo cilp ja", welt.anna))) is None
        with mock.patch.object(features, "is_on_in", lambda g, k: k != "clips"):
            welt.inst.rein(ANNA)
            assert "du bist raus" in _frag(_nachricht(welt, "flo clpi nein bitte", welt.anna),
                                           clips.datenschutz)


def test_clips_grammatik_mich_und_jemand_sind_zwei():
    """'mich' plus eine Erwaehnung von jemand anderem sind zwei Leute - also
    'mehrere'. Erwaehnt man sich selbst, bleibt es einer."""
    with _welt() as welt:
        def b(text, autor, *erwaehnt):
            return welt.inst.befehl(_nachricht(welt, "flo " + text, autor, erwaehnt=erwaehnt))

        assert b("clip 1m mich", welt.anna, welt.bob) == ("fehler", "mehrere")
        assert b("clip nur mich", welt.anna, welt.bob) == ("fehler", "mehrere")
        assert b("clip mich", welt.anna, welt.anna) == ("clip", None, welt.anna)
        assert b("clip 30 nur mich", welt.anna) == ("clip", 30, "ich")
        assert "Nicht drei" in asyncio.run(clips.handle(_nachricht(
            welt, "flo clip 1m mich", welt.anna, erwaehnt=[welt.bob])))


def test_clips_inventar_liest_genau_die_befehlswoerter():
    """clips steht in inventar.NICHT_PROBIEREN (es koennte in den Voice) - das
    Inventar liest seine Woerter dann nur an der Befehlsstelle. Dort stehen
    genau 'clip' und 'clips', keine Fuellwoerter wie 'alle', 'mich' oder
    'minute' (die kaemen sonst als Befehle von clips ins Inventar)."""
    with mock.patch.dict(os.environ):              # inventar setzt DATA_DIR hart
        from werkzeug import inventar
    jagd = inventar.Woerterjagd(inventar.Quelltext.hol(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "clips.py")))
    assert jagd.nur_befehlsstellen() == set(clips._CMDS)
    assert "clips" in inventar.NICHT_PROBIEREN


def test_clips_puffer_mb_aus_der_env_kippt_nie_den_start():
    """CLIP_PUFFER_MB wird beim Import gelesen - ein Tippfehler in der .env darf
    den Bot nicht umwerfen. Dann gilt die Vorgabe; Unsinn wird begrenzt."""
    mb = 1024 * 1024
    for roh, erwartet in (("", 48), ("64", 64), ("12,5", 12.5), ("abc", 48), ("-3", 48),
                          ("0", 48), ("nan", 48), ("inf", 160), ("9999", 160), ("0.1", 1)):
        with mock.patch.dict(os.environ, {"CLIP_PUFFER_MB": roh}):
            assert clips._env_mb("CLIP_PUFFER_MB", 48, 160) == int(erwartet * mb), roh
    assert mb <= clips.PUFFER_BYTES <= clips.GESAMT_BYTES


# =============================================================================
# Mithoeren: Ansage, Taubheit, Wecken
# =============================================================================
def test_clips_ansage_dann_puffer_und_ohren_auf():
    """In einer echten Musik-Sitzung: erst die Ansage im Voice-Chat (mit 🙅 und
    🔇), DANN Lauscher an, eigene Spur an, Taubheit weg, einmal wecken."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        assert s.aktiv and s.kanal_id == VOICE and s.grund == ""
        ansage = welt.kanal.gesendet[0]
        assert "Ab jetzt hör ich hier mit" in ansage.content
        assert "flo clip nein" in ansage.content.lower() and "<#" in ansage.content
        assert isinstance(ansage.view, clips.AnsageView)
        assert [k.custom_id for k in ansage.view.children] == [clips.OHNE_ID,
                                                               f"flo:clip:aus:{VOICE}"]
        assert ansage.kw["allowed_mentions"].everyone is False
        assert clips.ist_ansage(ansage) is True
        assert welt.inst._daten("ansagen")[str(VOICE)]["mid"] == ansage.id
        # Lauscher und Register
        e = voiceempfang.fuer(GID)
        assert e is s.empfaenger and e.client is welt.vc
        assert welt.vc._connection.lauscher == [e.roh]
        assert welt.vc.eigen_senke == s.puffer.eigen_anhaengen
        # Erst NACH der Ansage hoert Flo: Taubheit aus, einmal geweckt.
        assert welt.guild.zustand == [{"kanal": VOICE, "self_deaf": False, "self_mute": False}]
        assert s.taub_von_uns and welt.vc.geweckt == 1
        # Ein zweiter Abgleich aendert nichts: keine zweite Ansage, kein zweites Wecken.
        asyncio.run(_starten(welt))
        assert len(welt.kanal.gesendet) == 1 and welt.vc.geweckt == 1
        assert len(welt.guild.zustand) == 1 and welt.vc._connection.lauscher == [e.roh]


def test_clips_ohne_ansage_keine_aufnahme():
    """Darf Flo im Voice-Chat nicht schreiben (oder Discord lehnt ab): kein
    Lauscher, Flo bleibt taub, und 10 Minuten lang kein neuer Versuch."""
    with _welt() as welt:
        welt.kanal._schreiben = False
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        assert not s.aktiv and s.grund == "ansage" and voiceempfang.fuer(GID) is None
        assert welt.vc._connection.lauscher == [] and welt.guild.zustand == []
        assert welt.kanal.gesendet == []
        welt.kanal._schreiben = True                 # erst nach der Pause wieder
        asyncio.run(_starten(welt))
        assert not s.aktiv and welt.kanal.gesendet == []
        s.ansage_fehlt = (VOICE, time.monotonic() - 1)
        asyncio.run(_starten(welt))
        assert s.aktiv and len(welt.kanal.gesendet) == 1
    with _welt() as welt:
        welt.kanal.kaputt = discord.Forbidden(SimpleNamespace(status=403, reason="nein"), "nein")
        asyncio.run(_starten(welt))
        assert not _sitzung(welt).aktiv and welt.guild.zustand == []


def test_clips_ansage_pause_und_geloeschte_ansage():
    """Innerhalb von 45 Minuten keine zweite Ansage im selben Kanal - aber nur,
    solange es die alte noch gibt. Weg (geloescht) -> neu ansagen."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        erste = welt.kanal.gesendet[0]
        welt.vc._connection.verbunden = False         # Flo fliegt raus, Musik will nicht mehr
        welt.spieler.active_channel_id = None
        asyncio.run(_starten(welt))
        assert not _sitzung(welt).aktiv
        welt.vc._connection.verbunden = True          # zurueck (z. B. nach Neustart)
        welt.spieler.active_channel_id = VOICE
        asyncio.run(_starten(welt))
        assert _sitzung(welt).aktiv and welt.kanal.gesendet == [erste]
        # Jemand loescht die Ansage -> beim naechsten Start eine neue.
        welt.vc._connection.verbunden = False
        welt.spieler.active_channel_id = None
        asyncio.run(_starten(welt))
        del welt.kanal.nachrichten[erste.id]
        welt.vc._connection.verbunden = True
        welt.spieler.active_channel_id = VOICE
        asyncio.run(_starten(welt))
        assert len(welt.kanal.gesendet) == 2 and _sitzung(welt).aktiv
        # Und nach 45 Minuten auch ohne Loeschen.
        welt.inst._daten("ansagen")[str(VOICE)]["ts"] -= clips.ANSAGE_PAUSE + 1
        welt.vc._connection.verbunden = False
        welt.spieler.active_channel_id = None
        asyncio.run(_starten(welt))
        welt.vc._connection.verbunden = True
        welt.spieler.active_channel_id = VOICE
        asyncio.run(_starten(welt))
        assert len(welt.kanal.gesendet) == 3


def test_clips_ansage_weg_mitten_in_der_runde():
    """'flo lösch 50' im Voice-Chat nimmt die Ansage mit - ohne Ansage keine
    Aufnahme, auch nicht mitten in der Runde. Meldet Discord die Loeschung,
    kommt sofort eine neue; ohne Meldung findet es der Takt. Geht keine neue
    mehr, hoert Flo auf (und stellt sich taub)."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        erste = welt.kanal.gesendet[0]
        del welt.kanal.nachrichten[erste.id]

        async def melden(ids):
            clips.geloescht(GID, VOICE, ids)
            await _nebenher(welt)

        asyncio.run(melden({erste.id}))
        assert s.aktiv and len(welt.kanal.gesendet) == 2
        zweite = welt.kanal.gesendet[1]
        assert "Ab jetzt hör ich hier mit" in zweite.content and s.ansage_msg is zweite
        assert welt.inst._daten("ansagen")[str(VOICE)]["mid"] == zweite.id
        # Ohne Meldung (etwa waehrend eines Gateway-Aussetzers): der Takt sieht nach.
        del welt.kanal.nachrichten[zweite.id]
        s.ansage_neu_um -= clips.ANSAGE_FEHLT_PAUSE + 1      # die Bremse ist hier egal
        asyncio.run(welt.inst.tick(laufzeit.client))         # noch nicht faellig
        assert len(welt.kanal.gesendet) == 2
        s.ansage_geprueft -= clips.ANSAGE_NACHSEHEN + 1
        asyncio.run(welt.inst.tick(laufzeit.client))
        assert s.aktiv and len(welt.kanal.gesendet) == 3
        # Steht sie noch, passiert beim Nachsehen nichts.
        s.ansage_geprueft -= clips.ANSAGE_NACHSEHEN + 1
        asyncio.run(welt.inst.tick(laufzeit.client))
        assert s.aktiv and len(welt.kanal.gesendet) == 3
        # Darf Flo keine neue mehr posten: aufhoeren, taub, und erst mal Ruhe.
        welt.kanal._schreiben = False
        s.ansage_neu_um -= clips.ANSAGE_FEHLT_PAUSE + 1
        asyncio.run(melden({welt.kanal.gesendet[2].id}))
        assert not s.aktiv and s.grund == "ansage" and s.puffer is None
        assert welt.guild.zustand[-1]["self_deaf"] is True
        asyncio.run(_starten(welt))
        assert not s.aktiv and len(welt.kanal.gesendet) == 3


def test_clips_ansage_kein_ping_pong_mit_einem_moderations_bot():
    """Loescht ein Moderations-Bot die Ansage jedes Mal sofort wieder, postet
    Flo nicht im Sekundentakt neue in den Voice-Chat: hoechstens eine neue je
    ANSAGE_FEHLT_PAUSE - kommt die auch weg, hoert Flo auf (taub, und erst mal
    Ruhe). Nach der Pause darf es von vorn losgehen."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)

        async def wegmoderieren():
            weg = welt.kanal.gesendet[-1]
            del welt.kanal.nachrichten[weg.id]
            clips.geloescht(GID, VOICE, {weg.id})
            await _nebenher(welt)

        asyncio.run(wegmoderieren())                      # die erste: eine neue
        assert s.aktiv and len(welt.kanal.gesendet) == 2
        asyncio.run(wegmoderieren())                      # gleich wieder weg: Schluss
        assert len(welt.kanal.gesendet) == 2
        assert not s.aktiv and s.grund == "ansage_weg" and s.puffer is None
        assert welt.guild.zustand[-1]["self_deaf"] is True
        for _ in range(3):                                # und keine Ansagen-Flut
            asyncio.run(_starten(welt))
            s.ansage_geprueft -= clips.ANSAGE_NACHSEHEN + 1
            asyncio.run(welt.inst.tick(laufzeit.client))
        assert not s.aktiv and len(welt.kanal.gesendet) == 2
        # Und Flo sagt ehrlich, warum - schreiben duerfte er ja.
        assert s.grund == "ansage_weg"
        antwort = _frag(_nachricht(welt, "flo clip 30", welt.anna))
        assert "dauernd die Ansage weg" in antwort and "nicht schreiben" not in antwort
        mod = _person(DORA, "Dora", mod=True)
        emb = asyncio.run(clips.handle(_nachricht(welt, "flo clip status", mod)))
        assert "Grund: `ansage_weg`" in emb.fields[0].value, emb.fields[0].value
        # Nach der Pause: wieder eine Ansage, und wieder nur eine neue dazu.
        s.ansage_fehlt = (VOICE, time.monotonic() - 1)
        s.ansage_neu_um -= clips.ANSAGE_FEHLT_PAUSE + 1
        asyncio.run(_starten(welt))
        assert s.aktiv and len(welt.kanal.gesendet) == 3
        asyncio.run(wegmoderieren())
        assert s.aktiv and len(welt.kanal.gesendet) == 4
        asyncio.run(wegmoderieren())
        assert not s.aktiv and len(welt.kanal.gesendet) == 4


def test_clips_von_hand_geloeschter_clip_fliegt_aus_dem_index():
    """Loescht ein Mod einen Clip von Hand (nicht per 🗑️), meldet Discord das -
    der Clip fliegt aus Index und Bestenliste, statt dort 35 Tage mit totem
    Link zu stehen. Und bot.py reicht beide Meldungen weiter."""
    import bot
    from test_fundament import _nur_geladen
    with _welt() as welt:
        post, cid = _ein_clip(welt)
        welt.inst._clips_von(GID)[cid]["stimmen"] = [CEM]
        assert f"/{post.id})" in asyncio.run(clips.handle(_nachricht(
            welt, "flo clips", welt.anna))).description
        clips.geloescht(GID, welt.ziel.id, {post.id, 12345})
        assert cid not in welt.inst._clips_von(GID)
        assert f"/{post.id})" not in asyncio.run(clips.handle(_nachricht(
            welt, "flo clips", welt.anna))).description
    gemeldet = []
    einzeln = SimpleNamespace(guild_id=GID, channel_id=VOICE, message_id=5)
    viele = SimpleNamespace(guild_id=GID, channel_id=VOICE, message_ids=[6, 7])
    with mock.patch.object(clips, "geloescht", lambda *a: gemeldet.append(a)):
        with _nur_geladen(bot, "CLIPS_ENABLED"):
            asyncio.run(bot.client.on_raw_message_delete(einzeln))
            asyncio.run(bot.client.on_raw_bulk_message_delete(viele))
        with _nur_geladen(bot):
            asyncio.run(bot.client.on_raw_message_delete(einzeln))
            asyncio.run(bot.client.on_raw_bulk_message_delete(viele))
    assert gemeldet == [(GID, VOICE, {5}), (GID, VOICE, {6, 7})]


def test_clips_ansage_geschuetzt_solange_flo_zuhoert():
    """Die Ansage steht unter Loesch-Schutz, solange Flo zuhoert - auch die
    wiedergefundene nach einem Neuaufbau. Hoert er auf, wird sie freigegeben:
    in einem Aufraeum-Kanal raeumt das Auto-Loeschen sie dann mit weg, und die
    naechste Runde kuendigt neu an."""
    with _welt() as welt:
        schutz, frei = [], []
        with mock.patch.object(laufzeit, "protect_message", lambda m: schutz.append(m.id)), \
                mock.patch.object(laufzeit, "release_message",
                                  lambda m, **_kw: frei.append(m.id)):
            asyncio.run(_starten(welt))
            ansage = welt.kanal.gesendet[0]
            assert schutz == [ansage.id] and frei == []
            # Flo fliegt raus und kommt zurueck: dieselbe Ansage, wieder geschuetzt.
            welt.vc._connection.verbunden = False
            welt.spieler.active_channel_id = None
            asyncio.run(_starten(welt))
            assert frei == [ansage.id]
            welt.vc._connection.verbunden = True
            welt.spieler.active_channel_id = VOICE
            asyncio.run(_starten(welt))
            assert schutz == [ansage.id, ansage.id] and len(welt.kanal.gesendet) == 1
            asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.anna)))
            assert frei == [ansage.id, ansage.id]


def test_clips_soundboard_besuch_hoert_nie_mit():
    """Flo ist nur fuer einen Sound im Kanal (voicegags, keine Musik-Sitzung):
    keine Ansage, kein Lauscher, keine Aenderung an der Taubheit."""
    with _welt(musik=False) as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        assert not s.aktiv and s.grund == "keine_sitzung"
        assert welt.kanal.gesendet == [] and welt.vc._connection.lauscher == []
        assert welt.guild.zustand == [] and voiceempfang.fuer(GID) is None
    # Und auch nicht, solange clips aus ist (Funktion/Einwilligung/Sendepause/ffmpeg).
    for art in ("feature", "server", "sendepause", "render", "server_taub"):
        with _welt() as welt:
            patches = contextlib.ExitStack()
            if art == "feature":
                patches.enter_context(mock.patch.object(features, "is_on_in",
                                                        lambda g, k: k != "clips"))
            elif art == "server":
                welt.schalter["aufnahme"] = False
            elif art == "sendepause":
                patches.enter_context(mock.patch.object(clips, "_sendepause", lambda: True))
            elif art == "render":
                welt.inst._kann_rendern = False
            else:
                welt.guild.me.voice.deaf = True
            with patches:
                asyncio.run(_starten(welt))
            assert not _sitzung(welt).aktiv and _sitzung(welt).grund == art, art
            assert welt.kanal.gesendet == [], art


def test_clips_keine_ansage_fuer_eine_runde_auf_verdacht():
    """'flo spiel' verbindet schon, waehrend es noch sucht (und geht wieder,
    wenn nichts gefunden wird); das Radio laedt erst seinen ersten Song. Fuer
    so eine Runde auf Verdacht keine Ansage und keine offenen Ohren - sonst
    stuende im Voice-Chat 'Ab jetzt hör ich hier mit' fuer eine Runde, die es
    nie gab. Laeuft der Song, kommt die Ansage von selbst. Eine LAUFENDE Runde
    stoert ein neues 'flo spiel' nicht."""
    with _welt() as welt:
        welt.spieler._laufende_anfragen = 1                # 'flo spiel ...' sucht noch
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        assert not s.aktiv and s.grund == "vorab"
        assert welt.kanal.gesendet == [] and welt.guild.zustand == []
        assert "gerade erst angefangen" in asyncio.run(clips.handle(_nachricht(
            welt, "flo clip 30", welt.anna)))
        # Nichts gefunden: die Musik geht wieder - es gab nie eine Ansage.
        welt.spieler._laufende_anfragen = 0
        welt.spieler.active_channel_id = None
        welt.guild.voice_client = None
        asyncio.run(_starten(welt))
        assert welt.kanal.gesendet == [] and welt.guild.zustand == []
    with _welt() as welt:
        welt.spieler._laufende_anfragen = 1
        s = _sitzung(welt)

        async def lauf():
            await welt.inst.abgleichen(welt.guild)
            assert not s.aktiv
            # Gefunden: der Song startet - ohne Haken, clips sieht von selbst nach.
            welt.spieler._laufende_anfragen = 0
            welt.spieler.current = SimpleNamespace(title="Song", live=False, duration=200)
            await _nebenher(welt)

        asyncio.run(lauf())
        assert s.aktiv and len(welt.kanal.gesendet) == 1
    with _welt() as welt:
        welt.spieler._advancing = True                     # Radio: der erste Song laedt
        asyncio.run(_starten(welt))
        assert not _sitzung(welt).aktiv and welt.kanal.gesendet == []
        welt.spieler.clip_halten = True                    # 'flo clip an' will es so
        asyncio.run(_starten(welt))
        assert _sitzung(welt).aktiv
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.spieler._laufende_anfragen = 1                # neues 'flo spiel' in der Runde
        asyncio.run(_starten(welt))
        assert _sitzung(welt).aktiv and len(welt.kanal.gesendet) == 1


def test_clips_kanalwechsel_leert_und_kuendigt_neu_an():
    """Flo wird verschoben: der Puffer aus dem alten Kanal ist weg, im neuen
    Kanal gibt es erst eine Ansage."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, ANNA, 10, 1)
        alt = _sitzung(welt).puffer
        assert alt.spuren
        neu = _voice_kanal_mit_chat(cid=881, name="Lounge")
        neu.guild = welt.guild
        welt.guild.kanaele[881] = neu
        welt.vc.channel = neu
        welt.spieler.active_channel_id = 881
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        assert s.aktiv and s.kanal_id == 881 and s.puffer is not alt
        assert not alt.spuren and not s.puffer.spuren
        assert len(neu.gesendet) == 1 and "Ab jetzt" in neu.gesendet[0].content


def test_clips_musik_trennt_leert_den_puffer():
    """'flo stop' (music.disconnect) meldet clips - der Puffer ist sofort leer,
    der Lauscher ab, und Flo stellt sich wieder taub (falls noch drin)."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, ANNA, 10, 1)

        async def stop():
            welt.spieler.voice = None                # music.disconnect ohne echten Client
            await welt.spieler.disconnect()
            await _nebenher(welt)

        with mock.patch.object(music, "_retire_panel", lambda _p: _als_nichts()), \
                mock.patch.object(music, "_zustand_vergessen", lambda _g: None):
            asyncio.run(stop())
        s = _sitzung(welt)
        assert not s.aktiv and s.puffer is None and voiceempfang.fuer(GID) is None
        assert welt.vc._connection.lauscher == []
        assert welt.guild.zustand[-1]["self_deaf"] is True        # wieder taub


async def _als_nichts():
    return None


def test_clips_client_wechsel_behaelt_puffer_in_der_gnade():
    """Die Musik baut neu auf (anderer Client, gleicher Kanal): der Puffer
    bleibt GNADE_SEK, der Lauscher geht an den neuen Client. Danach: weg."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, ANNA, 10, 1)
        s = _sitzung(welt)
        puffer = s.puffer
        welt.vc._connection.verbunden = False         # Verbindung reisst
        asyncio.run(_starten(welt))
        assert s.aktiv and s.puffer is puffer and s.getrennt_seit is not None
        assert s.empfaenger.client is None and s.grund == "getrennt"
        # Neuer Client, gleicher Kanal - die Musik adoptiert ihn.
        neu = _client(welt.kanal)
        welt.guild.voice_client = neu
        welt.spieler.voice = neu
        asyncio.run(_starten(welt))
        assert s.aktiv and s.puffer is puffer and ANNA in puffer.spuren
        assert s.empfaenger.client is neu and neu._connection.lauscher == [s.empfaenger.roh]
        assert neu.eigen_senke == puffer.eigen_anhaengen and s.getrennt_seit is None
        # Nochmal weg - und diesmal laeuft die Gnade ab.
        neu._connection.verbunden = False
        asyncio.run(_starten(welt))
        s.getrennt_seit -= clips.GNADE_SEK + 1
        asyncio.run(_starten(welt))
        assert not s.aktiv and s.puffer is None and not puffer.spuren


def test_clips_nachsehen_waehrend_des_handshakes_hoert_auf():
    """Kein Ereignis meldet, wann ein Handshake fertig ist - clips sieht mit
    wachsendem Abstand nach, aber nur SPAETER_MAX Mal; dann macht der Takt
    weiter. Kein Dauerfeuer."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.vc._connection.verbunden = False
        gerufen = []
        echt = welt.inst.abgleichen

        async def zaehlen(guild):
            gerufen.append(time.monotonic())
            await echt(guild)

        with mock.patch.object(welt.inst, "abgleichen", zaehlen):
            asyncio.run(_starten(welt))
        assert len(gerufen) == clips.SPAETER_MAX + 1, gerufen
        assert welt.inst._spaeter_n[GID] == clips.SPAETER_MAX
        assert not [t for t in welt.inst._tasks if not t.done()]
        # Kommt die Verbindung zurueck, faengt die Zaehlung von vorn an.
        welt.vc._connection.verbunden = True
        asyncio.run(_starten(welt))
        assert GID not in welt.inst._spaeter_n and _sitzung(welt).aktiv


def test_clips_mikro_aus_wartet_auf_die_musik_und_stellt_taub():
    """Mikro aus, waehrend die Musik ihre Voice-Sperre haelt (Tempo-Wechsel):
    der Puffer ist SOFORT leer, das Taubstellen kommt, sobald sie frei ist."""
    with _welt() as welt:
        async def lauf():
            await _starten(welt)
            _reden(welt, ANNA, 10, 1)
            await welt.spieler._voice_lock.acquire()
            aus = asyncio.ensure_future(clips.handle(_nachricht(welt, "flo clip aus",
                                                                welt.bob)))
            await asyncio.sleep(0.3)
            leer = _sitzung(welt).puffer is None and not _sitzung(welt).aktiv
            noch_nicht = welt.guild.zustand[-1]["self_deaf"] is False
            welt.spieler._voice_lock.release()
            antwort = await aus
            return leer, noch_nicht, antwort

        leer, noch_nicht, antwort = asyncio.run(lauf())
        assert leer and noch_nicht and "Mikro aus" in antwort
        assert welt.guild.zustand[-1]["self_deaf"] is True


def test_clips_wieder_hoeren_wartet_auf_die_musik_sperre():
    """Baut die Musik gerade (ihre Voice-Sperre ist belegt), wird an der
    Taubheit nichts gedreht - erst wenn sie frei ist."""
    with _welt() as welt:
        async def lauf():
            await welt.spieler._voice_lock.acquire()
            await welt.inst.abgleichen(welt.guild)
            await asyncio.sleep(0.5)
            vorher = list(welt.guild.zustand)
            welt.spieler._voice_lock.release()
            await _nebenher(welt)
            return vorher

        vorher = asyncio.run(lauf())
        assert vorher == [] and _sitzung(welt).aktiv
        assert welt.guild.zustand == [{"kanal": VOICE, "self_deaf": False, "self_mute": False}]


def test_clips_neuer_client_bekommt_sofort_ohren():
    """Wartet das Hoeren noch auf einen ALTEN Client (die Musik baut gerade neu
    auf), bekommt der neue trotzdem sofort seins - nicht erst im naechsten Takt."""
    with _welt() as welt:
        async def lauf():
            await welt.spieler._voice_lock.acquire()      # die Musik baut gerade
            await welt.inst.abgleichen(welt.guild)        # Hoeren wartet auf die Sperre
            alt = _sitzung(welt).hoeren_task
            neu = _client(welt.kanal)                     # gleiche Runde, neuer Client
            welt.guild.voice_client = neu
            welt.spieler.voice = neu
            await welt.inst.abgleichen(welt.guild)
            welt.spieler._voice_lock.release()
            await _nebenher(welt)
            return alt, neu

        alt, neu = asyncio.run(lauf())
        s = _sitzung(welt)
        assert neu.geweckt == 1 and welt.vc.geweckt == 0
        assert welt.guild.zustand == [{"kanal": VOICE, "self_deaf": False, "self_mute": False}]
        assert alt.done() and s.empfaenger.client is neu and s.hoeren_vc is neu


def test_clips_taubstellen_wird_nachgeholt():
    """Haelt die Musik ihre Sperre laenger als HOEREN_WARTEN (Tempo-Wechsel),
    kommt das Taubstellen nach 'Mikro aus' nicht gleich durch. Vergessen wird es
    nicht: der naechste Abgleich (spaetestens der Takt) holt es nach - sonst
    saesse Flo mit offenen Ohren da, ohne zuzuhoeren."""
    with _welt() as welt:
        async def lauf():
            await _starten(welt)
            offen = welt.guild.me.voice.self_deaf is False
            await welt.spieler._voice_lock.acquire()
            with mock.patch.object(clips, "HOEREN_WARTEN", 0.3):
                antwort = await clips.handle(_nachricht(welt, "flo clip aus", welt.anna))
                noch_offen = welt.guild.me.voice.self_deaf is False
                welt.spieler._voice_lock.release()
                await welt.inst.tick(laufzeit.client)
            return offen, antwort, noch_offen

        offen, antwort, noch_offen = asyncio.run(lauf())
        assert offen and "Mikro aus" in antwort and noch_offen
        assert welt.guild.me.voice.self_deaf is True
        assert welt.guild.zustand[-1]["self_deaf"] is True
        assert not _sitzung(welt).taub_von_uns
        # Danach ist Ruhe: kein weiteres Taubstellen bei jedem Takt.
        n = len(welt.guild.zustand)
        asyncio.run(welt.inst.tick(laufzeit.client))
        assert len(welt.guild.zustand) == n


def test_clips_mikro_aus_waehrend_discord_neu_verbindet_stellt_danach_taub():
    """'flo clip aus' genau in dem Moment, in dem discord.py die Verbindung
    selbst neu aufbaut (der Client bleibt, ist kurz nicht verbunden - und kommt
    mit dem Zustand von JETZT zurueck, also hoerend). Das Taubstellen darf
    dabei nicht verloren gehen: sonst saesse Flo mit offenen Ohren da, ohne
    zuzuhoeren, und das Kopfhoerer-Symbol luege."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        assert welt.guild.me.voice.self_deaf is False and s.taub_von_uns
        welt.vc._connection.verbunden = False              # Neuaufbau laeuft
        antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.anna)))
        assert "Mikro aus" in antwort and not s.aktiv and s.puffer is None
        assert s.taub_von_uns                              # vorgemerkt, nicht vergessen
        welt.vc._connection.verbunden = True               # zurueck - noch hoerend
        assert welt.guild.me.voice.self_deaf is False
        asyncio.run(welt.inst.tick(laufzeit.client))
        assert welt.guild.me.voice.self_deaf is True and not s.taub_von_uns
        # Ist Flo ganz raus (kein Client mehr), gibt es nichts nachzuholen.
        s.taub_von_uns = True
        welt.guild.voice_client = None
        asyncio.run(welt.inst.abgleichen(welt.guild))
        assert not s.taub_von_uns


def test_clips_neuer_client_der_musik_hoert_sofort_trotz_bremse():
    """Die Musik baut neu auf (neuer Client, und der verbindet taub). Die
    Bremse 'hoechstens alle TAUB_ALLE s entstauben' gilt nur fuer DENSELBEN
    Client - sonst saesse Flo bis zu einer halben Minute taub in einer Runde,
    die laut Status laeuft."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        welt.vc._connection.verbunden = False              # Abriss ...
        asyncio.run(_starten(welt))
        assert s.aktiv and s.getrennt_seit is not None
        neu = _client(welt.kanal)                          # ... und Neuaufbau, taub
        neu._connection.ws = SimpleNamespace()             # eine neue Verbindung
        welt.guild.voice_client = neu
        welt.spieler.voice = neu
        welt.guild.me.voice.self_deaf = True
        asyncio.run(_starten(welt))
        assert s.aktiv and s.empfaenger.client is neu
        assert welt.guild.me.voice.self_deaf is False and neu.geweckt == 1
        # Derselbe Client, gleich wieder taub (wer auch immer): dann bremst es.
        welt.guild.me.voice.self_deaf = True
        s.hoeren_vc = None
        asyncio.run(_starten(welt))
        assert welt.guild.me.voice.self_deaf is True


def test_clips_nach_dem_herunterfahren_faengt_nichts_mehr_an():
    """herunterfahren() leert alles - und bis zum Abmelden koennen noch ein
    Voice-Ereignis oder ein Takt kommen: keine neue Ansage, kein Lauscher."""
    with _welt() as welt:
        with mock.patch.object(clipstext.transkriber, "schliessen", _als_nichts):
            asyncio.run(welt.inst.herunterfahren())
        asyncio.run(_starten(welt))
        asyncio.run(welt.inst.tick(laufzeit.client))
        s = _sitzung(welt)
        assert not s.aktiv and s.grund == "ende" and voiceempfang.fuer(GID) is None
        assert welt.kanal.gesendet == [] and welt.guild.zustand == []
        assert "fahr gerade runter" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip", welt.anna)))


def test_clips_takt_leert_bei_funktion_aus_einwilligung_aus_und_sendepause():
    """Was keinen eigenen Haken hat (Funktions-Schalter, Sendepause), holt der
    15-s-Takt nach: Puffer leer, laufende Schnitte und Dialoge verworfen."""
    for art in ("feature", "server", "sendepause"):
        with _welt() as welt:
            asyncio.run(_starten(welt))
            _reden(welt, ANNA, 10, 1)
            s = _sitzung(welt)
            gen = s.sperre_gen
            welt.spieler.clip_halten = True
            patches = contextlib.ExitStack()
            if art == "feature":
                patches.enter_context(mock.patch.object(features, "is_on_in",
                                                        lambda g, k: k != "clips"))
            elif art == "server":
                welt.schalter["aufnahme"] = False
            else:
                patches.enter_context(mock.patch.object(clips, "_sendepause", lambda: True))
            with patches:
                asyncio.run(welt.inst.tick(laufzeit.client))
            assert not s.aktiv and s.puffer is None, art
            assert s.sperre_gen == gen + 1 and welt.spieler.clip_halten is False, art


def test_clips_einstellung_meldet_sich_sofort():
    """'clip_aufnahme' aus (Befehl oder Panel): guildcfg ruft den Hoerer, clips
    leert sofort - nicht erst im naechsten Takt."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        assert _sitzung(welt).aktiv

        async def umschalten():
            welt.schalter["aufnahme"] = False
            clips._einstellung_neu(GID)
            await _nebenher(welt)

        asyncio.run(umschalten())
        assert not _sitzung(welt).aktiv


def test_clips_takt_haelt_wach_berichtet_und_warnt():
    """Der 15-s-Takt: Discord wach halten, wenn keine Musik laeuft; alle fuenf
    Minuten eine Zeile (nur Zahlen); eine Warnung, wenn Pakete ankommen und
    nichts im Puffer landet - hoechstens einmal je zehn Minuten."""
    from test_fundament import _Mitschnitt
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        assert welt.vc.geweckt == 1
        s.letzte_weckung -= clips.WECKEN_ALLE + 1
        asyncio.run(welt.inst.tick(laufzeit.client))
        assert welt.vc.geweckt == 2
        welt.vc.spielt_ = True                       # Musik laeuft: kein fremdes Wecken
        s.letzte_weckung -= clips.WECKEN_ALLE + 1
        asyncio.run(welt.inst.tick(laufzeit.client))
        assert welt.vc.geweckt == 2
        welt.vc.spielt_ = False
        _reden(welt, ANNA, 10, 1)
        s.bericht_um -= clips.BERICHT_ALLE + 1
        with _Mitschnitt("dcbot.clips") as mit:
            asyncio.run(welt.inst.tick(laufzeit.client))
        zeilen = [e.getMessage() for e in mit.eintraege if "Pakete" in e.getMessage()]
        assert len(zeilen) == 1 and f"Server {GID}" in zeilen[0], zeilen
        assert "Anna" not in zeilen[0] and str(ANNA) not in zeilen[0]
        # Pakete kommen, aber nichts wird gespeichert (z. B. DAVE klemmt).
        e = s.empfaenger
        e._zaehler["roh"] += 200
        with _Mitschnitt("dcbot.clips") as mit:
            asyncio.run(welt.inst.tick(laufzeit.client))
            e._zaehler["roh"] += 200
            asyncio.run(welt.inst.tick(laufzeit.client))
        warnungen = [x.getMessage() for x in mit.ab(logging.WARNING)]
        assert len(warnungen) == 1 and "nichts landet im Puffer" in warnungen[0], warnungen


def test_clips_takt_warnt_bei_stille_mit_leuten_im_call():
    from test_fundament import _Mitschnitt
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        s.ton_um -= clips.STILL_SEK + 1
        with _Mitschnitt("dcbot.clips") as mit:
            asyncio.run(welt.inst.tick(laufzeit.client))
        warnungen = [x.getMessage() for x in mit.ab(logging.WARNING)]
        assert len(warnungen) == 1 and "kein einziges Paket" in warnungen[0], warnungen
    # Alle stumm geschaltet: kein Grund zur Sorge.
    stumm = [SimpleNamespace(id=uid, bot=False, voice=SimpleNamespace(self_mute=True, mute=False))
             for uid in (ANNA, BOB)]
    with _welt(leute=stumm) as welt:
        asyncio.run(_starten(welt))
        _sitzung(welt).ton_um -= clips.STILL_SEK + 1
        with _Mitschnitt("dcbot.clips") as mit:
            asyncio.run(welt.inst.tick(laufzeit.client))
        assert not mit.ab(logging.WARNING)


def test_clips_warnungen_zaehlen_nur_ton_und_niemanden_der_raus_ist():
    """Berichte (RTCP) sind kein Ton, und wer raus ist, wird mit Absicht
    verworfen: beides loest keinen Alarm aus - und RTCP allein verdeckt auch
    nicht, dass minutenlang gar kein Ton kommt."""
    from test_fundament import _Mitschnitt
    with _welt() as welt:
        asyncio.run(_starten(welt))
        s = _sitzung(welt)
        e = s.empfaenger
        asyncio.run(welt.inst.tick(laufzeit.client))        # ein Stand im Verlauf
        e._zaehler["roh"] += 400                             # 100 RTCP, 300 von Leuten,
        e._zaehler["rtcp"] += 100                            # die raus sind
        e._zaehler["raus"] += 300
        with _Mitschnitt("dcbot.clips") as mit:
            asyncio.run(welt.inst.tick(laufzeit.client))
        assert not mit.ab(logging.WARNING)
        # Ab jetzt nur noch RTCP - nach STILL_SEK ohne Ton kommt die Warnung.
        s.ton_um -= clips.STILL_SEK + 1
        e._zaehler["roh"] += 30
        e._zaehler["rtcp"] += 30
        with _Mitschnitt("dcbot.clips") as mit:
            asyncio.run(welt.inst.tick(laufzeit.client))
        assert ["kein einziges Paket" in x.getMessage()
                for x in mit.ab(logging.WARNING)] == [True]
    # Sitzt im Call nur, wer raus ist, wartet auch niemand auf Ton.
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.inst.raus(ANNA)
        welt.inst.raus(BOB)
        _sitzung(welt).ton_um -= clips.STILL_SEK + 1
        with _Mitschnitt("dcbot.clips") as mit:
            asyncio.run(welt.inst.tick(laufzeit.client))
        assert not mit.ab(logging.WARNING)


def test_clips_speicher_raeumt_auf_und_merkt_das_kontingent():
    """Clips nach 35 Tagen, Ansagen nach 7, 'aus' nach 12 Stunden. Das
    Whisper-Kontingent ueberlebt einen Neustart (clips.json)."""
    with _welt() as welt:
        jetzt = time.time()
        inst = welt.inst
        inst._daten("ansagen").update({"1": {"ts": jetzt - 8 * 86400, "mid": 5},
                                        "2": {"ts": jetzt - 86400, "mid": 6}})
        inst._daten("aus").update({"3": jetzt - 13 * 3600, "4": jetzt - 3600,
                                    "5": jetzt + 9 * 3600})   # Uhrsprung: gilt als jetzt
        index = inst._clips_von(GID, anlegen=True)
        index["alt00000"] = {"mid": 1, "ts": jetzt - 40 * 86400, "stimmen": []}
        index["neu00000"] = {"mid": 2, "ts": jetzt - 86400, "stimmen": []}
        assert inst._aufraeumen(jetzt) == 3
        assert set(inst._daten("ansagen")) == {"2"} and set(inst._daten("aus")) == {"4", "5"}
        assert set(index) == {"neu00000"}
        # Kontingent: gebucht -> im Speicher, und ein frisches clips liest es zurueck.
        kontingent = clipstext.transkriber.kontingent
        alt = kontingent.als_dict()
        try:
            kontingent.buchen([30])
            inst._kontingent_merken()
            assert inst._store.data["whisper"]["eimer"], inst._store.data["whisper"]
            kontingent.aus_dict({})
            assert kontingent.heute() == (0, 0)
            kontingent.aus_dict(inst._store.data["whisper"])
            assert kontingent.heute() == (1, 30)
        finally:
            kontingent.aus_dict(alt)


def test_clips_herunterfahren_leert_alles():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, ANNA, 10, 1)
        s = _sitzung(welt)
        puffer = s.puffer
        with mock.patch.object(clipstext.transkriber, "schliessen", _als_nichts):
            asyncio.run(welt.inst.herunterfahren())
        assert not s.aktiv and not puffer.spuren and voiceempfang.fuer(GID) is None
        assert welt.inst._abbruch is True and welt.inst._store.gespeichert >= 1
        assert "whisper" in welt.inst._store.data


def test_clips_voice_state_nur_fuer_flo():
    """Nur Flos eigene Voice-Ereignisse gleichen ab - jemand anderes nicht."""
    with _welt() as welt:
        gerufen = []

        async def abgleichen(guild):
            gerufen.append(guild.id)

        with mock.patch.object(welt.inst, "abgleichen", abgleichen):
            jemand = SimpleNamespace(id=ANNA, guild=welt.guild)
            flo = SimpleNamespace(id=FLO, guild=welt.guild)
            drin = SimpleNamespace(channel=welt.kanal, deaf=False, self_deaf=True)
            draussen = SimpleNamespace(channel=None, deaf=False, self_deaf=True)
            asyncio.run(welt.inst.voice_state(jemand, draussen, drin))
            assert gerufen == []
            asyncio.run(welt.inst.voice_state(flo, draussen, drin))
            asyncio.run(welt.inst.voice_state(flo, drin, SimpleNamespace(
                channel=welt.kanal, deaf=True, self_deaf=True)))
            asyncio.run(welt.inst.voice_state(flo, drin, drin))     # nichts geaendert
            assert gerufen == [GID, GID]


# =============================================================================
# Mikro aus / an
# =============================================================================
def test_clips_mikro_aus_und_ueber_den_neustart():
    """'flo clip aus': Puffer leer, wieder taub, Hinweis im Voice-Chat, und es
    gilt auch nach einem Neustart - bis 'flo clip an'."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, BOB, 10, 1)
        s = _sitzung(welt)
        gen = s.sperre_gen
        assert welt.vc.eigen_senke is not None
        antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.anna)))
        assert "Mikro aus" in antwort and "Puffer gelöscht" in antwort
        assert not s.aktiv and s.puffer is None and s.sperre_gen > gen
        # Auch Flos eigene Spur haengt nicht mehr am Client (sonst wuechse der
        # verwaiste Puffer weiter, solange die Verbindung lebt).
        assert welt.vc.eigen_senke is None and welt.vc._eigene_senke() is None
        assert str(VOICE) in welt.inst._daten("aus")
        assert welt.guild.zustand[-1]["self_deaf"] is True
        hinweis = welt.kanal.gesendet[-1]
        assert "Mitschnitt aus" in hinweis.content and f"<@{ANNA}>" in hinweis.content
        assert hinweis.kw["allowed_mentions"].users is False
        # Zweimal aus: "Ich hoer doch gar nicht mit."
        assert "gar nicht mit" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip aus", welt.anna)))
        # Neustart: ein frisches clips mit demselben Speicher hoert NICHT mit.
        speicher = welt.inst._store
        vars(welt.inst).update(vars(clips.Clips()))
        welt.inst._store, welt.inst._enabled, welt.inst._kann_rendern = speicher, True, True
        welt.inst._uhr = welt.uhr
        asyncio.run(_starten(welt))
        assert not _sitzung(welt).aktiv and _sitzung(welt).grund == "aus"
        # 'flo clip an' hebt es auf und kuendigt NEU an (nach der Umschalt-Pause).
        async def verbinden(kanal):
            return welt.vc

        welt.inst._daten("aus")[str(VOICE)] -= clips.AN_PAUSE + 1
        with mock.patch.object(welt.spieler, "connect", verbinden):
            antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip an", welt.anna)))
        assert "hör mit" in antwort and _sitzung(welt).aktiv
        assert welt.spieler.clip_halten is True
        assert "Ab jetzt hör ich hier mit" in welt.kanal.gesendet[-1].content


def test_clips_mikro_aus_nur_aus_dem_call_und_endet_mit_der_runde():
    """Ausmachen darf, wer im Call sitzt, oder ein Mod. Das 'aus' endet, wenn
    die Runde in diesem Kanal vorbei ist."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        draussen = _person(CEM, "Cem")
        assert "Zaungast" in asyncio.run(clips.handle(_nachricht(welt, "flo clip aus",
                                                                 draussen)))
        assert _sitzung(welt).aktiv
        mod = _person(DORA, "Dora", mod=True)
        assert "Mikro aus" in asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", mod)))
        assert str(VOICE) in welt.inst._daten("aus")
        # Die Runde endet: Flo raus, die Musik will auch nicht mehr hin.
        welt.vc._connection.verbunden = False
        welt.spieler.active_channel_id = None
        asyncio.run(_starten(welt))
        assert str(VOICE) not in welt.inst._daten("aus")


def test_clips_mikro_aus_bleibt_waehrend_des_neuaufbaus():
    """Reisst nur die Verbindung (die Musik will weiter in den Kanal), bleibt
    das 'aus' - sonst hoerte Flo nach jedem Neuaufbau wieder mit."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.anna)))
        welt.vc._connection.verbunden = False
        asyncio.run(_starten(welt))
        welt.vc._connection.verbunden = True
        asyncio.run(_starten(welt))
        assert str(VOICE) in welt.inst._daten("aus") and not _sitzung(welt).aktiv


def test_clips_mikro_aus_und_raus_wenn_nur_dafuer_da():
    """Kam Flo nur zum Mithoeren ('flo clip an') und laeuft nichts: nach 'aus'
    geht er."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.spieler.clip_halten = True
        getrennt = []

        async def disconnect(**_kw):
            getrennt.append(True)

        with mock.patch.object(welt.spieler, "disconnect", disconnect):
            antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.anna)))
        assert "ich bin raus" in antwort and getrennt == [True]


def test_clips_mikro_aus_nach_der_runde_verwirft_was_noch_offen_ist():
    """Flo ist schon weg (die Musik hat getrennt), aber ein Dialog ist noch
    offen: 🔇 und 'flo clip aus' verwerfen ihn trotzdem - statt 'Ich hör doch
    gar nicht mit' zu sagen und den Clip dann doch zu posten. Ein Zaungast darf
    das nicht; ist nichts mehr offen, kommt die ehrliche Antwort."""
    for weg in ("knopf", "befehl"):
        with _welt() as welt:
            asyncio.run(_starten(welt))
            welt.uhr.vor(60)
            _reden(welt, ANNA, 25, 15)
            _reden(welt, BOB, 20, 5, strang=2)
            asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
            msg = welt.text.gesendet[-1]
            dialog = msg.view
            welt.spieler.active_channel_id = None           # Flo geht, die Runde ist vorbei
            welt.guild.voice_client = None
            asyncio.run(_starten(welt))
            assert not _sitzung(welt).aktiv and dialog.snap is not None
            draussen = _person(CEM, "Cem")
            assert "Zaungast" in asyncio.run(clips.handle(_nachricht(welt, "flo clip aus",
                                                                     draussen)))
            assert dialog.snap is not None
            if weg == "knopf":
                klick = _klick_im_voice(BOB, welt.kanal)
                klick.guild = welt.guild
                asyncio.run(welt.inst.aus_knopf(klick, VOICE))
                antwort = klick.protokoll[0][1]
            else:
                antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.bob)))
            assert "noch offen war, ist weg" in antwort, (weg, antwort)
            knopf = [k for k in dialog.children if getattr(k, "label", "") == "30 s"][0]
            with _schnitt() as auftraege:
                asyncio.run(knopf.callback(_Klick(ANNA, gid=GID)))
            assert auftraege == [] and welt.ziel.gesendet == [], weg
            assert "inzwischen ausgemacht" in msg.content, weg
            # Jetzt ist nichts mehr offen - und kein 'aus' fuer den Kanal gesetzt.
            assert "gar nicht mit" in asyncio.run(clips.handle(_nachricht(
                welt, "flo clip aus", welt.bob)))
            assert str(VOICE) not in welt.inst._daten("aus")


def test_clips_mikro_aus_nach_der_runde_trifft_auch_den_laufenden_schnitt():
    """Flo geht, waehrend der Schnitt noch laeuft (hier: waehrend die KI ihren
    Spruch sucht), und Bob macht danach das Mikro aus: der Clip kommt nicht."""
    with _welt(transkript=True) as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        _reden(welt, BOB, 20, 5, strang=2)
        seg = {ANNA: [clipstext.Segment(0.5, 2.0, "Wer hat die Pizza gegessen?")]}
        antworten = []

        async def kommentieren(gid, zeilen, dauer):
            welt.spieler.active_channel_id = None
            welt.guild.voice_client = None
            await welt.inst.abgleichen(welt.guild)
            antworten.append(await clips.handle(_nachricht(welt, "flo clip aus", welt.bob)))
            return "Spannend wie Steuerrecht."

        with _mit_transkript(welt, seg), mock.patch.object(clipstext, "kommentar", kommentieren):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
        assert "noch offen war, ist weg" in antworten[0], antworten
        assert welt.ziel.gesendet == []
        assert "inzwischen ausgemacht" in welt.text.gesendet[-1].content
        assert not welt.inst._laufend


def test_clips_aus_knopf():
    """🔇 an der Ansage - geht fuer Leute im Call, nicht fuer Zaungaeste."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        view = welt.kanal.gesendet[0].view
        cid = f"flo:clip:aus:{VOICE}"
        fremd = _klick_im_voice(CEM, _voice_kanal_mit_chat(cid=999))
        fremd.guild = welt.guild
        assert _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, view, cid, fremd) == 1
        assert "Zaungast" in fremd.protokoll[0][1] and _sitzung(welt).aktiv
        klick = _klick_im_voice(ANNA, welt.kanal)
        klick.guild = welt.guild
        assert _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, view, cid, klick) == 1
        art, text, kw = klick.protokoll[0]
        assert art == "send" and "Mikro aus" in text and kw.get("ephemeral") is True
        assert not _sitzung(welt).aktiv and str(VOICE) in welt.inst._daten("aus")


def test_clips_an_ueber_die_musik():
    """'flo clip an' verbindet ueber den Musik-Player (der haelt die Sitzung)."""
    with _welt(musik=False) as welt:
        welt.guild.voice_client = None
        verbunden = []

        async def verbinden(kanal):
            verbunden.append(kanal.id)
            welt.guild.voice_client = welt.vc
            welt.spieler.voice = welt.vc
            welt.spieler.active_channel_id = kanal.id
            return welt.vc

        with mock.patch.object(welt.spieler, "connect", verbinden):
            antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip an", welt.anna)))
            assert verbunden == [VOICE] and "Bin da und hör mit" in antwort
            assert welt.spieler.clip_halten and welt.spieler.text_channel is welt.text
            assert "schon mit" in asyncio.run(clips.handle(_nachricht(welt, "flo clip an",
                                                                      welt.anna)))
        # Wer nicht im Voice ist; Flo in einer Musik-Sitzung woanders.
        draussen = _person(CEM, "Cem")
        assert "Geh erst in einen Voice" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip an", draussen)))
        woanders = _voice_kanal_mit_chat(cid=990, name="Keller")
        cem = _person(CEM, "Cem", woanders)
        assert "Zocken" in asyncio.run(clips.handle(_nachricht(welt, "flo clip an", cem)))


def test_clips_an_verweigert():
    with _welt(musik=False) as welt:
        welt.guild.voice_client = None

        async def kaputt(_kanal):
            raise RuntimeError("davey library needed")

        from test_fundament import _Mitschnitt
        with mock.patch.object(welt.spieler, "connect", kaputt), \
                _Mitschnitt("dcbot.clips") as mit:
            assert asyncio.run(clips.handle(_nachricht(welt, "flo clip an", welt.anna))) \
                == music.VOICE_KAPUTT
        assert [x.getMessage() for x in mit.ab(logging.WARNING)] == [
            f"Clips: Server {GID} – 'clip an' kam nicht in den Voice (RuntimeError)."]
        welt.schalter["aufnahme"] = False
        assert "einstellung clip_aufnahme an" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip an", welt.anna)))
    with _welt(musik=False) as welt:
        welt.guild.voice_client = None
        welt.kanal._schreiben = False

        async def verbinden(kanal):
            welt.guild.voice_client = welt.vc
            welt.spieler.voice = welt.vc
            welt.spieler.active_channel_id = kanal.id
            return welt.vc

        with mock.patch.object(welt.spieler, "connect", verbinden):
            antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip an", welt.anna)))
        assert "Schreibrechte" in antwort and not _sitzung(welt).aktiv
        # Ohne Ansage wird hier nichts: 'clip an' haelt Flo dann nicht im Kanal,
        # es gilt wieder der normale Leerlauf.
        assert welt.spieler.clip_halten is False


# =============================================================================
# Opt-out
# =============================================================================
def test_clips_ohne_mich_leert_sofort_und_haelt_alles_auf():
    """'flo clip nein': was im Puffer lag, ist weg; neue Pakete kommen nicht mehr
    hinein (Puffer UND Empfaenger vor DAVE fragen); offene Dialoge vergessen."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, ANNA, 10, 1)
        _reden(welt, BOB, 10, 1, strang=2)
        s = _sitzung(welt)
        snap = s.puffer.schnappschuss(welt.uhr())
        dialog = SimpleNamespace(snap=snap, vergessen=snap.vergessen)
        welt.inst._dialoge[GID] = {CEM: dialog}
        # Ein echtes Paket von Annas zweiter SSRC, das noch auf op 5 wartet.
        s.empfaenger.roh(_rtp(_STIMME, 7, 960, 5151, 1))
        s.empfaenger.abarbeiten()
        assert s.empfaenger.stand()["gehalten"] == 1
        welt.vc.ssrc_zu_uid[5151] = ANNA
        antwort = _frag(_nachricht(welt, "flo clip nein", welt.anna))
        assert "du bist raus" in antwort
        assert s.empfaenger.stand()["gehalten"] == 0
        assert ANNA not in s.puffer.spuren and BOB in s.puffer.spuren
        assert ANNA not in snap.stimmen and BOB in snap.stimmen
        assert s.empfaenger.opt_out(ANNA) is True and s.empfaenger.opt_out(BOB) is False
        s.puffer.einfuegen(ANNA, 1, 1, 1, _STIMME, welt.uhr())
        assert ANNA not in s.puffer.spuren
        assert ANNA not in s.puffer.schnappschuss(welt.uhr()).stimmen
        assert str(ANNA) in welt.inst._daten("opt_out")
        assert "schon raus" in _frag(_nachricht(welt, "flo clip nein", welt.anna))
        # Zurueck.
        assert "wieder drin" in _frag(_nachricht(welt, "flo clip ja", welt.anna))
        assert "nie raus" in _frag(_nachricht(welt, "flo clip ja", welt.anna))
        s.puffer.einfuegen(ANNA, 1, 1, 1, _STIMME, welt.uhr())
        assert ANNA in s.puffer.spuren


def test_clips_niemand_entscheidet_fuer_andere():
    with _welt() as welt:
        for befehl in ("flo clip nein", "flo clip ja"):
            antwort = _frag(_nachricht(welt, befehl, welt.anna, erwaehnt=[welt.bob]))
            assert "Das entscheidet Bob selbst" in antwort, befehl
        assert not welt.inst.ist_raus(BOB)
        # Sich selbst erwaehnen ist in Ordnung.
        assert "raus" in _frag(_nachricht(welt, "flo clip nein", welt.anna,
                                          erwaehnt=[welt.anna]))


def test_clips_ohne_mich_knopf_geht_immer():
    """🙅 geht auch in der Sendepause und wo Clips aus sind."""
    with _welt() as welt:
        view = clips.AnsageView(VOICE)
        with mock.patch.object(clips, "_sendepause", lambda: True), \
                mock.patch.object(features, "is_on_in", lambda g, k: False):
            klick = _Klick(CEM, gid=GID)
            assert _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, view, clips.OHNE_ID, klick) == 1
        art, text, kw = klick.protokoll[0]
        assert art == "send" and "du bist raus" in text and kw["ephemeral"] is True
        assert welt.inst.ist_raus(CEM)
        klick = _Klick(CEM, gid=GID)
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, view, clips.OHNE_ID, klick)
        assert "schon raus" in klick.protokoll[0][1]
    # Ohne geladenes clips: ehrliche Antwort, kein Absturz.
    frisch = clips.Clips()
    klick = _Klick(CEM, gid=GID)
    with mock.patch.object(clips, "instance", frisch):
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, clips.AnsageView(VOICE), clips.OHNE_ID,
                           klick)
    assert "gar nicht" in klick.protokoll[0][1]


def test_clips_datenschutz_wo_clips_aus_sind():
    """datenschutz() steht IMMER in der Kette: Opt-out und Opt-in gehen auch, wo
    Clips aus sind; jeder andere Clip-Befehl bekommt dort ein klares 'aus'."""
    with _welt() as welt:
        with mock.patch.object(features, "is_on_in", lambda g, k: k != "clips"):
            assert "du bist raus" in _frag(_nachricht(welt, "flo clip nein", welt.anna),
                                           clips.datenschutz)
            assert welt.inst.ist_raus(ANNA)
            assert "wieder drin" in _frag(_nachricht(welt, "flo clip ja", welt.anna),
                                          clips.datenschutz)
            for befehl in ("flo clip", "flo clips", "flo clip 1m", "flo clip an"):
                assert asyncio.run(clips.datenschutz(_nachricht(welt, befehl, welt.anna))) \
                    == clips.TEXTE["aus_hier"], befehl
            assert asyncio.run(clips.datenschutz(_nachricht(welt, "flo clip ist cool",
                                                            welt.anna))) is None
        # Wo Clips an sind, hat handle() schon geantwortet - datenschutz schweigt.
        assert asyncio.run(clips.datenschutz(_nachricht(welt, "flo clip 1m", welt.anna))) is None


def test_clips_wer_raus_ist_clippt_auch_keine_anderen():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.inst.raus(ANNA)
        assert "Selbst nicht mitgeschnitten" in _frag(_nachricht(welt, "flo clip 1m",
                                                                 welt.anna))
        welt.inst.raus(BOB)
        welt.inst.rein(ANNA)
        assert "Bob clip ich nicht" in _frag(_nachricht(welt, "flo clip 1m", welt.anna,
                                                        erwaehnt=[welt.bob]))


def test_clips_wer_raus_ist_fehlt_in_jeder_stufe():
    """Jede Stufe filtert fuer sich, keine verlaesst sich auf die davor: der
    Schnappschuss des Puffers (auch wenn vergessen() noch nicht lief) und der
    Auftrag (auch fuer einen Schnappschuss von VOR dem Opt-out, den kein
    Dialog kennt - etwa einer, der auf den Schnitt wartet)."""
    weg = set()
    puffer = clips.Ringpuffer(GID, VOICE, raus=lambda uid: uid in weg, uhr_=lambda: 0.0)
    for k in range(50):
        puffer.einfuegen(ANNA, 1, k, k * 960, _STIMME, 1.0 + k * 0.02)
        puffer.einfuegen(BOB, 2, k, k * 960, _STIMME, 1.0 + k * 0.02)
    weg.add(ANNA)
    snap = puffer.schnappschuss(5.0)
    assert ANNA not in snap.stimmen and BOB in snap.stimmen
    assert ANNA in puffer.spuren                       # nur der Schnappschuss filtert
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 5)
        _reden(welt, BOB, 25, 5, strang=2)
        snap = _sitzung(welt).puffer.schnappschuss(welt.uhr())
        assert {ANNA, BOB} <= set(snap.stimmen)
        welt.inst._raus_ids.add(ANNA)                  # raus - der Schnappschuss weiss es nicht
        auftrag, info = welt.inst._auftrag_bauen(welt.guild, snap, 30, None, welt.bob,
                                                 lambda: False)
        assert [sp.uid for sp in auftrag.stimmen] == [BOB] and ANNA not in info["namen"]
        auftrag, text = welt.inst._auftrag_bauen(welt.guild, snap, 30, ANNA, welt.bob,
                                                 lambda: False)
        assert auftrag is None and text == "Anna clip ich nicht. Such dir wen anders."
        # Auch wer selbst fragt, kommt nicht hinein, wenn er raus ist.
        auftrag, info = welt.inst._auftrag_bauen(welt.guild, snap, 30, None, welt.anna,
                                                 lambda: False)
        assert [sp.uid for sp in auftrag.stimmen] == [BOB]


# =============================================================================
# Clip anfordern: Sperren, Rechte, Grenzen
# =============================================================================
def test_clips_clip_gruende_und_rechte():
    with _welt() as welt:
        # Flo nicht im Voice.
        welt.vc._connection.verbunden = False
        welt.spieler.active_channel_id = None
        assert "keinem Voice" in asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
    with _welt() as welt:
        welt.schalter["aufnahme"] = False
        assert "hör ich nicht mit" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip", welt.anna)))
    with _welt(musik=False) as welt:
        asyncio.run(_starten(welt))
        assert "nur kurz für einen Sound" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip", welt.anna)))
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, ANNA, 10, 1)
        welt.uhr.vor(30)
        draussen = _person(CEM, "Cem")
        assert "nicht mal im Call" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip", draussen)))
        # Wer gerade erst kam: Flo hoert noch keine 3 Sekunden zu.
    with _welt() as welt:
        asyncio.run(_starten(welt))
        assert "gerade erst angefangen" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip 30", welt.anna)))


def test_clips_wer_im_puffer_redet_darf_auch_nach_dem_gehen():
    """'Clips gibt's nur fuer Leute, die dabei waren': wer im Puffer redet, darf
    clippen, auch wenn er den Call gerade verlassen hat. Mods immer."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(20)
        _reden(welt, CEM, 10, 1)
        cem = _person(CEM, "Cem")
        with _schnitt():
            antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", cem)))
        assert antwort is clips.HANDLED
        mod = _person(DORA, "Dora", mod=True)
        assert welt.inst._darf_clippen(mod, _sitzung(welt))


def test_clips_abkuehlen_stunde_warteschlange():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(20)
        _reden(welt, BOB, 10, 1)
        with _schnitt() as auftraege, mock.patch.object(clipstext, "kommentar", _kein_kommentar):
            assert asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna))) \
                is clips.HANDLED
            assert len(auftraege) == 1
            antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
            assert antwort.startswith("Chill. Noch ") and len(auftraege) == 1
            # Stundendeckel je Server.
            welt.inst._zuletzt.clear()
            welt.inst._stunde[GID] = deque([time.monotonic()] * clips.STUNDE_MAX)
            assert "Genug geclippt" in asyncio.run(clips.handle(
                _nachricht(welt, "flo clip 30", welt.anna)))
            welt.inst._stunde[GID].clear()
            welt.inst._in_arbeit = clips.WARTESCHLANGE_MAX
            assert "schneid gerade" in asyncio.run(clips.handle(
                _nachricht(welt, "flo clip 30", welt.anna)))


def test_clips_gleichzeitige_anfragen_umgehen_das_abkuehlen_nicht():
    """Zwei, drei 'flo clip 30' derselben Person im selben Augenblick: nur EINER
    wird geschnitten - Abkuehlen und Stunde sind belegt, bevor das erste await
    kommt. Ebenso oeffnen zwei gleichzeitige 'flo clip' keine zwei Dialoge.
    Wird gar nicht erst geschnitten (nichts zu hoeren), gibt es die Anrechnung
    zurueck."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        _reden(welt, BOB, 20, 5, strang=2)
        clips.SETTLE_SEK = 0.05

        async def mehrere(text, n):
            return await asyncio.gather(*(clips.handle(_nachricht(welt, text, welt.anna))
                                          for _ in range(n)))

        with _schnitt() as auftraege, mock.patch.object(clipstext, "kommentar", _kein_kommentar):
            antworten = asyncio.run(mehrere("flo clip 30", 3))
        assert len(auftraege) == 1 and len(welt.ziel.gesendet) == 1
        assert sum(1 for a in antworten if a is clips.HANDLED) == 1
        assert sum(1 for a in antworten if isinstance(a, str) and a.startswith("Chill")) == 2
        assert len(welt.inst._stunde[GID]) == 1
        welt.inst._zuletzt.clear()
        antworten = asyncio.run(mehrere("flo clip", 2))
        assert len(welt.inst._dialoge[GID]) == 1
        assert sorted(str(a)[:20] for a in antworten if a is not clips.HANDLED) == [
            clips.TEXTE["dialog_offen"][:20]]
        assert not welt.inst._dialog_kommt
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)                               # keiner hat geredet
        with _schnitt() as auftraege:
            assert _direkt(welt) is clips.HANDLED
        assert auftraege == [] and "Da war nix" in welt.text.gesendet[-1].content
        assert ANNA not in welt.inst._zuletzt and not welt.inst._stunde.get(GID)


def test_clips_an_aus_im_sekundentakt_spammt_den_voice_chat_nicht():
    """Jedes 'aus' postet einen Hinweis in den Voice-Chat, jedes 'an' eine neue
    Ansage. Wer im Sekundentakt umschaltet, bekommt nach dem 'aus' eine Minute
    lang kein 'an' - danach geht es wieder (mit neuer Ansage)."""
    with _welt() as welt:
        asyncio.run(_starten(welt))

        async def verbinden(kanal):
            return welt.vc

        with mock.patch.object(welt.spieler, "connect", verbinden), \
                mock.patch.object(welt.spieler, "nichts_zu_tun", lambda: False):
            for _ in range(5):
                asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.anna)))
                antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip an", welt.anna)))
            assert "Entscheidet euch" in antwort and not _sitzung(welt).aktiv
            ansagen = [m for m in welt.kanal.gesendet if "Ab jetzt hör" in (m.content or "")]
            aus = [m for m in welt.kanal.gesendet if "Mitschnitt aus" in (m.content or "")]
            assert len(ansagen) == 1 and len(aus) == 1
            welt.inst._daten("aus")[str(VOICE)] -= clips.AN_PAUSE + 1
            antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip an", welt.anna)))
        assert "hör mit" in antwort and _sitzung(welt).aktiv
        assert sum(1 for m in welt.kanal.gesendet if "Ab jetzt hör" in (m.content or "")) == 2


def test_clips_person_in_der_notlage_ist_kein_ziel():
    """Wer gerade am Boden ist, wird nicht einzeln geclippt - und die Abfuhr
    ist DIESELBE wie fuer jemanden, der raus ist: 'flo clip @Bob' verraet nie,
    dass Bob ein Notsignal geschrieben hat (die Sperre gilt auf jedem Server).
    Im Befehls-Log von bot.py steht sie auch nicht."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(20)
        _reden(welt, BOB, 10, 1)
        basis.notlage_merken(BOB)
        nachricht = _nachricht(welt, "flo clip 30", welt.anna, erwaehnt=[welt.bob])
        assert asyncio.run(clips.handle(nachricht)) is clips.HANDLED
        am_boden = welt.text.gesendet[-1].content
        basis._notlage_seit.pop(BOB, None)
        welt.inst.raus(BOB)
        raus = _frag(_nachricht(welt, "flo clip 30", welt.anna, erwaehnt=[welt.bob]))
        assert am_boden == raus == "Bob clip ich nicht. Such dir wen anders."
        # Auch im Schnitt (der Dialog wusste es noch nicht): dieselbe Abfuhr.
        welt.inst.rein(BOB)
        _reden(welt, BOB, 10, 1)                     # was vorher da war, flog beim Ausstieg
        snap = _sitzung(welt).puffer.schnappschuss(welt.uhr())
        basis.notlage_merken(BOB)
        auftrag, text = welt.inst._auftrag_bauen(welt.guild, snap, 30, BOB, welt.anna,
                                                 lambda: False)
        assert auftrag is None and text == raus


def test_clips_wer_aussteigt_steht_nie_im_befehls_log():
    """bot.py schreibt jede Text-Antwort mit dem Namen des Fragenden ins
    Journal ('Befehl von Anna: ...'). Die Antworten zum Aus- und Einstieg - und
    die, die verraten, wer raus ist - schickt clips deshalb selbst."""
    import bot
    from test_fundament import _Mitschnitt, _nur_geladen
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(20)
        _reden(welt, BOB, 10, 1)
        welt.inst.raus(BOB)
        nachrichten = [_nachricht(welt, "flo clip 30", welt.anna, erwaehnt=[welt.bob])]
        nachrichten += [_nachricht(welt, t, welt.anna) for t in (
            "flo clip nein", "flo clip ja", "flo clip nein bitte")]
        nachrichten.append(_nachricht(welt, "flo clip 30", welt.anna))      # Anna ist raus
        for n in nachrichten:
            n.attachments, n.type, n.embeds = [], discord.MessageType.default, []
            n.author.bot = False
        with _nur_geladen(bot, "CLIPS_ENABLED", "AI_ENABLED"), \
                mock.patch.object(bot.features, "fuer", lambda _g: (lambda _k: True)), \
                mock.patch.object(type(bot.client), "user",
                                  property(lambda s: SimpleNamespace(id=FLO))), \
                _Mitschnitt("dcbot") as mit:
            for n in nachrichten:
                asyncio.run(bot.client.on_message(n))
        befehle = [e.getMessage() for e in mit.eintraege if "Befehl von" in e.getMessage()]
        assert befehle == [], befehle
        gesagt = [m.content for m in welt.text.gesendet]
        assert any("du bist raus" in t for t in gesagt) and any("wieder drin" in t
                                                              for t in gesagt)
        assert any("Bob clip ich nicht" in t for t in gesagt)
        assert any("Selbst nicht mitgeschnitten" in t for t in gesagt)


# =============================================================================
# Der Dialog
# =============================================================================
def test_clips_dialog_nur_fuer_den_fragenden_und_einmal():
    """'flo clip': Laenge und Person per Knopf - nur fuer den, der gefragt hat,
    und ein Doppelklick schneidet nicht zweimal. Das Fenster endet bei der
    Frage, nicht beim Klick."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 40, 20)
        _reden(welt, BOB, 30, 5, strang=2)
        frage_um = welt.uhr()
        antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        assert antwort is clips.HANDLED
        msg = welt.text.gesendet[-1]
        dialog = msg.view
        assert isinstance(dialog, clips.ClipDialog) and "Was soll ich clippen" in msg.content
        assert dialog.snap.bis == frage_um
        werte = [o.value for o in dialog.auswahl.options]
        assert werte[0] == "alle" and set(werte[1:]) == {str(ANNA), str(BOB)}
        assert welt.inst._dialoge[GID][ANNA] is dialog
        # Ein Fremder darf nicht.
        fremd = _Klick(BOB, gid=GID)
        assert asyncio.run(dialog.interaction_check(fremd)) is False
        assert "nicht dein Clip" in fremd.protokoll[0][1]
        assert asyncio.run(dialog.interaction_check(_Klick(ANNA, gid=GID))) is True
        # Spaeter klicken: das Fenster bleibt das von der Frage.
        welt.uhr.vor(40)
        knopf = [k for k in dialog.children if getattr(k, "label", "") == "30 s"][0]
        klick1, klick2 = _Klick(ANNA, gid=GID), _Klick(ANNA, gid=GID)
        with _schnitt() as auftraege, mock.patch.object(clipstext, "kommentar", _kein_kommentar):
            async def doppelt():
                await asyncio.gather(knopf.callback(klick1), knopf.callback(klick2))
            asyncio.run(doppelt())
        assert len(auftraege) == 1 and auftraege[0].bis == frage_um
        assert auftraege[0].dauer == 30.0
        assert klick1.protokoll[0][0] == "edit" and "Schneide 30 s" in klick1.protokoll[0][1][
            "content"]
        assert klick2.protokoll[0][0] == "defer"
        assert GID not in welt.inst._dialoge
        assert "Clip ist raus" in msg.content


def test_clips_dialog_person_waehlen_abbrechen_und_zu_langsam():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 40, 20)
        _reden(welt, BOB, 30, 5, strang=2)
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        dialog = welt.text.gesendet[-1].view
        # Bob waehlen, dann 1 min.
        klick = _Klick(ANNA, gid=GID, werte=[str(BOB)])
        dialog.auswahl._values = [str(BOB)]
        with mock.patch.object(type(dialog.auswahl), "values", property(lambda s: [str(BOB)])):
            asyncio.run(dialog.auswahl.callback(klick))
        assert dialog.wahl == BOB and klick.protokoll[0][0] == "defer"
        knopf = [k for k in dialog.children if getattr(k, "label", "") == "1 min"][0]
        with _schnitt() as auftraege, mock.patch.object(clipstext, "kommentar", _kein_kommentar):
            asyncio.run(knopf.callback(_Klick(ANNA, gid=GID)))
        assert [sp.uid for sp in auftraege[0].stimmen] == [BOB] and auftraege[0].flo == []
        post = welt.ziel.gesendet[-1]
        assert f"Nur <@{BOB}>." in post.content

    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 40, 20)
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        msg = welt.text.gesendet[-1]
        dialog = msg.view
        weg = [k for k in dialog.children if str(getattr(k, "emoji", "")) == "✖️"][0]
        klick = _Klick(ANNA, gid=GID)
        asyncio.run(weg.callback(klick))
        assert "Dann halt nicht" in klick.protokoll[0][1]["content"]
        assert dialog.snap is None and GID not in welt.inst._dialoge

    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 40, 20)
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        msg = welt.text.gesendet[-1]
        dialog = msg.view
        asyncio.run(dialog.on_timeout())
        assert msg.edits[-1] == {"content": clips.TEXTE["zu_langsam"], "view": None}
        assert dialog.snap is None and GID not in welt.inst._dialoge


def test_clips_dialog_grenzen():
    """Je Person ein Dialog, je Server drei. Der Takt raeumt vergessene weg."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 40, 20)
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        assert "schon einen Clip-Dialog" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip", welt.anna)))
        for uid in (CEM, DORA):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip", _person(uid, "X", welt.kanal))))
        assert len(welt.inst._dialoge[GID]) == 3
        assert "genug Clip-Dialoge" in asyncio.run(clips.handle(
            _nachricht(welt, "flo clip", welt.bob)))
        for dialog in welt.inst._dialoge[GID].values():
            dialog.erstellt -= clips.DIALOG_ABLAUF + 1
        asyncio.run(welt.inst.tick(laufzeit.client))
        assert GID not in welt.inst._dialoge


def test_clips_dialog_ohne_timeout_raeumt_der_takt_ganz_weg():
    """Kam der Timeout eines Dialogs nie an, macht der Takt nach DIALOG_ABLAUF
    dasselbe wie er: 'Zu langsam', Schnappschuss weg, Loesch-Schutz frei. Und
    das Abkuehlen vergisst, wer laengst wieder darf."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 40, 20)
        geschuetzt, frei = [], []
        welt.inst._zuletzt[CEM] = time.monotonic() - clips.ABKUEHLEN_SEK - 1
        with mock.patch.object(laufzeit, "protect_message", lambda m: geschuetzt.append(m.id)), \
                mock.patch.object(laufzeit, "release_message",
                                  lambda m, **_kw: frei.append(m.id)):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
            msg = welt.text.gesendet[-1]
            dialog = msg.view
            dialog.erstellt -= clips.DIALOG_ABLAUF + 1

            async def takt():
                await welt.inst.tick(laufzeit.client)
                await _nebenher(welt)

            asyncio.run(takt())
        assert msg.id in geschuetzt and msg.id in frei
        assert msg.edits[-1] == {"content": clips.TEXTE["zu_langsam"], "view": None}
        assert dialog.snap is None and dialog._fertig and GID not in welt.inst._dialoge
        assert CEM not in welt.inst._zuletzt


def test_clips_dialog_wer_zwischendurch_aussteigt_bekommt_nichts():
    """Steigt der Fragende zwischen Frage und Klick aus, gibt es keinen Clip -
    und geschnitten wird gar nicht erst."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, BOB, 40, 20)
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        msg = welt.text.gesendet[-1]
        dialog = msg.view
        assert "raus" in _frag(_nachricht(welt, "flo clip nein", welt.anna))
        knopf = [k for k in dialog.children if getattr(k, "label", "") == "30 s"][0]
        with _schnitt() as auftraege:
            asyncio.run(knopf.callback(_Klick(ANNA, gid=GID)))
        assert auftraege == [] and welt.ziel.gesendet == []
        assert "ausgestiegen" in msg.content


def test_clips_dialog_nach_mikro_aus_verworfen():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, BOB, 40, 20)
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        msg = welt.text.gesendet[-1]
        dialog = msg.view
        asyncio.run(clips.handle(_nachricht(welt, "flo clip aus", welt.bob)))
        knopf = [k for k in dialog.children if getattr(k, "label", "") == "30 s"][0]
        with _schnitt() as auftraege:
            asyncio.run(knopf.callback(_Klick(ANNA, gid=GID)))
        assert auftraege == [] and "inzwischen ausgemacht" in msg.content


# =============================================================================
# Der Schnitt und der Post
# =============================================================================
def _direkt(welt, text="flo clip 30", autor=None, *, erwaehnt=()):
    with mock.patch.object(clipstext, "kommentar", _kein_kommentar):
        return asyncio.run(clips.handle(_nachricht(welt, text, autor or welt.anna,
                                                   erwaehnt=erwaehnt)))


def test_clips_post_mit_datei_knoepfen_und_ohne_ping():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        _reden(welt, BOB, 20, 5, strang=2)
        _flo_spielt(welt, 30, 0)
        with _schnitt() as auftraege:
            assert _direkt(welt) is clips.HANDLED
        auftrag = auftraege[0]
        assert auftrag.dauer == 30.0 and auftrag.budget == int(9.5 * 1024 * 1024)
        assert {sp.uid for sp in auftrag.stimmen} == {ANNA, BOB} and auftrag.flo
        assert all(auftrag.bis - 31 <= p[0] <= auftrag.bis + 0.3
                   for sp in auftrag.stimmen for p in sp.pakete)
        assert auftrag.whisper is False and callable(auftrag.abbruch)
        post = welt.ziel.gesendet[-1]
        assert post.file.filename.startswith("flo-clip-") and post.file.filename.endswith(
            "-30s.mp3")
        assert post.file.fp.read() == b"ID3\x03" + bytes(2000)
        assert post.kw["allowed_mentions"].users is False
        assert post.kw["allowed_mentions"].everyone is False
        assert post.embed is None                    # ohne Transkript kein Embed
        assert isinstance(post.view, clips.ClipView)
        teile = post.content.split("\n")
        assert teile[0] == f"🎬 **Clip · 30 s** aus 🔊 **Zocken** · geclippt von <@{ANNA}>"
        assert teile[1].startswith("Mit dabei: ") and f"<@{BOB}>" in teile[1]
        assert teile[2].startswith("> ") and teile[2][2:] in clipstext.ERSATZ_KOMMENTARE
        assert teile[3].startswith("-# Nicht jeder ist in jedem Clip")
        eintrag = next(iter(welt.inst._clips_von(GID).values()))
        assert eintrag["mid"] == post.id and eintrag["cid"] == welt.ziel.id
        assert eintrag["vc"] == VOICE and eintrag["von"] == ANNA and eintrag["dauer"] == 30
        assert sorted(eintrag["sprecher"]) == [ANNA, BOB] and eintrag["stimmen"] == []
        antwort = welt.text.gesendet[-1]
        assert antwort.content == f"🎬 Clip ist raus: {post.jump_url}"


def test_clips_budget_haengt_an_der_upload_grenze():
    with _welt() as welt:
        assert welt.inst._budget(welt.guild) == int(9.5 * 1024 * 1024)
        welt.guild.filesize_limit = 8 * 1024 * 1024
        assert welt.inst._budget(welt.guild) == 8 * 1024 * 1024 - 512 * 1024
        welt.guild.filesize_limit = 100 * 1024 * 1024
        assert welt.inst._budget(welt.guild) == int(9.5 * 1024 * 1024)
        welt.guild.filesize_limit = None
        assert welt.inst._budget(welt.guild) == int(9.5 * 1024 * 1024)


def test_clips_leer_nur_musik_und_kuerzer():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        with _schnitt() as auftraege:
            assert _direkt(welt) is clips.HANDLED
            assert "Da war nix" in welt.text.gesendet[-1].content
            _flo_spielt(welt, 30, 0)
            welt.inst._zuletzt.clear()
            _direkt(welt)
            assert "nur meine Musik" in welt.text.gesendet[-1].content
            # Ein Raeuspern (unter 1 s) ist auch keine Stimme.
            _reden(welt, BOB, 5, 4.5)
            welt.inst._zuletzt.clear()
            _direkt(welt)
            assert "nur meine Musik" in welt.text.gesendet[-1].content
        assert auftraege == []
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(40)                             # erst 40 s zugehoert
        _reden(welt, BOB, 30, 5)
        with _schnitt() as auftraege:
            _direkt(welt, "flo clip 1m")
        assert auftraege[0].dauer == 40.0
        assert "Hab erst seit 40 s zugehört" in welt.text.gesendet[-1].content


def test_clips_einzelner_mensch_ohne_flo_und_stumm():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 20, 10)
        _flo_spielt(welt, 30, 0)
        welt.guild.leute[BOB] = welt.bob
        with _schnitt() as auftraege:
            _direkt(welt, "flo clip 30", erwaehnt=[welt.bob])
            assert "Von Bob hab ich in der Zeit nix gehört" in welt.text.gesendet[-1].content
            assert auftraege == []
            welt.inst._zuletzt.clear()
            _direkt(welt, "flo clip 30", erwaehnt=[welt.anna])
        assert [sp.uid for sp in auftraege[0].stimmen] == [ANNA] and auftraege[0].flo == []


def test_clips_notlage_bleibt_still_draussen():
    """Wer gerade am Boden ist, fehlt im Clip - ohne Begruendung. Fragt er
    selbst, ist er drin, aber Flo kommentiert nicht."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        _reden(welt, BOB, 20, 5, strang=2)
        basis.notlage_merken(BOB)
        with _schnitt() as auftraege:
            _direkt(welt)
        assert [sp.uid for sp in auftraege[0].stimmen] == [ANNA]
        post = welt.ziel.gesendet[-1]
        assert f"<@{BOB}>" not in post.content and "> " in post.content
        # Fragt Bob selbst: drin, aber kein Spruch.
        welt.inst._zuletzt.clear()
        with _schnitt() as auftraege:
            _direkt(welt, autor=welt.bob)
        assert {sp.uid for sp in auftraege[0].stimmen} == {ANNA, BOB}
        assert "\n> " not in welt.ziel.gesendet[-1].content


def _mit_transkript(welt, segmente, *, kommentar=None):
    """Whisper und KI gefakt: je_sprecher liefert 'segmente', kommentar() den Satz."""
    stapel = contextlib.ExitStack()

    async def je_sprecher(audio, frist=None):
        return segmente

    async def kommentieren(gid, zeilen, dauer):
        return kommentar

    stapel.enter_context(mock.patch.object(clipstext.transkriber, "an", lambda: True))
    stapel.enter_context(mock.patch.object(clipstext.transkriber, "je_sprecher", je_sprecher))
    stapel.enter_context(mock.patch.object(clipstext, "kommentar", kommentieren))

    def ergebnis(auftrag):
        assert auftrag.whisper is True
        return _ergebnis(auftrag, whisper={sp.uid: clipschnitt.WhisperAudio(
            mp3=b"x", karte=[(0.0, 5.0, 10.0)]) for sp in auftrag.stimmen})

    stapel.enter_context(_schnitt(ergebnis))
    return stapel


def test_clips_transkript_kommt_ins_embed_mit_kommentar():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        seg = {ANNA: [clipstext.Segment(0.5, 2.0, "Wer hat die Pizza gegessen?")]}
        with _mit_transkript(welt, seg, kommentar="Spannend wie Steuerrecht."):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
        post = welt.ziel.gesendet[-1]
        assert post.embed.title == "📝 Transkript (KI, ohne Gewähr)"
        assert "Anna" in post.embed.description and "Pizza" in post.embed.description
        assert "> Spannend wie Steuerrecht." in post.content


def test_clips_notsignal_im_transkript_haelt_den_clip_zurueck():
    """Ein Notsignal im Transkript zaehlt wie eins im Chat: der Clip bleibt
    liegen, basis merkt es sich - und nachhaken auch (alles Offene weg, keine
    Nachfrage wie 'na, wie lief die Pruefung?')."""
    import nachhaken
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, BOB, 25, 15)
        seg = {BOB: [clipstext.Segment(0.5, 2.0, "ich will nicht mehr leben")]}
        gemeldet = []
        with _mit_transkript(welt, seg, kommentar="nie"), \
                mock.patch.object(nachhaken, "notlage",
                                  lambda gid, uid: gemeldet.append((gid, uid))):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
        assert welt.ziel.gesendet == []
        assert "lass ich liegen" in welt.text.gesendet[-1].content
        assert basis.kuerzlich_notlage(BOB) is True
        assert gemeldet == [(GID, BOB)]
        assert not welt.inst._clips_von(GID)
    # Ohne nachhaken (nicht geladen) reicht basis - und nichts wirft.
    with mock.patch.dict(sys.modules, {"nachhaken": None}):
        alt = dict(basis._notlage_seit)
        try:
            clips._notlage_melden(GID, DORA)
            assert basis.kuerzlich_notlage(DORA) is True
        finally:
            basis._notlage_seit.clear()
            basis._notlage_seit.update(alt)


def test_clips_hetze_im_transkript_haelt_den_clip_zurueck():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, BOB, 25, 15)
        seg = {BOB: [clipstext.Segment(0.5, 2.0, "Ausländer raus")]}
        with _mit_transkript(welt, seg):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
        assert welt.ziel.gesendet == []
        assert "menschenfeindlicher Müll" in welt.text.gesendet[-1].content


def test_clips_private_daten_werden_geschwaerzt():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, BOB, 25, 15)
        seg = {BOB: [clipstext.Segment(0.5, 2.0, "Meine Nummer ist 0171 2345678")]}
        with _mit_transkript(welt, seg, kommentar="Toll."):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
        post = welt.ziel.gesendet[-1]
        assert clipstext.ZENSIERT in post.embed.description and "2345678" not in \
            post.embed.description


def test_clips_mikro_aus_oder_opt_out_waehrend_des_schnitts():
    """Waehrend ffmpeg rechnet, macht jemand das Mikro aus - oder steigt aus:
    der Clip wird verworfen, nichts geht raus."""
    for art in ("aus", "raus", "fragender_raus"):
        with _welt() as welt:
            asyncio.run(_starten(welt))
            welt.uhr.vor(60)
            _reden(welt, ANNA, 25, 15)
            _reden(welt, BOB, 20, 5, strang=2)

            def ergebnis(auftrag, art=art, welt=welt):
                if art == "aus":
                    _sitzung(welt).sperre_gen += 1
                    assert auftrag.abbruch() is True
                elif art == "raus":
                    welt.inst.raus(BOB)
                else:
                    welt.inst.raus(ANNA)
                return _ergebnis(auftrag)

            with _schnitt(ergebnis):
                _direkt(welt)
            assert welt.ziel.gesendet == [], art
            text = welt.text.gesendet[-1].content
            assert ("inzwischen ausgemacht" if art == "aus" else "ausgestiegen") in text, art


def test_clips_nach_dem_ausstieg_geht_kein_ton_zu_whisper():
    """Steigt jemand aus dem Clip aus (oder geht das Mikro aus), waehrend ffmpeg
    rechnet, geht sein Ton nicht mehr zu Groq: verworfen wird VOR dem
    Transkript, nicht erst vor dem Post."""
    for art in ("raus", "aus"):
        with _welt(transkript=True) as welt:
            asyncio.run(_starten(welt))
            welt.uhr.vor(60)
            _reden(welt, ANNA, 25, 15)
            _reden(welt, BOB, 20, 5, strang=2)
            geschickt = []

            async def je_sprecher(audio, frist=None):
                geschickt.append(sorted(audio))
                return {}

            def ergebnis(auftrag, art=art, welt=welt):
                if art == "raus":
                    welt.inst.raus(BOB)                 # mitten im Schnitt
                else:
                    _sitzung(welt).sperre_gen += 1      # Mikro aus mitten im Schnitt
                return _ergebnis(auftrag, whisper={sp.uid: clipschnitt.WhisperAudio(
                    mp3=b"x", karte=[(0.0, 5.0, 10.0)]) for sp in auftrag.stimmen})

            with mock.patch.object(clipstext.transkriber, "je_sprecher", je_sprecher), \
                    mock.patch.object(clipstext.transkriber.kontingent, "gesperrt",
                                      lambda: False), \
                    _schnitt(ergebnis) as auftraege:
                _direkt(welt)
            assert auftraege and auftraege[0].whisper is True, art
            assert geschickt == [] and welt.ziel.gesendet == [], art
            text = welt.text.gesendet[-1].content
            assert ("ausgestiegen" if art == "raus" else "inzwischen ausgemacht") in text, art


def test_clips_ausstieg_waehrend_des_spruchs_kommt_nicht_in_den_post():
    """Der KI-Spruch darf bis zu clipstext.KI_FRIST dauern - genau dann drueckt
    Bob 🙅, jemand 🔇, Bob schreibt ein Notsignal oder die Sendepause beginnt.
    Gepostet wird dann nichts: die letzte Pruefung steht unmittelbar vor dem
    Hochladen, nicht vor dem Spruch."""
    erwartet = {"raus": "ausgestiegen", "aus": "inzwischen ausgemacht",
                "notlage": "lass ich liegen", "sendepause": "Sendepause"}
    for art, text in erwartet.items():
        with _welt(transkript=True) as welt:
            asyncio.run(_starten(welt))
            welt.uhr.vor(60)
            _reden(welt, ANNA, 25, 15)
            _reden(welt, BOB, 20, 5, strang=2)
            seg = {ANNA: [clipstext.Segment(0.5, 2.0, "Wer hat die Pizza gegessen?")],
                   BOB: [clipstext.Segment(6.0, 7.0, "Ich nicht.")]}
            pause = []

            async def kommentieren(gid, zeilen, dauer, art=art, welt=welt, pause=pause):
                if art == "raus":
                    welt.inst.raus(BOB)
                elif art == "aus":
                    welt.inst._mikro_aus_sofort(welt.guild, VOICE)
                elif art == "notlage":
                    basis.notlage_merken(BOB)
                else:
                    pause.append(True)
                await asyncio.sleep(0)
                return "Spannend wie Steuerrecht."

            with _mit_transkript(welt, seg), \
                    mock.patch.object(clipstext, "kommentar", kommentieren), \
                    mock.patch.object(clips, "_sendepause", lambda pause=pause: bool(pause)):
                asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
            assert welt.ziel.gesendet == [], art
            assert text in welt.text.gesendet[-1].content, (art, welt.text.gesendet[-1].content)
            assert not welt.inst._clips_von(GID), art


def test_clips_ausstieg_waehrend_des_hochladens_nimmt_den_post_zurueck():
    """Auch das Hochladen dauert (ein paar MB): steigt Bob genau dann aus, ist
    der Post gleich wieder weg - ohne Eintrag im Index. Laesst er sich nicht
    loeschen, bleibt er wie jeder Clip im Index (🗑️ geht dann trotzdem)."""
    for loeschen_geht in (True, False):
        with _welt() as welt:
            asyncio.run(_starten(welt))
            welt.uhr.vor(60)
            _reden(welt, ANNA, 25, 15)
            _reden(welt, BOB, 20, 5, strang=2)
            echt = welt.ziel.send

            async def senden(content=None, echt=echt, welt=welt, geht=loeschen_geht, **kw):
                msg = await echt(content, **kw)
                welt.inst.raus(BOB)              # 🙅, waehrend die MP3 hochlaedt
                if not geht:
                    async def kaputt():
                        raise discord.Forbidden(SimpleNamespace(status=403, reason="nein"),
                                                "nein")
                    msg.delete = kaputt
                return msg

            welt.ziel.send = senden
            from test_fundament import _Mitschnitt
            with _schnitt(), _Mitschnitt("dcbot.clips"):
                _direkt(welt)
            post = welt.ziel.gesendet[-1]
            if loeschen_geht:
                assert post.geloescht and not welt.inst._clips_von(GID)
                assert "ausgestiegen" in welt.text.gesendet[-1].content
            else:
                assert not post.geloescht and welt.inst._clips_von(GID)
                assert "Clip ist raus" in welt.text.gesendet[-1].content


def test_clips_schnitt_scheitert():
    from test_fundament import _Mitschnitt
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        with _schnitt(fehler=clipschnitt.RenderFehler("ffmpeg", "kaputt")), \
                _Mitschnitt("dcbot.clips") as mit:
            _direkt(welt)
        assert "ffmpeg abgeschmiert" in welt.text.gesendet[-1].content
        assert [x.getMessage() for x in mit.ab(logging.WARNING)] == [
            f"Clips: Server {GID} – Schnitt gescheitert (ffmpeg)."]
        welt.inst._zuletzt.clear()
        with _schnitt(fehler=clipschnitt.RenderFehler("leer")):
            _direkt(welt)
        assert "Da war nix" in welt.text.gesendet[-1].content
        assert welt.inst._in_arbeit == 0


def test_clips_technisch_gescheitert_kostet_kein_abkuehlen():
    """'Nochmal versuchen' muss auch gehen: scheitert der Schnitt an ffmpeg oder
    lehnt Discord den Upload ab, zaehlt der Versuch weder fuers Abkuehlen noch
    fuer die Stunde. Ein leerer Schnitt zaehlt - Stille schneidet man nicht im Takt."""
    from test_fundament import _Mitschnitt
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        with _schnitt(fehler=clipschnitt.RenderFehler("ffmpeg", "kaputt")), \
                _Mitschnitt("dcbot.clips"):
            _direkt(welt)
        assert "ffmpeg abgeschmiert" in welt.text.gesendet[-1].content
        assert ANNA not in welt.inst._zuletzt and not welt.inst._stunde.get(GID)
        welt.ziel.kaputt = discord.HTTPException(SimpleNamespace(status=413, reason="zu gross"),
                                                 "Request entity too large")
        with _schnitt(), _Mitschnitt("dcbot.clips"):
            _direkt(welt)
        assert "wollte den Clip nicht" in welt.text.gesendet[-1].content
        assert ANNA not in welt.inst._zuletzt and not welt.inst._stunde.get(GID)
        welt.ziel.kaputt = None
        with _schnitt() as auftraege:
            assert _direkt(welt) is clips.HANDLED
        assert len(auftraege) == 1 and len(welt.ziel.gesendet) == 1
        assert ANNA in welt.inst._zuletzt and len(welt.inst._stunde[GID]) == 1
        # Ein leerer Schnitt zaehlt.
        welt.inst._zuletzt.clear()
        with _schnitt(fehler=clipschnitt.RenderFehler("leer")):
            _direkt(welt)
        assert "Da war nix" in welt.text.gesendet[-1].content
        assert ANNA in welt.inst._zuletzt and len(welt.inst._stunde[GID]) == 2


def test_clips_zwischenmeldung_bleibt_bis_zum_ergebnis_geschuetzt():
    """In einem Aufraeum-Kanal loescht die Freigabe eine Nachricht nach der
    Gnadenfrist. Dialog und 'Schneide ...' werden deshalb erst freigegeben,
    wenn das Ergebnis drinsteht - nicht schon beim Klick."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 40, 20)
        schutz, frei = set(), {}

        def schuetzen(m):
            schutz.add(m.id)

        def freigeben(m, **_kw):
            frei.setdefault(m.id, []).append(m.content)

        with mock.patch.object(laufzeit, "protect_message", schuetzen), \
                mock.patch.object(laufzeit, "release_message", freigeben), \
                mock.patch.object(clipstext, "kommentar", _kein_kommentar), _schnitt():
            asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
            msg = welt.text.gesendet[-1]
            knopf = [k for k in msg.view.children if getattr(k, "label", "") == "30 s"][0]
            asyncio.run(knopf.callback(_Klick(ANNA, gid=GID)))
            # Genau EINE Freigabe - und da stand das Ergebnis schon drin.
            assert msg.id in schutz and len(frei[msg.id]) == 1, frei
            assert frei[msg.id][0].startswith("🎬 Clip ist raus"), frei
            # Direkt ('flo clip 30'): die Zwischenmeldung ebenso.
            welt.inst._zuletzt.clear()
            asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna)))
            antwort = welt.text.gesendet[-1]
            assert antwort is not msg and antwort.id in schutz and len(frei[antwort.id]) == 1
            assert frei[antwort.id][0].startswith("🎬 Clip ist raus"), frei


def test_clips_zielkanal_privat_und_ohne_rechte():
    """Privater Voice: der Clip bleibt in dessen Voice-Chat (oder es gibt
    keinen). Kein Clip-Kanal mit Datei-Recht: klare Ansage an den Admin."""
    def privat_machen(welt):
        jeder = SimpleNamespace(id=1, managed=False, _rolle=True)
        mitglied = SimpleNamespace(id=2, managed=False, _rolle=True)
        welt.guild.roles = [jeder, mitglied]
        welt.guild.default_role = jeder
        welt.kanal._lesen = {2}                      # nur Mitglieder sehen den Voice

        def ziel_rechte(wer):
            if getattr(wer, "_rolle", False):
                return SimpleNamespace(read_messages=True)
            return SimpleNamespace(send_messages=True, attach_files=True, view_channel=True)
        welt.ziel.permissions_for = ziel_rechte

    with _welt() as welt:
        privat_machen(welt)
        asyncio.run(_starten(welt))
        assert "diesen Voice-Chat" in welt.kanal.gesendet[0].content
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        with _schnitt():
            _direkt(welt)
        assert welt.ziel.gesendet == []
        post = welt.kanal.gesendet[-1]
        assert post.file is not None and isinstance(post.view, clips.ClipView)
        # Gefragt wurde in #allgemein - dort steht nur der Verweis, kein "hier".
        antwort = welt.text.gesendet[-1].content
        assert "im Voice-Chat eures Voice" in antwort and "hier unter euch" not in antwort
        # Im Voice-Chat selbst gefragt: "hier unter euch".
        welt.inst._zuletzt.clear()
        with _schnitt(), mock.patch.object(clipstext, "kommentar", _kein_kommentar):
            asyncio.run(clips.handle(_nachricht(welt, "flo clip 30", welt.anna,
                                                kanal=welt.kanal)))
        assert any("hier unter euch" in (m.content or "") for m in welt.kanal.gesendet)
        # Darf Flo dort keine Dateien hochladen: kein Clip.
        welt.kanal._dateien = False
        welt.inst._zuletzt.clear()
        with _schnitt():
            _direkt(welt)
        assert "Also kein Clip" in welt.text.gesendet[-1].content
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        welt.ziel.dateien = False
        with _schnitt() as auftraege:
            _direkt(welt)
        assert "keine Dateien hochladen" in welt.text.gesendet[-1].content
        welt.ziel.schreiben = False
        welt.inst._zuletzt.clear()
        with _schnitt():
            _direkt(welt)
        assert "einstellung clip_channel" in welt.text.gesendet[-1].content
        assert len(auftraege) == 1


def test_clips_dialog_fuer_einen_privaten_voice_zeigt_draussen_keine_namen():
    """Wer in einem oeffentlichen Kanal 'flo clip' fuer einen PRIVATEN Voice
    sagt: das Menue zeigte allen dort, wer im Voice redet (aufklappen kann es
    jeder, nur waehlen nicht). Also nur 'Alle' - und wen der Fragende selbst
    schon genannt hat. Im Voice-Chat selbst gibt es die volle Liste."""
    with _welt() as welt:
        jeder = SimpleNamespace(id=1, managed=False, _rolle=True)
        mitglied = SimpleNamespace(id=2, managed=False, _rolle=True)
        welt.guild.roles = [jeder, mitglied]
        welt.guild.default_role = jeder
        welt.kanal._lesen = {2}
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        _reden(welt, BOB, 20, 5, strang=2)

        def optionen(msg):
            return [o.label for teil in msg.components for k in getattr(teil, "children", [teil])
                    for o in (getattr(k, "options", None) or ())]

        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna)))
        draussen = welt.text.gesendet[-1]
        assert draussen.view.auswahl is not None and optionen(draussen) == ["Alle"]
        assert "Bob" not in draussen.content and "privat" in draussen.content
        welt.inst._dialoge.clear()
        # Den selbst genannten Bob darf es zeigen - der steht ja schon da.
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna, erwaehnt=[welt.bob])))
        assert optionen(welt.text.gesendet[-1]) == ["Alle", "Bob"]
        welt.inst._dialoge.clear()
        asyncio.run(clips.handle(_nachricht(welt, "flo clip", welt.anna, kanal=welt.kanal)))
        drinnen = welt.kanal.gesendet[-1]
        assert sorted(optionen(drinnen)) == ["Alle", "Anna", "Bob"]


def test_clips_nie_in_einen_aufraeum_kanal():
    """Steht der Clip-Kanal auf der Aufraeum-Liste, nimmt Flo ihn nicht."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        original = guildcfg.get

        def get(gid, key):
            if key == "autodelete_channels":
                return [guildcfg.CLIP_CHANNEL]
            return original(gid, key)

        with mock.patch.object(guildcfg, "get", get), _schnitt():
            _direkt(welt)
        assert welt.ziel.gesendet == []
        assert "einstellung clip_channel" in welt.text.gesendet[-1].content


def test_clips_upload_abgelehnt():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        welt.ziel.kaputt = discord.HTTPException(SimpleNamespace(status=413, reason="zu gross"),
                                                 "Request entity too large")
        from test_fundament import _Mitschnitt
        with _schnitt(), _Mitschnitt("dcbot.clips") as mit:
            _direkt(welt)
        assert "wollte den Clip nicht" in welt.text.gesendet[-1].content
        assert not welt.inst._clips_von(GID)
        assert [x.getMessage() for x in mit.ab(logging.WARNING)] == [
            f"Clips: Server {GID} – Upload abgelehnt (413)."]


# =============================================================================
# Flammen, Muelleimer, Bestenliste, Status
# =============================================================================
def _ein_clip(welt):
    asyncio.run(_starten(welt))
    welt.uhr.vor(60)
    _reden(welt, ANNA, 25, 15)
    _reden(welt, BOB, 20, 5, strang=2)
    with _schnitt():
        _direkt(welt)
    post = welt.ziel.gesendet[-1]
    cid = next(iter(welt.inst._clips_von(GID)))
    return post, cid


def test_clips_flammen():
    with _welt() as welt:
        post, cid = _ein_clip(welt)
        feuer = f"flo:clip:feuer:{cid}"
        selbst = _Klick(ANNA, gid=GID)
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, feuer, selbst)
        assert "Eigenlob stinkt" in selbst.protokoll[0][1]
        klick = _Klick(CEM, gid=GID)
        assert _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, feuer, klick) == 1
        art, kw = klick.protokoll[0]
        assert art == "edit" and isinstance(kw["view"], clips.ClipView)
        assert kw["view"].children[0].item.label == "1"
        assert welt.inst._clips_von(GID)[cid]["stimmen"] == [CEM]
        nochmal = _Klick(CEM, gid=GID)
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, kw["view"], feuer, nochmal)
        assert nochmal.protokoll[0][1]["view"].children[0].item.label == "0"
        assert welt.inst._clips_von(GID)[cid]["stimmen"] == []
        # Unbekannter Clip; Clips aus.
        weg = _Klick(CEM, gid=GID)
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, clips.ClipView("zzzzzzzz"),
                           "flo:clip:feuer:zzzzzzzz", weg)
        assert "aus der Wertung" in weg.protokoll[0][1]
        with mock.patch.object(features, "is_on_in", lambda g, k: False):
            aus = _Klick(CEM, gid=GID)
            _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, feuer, aus)
        assert "aus" in aus.protokoll[0][1]


def test_clips_muelleimer_nur_fuer_beteiligte_und_mods():
    with _welt() as welt:
        post, cid = _ein_clip(welt)
        weg = f"flo:clip:weg:{cid}"

        def klick_mit_loeschen(uid, **kw):
            klick = _Klick(uid, gid=GID, **kw)
            klick.geloescht = []

            async def loeschen():
                klick.geloescht.append(True)
            klick.message.delete = loeschen
            return klick

        fremd = klick_mit_loeschen(CEM)
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, weg, fremd)
        assert "Nicht dein Clip" in fremd.protokoll[0][1] and not fremd.geloescht
        bob = klick_mit_loeschen(BOB)                  # spricht im Clip
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, weg, bob)
        assert "Weg ist er" in bob.protokoll[0][1] and bob.geloescht == [True]
        assert cid not in welt.inst._clips_von(GID)
        # Mods duerfen immer - auch wenn der Index den Clip nicht mehr kennt.
        mod = klick_mit_loeschen(DORA, member=_person(DORA, "Dora", mod=True))
        with mock.patch.object(features, "is_on_in", lambda g, k: False):
            _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, weg, mod)
        assert mod.geloescht == [True]


def test_clips_muelleimer_auch_wenn_der_index_den_clip_vergessen_hat():
    """Nach CLIPS_TAGE fliegt der Clip aus dem Index - der Post steht aber
    weiter auf Discord. Wer drin ist (und wer geclippt hat), darf ihn trotzdem
    loeschen: das steht im Post selbst. Ein Fremder nicht. Und 🔥 sagt nicht
    'gibt's nicht mehr' ueber einen Clip, der sichtbar dasteht."""
    with _welt() as welt:
        post, cid = _ein_clip(welt)
        assert f"<@{BOB}>" in post.content
        assert welt.inst._aufraeumen(jetzt=time.time() + (clips.CLIPS_TAGE + 1) * 86400) >= 1
        assert cid not in welt.inst._clips_von(GID)
        weg = f"flo:clip:weg:{cid}"

        def klick_auf_den_post(uid):
            klick = _Klick(uid, gid=GID)
            klick.message.content = post.content
            klick.geloescht = []

            async def loeschen():
                klick.geloescht.append(True)
            klick.message.delete = loeschen
            return klick

        fremd = klick_auf_den_post(CEM)
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, weg, fremd)
        assert "Nicht dein Clip" in fremd.protokoll[0][1] and not fremd.geloescht
        for uid in (BOB, ANNA):                       # im Clip / hat geclippt
            klick = klick_auf_den_post(uid)
            _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, weg, klick)
            assert "Weg ist er" in klick.protokoll[0][1] and klick.geloescht == [True], uid
        feuer = _Klick(CEM, gid=GID)
        _dynamisch_klicken(clips.DYNAMISCHE_KNOEPFE, post.view, f"flo:clip:feuer:{cid}", feuer)
        assert "aus der Wertung" in feuer.protokoll[0][1] and "5 Wochen" in feuer.protokoll[0][1]
    # Wer drin ist, liest clips nur aus Flos eigenen Zeilen - ein '@' im
    # Kanalnamen ist dort schon entschaerft und zaehlt nie.
    kopf = clips.TEXTE["post_kopf"].format(
        dauer="30 s", voice=clips._sauber("· geclippt von <@999>"), von=f"<@{ANNA}>")
    text = "\n".join([kopf, f"Mit dabei: <@{BOB}>, <@{CEM}>", "> Kunst.",
                      clips.TEXTE["post_fuss"].format(name="flo")])
    assert clips._beteiligte_im_post(text) == {ANNA, BOB, CEM}
    assert clips._beteiligte_im_post(f"{kopf}\nNur <@{DORA}>.") == {ANNA, DORA}
    assert clips._beteiligte_im_post("") == set()


def test_clips_bestenliste_alle_ist_ehrlich_und_privates_bleibt_privat():
    """'flo clips alle' reicht so weit wie der Index (5 Wochen) - und sagt
    das auch. Clips aus dem Voice-Chat eines privaten Voice (oder aus einem
    Kanal, den es nicht mehr gibt) tauchen in einem oeffentlichen Kanal nicht
    auf, auch nicht als 'von wem, wann, wie viele 🔥'."""
    with _welt() as welt:
        leer = asyncio.run(clips.handle(_nachricht(welt, "flo clips alle", welt.anna)))
        assert leer.title == "🔥 Die besten Clips der letzten 5 Wochen"
        assert "35 Tagen" in leer.description and "Jahr" not in leer.title
        jeder = SimpleNamespace(id=1, managed=False, _rolle=True)
        mitglied = SimpleNamespace(id=2, managed=False, _rolle=True)
        welt.guild.roles = [jeder, mitglied]
        welt.guild.default_role = jeder
        welt.kanal._lesen = {2}                      # nur Mitglieder sehen den Voice
        welt.ziel.permissions_for = lambda wer: SimpleNamespace(
            read_messages=True, send_messages=True, attach_files=True, view_channel=True)
        welt.text.permissions_for = welt.ziel.permissions_for
        index = welt.inst._clips_von(GID, anlegen=True)
        jetzt = time.time()
        index["oeffentl"] = {"mid": 21, "cid": welt.ziel.id, "vc": VOICE, "ts": jetzt - 60,
                             "von": ANNA, "dauer": 30, "sprecher": [ANNA], "stimmen": [CEM]}
        index["privat00"] = {"mid": 22, "cid": VOICE, "vc": VOICE, "ts": jetzt - 30,
                             "von": BOB, "dauer": 30, "sprecher": [BOB], "stimmen": [CEM, DORA]}
        index["kanalweg"] = {"mid": 23, "cid": 4711, "vc": VOICE, "ts": jetzt - 30,
                             "von": BOB, "dauer": 30, "sprecher": [BOB], "stimmen": [CEM]}
        for zeitraum in ("", " monat", " alle"):
            brett = asyncio.run(clips.handle(_nachricht(welt, "flo clips" + zeitraum,
                                                        welt.anna)))
            assert "/21" in brett.description, zeitraum
            assert "/22" not in brett.description and f"<@{BOB}>" not in brett.description
            assert "/23" not in brett.description
        # Im Voice-Chat des privaten Voice selbst ist er dagegen kein Geheimnis.
        im_voice = asyncio.run(clips.handle(_nachricht(welt, "flo clips", welt.anna,
                                                       kanal=welt.kanal)))
        assert "/22" in im_voice.description


def test_clips_bestenliste():
    with _welt() as welt:
        index = welt.inst._clips_von(GID, anlegen=True)
        jetzt = time.time()
        ziel = welt.ziel.id
        index["aaaaaaaa"] = {"mid": 11, "cid": ziel, "vc": VOICE, "ts": jetzt - 3600, "von": ANNA,
                             "dauer": 30, "sprecher": [ANNA], "stimmen": [1, 2, 3]}
        index["bbbbbbbb"] = {"mid": 12, "cid": ziel, "vc": VOICE, "ts": jetzt - 20 * 86400,
                             "von": BOB, "dauer": 60, "sprecher": [BOB], "stimmen": [1]}
        index["cccccccc"] = {"mid": 13, "cid": ziel, "vc": VOICE, "ts": jetzt, "von": BOB,
                             "dauer": 60, "sprecher": [BOB], "stimmen": []}
        woche = asyncio.run(clips.handle(_nachricht(welt, "flo clips", welt.anna)))
        assert woche.title == "🔥 Clips der Woche" and "/11" in woche.description
        assert "/12" not in woche.description and "/13" not in woche.description
        monat = asyncio.run(clips.handle(_nachricht(welt, "flo clips monat", welt.anna)))
        assert "/11" in monat.description and "/12" in monat.description
        assert monat.description.index("/11") < monat.description.index("/12")


def test_clips_status_nur_fuer_mods_und_nur_zahlen():
    with _welt() as welt:
        asyncio.run(_starten(welt))
        welt.uhr.vor(60)
        _reden(welt, ANNA, 25, 15)
        assert "Das ist für Mods" in asyncio.run(clips.handle(_nachricht(welt, "flo clip status",
                                                                         welt.anna)))
        mod = _person(DORA, "Dora", mod=True)
        emb = asyncio.run(clips.handle(_nachricht(welt, "flo clip status", mod)))
        assert isinstance(emb, discord.Embed)
        text = _embed_text(emb)
        for feld in ("Sitzung", "Verbindung", "Pakete (60 s)", "Leute", "Puffer",
                     "Schnitt & Transkript"):
            assert feld in text, feld
        assert "läuft seit" in text and "_Client" in text and "aead_xchacha20" in text
        assert "Anna" not in text and "Bob" not in text and str(ANNA) not in text
        welt.inst.raus(BOB)
        assert "ausgestiegen 1" in _embed_text(asyncio.run(clips.handle(
            _nachricht(welt, "flo clip status", mod))))


def test_clips_hilfe_und_fehlertexte():
    with _welt() as welt:
        hilfe = asyncio.run(clips.handle(_nachricht(welt, "flo clip hilfe", welt.anna)))
        assert "`flo clip`" in hilfe.lower() and "`flo clips`" in hilfe.lower()
        assert "Nicht drei" in asyncio.run(clips.handle(_nachricht(
            welt, "flo clip 30", welt.anna, erwaehnt=[welt.bob, _person(CEM, "Cem")])))
        assert "Bots clip ich nicht" in asyncio.run(clips.handle(_nachricht(
            welt, "flo clip 30", welt.anna, erwaehnt=[_person(CEM, "Robo", bot=True)])))
        assert "5 Minuten" in asyncio.run(clips.handle(_nachricht(welt, "flo clip 10m",
                                                                  welt.anna)))
        assert asyncio.run(clips.handle(_nachricht(welt, "flo clip ist cool", welt.anna))) \
            is None


# =============================================================================
# Puffer
# =============================================================================
def test_clips_ringpuffer_deckel_und_trimmen():
    konto = clips._Konto()
    puffer = clips.Ringpuffer(GID, VOICE, konto=konto, max_bytes=10_000, uhr_=lambda: 0.0)
    for k in range(100):
        puffer.einfuegen(ANNA, 1, k, k * 960, _STIMME, float(k))
    assert puffer.bytes <= 10_000 and konto.bytes == puffer.bytes
    aeltestes = puffer.spuren[ANNA].pakete[0][0]
    assert aeltestes > 0                               # das Aelteste flog zuerst
    puffer.trimmen(100 + clips.HALTEN_SEK)
    assert all(p[0] >= 100 for s in puffer.spuren.values() for p in s.pakete)
    # Hoechstens SPRECHER_MAX Sprecher: wer am laengsten still ist, fliegt.
    puffer = clips.Ringpuffer(GID, VOICE, uhr_=lambda: 0.0)
    for uid in range(clips.SPRECHER_MAX + 1):
        puffer.einfuegen(1000 + uid, 1, 1, 960, _STIMME, float(uid))
    assert len(puffer.spuren) == clips.SPRECHER_MAX and 1000 not in puffer.spuren
    puffer.leeren()
    assert puffer.bytes == 0 and not puffer.spuren and not puffer.eigen
    # Der Gesamtdeckel ueber alle Server.
    konto = clips._Konto()
    konto.bytes = clips.GESAMT_BYTES
    puffer = clips.Ringpuffer(GID, VOICE, konto=konto, uhr_=lambda: 0.0)
    puffer.einfuegen(ANNA, 1, 1, 960, _STIMME, 1.0)
    assert not puffer.spuren and konto.bytes == clips.GESAMT_BYTES


def test_clips_ringpuffer_kennt_namen_vom_ersten_paket():
    """Ohne Members-Intent kennt der Cache nur, wer gerade im Voice ist: der
    Name wird beim ERSTEN Paket gemerkt."""
    gefragt = []

    def kennen(uid):
        gefragt.append(uid)
        return ("Anna", False)

    puffer = clips.Ringpuffer(GID, VOICE, kennen=kennen, uhr_=lambda: 0.0)
    for k in range(5):
        puffer.einfuegen(ANNA, 1, k, k * 960, _STIMME, float(k))
    assert gefragt == [ANNA]
    snap = puffer.schnappschuss(10.0)
    assert snap.stimmen[ANNA][0] == "Anna" and len(snap.stimmen[ANNA][2]) == 5
    assert puffer.stimm_sek(ANNA, 0, 10) == 0.1


# =============================================================================
# Einbau in music und voicegags
# =============================================================================
def test_clips_verbindungs_optionen():
    """Ohne clips (nicht geladen oder aus): EXAKT die Flags. Mit clips: dazu
    die Empfangs-Klasse."""
    with mock.patch.dict(sys.modules, {"clips": None}):
        assert music.verbindungs_optionen(self_deaf=True, reconnect=True) == {
            "self_deaf": True, "reconnect": True}
    frisch = clips.Clips()
    with mock.patch.object(clips, "instance", frisch):
        assert music.verbindungs_optionen(**music._VOICE_FLAGS) == music._VOICE_FLAGS
        frisch._enabled = True
        assert music.verbindungs_optionen(self_deaf=False) == {
            "self_deaf": False, "cls": voiceempfang.EmpfangsClient}

        def kaputt():
            raise RuntimeError("x")
        frisch.empfangs_klasse = kaputt
        assert music.verbindungs_optionen(self_deaf=True) == {"self_deaf": True}


def test_clips_alle_wege_in_den_voice_geben_die_klasse_nur_mit_clips():
    """Musik (_verbinden), Soundboard und TTS verbinden alle ueber
    verbindungs_optionen - mit clips aus exakt wie vorher."""
    import voicegags
    frisch = clips.Clips()
    for an in (False, True):
        frisch._enabled = an
        extra = {"cls": voiceempfang.EmpfangsClient} if an else {}
        with mock.patch.object(clips, "instance", frisch):
            gesehen = []

            async def connect(**kw):
                gesehen.append(kw)
                return SimpleNamespace(is_connected=lambda: True)

            kanal = SimpleNamespace(connect=connect)
            asyncio.run(music.GuildPlayer._verbinden(kanal))
            asyncio.run(music.GuildPlayer._verbinden(kanal, timeout=5))
            assert gesehen == [{**music._VOICE_FLAGS, **extra},
                               {"timeout": 5, **music._VOICE_FLAGS, **extra}], gesehen
            # voicegags: Soundboard (nicht taub) und Datei-Sound/TTS (taub).
            vg = voicegags.VoiceGags()
            verbunden = []

            class _Kanal:
                id = 42
                name = "voice42"

                def permissions_for(self, _wer):
                    return SimpleNamespace(use_soundboard=True, speak=True)

                async def send_sound(self, _s):
                    pass

                async def connect(self, **kw):
                    verbunden.append(kw)
                    return SimpleNamespace(channel=self, is_connected=lambda: True,
                                           is_playing=lambda: False, is_paused=lambda: False)

            async def zustand(**_kw):
                pass

            gilde = SimpleNamespace(id=77, voice_client=None, change_voice_state=zustand,
                                    me=SimpleNamespace(voice=None, guild_permissions=SimpleNamespace(
                                        use_soundboard=True, speak=True)))
            with mock.patch.object(vg, "_spawn", lambda coro: coro.close()):
                asyncio.run(vg._discord_sound_spielen(gilde, _Kanal(), SimpleNamespace(name="x")))
            assert verbunden == [{"self_deaf": False, **extra}], verbunden
            verbunden.clear()
            with mock.patch.object(music, "is_voice_busy", lambda _g: False):
                async def spielen():
                    try:
                        await vg._play_path(gilde, _Kanal(), "nirgends.mp3")
                    except Exception:  # noqa: BLE001 - das Abspielen selbst ist egal
                        pass
                asyncio.run(spielen())
            assert verbunden and verbunden[0] == {"self_deaf": True, **extra}, verbunden


def test_clips_leerlauf_mit_clip_halten():
    """'flo clip an': mit Menschen bleibt Flo (und der Watchdog holt ihn
    zurueck); sitzen nur Bots drin, geht er nach MUSIC_IDLE_SEKUNDEN."""
    from testhilfe import _voice_kanal
    with _welt():
        spieler = music.GuildPlayer(loop=None, guild_id=GID)
        spieler.active_channel_id = 42
        spieler.clip_halten = True
        assert asyncio.run(spieler._leerlauf(_voice_kanal(2, 1))) is False
        assert spieler._leer_seit is None
        # Ohne clip_halten: die Leerlauf-Uhr laeuft wie immer.
        spieler.clip_halten = False
        assert asyncio.run(spieler._leerlauf(_voice_kanal(2, 1))) is True
        assert spieler._leer_seit is not None
        # Nur Bots: auch mit clip_halten laeuft die Uhr - und nach der Frist geht er.
        spieler.clip_halten = True
        spieler._leer_seit = None
        getrennt = []

        async def disconnect(**_kw):
            getrennt.append(True)

        spieler.disconnect = disconnect
        assert asyncio.run(spieler._leerlauf(_voice_kanal(0, 1))) is True
        spieler._leer_seit -= music.MUSIC_IDLE_SEKUNDEN + 1
        assert asyncio.run(spieler._leerlauf(_voice_kanal(0, 1))) is True and getrennt == [True]


def test_clips_halten_nur_solange_clips_dort_mithoeren_darf():
    """'flo clip an' haelt Flo nur, solange clips in dem Kanal mithoeren DARF -
    ein liegengebliebener Schalter haelt keinen Flo fest, der ohnehin nicht
    zuhoert (Mikro aus, vom Server taub, Ansage gescheitert, Einwilligung oder
    Clips aus). Dann laeuft die normale Leerlauf-Uhr."""
    def gesperrt_durch(art, welt):
        if art == "aus":
            welt.inst._daten("aus")[str(VOICE)] = round(time.time())
        elif art == "server_taub":
            welt.guild.me.voice.deaf = True
        elif art == "ansage":
            _sitzung(welt).ansage_fehlt = (VOICE, time.monotonic() + 600)
        elif art == "ansage_weg":
            _sitzung(welt).ansage_fehlt = (VOICE, time.monotonic() + 600, "ansage_weg")
        elif art == "server":
            welt.schalter["aufnahme"] = False
        elif art == "clips_aus":
            welt.inst._enabled = False

    for art in ("frei", "aus", "server_taub", "ansage", "ansage_weg", "server", "clips_aus",
                "sendepause"):
        with _welt() as welt:
            welt.spieler.clip_halten = True
            stapel = contextlib.ExitStack()
            if art == "sendepause":
                stapel.enter_context(mock.patch.object(clips, "_sendepause", lambda: True))
            elif art != "frei":
                gesperrt_durch(art, welt)
            with stapel:
                bleibt = asyncio.run(welt.spieler._leerlauf(welt.kanal)) is False
            assert bleibt is (art == "frei"), art
            assert (welt.spieler._leer_seit is None) is (art == "frei"), art


def test_clips_parken_und_verschwundener_kanal_nehmen_clip_an_zurueck():
    """Gibt der Watchdog auf (_parken) oder ist der Kanal weg, endet die
    Sitzung ohne disconnect() - 'flo clip an' endet mit ihr. Die naechste
    Sitzung ('weiter', 'spiel') erbt es nicht: nach dem letzten Song geht Flo
    wieder nach MUSIC_IDLE_SEKUNDEN."""
    with _welt() as welt:
        spieler = welt.spieler
        spieler.clip_halten = True
        with mock.patch.object(music, "_retire_panel", lambda _p: _als_nichts()), \
                mock.patch.object(music, "_zustand_merken", lambda *_a: None):
            asyncio.run(spieler._parken(welt.kanal))
        assert spieler.active_channel_id is None and spieler.clip_halten is False
        # Die naechste Sitzung im selben Kanal: kein Song mehr, Menschen da.
        spieler.active_channel_id = VOICE
        assert asyncio.run(spieler._leerlauf(welt.kanal)) is True
        assert spieler._leer_seit is not None
    with _welt() as welt:
        welt.spieler.clip_halten = True
        welt.spieler.active_channel_id = 4711            # den Kanal gibt es nicht mehr
        asyncio.run(welt.spieler.heal(welt.guild))
        assert welt.spieler.active_channel_id is None and welt.spieler.clip_halten is False


def test_clips_an_haelt_auch_wenn_schon_mitgehoert_wird():
    """Laeuft Musik und Flo hoert schon mit, heisst 'flo clip an' trotzdem:
    nach dem letzten Song bleiben, solange Menschen da sind."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        assert welt.spieler.clip_halten is False
        antwort = asyncio.run(clips.handle(_nachricht(welt, "flo clip an", welt.anna)))
        assert "schon mit" in antwort and welt.spieler.clip_halten is True
        assert asyncio.run(welt.spieler._leerlauf(welt.kanal)) is False


def test_clips_watchdog_adoptiert_keinen_sound_besuch_woanders():
    """'flo clip an' in A, Flos Verbindung reisst, und genau dann verbindet ein
    Join-Sound (voicegags) einen kurzen Besuch in B. Der Watchdog darf den
    nicht zur Musik-Sitzung machen: sonst kuendigte clips in B an, hoerte dort
    mit, und Flo bliebe fuer immer in B. Ohne clips adoptiert er wie eh und je."""
    with _welt() as welt:
        asyncio.run(_starten(welt))
        _reden(welt, ANNA, 10, 1)
        s = _sitzung(welt)
        welt.spieler.clip_halten = True
        welt.vc._connection.verbunden = False              # Abriss in A
        b = _voice_kanal_mit_chat(cid=881, name="Lounge")
        b.guild = welt.guild
        welt.guild.kanaele[881] = b
        besuch = _client(b)                                 # Join-Sound in B
        welt.guild.voice_client = besuch
        asyncio.run(welt.spieler.heal(welt.guild))
        assert welt.spieler.voice is welt.vc and welt.spieler.active_channel_id == VOICE
        assert not music.gehoert_der_musik(GID, besuch)
        import voicegags
        assert voicegags.VoiceGags._musik_hat_sie(welt.guild, besuch) is False
        asyncio.run(_starten(welt))
        assert b.gesendet == [] and besuch._connection.lauscher == []
        assert s.aktiv and s.kanal_id == VOICE and s.grund == "getrennt"
        assert ANNA in s.puffer.spuren                     # die Gnade laeuft
        # Die Regel selbst: nur ein FREMDER Client in einem ANDEREN Kanal - und
        # nur, solange clips laeuft (oder 'clip an' gilt).
        spieler = welt.spieler
        assert spieler._fremder_besuch(besuch) is True
        assert spieler._fremder_besuch(_client(welt.kanal)) is False   # gleicher Kanal
        assert spieler._fremder_besuch(spieler.voice) is False         # der eigene
        spieler.clip_halten = False
        assert spieler._fremder_besuch(besuch) is True                 # clips laeuft
        # Wo clips nicht mithoeren kann, bleibt die Musik genau wie vorher.
        with mock.patch.object(features, "is_on_in", lambda g, k: k != "clips"):
            assert spieler._fremder_besuch(besuch) is False            # hier aus
        welt.schalter["aufnahme"] = False
        assert spieler._fremder_besuch(besuch) is False                # keine Einwilligung
        welt.schalter["aufnahme"] = True
        welt.inst._enabled = False
        assert spieler._fremder_besuch(besuch) is False                # wie vorher
    with mock.patch.dict(sys.modules, {"clips": None}):
        spieler = music.GuildPlayer(loop=None, guild_id=GID)
        spieler.active_channel_id = VOICE
        anderswo = SimpleNamespace(channel=SimpleNamespace(id=881))
        assert spieler._fremder_besuch(anderswo) is False


def test_clips_musik_meldet_verbinden_und_trennen():
    """connect() und disconnect() der Musik melden clips - beim Aufruf ueber
    sys.modules, nie per Anmeldung. Ohne clips: nichts."""
    gemeldet = []
    frisch = clips.Clips()
    frisch.sitzung_geaendert = gemeldet.append
    spieler = music.GuildPlayer(loop=None, guild_id=GID)
    vc = SimpleNamespace(is_connected=lambda: True, channel=SimpleNamespace(id=42))

    async def fresh(_kanal):
        spieler.voice = vc

    spieler._fresh_connect = fresh
    spieler.clip_halten = True
    kanal = SimpleNamespace(id=42, guild=SimpleNamespace(voice_client=None), name="x")
    with mock.patch.object(clips, "instance", frisch), \
            mock.patch.object(music, "_retire_panel", lambda _p: _als_nichts()), \
            mock.patch.object(music, "_zustand_vergessen", lambda _g: None):
        asyncio.run(spieler.connect(kanal))
        assert gemeldet == [GID]
        spieler.voice = None
        asyncio.run(spieler.disconnect())
        assert gemeldet == [GID, GID] and spieler.clip_halten is False
    with mock.patch.dict(sys.modules, {"clips": None}), \
            mock.patch.object(music, "_retire_panel", lambda _p: _als_nichts()), \
            mock.patch.object(music, "_zustand_vergessen", lambda _g: None):
        asyncio.run(spieler.connect(kanal))
        asyncio.run(spieler.disconnect())
    assert gemeldet == [GID, GID]


def test_clips_bot_haken_und_ansage_bleibt_stehen():
    """bot.py: Voice-Ereignis, Takt und Herunterfahren rufen clips; die Ansage
    ist vom Auto-Loeschen ausgenommen."""
    import bot
    from test_fundament import _nur_geladen
    aufrufe = []

    async def voice_state(*a):
        aufrufe.append("voice_state")

    async def tick(_c):
        aufrufe.append("tick")

    async def runter():
        aufrufe.append("herunterfahren")

    with _nur_geladen(bot, "CLIPS_ENABLED"), \
            mock.patch.object(clips, "voice_state", voice_state), \
            mock.patch.object(clips, "tick", tick), \
            mock.patch.object(clips, "herunterfahren", runter), \
            mock.patch.object(bot.store, "alle_sichern", _als_nichts):
        async def lauf():
            mitglied = SimpleNamespace(id=5, bot=False, guild=SimpleNamespace(id=77))
            vorher = SimpleNamespace(channel=None, self_stream=False)
            await bot.client.on_voice_state_update(mitglied, vorher, vorher)
            await asyncio.sleep(0)
            await bot.client.voice_heal_loop.coro(bot.client)
            await bot.client._alles_sichern(vor_abmelden=True)
            await bot.client._alles_sichern(vor_abmelden=False)
        with mock.patch.object(type(bot.client), "guilds", property(lambda s: [])):
            asyncio.run(lauf())
    assert aufrufe == ["voice_state", "tick", "herunterfahren"], aufrufe
    with _welt() as welt:
        asyncio.run(_starten(welt))
        ansage = welt.kanal.gesendet[0]
        ansage.author = SimpleNamespace(id=FLO)
        andere = SimpleNamespace(author=SimpleNamespace(id=FLO), components=[], embeds=[])
        with _nur_geladen(bot, "CLIPS_ENABLED"), \
                mock.patch.object(type(bot.client), "user",
                                  property(lambda s: SimpleNamespace(id=FLO))), \
                mock.patch.object(bot.music, "ist_panel", lambda _m: False):
            assert bot.client._keep_bot_msg(ansage) is True
            assert bot.client._keep_bot_msg(andere) is False


def test_clips_alte_ansage_raeumt_das_auto_loeschen_doch_weg():
    """Im Aufraeum-Kanal bleibt nur die AKTUELLE Ansage stehen - solange Flo
    dort zuhoert oder sie juenger als ANSAGE_PAUSE ist (dann holt eine neue
    Runde sie zurueck). Nach einem Neustart ohne neue Runde, oder eine alte,
    die laengst ersetzt ist: weg damit, statt fuer immer 'Ab jetzt hör ich hier
    mit' zu sagen."""
    import bot
    from test_fundament import _nur_geladen
    with _welt() as welt:
        asyncio.run(_starten(welt))
        ansage = welt.kanal.gesendet[0]
        ansage.author = SimpleNamespace(id=FLO)
        ansage.embeds, ansage.pinned = [], False
        with _nur_geladen(bot, "CLIPS_ENABLED"), \
                mock.patch.object(type(bot.client), "user",
                                  property(lambda s: SimpleNamespace(id=FLO))), \
                mock.patch.object(bot.music, "ist_panel", lambda _m: False):
            assert bot.client._keep_bot_msg(ansage) is True            # Flo hoert zu
            # Neustart: ein frisches clips mit demselben Speicher, keine Runde.
            speicher = welt.inst._store
            vars(welt.inst).update(vars(clips.Clips()))
            welt.inst._store, welt.inst._enabled = speicher, True
            assert bot.client._keep_bot_msg(ansage) is True            # juenger als 45 min
            welt.inst._daten("ansagen")[str(VOICE)]["ts"] -= clips.ANSAGE_PAUSE + 1
            assert bot.client._keep_bot_msg(ansage) is False           # vorbei: darf weg
            assert bot.client._sweepable(ansage) is True
            # Eine laengst ersetzte Ansage zaehlt nie.
            welt.inst._daten("ansagen")[str(VOICE)] = {"ts": round(time.time()),
                                                       "mid": ansage.id + 1}
            assert bot.client._keep_bot_msg(ansage) is False


def test_clips_status_nennt_den_grund_von_jetzt():
    """'flo clip status' auf einem Server, auf dem Flo nie zugehoert hat: der
    Grund von JETZT (hier: die Einwilligung des Servers fehlt), nicht ein
    zwischengespeicherter von frueher."""
    with _welt(aufnahme=False) as welt:
        mod = _person(DORA, "Dora", mod=True)
        emb = asyncio.run(clips.handle(_nachricht(welt, "flo clip status", mod)))
        assert "Grund: `server`" in emb.fields[0].value, emb.fields[0].value


def test_clips_ausstieg_per_privatnachricht():
    """Der Ausstieg gilt auf jedem Server - also geht er auch privat: 'clip
    nein' (mit oder ohne 'flo') an Flo, und die Nachricht landet NICHT beim
    Besitzer. Alles andere in einer Privatnachricht bleibt, wie es war."""
    import bot
    from test_fundament import _nur_geladen
    with _welt() as welt:
        kanal = _TextKanal(cid=9, name="dm")

        def dm(text, uid=CEM):
            autor = _person(uid, "Cem")
            return SimpleNamespace(content=text, author=autor, guild=None, channel=kanal,
                                   mentions=[], id=778, reference=None,
                                   to_reference=lambda **_kw: _Bezug())

        assert asyncio.run(clips.datenschutz_dm(dm("clip nein"))) is True
        assert welt.inst.ist_raus(CEM) and "du bist raus" in kanal.gesendet[-1].content
        assert asyncio.run(clips.datenschutz_dm(dm("flo clip ja bitte"))) is True
        assert not welt.inst.ist_raus(CEM) and "wieder drin" in kanal.gesendet[-1].content
        assert asyncio.run(clips.datenschutz_dm(dm("flo clip 30"))) is False
        assert asyncio.run(clips.datenschutz_dm(dm("hallo flo"))) is False
        # bot.py: vor dem Weiterleiten an den Besitzer.
        weiter = []

        async def weiterleiten(_self, nachricht):
            weiter.append(nachricht.content)

        async def lauf(text):
            nachricht = dm(text)
            nachricht.author.bot = False
            await bot.client.on_message(nachricht)
            await asyncio.sleep(0)

        with _nur_geladen(bot, "CLIPS_ENABLED"), \
                mock.patch.object(bot, "OWNER_ID", 1), \
                mock.patch.object(type(bot.client), "_forward_dm_to_owner", weiterleiten):
            for text in ("clip nein", "hallo flo"):
                asyncio.run(lauf(text))
            assert welt.inst.ist_raus(CEM) and weiter == ["hallo flo"]

            # Ein Fehler im Ausstieg legt die Privatnachrichten nicht lahm.
            async def kaputt(_nachricht):
                raise RuntimeError("kaputt")

            with mock.patch.object(clips, "datenschutz_dm", kaputt), \
                    mock.patch.object(bot.log, "exception") as gemeldet:
                asyncio.run(lauf("hallo nochmal"))
        assert weiter == ["hallo flo", "hallo nochmal"]
        assert gemeldet.call_args[0][0] == "Clips: Ausstieg per Privatnachricht ging nicht"


def test_clips_ansage_nennt_groq_nur_wenn_alles_bei_groq_landet():
    """Das Transkript geht zu Groq, der Spruch an die KI von ai.py - und die
    kann ein ganz anderer Anbieter sein. 'ueber Groq' steht nur da, wenn beides
    dort landet."""
    import ai
    with _welt(transkript=True) as welt:
        for url, ki, erwartet in (
                ("https://api.groq.com/openai/v1", True, "über Groq"),
                ("https://openrouter.ai/api/v1", False, "über Groq"),
                ("https://openrouter.ai/api/v1", True, "über einen externen KI-Dienst")):
            with mock.patch.object(ai.instance, "_basis_url", url, create=True), \
                    mock.patch.object(ai, "is_enabled", lambda ki=ki: ki):
                text = welt.inst._ansage_text(welt.guild, welt.kanal)
            assert erwartet in text, (url, ki, text)
            if "extern" in erwartet:
                assert "Groq" not in text


def test_clips_clip_im_privaten_voice_chat_bleibt_stehen():
    """Ein Clip-Post (am 🔥 erkannt) bleibt vom Auto-Loeschen verschont: der
    Clip-Kanal ist nie ein Aufraeum-Kanal, der Voice-Chat eines privaten Voice
    kann einer sein - dort verschwaende der Clip sonst nach einer Minute."""
    import bot
    from test_fundament import _nur_geladen
    with _welt() as welt:
        post, _cid = _ein_clip(welt)
        post.author = SimpleNamespace(id=FLO)
        post.embeds = []
        assert clips.ist_clip(post) and not clips.ist_ansage(post)
        assert not clips.ist_clip(welt.kanal.gesendet[0])          # die Ansage
        assert not clips.ist_clip(SimpleNamespace(components=[]))
        flo = mock.patch.object(type(bot.client), "user",
                                property(lambda s: SimpleNamespace(id=FLO)))
        with flo, mock.patch.object(bot.music, "ist_panel", lambda _m: False):
            with _nur_geladen(bot, "CLIPS_ENABLED"):
                assert bot.client._keep_bot_msg(post) is True
            with _nur_geladen(bot):
                assert bot.client._keep_bot_msg(post) is False


def test_clips_k_zeigt_jede_clip_meldung():
    """'k c' sucht im Journal nach CLIPMUSTER - jede Logzeile aus clips,
    clipschnitt, clipstext und voiceempfang muss es treffen."""
    import re
    wurzel = os.path.dirname(os.path.abspath(__file__))
    arzt = open(os.path.join(wurzel, "k"), encoding="utf-8").read()
    muster = re.search(r'CLIPMUSTER="([^"]+)"', arzt)
    assert muster, "in 'k' gibt es kein CLIPMUSTER"
    assert re.search(r'c\|clips\)\s*\n\s*logs "[^"]*" "\$CLIPMUSTER"', arzt), "kein 'k c'"
    assert "k c = Clips" in arzt
    suche = re.compile(muster.group(1))
    durchgefallen = []
    for datei in ("clips.py", "clipschnitt.py", "clipstext.py", "voiceempfang.py"):
        quelle = open(os.path.join(wurzel, datei), encoding="utf-8").read()
        for treffer in re.finditer(r'log\.(?:info|warning|error|exception)\(\s*"([^"]*)"',
                                   quelle):
            if not suche.search(treffer.group(1)):
                durchgefallen.append(f"{datei}: {treffer.group(1)[:60]}")
    assert not durchgefallen, durchgefallen


def test_clips_davey_ist_gedeckelt():
    """davey 0.2 aendert, wie Schluessel und Nonces gefuehrt werden - darauf
    baut der Empfang. Der Deckel steht in requirements.txt."""
    zeilen = open("requirements.txt", encoding="utf-8").read().splitlines()
    assert "davey>=0.1.6,<0.2" in zeilen


# =============================================================================
# Ende zu Ende: echtes Opus, echtes PyNaCl, echter Empfaenger, echter Schnitt
# =============================================================================
def _opus_da():
    try:
        return bool(discord.opus.is_loaded() or discord.opus._load_default())
    except Exception:  # noqa: BLE001
        return False


def _sinus_rahmen(freq, n, amp=9000):
    kodierer = discord.opus.Encoder()
    rahmen = []
    for k in range(n):
        werte = []
        for i in range(960):
            s = int(amp * math.sin(2 * math.pi * freq * (k * 960 + i) / 48000))
            werte += (s, s)
        rahmen.append(kodierer.encode(struct.pack(f"<{len(werte)}h", *werte), 960))
    return rahmen


def _rtp(opus, seq, ts, ssrc, nonce):
    """Ein RTP-Paket wie von Discord: rtpsize, echt mit PyNaCl verschluesselt."""
    kopf = struct.pack(">BBHII", 0x80, voiceempfang.OPUS_PT, seq & 0xFFFF, ts & 0xFFFFFFFF,
                       ssrc)
    n = struct.pack(">I", nonce)
    geheim = nacl.secret.Aead(_SCHLUESSEL).encrypt(opus, kopf, n + bytes(20)).ciphertext
    return kopf + geheim + n


def _ffprobe_dauer(mp3):
    import tempfile
    import subprocess
    ordner = tempfile.mkdtemp(prefix="flo-clip-test-")
    pfad = os.path.join(ordner, "x.mp3")
    try:
        with open(pfad, "wb") as datei:
            datei.write(mp3)
        lauf = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", pfad], capture_output=True,
                              text=True, timeout=30)
        return float(lauf.stdout.strip())
    finally:
        shutil.rmtree(ordner, ignore_errors=True)


def test_clips_ende_zu_ende_vom_rtp_paket_zur_mp3():
    """Anna redet 12 s (Sinus, echtes libopus), Discord schickt es als RTP mit
    echtem PyNaCl - die ersten Pakete, bevor op 5 sagt, wem die SSRC gehoert
    (die haelt der Empfaenger, bis der echte Websocket-Haken die Zuordnung
    meldet). Der Empfaenger entschluesselt, der Puffer haelt es, Flos Musik
    laeuft daneben - 'flo clip 10s' schneidet mit dem ECHTEN clipschnitt eine
    MP3, und die liegt mit der erwarteten Laenge und Groesse im Clip-Kanal."""
    if not (_opus_da() and clipschnitt.ffmpeg_pruefen() == "" and shutil.which("ffprobe")):
        return                                     # ohne libopus/ffmpeg nichts zu pruefen
    anna_rahmen = _sinus_rahmen(440, 600)          # 12 s
    flo_rahmen = _sinus_rahmen(220, 600, amp=6000)
    vor_op5 = 25                                   # 0,5 s, bevor op 5 ankommt
    with _welt() as welt:
        async def lauf():
            await _starten(welt)
            s = _sitzung(welt)
            e = s.empfaenger
            start = welt.uhr()
            for k, opus in enumerate(anna_rahmen):
                welt.uhr.t = start + 1.0 + k * 0.02 + 0.03      # 30 ms Netz
                e.roh(_rtp(opus, 65500 + k, 2 ** 32 - 960 * 100 + k * 960, 4242, k + 1))
                s.puffer.eigen_anhaengen(flo_rahmen[k], start + 1.0 + k * 0.02)
                if k == vor_op5 - 1:
                    e.abarbeiten()
                    assert e.stand()["gehalten"] == vor_op5 and ANNA not in s.puffer.spuren
                    # op 5 (SPEAKING) ueber den echten Haken des Clients.
                    await welt.vc._ws_hook(None, {"op": 5, "d": {
                        "user_id": str(ANNA), "ssrc": 4242, "speaking": 1}})
                    assert e.stand()["gehalten"] == 0
                if k % 50 == 49:
                    e.abarbeiten()
            e.abarbeiten()
            assert e.zaehler()["gespeichert"] == 600, e.zaehler()
            assert e.zaehler()["unbekannt"] == vor_op5
            assert e.zaehler()["transport_fehler"] == 0
            # Die gehaltenen Pakete behalten ihre Ankunftszeit.
            assert s.puffer.spuren[ANNA].pakete[0][0] == start + 1.0 + 0.03
            welt.uhr.t = start + 13.1
            with mock.patch.object(clipstext, "kommentar", _kein_kommentar):
                return await clips.handle(_nachricht(welt, "flo clip 10s", welt.anna))

        assert asyncio.run(lauf()) is clips.HANDLED
        post = welt.ziel.gesendet[-1]
        mp3 = post.file.fp.read()
        assert mp3[:3] == b"ID3" or mp3[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xfa")
        # 10 s bei der Bitrate, die das Budget hergibt (192 kbps) - plus Kopf.
        kbps = clipschnitt.bitrate_fuer(10.0, welt.inst._budget(welt.guild))
        erwartet = kbps * 1000 / 8 * 10
        assert kbps == 192 and abs(len(mp3) - erwartet) < 0.08 * erwartet, len(mp3)
        assert len(mp3) <= int(9.5 * 1024 * 1024)
        assert abs(_ffprobe_dauer(mp3) - 10.0) < 0.15
        assert post.file.filename.endswith("-10s.mp3")
        assert f"<@{ANNA}>" in post.content and welt.inst._clips_von(GID)
        assert "Clip ist raus" in welt.text.gesendet[-1].content


if __name__ == "__main__":
    run(globals())
