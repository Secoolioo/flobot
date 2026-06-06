"""Discord-Bot: setzt das Server-Icon je nach Tageszeit und Jahreszeit.

Start:
    python bot.py            # Dauerbetrieb (prueft regelmaessig)
    python bot.py --once     # einmalig setzen und beenden (z. B. fuer cron)
    python bot.py --check    # nur pruefen (Login, Rechte, Bilder), nichts aendern
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from dotenv import load_dotenv

import ai
import casino
import economy
import fun
import games
import moderation
import music
import schedule_logic
import voicegags

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dcbot")

TOKEN = os.getenv("DISCORD_TOKEN")
APPLICATION_ID = os.getenv("APPLICATION_ID", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
# Bot-Besitzer: nur diese Person darf den ganzen Bot per 'Flo restart' neu starten.
# Standard = Secoolio; per .env (OWNER_ID) ueberschreibbar.
OWNER_ID = int(os.getenv("OWNER_ID", "1040135855710404659") or "0")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
IMAGE_DIR = Path(os.getenv("IMAGE_DIR", str(Path(__file__).resolve().parent)))
CHECK_INTERVAL_SECONDS = float(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
STATUS_INTERVAL_SECONDS = float(os.getenv("STATUS_INTERVAL_SECONDS", "10"))

# Auto-Loeschen: In diesen Channels werden ALLE Nachrichten (auch die des Bots
# selbst) nach AUTODELETE_SECONDS Sekunden geloescht. Mehrere IDs per Komma
# trennen. Der Bot braucht dort das Recht 'Nachrichten verwalten' (Manage
# Messages). Leerer Wert -> Funktion aus.
AUTODELETE_SECONDS = float(os.getenv("AUTODELETE_SECONDS", "60"))
AUTODELETE_CHANNEL_IDS = {
    int(part)
    for part in re.split(
        r"[,\s]+", os.getenv("AUTODELETE_CHANNEL_IDS", "1512045750362837013").strip()
    )
    if part.isdigit()
}
# Sicherheitsnetz: Wie oft (Sekunden) ein Hintergrund-Sweep die Auto-Loesch-
# Channels nach Altlasten durchforstet - Backlog, der vor dem Start lag, oder
# Nachrichten, die einen Neustart im 60-s-Fenster ueberlebt haben. Geloescht wird
# ALLES, was aelter als AUTODELETE_SECONDS ist (ausser Level-Ups + Angepinntes).
AUTODELETE_SWEEP_SECONDS = float(os.getenv("AUTODELETE_SWEEP_SECONDS", "30"))

# KI-Feature ('Flo') initialisieren - liest ANTHROPIC_API_KEY etc. aus der .env.
# Ohne API-Key bleibt das Feature aus und der Bot laeuft wie gehabt weiter.
AI_ENABLED = ai.setup()
# Trigger: der Name "Flo" ODER ein Alias ("Florian", per BOT_ALIASES anpassbar) -
# als ganzes Wort irgendwo in der Nachricht. So reagiert Flo wie eine Alexa,
# sobald jemand "Flo" oder "Florian" schreibt.
_TRIGGER_RE = ai.trigger_re()

# Musik-Feature initialisieren (YouTube via yt-dlp, Spotify-Aufloesung ueber die
# Spotify-API). Ohne yt-dlp/ffmpeg/PyNaCl bleibt es aus, der Bot laeuft weiter.
MUSIC_ENABLED = music.setup()

# Spass-Features (jedes faellt einzeln aus, ohne den Rest zu stoeren):
#   economy  = Level & Flo Coins (XP fuers Schreiben/Voice, Shop, Daily)
#   games    = Mini-Games & Zufalls-Events (Quiz, Slot, Counting ...)
#   fun      = Chaos & Persoenlichkeit (Roast/Hype/Spruch, Reactions) - braucht KI
#   voicegags= Soundboard, TTS, Join-Sounds - braucht ffmpeg/PyNaCl wie die Musik
ECONOMY_ENABLED = economy.setup()
GAMES_ENABLED = games.setup()
FUN_ENABLED = fun.setup()
VOICE_GAGS_ENABLED = voicegags.setup()
# Casino (Blackjack, Crash, Keno, Roulette) - spielt mit den Flo Coins aus economy.
# Faellt aus, wenn economy aus ist (dort liegt der Coin-Topf).
CASINO_ENABLED = casino.setup()
# Moderation (Nachrichten loeschen / Purge). Faellt nie technisch aus - das noetige
# Recht 'Nachrichten verwalten' wird erst beim Befehl pro Nutzer/Bot geprueft.
MOD_ENABLED = moderation.setup()

# Takt fuer Zufalls-Events (Sekunden). Bei jedem Tick zieht games.maybe_event mit
# kleiner Wahrscheinlichkeit (GAMES_EVENT_CHANCE) ein Event.
EVENT_INTERVAL_SECONDS = float(os.getenv("GAMES_EVENT_INTERVAL", "300"))

if "--once" in sys.argv:
    MODE = "once"
elif "--check" in sys.argv:
    MODE = "check"
else:
    MODE = "loop"

# 10 kurze Weisheiten, die rotierend im Bot-Status erscheinen.
WEISHEITEN = [
    "Wer fällt, lernt aufzustehen.",
    "Kleine Schritte führen weit.",
    "Geduld ist auch eine Stärke.",
    "Mut beginnt mit einem Atemzug.",
    "Wissen wächst, wenn man es teilt.",
    "Ruhe ist die Kraft der Klugen.",
    "Wer zuhört, versteht mehr.",
    "Aus Fehlern werden Wege.",
    "Jeder Tag ist ein neuer Anfang.",
    "Heute ist der jüngste Tag deines Lebens.",
]

# Alle nachrichtengetriebenen Features brauchen die Message-Events + den Text.
_NEED_MESSAGES = any(
    [AI_ENABLED, MUSIC_ENABLED, FUN_ENABLED, ECONOMY_ENABLED, GAMES_ENABLED,
     VOICE_GAGS_ENABLED, CASINO_ENABLED, MOD_ENABLED]
)
intents = discord.Intents.none()
intents.guilds = True
if _NEED_MESSAGES or AUTODELETE_CHANNEL_IDS:
    # guild_messages: noetig, um Nachrichten-Events ueberhaupt zu EMPFANGEN
    # (sonst feuert on_message nie). Ist KEIN privilegiertes Intent.
    intents.guild_messages = True
if _NEED_MESSAGES:
    # message_content: noetig, um den TEXT der Nachricht zu lesen. Privilegiert -
    # muss zusaetzlich im Discord Developer Portal aktiviert sein.
    intents.message_content = True
if MUSIC_ENABLED or VOICE_GAGS_ENABLED or ECONOMY_ENABLED:
    # voice_states: noetig, um zu sehen, wer in welchem Sprachkanal steckt
    # (Musik, Soundboard/Join-Sounds, Voice-XP). Nicht privilegiert.
    intents.voice_states = True
client = discord.Client(
    intents=intents,
    status=discord.Status.idle,
    activity=discord.CustomActivity(name=WEISHEITEN[0]),
)

# Merkt sich das zuletzt gesetzte Bild, damit nicht unnoetig editiert wird
# (Discord limitiert Server-Aenderungen).
_current_filename: str | None = None
_weisheit_index: int = 0


def _split_message(text: str, limit: int = 1900) -> list[str]:
    """Zerlegt lange KI-Antworten in Discord-taugliche Stuecke (<2000 Zeichen)."""
    text = text.strip() or "..."
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    chunks.append(text)
    return chunks


async def _reply_chunks(message: discord.Message, text: str) -> None:
    """Schickt eine (ggf. lange) Antwort: erstes Stueck als Reply, Rest normal."""
    for i, teil in enumerate(_split_message(text)):
        try:
            if i == 0:
                await message.reply(teil, mention_author=False)
            else:
                await message.channel.send(teil)
        except discord.HTTPException as exc:
            log.error("Antwort konnte nicht gesendet werden: %s", exc)
            break


async def _send_reply(
    message: discord.Message, payload: "str | discord.Embed | discord.File"
) -> None:
    """Sendet eine Antwort - Bilder (File) als Anhang, Menues (Embed) als Embed,
    normale Antworten als Text."""
    if isinstance(payload, discord.File):
        try:
            await message.reply(file=payload, mention_author=False)
        except discord.HTTPException as exc:
            log.error("Bild-Antwort konnte nicht gesendet werden: %s", exc)
        return
    if isinstance(payload, discord.Embed):
        try:
            await message.reply(embed=payload, mention_author=False)
        except discord.HTTPException as exc:
            log.error("Embed-Antwort konnte nicht gesendet werden: %s", exc)
        return
    await _reply_chunks(message, payload)


_HELP_RE = re.compile(
    r"^(hilfe|help|befehle|commands?|men[uü]|was kannst du)\b", re.IGNORECASE
)


def _is_help(content: str) -> bool:
    """True, wenn jemand 'Flo hilfe' / 'Florian befehle' o. Ae. schreibt."""
    return bool(_HELP_RE.match(ai.strip_lead(content)))


# --- Neustart (nur Bot-Besitzer) -----------------------------------------
_RESTART_RE = re.compile(
    r"^(?:restart|reboot|neustart\w*|neu\s*starten?|neue?\s*starten?|starte?\s+neu)\b",
    re.IGNORECASE,
)


def _is_restart(content: str) -> bool:
    """True bei 'Flo restart' / 'Flo neustarten' / 'Flo neu starten' usw."""
    return bool(_RESTART_RE.match(ai.strip_lead(content)))


async def _restart_bot() -> None:
    """Startet den GANZEN Prozess neu (re-exec) - funktioniert lokal und unter
    systemd, unabhaengig von einem Supervisor. Vorher Voice/Gateway sauber
    schliessen, damit ffmpeg-Subprozesse nicht verwaisen."""
    await asyncio.sleep(0.4)  # der Interaktions-Antwort Zeit zum Rausgehen geben
    try:
        await client.close()
    except Exception:  # noqa: BLE001 - egal, wir starten gleich eh neu
        log.exception("Schliessen vor dem Neustart fehlgeschlagen")
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    log.warning("Neustart per Re-exec: %s", " ".join(argv))
    try:
        os.execv(argv[0], argv)
    except OSError:
        # Fallback: sauber beenden (systemd 'Restart=' bringt ihn wieder hoch).
        log.exception("Re-exec fehlgeschlagen - beende stattdessen mit Code 42.")
        os._exit(42)


class RestartConfirmView(discord.ui.View):
    """Sicherheitsabfrage vor dem Neustart - nur der Bot-Besitzer darf klicken."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=30)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Nur mein Besitzer darf mich neu starten.", ephemeral=True)
        return False

    @discord.ui.button(label="Ja, neu starten", emoji="🔄", style=discord.ButtonStyle.danger)
    async def _yes(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🔄 Neustart läuft …",
                description="Bin gleich wieder da. Moment …",
                color=discord.Color.orange()),
            view=self)
        log.warning("Neustart angefordert von %s (%s).",
                    interaction.user, interaction.user.id)
        self.stop()
        _spawn(_restart_bot())

    @discord.ui.button(label="Abbrechen", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def _no(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Abgebrochen",
                description="Kein Neustart – alles bleibt, wie es ist.",
                color=discord.Color.greyple()),
            view=None)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# --- Interaktives Hilfe-Menue --------------------------------------------
