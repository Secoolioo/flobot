"""Discord-Bot: setzt das Server-Icon je nach Tageszeit und Jahreszeit.

Start:
    python bot.py            # Dauerbetrieb (prueft regelmaessig)
    python bot.py --once     # einmalig setzen und beenden (z. B. fuer cron)
    python bot.py --check    # nur pruefen (Login, Rechte, Bilder), nichts aendern
"""

import asyncio
import io
import logging
import os
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from dotenv import load_dotenv

import admin
import ai
import bayern
import casino
import cmdnorm
import economy
import fun
import food
import games
import handel
import luxus
import media
import moderation
import music
import render
import schedule_logic
import voicegags
import words

# WICHTIG: Der Bot laeuft als 'python bot.py' - dieses Modul heisst dann
# '__main__'. Die Feature-Module (admin, casino, games, economy) machen aber
# lazy 'import bot' fuer client/protect_message. Ohne den Alias unten wuerde
# das bot.py ein ZWEITES Mal ausfuehren: doppelte Setups im Log, ein zweiter
# (nie eingeloggter) discord.Client, dessen get_channel() immer None liefert,
# und ein Loesch-Schutz, der ins Leere zeigt. Der Alias sorgt dafuer, dass
# 'import bot' IMMER dieses laufende Modul liefert.
if __name__ == "__main__":
    sys.modules.setdefault("bot", sys.modules[__name__])

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

# Schutz aktiver Spiele vorm Auto-Loeschen: Solange ein Spiel laeuft (Blackjack,
# Crash/Keno/Roulette mit 'Nochmal'-Buttons, Casino-Menue, Quiz, Zahlenraten),
# darf seine Nachricht NICHT weggeraeumt werden - erst wenn keine Reaktion mehr
# kommt (View-Timeout / Runde vorbei). Die Spiel-Module melden ihre Nachrichten
# ueber protect_message() an und mit release_message() wieder ab. Nach dem
# Abmelden bleibt die Nachricht noch eine kurze Gnadenfrist sichtbar und wird
# dann geloescht.
PROTECT_RELEASE_GRACE = float(os.getenv("PROTECT_RELEASE_GRACE", "12"))

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
# Bilder: generieren (Pollinations, kostenlos) + Quote-Meme (Pillow). Bild-Lesen
# (Vision) laeuft ueber die KI und braucht daher AI_ENABLED.
MEDIA_ENABLED = media.setup()
# Kalorien-Analyse: Essensfotos im Kalorien-Channel automatisch analysieren.
FOOD_ENABLED = food.setup()
# Bayrisch/Oesterreichisch: Dialekt-Begruessungen + KI-Dialekt-Toggle.
BAYERN_ENABLED = bayern.setup() if AI_ENABLED else False
VOICE_GAGS_ENABLED = voicegags.setup()
# Casino (Blackjack, Crash, Keno, Roulette) - spielt mit den Flo Coins aus economy.
# Faellt aus, wenn economy aus ist (dort liegt der Coin-Topf).
CASINO_ENABLED = casino.setup()
# Moderation (Nachrichten loeschen / Purge). Faellt nie technisch aus - das noetige
# Recht 'Nachrichten verwalten' wird erst beim Befehl pro Nutzer/Bot geprueft.
MOD_ENABLED = moderation.setup()
# Wort-Zaehler ('Flo woerter <wort>'): zaehlt passiv jedes Wort auf dem Server,
# beim ersten Start liest ein Backfill die komplette History ein.
WORDS_ENABLED = words.setup()
# Admin-Befehle (nur OWNER_ID): Coins geben/nehmen/setzen, XP, Ansagen, Shop -
# im Server UND privat per DM. Andere bekommen in DMs keine Antwort.
ADMIN_ENABLED = admin.setup()
# Luxus-Shop ('Flo luxus'): Prestige-Coin-Senke von 15k bis 1 MILLIARDE
# (Level-Karten-Rahmen, Krone, Imperium) + DER THRON (Unikat, eroberbar).
LUXUS_ENABLED = luxus.setup()
# Coin-Handelsbuch ('Flo handel'): dokumentiert JEDE Coin-Bewegung (Casino,
# Spiele, Daily, Shop, Pay, ...) und zeigt sie als Statistik-Karte. Braucht
# economy (dort liegt der Coin-Topf).
HANDEL_ENABLED = handel.setup()

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
     VOICE_GAGS_ENABLED, CASINO_ENABLED, MOD_ENABLED, MEDIA_ENABLED, FOOD_ENABLED,
     WORDS_ENABLED, ADMIN_ENABLED, LUXUS_ENABLED, HANDEL_ENABLED]
)
intents = discord.Intents.none()
intents.guilds = True
if _NEED_MESSAGES or AUTODELETE_CHANNEL_IDS:
    # guild_messages: noetig, um Nachrichten-Events ueberhaupt zu EMPFANGEN
    # (sonst feuert on_message nie). Ist KEIN privilegiertes Intent.
    intents.guild_messages = True
    # dm_messages: Privatnachrichten empfangen (Owner-DM-Steuerung). Der
    # Nachrichten-TEXT ist in DMs immer verfuegbar, das privilegierte
    # message_content-Intent gilt nur fuer Server-Nachrichten.
    intents.dm_messages = True
if _NEED_MESSAGES:
    # message_content: noetig, um den TEXT der Nachricht zu lesen. Privilegiert -
    # muss zusaetzlich im Discord Developer Portal aktiviert sein.
    intents.message_content = True
if MUSIC_ENABLED or VOICE_GAGS_ENABLED or ECONOMY_ENABLED:
    # voice_states: noetig, um zu sehen, wer in welchem Sprachkanal steckt
    # (Musik, Soundboard/Join-Sounds, Voice-XP). Nicht privilegiert.
    intents.voice_states = True


def _split_message(text, limit = 1900):
    """Zerlegt lange KI-Antworten in Discord-taugliche Stuecke (<2000 Zeichen)."""
    text = text.strip() or "..."
    chunks = []
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


_HELP_RE = re.compile(
    r"^(hilfe|help|befehle|commands?|men[uü]|was kannst du)\b", re.IGNORECASE
)


def _is_help(content):
    """True, wenn jemand 'Flo hilfe' / 'Florian befehle' o. Ae. schreibt."""
    return bool(_HELP_RE.match(ai.strip_lead(content)))


# 'Flo moderation' / 'Flo musik' o. Ae. -> direkt die passende Hilfe-Kategorie
# statt einer KI-Antwort. Nur Woerter, die KEIN anderes Modul als Befehl nutzt
# ('spiele' fehlt bewusst - das ist der Musik-Play-Befehl; 'casino' oeffnet den
# Casino-Hub; 'woerter' die Top-Liste).
_HELP_CATEGORY_ALIASES = {
    "moderation": "mod", "mod": "mod",
    "musik": "musik", "music": "musik",
    "bilder": "bilder", "voice": "voice",
    "economy": "economy",
}


