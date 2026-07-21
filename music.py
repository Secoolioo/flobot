"""Musik-Feature fuer Flo: spielt YouTube-/Spotify-Links im Sprachkanal ab.

Funktionsweise:
- YouTube:  Link (oder Suchtext) -> yt-dlp zieht den Audio-Stream -> FFmpeg
            spielt ihn in den Voice-Channel. KEIN API-Key noetig.
- Spotify:  Spotify erlaubt KEIN direktes Audio-Streaming. Darum wird der Link
            ueber die Spotify-Web-API zu "Kuenstler - Titel" aufgeloest und das
            Ergebnis auf YouTube gesucht und abgespielt. Dafuer braucht es die
            SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET aus der .env.

Voraussetzungen (sonst ist das Feature einfach aus):
- pip:    yt-dlp, PyNaCl   (PyNaCl = Voice-Verschluesselung fuer discord.py)
- System: ffmpeg           (z. B.  apt install ffmpeg)

Das Modul ist bewusst von der KI entkoppelt. Faellt es aus, laeuft der restliche
Bot (Icon/Status/KI) normal weiter.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import random
import re
import shutil
import time
import urllib.parse
from dataclasses import dataclass, field

import aiohttp
import discord

import ai

try:  # Optional: Bot soll auch ohne yt-dlp starten.
    import yt_dlp
except ImportError:  # pragma: no cover - nur relevant ohne Paket
    yt_dlp = None  # type: ignore[assignment]

log = logging.getLogger("dcbot.music")

# Sentinel: das Modul hat selbst geantwortet (Embed + Buttons direkt gesendet).
# bot.py erkennt das und schickt KEINE zusaetzliche Antwort.
HANDLED = object()

MAX_QUEUE = 50          # Schutz: maximale Laenge der Warteschlange pro Server
DEFAULT_VOLUME = 0.5    # 0.0 - 1.0
# Takt des Voice-Watchdogs (bot.py-Loop). Haelt die Verbindung am Leben und
# repariert Desyncs/Zombies selbst, solange der Bot in einem Call sein SOLL.
VOICE_HEAL_SECONDS = 15
VOICE_ZOMBIE_TICKS = 3        # so viele stille Ticks (=Sek*Ticks) bis "Zombie" -> Neustart
VOICE_RECONNECT_MIN_GAP = 20.0  # Mindestabstand zwischen Reconnects (Loop-Bremse)
VOICE_RECONNECT_MAX_FAILS = 5   # nach so vielen Fehlversuchen am Stueck aufgeben

# Titel des 'Jetzt laeuft'-Panels. bot.py nimmt Bot-Nachrichten mit diesem Titel
# vom Auto-Loeschen aus, damit die Steuer-Buttons den ganzen Song erreichbar
# bleiben (alte Panels raeumt der Player beim Songwechsel selbst weg).
NOWPLAYING_EMBED_TITLE = "▶️  Jetzt läuft"

# --- Optik: Farben + Embed-Helfer ----------------------------------------
_COL_PLAY = 0x1DB954     # Gruen  - laeuft / spielt
_COL_QUEUE = 0x5865F2    # Blurple - Warteschlange / hinzugefuegt
_COL_CTRL = 0xFEE75C     # Gelb   - Steuerung (Pause/Skip/Lautstaerke)
_COL_INFO = 0x95A5A6     # Grau   - neutrale Info
_COL_ERR = 0xED4245      # Rot    - geht gerade nicht

# Audio-Optionen fuer yt-dlp und FFmpeg (bewaehrte Standardwerte).
_YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,          # bei Playlist-Link nur das eine Video nehmen
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",  # IPv4 erzwingen (vermeidet manche Sperren)
    "cachedir": False,
}
# FFmpeg gegen Ruckler/Aussetzer haerten: Die haeufigste Ursache fuer "Lag" beim
# YouTube-Streaming sind kurze Netzwerk-Aussetzer. Mit -reconnect* baut FFmpeg die
# Verbindung selbsttaetig neu auf, statt den Stream abzubrechen.
#   -reconnect 1                 : nach Verbindungsabbruch neu verbinden
#   -reconnect_streamed 1        : auch bei Live-/Nicht-Spulbaren Streams
#   -reconnect_on_network_error 1: auch bei TCP/TLS-Fehlern (ffmpeg >= 4.3)
#   -reconnect_delay_max 5       : bis zu 5 s zwischen den Versuchen warten
_FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
    "-reconnect_delay_max 5"
)
_FFMPEG_OPTS = "-vn"

# --- Geschwindigkeit / "slowed + reverb" ---------------------------------
# Discord-Audio ist immer 48000 Hz Stereo (discord.py haengt -f s16le -ar 48000
# -ac 2 vor unsere -filter:a-Optionen).
_AUDIO_RATE = 48000

# Beim VERLANGSAMEN (speed < 1.0) bauen wir den klassischen "slowed + reverb"-Sound:
# asetrate zieht Tempo UND Tonhoehe zusammen runter (der tiefe, traeumerische Vibe),
# danach eine getunte Hall-Kette. Diese Suffix-Kette folgt auf das Slow-Praefix
#   aresample=48000,asetrate=<R>,aresample=48000
# und ist bewusst rate-unabhaengig (gilt identisch fuer 0.5x und 0.75x).
#
# Aufbau der Kette (per FFmpeg validiert: 0 Clipping, ~ -1.0 dBFS, 113x Realtime):
#   highpass=45          -> raeumt den Sub-Matsch weg, der beim Oktav-Drop (0.5x) entsteht
#   2x aecho             -> dichte Frueh-Reflexionen + weicher Nachhall = lush, nicht Slapback
#   bass/treble/lowpass  -> warmer, dunkler "Tape"-Ton statt schrill/metallisch
#   extrastereo          -> breiteres, immersiveres Hallfeld
#   volume=2.2           -> statischer Make-up-Gain, damit slowed nicht leiser als normal ist
#   alimiter(level=false)-> harte Brick-Wall bei ~ -1 dBFS, verhindert jedes Clipping
_REVERB_SUFFIX = (
    "highpass=f=45,"
    "aecho=0.85:0.88:29|47|71|97:0.5|0.36|0.26|0.18,"
    "aecho=0.8:0.75:131|181:0.22|0.14,"
    "bass=g=2:f=110,treble=g=-3.5:f=4000,lowpass=f=10500,"
    "extrastereo=m=1.5,volume=2.2,"
    "alimiter=level=false:limit=0.89:attack=2:release=80"
)


# --- URL-Erkennung -------------------------------------------------------
_URL_RE = re.compile(r"(https?://\S+|spotify:[a-z]+:\S+)", re.IGNORECASE)
# Hinweis: Die Spotify-App schiebt bei geteilten Links ein Sprach-Praefix ein,
# z. B. open.spotify.com/intl-de/track/...  ->  '(?:intl-[a-z]{2}/)?' faengt das ab.
_SPOTIFY_TRACK_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?track/|spotify:track:)([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_SPOTIFY_PLAYLIST_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:playlist|album)/"
    r"|spotify:(?:playlist|album):",
    re.IGNORECASE,
)
# Wie oben, aber mit Typ (playlist/album) und ID als Gruppen fuer den API-Abruf.
# So viele YouTube-Kandidaten zieht Flo bei Spotify-Songs, um den besten
# (Dauer-/Titel-Match) auszuwaehlen statt blind den ersten Treffer.
_SPOTIFY_SEARCH_N = 6
# Varianten, die bei einem Spotify-Song FAST NIE gemeint sind -> im Best-Match
# abwerten (ausser der Titel selbst enthaelt das Wort). (Wort, Strafpunkte).
_YT_BAD_VARIANTS = (
    ("sped up", 35), ("speed up", 35), ("nightcore", 40), ("slowed", 30),
    ("reverb", 18), ("8d audio", 30), ("cover", 30), ("karaoke", 45),
    ("instrumental", 28), ("remix", 22), ("mashup", 22), ("reaction", 55),
    ("live", 16), ("1 hour", 55), ("1hour", 55), ("10 hours", 60),
    ("loop", 30), ("bass boosted", 22), ("lyrics video", 6),
)

_SPOTIFY_LIST_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?(playlist|album)/"
    r"|spotify:(playlist|album):)([A-Za-z0-9]+)",
    re.IGNORECASE,
)
# Das oeffentliche Embed liefert die Songliste im __NEXT_DATA__-JSON - das umgeht
# die 403-Sperre der Web-API fuer Playlist-Tracks (Client-Credentials duerfen sie
# nicht mehr lesen). Wir ziehen das JSON aus dem <script>-Tag.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
# YouTube-Playlist-ID aus dem Link ziehen. Echte Playlists: PL.../UU.../OLAK5uy_...;
# RD... ist nur ein Auto-Mix/Radio (wird beim Teilen oft angehaengt) -> kein Playlist.
_YT_LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)", re.IGNORECASE)

# Steuerbefehle: (Aktion, Regex am Satzanfang). Reihenfolge = Prioritaet.
_CONTROL = [
    ("skip",   re.compile(r"^(skip|ueberspring|überspring|naechst|nächst|next)", re.I)),
    ("pause",  re.compile(r"^(pause|pausier)", re.I)),
    ("resume", re.compile(r"^(resume|weiter|fortsetz|weiterspiel)", re.I)),
    ("stop",   re.compile(r"^(stop|stopp|halt|aufhoer|aufhör|hoer auf|hör auf)", re.I)),
    ("leave",  re.compile(r"^(leave|verlass|geh raus|hau ab|raus|disconnect)", re.I)),
    ("queue",  re.compile(r"^(queue|warteschlange|liste)", re.I)),
    ("join",   re.compile(r"^(?:join\w*|connect|verbinde\w*|komm)\b", re.I)),
]
# "flo spiel <suchbegriff>" ohne Link -> YouTube-Suche. Nur Imperativ-Formen
# (spiel/spiele/play), damit Fragen wie "spielst du..." NICHT als Befehl gelten.
# Fuellwoerter nach dem Verb (mal/mir/uns/doch/bitte) werden weggeschluckt, damit
# "spiel mir mal <Song>" nicht nach "mir mal <Song>" sucht.
_PLAY_TEXT_RE = re.compile(
    r"^(?:spiele?|play)\s+(?:(?:mal|mir|uns|doch|bitte)\s+)*(.+)", re.I)

# Natuerlichsprachige Play-Trigger: der Song steht in der MITTE ("mach mal <X>
# an", "leg <X> auf", "hau <X> raus", "pack <X> auf/an", "spiel <X> vor", "tu <X>
# an/auf", "kannst du <X> (ab)spielen"). Gruppe 1 = Suchbegriff. Greift nur, wenn
# Flo direkt angesprochen wurde (bot.py ruft music.handle nur dann auf).
_NAT_PLAY_RES = [
    re.compile(r"^mach(?:\s+mir|\s+uns)?(?:\s+mal)?\s+(.+?)\s+an$", re.I),
    re.compile(r"^leg(?:\s+mir|\s+uns)?(?:\s+mal)?\s+(.+?)\s+auf$", re.I),
    re.compile(r"^hau(?:\s+mir|\s+uns)?(?:\s+mal)?\s+(.+?)\s+(?:raus|rein)$", re.I),
    re.compile(r"^pack(?:\s+mir|\s+uns)?(?:\s+mal)?\s+(.+?)\s+(?:auf|an)$", re.I),
    re.compile(r"^tu(?:\s+mir|\s+uns)?(?:\s+mal)?\s+(.+?)\s+(?:an|auf)$", re.I),
    re.compile(r"^spiel(?:e)?(?:\s+mir|\s+uns)?(?:\s+mal)?\s+(.+?)\s+vor$", re.I),
    re.compile(r"^kannst\s+du(?:\s+mir|\s+uns)?(?:\s+mal)?\s+(.+?)\s+(?:ab)?spielen$", re.I),
]
# "mach die musik aus", "stell die mucke ab", "dreh die musik weg" -> stoppen.
_NAT_STOP_RE = re.compile(
    r"^(?:mach|stell|dreh|schalt)\s+(?:die\s+|das\s+|den\s+)?"
    r"(?:musik|music|mucke|mukke|lied|song|sound|radio|beats|playback)\s+"
    r"(?:aus|ab|weg)$", re.I)
# Generische "Musik an"-Floskeln OHNE konkreten Song -> fortsetzen bzw. Hinweis.
_NAT_GENERIC = {
    "musik", "music", "mucke", "mukke", "mukge", "lied", "song", "sound", "sounds",
    "beats", "party", "radio", "was", "etwas", "irgendwas", "irgendwatt", "tunes",
    "playback", "playlist", "playlists", "mukke", "krach", "stimmung", "pause",
}
# Fuehrende Fuellwoerter/Artikel vor dem Song entfernen ("mal die musik" -> "musik").
_NAT_ARTICLE_RE = re.compile(
    r"^(?:die|das|der|den|ne|nen|einen?|eine|bisschen|bissl|etwas|mal|noch|"
    r"wieder|schnell|ma|halt|jetzt)\s+", re.I)
# Feature-/Spielnamen: die sind KEIN Song. Sonst wuerde "mach mal das quiz an"
# YouTube nach "das quiz" durchsuchen, statt das Spiel dem richtigen Handler
# (bzw. der KI) zu ueberlassen. -> in dem Fall gibt der Musik-Parser None zurueck.
_NAT_NOT_A_SONG = {
    "quiz", "casino", "blackjack", "mines", "roulette", "crash", "slots", "slot",
    "keno", "tower", "turm", "hilo", "baccarat", "bakkarat", "rubbellos",
    "glücksrad", "gluecksrad", "don", "duell", "duel", "zahlenraten", "anagramm",
    "mathe", "reaktion", "soundboard", "spiel", "spiele", "game", "runde", "shop",
    "level", "daily", "quizduell", "sieben", "ssp", "rad", "bombe", "bomben",
}

# "flo spiel random" / "flo random" / "flo überrasch mich" -> Genre-Auswahl (Dropdown),
# danach ein zufaelliger Song aus dem Genre. Fuellwoerter (mir/uns/mal/was ...) egal.
_RANDOM_RE = re.compile(
    r"^(?:spiel(?:e|st)?\s+)?"
    r"(?:mir\s+|uns\s+|mal\s+|was\s+|etwas\s+|nen\s+|einen\s+|ne\s+|nal\s+)*"
    r"(?:random|zufall\w*|überrasch\w*|ueberrasch\w*)\b", re.I)

# "flo lyrics [song]" / "songtext" -> Songtext des aktuellen Songs oder eines
# genannten Titels. Gruppe 1 = optionaler Suchbegriff ("Kuenstler - Titel").
_LYRICS_RE = re.compile(r"^(?:lyrics?|songtext|liedtext|text\s+von)\s*(.*)", re.I)
# Kostenlose Songtext-API (kein Key noetig): /v1/<artist>/<title> -> {"lyrics": ...}.
_LYRICS_API = "https://api.lyrics.ovh/v1"
# Deko-Woerter, die YouTube-Titel verschmutzen ("(Official Video)", "[HD]", ...).
_LYRICS_NOISE_RE = re.compile(
    r"\b(official|video|audio|lyrics?|lyric|hd|4k|hq|mv|visualizer|"
    r"music\s*video|remaster(?:ed)?|explicit|prod|clip|full\s*album|"
    r"official\s*music\s*video)\b", re.I)

# Genre -> (Anzeige-Label, Emoji, Song-Pool). Der Pool sind YouTube-Suchbegriffe
# ("Kuenstler - Titel"); daraus zieht Flo per Zufall einen Song. Bewusst bekannte
# Titel, damit die YouTube-Suche zuverlaessig etwas Gutes findet.
_RANDOM_GENRES = {
    "phonk": ("Phonk", "🌫️", [
        "Kordhell - Murder In My Mind", "MoonDeity - Neon Blade",
        "Ghostface Playa - Why Not", "DVRST - Close Eyes", "Hensonn - Sahara",
        "PHARMACIST - Gigachad Theme", "Interworld - Metamorphosis",
        "Freddie Dredd - Cha Cha", "KSLV Noh - Empire", "Scary Garry - Sahara",
        "SVDDEN DEATH - VOID", "PlayaPhonk - Close Eyes", "Sxmbra - Montagem",
        "9mm - Phonk", "Kordhell - Sate",
    ]),
    "deutschrap": ("Deutschrap", "🎤", [
        "Cro - Easy", "Bausa - Was du Liebe nennst", "Capital Bra - Neymar",
        "RAF Camora - Andere Liga", "Kontra K - Erfolg ist kein Glück",
        "Sido - Bilder im Kopf", "Apache 207 - Roller", "Marteria - Kids",
        "Haftbefehl - Chabos wissen wer der Babo ist", "Ufo361 - Ich bin 3 Berliner",
        "Shindy - Affalterbach", "Kollegah - King", "SSIO - 0900",
        "Luciano - Beautiful Girl", "Bonez MC - Mörder",
    ]),
    "rapus": ("Hip-Hop / Rap", "🇺🇸", [
        "Eminem - Lose Yourself", "Kendrick Lamar - HUMBLE", "50 Cent - In Da Club",
        "Drake - God's Plan", "Travis Scott - SICKO MODE", "Kanye West - Stronger",
        "Snoop Dogg - Drop It Like Its Hot", "Dr. Dre - Still D.R.E.",
        "Post Malone - rockstar", "J. Cole - Middle Child", "Tyler The Creator - EARFQUAKE",
        "2Pac - California Love", "Nas - N.Y. State of Mind", "Lil Nas X - Old Town Road",
        "Cardi B - Bodak Yellow",
    ]),
    "rock": ("Rock", "🎸", [
        "Queen - Bohemian Rhapsody", "AC/DC - Thunderstruck",
        "Guns N Roses - Sweet Child O Mine", "Nirvana - Smells Like Teen Spirit",
        "Led Zeppelin - Stairway to Heaven", "Survivor - Eye of the Tiger",
        "Bon Jovi - Livin on a Prayer", "Toto - Africa", "Kansas - Carry On Wayward Son",
        "Deep Purple - Smoke on the Water", "Foo Fighters - Everlong",
        "The Killers - Mr Brightside", "Red Hot Chili Peppers - Californication",
        "Europe - The Final Countdown", "The Rolling Stones - Paint It Black",
    ]),
    "metal": ("Metal", "🤘", [
        "Metallica - Master of Puppets", "System of a Down - Toxicity",
        "Rammstein - Du Hast", "Slipknot - Duality", "Iron Maiden - The Trooper",
        "Sabaton - Bismarck", "Disturbed - Down with the Sickness",
        "Black Sabbath - Paranoid", "Megadeth - Symphony of Destruction",
        "Pantera - Walk", "Lamb of God - Laid to Rest", "Gojira - Stranded",
        "Bring Me The Horizon - Throne", "Trivium - In Waves", "Amon Amarth - Raise Your Horns",
    ]),
    "edm": ("EDM / House", "🔊", [
        "Avicii - Levels", "Martin Garrix - Animals", "Alan Walker - Faded",
        "Swedish House Mafia - Don't You Worry Child", "Skrillex - Bangarang",
        "David Guetta - Titanium", "Calvin Harris - Summer", "Marshmello - Alone",
        "Zedd - Clarity", "deadmau5 - Strobe", "Daft Punk - One More Time",
        "The Chainsmokers - Closer", "Kygo - Firestone", "Tiesto - Red Lights",
        "Illenium - Good Things Fall Apart",
    ]),
    "pop": ("Pop", "✨", [
        "The Weeknd - Blinding Lights", "Dua Lipa - Levitating", "Ed Sheeran - Shape of You",
        "Billie Eilish - bad guy", "Harry Styles - As It Was", "Michael Jackson - Billie Jean",
        "Miley Cyrus - Flowers", "Bruno Mars - Uptown Funk", "Taylor Swift - Shake It Off",
        "Ariana Grande - 7 rings", "Justin Bieber - Sorry", "Lady Gaga - Poker Face",
        "Rihanna - Umbrella", "Katy Perry - Firework", "Olivia Rodrigo - good 4 u",
    ]),
    "party": ("Party / Malle", "🥳", [
        "Mickie Krause - Finger im Po Mexiko", "Scooter - How Much Is The Fish",
        "DJ Ötzi - Anton aus Tirol", "Peter Wackel - Joana", "Lorenz Büffel - Johnny Däpp",
        "Almklausi - Mallorca da bin ich daheim", "Jürgen Drews - Ein Bett im Kornfeld",
        "DJ Robin - Layla", "Klaus und Klaus - An der Nordseeküste", "Loona - Bailando",
        "Ikke Hüftgold - Dicke", "Culcha Candela - Hamma", "Brings - Superjeilezick",
        "Wolfgang Petry - Wahnsinn", "Mia Julia - Oewer",
    ]),
    "lofi": ("Lofi / Chill", "🌙", [
        "lofi hip hop radio beats to relax", "Nujabes - Aruarian Dance",
        "Joji - Slow Dancing in the Dark", "Idealism - Controlla",
        "Kudasai - The Girl I Havent Met", "Potsu - Im Closing My Eyes",
        "Aso - Bloom", "jinsang - affection", "Sarcastic Sounds - Lonely",
        "Powfu - death bed", "L'indécis - Soulful", "Philanthrope - Landscape",
        "sleepy - lost", "Chillhop Essentials", "Mac Ayres - Slow Down",
    ]),
    "eighties": ("80er", "📼", [
        "a-ha - Take On Me", "Michael Jackson - Thriller",
        "Rick Astley - Never Gonna Give You Up", "Journey - Don't Stop Believin",
        "Whitney Houston - I Wanna Dance with Somebody",
        "Tears for Fears - Everybody Wants to Rule the World",
        "Cyndi Lauper - Girls Just Want to Have Fun", "Dead or Alive - You Spin Me Round",
        "Depeche Mode - Enjoy the Silence", "Queen - Another One Bites the Dust",
        "Bonnie Tyler - Total Eclipse of the Heart", "Toto - Africa",
        "Europe - The Final Countdown", "Kim Wilde - Kids in America",
        "Duran Duran - Hungry Like the Wolf",
    ]),
    "gaming": ("Gaming / Hype", "🎮", [
        "TheFatRat - Unity", "TheFatRat - Monody", "Warriyo - Mortals",
        "Different Heaven - Nekozilla", "NEFFEX - Cold", "NEFFEX - Fight Back",
        "Alan Walker - Spectre", "Tobu - Hope", "Elektronomia - Sky High",
        "K-391 - Earth", "Razihel - Love U", "DM DOKURO - The Tale of a Cruel World",
        "Ross Bugden - Battle", "CS GO Main Menu Theme", "Rob Gasser - I Remember",
    ]),
}

# "flo nochmal", "flo spiel nochmal 2", "flo repeat 3", "flo wiederhole" ->
# den zuletzt (bzw. N-t-letzten) gespielten Song noch einmal spielen.
_REPLAY_RE = re.compile(
    r"^(?:spiel(?:e|st)?\s+)?"
    r"(?:nochmal(?:s)?|noch\s*mal|repeat|replay|wiederhol(?:e|en|st)?)"
    r"\s*(\d+)?\b", re.I)

# Lautstaerke - tolerant: "flo lautstärke 30", "flo ls 80", "flo LS", "flo vol 50",
# "flo lautstärke auf 30" sowie gaengige Tippfehler. Ohne Zahl -> aktuelle anzeigen.
_VOLUME_UP_RE = re.compile(r"^(?:lauter|louder|lautr)\b", re.I)
_VOLUME_DOWN_RE = re.compile(r"^(?:leiser|quieter|leise)\b", re.I)
# Erstes Wort + optionale Zahl ("auf"/"%"/ohne Leerzeichen alles ok).
_VOLUME_ARG_RE = re.compile(r"^([A-Za-zÄÖÜäöüß]+)\.?\s*(?:auf\s*)?(\d{1,3})?", re.I)
# Eindeutige Kurz-/Langformen (Vergleich case-insensitiv ueber .lower()).
_VOLUME_WORDS = {
    "ls", "lst", "lstk", "lstrk", "lstrke", "vol", "volume", "lautst", "lautstk",
    "lautstaerke", "lautstärke", "lautstarke", "lautstrke", "lautstaerk",
    "lautstärk", "lautsärke", "lautstärje", "lautsterke", "lautstaeke", "lautsärcke",
}
# Kanonische Schreibweisen fuer den Tippfehler-Abgleich (difflib).
_VOLUME_CANON = ("lautstärke", "lautstaerke", "lautstarke", "volume")


# --- Track + Player ------------------------------------------------------
@dataclass
class Track:
    title: str
    stream_url: str            # leer = noch nicht aufgeloest (lazy, siehe query)
    webpage_url: str = ""
    duration: int | None = None
    requested_by: str = ""
    query: str = ""            # YouTube-Suchbegriff fuer spaetes Aufloesen (Playlist)
    thumbnail: str = ""        # Cover/Vorschaubild fuer das Embed (sofern bekannt)
    match_hint: "dict | None" = None  # Spotify-Metadaten (Titel/Kuenstler/Dauer) fuer Best-Match


@dataclass
class GuildPlayer:
    """Haelt Voice-Verbindung und Warteschlange fuer EINEN Server."""
    loop: asyncio.AbstractEventLoop
    queue: list[Track] = field(default_factory=list)
    history: list[Track] = field(default_factory=list)  # zuletzt gespielt (fuer 'nochmal')
    voice: discord.VoiceClient | None = None
    current: Track | None = None
    text_channel: discord.abc.Messageable | None = None
    volume: float = DEFAULT_VOLUME   # 0.0 - 2.0, per Befehl aenderbar
    panel_message: "discord.Message | None" = None  # aktuelles Steuer-Panel
    speed: float = 1.0               # 0.5 - 2.0, per Tempo-Dropdown im Panel waehlbar
    _seg_start: float | None = None  # monotonic: Start des laufenden Abschnitts (None=aus/pausiert)
    _played: float = 0.0             # bereits gespielte Song-Sekunden vor diesem Abschnitt
    _play_gen: int = 0               # Generation des aktuell gueltigen Players (gegen Race beim Neustart)
    active_channel_id: int | None = None  # in DIESEM Kanal soll der Bot bleiben (None = bewusst raus)
    _advancing: bool = False         # laeuft gerade _advance (Songwechsel)? -> Watchdog haelt sich raus
    _stall_ticks: int = 0            # Zaehler fuer "verbunden, aber still" (Zombie-Erkennung, entprellt)
    _last_reconnect: float = 0.0     # monotonic des letzten Reconnect-Versuchs (Loop-Bremse)
    _reconnect_fails: int = 0        # aufeinanderfolgende fehlgeschlagene Reconnects (Aufgabe-Schwelle)
    # Serialisiert ALLE voice-veraendernden Ops (connect/_reconnect/apply_speed),
    # damit nie zwei channel.connect() gleichzeitig laufen.
    _voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def connect(self, channel):
        # Lock: nie gleichzeitig mit einem Watchdog-_reconnect verbinden.
        async with self._voice_lock:
            vc = self.voice if (self.voice and self.voice.is_connected()) else channel.guild.voice_client
            if vc is not None and vc.is_connected():
                self.voice = vc
                if vc.channel.id != channel.id:
                    try:
                        await vc.move_to(channel)
                    except Exception:  # noqa: BLE001 - move_to gescheitert -> sauber neu verbinden
                        log.warning("move_to gescheitert, verbinde neu in '%s'", channel.name)
                        await self._fresh_connect(channel)
            else:
                await self._fresh_connect(channel)
            self.active_channel_id = channel.id   # ab jetzt: hier drinbleiben (Watchdog haelt's am Leben)
            self._reconnect_fails = 0
        return self.voice

    async def _fresh_connect(self, channel):
        """Raeumt einen evtl. haengenden Client weg und verbindet frisch.
        NUR aus gehaltenem _voice_lock heraus aufrufen."""
        stale = self.voice or channel.guild.voice_client
        if stale is not None:
            try:
                await asyncio.wait_for(stale.disconnect(force=True), timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.voice = None
        self.voice = await channel.connect(self_deaf=True, reconnect=True)

    def is_active(self):
        return self.voice is not None and (self.voice.is_playing() or self.voice.is_paused())

    def start(self, track, *, seek = 0.0, keep_speed = False):
        """Startet einen Track sofort (nutzt die bereits aufgeloeste Stream-URL).

        seek = Song-Sekunde, ab der gespielt wird (fuer nahtlosen Tempo-Wechsel).
        keep_speed = True nur beim Effekt-Neustart DESSELBEN Songs (apply_speed) -
        dann bleibt das gewaehlte Tempo; sonst startet jeder neue Song auf Normaltempo.
        Bei speed != 1.0 wird die passende Filterkette angehaengt (atempo bzw.
        slowed+reverb)."""
        if self.voice is None or not self.voice.is_connected():
            raise RuntimeError("keine Voice-Verbindung")
        if not keep_speed:
            # Jeder NEUE Song startet immer auf Normaltempo - der Effekt wird pro Song
            # einzeln gewaehlt.
            self.speed = 1.0
        before = _FFMPEG_BEFORE
        if seek > 0.5:
            # -ss VOR -i = schneller Eingangs-Seek, damit der Song an der Stelle
            # weiterlaeuft statt von vorne (Tempo/Reverb aendern nur den Klang, nicht die Pos.)
            before = f"-ss {seek:.2f} {_FFMPEG_BEFORE}"
        opts = _FFMPEG_OPTS
        af = _build_audio_filter(self.speed)
        if af is not None:
            # Speed-up: atempo (Tonhoehe bleibt). Slow: slowed + reverb (siehe _build_audio_filter).
            opts = f"{_FFMPEG_OPTS} -filter:a {af}"
        source = discord.FFmpegPCMAudio(
            track.stream_url, before_options=before, options=opts
        )
        self.current = track
        self._played = seek          # Positions-Uhr auf die Startstelle setzen
        self._seg_start = time.monotonic()
        self._stall_ticks = 0        # frisch gestartet (buffert evtl. kurz) -> kein Zombie-Alarm
        # Jede Wiedergabe bekommt eine eigene Generation. Der after-Callback merkt
        # sie sich fest - so kann ein verspaeteter Callback eines bereits ersetzten
        # Players (z. B. nach einem Tempo-Wechsel) nichts mehr ausloesen.
        self._play_gen += 1
        gen = self._play_gen
        try:
            self.voice.play(
                discord.PCMVolumeTransformer(source, self.volume),
                after=lambda err, g=gen: self._after(err, g),
            )
        except Exception:
            # play() wirft (z. B. 'Already playing' / 'Not connected') -> der schon
            # gespawnte ffmpeg-Prozess muss beendet werden, sonst bleibt ein Zombie.
            source.cleanup()
            raise
        if not keep_speed:
            # Jeden NEU gestarteten Song in den Verlauf legen (fuer 'flo nochmal').
            # Effekt-/Tempo-Neustarts (keep_speed) zaehlen nicht als neuer Song.
            self.history.append(track)
            del self.history[:-30]   # nur die letzten 30 behalten

    def position(self):
        """Aktuelle Song-Position in Sekunden (best effort, tempo-/pausen-bewusst)."""
        pos = self._played
        if self._seg_start is not None:
            pos += (time.monotonic() - self._seg_start) * self.speed
        return max(0.0, pos)

    def _clock_pause(self):
        """Positions-Uhr beim Pausieren einfrieren."""
        if self._seg_start is not None:
            self._played += (time.monotonic() - self._seg_start) * self.speed
            self._seg_start = None

    def _clock_resume(self):
        """Positions-Uhr beim Fortsetzen weiterlaufen lassen."""
        if self._seg_start is None:
            self._seg_start = time.monotonic()

    async def apply_speed(self, new_speed):
        """Setzt die Geschwindigkeit und startet den laufenden Song an der aktuellen
        Stelle mit neuem Tempo neu. True = live umgestellt, False = nur gemerkt
        (gilt dann fuer den naechsten Song)."""
        new_speed = max(0.5, min(2.0, float(new_speed)))
        # Lock: serialisiert schnelle Doppelklicks und haelt den Watchdog waehrend
        # des stop->start-Fensters raus (heal() ueberspringt, solange das Lock haelt).
        async with self._voice_lock:
            track = self.current
            if track is None or self.voice is None or not self.voice.is_connected() \
                    or not (self.voice.is_playing() or self.voice.is_paused()):
                self.speed = new_speed   # nichts laeuft -> nur merken, gilt fuer naechsten Song
                return False
            pos = self.position()        # Position noch mit ALTEM Tempo berechnen ...
            self.speed = new_speed       # ... dann erst auf das neue Tempo umstellen
            # Generation hochzaehlen, BEVOR wir stoppen: der after-Callback des jetzt
            # gestoppten Players ist damit garantiert veraltet und loest kein _advance aus -
            # egal, wann er (verspaetet, aus dem FFmpeg-Thread) feuert.
            self._play_gen += 1
            try:
                self.voice.stop()                 # killt die alte Quelle (ihr after ist jetzt stale)
                for _ in range(40):               # warten bis die alte Quelle wirklich weg ist
                    if not self.voice.is_playing():
                        break
                    await asyncio.sleep(0.05)
                self.start(track, seek=pos, keep_speed=True)   # gleiche Stelle, Tempo bleibt
            except Exception:
                log.exception("Tempo-Wechsel fehlgeschlagen")
                return False
        return True

    def _after(self, error, gen):
        # Laeuft in einem FFmpeg-Thread -> Arbeit zurueck in den Event-Loop schieben.
        # Alles abfangen: ein Fehler hier darf den Player-Thread NICHT mitreissen.
        if error:
            log.error("FFmpeg/Player-Fehler: %s", error)
        if gen != self._play_gen:
            return  # veralteter Callback eines ersetzten/gestoppten Players -> ignorieren
        try:
            asyncio.run_coroutine_threadsafe(self._advance(), self.loop)
        except Exception:
            log.exception("Konnte naechsten Track nach Songende nicht einplanen")

    async def _advance(self):
        """Spielt den naechsten abspielbaren Track. Kaputte/altersbeschraenkte
        Eintraege (yt-dlp DownloadError, 'keine Treffer', tote Links) werden
        UEBERSPRUNGEN statt den Player anzuhalten - so bleibt die Musik bei einem
        faulen Song nicht stehen. Schleife statt Rekursion, damit auch eine ganze
        Reihe toter Songs sauber uebersprungen wird."""
        # _advancing markiert die (ggf. langsame) Aufloesephase, damit der Voice-
        # Watchdog in dieser Luecke KEINEN Zombie-Alarm ausloest.
        self._advancing = True
        try:
            while True:
                if not self.voice or not self.voice.is_connected() or not self.queue:
                    self.current = None
                    await _retire_panel(self)
                    return
                track = self.queue.pop(0)
                try:
                    if not track.stream_url and track.query:
                        track = await _resolve_track(track)  # Playlist-Track jetzt aufloesen
                    self.start(track)
                except Exception:
                    log.exception("Track uebersprungen (nicht ladbar): %s", track.title)
                    continue  # naechsten Song versuchen, nicht stoppen
                # Erfolgreich gestartet. Das Panel ist nur Deko - faellt es (Netzwerk)
                # aus, darf das den laufenden Song NICHT abbrechen.
                try:
                    await _send_panel(self, track)
                except Exception:
                    log.exception("Now-Playing-Panel nach Advance fehlgeschlagen (egal)")
                return
        finally:
            self._advancing = False

    async def disconnect(self):
        self.queue.clear()
        self.current = None
        self.speed = 1.0           # frische Session startet wieder mit Normaltempo
        self._seg_start = None
        self._played = 0.0
        self.active_channel_id = None   # bewusst raus -> Watchdog soll NICHT zurueckholen
        self._stall_ticks = 0
        self._play_gen += 1             # alte after-Callbacks entwerten
        await _retire_panel(self)
        if self.voice is not None:
            try:
                await self.voice.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
            self.voice = None

    # --- Selbstheilung: haelt die Voice-Verbindung am Leben ---------------
    async def heal(self, guild):
        """Periodischer Watchdog (bot.py-Loop). Sorgt dafuer, dass der Bot in
        SEINEM Kanal verbunden bleibt und repariert Desyncs/Zombies selbst.
        Tut nichts, wenn der Bot bewusst draussen ist, gerade ein Songwechsel
        laeuft oder schon eine voice-Op (connect/reconnect/Tempo) aktiv ist."""
        if self.active_channel_id is None or self._advancing or self._voice_lock.locked():
            return
        channel = guild.get_channel(self.active_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            self.active_channel_id = None   # Kanal gibt es nicht mehr -> aufgeben
            return
        # Realen Voice-Client bestimmen (unser Objekt KANN abgehaengt sein).
        vc = self.voice if (self.voice and self.voice.is_connected()) else guild.voice_client
        if vc is None or not vc.is_connected():
            log.warning("Voice-Desync: sollte in '%s' verbunden sein, ist es nicht.", channel.name)
            await self._reconnect(channel)
            return
        self.voice = vc   # echten Client adoptieren (Discord kennt ihn, wir bisher nicht)
        # Zombie: verbunden, sollte spielen, tut es aber mehrere Ticks lang nicht.
        if self.current is not None and not vc.is_paused() and not vc.is_playing():
            self._stall_ticks += 1
            if self._stall_ticks >= VOICE_ZOMBIE_TICKS:
                self._stall_ticks = 0
                log.warning("Voice-Zombie: verbunden, aber still - starte neu.")
                await self._reconnect(channel)
        else:
            self._stall_ticks = 0

    async def _reconnect(self, channel):
        """Raeumt eine tote/zombie Verbindung weg, verbindet frisch und setzt den
        laufenden Song fort. Loop-gebremst (Mindestabstand) und mit Aufgabe-
        Schwelle gegen Endlos-Versuche; alles mit Timeouts gegen Haenger."""
        if time.monotonic() - self._last_reconnect < VOICE_RECONNECT_MIN_GAP:
            return  # zu kurz her -> der Verbindung/dem Buffering erst Zeit geben
        async with self._voice_lock:
            # Unter Lock nochmal pruefen: hat sich das Problem schon erledigt
            # (discord.py-Auto-Reconnect oder paralleler connect)? Dann NICHT abreissen.
            live = self.voice if (self.voice and self.voice.is_connected()) else channel.guild.voice_client
            if live is not None and live.is_connected() and (
                    self.current is None or live.is_playing() or live.is_paused()):
                self.voice = live
                self._reconnect_fails = 0
                return
            # Wiedergabe ist gerissen -> Positions-Uhr JETZT einfrieren, damit der Song
            # an der zuletzt gehoerten Stelle fortsetzt und nicht die Ausfallzeit ueberspringt.
            self._clock_pause()
            self._last_reconnect = time.monotonic()
            self._play_gen += 1   # evtl. noch fliegende after-Callbacks entwerten
            # alte/halbtote Verbindung hart wegraeumen
            old = self.voice or channel.guild.voice_client
            if old is not None:
                try:
                    await asyncio.wait_for(old.disconnect(force=True), timeout=10)
                except Exception:  # noqa: BLE001
                    pass
            self.voice = None
            try:
                self.voice = await asyncio.wait_for(
                    channel.connect(self_deaf=True, reconnect=True), timeout=20)
            except discord.ClientException:
                # 'Already connected' -> Geist-Client haengt im Guild. Hart weg, 1x retry.
                ghost = channel.guild.voice_client
                if ghost is not None:
                    try:
                        await asyncio.wait_for(ghost.disconnect(force=True), timeout=10)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    self.voice = await asyncio.wait_for(
                        channel.connect(self_deaf=True, reconnect=True), timeout=20)
                except Exception:  # noqa: BLE001
                    self._note_reconnect_fail(channel)
                    return
            except Exception:  # noqa: BLE001
                self._note_reconnect_fail(channel)
                return
            # Erfolg: Wiedergabe fortsetzen (laufenden Song an aktueller Stelle, sonst naechsten).
            self._reconnect_fails = 0
            if self.current is not None:
                try:
                    self.start(self.current, seek=self.position(), keep_speed=True)
                except Exception:  # noqa: BLE001
                    log.exception("Resume nach Reconnect fehlgeschlagen")
            elif self.queue:
                await self._advance()
            log.info("Voice in '%s' wiederhergestellt.", channel.name)

    def _note_reconnect_fail(self, channel):
        """Zaehlt fehlgeschlagene Reconnects; nach zu vielen am Stueck gibt der
        Watchdog auf (Marker loeschen), damit kein Endlos-Loop entsteht. Ein neues
        'Flo spiel' startet sauber neu."""
        self._reconnect_fails += 1
        if self._reconnect_fails >= VOICE_RECONNECT_MAX_FAILS:
            log.error("Voice-Reconnect in '%s' nach %d Versuchen aufgegeben.",
                      channel.name, self._reconnect_fails)
            self.active_channel_id = None
            self._reconnect_fails = 0
        else:
            log.warning("Voice-Reconnect fehlgeschlagen (%d/%d).",
                        self._reconnect_fails, VOICE_RECONNECT_MAX_FAILS)


# --- Interaktiv: Position in der Warteschlange aendern --------------------
class _PositionModal(discord.ui.Modal):
    """Tippfeld fuer eine konkrete Wunsch-Position."""

    def __init__(self, view):
        super().__init__(title="Position in der Warteschlange")
        self._view = view
        self.feld = discord.ui.TextInput(
            label="Position (1 = als Nächstes)",
            placeholder=f"1 – {max(1, len(view.player.queue))}",
            required=True, max_length=3,
        )
        self.add_item(self.feld)

    async def on_submit(self, interaction):
        raw = (self.feld.value or "").strip()
        if not raw.lstrip("+").isdigit():
            await interaction.response.send_message(
                "Gib bitte eine Zahl ein (z. B. `1` für als Nächstes).", ephemeral=True)
            return
        emb = self._view.apply_move(int(raw) - 1)
        if emb is None:
            await interaction.response.edit_message(
                embed=_gone_embed(self._view.track), view=None)
            self._view.stop()
            return
        await interaction.response.edit_message(embed=emb, view=self._view)


class _RandomGenreSelect(discord.ui.Select):
    """Dropdown mit allen Genres (plus 'Überrasch mich' fuer voll zufaellig)."""

    def __init__(self):
        options = [discord.SelectOption(
            label="Überrasch mich", value="surprise", emoji="🎲",
            description="völlig zufälliges Genre")]
        for key, (label, emoji, _pool) in _RANDOM_GENRES.items():
            options.append(discord.SelectOption(label=label, value=key, emoji=emoji))
        super().__init__(placeholder="Welches Genre? 🎧", min_values=1, max_values=1,
                         options=options)

    async def callback(self, interaction):
        # Auswahl ist getroffen - View beenden und den Zufalls-Song starten.
        self.view.stop()
        await instance.start_random(interaction, self.values[0])


class RandomGenreView(discord.ui.View):
    """Genre-Auswahl fuer 'flo spiel random'. Nur der Aufrufer darf waehlen."""

    def __init__(self, owner_id, *, timeout = 120.0):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message = None
        self.add_item(_RandomGenreSelect())

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Das ist nicht deine Auswahl – tipp dir mit `flo spiel random` eine eigene. 🎲",
            ephemeral=True)
        return False

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class QueuePositionView(discord.ui.View):
    """Buttons unter einem frisch hinzugefuegten Song: an Position vorziehen."""

    def __init__(self, player, track, owner_id,
                *, timeout = 120.0):
        super().__init__(timeout=timeout)
        self.player = player
        self.track = track
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id == self.owner_id or (perms and perms.manage_messages):
            return True
        await interaction.response.send_message(
            "Nur wer den Song hinzugefügt hat (oder das Team) darf die Position ändern.",
            ephemeral=True)
        return False

    def _index(self):
        """Aktuelle Stelle des Tracks (per Identitaet, da er weiterrueckt)."""
        for i, t in enumerate(self.player.queue):
            if t is self.track:
                return i
        return None

    def apply_move(self, target_index):
        """Verschiebt den Track an target_index (0-basiert). None = nicht mehr da."""
        idx = self._index()
        if idx is None:
            return None
        total = len(self.player.queue)
        target_index = max(0, min(target_index, total - 1))
        if target_index != idx:
            t = self.player.queue.pop(idx)
            self.player.queue.insert(target_index, t)
        return _added_embed(
            self.track, target_index + 1, len(self.player.queue),
            title="📍  Position aktualisiert",
            footer="Passt? Sonst nochmal verschieben.",
        )

    @discord.ui.button(label="Als Nächstes", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def _next(self, interaction, _button):
        emb = self.apply_move(0)
        if emb is None:
            await interaction.response.edit_message(embed=_gone_embed(self.track), view=None)
            self.stop()
            return
        await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Position wählen", emoji="📍", style=discord.ButtonStyle.secondary)
    async def _choose(self, interaction, _button):
        if self._index() is None:
            await interaction.response.edit_message(embed=_gone_embed(self.track), view=None)
            self.stop()
            return
        await interaction.response.send_modal(_PositionModal(self))

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# Auswaehlbare Geschwindigkeiten (atempo deckt 0.5-2.0 ab).
_SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)


class _SpeedSelect(discord.ui.Select):
    """Dropdown im Panel: Songgeschwindigkeit waehlen. Stellt den laufenden Song
    sofort an der aktuellen Stelle mit dem neuen Tempo um (FFmpeg atempo)."""

    def __init__(self, player):
        self.player = player
        super().__init__(placeholder="🎚️ Geschwindigkeit wählen …",
                         min_values=1, max_values=1, options=self._opts(), row=1)

    def _opts(self):
        cur = self.player.speed
        out = []
        for s in _SPEEDS:
            if s < 1.0:
                emoji, label = "🌌", f"{s:g}× · slowed + reverb"
                desc = "langsamer & tiefer mit Hall"
            elif s > 1.0:
                emoji, label, desc = "🚀", f"{s:g}× · speed", "schneller, gleiche Tonhöhe"
            else:
                emoji, label, desc = "🎵", "1× · normal", "Originaltempo"
            out.append(discord.SelectOption(label=label, value=f"{s}", emoji=emoji,
                                            description=desc, default=abs(s - cur) < 1e-3))
        return out

    def refresh(self):
        """Optionen neu aufbauen, damit das aktuelle Tempo als ausgewaehlt erscheint."""
        self.options = self._opts()

    async def callback(self, interaction):
        v = self.player.voice
        if v is None or not (v.is_playing() or v.is_paused()):
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        new = float(self.values[0])
        await interaction.response.defer()        # Tempo-Wechsel kann ~1s dauern
        await self.player.apply_speed(new)
        self.refresh()
        try:
            cur = self.player.current
            if cur is not None:
                emb = _now_playing_embed(cur, len(self.player.queue), speed=self.player.speed)
                await interaction.edit_original_response(embed=emb, view=self.view)
            else:
                await interaction.edit_original_response(view=self.view)
        except discord.HTTPException:
            pass


class LyricsView(discord.ui.View):
    """Blaettert lange Songtexte seitenweise durch (◀ / ▶). Bei nur einer Seite
    kommen keine Buttons. Funktioniert oeffentlich UND ephemer (Button-Callbacks
    editieren die Nachricht ueber die Interaction)."""

    def __init__(self, pages, artist, title, thumb, *, timeout = 300.0):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.artist = artist
        self.title = title
        self.thumb = thumb
        self.idx = 0
        self.message = None
        if len(pages) <= 1:
            self.clear_items()      # eine Seite -> keine Blaetter-Buttons noetig
        else:
            self._sync()

    def embed(self):
        return instance._lyrics_embed(
            self.artist, self.title, self.pages[self.idx], self.idx, len(self.pages),
            self.thumb)

    def _sync(self):
        self._prev.disabled = self.idx <= 0
        self._next.disabled = self.idx >= len(self.pages) - 1

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def _prev(self, interaction, _b):
        self.idx = max(0, self.idx - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def _next(self, interaction, _b):
        self.idx = min(len(self.pages) - 1, self.idx + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PlaybackControlView(discord.ui.View):
    """Steuerpanel unter 'Jetzt laeuft': Pause/Weiter, Skip, Stop, Queue + Tempo-Dropdown.

    timeout=None: bleibt fuer die ganze (ggf. lange) Songdauer aktiv. Beim Posten
    eines neuen Panels wird das alte ueber _send_panel sauber entschaerft.
    """

    def __init__(self, player):
        super().__init__(timeout=None)
        self.player = player
        self.message = None
        self._sync_pause()
        self._speed_select = _SpeedSelect(player)   # eigene Zeile unter den Buttons
        self.add_item(self._speed_select)

    def _sync_pause(self):
        """Pause-Button passend zum aktuellen Zustand beschriften."""
        v = self.player.voice
        paused = bool(v and v.is_paused())
        self._pause.label = "Weiter" if paused else "Pause"
        self._pause.emoji = "▶️" if paused else "⏸️"
        self._pause.style = (discord.ButtonStyle.success if paused
                             else discord.ButtonStyle.secondary)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def _pause(self, interaction, _b):
        v = self.player.voice
        if v is None or not (v.is_playing() or v.is_paused()):
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        if v.is_paused():
            v.resume()
            self.player._clock_resume()   # Positions-Uhr weiterlaufen lassen
        else:
            v.pause()
            self.player._clock_pause()    # Positions-Uhr einfrieren
        self._sync_pause()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def _skip(self, interaction, _b):
        if not self.player.is_active():
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        # stop() loest _after -> _advance aus; _advance postet ein frisches Panel
        # und entschaerft dabei dieses hier. Darum nur kurz bestaetigen.
        self.player.voice.stop()  # type: ignore[union-attr]
        await interaction.response.defer()

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def _stop(self, interaction, _b):
        if self.player.voice is None or not self.player.voice.is_connected():
            await interaction.response.send_message("Ich bin in keinem Sprachkanal.", ephemeral=True)
            return
        # Diese Nachricht wird gleich zur 'Gestoppt'-Bestaetigung umgebaut -> aus der
        # Panel-Verwaltung nehmen, damit disconnect()->_retire_panel sie NICHT loescht.
        self.player.panel_message = None
        await self.player.disconnect()
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(
            embed=_embed("Musik gestoppt und raus aus dem Sprachkanal.",
                         title="⏹️  Gestoppt", color=_COL_INFO),
            view=self)
        self.stop()

    @discord.ui.button(label="Queue", emoji="🎶", style=discord.ButtonStyle.secondary)
    async def _queue(self, interaction, _b):
        await interaction.response.send_message(embed=_queue_embed(self.player), ephemeral=True)

    @discord.ui.button(label="Lyrics", emoji="🎤", style=discord.ButtonStyle.secondary)
    async def _lyrics(self, interaction, _b):
        track = self.player.current
        if track is None:
            await interaction.response.send_message("Gerade läuft nichts. 🤔", ephemeral=True)
            return
        # Nur der Klickende sieht den Text (ephemer) - kein Zuspammen des Channels.
        # Abruf kann dauern -> defer, sonst reisst die 3s-Frist.
        await interaction.response.defer(ephemeral=True)
        emb, view = await instance._build_lyrics(
            track.title, getattr(track, "thumbnail", "") or None)
        if view is not None:
            await interaction.followup.send(embed=emb, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=emb, ephemeral=True)


class Music:
    """Buendelt Zustand und Logik des Musik-Features (frueher freie
    Modul-Funktionen und globale Variablen dieses Moduls)."""

    def __init__(self):
        # --- Konfiguration (in setup() aus der .env gelesen) ---------------------
        self._enabled = False
        self._bot_name = "Flo"
        self._spotify_id = ""
        self._spotify_secret = ""
        # --- Spotify-Token (Client-Credentials, 1 h gueltig, hier gecached) ------
        self._sp_token = {"value": "", "exp": 0.0}
        # Player-/Queue-Zustand pro Server (guild_id -> GuildPlayer).
        self._players = {}

    def _fmt_dur(self, secs):
        """Sekunden -> 'm:ss' bzw. 'h:mm:ss' (leer, wenn unbekannt)."""
        if not secs or secs <= 0:
            return ""
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _short(self, text, limit = 60):
        """Kuerzt lange Titel fuer Listen (haelt Embed-Felder unter dem 1024er-Limit)."""
        text = (text or "").strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _embed(self, desc = "", *, title = None, color = _COL_INFO):
        """Kleiner Embed-Baukasten fuer einzeilige Antworten."""
        e = discord.Embed(color=color)
        if title:
            e.title = title
        if desc:
            e.description = desc
        return e

    def _build_audio_filter(self, speed):
        """Baut die -filter:a-Kette fuer die gewuenschte Geschwindigkeit.

        None  -> Normaltempo, kein Filter.
        >1.0  -> reines atempo (Tonhoehe bleibt, kein Reverb) - Speed-up.
        <1.0  -> slowed + reverb (asetrate-Pitchdrop + Hall-Kette)."""
        if abs(speed - 1.0) <= 1e-3:
            return None
        if speed > 1.0:
            return f"atempo={speed:.3f}"
        rate = round(_AUDIO_RATE * speed)   # 0.5 -> 24000 (Oktave tiefer), 0.75 -> 36000
        return f"aresample={_AUDIO_RATE},asetrate={rate},aresample={_AUDIO_RATE},{_REVERB_SUFFIX}"

    def _is_volume_word(self, word):
        """True, wenn das Wort 'Lautstaerke' meint - inkl. Kurzform (ls) und Tippfehler."""
        w = word.lower().strip(".:!?")
        if w in ("lauter", "louder", "lautr", "leiser", "quieter", "leise"):
            return False  # relative Befehle - die laufen ueber _VOLUME_UP/DOWN_RE
        if w in _VOLUME_WORDS:
            return True
        # Tippfehler: ab 5 Zeichen nah an einer kanonischen Schreibweise.
        return len(w) >= 5 and bool(
            difflib.get_close_matches(w, _VOLUME_CANON, n=1, cutoff=0.8)
        )

    def setup(self):
        """Liest die Konfiguration und prueft die Voraussetzungen.

        Rueckgabe: True, wenn das Musik-Feature aktiv ist.
        """
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        self._spotify_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self._spotify_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

        if yt_dlp is None:
            log.warning("Musik-Feature aus: Paket 'yt-dlp' ist nicht installiert.")
            return False
        if shutil.which("ffmpeg") is None:
            log.warning("Musik-Feature aus: 'ffmpeg' nicht gefunden (z. B. 'apt install ffmpeg').")
            return False
        try:  # Voice braucht PyNaCl.
            import nacl  # noqa: F401
        except ImportError:
            log.warning("Musik-Feature aus: Paket 'PyNaCl' ist nicht installiert (Voice).")
            return False

        self._enabled = True
        spotify_ok = bool(self._spotify_id and self._spotify_secret)
        log.info(
            "Musik-Feature aktiv (YouTube: ja, Spotify: %s).",
            "ja" if spotify_ok else "nein - nur YouTube-Links",
        )
        return True

    def is_enabled(self):
        return self._enabled

    def _player_for(self, guild_id):
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(loop=asyncio.get_running_loop())
            self._players[guild_id] = player
        return player

    async def heal_voice(self, guild):
        """Vom bot.py-Watchdog-Loop aufgerufen: haelt die Voice-Verbindung dieses
        Servers am Leben und repariert Desyncs selbst. No-op, wenn kein Player aktiv."""
        player = self._players.get(guild.id)
        if player is not None:
            await player.heal(guild)

    def is_voice_busy(self, guild_id):
        """True, wenn die Musik den Voice-Channel dieses Servers belegt - auch in
        Songpausen, beim Tempo-Wechsel oder waehrend eines Reconnects. voicegags
        fragt das, um nicht in den Musik-Voice-Client reinzugraetschen."""
        player = self._players.get(guild_id)
        if player is None:
            return False
        if player.active_channel_id is not None:
            return True   # Bot soll in einem Kanal sein (Session laeuft) -> belegt
        return player.voice is not None and player.voice.is_connected()

    # --- yt-dlp / Spotify Helfer ---------------------------------------------

    async def _extract(self, query_or_url):
        """Loest einen YouTube-Link ODER Suchtext zu einem abspielbaren Track auf."""
        loop = asyncio.get_running_loop()

        def work():
            with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:  # type: ignore[union-attr]
                info = ydl.extract_info(query_or_url, download=False)
            if info and "entries" in info:  # Suche/Playlist -> ersten Treffer nehmen
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise ValueError("keine Treffer")
                info = entries[0]
            return info

        info = await loop.run_in_executor(None, work)
        stream_url = info.get("url")
        if not stream_url:
            raise ValueError("kein abspielbarer Stream gefunden")
        return Track(
            title=info.get("title", "Unbekannter Titel"),
            stream_url=stream_url,
            webpage_url=info.get("webpage_url", ""),
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail") or "",
        )

    def _norm_match(self, s):
        """Titel/Namen fuer den Vergleich vereinheitlichen: klein, Sonderzeichen ->
        Leerzeichen (Klammer-WOERTER bleiben erhalten, z. B. 'Faded (Sped Up)' ->
        'faded sped up'), Mehrfach-Leerzeichen zusammengefasst."""
        s = (s or "").lower()
        s = re.sub(r"[^a-z0-9äöüß]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def _pick_best_match(self, entries, want_dur, want_title, want_artist):
        """Waehlt aus YouTube-Suchtreffern den besten fuer einen bestimmten Song:
        Dauer-Naehe (starkes Signal), Titel-/Kuenstler-Treffer, Abwertung von
        Sped-Up/Cover/Live/1-Stunden-Loops. Gibt den besten Eintrag zurueck."""
        want_t = self._norm_match(want_title)
        want_a = self._norm_match(want_artist)
        best, best_score = None, -1e9
        for i, e in enumerate(entries):
            full = self._norm_match(e.get("title") or "")   # inkl. Klammer-Woerter
            score = 0.0
            if want_t and want_t in full:
                score += 45
            if want_a and want_a in full:
                score += 25
            dur = e.get("duration")
            if want_dur and dur:
                diff = abs(dur - want_dur)
                if diff <= 3:
                    score += 50
                elif diff <= 7:
                    score += 32
                elif diff <= 15:
                    score += 12
                else:
                    score -= min(60, diff)   # weit weg (Loop/Live/Sped-Up) -> raus
            for bad, pen in _YT_BAD_VARIANTS:
                # Wortgenau pruefen ('live' darf nicht in 'alive' matchen); nicht
                # abwerten, wenn der gewuenschte Titel das Wort selbst enthaelt.
                if bad not in want_t and re.search(rf"\b{re.escape(bad)}\b", full):
                    score -= pen
            score += max(0, 6 - i)           # YouTube-Ranking als leichter Tie-Break
            if score > best_score:
                best, best_score = e, score
        return best

    async def _youtube_search_best(self, query, *, want_dur=None, want_title="",
                                   want_artist=""):
        """Sucht mehrere YouTube-Treffer (flach) und liefert die Video-URL des
        besten Matches - oder None, wenn nichts brauchbar war."""
        loop = asyncio.get_running_loop()
        opts = dict(_YDL_OPTS)
        opts["noplaylist"] = True
        opts["extract_flat"] = "in_playlist"

        def work():
            with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(
                    f"ytsearch{_SPOTIFY_SEARCH_N}:{query}", download=False)

        try:
            info = await loop.run_in_executor(None, work)
        except Exception as exc:  # noqa: BLE001 - yt-dlp wirft viele Fehlerarten
            log.warning("YouTube-Best-Match-Suche fehlgeschlagen (%s): %s", query, exc)
            return None
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        best = self._pick_best_match(entries, want_dur, want_title, want_artist)
        if best is None:
            return None
        vid = best.get("url") or best.get("id")
        if vid and not str(vid).startswith("http"):
            vid = f"https://www.youtube.com/watch?v={vid}"
        return vid

    async def _resolve_input(self, extract_input, hint):
        """Loest eine yt-dlp-Eingabe zu einem Track auf. Mit 'hint' (Spotify-Meta:
        query/dur/title/artist) wird der beste YouTube-Treffer per Dauer/Titel
        gewaehlt statt blind der erste; scheitert das, Fallback auf extract_input."""
        if hint and hint.get("query"):
            try:
                vid = await self._youtube_search_best(
                    hint["query"], want_dur=hint.get("dur"),
                    want_title=hint.get("title", ""), want_artist=hint.get("artist", ""))
            except Exception:  # noqa: BLE001 - nie den Song wegen Matching verlieren
                log.exception("Best-Match fehlgeschlagen - nutze ersten Treffer")
                vid = None
            if vid:
                return await self._extract(vid)
        return await self._extract(extract_input)

    async def _resolve_track(self, track):
        """Loest einen vorgemerkten Track auf. track.query = komplette yt-dlp-Eingabe
        (direkte URL ODER 'ytsearch1:Kuenstler - Titel'); track.match_hint bringt bei
        Spotify-Songs die Metadaten fuer die Best-Match-Auswahl mit."""
        resolved = await self._resolve_input(track.query, track.match_hint)
        resolved.requested_by = track.requested_by
        resolved.query = track.query
        return resolved

    def _lazy_track(self, extract_input, title, requested_by, hint=None):
        """Noch nicht aufgeloester Track (wird erst beim Abspielen geladen).
        extract_input = yt-dlp-Eingabe (URL oder 'ytsearch1:...'), title = Anzeigename,
        hint = optionale Spotify-Metadaten fuer die Best-Match-Auswahl."""
        return Track(
            title=title, stream_url="", query=extract_input, requested_by=requested_by,
            match_hint=hint,
        )

    async def _youtube_playlist(self, url):
        """YouTube-Playlist -> Liste (video_url, titel). Schnell via extract_flat;
        die einzelnen Videos werden erst beim Abspielen aufgeloest."""
        loop = asyncio.get_running_loop()
        opts = dict(_YDL_OPTS)
        opts["noplaylist"] = False
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = MAX_QUEUE
        opts["ignoreerrors"] = True  # einzelne kaputte Videos ueberspringen, nicht crashen

        def work():
            with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=False)

        try:
            info = await loop.run_in_executor(None, work)
        except Exception as exc:  # noqa: BLE001
            log.warning("YouTube-Playlist nicht ladbar (%s): %s", url, exc)
            return None

        entries = info.get("entries") if info else None
        if not entries:
            return None
        out = []
        for e in entries:
            if not e:
                continue
            vid = e.get("url") or e.get("id")
            if not vid:
                continue
            if not str(vid).startswith("http"):
                vid = f"https://www.youtube.com/watch?v={vid}"
            out.append((vid, e.get("title", "Unbekannter Titel")))
        return out or None

    async def _spotify_token(self):
        """Holt (und cached) ein Spotify-App-Token (Client-Credentials-Flow)."""
        if not (self._spotify_id and self._spotify_secret):
            return ""
        now = time.time()
        if self._sp_token["value"] and self._sp_token["exp"] > now + 30:
            return self._sp_token["value"]  # type: ignore[return-value]

        auth = base64.b64encode(f"{self._spotify_id}:{self._spotify_secret}".encode()).decode()
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(
                    "https://accounts.spotify.com/api/token",
                    data={"grant_type": "client_credentials"},
                    headers={"Authorization": f"Basic {auth}"},
                ) as r:
                    if r.status != 200:
                        log.error("Spotify-Token fehlgeschlagen (HTTP %s).", r.status)
                        return ""
                    data = await r.json()
        except (aiohttp.ClientError, OSError) as exc:
            log.error("Spotify nicht erreichbar: %s", exc)
            return ""

        self._sp_token["value"] = data.get("access_token", "")
        self._sp_token["exp"] = now + float(data.get("expires_in", 3600))
        return self._sp_token["value"]  # type: ignore[return-value]

    async def _spotify_track_meta(self, url):
        """Spotify-Track-Link -> Metadaten fuer die YouTube-Suche:
        {query, name, artist, dur}. 'query' = 'Kuenstler - Titel', 'artist' = der
        HAUPT-Kuenstler, 'dur' = Laenge in Sekunden (fuer den Dauer-Match).
        None, wenn der Link/Token nicht aufloesbar ist."""
        m = _SPOTIFY_TRACK_RE.search(url)
        if not m:
            return None
        token = await self._spotify_token()
        if not token:
            return None
        track_id = m.group(1)
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(
                    f"https://api.spotify.com/v1/tracks/{track_id}",
                    headers={"Authorization": f"Bearer {token}"},
                ) as r:
                    if r.status != 200:
                        log.error("Spotify-Track-Abruf fehlgeschlagen (HTTP %s).", r.status)
                        return None
                    data = await r.json()
        except (aiohttp.ClientError, OSError) as exc:
            log.error("Spotify nicht erreichbar: %s", exc)
            return None

        name = (data.get("name") or "").strip()
        alle = [a.get("name", "") for a in data.get("artists", []) if a.get("name")]
        haupt = alle[0] if alle else ""
        if not name:
            return None
        dur_ms = data.get("duration_ms")
        dur = int(round(dur_ms / 1000)) if isinstance(dur_ms, (int, float)) else None
        # Suchanfrage: Haupt-Kuenstler + Titel (ohne Kommas) trifft die YouTube-
        # Suche zuverlaessiger als eine lange Kuenstlerliste.
        query = f"{haupt} {name}".strip() or name
        return {"query": query, "name": name, "artist": haupt, "dur": dur,
                "artists": ", ".join(alle)}

    async def _spotify_to_query(self, url):
        """Spotify-Track-Link -> 'Kuenstler - Titel' (Kompatibilitaets-Wrapper)."""
        meta = await self._spotify_track_meta(url)
        if not meta:
            return None
        arts = meta.get("artists") or meta.get("artist") or ""
        return f"{arts} - {meta['name']}".strip(" -") or None

    async def _spotify_list_tracks(self, url):
        """Spotify-Playlist-/Album-Link -> Liste Metadaten-Dicts
        {query, name, artist, dur, display} (max. MAX_QUEUE). 'dur' erlaubt beim
        Abspielen die Dauer-genaue YouTube-Auswahl (kein Sped-Up/Loop)."""
        m = _SPOTIFY_LIST_RE.search(url)
        if not m:
            return None
        kind = (m.group(1) or m.group(2) or "").lower()
        list_id = m.group(3)
        token = await self._spotify_token()
        if not token:
            return None

        if kind == "playlist":
            next_url = (
                f"https://api.spotify.com/v1/playlists/{list_id}/tracks"
                "?limit=100&fields=items(track(name,artists(name),duration_ms)),next"
            )
        else:  # album
            next_url = f"https://api.spotify.com/v1/albums/{list_id}/tracks?limit=50"

        tracks = []
        headers = {"Authorization": f"Bearer {token}"}
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                while next_url and len(tracks) < MAX_QUEUE:
                    async with s.get(next_url, headers=headers) as r:
                        if r.status != 200:
                            log.error("Spotify-%s-Abruf fehlgeschlagen (HTTP %s).", kind, r.status)
                            break
                        data = await r.json()
                    for item in data.get("items", []):
                        tr = item.get("track") if kind == "playlist" else item
                        if not tr:
                            continue
                        name = (tr.get("name") or "").strip()
                        if not name:
                            continue
                        alle = [a.get("name", "") for a in tr.get("artists", []) if a.get("name")]
                        haupt = alle[0] if alle else ""
                        dms = tr.get("duration_ms")
                        dur = int(round(dms / 1000)) if isinstance(dms, (int, float)) else None
                        tracks.append({
                            "query": f"{haupt} {name}".strip() or name,
                            "name": name, "artist": haupt, "dur": dur,
                            "display": f"{', '.join(alle)} - {name}".strip(" -") or name,
                        })
                    next_url = data.get("next")
        except (aiohttp.ClientError, OSError) as exc:
            log.error("Spotify nicht erreichbar: %s", exc)
            return None
        return tracks

    def _deep_find(self, obj, key):
        """Sucht rekursiv den ersten Wert zu 'key' in verschachtelten dict/list."""
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for value in obj.values():
                found = self._deep_find(value, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj:
                found = self._deep_find(value, key)
                if found is not None:
                    return found
        return None

    async def _spotify_playlist_via_embed(self, url):
        """Spotify-Playlist -> Liste 'Kuenstler - Titel' ueber das oeffentliche Embed.

        Die Web-API verbietet Client-Credentials-Apps den Playlist-Track-Zugriff
        (HTTP 403). Das Embed (open.spotify.com/embed/playlist/<id>) liefert die
        Songliste dagegen ohne Login im __NEXT_DATA__-JSON.
        """
        m = _SPOTIFY_LIST_RE.search(url)
        if not m:
            return None
        list_id = m.group(3)
        embed_url = f"https://open.spotify.com/embed/playlist/{list_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept-Language": "de,en;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(embed_url, headers=headers) as r:
                    if r.status != 200:
                        log.error(
                            "Spotify-Playlist-Embed fehlgeschlagen (HTTP %s).", r.status
                        )
                        return None
                    html = await r.text()
        except (aiohttp.ClientError, OSError) as exc:
            log.error("Spotify-Embed nicht erreichbar: %s", exc)
            return None

        m2 = _NEXT_DATA_RE.search(html)
        if not m2:
            log.error("Spotify-Embed: __NEXT_DATA__ nicht gefunden (Struktur geaendert?).")
            return None
        try:
            data = json.loads(m2.group(1))
        except json.JSONDecodeError as exc:
            log.error("Spotify-Embed: JSON nicht lesbar (%s).", exc)
            return None

        track_list = self._deep_find(data, "trackList")
        if not isinstance(track_list, list) or not track_list:
            log.error("Spotify-Embed: keine Songliste im JSON gefunden.")
            return None

        queries = []
        for entry in track_list:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            artist = str(entry.get("subtitle") or "").strip()
            query = f"{artist} - {title}".strip(" -")
            if query:
                queries.append(query)
            if len(queries) >= MAX_QUEUE:
                break
        return queries or None

    # --- Befehls-Erkennung ---------------------------------------------------

    def _clean_lead(self, text):
        """Entfernt @-Mentions und den fuehrenden Botnamen/Alias ('Florian, spiel ...'
        -> 'spiel ...'). Zentral in ai.strip_lead, damit alle Module gleich reagieren
        (so gehen Musik-Befehle auch mit dem Alias 'Florian', nicht nur 'Flo')."""
        return ai.strip_lead(text)

    def parse_command(self, text):
        """Erkennt einen Musik-Befehl. Rueckgabe: (aktion, argument) oder None.

        Aktionen: play, search, spotify_album, spotify_playlist, yt_playlist,
                  volume, skip, pause, resume, stop, leave, queue.
        """
        # 1) Link in der Nachricht? (staerkstes Signal)
        for url in _URL_RE.findall(text):
            low = url.lower()
            m = _SPOTIFY_LIST_RE.search(url)
            if m:
                kind = (m.group(1) or m.group(2) or "").lower()
                return ("spotify_album" if kind == "album" else "spotify_playlist", url)
            if "youtube.com" in low or "youtu.be" in low:
                # Echte Playlist abspielen - auch wenn ein einzelnes Video dabei steht
                # (Teilen aus einer Playlist liefert watch?v=...&list=...). Nur Auto-Mixe
                # (list=RD...) ignorieren wir und spielen das einzelne Video.
                lm = _YT_LIST_RE.search(url)
                if lm and not lm.group(1).upper().startswith("RD"):
                    return ("yt_playlist", url)
                return ("play", url)
            if _SPOTIFY_TRACK_RE.search(url):
                return ("play", url)

        cleaned = self._clean_lead(text)
        if not cleaned:
            return None

        # 2a) Wiederholen? (vor der Freitext-Suche, sonst wuerde "spiel nochmal"
        #     als Suche nach "nochmal" gedeutet.)
        rm = _REPLAY_RE.match(cleaned)
        if rm:
            return ("replay", rm.group(1) or "1")

        # 2) Steuerbefehl am Satzanfang?
        for action, pattern in _CONTROL:
            if pattern.match(cleaned):
                return (action, "")

        # 3) Lautstaerke? Relativ (lauter/leiser) oder absolut ("ls 30", "vol 80",
        #    Tippfehler ...). Ohne Zahl -> aktuelle Lautstaerke anzeigen ("?").
        if _VOLUME_UP_RE.match(cleaned):
            return ("volume", "+")
        if _VOLUME_DOWN_RE.match(cleaned):
            return ("volume", "-")
        vm = _VOLUME_ARG_RE.match(cleaned)
        if vm and self._is_volume_word(vm.group(1)):
            return ("volume", vm.group(2) or "?")

        # 3b) "random" / "zufall" / "überrasch mich" -> Genre-Auswahl per Dropdown.
        #     (vor der Freitext-Suche, sonst wuerde nach "random" gesucht.)
        if _RANDOM_RE.match(cleaned):
            return ("random", "")

        # 3c) "lyrics [song]" / "songtext [song]" -> Songtext (aktueller Song oder
        #     genannter Titel). Vor der Freitext-Suche, sonst wird danach gesucht.
        lm = _LYRICS_RE.match(cleaned)
        if lm:
            return ("lyrics", (lm.group(1) or "").strip())

        # 4a) "mach die musik aus" / "stell die mucke ab" -> stoppen.
        if _NAT_STOP_RE.match(cleaned):
            return ("stop", "")

        # 4b) Natuerlichsprachig: "mach mal <X> an", "leg <X> auf", "hau <X> raus",
        #     "kannst du <X> spielen" ... -> wie ein Play-Befehl behandeln. Steht kein
        #     konkreter Song da ("mach mal musik an"), fortsetzen/Hinweis geben.
        for pat in _NAT_PLAY_RES:
            nm = pat.match(cleaned)
            if nm:
                q = nm.group(1).strip()
                bare = _NAT_ARTICLE_RE.sub("", q).strip().lower()
                if not bare or bare in _NAT_GENERIC:
                    return ("resume_or_hint", "")
                # Spielt auf ein anderes Feature an (Spiel/Casino/Shop ...) -> nicht
                # als Song deuten, damit der echte Handler bzw. die KI drankommt.
                if bare.split()[0] in _NAT_NOT_A_SONG:
                    return None
                return ("search", q)

        # 4) "spiel <suchbegriff>" ohne Link -> YouTube-Suche
        m = _PLAY_TEXT_RE.match(cleaned)
        if m:
            return ("search", m.group(1).strip())

        return None

    async def start_random(self, interaction, genre_key):
        """Spielt aus einer Genre-Auswahl (Dropdown) heraus einen zufaelligen Song.
        'genre_key' ist ein Schluessel aus _RANDOM_GENRES oder 'surprise' (Genre
        wird dann selbst zufaellig gezogen). Antwortet ueber die Interaction."""
        if not self._enabled or interaction.guild is None:
            await interaction.response.send_message("Musik ist gerade aus.", ephemeral=True)
            return
        key = random.choice(list(_RANDOM_GENRES)) if genre_key == "surprise" else genre_key
        genre = _RANDOM_GENRES.get(key)
        if genre is None:
            await interaction.response.send_message("Dieses Genre kenne ich nicht. 🤔",
                                                    ephemeral=True)
            return
        label, emoji, pool = genre
        query = random.choice(pool)

        # Der Klickende muss selbst im Sprachkanal sein.
        voice_state = getattr(interaction.user, "voice", None)
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Geh erst in einen Sprachkanal, dann leg ich los. 🎧", ephemeral=True)
            return

        # Aufloesen + Connect kann laenger als Discords 3s-Frist dauern -> defer.
        await interaction.response.defer()
        player = self._player_for(interaction.guild.id)
        player.text_channel = interaction.channel
        try:
            track = await self._extract(f"ytsearch1:{query}")
        except Exception:  # noqa: BLE001 - yt-dlp wirft viele verschiedene Fehler
            log.exception("Random-Track nicht aufloesbar: %s", query)
            await interaction.followup.send(embed=self._embed(
                "Den Zufalls-Song konnte ich gerade nicht laden – probier's nochmal. 🎲",
                color=_COL_ERR))
            return
        track.requested_by = interaction.user.display_name
        try:
            await player.connect(voice_state.channel)
        except (discord.ClientException, RuntimeError) as exc:
            log.error("Random-Connect fehlgeschlagen: %s", exc)
            await interaction.followup.send(embed=self._embed(
                "Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                color=_COL_ERR))
            return

        # Auswahl-Menue zur Bestaetigung umschreiben (Dropdown weg).
        try:
            await interaction.edit_original_response(
                embed=self._embed(
                    f"**{emoji} {label}** – ich hab **{self._short(track.title, 80)}** "
                    "rausgekramt. Viel Spaß! 🎶",
                    title="🎲  Zufalls-Song", color=_COL_PLAY),
                view=None)
        except discord.HTTPException:
            pass

        # Laeuft schon was? -> einreihen, sonst starten + Panel posten.
        if player.is_active():
            player.queue.append(track)
            await interaction.followup.send(
                embed=self._added_embed(track, len(player.queue), len(player.queue)))
            return
        try:
            player.start(track)
        except Exception:  # noqa: BLE001
            log.exception("Random-Track nicht abspielbar: %s", track.title)
            await interaction.followup.send(embed=self._embed(
                "Den Song konnte ich gerade nicht abspielen – zieh nochmal. 🎲",
                color=_COL_ERR))
            return
        await self._send_panel(player, track)

    # --- Songtext (Lyrics) ------------------------------------------------
    def _split_artist_title(self, raw):
        """Zerlegt einen (YouTube-)Titel bestmoeglich in (Kuenstler, Titel).
        Entfernt Deko wie '(Official Video)', '[HD]', 'feat. ...' und splittet am
        ersten ' - '. Ohne Trenner: Kuenstler leer, alles ist der Titel."""
        s = raw or ""
        s = re.sub(r"\[[^\]]*\]", " ", s)          # [Official Video]
        s = re.sub(r"\([^)]*\)", " ", s)           # (Official Audio) / (Lyrics)
        s = s.split("|")[0]                         # "Song | Label" -> "Song"
        s = re.sub(r"\b(?:feat\.?|ft\.?|featuring|prod\.?)\b.*$", "", s, flags=re.I)
        s = _LYRICS_NOISE_RE.sub(" ", s)
        s = re.sub(r"\s+", " ", s).strip(" -–—\"'“”„")
        for sep in (" - ", " – ", " — ", "–", "—"):
            if sep in s:
                artist, title = s.split(sep, 1)
                return artist.strip(" -–—\"'“”„"), title.strip(" -–—\"'“”„")
        return "", s.strip()

    async def fetch_lyrics(self, artist, title):
        """Holt den Songtext von der kostenlosen lyrics.ovh-API (kein Key noetig).
        Rueckgabe: Text (str) oder None, wenn nichts gefunden/erreichbar."""
        if not title:
            return None
        url = (f"{_LYRICS_API}/{urllib.parse.quote(artist.strip())}/"
               f"{urllib.parse.quote(title.strip())}")
        try:
            session = ai.http_session()
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError):
            log.warning("Lyrics-Abruf fehlgeschlagen: %s - %s", artist, title)
            return None
        lyr = (data or {}).get("lyrics") or ""
        lyr = lyr.replace("\r\n", "\n").replace("\r", "\n").strip()
        return lyr or None

    def _lyrics_pages(self, text, limit = 3800):
        """Zerlegt den Text in lesbare Seiten: bricht bevorzugt an Strophen
        (Leerzeilen), zu grosse Strophen notfalls an Zeilen. Max 'limit' Zeichen
        je Seite (unter Discords 4096er-Embed-Limit)."""
        text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
        pages, cur = [], ""

        def flush():
            nonlocal cur
            if cur.strip():
                pages.append(cur.strip())
            cur = ""

        for stanza in text.split("\n\n"):
            stanza = stanza.strip()
            if not stanza:
                continue
            if len(stanza) > limit:                 # Riesen-Strophe -> zeilenweise
                for line in stanza.split("\n"):
                    if len(cur) + len(line) + 1 > limit:
                        flush()
                    cur += line + "\n"
                cur += "\n"
                continue
            if len(cur) + len(stanza) + 2 > limit:
                flush()
            cur += stanza + "\n\n"
        flush()
        return pages or ["_(Kein Text gefunden.)_"]

    def _lyrics_embed(self, artist, title, page_text, page_idx, total, thumb):
        """Baut das huebsche Lyrics-Embed fuer eine Seite."""
        kopf = f"{artist} – {title}" if artist else (title or "Songtext")
        emb = self._embed(page_text, title=f"🎤  {self._short(kopf, 240)}", color=_COL_PLAY)
        if thumb:
            try:
                emb.set_thumbnail(url=thumb)
            except Exception:  # noqa: BLE001 - Thumbnail ist nur Deko
                pass
        quelle = "Quelle: lyrics.ovh"
        emb.set_footer(text=f"Seite {page_idx + 1}/{total}  ·  {quelle}"
                       if total > 1 else quelle)
        return emb

    async def _build_lyrics(self, raw_title, thumbnail = None):
        """Ermittelt Kuenstler/Titel aus 'raw_title', holt den Text und baut
        (Embed, LyricsView). View ist None, wenn kein Text gefunden wurde."""
        artist, title = self._split_artist_title(raw_title)
        lyr = await self.fetch_lyrics(artist, title)
        if lyr is None and artist:
            # Manche YT-Titel sind 'Titel - Kuenstler' -> einmal vertauscht probieren.
            lyr = await self.fetch_lyrics(title, artist)
            if lyr is not None:
                artist, title = title, artist
        if lyr is None:
            kopf = f"{artist} – {title}" if artist else (title or raw_title)
            return (self._embed(
                f"Für **{self._short(kopf, 200)}** hab ich online keinen Songtext "
                f"gefunden. 😕\nTipp: `{self._bot_name} lyrics Künstler - Titel` "
                "klappt am zuverlässigsten.",
                title="🎤  Kein Text gefunden", color=_COL_ERR), None)
        pages = self._lyrics_pages(lyr)
        view = LyricsView(pages, artist, title, thumbnail)
        return (view.embed(), view)

    def _unpack_item(self, item):
        """Ein _play_many-Item ist (yt-dlp-Eingabe, Titel) ODER
        (yt-dlp-Eingabe, Titel, Match-Hint). Liefert immer (inp, titel, hint)."""
        inp, title, *rest = item
        return inp, title, (rest[0] if rest else None)

    async def _play_many(
        self,
        player,
        channel,
        items,
        requested_by,
        label,
        reply_to = None,
    ):
        """Spielt mehrere Songs: ersten sofort, Rest lazy in die Warteschlange.

        items = Liste (yt-dlp-Eingabe, Anzeigetitel[, Match-Hint]),
        label z. B. 'aus dem Album'.
        Rueckgabe: Embed (eingereiht/Fehler) ODER HANDLED (frisch gestartet -> Panel).
        """
        try:
            await player.connect(channel)
        except discord.ClientException as exc:
            log.error("Voice-Connect fehlgeschlagen: %s", exc)
            return self._embed("Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                               color=_COL_ERR)

        space = MAX_QUEUE - len(player.queue)
        if space <= 0:
            return self._embed(f"Die Warteschlange ist voll ({MAX_QUEUE}). Warte kurz.", color=_COL_ERR)
        items = items[:space]

        if player.is_active():
            for item in items:
                inp, title, hint = self._unpack_item(item)
                player.queue.append(self._lazy_track(inp, title, requested_by, hint))
            return self._embed(
                f"**{len(items)}** Songs {label} eingereiht – ab **#{len(player.queue) - len(items) + 1}** "
                f"in der Warteschlange.",
                title="➕  Zur Warteschlange hinzugefügt", color=_COL_QUEUE,
            )

        first_inp, _first_title, first_hint = self._unpack_item(items[0])
        rest = items[1:]
        try:
            track = await self._resolve_input(first_inp, first_hint)
        except Exception:  # noqa: BLE001
            log.exception("Erster Track nicht ladbar: %s", first_inp)
            return self._embed("Den ersten Song konnte ich nicht laden.", color=_COL_ERR)
        track.requested_by = requested_by
        track.query = first_inp
        track.match_hint = first_hint
        for item in rest:
            inp, title, hint = self._unpack_item(item)
            player.queue.append(self._lazy_track(inp, title, requested_by, hint))
        try:
            player.start(track)
        except Exception:
            log.exception("Erster Track (Mehrfach) nicht abspielbar: %s", track.title)
            return self._embed("Den ersten Song konnte ich gerade nicht abspielen.", color=_COL_ERR)
        extra = f"+{len(rest)} weitere {label}" if rest else ""
        await self._send_panel(player, track, reply_to=reply_to, extra=extra)
        return HANDLED

    # --- Optik: groessere Embeds ---------------------------------------------

    def _title_value(self, track):
        """Titel als Link (falls webpage_url bekannt), sonst fett."""
        if track.webpage_url:
            return f"**[{self._short(track.title, 90)}]({track.webpage_url})**"
        return f"**{self._short(track.title, 90)}**"

    def _now_playing_embed(self, track, queue_len = 0, extra = "",
                           speed = 1.0):
        """Schoenes 'Jetzt laeuft'-Embed mit Dauer, Wunsch-Person und Thumbnail."""
        e = discord.Embed(title=NOWPLAYING_EMBED_TITLE, description=self._title_value(track),
                          color=_COL_PLAY)
        dur = self._fmt_dur(track.duration)
        if dur:
            e.add_field(name="Länge", value=f"`{dur}`", inline=True)
        if track.requested_by:
            e.add_field(name="Gewünscht von", value=track.requested_by, inline=True)
        if queue_len > 0:
            e.add_field(name="In der Schlange", value=f"{queue_len} Song(s)", inline=True)
        if abs(speed - 1.0) > 1e-3:
            if speed < 1.0:
                e.add_field(name="Effekt", value=f"🌌 `{speed:g}×` slowed + reverb", inline=True)
            else:
                e.add_field(name="Tempo", value=f"🚀 `{speed:g}×`", inline=True)
        # Fussnote: optionaler Extra-Text und (falls aktiv) die Tempo-/Effekt-Anzeige.
        foot = []
        if extra:
            foot.append(extra)
        if speed < 1.0 - 1e-3:
            foot.append(f"🌌 Slowed + Reverb aktiv ({speed:g}×)")
        elif speed > 1.0 + 1e-3:
            foot.append(f"🎚️ Tempo {speed:g}× aktiv")
        if foot:
            e.set_footer(text="  ·  ".join(foot))
        else:
            e.set_footer(text="🎚️ Tempo & Effekte: Menü unter den Buttons")
        if track.thumbnail:
            e.set_thumbnail(url=track.thumbnail)
        return e

    def _added_embed(self, track, position, total, *,
                    title = "➕  Zur Warteschlange hinzugefügt",
                    footer = None):
        """Embed fuer einen frisch eingereihten Song."""
        e = discord.Embed(title=title, description=self._title_value(track), color=_COL_QUEUE)
        e.add_field(name="Position", value=f"**#{position}** von {total}", inline=True)
        dur = self._fmt_dur(track.duration)
        if dur:
            e.add_field(name="Länge", value=f"`{dur}`", inline=True)
        if track.requested_by:
            e.add_field(name="Von", value=track.requested_by, inline=True)
        if footer:
            e.set_footer(text=footer)
        if track.thumbnail:
            e.set_thumbnail(url=track.thumbnail)
        return e

    def _gone_embed(self, track):
        return self._embed(f"**{self._short(track.title, 90)}** ist nicht mehr in der Warteschlange.",
                           title="⌛  Schon durch", color=_COL_INFO)

    def _queue_embed(self, player):
        """Uebersichtliche Warteschlange: aktueller Song + naechste 10."""
        e = discord.Embed(title="🎶  Warteschlange", color=_COL_QUEUE)
        if player.current:
            dur = self._fmt_dur(player.current.duration)
            cur = f"**{self._short(player.current.title, 80)}**"
            if dur:
                cur += f"  ·  `{dur}`"
            e.add_field(name="▶️  Jetzt", value=cur, inline=False)
        if player.queue:
            lines = []
            for i, t in enumerate(player.queue[:10], start=1):
                dur = self._fmt_dur(t.duration)
                line = f"`{i:>2}.`  {self._short(t.title, 55)}"
                if dur:
                    line += f"  ·  `{dur}`"
                lines.append(line)
            more = len(player.queue) - 10
            if more > 0:
                lines.append(f"…und **{more}** weitere")
            e.add_field(name=f"⬆️  Als Nächstes  ({len(player.queue)})",
                        value="\n".join(lines), inline=False)
        else:
            e.set_footer(text="Keine weiteren Songs – wirf was rein!")
        if player.current and player.current.thumbnail:
            e.set_thumbnail(url=player.current.thumbnail)
        return e

    async def _retire_panel(self, player):
        """Loescht das zuletzt gepostete Steuer-Panel selbst - der Song dazu ist vorbei
        bzw. wird gleich durch ein neues ersetzt. Das AKTUELLE Panel ist beim Auto-
        Loeschen ausgenommen (bot.py, ueber NOWPLAYING_EMBED_TITLE); alte raeumen wir
        hier sofort weg, damit nichts liegen bleibt."""
        msg = player.panel_message
        player.panel_message = None
        if msg is not None:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    async def _send_panel(self, player, track, *,
                         reply_to = None, extra = ""):
        """Postet ein 'Jetzt laeuft'-Panel mit Steuer-Buttons (altes wird geloescht).
        Das Panel traegt NOWPLAYING_EMBED_TITLE - bot.py haelt solche Bot-Nachrichten
        vom Auto-Loeschen frei, damit die Buttons den ganzen Song erreichbar bleiben."""
        await self._retire_panel(player)
        emb = self._now_playing_embed(track, len(player.queue), extra=extra, speed=player.speed)
        view = PlaybackControlView(player)
        try:
            if reply_to is not None:
                msg = await reply_to.reply(embed=emb, view=view, mention_author=False)
            elif player.text_channel is not None:
                msg = await player.text_channel.send(embed=emb, view=view)
            else:
                return
        except discord.HTTPException as exc:
            log.error("Now-Playing-Panel fehlgeschlagen: %s", exc)
            return
        view.message = msg
        player.panel_message = msg

    # --- Oeffentlicher Einstieg ----------------------------------------------

    async def handle(self, message):
        """Prueft, ob die Nachricht ein Musik-Befehl ist, und fuehrt ihn aus.

        Rueckgabe:
        - discord.Embed -> es war ein Musik-Befehl; bot.py schickt das Embed.
        - HANDLED        -> das Modul hat selbst geantwortet (Embed + Buttons).
        - None           -> kein Musik-Befehl; die KI soll uebernehmen.
        """
        if not self._enabled or message.guild is None:
            return None

        cmd = self.parse_command(message.content or "")
        if cmd is None:
            return None
        action, arg = cmd
        player = self._player_for(message.guild.id)
        player.text_channel = message.channel

        # --- Wiederholen: den (N-t-)letzten Song aus dem Verlauf erneut spielen ---
        if action == "replay":
            try:
                idx = max(1, int(arg))
            except (TypeError, ValueError):
                idx = 1
            if idx > len(player.history):
                if not player.history:
                    return self._embed("Ich hab noch keinen Song im Verlauf. Spiel erst was! 🎵",
                                       color=_COL_ERR)
                return self._embed(f"So weit reicht mein Verlauf nicht – ich kenne die letzten "
                                   f"**{len(player.history)}** Songs.", color=_COL_ERR)
            want = player.history[-idx]
            again = want.webpage_url or want.query or want.title
            if not again:
                return self._embed("Diesen Song kann ich leider nicht nochmal laden.", color=_COL_ERR)
            # Wie ein normaler Play-Befehl weiterbehandeln.
            action, arg = "play", again

        # --- Steuerbefehle, die keine Voice-Verbindung voraussetzen ---
        if action == "volume":
            cur = int(round(player.volume * 100))
            if arg == "?":
                bar = "🔉" if cur < 50 else ("🔊" if cur <= 100 else "📢")
                return self._embed(
                    f"Lautstärke steht aktuell auf **{cur}%**.\n"
                    f"Ändern z. B. mit `flo ls 50`, `flo lauter` oder `flo leiser`.",
                    title=f"{bar}  Lautstärke", color=_COL_CTRL)
            if arg == "+":
                new = min(200, cur + 20)
            elif arg == "-":
                new = max(0, cur - 20)
            else:
                new = max(0, min(200, int(arg)))
            player.volume = new / 100
            if player.voice is not None and isinstance(
                player.voice.source, discord.PCMVolumeTransformer
            ):
                player.voice.source.volume = player.volume  # live anwenden
            bar = "🔉" if new < 50 else ("🔊" if new <= 100 else "📢")
            return self._embed(f"Lautstärke steht jetzt auf **{new}%**.",
                               title=f"{bar}  Lautstärke", color=_COL_CTRL)

        if action in ("stop", "leave"):
            if player.voice is None or not player.voice.is_connected():
                return self._embed("Ich bin gerade in keinem Sprachkanal.", color=_COL_ERR)
            await player.disconnect()
            return self._embed("Musik gestoppt, Warteschlange geleert und raus aus dem Sprachkanal.",
                               title="⏹️  Gestoppt", color=_COL_INFO)

        if action == "skip":
            if not player.is_active():
                return self._embed("Ich spiele gerade nichts.", color=_COL_ERR)
            skipped = player.current.title if player.current else ""
            player.voice.stop()  # type: ignore[union-attr]  -> loest _after -> naechster Track
            desc = f"**{self._short(skipped, 90)}** übersprungen." if skipped else "Übersprungen."
            return self._embed(desc, title="⏭️  Skip", color=_COL_CTRL)

        if action == "pause":
            if player.voice is None or not player.voice.is_playing():
                return self._embed("Ich spiele gerade nichts.", color=_COL_ERR)
            player.voice.pause()
            return self._embed(f"Pausiert. Sag `{self._bot_name} weiter`, wenn's weitergehen soll.",
                               title="⏸️  Pause", color=_COL_CTRL)

        if action == "resume":
            if player.voice is None or not player.voice.is_paused():
                return self._embed("Da ist nichts pausiert.", color=_COL_ERR)
            player.voice.resume()
            return self._embed("Weiter geht's.", title="▶️  Fortgesetzt", color=_COL_PLAY)

        # "mach mal Musik an" ohne konkreten Song: pausiert -> weiter, laeuft schon ->
        # kurzer Hinweis, sonst freundlich nach dem Wunsch-Song fragen.
        if action == "resume_or_hint":
            if player.voice is not None and player.voice.is_paused():
                player.voice.resume()
                return self._embed("Weiter geht's.", title="▶️  Fortgesetzt", color=_COL_PLAY)
            if player.is_active():
                return self._embed("Läuft doch schon. 🎶", color=_COL_INFO)
            return self._embed(
                f"Klar – was soll ich spielen? Sag z. B. `{self._bot_name} mach mal "
                f"Bohemian Rhapsody an` oder `{self._bot_name} spiel <Song/Link>`.",
                title="🎵  Was denn?", color=_COL_QUEUE)

        if action == "queue":
            if not player.current and not player.queue:
                return self._embed("Die Warteschlange ist leer – wirf was rein!",
                                   title="🎶  Warteschlange", color=_COL_INFO)
            return self._queue_embed(player)

        # "random"/"zufall"/"überrasch mich" -> Genre-Dropdown, danach Zufalls-Song.
        if action == "random":
            view = RandomGenreView(message.author.id)
            emb = self._embed(
                "Bock auf Zufall? 🎲 Wähl unten dein **Genre** – ich kram dir einen "
                "Song raus und leg ihn im Voice auf.\n_(Du musst dafür in einem "
                "Sprachkanal sein.)_",
                title="🎲  Zufalls-Song", color=_COL_QUEUE)
            try:
                view.message = await message.reply(embed=emb, view=view, mention_author=False)
            except discord.HTTPException as exc:
                log.error("Random-Menü konnte nicht gesendet werden: %s", exc)
                return self._embed("Das Zufalls-Menü ging gerade nicht auf.", color=_COL_ERR)
            return HANDLED

        # "lyrics [song]" -> Songtext des aktuellen Songs oder eines genannten Titels.
        if action == "lyrics":
            raw = arg.strip() if arg else ""
            thumb = None
            if not raw:
                if player.current is None:
                    return self._embed(
                        f"Gerade läuft nichts. Sag `{self._bot_name} lyrics "
                        "<Künstler - Titel>` oder starte erst einen Song.",
                        title="🎤  Lyrics", color=_COL_ERR)
                raw = player.current.title
                thumb = getattr(player.current, "thumbnail", "") or None
            async with message.channel.typing():
                emb, lview = await self._build_lyrics(raw, thumb)
            kwargs = {"embed": emb, "mention_author": False}
            if lview is not None:
                kwargs["view"] = lview
            try:
                msg = await message.reply(**kwargs)
            except discord.HTTPException:
                log.exception("Lyrics senden fehlgeschlagen")
                return HANDLED
            if lview is not None:
                lview.message = msg
            return HANDLED

        if action == "join":
            # Nur in den Sprachkanal kommen (ohne etwas abzuspielen).
            voice_state = getattr(message.author, "voice", None)
            if voice_state is None or voice_state.channel is None:
                return self._embed("Geh erst in einen Sprachkanal, dann komme ich dazu.", color=_COL_ERR)
            try:
                await player.connect(voice_state.channel)
            except RuntimeError as exc:  # discord.py >= 2.7 ohne davey
                log.error("Voice nicht moeglich (join): %s", exc)
                return self._embed("Voice ist hier gerade nicht eingerichtet "
                                   "(auf dem Server fehlt vermutlich `davey`).", color=_COL_ERR)
            except discord.ClientException as exc:
                log.error("Voice-Connect (join) fehlgeschlagen: %s", exc)
                return self._embed("Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                                   color=_COL_ERR)
            return self._embed(f"Bin da in **{voice_state.channel.name}**. "
                               f"Sag z. B. `{self._bot_name} spiel <song>`.",
                               title="👋  Eingeklinkt", color=_COL_PLAY)

        # --- Abspielen: Nutzer muss im Sprachkanal sein ---
        voice_state = getattr(message.author, "voice", None)
        if voice_state is None or voice_state.channel is None:
            return self._embed("Geh erst in einen Sprachkanal, dann spiele ich dort.", color=_COL_ERR)

        # --- Mehrere Songs auf einmal (Spotify-Album / YouTube-Playlist) ---
        if action == "spotify_album":
            metas = await self._spotify_list_tracks(arg)
            if not metas:
                return self._embed("Das Spotify-Album konnte ich nicht laden (Token, privat oder leer?).",
                                   color=_COL_ERR)
            # Jeder Song bringt seine Spotify-Metadaten als Match-Hint mit -> beim
            # Abspielen wird der laengen-genaue YouTube-Treffer gewaehlt.
            items = [(f"ytsearch1:{mt['query']}", mt["display"],
                      {"query": mt["query"], "dur": mt["dur"],
                       "title": mt["name"], "artist": mt["artist"]}) for mt in metas]
            return await self._play_many(
                player, voice_state.channel, items,
                message.author.display_name, "aus dem Album", reply_to=message,
            )

        if action == "spotify_playlist":
            queries = await self._spotify_playlist_via_embed(arg)
            if not queries:
                return self._embed(
                    "An diese Spotify-**Playlist** komme ich nicht ran – Spotify sperrt den "
                    "Playlist-Zugriff für Bots. Was sicher geht: ein Spotify-**Album**, ein "
                    "einzelner Song-Link oder eine **YouTube-Playlist**.",
                    title="🚫  Playlist gesperrt", color=_COL_ERR)
            # Ueber das Embed gibt's keine Dauer - trotzdem als Hint durchreichen,
            # damit der Best-Match wenigstens Sped-Up/Loop/Cover abwertet.
            items = [(f"ytsearch1:{q}", q, {"query": q, "title": q}) for q in queries]
            return await self._play_many(
                player, voice_state.channel, items,
                message.author.display_name, "aus der Playlist", reply_to=message,
            )

        if action == "yt_playlist":
            entries = await self._youtube_playlist(arg)
            if not entries:
                return self._embed("Die YouTube-Playlist konnte ich nicht laden (leer oder privat?).",
                                   color=_COL_ERR)
            return await self._play_many(
                player, voice_state.channel, entries,
                message.author.display_name, "aus der Playlist", reply_to=message,
            )

        if len(player.queue) >= MAX_QUEUE:
            return self._embed(f"Die Warteschlange ist voll ({MAX_QUEUE}). Warte kurz.", color=_COL_ERR)

        # Track aufloesen (Spotify -> Suchtext, sonst Link/Text direkt)
        try:
            if action == "play" and _SPOTIFY_TRACK_RE.search(arg):
                meta = await self._spotify_track_meta(arg)
                if not meta:
                    return self._embed("Den Spotify-Link konnte ich nicht auflösen (Keys/Token?).",
                                       color=_COL_ERR)
                # Besten YouTube-Treffer per Dauer/Titel waehlen (statt blind den
                # ersten - der ist bei Spotify-Songs oft ein Sped-Up/Loop/Cover).
                track = await self._resolve_input(f"ytsearch1:{meta['query']}", {
                    "query": meta["query"], "dur": meta.get("dur"),
                    "title": meta["name"], "artist": meta.get("artist", ""),
                })
            elif action == "play":
                track = await self._extract(arg)
            else:  # search
                track = await self._extract(f"ytsearch1:{arg}")
        except Exception:  # noqa: BLE001 - yt-dlp wirft viele verschiedene Fehler
            log.exception("Track konnte nicht aufgeloest werden: %s", arg)
            return self._embed("Den Song konnte ich nicht laden. Probier einen anderen Link "
                               "oder Suchbegriff.", color=_COL_ERR)

        track.requested_by = message.author.display_name

        try:
            await player.connect(voice_state.channel)
        except discord.ClientException as exc:
            log.error("Voice-Connect fehlgeschlagen: %s", exc)
            return self._embed("Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                               color=_COL_ERR)

        # Es laeuft schon was -> einreihen. Ab >=2 wartenden Songs gibt's Buttons,
        # mit denen die Person ihren frischen Song an eine Wunsch-Position zieht.
        if player.is_active():
            player.queue.append(track)
            pos = len(player.queue)
            if pos >= 2:
                view = QueuePositionView(player, track, message.author.id)
                emb = self._added_embed(track, pos, pos,
                                        footer="⏭️ = als Nächstes · 📍 = Position wählen")
                try:
                    view.message = await message.reply(embed=emb, view=view, mention_author=False)
                except discord.HTTPException as exc:
                    log.error("Queue-Embed mit Buttons fehlgeschlagen: %s", exc)
                    return emb  # Notfall: wenigstens das Embed ohne Buttons
                log.info("In Warteschlange (#%d) + Position-Buttons: %s", pos, track.title)
                return HANDLED
            return self._added_embed(track, pos, pos)

        try:
            player.start(track)
        except Exception:
            log.exception("Track nicht abspielbar: %s", track.title)
            return self._embed("Den Song konnte ich gerade nicht abspielen. Probier einen anderen.",
                               color=_COL_ERR)
        await self._send_panel(player, track, reply_to=message)
        return HANDLED


# Eine Instanz fuer das ganze Modul - bot.py & Co. nutzen die Aliase darunter.
instance = Music()

# --- Modul-Aliase: bisherige Modul-Funktionen bleiben unter ihren alten
# --- Namen aufrufbar (bot.py/voicegags.py und interne Klassen nutzen sie).
_fmt_dur = instance._fmt_dur
_short = instance._short
_embed = instance._embed
_build_audio_filter = instance._build_audio_filter
_is_volume_word = instance._is_volume_word
setup = instance.setup
is_enabled = instance.is_enabled
_player_for = instance._player_for
heal_voice = instance.heal_voice
is_voice_busy = instance.is_voice_busy
_extract = instance._extract
_resolve_input = instance._resolve_input
_resolve_track = instance._resolve_track
_lazy_track = instance._lazy_track
_norm_match = instance._norm_match
_pick_best_match = instance._pick_best_match
_youtube_search_best = instance._youtube_search_best
_youtube_playlist = instance._youtube_playlist
_spotify_token = instance._spotify_token
_spotify_to_query = instance._spotify_to_query
_spotify_track_meta = instance._spotify_track_meta
_spotify_list_tracks = instance._spotify_list_tracks
_deep_find = instance._deep_find
_spotify_playlist_via_embed = instance._spotify_playlist_via_embed
_clean_lead = instance._clean_lead
parse_command = instance.parse_command
_play_many = instance._play_many
_title_value = instance._title_value
_now_playing_embed = instance._now_playing_embed
_added_embed = instance._added_embed
_gone_embed = instance._gone_embed
_queue_embed = instance._queue_embed
_retire_panel = instance._retire_panel
_send_panel = instance._send_panel
handle = instance.handle