def _help_categories() -> "list[tuple[str, str, str]]":
    """Liefert (key, emoji, label) je AKTIVEM Bereich - Reihenfolge = Button-Reihenfolge."""
    cats = [
        ("musik", "🎵", "Musik", MUSIC_ENABLED),
        ("spiele", "🎮", "Spiele", GAMES_ENABLED),
        ("economy", "📈", "Level & Coins", ECONOMY_ENABLED),
        ("casino", "🎰", "Casino", CASINO_ENABLED),
        ("chaos", "😈", "Chaos", FUN_ENABLED),
        ("voice", "🔊", "Voice", VOICE_GAGS_ENABLED),
        ("mod", "🛡️", "Moderation", MOD_ENABLED),
        ("ki", "💬", "KI", AI_ENABLED),
    ]
    return [(k, e, l) for k, e, l, on in cats if on]


def _help_overview_embed() -> discord.Embed:
    """Startansicht des Hilfe-Menues - darunter ein Button je Kategorie."""
    name = ai.bot_name()
    aliases = [n for n in ai.names() if n.lower() != name.lower()]
    anrede = f"`{name} ...`"
    if aliases:
        anrede += " oder " + " / ".join(f"`{a} ...`" for a in aliases)
    emb = discord.Embed(
        title=f"🤖 {name} – Hilfe",
        description=(f"Sprich mich mit {anrede} an.\n"
                     "Tippe unten auf eine **Kategorie** für die passenden Befehle. 👇"),
        color=discord.Color.blurple(),
    )
    if client.user is not None:
        try:
            emb.set_thumbnail(url=client.user.display_avatar.url)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko
            pass
    cats = _help_categories()
    if cats:
        emb.add_field(
            name="Aktive Bereiche",
            value="\n".join(f"{e}  **{l}**" for _k, e, l in cats),
            inline=False,
        )
    else:
        emb.add_field(name="—", value="Gerade sind keine Spaß-Features aktiv.", inline=False)
    emb.set_footer(text="Tipp: Kauf dir im Shop einen Titel – dann spricht Flo dich damit an!")
    return emb


