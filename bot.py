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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from dotenv import load_dotenv

import ai
import music
import schedule_logic

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

# KI-Feature ('Flo') initialisieren - liest ANTHROPIC_API_KEY etc. aus der .env.
# Ohne API-Key bleibt das Feature aus und der Bot laeuft wie gehabt weiter.
AI_ENABLED = ai.setup()
# Trigger: das Wort "Flo" (Gross-/Kleinschreibung egal) irgendwo in der Nachricht.
_TRIGGER_RE = re.compile(rf"\b{re.escape(ai.bot_name())}\b", re.IGNORECASE)

# Musik-Feature initialisieren (YouTube via yt-dlp, Spotify-Aufloesung ueber die
# Spotify-API). Ohne yt-dlp/ffmpeg/PyNaCl bleibt es aus, der Bot laeuft weiter.
MUSIC_ENABLED = music.setup()

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

intents = discord.Intents.none()
intents.guilds = True
if AI_ENABLED or MUSIC_ENABLED:
    # guild_messages: noetig, um Nachrichten-Events ueberhaupt zu EMPFANGEN
    # (sonst feuert on_message nie). Ist KEIN privilegiertes Intent.
    intents.guild_messages = True
    # message_content: noetig, um den TEXT der Nachricht zu lesen. Privilegiert -
    # muss zusaetzlich im Discord Developer Portal aktiviert sein.
    intents.message_content = True
if MUSIC_ENABLED:
    # voice_states: noetig, um zu sehen, in welchem Sprachkanal der Nutzer steckt.
    # Nicht privilegiert.
    intents.voice_states = True
if AUTODELETE_CHANNEL_IDS:
    # Auch ohne KI/Musik muessen wir die Nachrichten-Events empfangen, um sie
    # spaeter loeschen zu koennen. guild_messages ist NICHT privilegiert.
    intents.guild_messages = True
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


def invite_url() -> str:
    """OAuth2-Einladungslink mit den noetigen Rechten.

    permissions=8224 = 'Server verwalten' (32, fuers Icon) + 'Nachrichten
    verwalten' (8192, fuers Auto-Loeschen).
    """
    return (
        f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
        "&permissions=8224&scope=bot"
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
    """Antwortet wie eine KI, wenn jemand 'Flo' schreibt oder den Bot erwaehnt."""
    # Auto-Loeschen: in konfigurierten Channels ALLE Nachrichten nach kurzer Zeit
    # entfernen. Bewusst GANZ oben (vor dem Bot-Check), damit auch die eigenen
    # Antworten des Bots dort wieder verschwinden.
    if message.channel.id in AUTODELETE_CHANNEL_IDS:
        _spawn(_delete_after(message, AUTODELETE_SECONDS))

    if message.author.bot:
        return
    # DIAGNOSE: jede gesehene Nachricht protokollieren - zeigt, ob der Text
    # ueberhaupt ankommt (Message-Content-Intent) und was getriggert wird.
    log.info(
        "Nachricht: ort=%s #%s von %s | inhalt=%r | mentions=%s",
        message.guild.name if message.guild else "DM",
        getattr(message.channel, "name", "?"),
        message.author.display_name,
        message.content,
        [m.name for m in message.mentions],
    )
    if message.guild is None or not (AI_ENABLED or MUSIC_ENABLED):
        return

    content = message.content or ""
    angesprochen = bool(_TRIGGER_RE.search(content))
    if not angesprochen and client.user in message.mentions:
        angesprochen = True
    if not angesprochen:
        return

    # Erst Musik-Befehle pruefen (z. B. "Flo spiel <link>", "Flo <link>", skip,
    # pause, stop). Gibt music.handle einen Text zurueck, war es ein Musik-Befehl
    # und wir sind fertig. Gibt es None zurueck, uebernimmt die KI.
    if MUSIC_ENABLED:
        async with message.channel.typing():
            try:
                musik_antwort = await music.handle(message)
            except Exception:
                log.exception("Musik-Befehl fehlgeschlagen")
                musik_antwort = "Beim Abspielen ist gerade etwas schiefgelaufen."
        if musik_antwort is not None:
            log.info(
                "Musik-Befehl von %s: %s",
                message.author.display_name, musik_antwort[:80],
            )
            try:
                await message.reply(musik_antwort, mention_author=False)
            except discord.HTTPException as exc:
                log.error("Antwort konnte nicht gesendet werden: %s", exc)
            return

    if not AI_ENABLED:
        return

    log.info("KI-Frage von %s: %s", message.author.display_name, content[:150])
    async with message.channel.typing():
        try:
            antwort = await ai.ask_flo(content, author=message.author.display_name)
        except Exception:
            log.exception("KI-Antwort fehlgeschlagen")
            antwort = "Ups, da ist gerade etwas schiefgelaufen. Versuch es gleich nochmal."
    log.info("KI-Antwort an %s (%d Zeichen)", message.author.display_name, len(antwort))

    for i, teil in enumerate(_split_message(antwort)):
        try:
            if i == 0:
                await message.reply(teil, mention_author=False)
            else:
                await message.channel.send(teil)
        except discord.HTTPException as exc:
            log.error("Antwort konnte nicht gesendet werden: %s", exc)
            break


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