def _help_category_key(content):
    """Kategorie-Schluessel, wenn die Nachricht NUR aus einem Kategorie-Wort
    besteht ('Flo moderation?'), sonst None."""
    word = ai.strip_lead(content).lower().strip(" ?!.,")
    return _HELP_CATEGORY_ALIASES.get(word)


# --- Neustart (nur Bot-Besitzer) -----------------------------------------
_RESTART_RE = re.compile(
    r"^(?:restart|reboot|neustart\w*|neu\s*starten?|neue?\s*starten?|starte?\s+neu)\b",
    re.IGNORECASE,
)


def _is_restart(content):
    """True bei 'Flo restart' / 'Flo neustarten' / 'Flo neu starten' usw."""
    return bool(_RESTART_RE.match(ai.strip_lead(content)))


class RestartConfirmView(discord.ui.View):
    """Sicherheitsabfrage vor dem Neustart - nur der Bot-Besitzer darf klicken."""

    def __init__(self, owner_id):
        super().__init__(timeout=30)
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Nur mein Besitzer darf mich neu starten.", ephemeral=True)
        return False

    @discord.ui.button(label="Ja, neu starten", emoji="🔄", style=discord.ButtonStyle.danger)
    async def _yes(self, interaction, _b):
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
        client._spawn(client._restart_bot())

    @discord.ui.button(label="Abbrechen", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def _no(self, interaction, _b):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Abgebrochen",
                description="Kein Neustart – alles bleibt, wie es ist.",
                color=discord.Color.greyple()),
            view=None)
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# --- Interaktives Hilfe-Menue --------------------------------------------
# Hilfe-Inhalte: (Titel, Farbe, [(befehl, kurz-beschreibung), ...]).
# Bewusst KOMPAKT - die Details stecken im gerenderten Bild, nicht im Text.
_HELP_DATA = {
    "musik": ("Musik", 0x1DB954, [
        ("flo spiel <song/link>", "YouTube & Spotify abspielen"),
        ("flo mach mal <song> an", "geht auch locker: leg/hau/pack … auf/raus"),
        ("flo skip · pause · weiter · stop", "Steuerung (oder die Buttons)"),
        ("flo nochmal [n]", "letzten Song nochmal"),
        ("flo queue", "Warteschlange zeigen"),
        ("flo lautstärke 50", "Lautstärke setzen"),
        ("flo join · leave", "Voice rein / raus"),
    ]),
    "spiele": ("Spiele", 0xE67E22, [
        ("flo quiz", "Quizfrage - Erster gewinnt 50 Coins"),
        ("flo zahlenraten", "1-100 erraten, schnell = mehr Coins"),
        ("flo würfel 2d6", "Würfeln (ohne Einsatz)"),
        ("flo mathe 100", "Kopfrechnen: richtig = x2"),
        ("flo reaktion 100", "Reaktionstest: schnell = bis x2,5"),
        ("flo anagramm 100", "Wort entwirren: richtig = x3"),
        ("flo quizduell @wer 100", "Quiz-Duell - Pot an den Schnellsten"),
        ("flo ssp @wer 100", "Schere-Stein-Papier ums Geld"),
        ("flo ssp schere", "SSP gegen Flo (+10 bei Sieg)"),
    ]),
    "economy": ("Level & Coins", 0xF1C40F, [
        ("flo level · top", "Level-Karte & Bestenliste"),
        ("flo daily", "Tagesbonus + Streak"),
        ("flo coins · pay @wer 1k", "Kontostand & überweisen (1k = 1000)"),
        ("flo shop · kaufen 3", "Tages-Titel (2 Uhr neu, Legendary wird ausgerufen)"),
        ("flo inventar · titel <name>", "Titel verwalten & anlegen"),
        ("flo luxus · thron", "Prestige bis 1 MILLIARDE & DER THRON"),
        ("flo handel [@wer]", "Coin-Handelsbuch: alle Transaktionen als Statistik"),
    ]),
    "casino": ("Casino", 0xE91E63, [
        ("flo casino", "Übersicht - alles per Button"),
        ("flo blackjack 100", "17+4: Karte / Stand / Double"),
        ("flo mines 100 [bomben]", "Diamanten sammeln, vor der Bombe raus"),
        ("flo roulette 100 rot", "Kessel dreht, Kugel entscheidet"),
        ("flo crash 100 2.0", "Rakete - rechtzeitig aussteigen"),
        ("flo slots 100 · keno 100 3 7", "Automat & Zahlen-Lotto"),
        ("flo rad 100 · rubbellos 100", "Glücksrad & Rubbellos"),
        ("flo hilo 100", "Höher/Tiefer - Serie aufbauen, Cashout"),
        ("flo tower 100", "Turm hochklettern, Falle meiden"),
        ("flo sieben 100 unter", "2 Würfel: unter / über / genau 7"),
        ("flo baccarat 100 bank", "Spieler, Bank oder Tie (x8)"),
        ("flo don 100", "Doppelt oder nichts - wie weit gehst du?"),
        ("flo duell @wer 100 · stats", "Münz-Duell & deine Bilanz"),
    ]),
    "wörter": ("Wörter", 0x5794F2, [
        ("flo wörter pizza", "wie oft schon gesagt + Top-Sager"),
        ("flo wörter", "Top 15 des Servers als Bild"),
    ]),
    "chaos": ("Chaos", 0x9B59B6, [
        ("flo roast @wer · hype @wer", "austeilen oder abfeiern"),
        ("flo rate @wer · rizz @wer", "0-100 Bewertung mit Spruch"),
        ("flo spruch · horoskop", "Weisheit & Tages-Horoskop"),
    ]),
    "bilder": ("Bilder", 0x9B7BE0, [
        ("flo male <was>", "Bild generieren (gratis)"),
        ("flo quote [@wer] <text>", "Quote-Meme - für dich, @wen oder als Reply"),
        ("flo kalorien + Bild", "Essen analysieren"),
        ("Bild anhängen + Frage", "Flo schaut sich Bilder an"),
    ]),
    "voice": ("Voice", 0x1ABC9C, [
        ("flo soundboard", "Sound-Buttons - drücken & lachen"),
        ("flo sound <name>", "einzelnen Sound abspielen"),
        ("flo sprich <text>", "Text-to-Speech im Voice"),
    ]),
    "mod": ("Moderation", 0xED4245, [
        ("flo lösch 20 · nuke", "aufräumen (Gepinntes bleibt)"),
        ("flo warn @wer <grund>", "verwarnen (3x = Auto-Timeout)"),
        ("flo timeout @wer 10m", "stummschalten"),
        ("flo kick · ban @wer", "rauswerfen / sperren"),
        ("flo unwarn · untimeout · unban", "alles wieder zurücknehmen"),
    ]),
    "ki": ("KI", 0x5865F2, [
        ("flo <frage>", "einfach fragen - mit Kontext & Bildern"),
        ("flo bayrisch an / aus", "Dialekt-Modus"),
    ]),
}
# Kurz-Hinweise fuer die Uebersichts-Karte.
_HELP_HINTS = {
    "musik": "spiel · skip · queue", "spiele": "quiz · mathe · duelle",
    "economy": "level · daily · shop · handel", "casino": "13 Spiele · stats",
    "wörter": "wörter <wort>",
    "chaos": "roast · rate · horoskop",
    "bilder": "male · quote · kalorien", "voice": "sounds · sprich",
    "mod": "lösch · warn · ban", "ki": "einfach fragen",
}