def _help_detail_embed(key: str) -> discord.Embed:
    """Detail-Ansicht einer Kategorie (wird beim Button-Klick eingeblendet)."""
    name = ai.bot_name()
    if key == "musik":
        emb = discord.Embed(title="🎵 Musik",
                            description="YouTube & Spotify direkt im Sprachkanal.",
                            color=0x1DB954)
        emb.add_field(name="Abspielen",
            value=(f"`{name} spiel <link/suche>`\n`{name} <youtube/spotify-link>`\n"
                   f"`{name} join` · `{name} leave`"), inline=False)
        emb.add_field(name="Steuerung",
            value=(f"`{name} skip` · `{name} pause` · `{name} weiter` · `{name} stop`\n"
                   "… oder einfach die **Buttons** unter dem laufenden Lied. 😉"), inline=False)
        emb.add_field(name="Warteschlange",
            value=(f"`{name} queue` · `{name} lautstärke 50`\n"
                   "Neues Lied bei laufender Musik? Du bekommst einen Button für die **Position**!"),
            inline=False)
        return emb
    if key == "spiele":
        emb = discord.Embed(title="🎮 Spiele",
                            description="Kleine Spiele für zwischendurch.",
                            color=0xE67E22)
        emb.add_field(name="Raten & Quiz",
            value=f"`{name} quiz` · `{name} zahlenraten`", inline=False)
        emb.add_field(name="Schnelle Runden",
            value=(f"`{name} ssp schere/stein/papier`\n"
                   f"`{name} coinflip 50 kopf` · `{name} slot 20` · `{name} würfel 2d6`"),
            inline=False)
        return emb
    if key == "economy":
        emb = discord.Embed(title="📈 Level & Flo Coins",
                            description="Sammle XP und Coins, gib in den Shop.",
                            color=0xF1C40F)
        emb.add_field(name="Level",
            value=f"`{name} level` · `{name} top`", inline=False)
        emb.add_field(name="Coins",
            value=f"`{name} coins` · `{name} daily` · `{name} pay @x 100`", inline=False)
        emb.add_field(name="Shop",
            value=(f"`{name} shop` · `{name} kaufen sigma` · `{name} inventar` · "
                   f"`{name} titel sigma`"), inline=False)
        return emb
    if key == "casino":
        emb = discord.Embed(title="🎰 Casino",
                            description="Setz deine Flo Coins – mit Köpfchen. 😏",
                            color=0xE91E63)
        emb.add_field(name="Übersicht", value=f"`{name} casino`", inline=False)
        emb.add_field(name="Blackjack",
            value=(f"`{name} blackjack 50` – danach per **Button** "
                   "`Karte` / `Stand` / `Double`."), inline=False)
        emb.add_field(name="Weitere Spiele",
            value=(f"`{name} crash 50 2.0` · `{name} keno 50 3 7 12` · "
                   f"`{name} roulette 50 rot`"), inline=False)
        return emb
    if key == "chaos":
        emb = discord.Embed(title="😈 Chaos",
                            description="Für die ganz feinen Sprüche.",
                            color=0x9B59B6)
        emb.add_field(name="Auf Personen",
            value=(f"`{name} roast @x` · `{name} hype @x` · `{name} rate @x` · "
                   f"`{name} rizz @x`"), inline=False)
        emb.add_field(name="Einfach so",
            value=f"`{name} spruch` · `{name} horoskop`", inline=False)
        return emb
    if key == "voice":
        emb = discord.Embed(title="🔊 Voice",
                            description="Sounds & Sprachausgabe im Sprachkanal.",
                            color=0x1ABC9C)
        emb.add_field(name="Befehle",
            value=(f"`{name} sounds` · `{name} sound <name>` · `{name} sprich <text>`"),
            inline=False)
        return emb
    if key == "mod":
        emb = discord.Embed(title="🛡️ Moderation",
                            description="Nur fürs Team – jede Aktion prüft Rechte.",
                            color=0xED4245)
        emb.add_field(name="Verwarnen",
            value=f"`{name} warn @x Grund` · `{name} warns @x` · `{name} unwarn @x`",
            inline=False)
        emb.add_field(name="Timeout / Mute",
            value=f"`{name} timeout @x 10m Grund` · `{name} untimeout @x`", inline=False)
        emb.add_field(name="Kick / Bann",
            value=f"`{name} kick @x Grund` · `{name} ban @x Grund` · `{name} unban <ID>`",
            inline=False)
        emb.add_field(name="Aufräumen",
            value=(f"`{name} lösch <anzahl>` · `{name} lösch alle` · `{name} nuke`\n"
                   "Angepinnte Nachrichten bleiben beim Löschen erhalten."), inline=False)
        return emb
    if key == "ki":
        emb = discord.Embed(title="💬 KI",
                            description="Kein Befehl erkannt? Dann antworte ich wie eine KI.",
                            color=0x5865F2)
        emb.add_field(name="So gehts",
            value=(f"Stell mir einfach eine Frage:\n`{name} wie wird das Wetter?`"),
            inline=False)
        return emb
    return _help_overview_embed()


