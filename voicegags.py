"""Voice-Gags (Pack 4): Soundboard, TTS und Join-Sounds.

Befehle (nach 'Flo'):
- sound <name>     spielt sounds/<name>.(mp3|wav|ogg|...) im Sprachkanal
- sounds           listet die verfuegbaren Sounds
- sprich <text>    spricht den Text per TTS aus (espeak-ng offline, oder gTTS)

Join-Sounds (optional, JOIN_SOUNDS=1): Betritt jemand einen Sprachkanal und es
gibt sounds/join/<user_id>.* (oder sounds/join/default.*), spielt Flo den Sound.

Voraussetzungen wie bei der Musik: ffmpeg + PyNaCl (+ davey bei discord.py >= 2.7).
Laeuft schon ein anderer Sound/Musik im Kanal, weicht das Modul hoeflich aus,
statt die Musik abzuwuergen. Die Sound-Dateien legt der Nutzer selbst in sounds/ ab.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

import discord

import ai
from store import JsonStore

log = logging.getLogger("dcbot.voice")

# Sentinel: voicegags hat selbst geantwortet (Soundboard-Menue) -> bot.py schweigt.
HANDLED = object()

SOUNDS_DIR = Path(os.getenv("SOUNDS_DIR", str(Path(__file__).resolve().parent / "sounds")))
JOIN_DIR = SOUNDS_DIR / "join"
_AUDIO_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".opus", ".flac")

_FFMPEG_OPTS = "-vn"


# --- Soundboard-Menue: ein Button je Sound, Klick = sofort abspielen -------
_SB_EMOJIS = ("🔊", "🎺", "📣", "💥", "🎵", "😂", "🔥", "🎉", "🥁", "📢")
_SB_STYLES = (discord.ButtonStyle.primary, discord.ButtonStyle.success,
              discord.ButtonStyle.danger, discord.ButtonStyle.secondary)


class _SoundBtn(discord.ui.Button):
    def __init__(self, name, idx):
        super().__init__(label=name[:20], emoji=_SB_EMOJIS[idx % len(_SB_EMOJIS)],
                         style=_SB_STYLES[idx % len(_SB_STYLES)], row=idx // 5)
        self.sound_name = name

    async def callback(self, interaction):
        if not instance.soundboard_enabled():
            await interaction.response.send_message(
                "Das Soundboard ist gerade **deaktiviert**. 🔇", ephemeral=True)
            return
        member = interaction.user
        vs = getattr(member, "voice", None)
        channel = vs.channel if vs and vs.channel else None
        if channel is None:
            await interaction.response.send_message(
                "Geh erst in einen Sprachkanal, dann drück nochmal. 🎧", ephemeral=True)
            return
        path = instance._find_sound(self.sound_name)
        if path is None:
            await interaction.response.send_message(
                f"`{self.sound_name}` ist verschwunden. 👻", ephemeral=True)
            return
        if instance._voice_beschaeftigt(interaction.guild):
            await interaction.response.send_message(
                "Gerade läuft was im Voice – gleich nochmal probieren. 🎶",
                ephemeral=True)
            return
        # Sofort bestaetigen (der Sound spielt bis zu 60 s im Hintergrund).
        await interaction.response.send_message(
            f"🔊 **{self.sound_name}**", ephemeral=True, delete_after=6)
        instance._spawn(instance._play_and_report(
            interaction, interaction.guild, channel, str(path)))


class SoundboardView(discord.ui.View):
    """Bunte Sound-Buttons - JEDER darf druecken (es ist ein Soundboard 😄)."""

    def __init__(self, sounds):
        super().__init__(timeout=600)
        self.message = None
        for i, name in enumerate(sounds[:25]):     # Discord: max 25 Buttons
            self.add_item(_SoundBtn(name, i))

    async def on_timeout(self):
        for ch in self.children:
            ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            instance._release(self.message)


class VoiceGags:
    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        self._tts_engine = ""          # "gtts", "espeak-ng", "espeak" oder "" (aus)
        self._join_sounds = False
        self._store = None   # persistente Schalter (Soundboard an/aus)

        # Hintergrund-Tasks (Sound spielt bis zu 60 s - Button antwortet sofort).
        self._bg = set()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _protect(self, msg):
        if msg is None:
            return
        try:
            import bot
            bot.protect_message(msg)
        except Exception:
            pass

    def _release(self, msg):
        if msg is None:
            return
        try:
            import bot
            bot.release_message(msg)
        except Exception:
            pass

    def setup(self):
        """Aktiv, wenn Voice moeglich ist (ffmpeg + PyNaCl). TTS-Engine wird erkannt."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("VOICE_GAGS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Voice-Gags aus (VOICE_GAGS_ENABLED=0).")
            return False
        if shutil.which("ffmpeg") is None:
            log.info("Voice-Gags aus: ffmpeg fehlt.")
            return False
        try:
            import nacl  # noqa: F401
        except ImportError:
            log.info("Voice-Gags aus: PyNaCl fehlt.")
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
        self._join_sounds = os.getenv("JOIN_SOUNDS", "0").strip().lower() in ("1", "true", "yes", "on")
        self._store = JsonStore("voicegags.json", default={"soundboard": True})
        self._enabled = True
        log.info(
            "Voice-Gags aktiv (Sounds: %s, TTS: %s, Join-Sounds: %s).",
            self._count_sounds(), self._tts_engine or "aus", "an" if self._join_sounds else "aus",
        )
        return True

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

    def soundboard_enabled(self):
        """Owner-Schalter: darf das Soundboard gerade benutzt werden?"""
        if self._store is None:
            return True
        return bool(self._store.data.get("soundboard", True))

    async def set_soundboard(self, an):
        """Schaltet das Soundboard an/aus (persistiert; nur admin.py ruft das)."""
        if self._store is None:
            return
        self._store.data["soundboard"] = bool(an)
        await self._store.save()

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
        for p in SOUNDS_DIR.iterdir():
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS and p.stem.lower() == name:
                return p
        return None

    def _list_sounds(self):
        if not SOUNDS_DIR.exists():
            return []
        return sorted(p.stem for p in SOUNDS_DIR.iterdir()
                      if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)

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

        if first in ("sounds", "soundboard", "soundliste"):
            if not self.soundboard_enabled():
                return "Das Soundboard ist gerade **deaktiviert**. 🔇"
            sounds = self._list_sounds()
            if not sounds:
                return (f"Noch keine Sounds da. Leg Dateien in `{SOUNDS_DIR.name}/` "
                        f"(mp3/wav/ogg), dann geht `{self._bot_name} sound <name>`.")
            return await self._open_soundboard(message, sounds)

        if first in ("sound", "sb", "soundeffekt"):
            if not self.soundboard_enabled():
                return "Das Soundboard ist gerade **deaktiviert**. 🔇"
            return await self._cmd_sound(message, rest)

        if first in ("sprich", "tts", "say", "vorlesen"):
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

    async def _play_and_report(self, interaction, guild, channel,
                               source):
        ok, err = await self._play_path(guild, channel, source)
        if not ok and err:
            try:
                await interaction.followup.send(err, ephemeral=True)
            except discord.HTTPException:
                pass

    async def _open_soundboard(self, message, sounds):
        emb = discord.Embed(
            title="🔊 Flo Soundboard",
            description="Ab in den Voice und **drücken**! 👇",
            color=discord.Color.blurple())
        if len(sounds) > 25:
            emb.description += f"\n({len(sounds) - 25} weitere per `{self._bot_name} sound <name>`)"
        emb.set_footer(text=f"{len(sounds)} Sounds · eigene Dateien einfach in "
                            f"{SOUNDS_DIR.name}/ legen")
        view = SoundboardView(sounds)
        try:
            msg = await message.reply(embed=emb, view=view, mention_author=False)
            view.message = msg
            self._protect(msg)
        except discord.HTTPException:
            log.exception("Soundboard konnte nicht gesendet werden")
        return HANDLED

    async def _cmd_sound(self, message, rest):
        if not rest.strip():
            return f"Welchen Sound? `{self._bot_name} sounds` zeigt alle."
        path = self._find_sound(rest)
        if path is None:
            return f"Den Sound `{rest.strip()}` kenne ich nicht. `{self._bot_name} sounds` zeigt alle."
        channel = self._user_voice_channel(message)
        if channel is None:
            return "Geh erst in einen Sprachkanal, dann lege ich los."
        ok, err = await self._play_path(message.guild, channel, str(path))
        if not ok:
            return err
        return f"🔊 **{path.stem}**"

    async def _cmd_say(self, message, text):
        if not self._tts_engine:
            return ("TTS ist nicht eingerichtet. Installier `espeak-ng` "
                    "(`apt install espeak-ng`) oder das Python-Paket `gTTS`.")
        text = text.strip()
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
            proc = await asyncio.create_subprocess_exec(
                self._tts_engine, "-v", "de", "-s", "150", "-w", path, text,
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
                    await vc.move_to(channel)
        except RuntimeError as exc:
            # discord.py >= 2.7 wirft RuntimeError('davey library needed ...'), wenn die
            # Voice-Verschluesselung fehlt (haeufig auf dem Server). Klar benennen.
            log.error("Voice nicht moeglich (Gag): %s", exc)
            return (False, "Voice ist hier gerade nicht eingerichtet "
                           "(auf dem Server fehlt vermutlich `davey`).")
        except (discord.ClientException, discord.HTTPException) as exc:
            log.error("Voice-Connect (Gag) fehlgeschlagen: %s", exc)
            return (False, "Ich komme gerade nicht in den Sprachkanal.")

        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _after(err):
            if err:
                log.error("Gag-Wiedergabe-Fehler: %s", err)
            loop.call_soon_threadsafe(done.set)

        try:
            audio = discord.FFmpegPCMAudio(source, options=_FFMPEG_OPTS)
            vc.play(audio, after=_after)
        except (discord.ClientException, OSError) as exc:
            log.error("Konnte Sound nicht starten: %s", exc)
            if created:
                await self._safe_disconnect(vc)
            return (False, "Den Sound konnte ich nicht abspielen.")

        try:
            await asyncio.wait_for(done.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        if created:
            await self._safe_disconnect(vc)
        return (True, "")

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
        if not self._enabled or not self._join_sounds or member.bot:
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
set_soundboard = instance.set_soundboard
handle = instance.handle
on_voice_state_update = instance.on_voice_state_update