def _rgb(farbe):
    return ((farbe >> 16) & 0xFF, (farbe >> 8) & 0xFF, farbe & 0xFF)


class _HelpNavButton(discord.ui.Button):
    """Ein Navigations-Button im Hilfe-Menue. key=None => Übersicht."""

    def __init__(self, key, emoji, label, *,
                 style):
        super().__init__(label=label, emoji=emoji, style=style)
        self.key = key

    async def callback(self, interaction):
        view = self.view  # type: ignore[assignment]
        await view.show(interaction, self.key)


class HelpView(discord.ui.View):
    """Interaktives Hilfe-Menue: Kategorie-Buttons wechseln das Embed in-place."""

    def __init__(self):
        super().__init__(timeout=180)
        self.message = None
        self.active = None
        self.add_item(_HelpNavButton(None, "🏠", "Übersicht",
                                     style=discord.ButtonStyle.primary))
        for key, emoji, label in client._help_categories():
            self.add_item(_HelpNavButton(key, emoji, label,
                                         style=discord.ButtonStyle.secondary))
        self._sync()

    def _sync(self):
        """Hebt den aktiven Bereich hervor (grün + deaktiviert)."""
        for child in self.children:
            if isinstance(child, _HelpNavButton):
                here = (child.key == self.active)
                child.disabled = here
                child.style = (discord.ButtonStyle.success if here else
                               (discord.ButtonStyle.primary if child.key is None
                                else discord.ButtonStyle.secondary))

    async def show(self, interaction, key):
        self.active = key
        self._sync()
        emb, file = await client._help_payload(key)
        await interaction.response.edit_message(
            embed=emb, view=self, attachments=[file] if file else [])

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def invite_url():
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


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _first_image_url(message):
    """URL des ersten Bildes - im Anhang der Nachricht ODER in der Nachricht,
    auf die geantwortet wurde. Sonst None (dann normale Text-KI)."""
    def _scan(msg):
        for att in getattr(msg, "attachments", None) or []:
            ct = (att.content_type or "").lower()
            if ct.startswith("image/") or att.filename.lower().endswith(_IMAGE_EXTS):
                return att.url
        return None
    url = _scan(message)
    if url:
        return url
    ref = message.reference.resolved if message.reference is not None else None
    if isinstance(ref, discord.Message):
        return _scan(ref)
    return None


# Taeglich um 02:00 (Europe/Berlin) wuerfelt der Flo Shop seine Titelauswahl neu.
# tasks.loop(time=...) feuert exakt zur angegebenen Ortszeit; bei mehreren
# Reconnects schuetzt is_running() vor doppeltem Start.
SHOP_REFRESH_TIME = dtime(hour=2, minute=0, tzinfo=TIMEZONE)