class _HelpNavButton(discord.ui.Button):
    """Ein Navigations-Button im Hilfe-Menue. key=None => Übersicht."""

    def __init__(self, key: "str | None", emoji: str, label: str, *,
                 style: discord.ButtonStyle) -> None:
        super().__init__(label=label, emoji=emoji, style=style)
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "HelpView" = self.view  # type: ignore[assignment]
        await view.show(interaction, self.key)


class HelpView(discord.ui.View):
    """Interaktives Hilfe-Menue: Kategorie-Buttons wechseln das Embed in-place."""

    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.message: discord.Message | None = None
        self.active: str | None = None
        self.add_item(_HelpNavButton(None, "🏠", "Übersicht",
                                     style=discord.ButtonStyle.primary))
        for key, emoji, label in _help_categories():
            self.add_item(_HelpNavButton(key, emoji, label,
                                         style=discord.ButtonStyle.secondary))
        self._sync()

    def _sync(self) -> None:
        """Hebt den aktiven Bereich hervor (grün + deaktiviert)."""
        for child in self.children:
            if isinstance(child, _HelpNavButton):
                here = (child.key == self.active)
                child.disabled = here
                child.style = (discord.ButtonStyle.success if here else
                               (discord.ButtonStyle.primary if child.key is None
                                else discord.ButtonStyle.secondary))

    async def show(self, interaction: discord.Interaction, key: "str | None") -> None:
        self.active = key
        self._sync()
        emb = _help_overview_embed() if key is None else _help_detail_embed(key)
        await interaction.response.edit_message(embed=emb, view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def invite_url() -> str:
    """OAuth2-Einladungslink mit den noetigen Rechten.

    permissions=1099511704614 = Kanal ansehen (1024) + Nachrichten senden (2048)
    + 'Server verwalten' (32, fuers Icon) + 'Nachrichten verwalten' (8192, fuers
    Auto-Loeschen/Purge) + 'Nachrichtenverlauf anzeigen' (65536, fuers Loeschen)
    + 'Mitglieder kicken' (2) + 'Mitglieder bannen' (4) + 'Mitglieder im Timeout'
    (1099511627776, fuer Timeout/Mute). Summe = 1099511704614.
    """
    return (
        f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
        "&permissions=1099511704614&scope=bot"
    )


async def update_icon(*, force: bool = False) -> bool:
    """Setzt das Server-Icon, falls ein anderes Bild faellig ist."""
    global _current_filename
    now = datetime.now(TIMEZONE)
    filename = schedule_logic.get_image_filename(now)

    if filename == _current_filename and not force:
        return False

    path = IMAGE_DIR / filename
    if not path.exists():
        log.error("Bilddatei fehlt: %s", path)
        return False

    guild = client.get_guild(GUILD_ID)
    if guild is None:
        log.error(
            "Server %s nicht gefunden. Bot einladen: %s", GUILD_ID, invite_url()
        )
        return False

    if not guild.me.guild_permissions.manage_guild:
        log.error("Bot fehlt die Berechtigung 'Server verwalten' (Manage Server).")
        return False

    try:
        data = path.read_bytes()
        await guild.edit(
            icon=data,
            reason="Automatische Tageszeit-/Jahreszeit-Anpassung",
        )
    except (discord.HTTPException, OSError) as exc:
        log.error("Icon-Aenderung fehlgeschlagen: %s", exc)
        return False

    _current_filename = filename
    log.info(
        "Server-Icon gesetzt: %s  (%s)",
        filename,
        now.strftime("%Y-%m-%d %H:%M %Z"),
    )
    return True


async def run_check() -> None:
    """Diagnose: Login, Server, Rechte und Bilder pruefen - ohne Aenderung."""
    now = datetime.now(TIMEZONE)
    target = schedule_logic.get_image_filename(now)
    log.info("Lokale Zeit: %s", now.strftime("%Y-%m-%d %H:%M %Z"))
    log.info("Aktuell faelliges Bild: %s", target)

    guild = client.get_guild(GUILD_ID)
    if guild is None:
        log.error("Server %s NICHT gefunden - Bot ist dort wohl nicht.", GUILD_ID)
        log.error("Einladen mit: %s", invite_url())
        return

    log.info("Server gefunden: %s (Mitglieder: %s)", guild.name, guild.member_count)
    has_perm = guild.me.guild_permissions.manage_guild
    log.info(
        "Berechtigung 'Server verwalten': %s",
        "JA" if has_perm else "NEIN - bitte dem Bot diese Rolle/Recht geben!",
    )

    log.info("Bilder im Ordner %s:", IMAGE_DIR)
    for fn in schedule_logic.all_image_filenames():
        exists = (IMAGE_DIR / fn).exists()
        log.info("   [%s] %s", "vorhanden" if exists else "  FEHLT  ", fn)


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def icon_loop() -> None:
    # Prueft regelmaessig (Standard: jede Minute), ob ein anderes Bild faellig
    # ist. Discord wird nur angesprochen, wenn sich das Bild wirklich aendert.
    # try/except, damit eine einzelne Fehlrunde die Schleife NICHT stoppt.
    try:
        await update_icon()
    except Exception:
        log.exception("Fehler im Icon-Check - Loop laeuft weiter")


@tasks.loop(seconds=STATUS_INTERVAL_SECONDS)
async def status_loop() -> None:
    """Wechselt alle paar Sekunden den Status-Text (Bot bleibt 'idle')."""
    global _weisheit_index
    weisheit = WEISHEITEN[_weisheit_index % len(WEISHEITEN)]
    _weisheit_index += 1
    try:
        await client.change_presence(
            status=discord.Status.idle,
            activity=discord.CustomActivity(name=weisheit),
        )
        log.info("Status (idle): %s", weisheit)
    except Exception as exc:
        log.error("Status-Update fehlgeschlagen: %s", exc)


@tasks.loop(seconds=economy.VOICE_TICK_SECONDS)
async def voice_xp_loop() -> None:
    """Gibt regelmaessig XP an aktive Mitglieder in Sprachkanaelen (Voice-Zeit)."""
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    try:
        await economy.tick_voice(guild)
    except Exception:
        log.exception("Voice-XP-Loop Fehler - laeuft weiter")


@tasks.loop(seconds=EVENT_INTERVAL_SECONDS)
async def event_loop() -> None:
    """Zieht im Takt mit kleiner Wahrscheinlichkeit ein Zufalls-Event (Schnell-tippen)."""
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    try:
        await games.maybe_event(guild)
    except Exception:
        log.exception("Event-Loop Fehler - laeuft weiter")


def _sweepable(m: discord.Message) -> bool:
    """True = diese Nachricht im Auto-Loesch-Channel darf weg. Level-Up-Ansagen
    des Bots und angepinnte Nachrichten bleiben (wie beim Einzel-Auto-Loeschen)."""
    if m.pinned:
        return False
    if (client.user is not None and m.author.id == client.user.id
            and m.embeds and m.embeds[0].title == economy.LEVELUP_EMBED_TITLE):
        return False
    return True


@tasks.loop(seconds=AUTODELETE_SWEEP_SECONDS)
async def autodelete_sweep_loop() -> None:
    """Sicherheitsnetz fuers Auto-Loeschen: raeumt in den konfigurierten Channels
    ALLES weg, was aelter als AUTODELETE_SECONDS ist - auch Altlasten von vor dem
    Start oder Nachrichten, die einen Neustart ueberlebt haben. So bleibt der
    Channel wirklich leer, nicht nur 'ab jetzt'. Erste Runde laeuft sofort beim
    Start (raeumt den Backlog ab)."""
    if not AUTODELETE_CHANNEL_IDS:
        return
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=AUTODELETE_SECONDS)
    for cid in AUTODELETE_CHANNEL_IDS:
        channel = guild.get_channel(cid)
        if channel is None or not hasattr(channel, "purge"):
            continue
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.manage_messages and perms.read_message_history):
            log.warning(
                "Auto-Loesch-Sweep: mir fehlen Rechte in #%s (Nachrichten verwalten / "
                "Verlauf lesen).", getattr(channel, "name", cid),
            )
            continue
        try:
            await channel.purge(limit=None, check=_sweepable, before=cutoff)
        except discord.HTTPException as exc:
            log.warning("Auto-Loesch-Sweep in #%s fehlgeschlagen: %s",
                        getattr(channel, "name", cid), exc)
        except Exception:
            log.exception("Auto-Loesch-Sweep Fehler - laeuft weiter")


