"""Voice-Gags (Pack 4): Soundboard, TTS und Join-Sounds.

Befehle (nach 'Flo'):
- sound <name>     spielt sounds/<name>.(mp3|wav|ogg|...) im Sprachkanal
- sounds           listet die verfuegbaren Sounds
- sprich <text>    spricht den Text per TTS aus (espeak-ng offline, oder gTTS)

Join-Sounds (optional, JOIN_SOUNDS=1): Betritt jemand einen Sprachkanal und es
gibt sounds/join/<user_id>.* (oder sounds/join/default.*), spielt Flo den Sound.

Voraussetzungen wie bei der Musik: ffmpeg + PyNaCl + libopus (+ davey bei
discord.py >= 2.7) - geprueft in setup() ueber music.voice_fehlt().
Laeuft schon ein anderer Sound/Musik im Kanal, weicht das Modul hoeflich aus,
statt die Musik abzuwuergen. Die Sound-Dateien legt der Nutzer selbst in sounds/ ab.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import discord

import ai
import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.voice")

# Woerter, die direkt hinter 'sprich/say/tts/vorlesen' stehen koennen, ohne dass
# ein Vorlese-Auftrag gemeint ist. Der Satz geht dann normal an die KI.
_KEIN_TTS = frozenset((
    "nicht", "mal", "bitte", "doch", "mit", "lauter", "leiser", "langsamer",
    "weiter", "ist", "war", "du", "mir", "wieder", "so", "leise", "laut",
    "what", "was", "wat", "wie", "warum", "wer", "wann", "macht", "kann",
    "koennen", "können", "gerne", "gern",
    # 'Flo sprich bayrisch, wie sagt man Semmel?' ist eine Frage an Flo, kein
    # Vorlese-Auftrag. Den Dialekt-Schalter erledigt bayern.py davor; laesst
    # der den Satz fallen, soll ihn die KI beantworten und nicht TTS vorlesen.
    "bayrisch", "boarisch", "bairisch", "bayerisch", "dialekt", "deutsch",
    "hochdeutsch", "englisch", "normal", "anders",
))

# Sentinel: voicegags hat selbst geantwortet (Soundboard-Menue) -> bot.py schweigt.
HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

SOUNDS_DIR = Path(os.getenv("SOUNDS_DIR", str(Path(__file__).resolve().parent / "sounds")))
JOIN_DIR = SOUNDS_DIR / "join"
_AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".opus", ".flac")

_FFMPEG_OPTS = "-vn"


# --- Soundboard-Menue: ein Button je Sound, Klick = sofort abspielen -------
_SB_EMOJIS = ("🔊", "🎺", "📣", "💥", "🎵", "😂", "🔥", "🎉", "🥁", "📢")
_SB_STYLES = (discord.ButtonStyle.primary, discord.ButtonStyle.success,
              discord.ButtonStyle.danger, discord.ButtonStyle.secondary)


class SoundKnopf(discord.ui.DynamicItem[discord.ui.Button],
                template=r"flo:sb:d:(?P<name>.{1,90})"):
    """Ein Datei-Sound (sounds/<name>.*) als Knopf. Die feste custom_id macht
    das Brett neustartfest - vorher war es nach zehn Minuten tot."""

    def __init__(self, name, idx=0):
        super().__init__(discord.ui.Button(
            label=name[:20], emoji=_SB_EMOJIS[idx % len(_SB_EMOJIS)],
            style=_SB_STYLES[idx % len(_SB_STYLES)], custom_id=f"flo:sb:d:{name[:90]}"))
        self.sound_name = name

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        knopf = cls(match["name"])
        knopf.item.emoji, knopf.item.style = item.emoji, item.style
        return knopf

    async def callback(self, interaction):
        await instance._klick(interaction, "datei", self.sound_name)


class SoundAuswahl(discord.ui.DynamicItem[discord.ui.Select],
                   template=r"flo:sb:(?P<art>datei|server|discord)"):
    """Ein Menue mit Sounds: weitere Dateien, die Sounds des Servers oder die
    Standard-Sounds von Discord (die beiden letzten laufen per send_sound
    UEBER die Musik drueber)."""

    PLATZHALTER = {"datei": "📁 Weitere Sounds …", "server": "🎛️ Server-Sounds …",
                   "discord": "🔔 Discord-Sounds …"}

    def __init__(self, art, optionen=()):
        optionen = list(optionen)[:25] or [discord.SelectOption(label="—", value="-")]
        super().__init__(discord.ui.Select(custom_id=f"flo:sb:{art}",
                                           placeholder=self.PLATZHALTER[art],
                                           options=optionen))
        self.art = art

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["art"], getattr(item, "options", ()))

    async def callback(self, interaction):
        await instance._klick(interaction, self.art, (self.item.values or [""])[0])


def _auswahl_emoji(emoji):
    """Nur Unicode-Emojis ins Menue - ein fremdes Server-Emoji laesst Discord
    dort nicht zu, und dann kaeme gar kein Brett."""
    if emoji is None:
        return "🔊"
    try:
        if emoji.is_unicode_emoji():
            return str(emoji)
    except AttributeError:
        return str(emoji) or "🔊"
    return "🔊"


class SoundboardView(discord.ui.LayoutView):
    """Das Brett: Datei-Sounds als bunte Knoepfe (JEDER darf druecken - es ist
    ein Soundboard 😄), dahinter Menues fuer weitere Dateien, die Sounds des
    Servers und die von Discord.

    Vorher: hoechstens 25 Knoepfe (und die waren schon voll), nur eigene
    Dateien, und nach zehn Minuten tot. Jetzt DynamicItems, timeout=None, nie
    stop() - sonst waeren die Vorlagen global weg."""

    KNOEPFE = 15

    def __init__(self, sounds, server=(), standard=()):
        super().__init__(timeout=None)
        teile = [discord.ui.TextDisplay(
            "## 🔊 Soundboard\nAb in den Voice und **drücken**! "
            + ("Server- und Discord-Sounds laufen über die Musik drüber."
               if server or standard else ""))]
        knoepfe = [SoundKnopf(name, i) for i, name in enumerate(sounds[:self.KNOEPFE])]
        for i in range(0, len(knoepfe), 5):
            teile.append(discord.ui.ActionRow(*knoepfe[i:i + 5]))
        rest = sounds[self.KNOEPFE:self.KNOEPFE + 25]
        if rest:
            teile.append(discord.ui.ActionRow(SoundAuswahl("datei", [
                discord.SelectOption(label=name[:100], value=name[:100], emoji="📁")
                for name in rest])))
        for art, liste in (("server", server), ("discord", standard)):
            if liste:
                teile.append(discord.ui.ActionRow(SoundAuswahl(art, [
                    discord.SelectOption(label=str(snd.name)[:100], value=str(snd.id),
                                         emoji=_auswahl_emoji(getattr(snd, "emoji", None)))
                    for snd in liste[:25]])))
        gesamt = len(sounds) + len(server) + len(standard)
        fuss = f"-# {gesamt} Sounds · eigene Dateien einfach in {SOUNDS_DIR.name}/ legen"
        if len(sounds) > self.KNOEPFE + 25:
            fuss += f" · {len(sounds) - self.KNOEPFE - 25} weitere per `sound <name>`"
        teile.append(discord.ui.TextDisplay(fuss))
        self.add_item(discord.ui.Container(*teile, accent_colour=0x5865F2))


# Werden in bot.setup_hook angemeldet (neustartfestes Brett).
DYNAMISCHE_KNOEPFE = (SoundKnopf, SoundAuswahl)

# Wie lange die Liste der Server-/Discord-Sounds gilt (Sekunden).
_SOUND_CACHE_SEK = 300
_STANDARD_CACHE_SEK = 3600
# Wie lange Flo nach einem Discord-Sound im Kanal bleibt, wenn er NUR dafuer
# gekommen ist (die Sounds dauern ein paar Sekunden).
_NACH_SOUND_SEK = 8


class VoiceGags(FeatureBasis):
    def __init__(self):
        self._enabled = False
        self._tts_engine = ""          # "gtts", "espeak-ng", "espeak" oder "" (aus)

        # Hintergrund-Tasks (Sound spielt bis zu 60 s - Button antwortet sofort).
        self._bg = set()
        # Server-Sounds je Server und Discords Standard-Sounds: (monotonic, liste).
        # 'Nie geholt' = -inf, siehe Monotonic-Falle.
        self._server_sounds = {}
        self._standard_sounds = (float("-inf"), [])

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # Loesch-Schutz: liegt in basis.FeatureBasis (schuetzen/freigeben).
    def _protect(self, msg):
        self.schuetzen(msg)

    def _release(self, msg):
        self.freigeben(msg)

    def setup(self):
        """Aktiv, wenn Voice moeglich ist (ffmpeg, PyNaCl, davey, libopus - siehe
        music.voice_fehlt). TTS-Engine wird erkannt."""
        if os.getenv("VOICE_GAGS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Voice-Gags aus (VOICE_GAGS_ENABLED=0).")
            return False
        if shutil.which("ffmpeg") is None:
            log.info("Voice-Gags aus: ffmpeg fehlt.")
            return False
        # Dieselbe Pruefung wie bei der Musik. Vorher stand hier nur PyNaCl:
        # ohne davey (Pflicht ab discord.py 2.7) oder libopus meldete das Log
        # "Voice-Gags aktiv", und dann scheiterte jeder Sound im Kanal.
        from music import voice_fehlt
        fehlt = voice_fehlt()
        if fehlt:
            log.warning("Voice-Gags aus: %s", fehlt)
            return False

        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
        # Eingebautes Soundpack: echte synthetisierte SFX (Airhorn, Boom, ...) -
        # nur fehlende Dateien werden erzeugt, eigene Sounds bleiben unberuehrt.
        if os.getenv("SOUND_PACK", "1").strip().lower() not in ("0", "false", "no", "off"):
            try:
                import soundpack
                neu = soundpack.ensure_pack(SOUNDS_DIR)
                if neu:
                    log.info("Soundpack: %d eingebaute Sounds generiert.", neu)
            except Exception:  # noqa: BLE001 - Pack ist Bonus, Feature laeuft auch ohne
                log.exception("Soundpack-Generierung fehlgeschlagen")
        self._tts_engine = self._detect_tts()
        # Soundboard und Join-Sounds liegen jetzt in guildcfg - je Server und
        # damit auch im Web-Panel. Vorher steckten sie in einem eigenen
        # Speicher, galten fuer ALLE Server gleich und liessen sich nur vom
        # Bot-Besitzer per Discord-Befehl umstellen.
        self._enabled = True
        log.info("Voice-Gags aktiv (Sounds: %s, TTS: %s). Soundboard und "
                 "Join-Sounds stellt jeder Server selbst ein.",
                 self._count_sounds(), self._tts_engine or "aus")
        return True

    async def altlast_migrieren(self, guild_ids):
        """Uebernimmt ein frueher global abgeschaltetes Soundboard EINMALIG.

        Ohne das waere ein bewusst ausgeschaltetes Board nach dem Update
        ueberall wieder an - eine stille Aenderung am laufenden Server. Die
        alte Datei wird danach umbenannt, damit das genau einmal passiert.
        Wird aus on_ready gerufen: erst dort sind die Server bekannt."""
        from store import DATA_DIR
        alt = DATA_DIR / "voicegags.json"
        if not alt.exists():
            return 0
        war_aus = False
        try:
            with open(alt, encoding="utf-8") as f:
                war_aus = json.load(f).get("soundboard") is False
        except Exception:  # noqa: BLE001 - unlesbar = nichts zu uebernehmen
            log.warning("Alte voicegags.json unlesbar - nehme den Standard (an).")
        gesetzt = 0
        if war_aus:
            import guildcfg
            for gid in guild_ids or []:
                ok, _w, _f = await guildcfg.setzen(gid, "soundboard", "aus")
                gesetzt += 1 if ok else 0
            log.warning("Soundboard war global AUS - auf %d Server(n) "
                        "uebernommen. Ab jetzt stellt das jeder Server selbst "
                        "ein, auch im Web-Panel.", gesetzt)
        try:
            alt.rename(alt.with_name("voicegags.json.uebernommen"))
        except OSError:
            log.warning("Alte voicegags.json liess sich nicht umbenennen - "
                        "die Uebernahme laeuft beim naechsten Start erneut.")
        return gesetzt

    def is_enabled(self):
        return self._enabled

    def _detect_tts(self):
        try:
            import gtts  # noqa: F401
            return "gtts"
        except ImportError:
            pass
        for binary in ("espeak-ng", "espeak"):
            if shutil.which(binary):
                return binary
        return ""

    async def _schalten(self, message, schalter):
        """'Flo soundboard an/aus' -> schreibt guildcfg (wie das Panel).

        Bewusst NICHT mehr nur fuer den Bot-Besitzer: es ist eine
        Server-Einstellung, also gilt dasselbe Recht wie fuer alle anderen -
        'Server verwalten'. Sonst braeuchte man fuer diesen einen Schalter den
        Bot-Betreiber, waehrend man alles andere selbst einstellen darf."""
        import guildcfg
        if not guildcfg.darf(message):
            return ("Das darf nur, wer den Server verwaltet. "
                    f"Ansehen geht immer: `{self._bot_name} sounds`.")
        an = schalter in ("an", "ein", "on")
        ok, _wert, fehler = await guildcfg.setzen(
            message.guild.id, "soundboard", "an" if an else "aus")
        if not ok:
            return fehler or "Das liess sich gerade nicht speichern."
        if an:
            return "🔊 Soundboard ist auf diesem Server wieder **AN**."
        return ("🔇 Soundboard ist auf diesem Server **AUS** "
                f"(wieder an: `{self._bot_name} soundboard an`).")

    @staticmethod
    def soundboard_enabled(gid=None):
        """Darf das Soundboard auf DIESEM Server benutzt werden?

        Wird bei jedem Gebrauch frisch gelesen, nicht beim Start gemerkt -
        sonst wirkte ein Klick im Panel erst nach einem Neustart. Genau das ist
        gemeint, wenn der Betreiber sagt, es soll synchron sein."""
        if not gid:
            return True
        try:
            import guildcfg
            return guildcfg.an(gid, "soundboard")
        except Exception:  # noqa: BLE001 - im Zweifel an
            return True

    @staticmethod
    def join_sounds_an(gid=None):
        """Join-Sounds auf diesem Server? Ebenfalls bei jedem Gebrauch gelesen."""
        if not gid:
            return False
        try:
            import guildcfg
            return guildcfg.an(gid, "join_sounds")
        except Exception:  # noqa: BLE001
            return False

    def _count_sounds(self):
        if not SOUNDS_DIR.exists():
            return 0
        return sum(1 for p in SOUNDS_DIR.iterdir()
                   if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)

    def _clean_lead(self, text):
        # Zentral in ai.strip_lead: entfernt @-Mentions + fuehrenden Namen/Alias
        # ('Florian sound nice' -> 'sound nice').
        return ai.strip_lead(text)

    def _find_sound(self, name):
        name = name.strip().lower()
        if not name or "/" in name or "\\" in name or ".." in name:
            return None  # kein Pfad-Ausbruch
        if not SOUNDS_DIR.is_dir():
            return None  # noch keine eigenen Sounds (dann evtl. ein Discord-Sound)
        for p in SOUNDS_DIR.iterdir():
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS and p.stem.lower() == name:
                return p
        return None

    def _list_sounds(self):
        if not SOUNDS_DIR.exists():
            return []
        # Je Name nur EINMAL: boom.wav + boom.mp3 ergaben zwei Knoepfe mit
        # derselben custom_id - Discord lehnte das ganze Brett ab.
        namen = {}
        for p in SOUNDS_DIR.iterdir():
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS:
                namen.setdefault(p.stem.lower(), p.stem)
        return sorted(namen.values(), key=str.lower)

    # --- Befehle -------------------------------------------------------------
    async def handle(self, message):
        if not self._enabled or message.guild is None:
            return None
        cmd = self._clean_lead(message.content or "")
        if not cmd:
            return None
        parts = cmd.split(maxsplit=1)
        first = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        # 'Flo soundboard an/aus' - schaltet das Brett fuer DIESEN Server.
        # Lag frueher in admin.py, war global und nur fuer den Bot-Besitzer.
        # Jetzt dieselbe Wahrheit wie das Web-Panel: guildcfg.
        if first in ("soundboard", "sounds", "soundliste"):
            schalter = rest.strip().lower().strip(".,!?")
            if schalter in ("an", "ein", "on", "aus", "off", "aus.", "off."):
                return await self._schalten(message, schalter)

        # Die Soundliste nimmt gar kein Argument - steht etwas dahinter, ist es
        # kein Befehl: "Flo sounds gut, lass uns das so machen" hat sonst das
        # komplette Soundboard-Menue aufgeklappt (denglisch 'sounds good').
        if first in ("sounds", "soundboard", "soundliste") and not rest.strip(" .,!?"):
            if not self.soundboard_enabled(message.guild.id):
                return "Das Soundboard ist gerade **deaktiviert**. 🔇"
            sounds = self._list_sounds()
            server, standard = await self._discord_sounds(message.guild)
            if not (sounds or server or standard):
                return (f"Noch keine Sounds da. Leg Dateien in `{SOUNDS_DIR.name}/` "
                        f"(mp3/wav/ogg), dann geht `{self._bot_name} sound <name>`.")
            return await self._open_soundboard(message, sounds, server, standard)

        if first in ("sound", "sb", "soundeffekt"):
            if not self.soundboard_enabled(message.guild.id):
                return "Das Soundboard ist gerade **deaktiviert**. 🔇"
            return await self._cmd_sound(message, rest)

        if first in ("sprich", "tts", "say", "vorlesen"):
            # Was dahinter steht, entscheidet: ein Satz-Fortsetzer oder ein
            # Fragewort ist kein Vorlese-Auftrag. "sprich nicht so laut",
            # "sprich mal mit ihm", "say what?", "vorlesen macht Spass" haben
            # sonst alle die Sprachausgabe angeworfen.
            erstes = rest.split()[0].lower().strip(".,!?") if rest.split() else ""
            if erstes in _KEIN_TTS:
                return None
            return await self._cmd_say(message, rest)
        return None

    def _voice_beschaeftigt(self, guild):
        """Schnell-Check ohne Verbindungsaufbau: laeuft gerade Musik/Sound?"""
        try:
            import music
            if music.is_voice_busy(guild.id):
                return True
        except Exception:  # noqa: BLE001
            pass
        vc = guild.voice_client
        return vc is not None and (vc.is_playing() or vc.is_paused())

    async def _open_soundboard(self, message, sounds, server=None, standard=None):
        if server is None or standard is None:
            server, standard = await self._discord_sounds(message.guild)
        view = SoundboardView(sounds, server, standard)
        msg = await basis.antworte(message, None, view=view)
        if msg is None:
            log.error("Soundboard konnte nicht gesendet werden")
            return "Das Soundboard ging gerade nicht raus. Nochmal, bitte."
        # Zehn Minuten vorm Auto-Loeschen geschuetzt, danach darf es weg - die
        # Knoepfe funktionieren aber, solange die Nachricht steht.
        self._protect(msg)
        self._spawn(self._spaeter_freigeben(msg))
        return HANDLED

    async def _spaeter_freigeben(self, msg, sekunden=600):
        await asyncio.sleep(sekunden)
        self._release(msg)

    # --- Discords eigene Sounds (Server-Soundboard + Standard) ----------------
    def _darf_discord_sounds(self, guild, channel=None):
        """Darf Flo hier Soundboard-Sounds abspielen? Ohne das Recht zeigt das
        Brett eben nur die Datei-Sounds."""
        try:
            ich = guild.me
            rechte = (channel.permissions_for(ich) if channel is not None
                      else ich.guild_permissions)
            return bool(rechte.use_soundboard and rechte.speak)
        except Exception:  # noqa: BLE001 - Attrappen / fehlender Cache
            return False

    async def _discord_sounds(self, guild):
        """(Server-Sounds, Discord-Standard-Sounds) - kurz zwischengespeichert,
        damit nicht jedes 'Flo sounds' zwei API-Aufrufe kostet."""
        if guild is None or not self._darf_discord_sounds(guild):
            return [], []
        jetzt = time.monotonic()
        stand, server = self._server_sounds.get(guild.id, (float("-inf"), []))
        if jetzt - stand > _SOUND_CACHE_SEK:
            try:
                server = [snd for snd in await guild.fetch_soundboard_sounds()
                          if getattr(snd, "available", True)]
            except Exception as exc:  # noqa: BLE001
                log.info("Server-Sounds nicht abrufbar (%s)", exc)
                server = []
            self._server_sounds[guild.id] = (jetzt, server)
        stand, standard = self._standard_sounds
        client = self.client
        if jetzt - stand > _STANDARD_CACHE_SEK and client is not None:
            try:
                standard = list(await client.fetch_soundboard_default_sounds())
            except Exception as exc:  # noqa: BLE001
                log.info("Discord-Sounds nicht abrufbar (%s)", exc)
                standard = []
            self._standard_sounds = (jetzt, standard)
        return server, standard

    async def _finde_discord_sound(self, guild, art, wert):
        """Sound nach ID (Menue) oder Namen (Textbefehl) finden."""
        server, standard = await self._discord_sounds(guild)
        liste = {"server": server, "discord": standard}.get(art, server + standard)
        wert = str(wert).strip().lower()
        for snd in liste:
            if str(snd.id) == wert or str(snd.name).lower() == wert:
                return snd
        return None

    async def _discord_sound_spielen(self, guild, channel, sound):
        """Spielt einen Soundboard-Sound. Anders als die Datei-Sounds laeuft der
        UEBER die Musik (Discord mischt ihn selbst dazu) - Flo muss dafuer nur
        im Kanal sitzen und darf nicht taub geschaltet sein."""
        if not self._darf_discord_sounds(guild, channel):
            return False, ("Mir fehlt hier das Recht „Soundboard verwenden“. "
                           "Gebt's mir, dann knallt's.")
        vc = guild.voice_client
        neu_da = False
        try:
            if vc is None or not vc.is_connected():
                vc = await channel.connect(self_deaf=False)
                neu_da = True
            elif vc.channel.id != channel.id:
                if self._voice_beschaeftigt(guild) or self._musik_hat_sie(guild, vc):
                    return False, (f"Ich häng gerade mit Musik in **{vc.channel.name}** "
                                   f"– komm rüber, dann drück nochmal.")
                await vc.move_to(channel)
            # Die Musik verbindet sich taub (spart Bandbreite). Soundboard-Sounds
            # nimmt Discord von einem tauben Flo aber nicht an.
            stimme = getattr(guild.me, "voice", None)
            if stimme is None or stimme.self_deaf or stimme.self_mute:
                await guild.change_voice_state(channel=vc.channel, self_deaf=False,
                                               self_mute=False)
            await vc.channel.send_sound(sound)
        except (discord.ClientException, discord.HTTPException, RuntimeError,
                asyncio.TimeoutError) as exc:
            import music
            log.error("Discord-Sound fehlgeschlagen: %s: %s", type(exc).__name__, exc)
            if neu_da and vc is not None:
                await self._safe_disconnect(vc)
            if isinstance(exc, discord.Forbidden):
                return False, "Discord lässt mich den Sound hier nicht spielen (Rechte?)."
            return False, music.VOICE_KAPUTT
        if neu_da:
            self._spawn(self._nach_sound_gehen(guild, vc))
        return True, ""

    async def _nach_sound_gehen(self, guild, vc):
        """Nur fuer den Sound gekommen -> nach ein paar Sekunden wieder weg,
        ausser die Musik hat den Kanal inzwischen uebernommen."""
        await asyncio.sleep(_NACH_SOUND_SEK)
        if (vc.is_connected() and not self._voice_beschaeftigt(guild)
                and not self._musik_hat_sie(guild, vc)):
            await self._safe_disconnect(vc)

    async def _klick(self, interaction, art, wert):
        """Ein Knopf oder Menue-Eintrag am Brett."""
        guild = interaction.guild

        async def sagen(text):
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

        if not self.soundboard_enabled(getattr(guild, "id", 0)):
            await sagen("Das Soundboard ist gerade **deaktiviert**. 🔇")
            return
        vs = getattr(interaction.user, "voice", None)
        channel = vs.channel if vs and vs.channel else None
        if channel is None:
            await sagen("Geh erst in einen Sprachkanal, dann drück nochmal. 🎧")
            return
        if art == "datei":
            path = self._find_sound(wert)
            if path is None:
                await interaction.response.send_message(
                    f"`{wert}` ist verschwunden. 👻", ephemeral=True)
                return
            if self._voice_beschaeftigt(guild):
                await interaction.response.send_message(
                    "Gerade läuft Musik – Datei-Sounds müssen warten. Die Server- und "
                    "Discord-Sounds im Menü gehen trotzdem. 🎶", ephemeral=True)
                return
            name, spielen = path.stem, self._play_path(guild, channel, str(path))
        else:
            # Die Sound-Liste kann einen Abruf bei Discord kosten (nach einem
            # Neustart oder alle paar Minuten) - erst bestaetigen, sonst reisst
            # unter Last die 3-Sekunden-Frist.
            await interaction.response.defer(ephemeral=True, thinking=True)
            sound = await self._finde_discord_sound(guild, art, wert)
            if sound is None:
                await sagen("Den Sound gibt's nicht mehr. 👻")
                return
            name, spielen = sound.name, self._discord_sound_spielen(guild, channel, sound)
        # Sofort bestaetigen (ein Datei-Sound spielt bis zu 60 s im Hintergrund).
        if interaction.response.is_done():
            await interaction.followup.send(f"🔊 **{name}**", ephemeral=True)
        else:
            await interaction.response.send_message(f"🔊 **{name}**", ephemeral=True,
                                                    delete_after=6)
        self._spawn(self._melden(interaction, spielen))

    async def _melden(self, interaction, spielen):
        ok, err = await spielen
        if not ok and err:
            try:
                await interaction.followup.send(err, ephemeral=True)
            except discord.HTTPException:
                pass

    async def _cmd_sound(self, message, rest):
        if not rest.strip():
            return f"Welchen Sound? `{self._bot_name} sounds` zeigt alle."
        path = self._find_sound(rest)
        sound = None
        if path is None:
            sound = await self._finde_discord_sound(message.guild, "alle", rest)
            if sound is None:
                return (f"Den Sound `{rest.strip()}` kenne ich nicht. "
                        f"`{self._bot_name} sounds` zeigt alle.")
        channel = self._user_voice_channel(message)
        if channel is None:
            return "Geh erst in einen Sprachkanal, dann lege ich los."
        if sound is not None:
            ok, err = await self._discord_sound_spielen(message.guild, channel, sound)
            return f"🔊 **{sound.name}**" if ok else err
        ok, err = await self._play_path(message.guild, channel, str(path))
        if not ok:
            return err
        return f"🔊 **{path.stem}**"

    async def _cmd_say(self, message, text):
        if not self._tts_engine:
            return ("TTS ist nicht eingerichtet. Installier `espeak-ng` "
                    "(`apt install espeak-ng`) oder das Python-Paket `gTTS`.")
        # Fuehrende Bindestriche weg, BEVOR der Text an ein fremdes Programm geht
        # (siehe _synthesize). Zweiter Riegel neben dem "--" dort: gTTS und
        # kuenftige Engines haben ihre eigene Optionserkennung.
        text = text.strip().lstrip("-").strip()
        if not text:
            return f"Was soll ich sagen? `{self._bot_name} sprich Hallo zusammen`"
        if len(text) > 300:
            text = text[:300]
        channel = self._user_voice_channel(message)
        if channel is None:
            return "Geh erst in einen Sprachkanal, dann sag ich's dort."
        try:
            wav = await self._synthesize(text)
        except Exception:  # noqa: BLE001
            log.exception("TTS-Synthese fehlgeschlagen")
            return "Das Aussprechen hat gerade nicht geklappt."
        if wav is None:
            return "Das Aussprechen hat gerade nicht geklappt."
        try:
            ok, err = await self._play_path(message.guild, channel, wav)
        finally:
            self._safe_unlink(wav)
        if not ok:
            return err
        return f"🗣️ \"{text}\""

    def _user_voice_channel(self, message):
        vs = getattr(message.author, "voice", None)
        return vs.channel if vs and vs.channel else None

    # --- TTS-Synthese --------------------------------------------------------
    async def _synthesize(self, text):
        """Erzeugt eine Audiodatei aus Text. Rueckgabe: Pfad (Aufrufer loescht sie)."""
        if self._tts_engine == "gtts":
            return await asyncio.to_thread(self._gtts_to_file, text)
        if self._tts_engine in ("espeak-ng", "espeak"):
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            # "--" beendet die Optionen. OHNE das liest espeak jeden Text mit
            # fuehrendem Bindestrich als OPTION - und das ist keine Theorie:
            #   Flo sprich -w/opt/flobot/data/economy.json
            #     -> espeak schreibt sein WAV DORTHIN und zerstoert die Daten
            #   Flo sprich -f/opt/flobot/.env
            #     -> espeak LIEST DIE DATEI VOR, also den Discord-Token im Voice
            # Jeder Nutzer, keine Rechtepruefung. create_subprocess_exec schuetzt
            # nur vor der Shell, nicht vor der Optionserkennung des Programms.
            proc = await asyncio.create_subprocess_exec(
                self._tts_engine, "-v", "de", "-s", "150", "-w", path, "--", text,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0 and os.path.getsize(path) > 0:
                return path
            self._safe_unlink(path)
        return None

    def _gtts_to_file(self, text):
        try:
            from gtts import gTTS
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            gTTS(text=text, lang="de").save(path)
            return path
        except Exception:  # noqa: BLE001
            log.exception("gTTS fehlgeschlagen")
            return None

    def _safe_unlink(self, path):
        try:
            os.unlink(path)
        except OSError:
            pass

    # --- Voice-Wiedergabe (vertraegt sich mit der Musik) ---------------------
    async def _play_path(self, guild, channel, source):
        """Spielt eine Datei. Reagiert ruecksichtsvoll auf einen schon laufenden
        Voice-Client (z. B. Musik): wird gerade gespielt, lehnt es hoeflich ab."""
        # Belegt die Musik den Voice-Channel (auch in Songpausen / beim Tempo-Wechsel /
        # waehrend eines Reconnects)? Dann NICHT reingraetschen - sonst kapern wir ihren
        # Voice-Client und sie bricht ab ("random leave").
        try:
            import music
            if music.is_voice_busy(guild.id):
                return (False, "Ich bin gerade im Voice mit Musik beschäftigt. "
                               "Kurz warten oder `Flo stop`.")
        except Exception:  # noqa: BLE001 - im Zweifel einfach normal weitermachen
            pass
        vc = guild.voice_client
        created = False
        try:
            if vc is None or not vc.is_connected():
                vc = await channel.connect(self_deaf=True)
                created = True
            else:
                if vc.is_playing() or vc.is_paused():
                    if not created:
                        return (False, "Ich bin gerade im Voice beschäftigt (Musik läuft). "
                                       "Kurz warten oder `Flo stop`.")
                if vc.channel.id != channel.id and not (vc.is_playing() or vc.is_paused()):
                    # Die Verbindung der Musik (Sitzung offen, gerade still) NICHT
                    # in einen anderen Kanal ziehen - ein Join-Sound hat sie
                    # sonst dorthin verschleppt und dort gelassen.
                    import music
                    if music.gehoert_der_musik(guild.id, vc):
                        return (False, f"Ich häng mit der Musik in **{vc.channel.name}** "
                                       f"– komm rüber.")
                    await vc.move_to(channel)
        except (discord.ClientException, discord.HTTPException, RuntimeError,
                asyncio.TimeoutError) as exc:
            # Derselbe Satz wie bei der Musik (music.VOICE_KAPUTT), der Grund
            # steht im Log: RuntimeError = davey/PyNaCl fehlt (discord.py >= 2.7),
            # TimeoutError = Handshake haengt. Den TimeoutError liess dieser Weg
            # vorher durch, und bot.py sagte nur "Da ist gerade etwas
            # schiefgelaufen." - bzw. beim Knopf/Join-Sound gar nichts.
            import music
            log.error("Voice-Connect (Gag) fehlgeschlagen: %s: %s",
                      type(exc).__name__, exc)
            return (False, music.VOICE_KAPUTT)

        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _after(err):
            if err:
                log.error("Gag-Wiedergabe-Fehler: %s", err)
            loop.call_soon_threadsafe(done.set)

        audio = None
        try:
            audio = discord.FFmpegPCMAudio(source, options=_FFMPEG_OPTS)
            vc.play(audio, after=_after)
        except (discord.ClientException, OSError) as exc:
            log.error("Konnte Sound nicht starten: %s", exc)
            if audio is not None:
                audio.cleanup()   # bereits gestarteten ffmpeg-Prozess beenden (kein Zombie)
            if created:
                await self._safe_disconnect(vc)
            return (False, "Den Sound konnte ich nicht abspielen.")

        try:
            await asyncio.wait_for(done.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        if created and not self._musik_hat_sie(guild, vc):
            await self._safe_disconnect(vc)
        return (True, "")

    @staticmethod
    def _musik_hat_sie(guild, vc):
        """Hat die Musik diese Verbindung uebernommen? ('Flo spiel X', waehrend
        ein Gag lief.) Dann NICHT trennen - sonst riss der Gag beim Ende die
        Musik mit, und flo_getrennt meldete einen Rauswurf samt leerer
        Warteschlange."""
        try:
            import music
            return music.gehoert_der_musik(guild.id, vc)
        except Exception:  # noqa: BLE001
            return False

    async def _safe_disconnect(self, vc):
        try:
            await vc.disconnect(force=True)
        except Exception:  # noqa: BLE001
            pass

    # --- Join-Sounds (bot.py ruft on_voice_state_update auf) -----------------
    def _find_join_sound(self, user_id):
        if not JOIN_DIR.exists():
            return None
        for stem in (str(user_id), "default"):
            for ext in _AUDIO_EXTS:
                p = JOIN_DIR / f"{stem}{ext}"
                if p.is_file():
                    return p
        return None

    async def on_voice_state_update(self, member, before, after):
        """Spielt einen Join-Sound, wenn jemand NEU einen Sprachkanal betritt."""
        if not self._enabled or member.bot:
            return
        if not self.join_sounds_an(getattr(getattr(member, "guild", None), "id", 0)):
            return
        if after.channel is None:
            return
        if before.channel is not None and before.channel.id == after.channel.id:
            return  # nur Mute/Deaf geaendert, kein echter Beitritt
        path = self._find_join_sound(member.id)
        if path is None:
            return
        guild = member.guild
        vc = guild.voice_client
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            return  # Musik laeuft - nicht stoeren
        await self._play_path(guild, after.channel, str(path))


instance = VoiceGags()

# Modul-Aliase: bot.py/admin.py nutzen weiterhin die gewohnten Modulnamen.
# (_store/_enabled bewusst OHNE Alias - sie werden zur Laufzeit neu zugewiesen,
# Zugriff darauf laeuft ueber voicegags.instance.)
setup = instance.setup
is_enabled = instance.is_enabled
soundboard_enabled = instance.soundboard_enabled
join_sounds_an = instance.join_sounds_an
altlast_migrieren = instance.altlast_migrieren
handle = instance.handle
on_voice_state_update = instance.on_voice_state_update
