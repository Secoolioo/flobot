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
import shlex
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field

import aiohttp
import discord

import numfmt

import ai
from basis import FeatureBasis
import guildcfg

try:  # Optional: Bot soll auch ohne yt-dlp starten.
    import yt_dlp
except ImportError:  # pragma: no cover - nur relevant ohne Paket
    yt_dlp = None  # type: ignore[assignment]

log = logging.getLogger("dcbot.music")

# Sentinel: das Modul hat selbst geantwortet (Embed + Buttons direkt gesendet).
# bot.py erkennt das und schickt KEINE zusaetzliche Antwort.
HANDLED = object()

MAX_QUEUE = int(os.getenv("MUSIC_MAX_QUEUE", "50") or "50")
# ^ Vorgabewert. Was WIRKLICH gilt, sagt max_queue(gid) - jeder Server stellt
#   seinen eigenen Deckel ein (guildcfg 'musik_max_queue').


def max_queue(gid=0):
    """Wie viele Songs hier gleichzeitig warten duerfen."""
    if not gid:
        return MAX_QUEUE
    try:
        import guildcfg
        wert = int(guildcfg.get(int(gid), "musik_max_queue") or 0)
        return wert if wert > 0 else MAX_QUEUE
    except Exception:  # noqa: BLE001 - im Zweifel der Vorgabewert
        return MAX_QUEUE
DEFAULT_VOLUME = 0.5    # 0.0 - 1.0
# Takt des Voice-Watchdogs (bot.py-Loop). Haelt die Verbindung am Leben und
# repariert Desyncs/Zombies selbst, solange der Bot in einem Call sein SOLL.
VOICE_HEAL_SECONDS = 15
VOICE_ZOMBIE_TICKS = 3        # so viele stille Ticks (=Sek*Ticks) bis "Zombie" -> Neustart
# So viele Ticks OHNE einen einzigen gesendeten Audio-Block, bis der Watchdog
# den Song neu anstoesst (2 x 15 s = 30 s). Das ist seit dem Rauswurf von
# -rw_timeout die EINZIGE Stall-Erkennung - und die genauere: sie misst echten
# TON, nicht Betrieb auf dem Socket, und kann eine gesunde Wiedergabe deshalb
# nicht abwuergen.
VOICE_STALL_TICKS = 2
# So viele Songs duerfen beim Weiterschalten HINTEREINANDER scheitern, bevor
# der Player aufgibt und die Warteschlange stehen laesst. Vorher gab es keine
# Grenze - ein kurzer Netz-Aussetzer hat so eine ganze Playlist in einem
# Durchlauf als "nicht ladbar" verbucht und kommentarlos entsorgt.
ADVANCE_MAX_FEHLER = 2
# So oft versucht der Watchdog, DENSELBEN Song wiederzubeleben, bevor er ihn
# aufgibt und zum naechsten geht.
#
# Ohne diese Grenze war der Bot in der Sackgasse, die die Nutzer gemeldet haben:
# ein Song mit toter Stream-Adresse (abgelaufener googlevideo-Link) haengt sofort
# wieder, der Watchdog startet ihn alle 30 s erneut - und weil dabei jedes Mal
# die Wiedergabe-Generation hochgezaehlt wird, entwertet er genau den
# after-Callback, an dem 'skip' haengt. Skip meldete "uebersprungen", passierte
# aber nichts; 'Flo spiel X' reihte nur ein, weil is_active() die ganze Zeit
# True blieb. Nur 'Flo stop' kam da raus.
NEUSTART_MAX_VERSUCHE = 2
# So viele Sekunden darf am Ende eines Songs fehlen, ohne dass es als ABBRUCH
# gilt. Alles darueber heisst: FFmpeg ist gestorben, der Song war nicht zu Ende.
#
# Das ist noetig, weil discord.py beides GLEICH meldet: stirbt der FFmpeg-Prozess,
# liefert read() einfach b"" - genau wie am Songende. Der after-Callback bekommt
# dabei KEINEN Fehler. Flo hielt einen nach 40 Sekunden abgestuerzten Song also
# fuer fertig und schaltete brav weiter. Fuer den Zuhoerer sieht das aus wie
# "spielt nur halb und springt dann zum naechsten".
ABBRUCH_TOLERANZ = 10
# So alt darf eine Stream-Adresse hoechstens sein, wenn der Song an die Reihe
# kommt. YouTube unterschreibt seine Adressen zeitlich; wer eine Playlist
# einwirft und eine Stunde spaeter beim zwanzigsten Song ankommt, hat dort eine
# tote URL. Der Song "startet" dann, liefert aber nie Ton - und genau das sah
# nach "der Song geht einfach nicht" aus. Vor dem Start wird deshalb neu
# aufgeloest, wenn die Adresse aelter ist.
STREAM_MAX_ALTER = float(os.getenv("MUSIC_STREAM_MAX_ALTER", "900") or "900")
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
#
# KEIN -rw_timeout. Das stand hier eine Runde lang und war ein Eigentor - hier
# die Messung (lokales ffmpeg 6.1.1, Leser im Echtzeit-Takt wie discord.py,
# Server liefert schubweise mit 20 s Pausen, so drosselt YouTube):
#
#     mit -rw_timeout 15000000 :  12,2 s Audio in 99,8 s Wanduhr
#                                 stderr: "Will reconnect at 0 in 0/1/3 second(s)"
#     ohne                     :  24,5 s Audio in 80,4 s Wanduhr, keine Reconnects
#
# Das Timeout deutet also eine voellig normale Liefer-Pause als NETZWERKFEHLER.
# Dann greift -reconnect_on_network_error, und FFmpeg verbindet sich neu - bei
# einem nicht spulbaren Stream wieder AB BYTE 0, der Song faengt von vorne an.
# Genau das war die Beschwerde "Songs funktionieren nur halbwegs".
# (Bei einem GESUNDEN Server macht die Option keinen Unterschied: 110 s
# Wiedergabe liefen mit und ohne sauber durch - der Schaden entsteht nur bei
# der schubweisen Lieferung, also im Normalbetrieb mit YouTube.)
#
# Und das Timeout hat seine eigene Aufgabe nicht einmal erfuellt. Derselbe
# Aufbau, aber mit einem Server, der mittendrin verstummt (die Verbindung bleibt
# offen) - also genau der Fall, fuer den es eingebaut wurde:
#
#     -rw_timeout MIT -reconnect* :  lebt nach 70 s noch, 4x "Will reconnect at 0"
#     nur -reconnect*             :  lebt nach 70 s noch, keine Reconnects
#     -rw_timeout OHNE -reconnect*:  stirbt nach 15,3 s (returncode 146)
#
# Die Reconnect-Flags heben das Timeout also auf: statt abzubrechen, verbindet
# FFmpeg endlos neu. Nur OHNE sie wuerde es greifen - dann aber wuerde es die
# oben gemessenen CDN-Pausen erst recht toedlich machen. Beides zusammen geht
# nicht; die Reconnect-Flags sind im Normalbetrieb das Wertvollere.
#
# Gegen den STILLEN Stall hilft deshalb der Fortschritts-Waechter in heal():
# der zaehlt die tatsaechlich ausgegebenen Audio-Bloecke (AudioPlayer.loops) und
# misst damit ECHTEN Ton statt Betrieb auf dem Socket. Steht der Zaehler, holt
# Flo eine frische Stream-Adresse und setzt an der Stelle fort; nach
# NEUSTART_MAX_VERSUCHE gibt er den Song auf und geht weiter. Das ist die
# genauere Messung - und sie kann eine gesunde Wiedergabe nicht abwuergen.
# Nach dieser Messung ist der Waechter die EINZIGE Stall-Erkennung, die es gibt.
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
# Die Spotify-HANDY-App teilt NICHT open.spotify.com, sondern einen Kurzlink:
# https://spotify.link/aBcDeFg (frueher auch spoti.fi). Der traf keinen einzigen
# Spotify-Regex, fiel durch die ganze URL-Schleife und landete in der YouTube-
# TEXTSUCHE - Flo suchte also nach der Zeichenkette "https://spotify.link/aBcDeFg".
# Genau das war das gemeldete "Spotify geht nur halb": am PC ging es, vom Handy
# geteilt nicht. Aufgeloest wird er ueber den HTTP-Redirect.
_SPOTIFY_KURZ_RE = re.compile(
    r"https?://(?:spotify\.link|spoti\.fi)/\S+", re.IGNORECASE)

# Satzzeichen und Klammern, die im Chat an einer URL kleben, aber nicht dazu
# gehoeren. Discord-Nutzer schreiben <https://...>, um die Vorschau zu
# unterdruecken, und Links stehen am Satzende. yt-dlp bekam das Zeichen bisher
# mit und suchte dann eine Adresse, die es so nicht gibt.
_URL_MUELL = ">).,;:!?\"'»«"


def _adresse_alt(track):
    """Ist die Stream-Adresse dieses Tracks zu alt zum Abspielen?"""
    if not track.geloest_um:
        return False          # unbekannt -> nicht anfassen
    return (time.monotonic() - track.geloest_um) > STREAM_MAX_ALTER


def _url_saeubern(url):
    """Haengt Satzzeichen ab, die im Chat an der URL kleben."""
    url = (url or "").strip()
    # Eine schliessende Klammer nur abschneiden, wenn sie nicht selbst zur
    # Adresse gehoert (Wikipedia-Links koennen Klammern enthalten).
    while url and url[-1] in _URL_MUELL:
        if url[-1] == ")" and url.count("(") > url.count(")"):
            break
        url = url[:-1]
    return url

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
    # Die alte Form open.spotify.com/user/<name>/playlist/<id> ist eine ganz
    # normale Playlist und kommt aus aelteren geteilten Links immer noch vor.
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:user/[^/\s]+/)?(playlist|album)/"
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
# Benennt die Adresse ein einzelnes VIDEO? Alle drei Schreibweisen, in denen
# YouTube das tut - watch?v=, der Kurzlink youtu.be/ und /shorts/. Steht eines
# davon drin, ist dieses Video gemeint, egal was fuer eine Liste danebensteht.
_YT_VIDEO_RE = re.compile(
    r"[?&]v=[A-Za-z0-9_-]{6,}|youtu\.be/[A-Za-z0-9_-]{6,}|"
    r"/shorts/[A-Za-z0-9_-]{6,}|/live/[A-Za-z0-9_-]{6,}", re.IGNORECASE)

# SoundCloud. yt-dlp bringt den Extractor mit - ohne Key, ohne Login fuer
# oeffentliche Tracks. Es fehlte also nur die ERKENNUNG: ein SC-Link fiel durch
# die URL-Schleife und wurde als Freitext behandelt, d. h. Flo suchte auf
# YouTube nach der URL-Zeichenkette.
# 'on.soundcloud.com' sind die Kurzlinks aus der App - die loesen wir NICHT
# selbst auf, yt-dlp folgt dem Redirect von allein.
_SC_RE = re.compile(
    r"https?://(?:www\.|m\.|on\.)?soundcloud\.com/\S+", re.IGNORECASE)
# Ein "Set" ist bei SoundCloud die Playlist (…/sets/<name>).
# Direkte Audio-Dateien: die spielt FFmpeg ohne Umweg.
_AUDIO_DATEI_RE = re.compile(
    r"\.(?:mp3|m4a|aac|ogg|oga|opus|wav|flac|webm)(?:\?|#|$)", re.IGNORECASE)
_SC_SET_RE = re.compile(
    r"https?://(?:www\.|m\.)?soundcloud\.com/[^/\s]+/sets/\S+", re.IGNORECASE)

