"""Discord-Bot: setzt das Server-Icon je nach Tageszeit und Jahreszeit.

Start:
    python bot.py            # Dauerbetrieb (prueft regelmaessig)
    python bot.py --once     # einmalig setzen und beenden (z. B. fuer cron)
    python bot.py --check    # nur pruefen (Login, Rechte, Bilder), nichts aendern
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from dotenv import load_dotenv

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
CHECK_INTERVAL_MINUTES = float(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
STATUS_INTERVAL_SECONDS = float(os.getenv("STATUS_INTERVAL_SECONDS", "10"))

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
client = discord.Client(
    intents=intents,
    status=discord.Status.idle,
    activity=discord.CustomActivity(name=WEISHEITEN[0]),
)

# Merkt sich das zuletzt gesetzte Bild, damit nicht unnoetig editiert wird
# (Discord limitiert Server-Aenderungen).
_current_filename: str | None = None
_weisheit_index: int = 0


def invite_url() -> str:
    """OAuth2-Einladungslink mit der noetigen Berechtigung (Server verwalten)."""
    return (
        f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
        "&permissions=32&scope=bot"
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
        await guild.edit(
            icon=path.read_bytes(),
            reason="Automatische Tageszeit-/Jahreszeit-Anpassung",
        )
    except discord.HTTPException as exc:
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


@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def icon_loop() -> None:
    await update_icon()


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
    except discord.HTTPException as exc:
        log.error("Status-Update fehlgeschlagen: %s", exc)


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
    else:
        await update_icon(force=True)
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