class FloBot(discord.Client):
    """Der Bot als Klasse: Event-Handler, Hintergrund-Loops und Hilfslogik als
    Methoden, der veraenderliche Zustand als Instanzattribute."""

    def __init__(self, **options):
        super().__init__(**options)
        # Merkt sich das zuletzt gesetzte Bild, damit nicht unnoetig editiert wird
        # (Discord limitiert Server-Aenderungen).
        self._current_filename = None
        self._weisheit_index = 0
        # Zwischenspeicher der gerenderten Hilfe-Karten (PNG je Kategorie).
        self._help_png_cache = {}
        # Schutz aktiver Spiele vorm Auto-Loeschen (siehe PROTECT_RELEASE_GRACE oben).
        self._protected_msg_ids = set()   # IDs aktiver Spiel-Nachrichten (nicht loeschen)
        self._releasing_ids = set()       # laufen gerade durch ihre Gnadenfrist
        # Laufende Hintergrund-Tasks festhalten, damit der Garbage Collector sie nicht
        # vorzeitig einsammelt (asyncio.create_task gibt nur eine schwache Referenz).
        self._bg_tasks = set()
        # Sammel-Loeschung: Ein Timer + einzelner DELETE-Call PRO Nachricht hat bei
        # Chat-Bursts das Rate-Limit gerissen (60 s spaeter feuerten Dutzende DELETEs
        # gleichzeitig -> 429). Stattdessen sammeln wir faellige Nachrichten je Channel
        # und loeschen sie gebuendelt (Bulk-Delete: bis 100 Nachrichten = 1 API-Call).
        self._pending_deletes = {}

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _reply_chunks(self, message, text):
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
        self, message, payload
    ):
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
        await self._reply_chunks(message, payload)

    async def _restart_bot(self):
        """Startet den GANZEN Prozess neu (re-exec) - funktioniert lokal und unter
        systemd, unabhaengig von einem Supervisor. Vorher Voice/Gateway sauber
        schliessen, damit ffmpeg-Subprozesse nicht verwaisen."""
        await asyncio.sleep(0.4)  # der Interaktions-Antwort Zeit zum Rausgehen geben
        # Wort-Zaehler speichert debounced - vor dem Neustart einmal hart sichern.
        if WORDS_ENABLED:
            try:
                await words.flush_now()
            except Exception:
                log.exception("Wort-Zaehler-Flush vor Neustart fehlgeschlagen")
        try:
            await self.close()
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

    def _help_categories(self):
        """Liefert (key, emoji, label) je AKTIVEM Bereich - Reihenfolge = Button-Reihenfolge."""
        cats = [
            ("musik", "🎵", "Musik", MUSIC_ENABLED),
            ("spiele", "🎮", "Spiele", GAMES_ENABLED),
            ("economy", "📈", "Level & Coins", ECONOMY_ENABLED),
            ("casino", "🎰", "Casino", CASINO_ENABLED),
            ("wörter", "📊", "Wörter", WORDS_ENABLED),
            ("chaos", "😈", "Chaos", FUN_ENABLED),
            ("bilder", "🎨", "Bilder", MEDIA_ENABLED),
            ("voice", "🔊", "Voice", VOICE_GAGS_ENABLED),
            ("mod", "🛡️", "Moderation", MOD_ENABLED),
            ("ki", "💬", "KI", AI_ENABLED),
        ]
        return [(k, e, l) for k, e, l, on in cats if on]

    async def _help_file(self, key):
        """Hilfe-Karte als Bild (einmal gerendert, dann aus dem Cache)."""
        png = self._help_png_cache.get(key)
        if png is None:
            try:
                if key == "_overview":
                    entries = [(label, _HELP_HINTS.get(k, ""), _rgb(_HELP_DATA[k][1]))
                               for k, _e, label in self._help_categories() if k in _HELP_DATA]
                    buf = await asyncio.to_thread(
                        render.help_card, f"{ai.bot_name().upper()} – HILFE",
                        (88, 101, 242), entries, subtitle="Kategorie unten antippen")
                else:
                    titel, farbe, entries = _HELP_DATA[key]
                    buf = await asyncio.to_thread(render.help_card, titel,
                                                  _rgb(farbe), entries)
                png = buf.getvalue()
                self._help_png_cache[key] = png
            except Exception:  # noqa: BLE001 - dann eben Text-Fallback
                log.exception("Hilfe-Karte fehlgeschlagen (%s)", key)
                return None
        fname = "help_uebersicht.png" if key == "_overview" else f"help_{key}.png"
        return discord.File(io.BytesIO(png), filename=fname)

    async def _help_payload(self, key):
        """Embed + Karten-Bild fuer eine Kategorie (None = Uebersicht)."""
        if key is None or key not in _HELP_DATA:
            emb = self._help_overview_embed()
            file = await self._help_file("_overview")
            if file is not None:
                emb.set_image(url="attachment://help_uebersicht.png")
            return emb, file
        titel, farbe, entries = _HELP_DATA[key]
        emb = discord.Embed(title=titel, color=farbe)
        file = await self._help_file(key)
        if file is not None:
            emb.set_image(url=f"attachment://help_{key}.png")
        else:
            emb.description = "\n".join(f"`{c}` – {d}" for c, d in entries)
        return emb, file

    def _help_overview_embed(self):
        """Startansicht des Hilfe-Menues - kompakt, die Karte zeigt die Details."""
        name = ai.bot_name()
        emb = discord.Embed(
            title=f"🤖 {name} – Hilfe",
            description="Kategorie unten antippen. 👇",
            color=discord.Color.blurple(),
        )
        if self.user is not None:
            try:
                emb.set_thumbnail(url=self.user.display_avatar.url)
            except Exception:  # noqa: BLE001 - Avatar ist nur Deko
                pass
        emb.set_footer(text=f"{name} <frage> geht immer · Titel im Shop ändern die Anrede")
        return emb

    async def update_icon(self, *, force = False):
        """Setzt das Server-Icon, falls ein anderes Bild faellig ist."""
        now = datetime.now(TIMEZONE)
        filename = schedule_logic.get_image_filename(now)

        if filename == self._current_filename and not force:
            return False

        path = IMAGE_DIR / filename
        if not path.exists():
            log.error("Bilddatei fehlt: %s", path)
            return False

        guild = self.get_guild(GUILD_ID)
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

        self._current_filename = filename
        log.info(
            "Server-Icon gesetzt: %s  (%s)",
            filename,
            now.strftime("%Y-%m-%d %H:%M %Z"),
        )
        return True

    async def run_check(self):
        """Diagnose: Login, Server, Rechte und Bilder pruefen - ohne Aenderung."""
        now = datetime.now(TIMEZONE)
        target = schedule_logic.get_image_filename(now)
        log.info("Lokale Zeit: %s", now.strftime("%Y-%m-%d %H:%M %Z"))
        log.info("Aktuell faelliges Bild: %s", target)

        guild = self.get_guild(GUILD_ID)
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
    async def icon_loop(self):
        # Prueft regelmaessig (Standard: jede Minute), ob ein anderes Bild faellig
        # ist. Discord wird nur angesprochen, wenn sich das Bild wirklich aendert.
        # try/except, damit eine einzelne Fehlrunde die Schleife NICHT stoppt.
        try:
            await self.update_icon()
        except Exception:
            log.exception("Fehler im Icon-Check - Loop laeuft weiter")

    @tasks.loop(seconds=STATUS_INTERVAL_SECONDS)
    async def status_loop(self):
        """Wechselt alle paar Sekunden den Status-Text (Bot bleibt 'idle')."""
        weisheit = WEISHEITEN[self._weisheit_index % len(WEISHEITEN)]
        self._weisheit_index += 1
        try:
            await self.change_presence(
                status=discord.Status.idle,
                activity=discord.CustomActivity(name=weisheit),
            )
            log.debug("Status (idle): %s", weisheit)
        except Exception as exc:
            log.error("Status-Update fehlgeschlagen: %s", exc)

    @tasks.loop(seconds=economy.VOICE_TICK_SECONDS)
    async def voice_xp_loop(self):
        """Gibt regelmaessig XP an aktive Mitglieder in Sprachkanaelen (Voice-Zeit)."""
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        try:
            await economy.tick_voice(guild)
        except Exception:
            log.exception("Voice-XP-Loop Fehler - laeuft weiter")

    @tasks.loop(seconds=music.VOICE_HEAL_SECONDS)
    async def voice_heal_loop(self):
        """Voice-Watchdog: haelt den Musik-Bot in seinem Sprachkanal und repariert
        Desyncs/Zombie-Verbindungen selbst (siehe music.heal_voice)."""
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        try:
            await music.heal_voice(guild)
        except Exception:
            log.exception("Voice-Heal-Loop Fehler - laeuft weiter")

    @tasks.loop(seconds=EVENT_INTERVAL_SECONDS)
    async def event_loop(self):
        """Zieht im Takt mit kleiner Wahrscheinlichkeit ein Zufalls-Event (Schnell-tippen)."""
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        try:
            await games.maybe_event(guild)
        except Exception:
            log.exception("Event-Loop Fehler - laeuft weiter")

    @tasks.loop(time=SHOP_REFRESH_TIME)
    async def shop_refresh_loop(self):
        """Wuerfelt jede Nacht um 2 Uhr die Tagesauswahl des Flo Shops neu (random,
        seltenheits-gewichtet). Ist ein LEGENDAERER Titel dabei, wird das im
        Level-Up-Channel ausgerufen. Faengt alle Fehler ab."""
        try:
            st = await economy.refresh_shop_async(force=True)
            log.info("Flo Shop (2 Uhr) aktualisiert: %d Titel fuer %s.",
                     len(st.get("items", [])), st.get("date", "?"))
            legendaere = [i for i in st.get("items", [])
                          if i.get("rarity") == "legendary"]
            if legendaere:
                self._spawn(self._announce_legendary(legendaere))
        except Exception:
            log.exception("Shop-Refresh (2 Uhr) fehlgeschlagen - Loop laeuft weiter")

    async def _announce_legendary(self, items):
        """Ruft legendaere Shop-Titel oeffentlich aus (nur heute im Angebot!)."""
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            return
        channel = guild.get_channel(economy.LEVELUP_CHANNEL_ID)
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = guild.system_channel
        if channel is None:
            return
        zeilen = "\n".join(f"**{i.get('label', i.get('text', '?'))}** – "
                           f"{i.get('price', '?')} {economy.COIN} (Nr. {i.get('n', '?')})"
                           for i in items)
        emb = discord.Embed(
            title="🟡 LEGENDÄRER Titel im Shop!",
            description=f"{zeilen}\n\nNur **heute** – `{ai.bot_name()} shop` 🏃",
            color=discord.Color.gold())
        try:
            await channel.send(embed=emb)
        except discord.HTTPException:
            log.warning("Legendary-Ansage konnte nicht gesendet werden")

    def _keep_bot_msg(self, m):
        """True = diese Bot-Nachricht ist vom Auto-Loeschen ausgenommen: Level-Up-
        Ansagen (Erfolge sollen sichtbar bleiben) und das aktuelle Musik-Panel
        'Jetzt laeuft' (die Steuer-Buttons muessen den ganzen Song erreichbar bleiben).
        Alte Panels raeumt der Musik-Player beim Songwechsel selbst weg."""
        if self.user is None or m.author.id != self.user.id or not m.embeds:
            return False
        title = m.embeds[0].title
        return title in (economy.LEVELUP_EMBED_TITLE, music.NOWPLAYING_EMBED_TITLE)

    def _sweepable(self, m):
        """True = diese Nachricht im Auto-Loesch-Channel darf weg. Level-Up-Ansagen,
        das Musik-Panel, angepinnte Nachrichten und aktive Spiele bleiben (wie beim
        Einzel-Auto-Loeschen)."""
        if m.pinned:
            return False
        if m.id in self._protected_msg_ids:
            return False    # laeuft noch (oder in der Gnadenfrist) -> nicht wegraeumen
        if self._keep_bot_msg(m):
            return False
        return True

    @tasks.loop(seconds=AUTODELETE_SWEEP_SECONDS)
    async def autodelete_sweep_loop(self):
        """Sicherheitsnetz fuers Auto-Loeschen: raeumt in den konfigurierten Channels
        ALLES weg, was aelter als AUTODELETE_SECONDS ist - auch Altlasten von vor dem
        Start oder Nachrichten, die einen Neustart ueberlebt haben. So bleibt der
        Channel wirklich leer, nicht nur 'ab jetzt'. Erste Runde laeuft sofort beim
        Start (raeumt den Backlog ab)."""
        if not AUTODELETE_CHANNEL_IDS:
            return
        guild = self.get_guild(GUILD_ID)
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
                await channel.purge(limit=None, check=self._sweepable, before=cutoff)
            except discord.HTTPException as exc:
                log.warning("Auto-Loesch-Sweep in #%s fehlgeschlagen: %s",
                            getattr(channel, "name", cid), exc)
            except Exception:
                log.exception("Auto-Loesch-Sweep Fehler - laeuft weiter")

    def _queue_delete(self, message, delay):
        """Merkt eine Nachricht fuers gebuendelte Auto-Loeschen vor."""
        self._pending_deletes.setdefault(message.channel.id, []).append(
            (time.monotonic() + delay, message))

    @tasks.loop(seconds=5.0)
    async def autodelete_batch_loop(self):
        """Loescht faellige Auto-Loesch-Nachrichten im Buendel. Geschuetzte
        Nachrichten (aktive Spiele) fallen raus - release_message() raeumt die
        spaeter selbst auf. Schon geloeschte Nachrichten ignoriert der Bulk-Call."""
        now = time.monotonic()
        for cid in list(self._pending_deletes):
            entries = self._pending_deletes.get(cid) or []
            due = [m for ts, m in entries
                   if ts <= now and m.id not in self._protected_msg_ids]
            rest = [(ts, m) for ts, m in entries if ts > now]
            if rest:
                self._pending_deletes[cid] = rest
            else:
                self._pending_deletes.pop(cid, None)
            if not due:
                continue
            channel = due[0].channel
            for i in range(0, len(due), 100):
                chunk = due[i:i + 100]
                try:
                    if len(chunk) == 1:
                        await chunk[0].delete()
                    else:
                        await channel.delete_messages(chunk)
                except discord.NotFound:
                    pass  # schon weg (Sweep/manuell)
                except discord.Forbidden:
                    log.warning(
                        "Auto-Loeschen: mir fehlt 'Nachrichten verwalten' in #%s.",
                        getattr(channel, "name", cid))
                except discord.HTTPException as exc:
                    log.warning("Auto-Loeschen (Buendel) fehlgeschlagen: %s", exc)
                except Exception as exc:  # noqa: BLE001
                    # Netzwerkfehler (z. B. aiohttp/OSError) sind KEINE HTTPException
                    # und wuerden sonst den ganzen tasks.loop dauerhaft killen.
                    log.warning("Auto-Loeschen (Buendel) unerwartet fehlgeschlagen: %s", exc)

    def protect_message(self, message):
        """Meldet eine aktive Spiel-Nachricht beim Auto-Loesch-Schutz an. Nur in den
        Auto-Loesch-Channels noetig (woanders wird ohnehin nichts geloescht). Von den
        Spiel-Modulen (casino, games) aufgerufen, sobald eine Runde startet."""
        if message is None or message.channel.id not in AUTODELETE_CHANNEL_IDS:
            return
        self._protected_msg_ids.add(message.id)

    def release_message(self, message, *, delay = None):
        """Spiel vorbei / keine Reaktion mehr -> Schutz nach kurzer Gnadenfrist
        aufheben und die Nachricht dann wegraeumen. Bis dahin bleibt sie geschuetzt
        (kein Sweep, kein vorzeitiges Loeschen). Mehrfachaufruf ist ungefaehrlich."""
        if message is None or message.id not in self._protected_msg_ids:
            return
        if message.id in self._releasing_ids:
            return  # laeuft schon durch die Gnadenfrist
        self._releasing_ids.add(message.id)
        grace = PROTECT_RELEASE_GRACE if delay is None else delay
        self._spawn(self._release_after(message, grace))

    async def _release_after(self, message, delay):
        """Wartet die Gnadenfrist ab, hebt dann den Schutz auf und loescht die
        Nachricht (best effort)."""
        try:
            await asyncio.sleep(max(0.0, delay))
        finally:
            self._protected_msg_ids.discard(message.id)
            self._releasing_ids.discard(message.id)
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning(
                "Auto-Loeschen (Spielende): mir fehlt 'Nachrichten verwalten' in #%s.",
                getattr(message.channel, "name", message.channel.id),
            )
        except discord.HTTPException as exc:
            log.warning("Auto-Loeschen (Spielende) fehlgeschlagen: %s", exc)

    async def _forward_dm_to_owner(self, message):
        """Leitet eine Fremd-DM still an den Besitzer weiter (Flo antwortet dem
        Absender nicht). So sieht der Besitzer Antworten auf seine 'flo dm's."""
        content = (message.content or "").strip()
        if not content and not message.attachments:
            return
        try:
            owner = self.get_user(OWNER_ID) or await self.fetch_user(OWNER_ID)
            text = (f"📥 **DM von {message.author.display_name}** "
                    f"(`{message.author.id}`):\n{content[:1500]}")
            if message.attachments:
                text += f"\n📎 {len(message.attachments)} Anhang/Anhänge"
            text += f"\n-# Antworten: `flo dm {message.author.id} <text>`"
            await owner.send(text)
        except Exception:  # noqa: BLE001 - Weiterleitung ist best effort
            log.exception("DM-Weiterleitung an den Besitzer fehlgeschlagen")

    async def _send_restart_prompt(self, message):
        """Schickt die Neustart-Sicherheitsabfrage (Server ODER Owner-DM)."""
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

    async def _handle_owner_dm(self, message):
        """Privatnachrichten des BESITZERS: Admin-Befehle, Restart, Hilfe und
        normaler KI-Chat - ganz ohne 'Flo' davor. Alle anderen DMs beantwortet
        der Bot bewusst nicht (der Aufrufer filtert schon auf OWNER_ID)."""
        content = message.content or ""
        if not content.strip() and not message.attachments:
            return

        if _is_restart(content):
            await self._send_restart_prompt(message)
            return
        if _is_help(content):
            view = HelpView()
            emb, file = await self._help_payload(None)
            try:
                view.message = await message.reply(
                    embed=emb, file=file, view=view, mention_author=False)
            except (discord.HTTPException, TypeError):
                log.exception("Hilfe (DM) konnte nicht gesendet werden")
            return

        # Tippfehler-Toleranz wie im Server (nur fuer den Befehls-Durchlauf).
        _orig_content = message.content
        try:
            _norm = cmdnorm.normalize(ai.strip_lead(content))
        except Exception:  # noqa: BLE001
            _norm = None
        if _norm is not None:
            message.content = f"{ai.bot_name()} {_norm}"

        antwort = None
        if ADMIN_ENABLED:
            try:
                antwort = await admin.handle(message)
            except Exception:
                log.exception("Admin-Befehl (DM) fehlgeschlagen")
                antwort = "Da ist gerade etwas schiefgelaufen."
        message.content = _orig_content

        if antwort is not None:
            log.info("Admin-DM von %s: %s", message.author.display_name, content[:80])
            await self._send_reply(message, antwort)
            return

        # Kein Admin-Befehl -> normaler KI-Chat (auch mit Bild).
        if not AI_ENABLED:
            return
        ai.note_message(message.channel.id, message.author.display_name, content)
        title = economy.get_title(message.author.id) if ECONOMY_ENABLED else ""
        tone = economy.get_tone(message.author.id) if ECONOMY_ENABLED else ""
        if LUXUS_ENABLED:
            tone = f"{tone} {luxus.get_tone_extra(message.author.id)}".strip()
        image_url = _first_image_url(message)
        async with message.channel.typing():
            try:
                if image_url:
                    antwort = await ai.see_image(
                        content, image_url, author=message.author.display_name,
                        title=title, tone=tone, channel_id=message.channel.id)
                else:
                    antwort = await ai.ask_flo(
                        content, author=message.author.display_name, title=title,
                        tone=tone, channel_id=message.channel.id)
            except Exception:
                log.exception("KI-Antwort (DM) fehlgeschlagen")
                antwort = "Ups, da ist gerade etwas schiefgelaufen. Versuch es gleich nochmal."
        ai.note_message(message.channel.id, ai.bot_name(), antwort, is_bot=True)
        await self._reply_chunks(message, antwort)

    async def on_message(self, message):
        """Zentrale Nachrichten-Verarbeitung: Auto-Loeschen, passive Spass-Hooks
        (XP, Spiele, Reactions) und - wenn 'Flo' angesprochen wird - Befehle + KI."""
        # Auto-Loeschen: in konfigurierten Channels ALLE Nachrichten nach kurzer Zeit
        # entfernen. Bewusst GANZ oben (vor dem Bot-Check), damit auch die eigenen
        # Antworten des Bots dort wieder verschwinden. AUSNAHME: Level-Up-Ansagen des
        # Bots bleiben stehen (Erfolge sollen sichtbar bleiben).
        if message.channel.id in AUTODELETE_CHANNEL_IDS:
            # Level-Up-Ansagen UND das aktuelle Musik-Panel bleiben stehen, alles
            # andere wird nach kurzer Zeit geloescht (gebuendelt, siehe
            # autodelete_batch_loop - schont das Rate-Limit).
            if not self._keep_bot_msg(message):
                self._queue_delete(message, AUTODELETE_SECONDS)

        if message.author.bot:
            return
        if message.guild is None:
            # Privatnachrichten: NUR der Besitzer bekommt Antworten (Admin-Befehle
            # + KI-Chat). Alle anderen duerfen schreiben - Flo bleibt ihnen
            # gegenueber stumm, LEITET die Nachricht aber an den Besitzer weiter
            # (so werden Antworten auf 'flo dm' sichtbar).
            if OWNER_ID and message.author.id == OWNER_ID:
                await self._handle_owner_dm(message)
            elif OWNER_ID:
                self._spawn(self._forward_dm_to_owner(message))
            return

        content = message.content or ""

        # Kurzzeit-Gedaechtnis: Flo merkt sich den laufenden Chat (auch ohne direkt
        # angesprochen zu sein), damit er dem Gespraech folgen kann, wenn man ihn fragt.
        if AI_ENABLED and content.strip():
            ai.note_message(message.channel.id, message.author.display_name, content)

        # --- Passive Hooks: sehen JEDE Nachricht (vor dem Flo-Trigger) ---
        # XP/Coins fuers Schreiben (laeuft nebenher, blockiert nicht).
        if ECONOMY_ENABLED:
            self._spawn(economy.on_message(message))
        # Wort-Zaehler: synchron und billig (reine dict-Arbeit, Speichern debounced).
        if WORDS_ENABLED:
            try:
                words.note_message(message)
            except Exception:
                log.exception("Wort-Zaehler-Hook fehlgeschlagen")
        # Kalorien-Channel: Essensfoto -> automatische Naehrwert-Analyse (nebenher).
        if FOOD_ENABLED:
            self._spawn(food.on_message_passive(message))
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
            self._spawn(fun.on_message_passive(message))

        # --- Ab hier nur, wenn Flo angesprochen wird ---
        angesprochen = bool(_TRIGGER_RE.search(content))
        if not angesprochen and self.user in message.mentions:
            angesprochen = True
        # Antwort auf eine Flo-Nachricht zaehlt auch als angesprochen (natuerliches
        # Weiterreden, ohne 'Flo' tippen zu muessen).
        if not angesprochen and message.reference is not None:
            ref = message.reference.resolved
            if (isinstance(ref, discord.Message) and self.user is not None
                    and ref.author.id == self.user.id):
                angesprochen = True
        if not angesprochen:
            return

        # Sendepause (nur der Besitzer schaltet sie per 'Flo sendepause'): ist sie
        # aktiv, blockiert Flo AB HIER alles Interaktive - Befehle UND KI - fuer
        # jeden ausser dem Besitzer. Die passiven Hooks oben (XP, Level, Coins,
        # Wortzaehler, Reactions) sind bewusst schon gelaufen und bleiben erhalten.
        if ADMIN_ENABLED and admin.is_locked() and message.author.id != OWNER_ID:
            return

        # 'Flo restart' / 'Flo neustarten' -> kompletter Neustart, NUR fuer den Besitzer.
        if _is_restart(content):
            if message.author.id != OWNER_ID:
                await self._send_reply(message, discord.Embed(
                    description="Nur mein Besitzer darf mich neu starten. 😉",
                    color=discord.Color.red()))
                return
            await self._send_restart_prompt(message)
            return

        # 'Flo hilfe' / 'Flo befehle' -> interaktives Menue mit Kategorie-Buttons.
        if _is_help(content):
            view = HelpView()
            emb, file = await self._help_payload(None)
            try:
                view.message = await message.reply(
                    embed=emb, file=file, view=view, mention_author=False)
            except (discord.HTTPException, TypeError):
                log.exception("Hilfe konnte nicht gesendet werden")
            return

        # 'Flo moderation' / 'Flo musik' -> direkt die passende Hilfe-Kategorie.
        _cat = _help_category_key(content)
        if _cat is not None:
            view = HelpView()
            view.active = _cat
            view._sync()
            emb, file = await self._help_payload(_cat)
            try:
                view.message = await message.reply(
                    embed=emb, file=file, view=view, mention_author=False)
            except (discord.HTTPException, TypeError):
                log.exception("Kategorie-Hilfe konnte nicht gesendet werden")
            return

        # Befehls-Normalisierung: erstes Wort auf Tippfehler/Dialekt korrigieren, damit
        # ALLE Befehle tolerant reagieren. message.content wird nur fuer den Befehls-
        # durchlauf angepasst und danach wiederhergestellt (die KI bekommt das Original).
        _orig_content = message.content
        try:
            _norm = cmdnorm.normalize(ai.strip_lead(content))
        except Exception:  # noqa: BLE001
            _norm = None
        if _norm is not None:
            message.content = f"{ai.bot_name()} {_norm}"

        # Befehls-Handler der Reihe nach durchgehen. Jeder gibt entweder eine Antwort
        # (Text ODER Embed = Befehl erkannt, fertig) oder None (= naechster ist dran).
        # BEWUSST OHNE channel.typing(): das war ein zusaetzlicher API-Roundtrip VOR
        # jedem Befehl (~100-200 ms Extra-Latenz) - die Spiele antworten schnell
        # genug; nur der (langsame) KI-Fallback tippt weiterhin.
        antwort = None
        for enabled, handler in (
            (BAYERN_ENABLED, bayern.handle),
            (MOD_ENABLED, moderation.handle),
            (ADMIN_ENABLED, admin.handle),
            (MUSIC_ENABLED, music.handle),
            (VOICE_GAGS_ENABLED, voicegags.handle),
            (GAMES_ENABLED, games.handle),
            (CASINO_ENABLED, casino.handle),
            (LUXUS_ENABLED, luxus.handle),
            (HANDEL_ENABLED, handel.handle),
            (WORDS_ENABLED, words.handle),
            (ECONOMY_ENABLED, economy.handle),
            (FOOD_ENABLED, food.handle),
            (MEDIA_ENABLED, media.handle),
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

        # Originaltext wiederherstellen: die KI (und alles Weitere) sieht wieder
        # exakt, was der Nutzer geschrieben hat - egal ob korrigiert wurde.
        message.content = _orig_content

        if antwort is not None:
            if (antwort is moderation.HANDLED or antwort is music.HANDLED
                    or antwort is casino.HANDLED or antwort is games.HANDLED
                    or antwort is economy.HANDLED or antwort is media.HANDLED
                    or antwort is food.HANDLED or antwort is words.HANDLED
                    or antwort is luxus.HANDLED or antwort is voicegags.HANDLED):
                return  # Modul hat selbst geantwortet (Musik / Casino / Spiele / Economy / Bild ...).
            if isinstance(antwort, discord.File):
                log.info("Befehl von %s: [Bild] %s", message.author.display_name, antwort.filename)
            elif isinstance(antwort, discord.Embed):
                log.info("Befehl von %s: [Embed] %s", message.author.display_name, antwort.title or "")
            else:
                log.info("Befehl von %s: %s", message.author.display_name, antwort[:80])
            await self._send_reply(message, antwort)
            return

        # --- KI-Fallback: kein Befehl erkannt -> Flo antwortet wie eine KI ---
        if not AI_ENABLED:
            return
        # Gekaufter Shop-Titel -> Flo spricht den Nutzer damit an. Je seltener der
        # getragene Titel, desto entspannter/ehrfuerchtiger redet Flo (tone).
        title = economy.get_title(message.author.id) if ECONOMY_ENABLED else ""
        tone = economy.get_tone(message.author.id) if ECONOMY_ENABLED else ""
        # Luxus-Status (Imperator/Thron) schlaegt sich im Tonfall nieder.
        if LUXUS_ENABLED:
            tone = f"{tone} {luxus.get_tone_extra(message.author.id)}".strip()
        # Bild dabei (Anhang oder in der beantworteten Nachricht)? -> Flo schaut es sich
        # an (Vision), statt nur den Text zu lesen.
        image_url = _first_image_url(message)
        # Dialekt-Modus in diesem Server aktiv? -> Flo antwortet boarisch.
        bavarian = BAYERN_ENABLED and bayern.is_on(message.guild.id)
        log.info("KI-Frage von %s%s%s: %s", message.author.display_name,
                 " [+Bild]" if image_url else "", " [boarisch]" if bavarian else "",
                 content[:150])
        async with message.channel.typing():
            try:
                if image_url:
                    antwort = await ai.see_image(
                        content, image_url, author=message.author.display_name,
                        title=title, tone=tone, channel_id=message.channel.id,
                        bavarian=bavarian,
                    )
                else:
                    antwort = await ai.ask_flo(
                        content, author=message.author.display_name, title=title, tone=tone,
                        channel_id=message.channel.id, bavarian=bavarian,
                    )
            except Exception:
                log.exception("KI-Antwort fehlgeschlagen")
                antwort = "Ups, da ist gerade etwas schiefgelaufen. Versuch es gleich nochmal."
        log.info("KI-Antwort an %s (%d Zeichen)", message.author.display_name, len(antwort))
        # Flos eigene Antwort ins Gedaechtnis legen -> der naechste Turn hat den Kontext.
        ai.note_message(message.channel.id, ai.bot_name(), antwort, is_bot=True)
        await self._reply_chunks(message, antwort)

    async def on_voice_state_update(self, member, before, after):
        """Join-Sounds: spielt einen Sound, wenn jemand einen Sprachkanal betritt."""
        if VOICE_GAGS_ENABLED:
            self._spawn(voicegags.on_voice_state_update(member, before, after))

    async def on_ready(self):
        log.info("Eingeloggt als %s (ID %s)", self.user, self.user.id)
        if MODE in ("check", "once"):
            try:
                if MODE == "check":
                    await self.run_check()
                else:
                    await self.update_icon(force=True)
            finally:
                await self.close()
            return

        # Dauerbetrieb: Loops starten. tasks.loop fuehrt die erste Runde sofort
        # aus, dadurch werden Icon und Status direkt beim Start gesetzt.
        # Bei einem Reconnect feuert on_ready erneut - dank is_running() starten
        # wir die Loops dann nicht doppelt.
        if AI_ENABLED:
            guild = self.get_guild(GUILD_ID)
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
        # Rarity-Farb-Rollen direkt beim Start im Server anlegen (idempotent), damit
        # die vier Farben (gruen/blau/lila/gold) sofort existieren - nicht erst beim
        # ersten Kauf. Fehlertolerant: fehlende Rechte sprengen den Start nie.
        if ECONOMY_ENABLED:
            for guild in self.guilds:
                try:
                    await economy.ensure_roles(guild)
                except Exception:
                    log.exception("Rarity-Rollen-Setup fehlgeschlagen in '%s'",
                                  getattr(guild, "name", "?"))
        if not self.icon_loop.is_running():
            self.icon_loop.start()
        if not self.status_loop.is_running():
            self.status_loop.start()
        if ECONOMY_ENABLED and not self.voice_xp_loop.is_running():
            self.voice_xp_loop.start()
        if MUSIC_ENABLED and not self.voice_heal_loop.is_running():
            self.voice_heal_loop.start()
        if GAMES_ENABLED and not self.event_loop.is_running():
            self.event_loop.start()
        if AUTODELETE_CHANNEL_IDS and not self.autodelete_sweep_loop.is_running():
            self.autodelete_sweep_loop.start()
        if AUTODELETE_CHANNEL_IDS and not self.autodelete_batch_loop.is_running():
            self.autodelete_batch_loop.start()
        # Wort-Zaehler: einmaliger History-Backfill (neustart-sicher, laeuft im
        # Hintergrund weiter; is_scanning() ist False, sobald alles eingelesen ist).
        if WORDS_ENABLED and words.is_scanning():
            guild = self.get_guild(GUILD_ID)
            if guild is not None:
                self._spawn(words.backfill(guild))
        if ECONOMY_ENABLED and not self.shop_refresh_loop.is_running():
            # Beim Start einmal sicherstellen, dass der Shop fuer HEUTE gewuerfelt ist
            # (falls der Bot ueber den 2-Uhr-Termin hinweg offline war), dann den
            # naechtlichen 2-Uhr-Task starten.
            try:
                await economy.refresh_shop_async(force=False)
            except Exception:
                log.exception("Shop-Start-Refresh fehlgeschlagen - egal, Loop folgt")
            self.shop_refresh_loop.start()

    async def on_disconnect(self):
        """Gateway-Verbindung weg (Internet-Hickup o. Ae.). discord.py versucht
        automatisch, sich wieder zu verbinden - wir loggen es nur leise."""
        log.debug("Discord-Verbindung getrennt - versuche automatisch erneut.")

    async def on_resumed(self):
        """Sitzung nach einem Verbindungsabbruch wieder aufgenommen - Flo ist zurueck,
        ohne dass der Prozess neu starten musste."""
        log.info("Discord-Sitzung wieder aufgenommen - Flo ist zurueck online.")

    async def on_error(self, event_method, *args, **kwargs):
        """Faengt JEDE unbehandelte Ausnahme aus einem Event-Handler ab und loggt sie,
        statt den Bot abstuerzen zu lassen. So bleibt Flo bei einem einzelnen Fehler
        online."""
        log.exception("Unbehandelter Fehler im Event %s - Bot laeuft weiter.", event_method)


client = FloBot(
    intents=intents,
    status=discord.Status.idle,
    activity=discord.CustomActivity(name=WEISHEITEN[0]),
)

# Modul-Aliasse: die Feature-Module (casino, games, economy, luxus, voicegags)
# rufen den Loesch-Schutz per lazy 'import bot' als bot.protect_message /
# bot.release_message auf - die gebundenen Methoden bleiben deshalb unter ihren
# alten Modul-Namen erreichbar.
protect_message = client.protect_message
release_message = client.release_message

# Nach einem Verbindungsproblem wartet der Prozess so lange, bevor er sich frisch
# neu startet (per .env RECONNECT_REEXEC_DELAY anpassbar).
RECONNECT_REEXEC_DELAY = float(os.getenv("RECONNECT_REEXEC_DELAY", "15"))


def _reexec_self():
    """Startet den GANZEN Prozess frisch neu (frischer Client, frische Event-Loop) -
    der robusteste Weg zurueck online, falls discord.py die Verbindung gar nicht
    erst aufbauen konnte (z. B. kein Internet beim Start)."""
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])
    log.warning("Prozess-Neustart per Re-exec nach Verbindungsproblem: %s", " ".join(argv))
    try:
        os.execv(argv[0], argv)
    except OSError:
        log.exception("Re-exec fehlgeschlagen - beende mit Code 42 (systemd startet neu).")
        os._exit(42)