# Steuerbefehle: (Aktion, Regex am Satzanfang). Reihenfolge = Prioritaet.
# Wichtig: JEDES Muster endet auf \b oder \w*\b. Ohne Wortgrenze reicht das
# blosse PRAEFIX - und dann kaperten die Steuerbefehle ganz normale Saetze:
# "verlass dich drauf" wurde zum Voice-Leave, und "rausschmeisen @wer" (die
# gaengige Ein-s-Schreibweise) liess Flo den Sprachkanal verlassen und die
# Musik abbrechen, statt die Person zu kicken.
_CONTROL = [
    ("skip",   re.compile(r"^(?:skip|ueberspring\w*|überspring\w*|naechst\w*|nächst\w*|next)\b", re.I)),
    ("pause",  re.compile(r"^(?:pause|pausier\w*)\b", re.I)),
    ("resume", re.compile(r"^(?:resume|weiter|fortsetz\w*|weiterspiel\w*)\b", re.I)),
    ("stop",   re.compile(r"^(?:stop|stopp|halt|aufhoer\w*|aufhör\w*|hoer auf|hör auf)\b", re.I)),
    # Negative Vorschau gegen die Redewendung: "verlass dich drauf" /
    # "verlass dich nicht darauf" ist Gerede, kein Befehl zum Rausgehen.
    ("leave",  re.compile(r"^(?:leave|verlasse?(?!\s+(?:dich|euch|sich|mich|uns))|"
                          r"geh raus|hau ab|raus|disconnect)\b", re.I)),
    # 'liste' zaehlt nur, wenn NICHTS dahinter steht: "liste mal auf, was du
    # kannst" ist eine Frage an die KI, keine Warteschlangen-Abfrage.
    ("queue",  re.compile(r"^(?:queue\b|warteschlange\b|liste\s*$)", re.I)),
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
# \d+ statt \d{1,3}: bei drei Ziffern wurde aus "ls 1000" ein 100-%-Befehl
# (die Null fiel einfach weg) statt der erwarteten Klemmung auf 200 %.
_VOLUME_ARG_RE = re.compile(r"^([A-Za-zÄÖÜäöüß]+)\.?\s*(?:auf\s*)?(\d+)?", re.I)
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
    # monotonic-Zeitpunkt, an dem stream_url geholt wurde. YouTube unterschreibt
    # seine Adressen zeitlich - eine, die lange in der Warteschlange lag, ist
    # beim Start tot. Dann spielt Flo "etwas", es kommt aber nie Ton.
    geloest_um: float = 0.0
    # Die HTTP-Kopfzeilen, mit denen yt-dlp die Adresse geholt hat. YouTube
    # unterschreibt eine Stream-Adresse fuer GENAU den Client, der sie angefragt
    # hat (in der Adresse steht z. B. 'c=ANDROID_VR'). Meldet sich beim Abholen
    # jemand anders - und ffmpeg meldet sich von Haus aus als 'Lavf/...' -,
    # antwortet YouTube mit 403 und der Song bricht "nach 0 von 178 s" ab.
    kopfzeilen: dict = field(default_factory=dict)

    # Kopfzeilen, die ffmpeg selbst setzen muss - die durchzureichen bricht die
    # Verbindung (Range/Host gehoeren zur Anfrage, nicht zum Client).
    _NICHT_WEITERGEBEN = ("range", "host", "accept-encoding", "connection",
                          "content-length")

    def ffmpeg_vorspann(self):
        """Die -user_agent/-headers-Optionen, mit denen ffmpeg die Adresse holen
        MUSS. Leer, wenn es nichts durchzureichen gibt."""
        if not self.kopfzeilen:
            return ""
        teile = []
        rest = []
        for name, wert in self.kopfzeilen.items():
            if not wert or name.lower() in self._NICHT_WEITERGEBEN:
                continue
            if name.lower() == "user-agent":
                teile += ["-user_agent", shlex.quote(str(wert))]
            else:
                rest.append(f"{name}: {wert}")
        if rest:
            # ffmpeg erwartet die Zeilen mit CRLF getrennt und abgeschlossen.
            teile += ["-headers", shlex.quote("".join(f"{z}\r\n" for z in rest))]
        return " ".join(teile)


@dataclass
class GuildPlayer:
    """Haelt Voice-Verbindung und Warteschlange fuer EINEN Server."""
    loop: asyncio.AbstractEventLoop
    # Zu welchem Server dieser Player gehoert - daran haengen die Einstellungen
    # dieses Servers (Lautstaerke, Warteschlangen-Deckel).
    guild_id: int = 0
    queue: list[Track] = field(default_factory=list)
    history: list[Track] = field(default_factory=list)  # zuletzt gespielt (fuer 'nochmal')
    voice: discord.VoiceClient | None = None
    current: Track | None = None
    text_channel: discord.abc.Messageable | None = None
    volume: float = DEFAULT_VOLUME   # 0.0 - 2.0, per Befehl aenderbar
    panel_message: "discord.Message | None" = None  # aktuelles Steuer-Panel
    # Die View zum Panel. Muss mitgefuehrt werden, um sie beim Ausmustern
    # abmelden zu koennen: sie laeuft mit timeout=None und wird deshalb von
    # discord.py NIE von selbst aus dem ViewStore genommen. Gemessen: 200
    # gepostete Panels = 200 Eintraege, die auch nach dem Loeschen der
    # Nachricht und aller Referenzen bestehen bleiben.
    panel_view: "discord.ui.View | None" = None
    speed: float = 1.0               # 0.5 - 2.0, per Tempo-Dropdown im Panel waehlbar
    _seg_start: float | None = None  # monotonic: Start des laufenden Abschnitts (None=aus/pausiert)
    _played: float = 0.0             # bereits gespielte Song-Sekunden vor diesem Abschnitt
    _play_gen: int = 0               # Generation des aktuell gueltigen Players (gegen Race beim Neustart)
    active_channel_id: int | None = None  # in DIESEM Kanal soll der Bot bleiben (None = bewusst raus)
    _advancing: bool = False         # laeuft gerade _advance (Songwechsel)? -> Watchdog haelt sich raus
    _stall_ticks: int = 0            # Zaehler fuer "verbunden, aber still" (Zombie-Erkennung, entprellt)
    # Fortschritts-Wache gegen den FFmpeg-Stall: discord.py zaehlt in
    # AudioPlayer.loops jeden gesendeten 20-ms-Block. Steht der Zaehler,
    # obwohl is_playing() True meldet, fliesst KEIN Ton mehr. position()
    # taugt dafuer nicht - die rechnet nur mit der Uhr und laeuft im Stall
    # munter weiter.
    _last_frames: int = -1           # zuletzt gesehener Block-Zaehler
    _frozen_ticks: int = 0           # so viele Ticks ohne neuen Block
    _panel_gen: int = 0              # Generation des zuletzt angeforderten Panels
    pausiert: bool = False           # hat jemand BEWUSST pausiert? (ueberlebt Reconnects)
    # Sitzungs-Generation: NUR disconnect() ('Flo stop'/'leave') zaehlt hoch.
    # Damit laesst sich "die Sitzung wurde beendet" sauber von "jemand hat
    # einfach etwas anderes gestartet" unterscheiden - _play_gen allein kann
    # das nicht, die steigt bei jedem Songwechsel.
    _session_gen: int = 0
    # _advance hat nach ADVANCE_MAX_FEHLER aufgegeben und die Warteschlange
    # bewusst stehen gelassen. Ohne diese Merke stiess der Watchdog (heal, Fall 3)
    # sie alle 15 s erneut an, fraß dabei je Takt einen Song und schickte
    # dieselbe Warnung immer wieder in den Chat.
    _advance_aufgegeben: bool = False
    # Wie oft der Watchdog den LAUFENDEN Song schon wiederbelebt hat. Wird bei
    # jedem echten Songwechsel zurueckgesetzt (start ohne keep_speed).
    _neustart_versuche: int = 0
    _last_reconnect: float = 0.0     # monotonic des letzten Reconnect-Versuchs (Loop-Bremse)
    _reconnect_fails: int = 0        # aufeinanderfolgende fehlgeschlagene Reconnects (Aufgabe-Schwelle)
    # Serialisiert ALLE voice-veraendernden Ops (connect/_reconnect/apply_speed),
    # damit nie zwei channel.connect() gleichzeitig laufen.
    _voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serialisiert die Songwechsel. Zwei gleichzeitige Laeufe (zwei schnelle
    # Skips, oder Skip waehrend der after-Callback schon laeuft) haben beide aus
    # derselben Warteschlange gepoppt - dabei ging ein Track spurlos verloren.
    _advance_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
            # einzeln gewaehlt. Und er startet spielend: eine alte Pause-Absicht
            # gilt nur fuer den Song, bei dem sie gesetzt wurde.
            self.speed = 1.0
            self.pausiert = False
            # Neuer Song -> die Wiederbelebungs-Versuche gelten wieder frisch.
            # (Der Watchdog-Neustart laeuft mit keep_speed=True und zaehlt hier
            # bewusst NICHT zurueck, sonst koennte er sich ewig selbst verlaengern.)
            self._neustart_versuche = 0
        # Reihenfolge der Eingangs-Optionen (alles VOR '-i', sonst ignoriert
        # ffmpeg sie): erst die Client-Kennung, dann der Seek, dann der Rest.
        vorne = [track.ffmpeg_vorspann()]
        if seek > 0.5:
            # -ss VOR -i = schneller Eingangs-Seek, damit der Song an der Stelle
            # weiterlaeuft statt von vorne (Tempo/Reverb aendern nur den Klang, nicht die Pos.)
            vorne.append(f"-ss {seek:.2f}")
        vorne.append(_FFMPEG_BEFORE)
        before = " ".join(t for t in vorne if t)
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

    def pausieren(self):
        """Anhalten: Wiedergabe, Uhr und ABSICHT an einer Stelle.

        Die gemerkte Absicht ist der eigentliche Punkt: der Voice-Client kann
        zwischendurch sterben (Reconnect, Tempo-Wechsel, Neustart nach Stall),
        und danach war is_paused() natuerlich False - der Bot spielte dann
        munter weiter, obwohl jemand pausiert hatte."""
        if self.voice is not None and self.voice.is_playing():
            self.voice.pause()
        self._clock_pause()
        self.pausiert = True

    def fortsetzen(self):
        """Weiterspielen: Wiedergabe, Uhr und Absicht an einer Stelle."""
        if self.voice is not None and self.voice.is_paused():
            self.voice.resume()
        self._clock_resume()
        self.pausiert = False

    def ist_pausiert(self):
        """True, wenn jemand bewusst pausiert hat ODER der Client pausiert ist."""
        if self.pausiert:
            return True
        return self.voice is not None and self.voice.is_paused()

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
            # War pausiert? Dann muss es NACH dem Neustart auch wieder pausiert
            # sein. Vorher hob jeder Tempo-Wechsel die Pause klammheimlich auf,
            # und der Pause-Knopf im Panel zeigte danach das Falsche an.
            war_pause = self.ist_pausiert()
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
                if war_pause:
                    self.pausieren()
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
            # Die Generation MITGEBEN: zwischen dieser Pruefung und dem
            # tatsaechlichen Lauf von _advance liegt der Sprung in den Event-Loop
            # und danach womoeglich sekundenlanges Aufloesen. In dieser Luecke
            # kann jemand selbst etwas starten - dann ist dieser Callback veraltet.
            asyncio.run_coroutine_threadsafe(self._advance(gen), self.loop)
        except Exception:
            log.exception("Konnte naechsten Track nach Songende nicht einplanen")

    async def _advance(self, gen=None):
        """Spielt den naechsten abspielbaren Track. Kaputte/altersbeschraenkte
        Eintraege (yt-dlp DownloadError, 'keine Treffer', tote Links) werden
        UEBERSPRUNGEN statt den Player anzuhalten - so bleibt die Musik bei einem
        faulen Song nicht stehen. Schleife statt Rekursion, damit auch eine ganze
        Reihe toter Songs sauber uebersprungen wird.

        'gen' ist die Player-Generation, aus der dieser Aufruf stammt. Hat sich
        die inzwischen geaendert, hat jemand selbst etwas gestartet und dieser
        Aufruf ist veraltet - dann NICHTS tun. Ohne diese Pruefung passierte
        Folgendes (nachgestellt): Song A endet, _advance haengt im Aufloesen
        eines Playlist-Tracks, in der Luecke sagt jemand 'flo spiel X'. Danach
        laeuft zwar X, aber _advance macht weiter: jedes start() scheitert an
        'Already playing audio.', wird als 'Track nicht ladbar' verbucht und
        uebersprungen - die KOMPLETTE Warteschlange lief leer (4 -> 0), current
        stand auf None, und das gerade gepostete Panel wurde geloescht.
        Aufrufe ohne 'gen' (z. B. aus _reconnect) pruefen nichts."""
        # WARTEN statt aussteigen. Der zweite Lauf wird nicht verschluckt - er
        # kommt nur nach dem ersten dran. Das ist wichtig: die Zusicherung
        # weiter unten ("ohne gen laeuft IMMER", music.py:992) bleibt damit
        # wahr. Ein frueher Ausstieg haette einen zweiten Skip stillschweigend
        # geschluckt, und genau das war an einem Vorschlag falsch, der hier
        # schon mal stand.
        #
        # Kein Deadlock: der einzige Weg zurueck nach _advance fuehrt ueber
        # _after, und das plant per run_coroutine_threadsafe einen NEUEN Task -
        # es ruft sich nie innerhalb desselben Aufrufs selbst.
        async with self._advance_lock:
            return await self._advance_intern(gen)

    async def _advance_intern(self, gen=None):
        """Der eigentliche Songwechsel. Nur ueber _advance aufrufen - der haelt
        den Lock."""
        # _advancing markiert die (ggf. langsame) Aufloesephase, damit der Voice-
        # Watchdog in dieser Luecke KEINEN Zombie-Alarm ausloest.
        if gen is not None and gen != self._play_gen:
            return
        if gen is None:
            # Ausdruecklich angestossen (skip, weiter, Reconnect) - dann ist eine
            # frueher aufgegebene Warteschlange wieder freigegeben.
            self._advance_aufgegeben = False
        self._advancing = True
        sitzung = self._session_gen    # gehoert dieser Lauf noch zur laufenden Sitzung?
        fehler_serie = 0        # Fehlschlaege DIREKT hintereinander
        try:
            # Kam der Callback, weil der Song ZU ENDE ist - oder weil FFmpeg
            # gestorben ist? Nur beim echten Ende wird weitergeschaltet.
            # (Bei gen=None hat ein Mensch 'skip' gedrueckt - der will weiter.)
            if gen is not None and await self._nach_abbruch_fortsetzen():
                return
            while True:
                if gen is not None and gen != self._play_gen:
                    return          # jemand hat inzwischen selbst gestartet
                if not self.voice or not self.voice.is_connected() or not self.queue:
                    self.current = None
                    await _retire_panel(self)
                    return
                track = self.queue.pop(0)
                try:
                    if track.stream_url and track.query and _adresse_alt(track):
                        # Die Adresse lag zu lange herum (siehe STREAM_MAX_ALTER):
                        # frisch holen, sonst startet ein Song, der nie Ton macht.
                        log.info("Stream-Adresse von '%s' ist veraltet - hole eine "
                                 "frische.", track.title)
                        track.stream_url = ""
                    if not track.stream_url and track.query:
                        track = await _resolve_track(track)  # Playlist-Track jetzt aufloesen
                        if gen is not None and gen != self._play_gen:
                            # Waehrend des Aufloesens hat jemand selbst gestartet.
                            # Den Track zurueck in die Schlange - ABER nur, wenn
                            # die Sitzung noch dieselbe ist. Nach 'Flo stop' ist
                            # die Warteschlange absichtlich leer; ein Track, der
                            # dort wieder hineinfaellt, spielt beim naechsten
                            # Play als Geist an ("ich hab doch gestoppt").
                            if self._session_gen == sitzung:
                                self.queue.insert(0, track)
                            return
                    # Der vorige Player raeumt noch auf - ohne dieses Warten
                    # wirft play() 'Already playing audio.', und das wurde als
                    # "Track nicht ladbar" verbucht: der Song war weg, obwohl
                    # mit ihm alles in Ordnung war.
                    await self._warte_bis_still()
                    self.start(track)
                except Exception:
                    fehler_serie += 1
                    log.exception("Track uebersprungen (nicht ladbar): %s", track.title)
                    # Zwei Fehlschlaege HINTEREINANDER sind kein Zufall mehr,
                    # sondern fast immer das Netz (kurzer DNS-/yt-dlp-Aussetzer).
                    # Frueher frass die Schleife dann in einem Rutsch die
                    # komplette Playlist als "nicht ladbar" - stumm, ohne ein
                    # Wort im Chat. Jetzt bleibt die Warteschlange stehen.
                    if fehler_serie >= ADVANCE_MAX_FEHLER:
                        self.queue.insert(0, track)
                        self.current = None
                        self._advance_aufgegeben = True
                        await _retire_panel(self)
                        log.error("Zwei Songs am Stueck nicht ladbar - Warteschlange "
                                  "(%d) bleibt stehen statt sie wegzuwerfen.",
                                  len(self.queue))
                        await self._sag(
                            f"⚠️ Ich komme gerade an keinen Song ran (Netz?). "
                            f"Die Warteschlange (**{len(self.queue)}**) bleibt "
                            f"stehen – `weiter` versucht es nochmal.")
                        return
                    continue  # naechsten Song versuchen, nicht stoppen
                fehler_serie = 0
                self._advance_aufgegeben = False
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
        self._session_gen += 1          # alles, was noch laeuft, gehoert zur ALTEN Sitzung
        self._stall_ticks = 0
        self._frozen_ticks = 0
        self._last_frames = -1
        self.pausiert = False
        self._play_gen += 1             # alte after-Callbacks entwerten
        await _retire_panel(self)
        if self.voice is not None:
            try:
                await self.voice.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
            self.voice = None

    async def _sag(self, text):
        """Kurze Meldung in den Musik-Kanal. Nie fatal - wenn das Reden nicht
        klappt, laeuft die Musik trotzdem weiter."""
        kanal = self.text_channel
        if kanal is None:
            return
        try:
            await kanal.send(text)
        except Exception:  # noqa: BLE001
            log.debug("Musik-Meldung konnte nicht gesendet werden", exc_info=True)

    @staticmethod
    def _frames(vc):
        """Wie viele 20-ms-Bloecke der Player bisher rausgeschickt hat.

        Der EINZIGE ehrliche Fortschritts-Beweis. discord.py zaehlt sie in
        AudioPlayer.loops mit; steht der Zaehler bei laufendem is_playing(),
        kommt kein Ton mehr an. -1 = kein Player da / Zaehler unbekannt (dann
        wird die Stall-Erkennung einfach uebersprungen, statt zu raten)."""
        spieler = getattr(vc, "_player", None)
        if spieler is None:
            return -1
        try:
            return int(getattr(spieler, "loops", -1))
        except (TypeError, ValueError):
            return -1

    async def _warte_bis_still(self, max_sekunden=2.0):
        """Wartet, bis der Player wirklich aufgehoert hat zu spielen.

        voice.stop() wirkt nicht sofort: der Player-Thread laeuft noch seinen
        letzten Block zu Ende. Ein play() in dieser Luecke wirft 'Already
        playing audio.' - und das wurde weiter oben als 'Track nicht ladbar'
        verbucht, der Song also uebersprungen."""
        schritte = int(max(1, max_sekunden / 0.05))
        for _ in range(schritte):
            if self.voice is None or not self.voice.is_playing():
                return True
            await asyncio.sleep(0.05)
        return False

    async def skip(self):
        """Zum naechsten Song - und zwar VERLAESSLICH.

        Frueher stand hier nur voice.stop() und der Rest hing am
        after-Callback. Der wird aber entwertet, sobald die Wiedergabe-
        Generation zwischendurch hochzaehlt (Watchdog-Neustart, Tempo-Wechsel,
        Reconnect). Fiel der Skip in so ein Fenster, meldete Flo
        'uebersprungen' - und es passierte nichts. Jetzt entwerten wir den
        Callback selbst und stossen den naechsten Song direkt an, damit es
        genau EINEN Weg gibt und der immer laeuft."""
        self._play_gen += 1              # laufenden after-Callback entwerten
        if self.voice is not None:
            try:
                self.voice.stop()
            except Exception:  # noqa: BLE001 - stop darf nie werfen
                log.debug("voice.stop beim Skip fehlgeschlagen", exc_info=True)
            await self._warte_bis_still()
        await self._advance()            # ohne gen -> laeuft IMMER

    async def _nach_abbruch_fortsetzen(self):
        """War der Song ABGEBROCHEN statt zu Ende? Dann dort weitermachen.

        discord.py meldet beides gleich: stirbt FFmpeg, liefert read() b"" -
        genau wie am Songende, und der after-Callback bekommt keinen Fehler.
        Ohne diese Pruefung schaltete Flo nach einem Absturz einfach zum
        naechsten Song; fuer den Zuhoerer bricht die Musik dann staendig
        mittendrin ab und springt weiter.

        Rueckgabe True = uebernommen (der Aufrufer darf NICHT weiterschalten)."""
        track = self.current
        if track is None or not track.duration:
            return False                 # ohne bekannte Laenge nicht zu beurteilen
        gehoert = self.position()
        fehlt = track.duration - gehoert
        if fehlt <= ABBRUCH_TOLERANZ:
            return False                 # normal zu Ende gelaufen
        if self._neustart_versuche >= NEUSTART_MAX_VERSUCHE:
            log.error("'%s' bricht immer wieder ab (%.0f von %d s) - gebe auf.",
                      track.title, gehoert, track.duration)
            await self._sag(f"⏭️ **{track.title}** bricht immer wieder ab – "
                            f"ich gehe zum nächsten.")
            return False                 # aufgeben -> normal weiterschalten
        self._neustart_versuche += 1
        log.warning("Song '%s' brach nach %.0f von %d s ab - setze fort "
                    "(Versuch %d/%d).", track.title, gehoert, track.duration,
                    self._neustart_versuche, NEUSTART_MAX_VERSUCHE)
        # Zwei Sekunden Ueberlappung: bis zum Abbruch war ja Ton da.
        return await self._neustart_an_position(verlust=2.0)

    async def _neustart_an_position(self, *, verlust=None):
        """Startet den laufenden Song an der zuletzt GEHOERTEN Stelle neu -
        ohne die Verbindung anzufassen und ohne die Warteschlange zu opfern.

        Genau das macht der Betreiber heute von Hand mit 'Flo stop' +
        'Flo nochmal' - nur dass dabei die gesammelte Warteschlange verloren
        geht. Hier bleibt sie stehen."""
        track = self.current
        if track is None or self.voice is None or not self.voice.is_connected():
            return False
        # Die Adresse, die gerade haengt, ist oft schlicht ABGELAUFEN: YouTube
        # unterschreibt seine Stream-Links zeitlich, und ein Song, der eine
        # Weile in der Warteschlange stand, hat beim Start eine tote URL.
        # Denselben toten Link nochmal zu starten heilt gar nichts - also
        # holen wir uns vorher eine frische Adresse. Scheitert das, geht es
        # mit der alten weiter (besser als gar kein Versuch).
        if track.query:
            try:
                frisch = await _resolve_track(track)
                if frisch is not None and frisch.stream_url:
                    track = frisch
                    self.current = track
            except Exception:  # noqa: BLE001 - dann eben mit der alten Adresse
                log.debug("Frische Stream-Adresse nicht zu bekommen", exc_info=True)
        async with self._voice_lock:
            # Die Uhr lief waehrend des Stalls weiter, gehoert hat man das
            # aber nicht. Die stillen Sekunden also wieder abziehen, damit der
            # Song nicht mittendrin weiterspringt. Bei einem ABBRUCH (FFmpeg
            # gestorben) war dagegen bis zuletzt Ton da - dort genuegen ein
            # paar Sekunden Ueberlappung.
            if verlust is None:
                verlust = VOICE_STALL_TICKS * VOICE_HEAL_SECONDS * max(0.1, self.speed)
            pos = max(0.0, self.position() - verlust)
            self._play_gen += 1        # haengenden after-Callback entwerten
            try:
                self.voice.stop()      # killt die haengende FFmpeg-Quelle
                await self._warte_bis_still()
                self.start(track, seek=pos, keep_speed=True)
                if self.pausiert:
                    self.pausieren()
            except Exception:  # noqa: BLE001 - Neustart darf den Bot nie mitreissen
                log.exception("Neustart nach Audio-Stall fehlgeschlagen")
                return False
        return True

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

        # Pausiert? Dann ist Stillstand genau richtig - Finger weg von allem.
        if self.ist_pausiert():
            self._stall_ticks = 0
            self._frozen_ticks = 0
            self._last_frames = self._frames(vc)
            return

        # Fall 1 - STILLER STALL: is_playing() meldet True, es fliesst aber kein
        # Ton (FFmpeg haengt im Lesen, der after-Callback feuert nie). Der alte
        # Watchdog war hier BLIND, weil er nur 'not is_playing()' kannte - und
        # genau dieser Zustand ist das gemeldete "Queue voll, spielt nicht".
        if vc.is_playing():
            self._stall_ticks = 0
            frames = self._frames(vc)
            if frames >= 0 and frames == self._last_frames:
                self._frozen_ticks += 1
                if self._frozen_ticks >= VOICE_STALL_TICKS:
                    self._frozen_ticks = 0
                    self._last_frames = -1
                    if self._neustart_versuche >= NEUSTART_MAX_VERSUCHE:
                        # Der Song ist nicht zu retten. WEITER statt ewig
                        # dasselbe versuchen: genau diese Endlosschleife war
                        # die Sackgasse, aus der nur 'Flo stop' herausfuehrte.
                        titel = self.current.title if self.current else "Der Song"
                        log.error("Audio-Stall: '%s' auch nach %d Neustarts still "
                                  "- ueberspringe ihn.", titel, self._neustart_versuche)
                        await self._sag(f"⏭️ **{titel}** liefert keinen Ton mehr – "
                                        f"ich gehe zum nächsten.")
                        await self.skip()
                        return
                    self._neustart_versuche += 1
                    log.warning("Audio-Stall: verbunden und 'spielend', aber seit "
                                "%d s kein Ton - starte den Song neu (Versuch %d/%d).",
                                VOICE_STALL_TICKS * VOICE_HEAL_SECONDS,
                                self._neustart_versuche, NEUSTART_MAX_VERSUCHE)
                    await self._neustart_an_position()
                    return
            else:
                self._frozen_ticks = 0
            self._last_frames = frames
            return

        self._frozen_ticks = 0
        self._last_frames = -1

        # Fall 2 - ZOMBIE: es SOLLTE etwas laufen, tut es aber mehrere Ticks nicht.
        if self.current is not None:
            self._stall_ticks += 1
            if self._stall_ticks >= VOICE_ZOMBIE_TICKS:
                self._stall_ticks = 0
                log.warning("Voice-Zombie: verbunden, aber still - starte neu.")
                await self._reconnect(channel)
            return

        self._stall_ticks = 0

        # Fall 3 - LIEGENGEBLIEBENE WARTESCHLANGE: verbunden, nichts laeuft, aber
        # es warten noch Songs. Dahin kommt man, wenn ein Song genau waehrend
        # eines kurzen Voice-Aussetzers endet: _advance sieht die Verbindung weg,
        # setzt current=None und laesst die Queue liegen. Danach stellt der
        # Watchdog zwar die Verbindung wieder her - aber angestossen hat die
        # Warteschlange bisher NIEMAND mehr. Zweiter Dauer-Steckzustand.
        # ... aber NICHT, wenn _advance gerade selbst aufgegeben hat: dann ist
        # die Warteschlange absichtlich stehengeblieben, und ein Anstossen im
        # 15-Sekunden-Takt wuerde sie Song fuer Song aufbrauchen und dabei
        # dieselbe Warnung immer wieder posten. 'weiter' setzt das zurueck.
        if self.queue and not self._advance_aufgegeben:
            log.info("Warteschlange lag liegen (%d Songs) - stosse sie an.",
                     len(self.queue))
            await self._advance()

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
                    if self.pausiert:
                        # Wer pausiert hat, will nach dem Reconnect KEINE Musik.
                        # Vorher spielte der Watchdog einfach weiter.
                        self.pausieren()
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
        if not numfmt.ist_zahl(raw.lstrip("+")):
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
        if self.player.ist_pausiert():
            self.player.fortsetzen()
        else:
            self.player.pausieren()
        self._sync_pause()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def _skip(self, interaction, _b):
        # Genau derselbe Weg wie der Textbefehl. Der Knopf hing noch am alten
        # voice.stop(), und das ist der unzuverlaessige Weg: haengt ein Song
        # (Watchdog zaehlt die Generation hoch), verpufft der Callback - der
        # Knopf tat dann nichts oder startete denselben Song neu.
        if self.player.current is None and not self.player.queue:
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.player.skip()

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def _stop(self, interaction, _b):
        # Gleiche Regel wie beim Textbefehl: bei Desync trotzdem aufraeumen,
        # sonst holt der Watchdog den Bot zurueck.
        if (self.player.voice is None and self.player.active_channel_id is None
                and not self.player.queue and self.player.current is None):
            await interaction.response.send_message("Ich bin in keinem Sprachkanal.", ephemeral=True)
            return
        # Diese Nachricht wird gleich zur 'Gestoppt'-Bestaetigung umgebaut -> aus der
        # Panel-Verwaltung nehmen, damit disconnect()->_retire_panel sie NICHT loescht.
        # Die View wird unten selbst gestoppt (stop() am Ende), also auch hier abmelden.
        self.player.panel_message = None
        self.player.panel_view = None
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


class Music(FeatureBasis):
    """Buendelt Zustand und Logik des Musik-Features (frueher freie
    Modul-Funktionen und globale Variablen dieses Moduls)."""

    def __init__(self):
        # --- Konfiguration (in setup() aus der .env gelesen) ---------------------
        self._enabled = False
        self._guter_client = ""   # player_client, der zuletzt durchkam
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
        # Bei einer Aenderung der Lautstaerke im Panel oder per Befehl sofort
        # nachziehen, statt sie nur beim Anlegen eines Players zu lesen.
        guildcfg.horcht_auf("lautstaerke", self.lautstaerke_nachziehen)
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

    # --- Selbsttest: laeuft die Kette wirklich durch? -----------------------
    # "Musik-Feature aktiv" hing bisher allein daran, dass yt-dlp, ffmpeg und
    # PyNaCl INSTALLIERT sind. Ob damit auch nur ein Ton herauskommt, hat nie
    # jemand geprueft - und genau das war der Ausfall: yt-dlp loeste sauber auf,
    # ffmpeg bekam vom Ziel aber 403, weil ihm die Client-Kennung fehlte. Im Log
    # stand trotzdem "aktiv". Eine Zusicherung, die niemand nachgesehen hat, ist
    # schlimmer als keine.
    SELBSTTEST_PROBE = "ytsearch1:lofi hip hop radio"
    SELBSTTEST_SEKUNDEN = 0.6

    async def selbsttest(self):
        """Loest EINEN Song auf und holt ein paar Zehntelsekunden echten Ton.
        Genau die Strecke, die im Betrieb bricht. Gibt (ok, grund) zurueck und
        wirft nie - ein Selbsttest darf den Start niemals kippen."""
        if not self._enabled:
            return False, "Musik-Feature ist aus"
        try:
            track = await self._extract(self.SELBSTTEST_PROBE)
        except Exception as exc:  # noqa: BLE001 - jeder Grund ist hier eine Antwort
            grund = f"yt-dlp kommt nicht durch ({type(exc).__name__}: {str(exc)[:160]})"
            log.error("Musik-Selbsttest: %s. Meist hilft:  venv/bin/pip install -U "
                      "yt-dlp", grund)
            return False, grund
        if not track.stream_url:
            log.error("Musik-Selbsttest: yt-dlp liefert keine Stream-Adresse.")
            return False, "keine Stream-Adresse"

        bytes_ton, fehler = await self._probe_ton(track)
        if bytes_ton <= 0:
            hinweis = ""
            if "403" in fehler:
                # Genau der Ausfall vom 20.08.2026 - beim Namen nennen.
                hinweis = (" Das ist die Client-Bindung: die Adresse gilt nur fuer "
                           "den Client, mit dem yt-dlp sie geholt hat.")
            log.error("Musik-Selbsttest: ffmpeg bekommt keinen Ton (%s).%s",
                      fehler or "kein Grund gemeldet", hinweis)
            return False, fehler or "kein Ton"
        log.info("Musik-Selbsttest ok (%s, %d Bytes Ton in %.1fs).",
                 track.title[:50], bytes_ton, self.SELBSTTEST_SEKUNDEN)
        return True, ""

    async def _probe_ton(self, track):
        """Zieht kurz echten Ton durch ffmpeg - so wie GuildPlayer.start es tut,
        inklusive Client-Kennung. Gibt (bytes, fehlertext) zurueck."""
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        vorne = [t for t in (track.ffmpeg_vorspann(), _FFMPEG_BEFORE) if t]
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error",
                *shlex.split(" ".join(vorne)), "-i", track.stream_url,
                "-t", str(self.SELBSTTEST_SEKUNDEN),
                "-f", "s16le", "-ar", "48000", "-ac", "2", "-"]

        def hole():
            fertig = subprocess.run(argv, capture_output=True, timeout=45)
            return len(fertig.stdout), fertig.stderr.decode("utf-8", "replace")[:200].strip()

        try:
            return await asyncio.get_running_loop().run_in_executor(None, hole)
        except Exception as exc:  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {str(exc)[:160]}"

    async def spotify_selbsttest(self):
        """Prueft die Spotify-Zugangsdaten wirklich, statt nur ihr Vorhandensein
        zu melden. Gibt (ok, grund). Ohne Keys ist es kein Fehler - dann kann Flo
        eben nur YouTube."""
        if not (self._spotify_id and self._spotify_secret):
            return True, ""
        token = await self._spotify_token()
        if not token:
            log.error("Musik-Selbsttest: Spotify-Zugangsdaten werden abgelehnt - "
                      "Spotify-Links koennen nicht aufgeloest werden. Pruefen mit:  "
                      "bash k m")
            return False, "Spotify-Token abgelehnt"
        log.info("Musik-Selbsttest: Spotify-Token ok.")
        return True, ""

    def _player_for(self, guild_id):
        player = self._players.get(guild_id)
        if player is None:
            player = GuildPlayer(loop=asyncio.get_running_loop(),
                                 guild_id=int(guild_id or 0),
                                 volume=self._start_lautstaerke(guild_id))
            self._players[guild_id] = player
        return player

    @staticmethod
    def _lautstaerke_anwenden(player, wert):
        """Setzt die Lautstaerke am Player UND an der laufenden Tonquelle.

        Beides zusammen, weil der Player die Zahl fuer den naechsten Song haelt
        und die Tonquelle das, was gerade zu hoeren ist. Nur eins davon zu
        setzen heisst: es wirkt erst beim naechsten Lied."""
        if player is None:
            return
        player.volume = max(0.0, min(2.0, float(wert)))
        if player.voice is not None and isinstance(
                player.voice.source, discord.PCMVolumeTransformer):
            player.voice.source.volume = player.volume

    def lautstaerke_nachziehen(self, gid):
        """guildcfg meldet: die Lautstaerke dieses Servers hat sich geaendert.

        Ohne das wirkte ein Klick im Web-Panel NIE: die Einstellung wurde nur
        beim ANLEGEN eines Players gelesen, und Player werden nie weggeraeumt.
        Wer also einmal Musik gehoert hatte, behielt seine Lautstaerke bis zum
        Neustart - waehrend 'flo ls 80' sofort griff. Genau dieser Unterschied
        ist gemeint, wenn es heisst, es soll synchron sein."""
        player = self._players.get(int(gid or 0)) or self._players.get(gid)
        if player is not None:
            self._lautstaerke_anwenden(player, self._start_lautstaerke(gid))

    @staticmethod
    def _start_lautstaerke(guild_id):
        """Womit dieser Server zu spielen anfaengt (guildcfg 'lautstaerke').

        Jeder Server stellt seine eigene ein - auf dem einen ist Flo im
        Hintergrund, auf dem anderen laut. Der Wert steht in Prozent."""
        try:
            prozent = guildcfg.get(guild_id, "lautstaerke")
            if prozent is None:
                return DEFAULT_VOLUME
            return max(0.0, min(2.0, float(prozent) / 100.0))
        except Exception:  # noqa: BLE001 - Musik laeuft auch ohne Einstellung
            return DEFAULT_VOLUME

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

    # Was yt-dlp im Fehlerfall sagt -> was der Nutzer wissen muss. Vorher gab es
    # fuer JEDEN Grund denselben Satz ("Den Song konnte ich nicht laden"), und der
    # Grund verschwand in einem Traceback. Ob YouTube gerade nach einem Login
    # fragt, das Video geloescht ist, oder yt-dlp schlicht veraltet ist, war von
    # aussen nicht zu unterscheiden - man konnte nur raten.
    #
    # Reihenfolge zaehlt: die spezifischen Muster stehen vorn.
    _YT_GRUENDE = (
        # 'alter' MUSS vor 'botcheck' stehen: YouTube sagt bei beidem
        # "Sign in to confirm ..." - nachgemessen landete die Altersfreigabe
        # sonst beim Bot-Check und der Nutzer bekam den falschen Rat.
        ("alter", ("age-restricted", "age restricted", "confirm your age",
                   "inappropriate for some users")),
        ("botcheck", ("not a bot", "confirm you're not a bot",
                      "confirm youre not a bot", "sign in to confirm",
                      "cookies-from-browser", "use --cookies")),
        ("land", ("available in your country", "geo restricted", "geo-restricted",
                  "blocked it in your country", "not available from your location",
                  "who has blocked it in your country")),
        ("weg", ("video unavailable", "private video", "has been removed",
                 "no longer available", "account associated with this video "
                 "has been terminated", "this video is unavailable")),
        ("drm", ("drm protection", "drm-protected")),
        ("limit", ("http error 429", "too many requests", "rate limit")),
        ("veraltet", ("please report this issue", "confirm you are on the latest",
                      "unable to extract", "failed to parse json",
                      "unable to download api page", "nsig extraction failed",
                      "signature extraction failed")),
        ("netz", ("unable to download webpage", "connection", "timed out",
                  "timeout", "temporary failure in name resolution",
                  "network is unreachable", "tunnel connection failed")),
        ("nichts", ("keine treffer", "no video results", "no results")),
        ("format", ("requested format is not available",
                    "no video formats found")),
    )

    _YT_SAETZE = {
        "botcheck": "YouTube will gerade einen Login sehen und haelt mich fuer "
                    "einen Bot. Das liegt nicht an dir und geht meist von selbst "
                    "wieder weg.",
        "alter": "Das Video ist altersbeschraenkt - da komme ich ohne Konto nicht ran.",
        "weg": "Das Video gibt es nicht mehr (geloescht oder privat).",
        "land": "Das Video ist in Deutschland gesperrt.",
        "drm": "Diese Seite ist kopiergeschuetzt, da komme ich nicht ran.",
        "limit": "YouTube drosselt mich gerade. Gib mir ein paar Minuten.",
        "veraltet": "Mein YouTube-Modul ist zu alt fuer die aktuelle Seite - "
                    "der Chef muss `pip install -U yt-dlp` machen.",
        "netz": "Ich komme gerade nicht ins Netz. Versuch es gleich nochmal.",
        "nichts": "Dazu habe ich nichts gefunden. Probier andere Suchwoerter.",
        "format": "Von dem Video gibt es keine abspielbare Tonspur.",
        "unbekannt": "Den Song konnte ich nicht laden. Probier einen anderen Link "
                     "oder Suchbegriff.",
    }

    @classmethod
    def yt_fehler_deuten(cls, exc):
        """(art, satz) zu einer yt-dlp-Ausnahme. Nie werfen - im Zweifel 'unbekannt'."""
        text = f"{exc}".lower()
        for art, muster in cls._YT_GRUENDE:
            if any(m in text for m in muster):
                return art, cls._YT_SAETZE[art]
        return "unbekannt", cls._YT_SAETZE["unbekannt"]

    # YouTube prueft seit Jahren, ob da ein echter Browser sitzt. Welcher
    # "player_client" ohne Login durchkommt, aendert sich alle paar Monate -
    # genau deshalb steht hier KEIN fester Name im Code, sondern eine Reihe.
    # Kommt der Standard nicht durch, probiert Flo die Reihe durch und merkt
    # sich, was ging. Ein Name, den die installierte yt-dlp-Fassung gar nicht
    # kennt, wird vorher aussortiert (sonst waere die Ausweichliste selbst der
    # naechste Fehler).
    # Reihenfolge ist NICHT beliebig. YouTube verlangt inzwischen fuer die
    # meisten Clients ein "PO Token", das yt-dlp selbst gar nicht erzeugen kann.
    # Genau drei Clients kommen laut yt-dlp OHNE so ein Token aus - und nur die
    # koennen auf einem nackten Server ueberhaupt noch klappen. Die stehen
    # deshalb vorne, der Rest ist nur noch Resthoffnung.
    _OHNE_POT = ("tv", "android_vr", "web_embedded")
    _CLIENT_REIHE = ("tv", "android_vr", "web_embedded", "tv_simply", "ios",
                     "mweb", "web_safari", "android")
    # Nur bei diesen Gruenden hilft ein anderer Client. Bei "geloescht",
    # "gesperrt" oder "nichts gefunden" waere jeder weitere Versuch nur Wartezeit
    # fuer den Nutzer.
    _CLIENT_HILFT = ("botcheck", "format", "veraltet", "unbekannt")

    @staticmethod
    def _pot_tokens():
        """Manuell hinterlegte PO Tokens (YTDLP_PO_TOKEN), mehrere per Komma.

        YouTube verlangt fuer die meisten player_clients ein "PO Token".
        yt-dlp kann so eines NICHT selbst erzeugen - es muss von aussen kommen,
        entweder aus dem Browser abgeschrieben oder von einem Anbieter-Plugin
        (bgutil-ytdlp-pot-provider) erzeugt. Format je Token:

            YTDLP_PO_TOKEN=web.gvs+XXXX,web_safari.gvs+YYYY
        """
        roh = os.getenv("YTDLP_PO_TOKEN", "").strip()
        return [t.strip() for t in roh.split(",") if t.strip()]

    @staticmethod
    def pot_anbieter_da():
        """Laeuft ein PO-Token-Anbieter-Plugin mit? Nur fuer die Diagnose."""
        try:
            import importlib.util
            return any(importlib.util.find_spec(n) is not None
                       for n in ("bgutil_ytdlp_pot_provider",
                                 "yt_dlp_plugins.extractor.getpot_bgutil"))
        except Exception:  # noqa: BLE001 - Diagnose darf nie etwas umwerfen
            return False

    @classmethod
    def _extractor_args(cls, client):
        """extractor_args fuer yt-dlp: player_client UND po_token zusammen.

        Beides landet unter demselben Schluessel "youtube". Wer nur eines davon
        setzt, loescht das andere - genau das war hier der Fehler."""
        yt = {}
        if client:
            yt["player_client"] = [client]
        tokens = cls._pot_tokens()
        if tokens:
            yt["po_token"] = tokens
        return {"youtube": yt} if yt else {}

    @staticmethod
    def _netz_optionen():
        """Ein eigener Ausgang NUR fuer yt-dlp (YTDLP_PROXY).

        YouTubes Bot-Pruefung haengt an der IP, nicht am Bot. Wer einen zweiten
        Weg ins Netz hat - VPN, ein kleiner Server woanders, ein Handy-Hotspot -
        kommt damit wieder an YouTube heran, ohne irgendwo ein Konto zu
        hinterlegen. Betrifft ausdruecklich nur die Musik-Aufloesung; Discord und
        die KI laufen weiter direkt.

            YTDLP_PROXY=http://benutzer:passwort@host:3128
            YTDLP_PROXY=socks5://127.0.0.1:1080
        """
        proxy = os.getenv("YTDLP_PROXY", "").strip()
        return {"proxy": proxy} if proxy else {}

    @classmethod
    def _cookie_optionen(cls):
        """Cookies fuer yt-dlp, falls eingerichtet.

        YouTubes Bot-Pruefung laesst sich mit keinem player_client mehr umgehen,
        wenn die IP einmal markiert ist. Dann bleibt nur ein angemeldeter
        Zugang - das ist auch der offizielle Rat von yt-dlp selbst.

            YTDLP_COOKIES=/opt/flobot/cookies.txt
            YTDLP_COOKIES_FROM_BROWSER=firefox        (nur mit Browser auf dem Server)

        WARNUNG, die man nicht verschweigen darf: nimm dafuer einen
        WEGWERF-Account. YouTube sperrt Konten, deren Cookies von einem Server
        aus benutzt werden - der Haupt-Account waere dann weg.
        """
        opts = {}
        datei = os.getenv("YTDLP_COOKIES", "").strip()
        if datei and not os.path.isfile(datei):
            log.warning("YTDLP_COOKIES zeigt auf %r - da liegt keine Datei. "
                        "Suche stattdessen selbst nach cookies.txt.", datei)
            datei = ""
        if not datei:
            datei = cls._cookie_datei_finden()
        if datei:
            opts["cookiefile"] = datei
        browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
        return opts

    # Ohne .env-Gefummel: liegt hier eine cookies.txt, wird sie benutzt. Der
    # Betreiber sitzt womoeglich am Handy - eine Datei ablegen kann er, eine
    # .env-Zeile tippen kaum.
    _COOKIE_ORTE = ("cookies.txt", "youtube.txt", "youtube_cookies.txt")

    @classmethod
    def _cookie_datei_finden(cls):
        """Sucht eine Cookie-Datei an den Stellen, wo sie ein Mensch ablegen wuerde.

        Gibt den Pfad zurueck oder "". Leere Dateien werden uebergangen - eine
        angefangene, aber nie gefuellte cookies.txt darf nicht dafuer sorgen,
        dass yt-dlp mit leerem Zugang losrennt und alles scheitert."""
        hier = os.path.dirname(os.path.abspath(__file__))
        ordner = [hier, os.path.join(hier, "data"),
                  os.getenv("DATA_DIR", "").strip() or hier]
        for ordner_pfad in ordner:
            for name in cls._COOKIE_ORTE:
                pfad = os.path.join(ordner_pfad, name)
                try:
                    if os.path.isfile(pfad) and os.path.getsize(pfad) > 0:
                        return pfad
                except OSError:
                    continue
        return ""

    @staticmethod
    def _bekannte_clients():
        """Die player_client-Namen, die DIESE yt-dlp-Fassung wirklich kennt."""
        try:
            from yt_dlp.extractor.youtube import _base
            return set(_base.INNERTUBE_CLIENTS)
        except Exception:  # noqa: BLE001 - dann eben ungefiltert
            return None

    def client_reihe(self):
        """Reihenfolge der Ausweich-Clients. YTDLP_PLAYER_CLIENT setzt sie fest."""
        fest = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
        if fest:
            return [fest]
        bekannt = self._bekannte_clients()
        reihe = [c for c in self._CLIENT_REIHE if bekannt is None or c in bekannt]
        # Was zuletzt funktioniert hat, zuerst.
        if self._guter_client and self._guter_client in reihe:
            reihe.remove(self._guter_client)
            reihe.insert(0, self._guter_client)
        return reihe

    @staticmethod
    def _suchtext(eingabe):
        """Der reine Suchtext aus einer yt-dlp-Eingabe - oder "" bei einer URL.

        Aus 'ytsearch1:rick astley' wird 'rick astley'. Den braucht die
        Ausweichquelle: SoundCloud kann mit einer YouTube-Adresse nichts
        anfangen, mit dem Suchtext dahinter schon."""
        roh = (eingabe or "").strip()
        if "://" in roh:
            return ""
        treffer = re.match(r"^yt(?:search)?\d*:(.+)$", roh, re.IGNORECASE)
        return (treffer.group(1) if treffer else roh).strip()

    # Ausweich auf SoundCloud abschaltbar. Wer YouTube WILL, bekommt sonst
    # stillschweigend etwas anderes - und merkt nicht, dass YouTube klemmt.
    # MUSIC_SOUNDCLOUD_FALLBACK=0 heisst: lieber eine ehrliche Fehlermeldung.
    @staticmethod
    def _ausweich_erlaubt():
        return os.getenv("MUSIC_SOUNDCLOUD_FALLBACK", "1").strip().lower() not in (
            "0", "false", "no", "off", "aus")

    async def _soundcloud_ausweich(self, text):
        """Denselben Song bei SoundCloud suchen. SoundCloud kennt YouTubes
        Bot-Pruefung nicht - ist die Server-IP dort markiert, ist das der
        einzige Weg, der OHNE Zutun des Betreibers noch Musik liefert."""
        if not text or not self._ausweich_erlaubt():
            return None
        loop = asyncio.get_running_loop()

        def work():
            opts = dict(_YDL_OPTS)
            opts.update(self._cookie_optionen())
            opts.update(self._netz_optionen())
            opts["default_search"] = "scsearch"
            with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[union-attr]
                info = ydl.extract_info(f"scsearch1:{text}", download=False)
            if info and "entries" in info:
                treffer = [e for e in info["entries"] if e]
                if not treffer:
                    raise ValueError("keine Treffer")
                info = treffer[0]
            return info

        try:
            return await loop.run_in_executor(None, work)
        except Exception as exc:  # noqa: BLE001 - Ausweich darf scheitern
            log.warning("Musik: SoundCloud-Ausweich fuer %r ging auch nicht (%s).",
                        text[:60], f"{exc}".replace("\n", " ")[:120])
            return None

    async def _mit_clientwechsel(self, work, was="YouTube"):
        """Fuehrt eine yt-dlp-Abfrage aus und wechselt bei Bot-Sperre den Client.

        'work' bekommt den player_client (oder None fuer yt-dlps Vorgabe) und
        laeuft im Executor. Die Suche und das Auflisten von Playlists brauchen
        genau dieselbe Behandlung wie das Abspielen: wird die Suche geblockt,
        kommt es nie bis zum Abspielen. Genau das fehlte hier."""
        loop = asyncio.get_running_loop()
        fest = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
        versuche = [fest] if fest else [None, *self.client_reihe()]
        letzter = None
        for nr, client in enumerate(versuche):
            try:
                ergebnis = await loop.run_in_executor(None, work, client)
            except Exception as exc:  # noqa: BLE001 - wird eingeordnet
                art, _satz = self.yt_fehler_deuten(exc)
                letzter = exc
                if art not in self._CLIENT_HILFT or nr == len(versuche) - 1:
                    raise
                log.warning("%s: blockt (%s) mit client=%s - versuche %s.",
                            was, art, client or "Standard",
                            versuche[nr + 1] or "Standard")
                continue
            if client and client != self._guter_client:
                self._guter_client = client
            return ergebnis
        raise letzter or RuntimeError("keine Aufloesung moeglich")

    async def _extract(self, query_or_url, ausweich_text=None):
        """Loest einen YouTube-Link ODER Suchtext zu einem abspielbaren Track auf.

        Scheitert es an YouTubes Bot-Pruefung, wird erst mit einem anderen
        player_client nachgesetzt - und wenn YouTube gar nichts mehr durchlaesst,
        derselbe Song bei SoundCloud gesucht, statt aufzugeben."""
        loop = asyncio.get_running_loop()

        def work(client, format_lax=False):
            opts = dict(_YDL_OPTS)
            opts.update(self._cookie_optionen())
            opts.update(self._netz_optionen())
            if format_lax:
                # Manche Clients kommen durch die Bot-Pruefung, liefern aber keine
                # reine Tonspur ("Requested format is not available"). Dann lieber
                # ein Video nehmen und den Ton daraus ziehen, als den Client
                # wegzuwerfen - er ist ja gerade der einzige, der ueberhaupt
                # durchkommt. ffmpeg verwirft das Bild sowieso (-vn).
                opts["format"] = "bestaudio/best/worst"
            args = self._extractor_args(client)
            if args:
                opts["extractor_args"] = args
            with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[union-attr]
                info = ydl.extract_info(query_or_url, download=False)
            if info and "entries" in info:  # Suche/Playlist -> ersten Treffer nehmen
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise ValueError("keine Treffer")
                info = entries[0]
            return info

        fest = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
        # Erst so, wie yt-dlp es selbst fuer richtig haelt (ausser es ist
        # festgenagelt), dann die Ausweichliste.
        versuche = ([fest] if fest else [None, *self.client_reihe()])
        letzter = None
        for nr, client in enumerate(versuche):
            try:
                info = await loop.run_in_executor(None, work, client)
            except Exception as exc:  # noqa: BLE001 - hier wird eingeordnet
                art, _satz = self.yt_fehler_deuten(exc)
                if art == "format":
                    # Dieser Client IST durchgekommen - nur das Format passte
                    # nicht. Ihn jetzt fallenzulassen waere der Fehler: er ist
                    # vielleicht der einzige, den YouTube noch durchlaesst.
                    try:
                        info = await loop.run_in_executor(None, work, client, True)
                    except Exception as exc2:  # noqa: BLE001
                        art, _satz = self.yt_fehler_deuten(exc2)
                        exc = exc2
                    else:
                        log.warning("Musik: client=%s hat keine reine Tonspur - "
                                    "nehme das Video und ziehe den Ton heraus.",
                                    client or "Standard")
                        letzter = None
                        if client and client != self._guter_client:
                            self._guter_client = client
                            log.warning("Musik: YouTube ging erst mit "
                                        "player_client=%r. Dauerhaft machen mit  "
                                        "YTDLP_PLAYER_CLIENT=%s  in der .env.",
                                        client, client)
                        break
                letzter = exc
                if art not in self._CLIENT_HILFT or nr == len(versuche) - 1:
                    if art == "botcheck":
                        # YouTube ist dicht. Bevor der Nutzer eine Fehlermeldung
                        # bekommt: denselben Song bei SoundCloud suchen.
                        text = ausweich_text or self._suchtext(query_or_url)
                        info = await self._soundcloud_ausweich(text)
                        if info:
                            log.warning("Musik: YouTube blockt komplett - spiele "
                                        "%r von SoundCloud.",
                                        (info.get("title") or text)[:60])
                            letzter = None
                            break
                    if art == "botcheck" and not self._cookie_optionen():
                        log.error("Musik: YouTube laesst KEINEN player_client mehr "
                                  "durch. Letzter Ausweg sind Cookies eines "
                                  "WEGWERF-Kontos: YTDLP_COOKIES=/opt/flobot/"
                                  "cookies.txt in die .env. Siehe 'k m'.")
                    raise
                log.warning("Musik: YouTube blockt (%s) mit client=%s - versuche %s.",
                            art, client or "Standard",
                            versuche[nr + 1] or "Standard")
                continue
            if client and client != self._guter_client:
                self._guter_client = client
                log.warning("Musik: YouTube ging erst mit player_client=%r. "
                            "Dauerhaft machen mit  YTDLP_PLAYER_CLIENT=%s  in der "
                            ".env.", client, client)
            break
        else:  # pragma: no cover - die Schleife bricht immer per return/raise ab
            raise letzter or RuntimeError("keine Aufloesung moeglich")
        stream_url = info.get("url")
        if not stream_url:
            raise ValueError("kein abspielbarer Stream gefunden")
        return Track(
            title=info.get("title", "Unbekannter Titel"),
            stream_url=stream_url,
            webpage_url=info.get("webpage_url", ""),
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail") or "",
            geloest_um=time.monotonic(),
            kopfzeilen=dict(info.get("http_headers") or {}),
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
        opts = dict(_YDL_OPTS)
        opts.update(self._cookie_optionen())
        opts.update(self._netz_optionen())
        opts["noplaylist"] = True
        opts["extract_flat"] = "in_playlist"

        def work(client):
            eigen = dict(opts)
            args = self._extractor_args(client)
            if args:
                eigen["extractor_args"] = args
            with yt_dlp.YoutubeDL(eigen) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(
                    f"ytsearch{_SPOTIFY_SEARCH_N}:{query}", download=False)

        try:
            info = await self._mit_clientwechsel(work, "YouTube-Suche")
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
                try:
                    # Der Suchtext wandert MIT: scheitert YouTube ganz, kann die
                    # SoundCloud-Ausweichquelle sonst nichts anfangen mit einer
                    # nackten Video-Adresse.
                    return await self._extract(vid, ausweich_text=hint["query"])
                except Exception:  # noqa: BLE001
                    # Der Docstring verspricht den Rueckfall - der fehlte hier:
                    # war das beste Video nicht ladbar (gesperrt, geloescht),
                    # flog der ganze Song raus, statt den Ersttreffer zu nehmen.
                    log.warning("Best-Match-Video nicht ladbar, nehme den "
                                "normalen Treffer: %s", vid)
        return await self._extract(
            extract_input,
            ausweich_text=(hint or {}).get("query") or self._suchtext(extract_input))

    async def _resolve_track(self, track):
        """Loest einen vorgemerkten Track auf. track.query = komplette yt-dlp-Eingabe
        (direkte URL ODER 'ytsearch1:Kuenstler - Titel'); track.match_hint bringt bei
        Spotify-Songs die Metadaten fuer die Best-Match-Auswahl mit."""
        resolved = await self._resolve_input(track.query, track.match_hint)
        resolved.requested_by = track.requested_by
        resolved.query = track.query
        # Den Hint MITNEHMEN. Ohne ihn waehlt das naechste Aufloesen desselben
        # Tracks (Watchdog-Neustart, Auffrischung) wieder blind den ersten
        # YouTube-Treffer - und dann laeuft ploetzlich ein Sped-Up-Remix statt
        # des Songs, den jemand angefragt hat.
        resolved.match_hint = track.match_hint
        return resolved

    @staticmethod
    def _einreihen(player, track):
        """Track hinten anhaengen. Ein neuer Song hebt eine frueher aufgegebene
        Warteschlange auf - vielleicht laesst der sich ja laden."""
        player.queue.append(track)
        player._advance_aufgegeben = False

    def _lazy_track(self, extract_input, title, requested_by, hint=None):
        """Noch nicht aufgeloester Track (wird erst beim Abspielen geladen).
        extract_input = yt-dlp-Eingabe (URL oder 'ytsearch1:...'), title = Anzeigename,
        hint = optionale Spotify-Metadaten fuer die Best-Match-Auswahl."""
        return Track(
            title=title, stream_url="", query=extract_input, requested_by=requested_by,
            match_hint=hint,
        )

    async def _flache_playlist(self, url, *, quelle="Playlist"):
        """Playlist/Set -> Liste (track_url, titel), OHNE die einzelnen Tracks
        schon aufzuloesen (extract_flat) - das passiert erst beim Abspielen.

        Gilt fuer YouTube UND SoundCloud: yt-dlp liefert bei beiden dieselbe
        flache Struktur. Der einzige Unterschied ist, dass YouTube pro Eintrag
        manchmal nur die Video-ID mitschickt - daraus bauen wir die volle URL.
        SoundCloud liefert immer die komplette Track-Adresse."""
        opts = dict(_YDL_OPTS)
        opts.update(self._cookie_optionen())
        opts.update(self._netz_optionen())
        opts["noplaylist"] = False
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = MAX_QUEUE
        opts["ignoreerrors"] = True  # einzelne kaputte Tracks ueberspringen, nicht crashen

        def work(client):
            eigen = dict(opts)
            args = self._extractor_args(client)
            if args:
                eigen["extractor_args"] = args
            with yt_dlp.YoutubeDL(eigen) as ydl:  # type: ignore[union-attr]
                return ydl.extract_info(url, download=False)

        try:
            info = await self._mit_clientwechsel(work, quelle)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s nicht ladbar (%s): %s", quelle, url, exc)
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
                # Nur YouTube liefert blosse IDs.
                vid = f"https://www.youtube.com/watch?v={vid}"
            out.append((vid, e.get("title", "Unbekannter Titel")))
        return out or None

    async def _youtube_playlist(self, url):
        """YouTube-Playlist -> Liste (video_url, titel)."""
        return await self._flache_playlist(url, quelle="YouTube-Playlist")

    async def _soundcloud_set(self, url):
        """SoundCloud-Set -> Liste (track_url, titel)."""
        return await self._flache_playlist(url, quelle="SoundCloud-Set")

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

    async def _spotify_kurzlink(self, url):
        """spotify.link/xxx -> die echte open.spotify.com-Adresse (oder None).

        Der Kurzlink ist eine reine Weiterleitung; wir brauchen nur das Ziel.
        Bewusst OHNE Token: das geht auch, wenn gar keine Spotify-Keys gesetzt
        sind - dann greift danach zwar die Track-Aufloesung nicht, aber der Bot
        sagt wenigstens ehrlich, woran es liegt, statt YouTube nach der URL
        abzusuchen."""
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, allow_redirects=True) as r:
                    ziel = str(r.url)
        except (aiohttp.ClientError, OSError) as exc:
            log.warning("Spotify-Kurzlink nicht aufloesbar (%s): %s", url, exc)
            return None
        if "spotify.com" not in ziel.lower():
            log.warning("Spotify-Kurzlink zeigt nicht auf Spotify: %s", ziel)
            return None
        return ziel

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

        Aktionen: play, search, spotify_album, spotify_playlist, spotify_kurz,
                  yt_playlist, sc_playlist,
                  volume, skip, pause, resume, stop, leave, queue.
        """
        # 1) Link in der Nachricht? (staerkstes Signal)
        for url in _URL_RE.findall(text):
            url = _url_saeubern(url)
            low = url.lower()
            if _SPOTIFY_KURZ_RE.match(url):
                # Kurzlink der Handy-App - das Ziel kennt erst der Redirect.
                return ("spotify_kurz", url)
            m = _SPOTIFY_LIST_RE.search(url)
            if m:
                kind = (m.group(1) or m.group(2) or "").lower()
                return ("spotify_album" if kind == "album" else "spotify_playlist", url)
            if "youtube.com" in low or "youtu.be" in low:
                # Benennt der Link ein VIDEO, ist das Video gemeint - auch wenn
                # eine Playlist danebensteht.
                #
                # Vorher lief das andersherum, und das war der Hauptgrund fuer
                # "YouTube-Links gehen nur halb": wer einen Song AUS einer
                # Playlist teilt, schickt watch?v=DERSONG&list=PL...&index=17 -
                # und Flo spielte dann Track 1 der Playlist, also einen ganz
                # anderen Song. Bei list=WL (Spaeter ansehen) oder list=LL
                # (Mag ich) kam sogar gar nichts: an diese Listen kommt der Bot
                # nicht heran, und der Fehler beendete den ganzen Befehl.
                #
                # Eine reine Playlist-Adresse (youtube.com/playlist?list=...)
                # benennt kein Video und wird weiterhin als Liste gespielt.
                lm = _YT_LIST_RE.search(url)
                if (lm and not lm.group(1).upper().startswith("RD")
                        and not _YT_VIDEO_RE.search(url)):
                    return ("yt_playlist", url)
                return ("play", url)
            if _SPOTIFY_TRACK_RE.search(url):
                return ("play", url)
            if _AUDIO_DATEI_RE.search(url):
                # Direkter Audio-Link (.mp3/.m4a/...) - den kann FFmpeg selbst
                # abspielen. Vorher landete auch der in der YouTube-TEXTSUCHE,
                # also in einer Suche nach der URL-Zeichenkette. Bewusst NUR
                # bei eindeutigen Audio-Endungen: eine beliebige Webseite im
                # Satz ("was haeltst du von https://…") darf die Musik nicht
                # an sich reissen.
                return ("play", url)
            if "spotify.com" in low or low.startswith("spotify:"):
                # Auffangnetz: JEDE Spotify-Adresse, die keiner der Zweige
                # oben kennt (Podcast-Episode, Show, Kuenstler-Seite, die alte
                # /user/<name>/playlist/-Form), landete bisher in der
                # YouTube-TEXTSUCHE - Flo suchte woertlich nach der URL und
                # spielte irgendein fremdes Video. Lieber ehrlich sagen, dass
                # es nicht geht.
                return ("spotify_unbekannt", url)
            if _SC_RE.match(url):
                # Set -> Playlist, alles andere ganz normal als Track. Ein
                # Kurzlink (on.soundcloud.com) KANN auch ein Set sein - das
                # sieht man erst nach dem Redirect; das faengt _extract ab.
                return ("sc_playlist" if _SC_SET_RE.match(url) else "play", url)

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
            self._einreihen(player, track)
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

        deckel = max_queue(player.guild_id)
        space = deckel - len(player.queue)
        if space <= 0:
            return self._embed(f"Die Warteschlange ist voll ({deckel}). Warte kurz.",
                               color=_COL_ERR)
        items = items[:space]

        if player.is_active():
            for item in items:
                inp, title, hint = self._unpack_item(item)
                self._einreihen(player, self._lazy_track(inp, title, requested_by, hint))
            return self._embed(
                f"**{len(items)}** Songs {label} eingereiht – ab **#{len(player.queue) - len(items) + 1}** "
                f"in der Warteschlange.",
                title="➕  Zur Warteschlange hinzugefügt", color=_COL_QUEUE,
            )

        # Der erste Song entscheidet NICHT mehr ueber die ganze Liste. Vorher
        # ging bei einem gesperrten/geloeschten ersten Titel die komplette
        # Playlist verloren ("Den ersten Song konnte ich nicht laden") - mit 49
        # einwandfreien Songs dahinter. Jetzt sucht Flo den ersten, der laeuft.
        track = None
        uebersprungen = 0
        rest = list(items)
        while rest:
            first_inp, first_title, first_hint = self._unpack_item(rest.pop(0))
            try:
                track = await self._resolve_input(first_inp, first_hint)
            except Exception:  # noqa: BLE001
                log.warning("Playlist: '%s' nicht ladbar - nehme den naechsten.",
                            first_title or first_inp)
                uebersprungen += 1
                if uebersprungen >= ADVANCE_MAX_FEHLER:
                    # Zwei am Stueck sind kein Zufall mehr, sondern das Netz.
                    # Dann NICHT die restliche Liste durchbrennen.
                    break
                continue
            track.requested_by = requested_by
            track.query = first_inp
            track.match_hint = first_hint
            break
        if track is None:
            return self._embed(
                "Von dieser Liste konnte ich gerade keinen Song laden (Netz? "
                "gesperrt?). Versuch's gleich nochmal.", color=_COL_ERR)
        for item in rest:
            inp, title, hint = self._unpack_item(item)
            self._einreihen(player, self._lazy_track(inp, title, requested_by, hint))
        try:
            await player._warte_bis_still()
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
        # Die View AUSDRUECKLICH beenden. Sie laeuft mit timeout=None und wird
        # von discord.py sonst nie aus dem ViewStore genommen - auch das Loeschen
        # der Nachricht raeumt dort nichts weg. Ohne das sammelte sich pro
        # gespieltem Song ein Eintrag an (gemessen: 200 Panels = 200 Eintraege,
        # die auch nach dem Loeschen aller Referenzen blieben).
        view = player.panel_view
        player.panel_view = None
        if view is not None:
            try:
                view.stop()
            except Exception:  # noqa: BLE001 - Aufraeumen darf nie stoeren
                pass
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
        # Sende-Generation: zwischen dem Absenden und der Antwort von Discord
        # koennen Sekunden liegen. Startet in dieser Luecke schon der naechste
        # Song (Doppel-Skip), speicherte frueher der SPAETER zurueckkehrende,
        # aeltere Aufruf sein Panel - das neuere blieb als Zombie mit
        # klickbaren Knoepfen im Kanal stehen und seine View (timeout=None)
        # leckte im ViewStore.
        player._panel_gen += 1
        meine_gen = player._panel_gen
        await self._retire_panel(player)
        emb = self._now_playing_embed(track, len(player.queue), extra=extra, speed=player.speed)
        view = PlaybackControlView(player)
        try:
            if reply_to is not None:
                msg = await reply_to.reply(embed=emb, view=view, mention_author=False)
            elif player.text_channel is not None:
                msg = await player.text_channel.send(embed=emb, view=view)
            else:
                view.stop()
                return
        except discord.HTTPException as exc:
            log.error("Now-Playing-Panel fehlgeschlagen: %s", exc)
            view.stop()
            return
        view.message = msg
        if meine_gen != player._panel_gen:
            # Ueberholt worden: das hier ist das ALTE Panel. Selbst wegraeumen,
            # statt das aktuelle zu ueberschreiben.
            view.stop()
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            return
        player.panel_message = msg
        player.panel_view = view      # zum spaeteren Abmelden (_retire_panel)

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
            # Lauter/leiser darf JEDER - das gilt fuer diese Sitzung.
            self._lautstaerke_anwenden(player, new / 100)
            # MERKEN ist etwas anderes: das ist eine Server-Einstellung und
            # ueberlebt Neustart und 'flo stop'. Sie zu aendern darf nicht
            # jeder - sonst stellt einer die Vorgabe fuer alle um, weil ihm ein
            # Lied zu laut war. Dasselbe Recht wie im Web-Panel.
            gemerkt = False
            if guildcfg.darf(message):
                try:
                    await guildcfg.setzen(message.guild.id, "lautstaerke", str(new))
                    gemerkt = True
                except Exception:  # noqa: BLE001 - Musik laeuft auch ohne Speichern
                    log.exception("Lautstaerke konnte nicht gespeichert werden")
            bar = "🔉" if new < 50 else ("🔊" if new <= 100 else "📢")
            zusatz = ("" if gemerkt else
                      "\n_Nur für jetzt – dauerhaft merken darf, wer den Server "
                      "verwaltet._")
            return self._embed(f"Lautstärke steht jetzt auf **{new}%**.{zusatz}",
                               title=f"{bar}  Lautstärke", color=_COL_CTRL)

        if action in ("stop", "leave"):
            # Auch bei Voice-DESYNC aufraeumen. Vorher wurde hier abgebrochen,
            # wenn die Verbindung schon weg war - dann blieb aber
            # active_channel_id gesetzt, und der Watchdog (heal) holte den Bot
            # samt laufender Musik prompt zurueck, obwohl der Nutzer gestoppt hat.
            # Nur wenn WIRKLICH nichts mehr offen ist, gibt es den Hinweis.
            if (player.voice is None and player.active_channel_id is None
                    and not player.queue and player.current is None):
                return self._embed("Ich bin gerade in keinem Sprachkanal.", color=_COL_ERR)
            await player.disconnect()
            return self._embed("Musik gestoppt, Warteschlange geleert und raus aus dem Sprachkanal.",
                               title="⏹️  Gestoppt", color=_COL_INFO)

        if action == "skip":
            # Nicht an is_active() haengen: genau wenn ein Song HAENGT, will man
            # skippen - und dann meldete das hier "Ich spiele gerade nichts"
            # oder der Skip verpuffte. Es reicht, dass es etwas zu tun gibt.
            if player.current is None and not player.queue:
                return self._embed("Ich spiele gerade nichts.", color=_COL_ERR)
            skipped = player.current.title if player.current else ""
            await player.skip()
            desc = f"**{self._short(skipped, 90)}** übersprungen." if skipped else "Übersprungen."
            return self._embed(desc, title="⏭️  Skip", color=_COL_CTRL)

        if action == "pause":
            if player.ist_pausiert():
                # Ehrlich antworten statt "Ich spiele gerade nichts" - das las
                # sich, als waere die Musik weg, obwohl sie nur pausiert war.
                return self._embed(
                    f"Ist schon pausiert. `{self._bot_name} weiter` spielt weiter.",
                    title="⏸️  Pause", color=_COL_INFO)
            if player.voice is None or not player.voice.is_playing():
                return self._embed("Ich spiele gerade nichts.", color=_COL_ERR)
            player.pausieren()
            return self._embed(f"Pausiert. Sag `{self._bot_name} weiter`, wenn's weitergehen soll.",
                               title="⏸️  Pause", color=_COL_CTRL)

        if action == "resume":
            if not player.ist_pausiert():
                # Flo empfiehlt nach zwei Fehlschlaegen selbst "weiter" - dann
                # muss "weiter" auch etwas tun. Vorher kam hier "Da ist nichts
                # pausiert", und die stehengebliebene Warteschlange blieb
                # stehen: eine Sackgasse, aus der nur 'stop' herausfuehrte.
                if player.queue and not player.is_active():
                    await player._advance()          # setzt das Aufgeben zurueck
                    if player.current is not None:
                        return HANDLED               # _advance postet das Panel
                    return self._embed(
                        "Ich komme an die Songs gerade nicht ran – probier's "
                        "gleich nochmal oder wirf einen anderen Link rein.",
                        color=_COL_ERR)
                return self._embed("Da ist nichts pausiert.", color=_COL_ERR)
            player.fortsetzen()
            return self._embed("Weiter geht's.", title="▶️  Fortgesetzt", color=_COL_PLAY)

        # "mach mal Musik an" ohne konkreten Song: pausiert -> weiter, laeuft schon ->
        # kurzer Hinweis, sonst freundlich nach dem Wunsch-Song fragen.
        if action == "resume_or_hint":
            if player.ist_pausiert():
                player.fortsetzen()
                return self._embed("Weiter geht's.", title="▶️  Fortgesetzt", color=_COL_PLAY)
            if player.is_active():
                return self._embed("Läuft doch schon. 🎶", color=_COL_INFO)
            if player.queue:
                # Es warten Songs, es laeuft aber nichts - anstossen statt fragen.
                await player._advance()
                if player.current is not None:
                    return HANDLED
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

        if action == "spotify_unbekannt":
            return self._embed(
                "Von diesem Spotify-Link kann ich nichts abspielen – ich kann "
                "**Songs**, **Alben** und **Playlists**, aber keine Podcasts, "
                "Shows oder Künstler-Seiten.\n"
                f"Sag mir einfach, was du hören willst: `{self._bot_name} spiel "
                f"Künstler Titel`.",
                title="🎧  Damit kann ich nichts anfangen", color=_COL_ERR)

        # --- Kurzlink der Spotify-App: erst aufloesen, dann normal weiter ---
        if action == "spotify_kurz":
            ziel = await self._spotify_kurzlink(arg)
            if not ziel:
                return self._embed(
                    "Diesen Spotify-Kurzlink konnte ich nicht auflösen. Schick mir "
                    "den langen Link (`open.spotify.com/...`) oder such direkt: "
                    f"`{self._bot_name} spiel Künstler Titel`.", color=_COL_ERR)
            neu = self.parse_command(f"spiel {ziel}")
            # 'spotify_unbekannt' MUSS hier mit rein: sonst faellt ein Kurzlink
            # auf eine Podcast-/Kuenstler-Seite genau in das Loch zurueck, das
            # der Auffangzweig gerade geschlossen hat.
            if neu is None or neu[0] in ("spotify_kurz", "spotify_unbekannt"):
                return self._embed(
                    "Dieser Spotify-Link zeigt auf etwas, das ich nicht abspielen "
                    "kann (Podcast, Künstler-Seite?).", color=_COL_ERR)
            action, arg = neu

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

        if action == "sc_playlist":
            entries = await self._soundcloud_set(arg)
            if not entries:
                return self._embed("Das SoundCloud-Set konnte ich nicht laden "
                                   "(leer, privat oder nur fuer Abonnenten?).",
                                   color=_COL_ERR)
            return await self._play_many(
                player, voice_state.channel, entries,
                message.author.display_name, "aus dem SoundCloud-Set", reply_to=message,
            )

        deckel = max_queue(player.guild_id)
        if len(player.queue) >= deckel:
            return self._embed(f"Die Warteschlange ist voll ({deckel}). Warte kurz.",
                               color=_COL_ERR)

        # Track aufloesen (Spotify -> Suchtext, sonst Link/Text direkt)
        try:
            if action == "play" and _SPOTIFY_TRACK_RE.search(arg):
                meta = await self._spotify_track_meta(arg)
                if not meta:
                    return self._embed("Den Spotify-Link konnte ich nicht auflösen (Keys/Token?).",
                                       color=_COL_ERR)
                # Besten YouTube-Treffer per Dauer/Titel waehlen (statt blind den
                # ersten - der ist bei Spotify-Songs oft ein Sped-Up/Loop/Cover).
                hinweis = {"query": meta["query"], "dur": meta.get("dur"),
                           "title": meta["name"], "artist": meta.get("artist", "")}
                track = await self._resolve_input(f"ytsearch1:{meta['query']}", hinweis)
                # Womit sich dieser Track SPAETER erneuern laesst - und das ist
                # NICHT der Spotify-Link. yt-dlp kann Spotify gar nicht oeffnen
                # ("[DRM] The requested site is known to use DRM protection"),
                # es kennt nur die YouTube-Suche dahinter. Ohne diese zwei Zeilen
                # schrieb der Block weiter unten die Spotify-Adresse als Quelle
                # ein, und jede Wiederbelebung eines abgebrochenen Spotify-Songs
                # war von vornherein chancenlos.
                track.query = f"ytsearch1:{meta['query']}"
                track.match_hint = hinweis
            elif action == "play":
                # Kurzlinks aus der SoundCloud-App (on.soundcloud.com) koennen
                # auch auf ein SET zeigen - das sieht man erst NACH dem
                # Redirect. Ohne diese Pruefung haette Flo davon nur den ersten
                # Track gespielt und den Rest stillschweigend verschluckt.
                if "on.soundcloud.com" in arg.lower():
                    eintraege = await self._soundcloud_set(arg)
                    if eintraege and len(eintraege) > 1:
                        return await self._play_many(
                            player, voice_state.channel, eintraege,
                            message.author.display_name, "aus dem SoundCloud-Set",
                            reply_to=message,
                        )
                track = await self._extract(arg)
            else:  # search
                track = await self._extract(f"ytsearch1:{arg}")
        except Exception as exc:  # noqa: BLE001 - yt-dlp wirft viele verschiedene Fehler
            art, satz = self.yt_fehler_deuten(exc)
            # EINE greppbare Zeile mit dem echten Grund - der Traceback nur noch
            # fuer den Fall, den wir nicht einordnen konnten.
            log.warning("Musik-Fehler: %s bei %r - %s", art, arg,
                        f"{exc}".replace("\n", " ")[:220])
            if art == "unbekannt":
                log.exception("Musik-Fehler im Detail")
            return self._embed(satz, color=_COL_ERR)

        track.requested_by = message.author.display_name
        # Merken, WOMIT dieser Track aufgeloest wurde. Ohne das kann Flo eine
        # tote Stream-Adresse spaeter nicht erneuern: die Wiederbelebung nach
        # einem Abbruch und die Auffrischung veralteter Adressen brauchen beide
        # diese Eingabe. Bei Playlists steht sie laengst drin, bei einem
        # einzelnen Link fehlte sie.
        if not track.query:
            track.query = arg if action == "play" else f"ytsearch1:{arg}"

        try:
            await player.connect(voice_state.channel)
        except discord.ClientException as exc:
            log.error("Voice-Connect fehlgeschlagen: %s", exc)
            return self._embed("Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                               color=_COL_ERR)

        # Es laeuft schon was -> einreihen. Ab >=2 wartenden Songs gibt's Buttons,
        # mit denen die Person ihren frischen Song an eine Wunsch-Position zieht.
        if player.is_active():
            self._einreihen(player, track)
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
selbsttest = instance.selbsttest
spotify_selbsttest = instance.spotify_selbsttest
is_enabled = instance.is_enabled
_player_for = instance._player_for
heal_voice = instance.heal_voice
is_voice_busy = instance.is_voice_busy
_extract = instance._extract
_resolve_input = instance._resolve_input
_resolve_track = instance._resolve_track
_lazy_track = instance._lazy_track
max_queue = max_queue
_norm_match = instance._norm_match
_pick_best_match = instance._pick_best_match
_youtube_search_best = instance._youtube_search_best
_youtube_playlist = instance._youtube_playlist
_soundcloud_set = instance._soundcloud_set
_flache_playlist = instance._flache_playlist
_spotify_token = instance._spotify_token
_spotify_to_query = instance._spotify_to_query
_spotify_track_meta = instance._spotify_track_meta
_spotify_list_tracks = instance._spotify_list_tracks
_deep_find = instance._deep_find
_spotify_playlist_via_embed = instance._spotify_playlist_via_embed
_spotify_kurzlink = instance._spotify_kurzlink
_url_saeubern = _url_saeubern
_adresse_alt = _adresse_alt
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