# Laufende Hintergrund-Tasks festhalten, damit der Garbage Collector sie nicht
# vorzeitig einsammelt (asyncio.create_task gibt nur eine schwache Referenz).
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _delete_after(message: discord.Message, delay: float) -> None:
    """Loescht eine Nachricht nach 'delay' Sekunden (best effort)."""
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except discord.NotFound:
        pass  # schon weg (manuell geloescht o. Ae.)
    except discord.Forbidden:
        log.warning(
            "Auto-Loeschen: mir fehlt das Recht 'Nachrichten verwalten' in #%s.",
            getattr(message.channel, "name", message.channel.id),
        )
    except discord.HTTPException as exc:
        log.warning("Auto-Loeschen fehlgeschlagen: %s", exc)


@client.event
async def on_message(message: discord.Message) -> None:
    """Zentrale Nachrichten-Verarbeitung: Auto-Loeschen, passive Spass-Hooks
    (XP, Spiele, Reactions) und - wenn 'Flo' angesprochen wird - Befehle + KI."""
    # Auto-Loeschen: in konfigurierten Channels ALLE Nachrichten nach kurzer Zeit
    # entfernen. Bewusst GANZ oben (vor dem Bot-Check), damit auch die eigenen
    # Antworten des Bots dort wieder verschwinden. AUSNAHME: Level-Up-Ansagen des
    # Bots bleiben stehen (Erfolge sollen sichtbar bleiben).
    if message.channel.id in AUTODELETE_CHANNEL_IDS:
        is_levelup = (
            message.author.id == client.user.id
            and message.embeds
            and message.embeds[0].title == economy.LEVELUP_EMBED_TITLE
        )
        if not is_levelup:
            _spawn(_delete_after(message, AUTODELETE_SECONDS))

    if message.author.bot:
        return
    if message.guild is None:
        return

    content = message.content or ""

    # --- Passive Hooks: sehen JEDE Nachricht (vor dem Flo-Trigger) ---
    # XP/Coins fuers Schreiben (laeuft nebenher, blockiert nicht).
    if ECONOMY_ENABLED:
        _spawn(economy.on_message(message))
    # Laufende Spiele/Events (Counting, Quiz-Antwort, Zahlenraten, Schnell-Event).
    # Gibt True zurueck, wenn die Nachricht ein Spielzug war -> dann sind wir fertig.
    if GAMES_ENABLED:
        try:
            if await games.on_message_passive(message):
                return
        except Exception:
            log.exception("Spiele-Hook fehlgeschlagen")
    # Seltene Zufalls-Einwuerfe / Auto-Reactions (laeuft nebenher).
    if FUN_ENABLED:
        _spawn(fun.on_message_passive(message))

    # --- Ab hier nur, wenn Flo angesprochen wird ---
    angesprochen = bool(_TRIGGER_RE.search(content))
    if not angesprochen and client.user in message.mentions:
        angesprochen = True
    if not angesprochen:
        return

    # 'Flo restart' / 'Flo neustarten' -> kompletter Neustart, NUR fuer den Besitzer.
    if _is_restart(content):
        if message.author.id != OWNER_ID:
            await _send_reply(message, discord.Embed(
                description="Nur mein Besitzer darf mich neu starten. 😉",
                color=discord.Color.red()))
            return
        view = RestartConfirmView(OWNER_ID)
        emb = discord.Embed(
            title="🔄 Kompletten Neustart?",
            description=("Soll ich den **ganzen Bot** neu starten? "
                         "Laufende Musik/Voice wird dabei getrennt."),
            color=discord.Color.orange())
        try:
            view.message = await message.reply(embed=emb, view=view, mention_author=False)
        except discord.HTTPException:
            log.exception("Restart-Abfrage konnte nicht gesendet werden")
        return

    # 'Flo hilfe' / 'Flo befehle' -> interaktives Menue mit Kategorie-Buttons.
    if _is_help(content):
        view = HelpView()
        try:
            view.message = await message.reply(
                embed=_help_overview_embed(), view=view, mention_author=False)
        except discord.HTTPException:
            log.exception("Hilfe konnte nicht gesendet werden")
        return

    # Befehls-Handler der Reihe nach durchgehen. Jeder gibt entweder eine Antwort
    # (Text ODER Embed = Befehl erkannt, fertig) oder None (= naechster ist dran).
    antwort: "str | discord.Embed | discord.File | None" = None
    async with message.channel.typing():
        for enabled, handler in (
            (MOD_ENABLED, moderation.handle),
            (MUSIC_ENABLED, music.handle),
            (VOICE_GAGS_ENABLED, voicegags.handle),
            (GAMES_ENABLED, games.handle),
            (CASINO_ENABLED, casino.handle),
            (ECONOMY_ENABLED, economy.handle),
            (FUN_ENABLED, fun.handle),
        ):
            if not enabled:
                continue
            try:
                antwort = await handler(message)
            except Exception:
                log.exception(
                    "Befehl fehlgeschlagen (%s)", getattr(handler, "__module__", "?")
                )
                antwort = "Da ist gerade etwas schiefgelaufen."
            if antwort is not None:
                break

    if antwort is not None:
        if antwort is moderation.HANDLED or antwort is music.HANDLED or antwort is casino.HANDLED:
            return  # Modul hat selbst geantwortet (Loesch-Bestaetigung / Musik- / Casino-Buttons).
        if isinstance(antwort, discord.File):
            log.info("Befehl von %s: [Bild] %s", message.author.display_name, antwort.filename)
        elif isinstance(antwort, discord.Embed):
            log.info("Befehl von %s: [Embed] %s", message.author.display_name, antwort.title or "")
        else:
            log.info("Befehl von %s: %s", message.author.display_name, antwort[:80])
        await _send_reply(message, antwort)
        return

    # --- KI-Fallback: kein Befehl erkannt -> Flo antwortet wie eine KI ---
    if not AI_ENABLED:
        return
    # Gekaufter Shop-Titel -> Flo spricht den Nutzer damit an.
    title = economy.get_title(message.author.id) if ECONOMY_ENABLED else ""
    log.info("KI-Frage von %s: %s", message.author.display_name, content[:150])
    async with message.channel.typing():
        try:
            antwort = await ai.ask_flo(
                content, author=message.author.display_name, title=title
            )
        except Exception:
            log.exception("KI-Antwort fehlgeschlagen")
            antwort = "Ups, da ist gerade etwas schiefgelaufen. Versuch es gleich nochmal."
    log.info("KI-Antwort an %s (%d Zeichen)", message.author.display_name, len(antwort))
    await _reply_chunks(message, antwort)