def main():
    if not TOKEN:
        log.error("DISCORD_TOKEN fehlt in der .env-Datei.")
        sys.exit(1)
    if not GUILD_ID:
        log.error("GUILD_ID fehlt in der .env-Datei.")
        sys.exit(1)
    log.info("Starte Bot im Modus: %s", MODE)
    try:
        # reconnect=True (Standard): discord.py faengt Verbindungsabbrueche im
        # laufenden Betrieb selbst ab und verbindet sich mit Backoff neu.
        client.run(TOKEN, reconnect=True, log_handler=None)
    except (discord.LoginFailure, discord.PrivilegedIntentsRequired):
        # Falscher Token / fehlende Intents -> KEIN Auto-Neustart, das muss der
        # Betreiber beheben (sonst Endlosschleife).
        log.exception("Fataler Konfigurationsfehler - bitte Token/Intents pruefen.")
        sys.exit(1)
    except (OSError, discord.GatewayNotFound, discord.ConnectionClosed,
            discord.HTTPException) as exc:
        # Reines Verbindungs-/Netzwerkproblem (kein Internet beim Start, Gateway
        # weg). Im Dauerbetrieb frisch neu starten, statt offline zu bleiben.
        if MODE != "loop":
            log.error("Verbindungsproblem im Modus %s: %s", MODE, exc)
            sys.exit(1)
        log.warning("Verbindungsproblem (%s) - Neustart in %.0fs.", exc, RECONNECT_REEXEC_DELAY)
        time.sleep(RECONNECT_REEXEC_DELAY)
        _reexec_self()


if __name__ == "__main__":
    main()