@client.event
async def on_voice_state_update(member: discord.Member, before, after) -> None:
    """Join-Sounds: spielt einen Sound, wenn jemand einen Sprachkanal betritt."""
    if VOICE_GAGS_ENABLED:
        _spawn(voicegags.on_voice_state_update(member, before, after))


@client.event
async def on_ready() -> None:
    log.info("Eingeloggt als %s (ID %s)", client.user, client.user.id)
    if MODE in ("check", "once"):
        try:
            if MODE == "check":
                await run_check()
            else:
                await update_icon(force=True)
        finally:
            await client.close()
        return

    # Dauerbetrieb: Loops starten. tasks.loop fuehrt die erste Runde sofort
    # aus, dadurch werden Icon und Status direkt beim Start gesetzt.
    # Bei einem Reconnect feuert on_ready erneut - dank is_running() starten
    # wir die Loops dann nicht doppelt.
    if AI_ENABLED:
        guild = client.get_guild(GUILD_ID)
        if guild is not None:
            lesbar = [
                c.name
                for c in guild.text_channels
                if c.permissions_for(guild.me).view_channel
            ]
            log.info(
                "KI aktiv - lesbare Text-Channels (%d/%d): %s",
                len(lesbar), len(guild.text_channels),
                ", ".join(lesbar) or "KEINE - Bot darf keine Channels lesen!",
            )
    if AUTODELETE_CHANNEL_IDS:
        log.info(
            "Auto-Loeschen aktiv: Channel(s) %s, jeweils nach %.0f s.",
            ", ".join(str(c) for c in sorted(AUTODELETE_CHANNEL_IDS)),
            AUTODELETE_SECONDS,
        )
    if not icon_loop.is_running():
        icon_loop.start()
    if not status_loop.is_running():
        status_loop.start()
    if ECONOMY_ENABLED and not voice_xp_loop.is_running():
        voice_xp_loop.start()
    if GAMES_ENABLED and not event_loop.is_running():
        event_loop.start()
    if AUTODELETE_CHANNEL_IDS and not autodelete_sweep_loop.is_running():
        autodelete_sweep_loop.start()


def main() -> None:
    if not TOKEN:
        log.error("DISCORD_TOKEN fehlt in der .env-Datei.")
        sys.exit(1)
    if not GUILD_ID:
        log.error("GUILD_ID fehlt in der .env-Datei.")
        sys.exit(1)
    log.info("Starte Bot im Modus: %s", MODE)
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
