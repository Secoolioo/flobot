"""Musik-Feature fuer Flo: spielt YouTube-/Spotify-Links im Sprachkanal ab.

Funktionsweise:
- YouTube:  Link (oder Suchtext) -> yt-dlp zieht den Audio-Stream -> FFmpeg
            spielt ihn in den Voice-Channel. KEIN API-Key noetig.
- Spotify:  Spotify erlaubt KEIN direktes Audio-Streaming. Darum wird der Link
            ueber die Spotify-Web-API zu "Kuenstler - Titel" aufgeloest und das
            Ergebnis auf YouTube gesucht und abgespielt. Dafuer braucht es die
            SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET aus der .env.

Voraussetzungen (sonst ist das Feature einfach aus, siehe voice_fehlt):
- pip:    yt-dlp, PyNaCl   (PyNaCl = Voice-Verschluesselung fuer discord.py)
          davey            (ab discord.py 2.7 Pflicht: DAVE-Verschluesselung)
- System: ffmpeg, libopus  (z. B.  apt install ffmpeg libopus0)

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
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, replace

import aiohttp
import discord

import numfmt

import ai
import basis
from basis import FeatureBasis
import guildcfg
from store import JsonStore

try:  # Optional: Bot soll auch ohne yt-dlp starten.
    import yt_dlp
except ImportError:  # pragma: no cover - nur relevant ohne Paket
    yt_dlp = None  # type: ignore[assignment]

log = logging.getLogger("dcbot.music")

# Sentinel: das Modul hat selbst geantwortet (Embed + Buttons direkt gesendet).
# bot.py erkennt das und schickt KEINE zusaetzliche Antwort.
HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

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
# Obergrenze fuer 'flo loop <n>'. Wer wirklich ewig will, nimmt 'endlos' -
# das steht dann auch so im Panel und laesst sich nicht mit einem Vertipper
# ('loop 99999') aus Versehen bauen.
LOOP_MAX = 50
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


def _env_sekunden(name, vorgabe):
    """Sekunden aus der .env - ein Tippfehler dort darf den Start nicht kippen."""
    roh = os.getenv(name, "")
    try:
        return float(roh) if roh.strip() else float(vorgabe)
    except ValueError:
        log.warning("%s=%r ist keine Zahl - nehme %s.", name, roh, vorgabe)
        return float(vorgabe)


# So lange bleibt Flo im Sprachkanal, wenn es nichts mehr zu tun gibt (Song zu
# Ende, Warteschlange leer, nicht pausiert) oder nur noch Bots drinhocken.
# Danach geht er von selbst. Vorher blieb er fuer immer: _advance setzte nur
# current=None, der Watchdog hielt ihn im Kanal, und Soundboard/TTS sagten
# bis zum Neustart "Gerade läuft was im Voice". 0 (oder weniger) = nie gehen.
MUSIC_IDLE_SEKUNDEN = _env_sekunden("MUSIC_IDLE_SEKUNDEN", 300)
# Den naechsten Song schon aufloesen, waehrend der aktuelle laeuft. Vorher kam
# nach jedem Song eine Stille von 1-5 s: erst am Songende fragte Flo YouTube
# nach der Adresse des naechsten. MUSIC_VORLADEN=0 schaltet es ab.
MUSIC_VORLADEN = os.getenv("MUSIC_VORLADEN", "1").strip().lower() not in (
    "0", "false", "no", "off", "aus")
# So lange wartet der Songwechsel hoechstens auf ein laufendes Vorladen, bevor
# er selbst aufloest (sonst haengt ein zaeher Vorlade-Versuch den Wechsel auf).
VORLADEN_WARTEN = 20.0
# So lange wartet flo_getrennt, bevor es eine Trennung als Rauswurf wertet.
# discord.py trennt bei manchen Aussetzern SELBST kurz und verbindet neu -
# Discord meldet das genauso wie einen Moderator-Kick.
VOICE_RAUSWURF_FRIST = 4.0

# Der eine Satz, wenn Flo nicht in den Sprachkanal kommt - egal ob Rechte,
# Zeitueberschreitung oder fehlende Voice-Bibliothek. Vorher kam in drei von
# fuenf Wegen nur Discords RuntimeError/TimeoutError bei bot.py an, und die
# Leute lasen "Da ist gerade etwas schiefgelaufen." Der Grund steht im Log.
VOICE_KAPUTT = ("Ich komm nicht in euren Voice rein – Rechte fehlen oder Discord "
                "zickt. Sag's dem Admin, nicht mir.")
# Alles, was channel.connect() werfen kann, wenn es nicht klappt: fehlende
# Rechte/schon verbunden (ClientException), fehlendes davey/PyNaCl
# (RuntimeError), Handshake haengt (asyncio.TimeoutError).
VOICE_CONNECT_FEHLER = (discord.ClientException, RuntimeError, asyncio.TimeoutError)

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


def _loop_key(track):
    """Kennzeichen eines Songs fuer den Loop.

    Bewusst NICHT die Objekt-Identitaet: der Loop reiht eine KOPIE des Songs
    ein (siehe _advance_intern). Dasselbe Track-Objekt gleichzeitig in
    'current' und in der Warteschlange wuerde QueuePositionView.apply_move
    durcheinanderbringen - die sucht per 'is'."""
    if track is None:
        return ""
    return (getattr(track, "webpage_url", "") or getattr(track, "query", "")
            or getattr(track, "title", "") or "")


def _loop_text(rest):
    """Wie der Loop im Panel steht. Leerer Text = kein Loop aktiv."""
    if not rest:
        return ""
    return "🔁 endlos" if rest < 0 else f"🔁 noch {rest}×"


def _opus_da():
    """Kann discord.py Ton kodieren? Dafuer braucht es libopus.

    discord.py laedt sie erst beim ersten Abspielen nach - fehlt sie, merkt man
    das also erst, wenn schon jemand im Kanal auf Musik wartet. Hier wird genau
    das Laden versucht, das discord.py spaeter selbst versuchen wuerde."""
    try:
        return bool(discord.opus.is_loaded() or discord.opus._load_default())
    except Exception:  # noqa: BLE001 - interne API weg? Dann wenigstens suchen.
        import ctypes.util
        try:
            return ctypes.util.find_library("opus") is not None
        except Exception:  # noqa: BLE001
            return False


def voice_fehlt():
    """Warum Voice auf diesem Rechner NICHT geht - als fertiger Log-Satz mit
    dem Befehl, der es behebt. Leer = alles da.

    Vorher prueften Musik und Voice-Gags nur yt-dlp, ffmpeg und PyNaCl. Im Log
    stand "Musik-Feature aktiv", und dann scheiterte JEDER Beitritt: seit
    discord.py 2.7 ist 'davey' Pflicht (DAVE-Verschluesselung, von Discord seit
    01.03.2026 erzwungen), und ohne libopus kommt kein Ton heraus. Bemerkt hat
    man das erst im Kanal, als "Da ist gerade etwas schiefgelaufen."."""
    try:
        import nacl  # noqa: F401
    except ImportError:
        return ("Paket 'PyNaCl' fehlt (Voice-Verschluesselung). Nachinstallieren:  "
                "venv/bin/pip install PyNaCl")
    if tuple(discord.version_info[:2]) >= (2, 7):
        try:
            import davey  # noqa: F401
        except ImportError:
            return ("Paket 'davey' fehlt - ab discord.py 2.7 Pflicht fuer Voice "
                    "(DAVE, von Discord seit 01.03.2026 erzwungen). "
                    "Nachinstallieren:  venv/bin/pip install davey")
    if not _opus_da():
        return ("libopus fehlt - ohne die kodiert discord.py keinen Ton. "
                "Nachinstallieren:  sudo apt install libopus0")
    return ""


def _nur_bots_im_kanal(channel):
    """Sitzen in diesem Sprachkanal nur noch Bots (Flo eingeschlossen)?

    Im Zweifel NEIN: kann die Mitgliederliste nicht gelesen werden oder ist sie
    ganz leer (dann fehlt Discords Cache, nicht die Leute), bleibt Flo lieber
    drin, als jemandem mitten im Song den Stecker zu ziehen."""
    try:
        leute = list(channel.members)
    except Exception:  # noqa: BLE001 - unbekannt ist nicht leer
        return False
    if not leute:
        return False
    return all(getattr(m, "bot", False) for m in leute)


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

# --- Ist das WIRKLICH ein Befehl? -----------------------------------------
# Ein Steuerwort am Satzanfang reichte bisher. Gemessen hat das ganz normales
# Deutsch gekapert - und zwar mit Folgen:
#   'halt die fresse' / 'halt dein maul' / 'halt mal kurz'  -> STOP (Voice weg,
#                                                   Warteschlange geloescht!)
#   'hau ab du opfer' / 'raus mit der sprache'      -> Voice verlassen
#   'komm mal klar' / 'komm schon'                  -> Voice beitreten
#   'weiter so'  -> fortsetzen    'nächste frage' / 'nächstes mal' -> skip
# Lief keine Musik, bekam man statt Flos Antwort "Ich bin gerade in keinem
# Sprachkanal." Jetzt gilt: hinter dem Steuerwort duerfen nur Fuellwoerter und
# Satzzeichen stehen - oder eines der wenigen ECHTEN Objekte, die zu genau
# dieser Aktion gehoeren ('skip den song', 'verlass den kanal'). Alles andere ist
# ein Satz, und den beantwortet die KI.
_FUELLWOERTER = frozenset(("bitte", "mal", "jetzt", "flo", "sofort", "doch",
                           "schnell", "endlich", "halt"))

# Die echten Objekte. Mit Artikel oder ohne; 'die musik' ja, 'die fresse' nie.
_OBJ_MUSIK = (r"(?:(?:die|den|das|dem|der|diese[nmrs]?|the|this)\s+)?"
              r"(?:musik|music|mucke|mukke|song|songs|lied|track|titel|wiedergabe|"
              r"playback|gedudel)")
_OBJ_KANAL = (r"(?:(?:den|dem|der|das|diesen|diesem|the|this)\s+)?"
              r"(?:kanal|channel|voice|voicechannel|voicechat|sprachkanal|"
              r"sprachchat|call|vc|talk)")

# Steuerbefehle: (Aktion, Kopf-Regex am Satzanfang, erlaubte Objekte,
# Merkmale). Reihenfolge = Prioritaet.
# Wichtig: JEDES Kopf-Muster endet auf \b oder \w*\b. Ohne Wortgrenze reicht
# das blosse PRAEFIX - und dann kaperten die Steuerbefehle ganz normale Saetze:
# "verlass dich drauf" wurde zum Voice-Leave, und "rausschmeisen @wer" (die
# gaengige Ein-s-Schreibweise) liess Flo den Sprachkanal verlassen und die
# Musik abbrechen, statt die Person zu kicken.
#
# Merkmale:
#   "ohne_mal" - 'mal' ist hier kein Fuellwort: 'nächstes mal' heisst "beim
#                naechsten Mal", nicht "naechster Song".
#   "alltag"   - das Wort ist AUCH Alltagsdeutsch ('Flo hau ab', 'Flo halt',
#                'Flo weiter'). Laeuft gar keine Musik, ist es kein Musik-
#                befehl, sondern eine Ansage an Flo - handle() gibt dann an
#                die KI ab (siehe _NUR_MIT_MUSIK).
_CONTROL = [
    # 'skip 2' ging schon immer (und skippt einen) - die Zahl bleibt erlaubt.
    ("skip",   re.compile(r"^(?:skip|ueberspring\w*|überspring\w*|next)\b", re.I),
     rf"{_OBJ_MUSIK}|das|den|dies|this|it|[0-9]+(?:\s+(?:songs?|lieder|tracks?))?", ()),
    ("skip",   re.compile(r"^(?:naechst\w*|nächst\w*)\b", re.I),
     _OBJ_MUSIK, ("ohne_mal",)),
    ("pause",  re.compile(r"^(?:pause|pausier\w*)\b", re.I), _OBJ_MUSIK, ()),
    ("resume", re.compile(r"^(?:resume|fortsetz\w*|weiterspiel\w*)\b", re.I),
     rf"(?:mit\s+)?{_OBJ_MUSIK}", ()),
    ("resume", re.compile(r"^weiter\b", re.I),
     rf"(?:mit\s+)?{_OBJ_MUSIK}|spielen|abspielen", ("alltag",)),
    ("stop",   re.compile(r"^(?:stop|stopp)\b", re.I),
     rf"(?:mit\s+)?{_OBJ_MUSIK}|alles", ()),
    ("stop",   re.compile(r"^(?:aufhoer\w*|aufhör\w*|hoer auf|hör auf)\b", re.I),
     rf"mit\s+{_OBJ_MUSIK}|zu\s+spielen|mit\s+dem\s+abspielen", ("alltag",)),
    # 'halt' bekommt KEIN Objekt: 'halt die/dein ...' ist so gut wie nie die
    # Musik, sondern 'halt die fresse'. Nur 'halt' (+ Fuellwort) stoppt.
    ("stop",   re.compile(r"^halt\b", re.I), None, ("alltag",)),
    # Negative Vorschau gegen die Redewendung: "verlass dich drauf" /
    # "verlass dich nicht darauf" ist Gerede, kein Befehl zum Rausgehen.
    ("leave",  re.compile(r"^(?:leave|verlasse?(?!\s+(?:dich|euch|sich|mich|uns))|"
                          r"disconnect)\b", re.I),
     rf"(?:aus\s+)?{_OBJ_KANAL}", ()),
    ("leave",  re.compile(r"^(?:geh raus|hau ab|raus)\b", re.I),
     rf"aus\s+{_OBJ_KANAL}", ("alltag",)),
    # 'liste' zaehlt nur, wenn NICHTS dahinter steht: "liste mal auf, was du
    # kannst" ist eine Frage an die KI, keine Warteschlangen-Abfrage.
    ("queue",  re.compile(r"^(?:queue\b|warteschlange\b|liste\s*$)", re.I),
     r"(?:an)?zeigen|zeig|anzeigen|auflisten", ()),
    ("join",   re.compile(r"^(?:join\w*|connect|verbinde\w*|komm)\b", re.I),
     rf"rein|her|rüber|rueber|dazu|dich|(?:in\s+|zu\s+uns\s+in\s+){_OBJ_KANAL}"
     rf"|{_OBJ_KANAL}", ()),
]

# Markierung im Argument eines Steuerbefehls: das Wort ist auch Alltagsdeutsch
# und gilt nur, wenn hier wirklich Musik laeuft (siehe handle()).
_NUR_MIT_MUSIK = "nur_mit_musik"
# Rueckgabe von _steuerbefehl: ein Steuerwort steht vorn, dahinter aber ein Satz.
_EIN_SATZ = "ein_satz"


def _restwoerter(rest, ohne_mal=False):
    """Die Woerter hinter dem Befehlswort - ohne Satzzeichen, Emojis und
    Fuellwoerter. Leere Liste = da stand nur Beiwerk ('stop!', 'skip bitte')."""
    text = (rest or "").lower().replace("'", "").replace("’", "")
    woerter = re.findall(r"[^\W_]+", text)
    fuell = _FUELLWOERTER - {"mal"} if ohne_mal else _FUELLWOERTER
    try:
        # Flos eigener Name hinten dran ('stop, florian') ist Anrede, kein Inhalt.
        fuell = fuell | {n.lower() for n in ai.names()}
    except Exception:  # noqa: BLE001 - ohne Namensliste reicht 'flo'
        pass
    return [w for w in woerter if w not in fuell]


def _steuerbefehl(cleaned):
    """Steuerbefehl am Satzanfang?

    Rueckgabe: (aktion, argument) fuer einen EINDEUTIGEN Befehl - das Argument
    ist leer oder _NUR_MIT_MUSIK (Alltagswort, siehe _CONTROL). _EIN_SATZ, wenn
    zwar ein Steuerwort vorn steht, dahinter aber ein ganzer Satz. None, wenn
    gar kein Steuerwort vorn steht."""
    for action, kopf, objekte, merkmale in _CONTROL:
        m = kopf.match(cleaned)
        if not m:
            continue
        rest = _restwoerter(cleaned[m.end():], ohne_mal="ohne_mal" in merkmale)
        if rest and not (objekte and re.fullmatch(objekte, " ".join(rest), re.I)):
            # KEIN anderes Steuerwort mehr probieren: 'halt die fresse' soll
            # nicht bei einem spaeteren Muster doch noch durchrutschen.
            return _EIN_SATZ
        # Mit echtem Objekt ('raus aus dem voice') ist es eindeutig Musik.
        alltag = "alltag" in merkmale and not rest
        return (action, _NUR_MIT_MUSIK if alltag else "")
    return None


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
# --- Link im Satz: Abspiel-Auftrag oder Gespraech? (siehe _link_ist_befehl) --
# Steht fuer die Pruefung an der Stelle des Links - ein Wort, das niemand tippt.
_LINK_PLATZ = "floxlinkxplatz"
# Neben einem NACKTEN Link darf nur das stehen ('Flo hier https://…').
_LINK_BEIWERK_NACKT = frozenset(("hier",))
# Kurze Abspiel-Verben vorn; dahinter nur Beiwerk ('schau mal <link> an',
# 'pack <link> in die queue', 'leg auf <link>', 'hör dir das an <link>').
_LINK_VERBEN = frozenset(("queue", "add", "abspielen", "schau", "guck", "hör",
                          "hoer", "leg", "pack", "hau", "mach", "tu", "lass"))
_LINK_BEIWERK = frozenset((
    "hier", "dir", "euch", "uns", "mir", "das", "den", "die", "dieses", "diesen",
    "diese", "video", "song", "lied", "track", "an", "auf", "rein", "ab", "raus",
    "in", "queue", "warteschlange", "hinzu", "laufen"))

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
    # Was man einen Chatbot fragt, ist auch kein Song: 'hau mal nen witz raus'
    # hat sonst YouTube nach "nen witz" durchsucht und das Ergebnis gespielt.
    "witz", "witze", "spruch", "sprüche", "sprueche", "joke", "jokes", "fakt",
    "fakten", "fact", "facts", "geschichte", "story", "gedicht", "zitat",
    "weisheit", "roast", "beleidigung", "licht", "fernseher", "heizung",
}

# "flo spiel random" / "flo random" / "flo überrasch mich" -> Genre-Auswahl (Dropdown),
# danach ein zufaelliger Song aus dem Genre. Fuellwoerter (mir/uns/mal/was ...) egal.
#
# Bis ans Satzende verankert (Gruppe 'rest', geprueft in parse_command):
# 'random frage', 'überraschung!', 'zufall oder nicht' klappten sonst das
# Genre-Menue auf, statt dass Flo antwortet. 'überrasch' braucht deshalb auch
# eine Wortgrenze - 'überraschung' ist ein Ausruf, kein Befehl.
_RANDOM_RE = re.compile(
    r"^(?:spiel(?:e|st)?\s+)?"
    r"(?:mir\s+|uns\s+|mal\s+|was\s+|etwas\s+|nen\s+|einen\s+|ne\s+|nal\s+)*"
    r"(?:random|zufall(?:s?song|s?lied|smusik|s?track)?|"
    r"(?:überrasch|ueberrasch)(?:e|t)?(?:\s+(?:mich|uns))?)"
    r"(?P<rest>\W.*)?$", re.I | re.S)
# Was hinter 'random' noch stehen darf ('random song', 'zufall musik').
_RANDOM_OBJ = rf"(?:(?:einen|ein|nen|ne)\s+)?(?:{_OBJ_MUSIK}|genre)"

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

# --- Musik-Verlauf ----------------------------------------------------------
# So viele gespielte Songs bleiben je Server erhalten - ueber Neustarts hinweg.
# Der Player selbst haelt nur die letzten 30 im Arbeitsspeicher; die sind nach
# einem Neustart weg, und genau danach will man "was lief gestern?" fragen.
VERLAUF_MAX = int(os.getenv("MUSIC_VERLAUF_MAX", "100") or "100")
VERLAUF_SEITE = 10          # Eintraege je Seite im Embed
VERLAUF_TIMEOUT = 180.0     # ~3 Minuten, dann sind die Knoepfe aus

# Woerter, die "Verlauf" meinen. Tippfehler faengt _ist_verlauf_wort ab.
_VERLAUF_WOERTER = ("verlauf", "history", "historie", "histori", "verlaufs",
                    "playlistverlauf", "songverlauf")
# Was davor stehen darf: 'nochmal verlauf', 'musik history', 'again history'.
_VERLAUF_VORWORT = ("nochmal", "nochmals", "nochmoi", "repeat", "replay",
                    "again", "wiederhole", "wiederholen", "wiederhol",
                    "musik", "music", "song", "songs", "lied", "lieder",
                    "spiel", "spiele", "played", "gespielt")

def _wort_abstand(a, b):
    """Levenshtein-Abstand, abgebrochen sobald er 3 ueberschreitet.

    Kein difflib: dessen ratio() haengt an der Wortlaenge und laesst bei kurzen
    Woertern viel zu viel durch. Hier zaehlen echte Tippfehler - eingefuegt,
    vergessen, vertauscht, vertippt."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 3
    vorige = list(range(len(b) + 1))
    for i, za in enumerate(a, 1):
        aktuell = [i]
        for j, zb in enumerate(b, 1):
            aktuell.append(min(vorige[j] + 1, aktuell[j - 1] + 1,
                               vorige[j - 1] + (za != zb)))
        if min(aktuell) > 2:
            return 3
        vorige = aktuell
    return vorige[-1]


def _ist_verlauf_wort(wort):
    """Meint dieses Wort den Verlauf - auch vertippt?

    Die Laengengrenze ist wichtig: mit Abstand 2 auf kurze Woerter waere fast
    jeder andere Befehl ploetzlich 'Verlauf'. Ab 6 Zeichen ist ein Abstand von
    2 ein Tippfehler und keine Verwechslung."""
    w = (wort or "").lower().strip(".,!?:;")
    if not w:
        return False
    if w in _VERLAUF_WOERTER:
        return True
    if len(w) < 6 or w in _KEIN_VERLAUF:
        return False
    return any(_wort_abstand(w, ziel) <= 2 for ziel in _VERLAUF_WOERTER)


# Echte Woerter, die nur zufaellig nah an 'verlauf' liegen. 'Flo verkauf' ist
# der Aktien-Verkauf - die Musik steht in der Kette aber VOR der Aktie und hat
# daraus den Songverlauf gemacht.
_KEIN_VERLAUF = frozenset({"verkauf", "verkaufe", "verkaufen", "verkauft",
                           "verlauft", "verlaufen", "verlaufe"})


def _ist_verlauf_vorwort(wort):
    """'nochmal', 'musik', 'again' ... - ebenfalls tippfehlertolerant."""
    w = (wort or "").lower().strip(".,!?:;")
    if w in _VERLAUF_VORWORT:
        return True
    if len(w) < 5:
        return False
    return any(_wort_abstand(w, ziel) <= 2 for ziel in _VERLAUF_VORWORT)


def verlauf_befehl(text):
    """Ist das ein Verlauf-Befehl? Erwartet den Text OHNE Botnamen.

    Erlaubt sind hoechstens zwei Woerter:
        history | histori | ...            (ein Wort)
        nochmal verlauf | musik history    (Vorwort + Verlaufwort)

    BEWUSST NICHT das nackte 'verlauf': das gehoert seit jeher dem
    Handelsbuch (handel.py '_CMDS'), und music.handle laeuft in der Kette VOR
    handel - Flo wuerde also ab sofort den Musik-Verlauf zeigen, wenn jemand
    seine Coin-Umsaetze sehen will. Ein bestehender Befehl darf davon nicht
    kaputtgehen."""
    teile = (text or "").split()
    if not teile or len(teile) > 2:
        return False
    if len(teile) == 1:
        # Ein Wort: nur die eindeutigen. 'verlauf' allein bleibt beim Handel.
        w = teile[0].lower().strip(".,!?:;")
        if w in ("verlauf", "verlaufs"):
            return False
        return _ist_verlauf_wort(w)
    return _ist_verlauf_vorwort(teile[0]) and _ist_verlauf_wort(teile[1])


# "flo nochmal", "flo spiel nochmal 2", "flo repeat 3", "flo wiederhole" ->
# den zuletzt (bzw. N-t-letzten) gespielten Song noch einmal spielen.
#
# Bis ans Satzende verankert (Gruppe 'rest', geprueft in parse_command). Vorher
# reichte das Wort am Anfang: 'nochmal bitte' (sag's nochmal), 'noch mal zum
# thema', 'wiederhol das', 'repeat after me' spielten alle den letzten Song,
# statt dass Flo antwortet. Ohne 'spiel' davor darf deshalb NUR eine Nummer
# folgen; mit 'spiel' ist klar, was gemeint ist, dann gehen auch Fuellwoerter.
_REPLAY_RE = re.compile(
    r"^(?P<spiel>spiel(?:e|st)?\s+)?"
    r"(?:nochmal(?:s)?|noch\s*mal|repeat|replay|wiederhol(?:e|en|st)?)"
    # 'nochmal nummer 3' / 'nochmal nr 3' / 'nochmal #3' - das Fuellwort davor
    # ist ueblich und wurde vorher als Suchtext gedeutet.
    # [0-9] statt \d: \d faengt auch fremde Ziffernsysteme (siehe _LOOP_RE).
    r"(?:\s*(?:nummer|nr\.?|no\.?|numer|nummber|#)?\s*(?P<nr>[0-9]+))?"
    r"(?P<rest>\W.*)?$", re.I | re.S)

# "flo loop", "flo loop 3", "flo loop aus", "flo dauerschleife 5" -> den
# LAUFENDEN Song wiederholen.
#
# Bewusst NICHT 'repeat'/'replay'/'wiederhol': die gehoeren seit jeher dem
# VERLAUF (_REPLAY_RE oben). "flo repeat 3" heisst hier "spiel Song Nummer 3 aus
# dem Verlauf" - wuerde der Loop sich das Wort nehmen, aendert sich die
# Bedeutung eines bestehenden Befehls.
#
# Der Ausdruck ist bis ans Zeilenende verankert: "flo was ist eigentlich ein
# loop" soll die KI beantworten, nicht die Musik kapern.
_LOOP_RE = re.compile(
    r"^(?:loop(?:e|en|t)?|dauerschleife|endlosschleife)"
    r"(?:\s+(aus|off|stop|weg|an|ein|on|endlos|unendlich|dauerhaft)"
    # [0-9] statt \d: \d faengt auch fremde Ziffernsysteme, und int() stirbt
    # daran (siehe numfmt.ist_zahl). Passt es nicht, uebernimmt die KI.
    r"|\s*([0-9]+)\s*(?:x|×|mal)?)?\s*$", re.I)

# Lautstaerke - tolerant: "flo lautstärke 30", "flo ls 80", "flo LS", "flo vol 50",
# "flo lautstärke auf 30" sowie gaengige Tippfehler. Ohne Zahl -> aktuelle anzeigen.
_VOLUME_UP_RE = re.compile(r"^(?:lauter|louder|lautr)\b", re.I)
_VOLUME_DOWN_RE = re.compile(r"^(?:leiser|quieter|leise)\b", re.I)
# Was hinter 'lauter'/'leiser' stehen darf. Dieselbe Regel wie bei den
# Steuerwoertern: 'leise rieselt der schnee' und 'lauter als du' drehten sonst
# an der Lautstaerke, statt dass Flo antwortet.
_VOLUME_REL_OBJ = (rf"(?:{_OBJ_MUSIK}\s+)?(?:machen|drehen|stellen)|{_OBJ_MUSIK}"
                   r"|[0-9]+")
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
    # Loop: wie oft der LAUFENDE Song noch wiederholt wird.
    #   0 = aus, N = noch N Wiederholungen, -1 = endlos.
    # Gehoert bewusst an den Player und NICHT in die View - das Panel wird bei
    # jedem Songwechsel neu gebaut. Und bewusst NICHT in den Speicher: der Loop
    # haengt an genau einem laufenden Song, nach einem Neustart haette ein
    # wiederhergestellter Zaehler kein Ziel mehr. Genauso haelt es 'speed'.
    loop_rest: int = 0
    loop_key: str = ""               # WELCHER Song gelooped wird (siehe _loop_key)
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
    # monotonic des letzten Reconnect-Versuchs (Loop-Bremse). "Nie" ist -inf
    # und NICHT 0.0: monotonic() zaehlt ab dem Hochfahren des Rechners. Mit 0.0
    # hiess "nie" in den ersten 20 s nach einem Server-Neustart "gerade eben",
    # und der erste Reconnect wurde stillschweigend verschluckt.
    _last_reconnect: float = float("-inf")
    _reconnect_fails: int = 0        # aufeinanderfolgende fehlgeschlagene Reconnects (Aufgabe-Schwelle)
    # Seit wann (monotonic) hier nichts mehr zu tun ist - kein Song, keine
    # Warteschlange, keine Pause - oder nur noch Bots im Kanal hocken. None =
    # es ist etwas zu tun. Nach MUSIC_IDLE_SEKUNDEN geht Flo raus (siehe heal).
    _leer_seit: float | None = None
    # Bis wann (monotonic) eine Trennung von UNS kommt: _fresh_connect und
    # _reconnect werfen den alten Client selbst raus. Discord meldet das
    # genauso wie einen Rauswurf durch einen Moderator - flo_getrennt muss den
    # Unterschied kennen, sonst beendet jeder Reconnect die Musik.
    _selbst_getrennt_bis: float = float("-inf")
    # Bis wann flo_getrennt gerade nachsieht, ob das ein Rauswurf war. Solange
    # haelt sich der Watchdog raus - sonst holt er Flo in genau dieser Luecke
    # zurueck in den Kanal, aus dem ihn gerade jemand geworfen hat.
    _rauswurf_bis: float = float("-inf")
    # Serialisiert ALLE voice-veraendernden Ops (connect/_reconnect/apply_speed),
    # damit nie zwei channel.connect() gleichzeitig laufen.
    _voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serialisiert die Songwechsel. Zwei gleichzeitige Laeufe (zwei schnelle
    # Skips, oder Skip waehrend der after-Callback schon laeuft) haben beide aus
    # derselben Warteschlange gepoppt - dabei ging ein Track spurlos verloren.
    _advance_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Vorladen des naechsten Songs (siehe MUSIC_VORLADEN): der laufende
    # Vorgang, fuer welchen Track, und der Wecker fuer lange Songs.
    _vorlade_task: "asyncio.Task | None" = None
    _vorlade_track: "Track | None" = None
    _vorlade_timer: "asyncio.TimerHandle | None" = None
    # Was zuletzt als Sprachkanal-Status gesetzt wurde (kein doppeltes Setzen).
    _kanal_status_text: "str | None" = None

    # --- Vorladen: der naechste Song ist fertig, wenn dieser endet ----------
    def _vorladen_planen(self):
        """Nach jedem Start: den naechsten Song rechtzeitig aufloesen.

        Rechtzeitig heisst: so spaet, dass die Adresse beim Songwechsel noch
        frisch ist (YouTube-Adressen altern, siehe STREAM_MAX_ALTER) - bei
        normalen Songs also sofort, bei einem Zwei-Stunden-Mix erst gegen Ende."""
        if not MUSIC_VORLADEN:
            return
        if self._vorlade_timer is not None:
            self._vorlade_timer.cancel()
            self._vorlade_timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        dauer = getattr(self.current, "duration", None) or 0
        verzug = dauer - (STREAM_MAX_ALTER - 120) if dauer else 0
        if verzug <= 0:
            self._vorladen_anstossen()
        else:
            self._vorlade_timer = loop.call_later(verzug, self._vorladen_anstossen)

    def _vorladen_anstossen(self):
        self._vorlade_timer = None
        if not self.queue:
            return
        naechster = self.queue[0]
        if naechster.stream_url or not naechster.query:
            return
        if self._vorlade_task is not None and not self._vorlade_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._vorlade_track = naechster
        self._vorlade_task = loop.create_task(self._vorladen(naechster, self._session_gen))

    async def _vorladen(self, track, sitzung):
        """Loest den Track auf und schreibt das Ergebnis IN DENSELBEN Track.

        Kein Austausch in der Warteschlange: ein Skip, ein Verschieben oder
        ein Entfernen dazwischen stoert so nicht - der Track traegt seine
        Adresse einfach mit, wohin er auch wandert."""
        try:
            frisch = await _resolve_track(track)
        except Exception as exc:  # noqa: BLE001 - dann eben beim Abspielen
            log.info("Vorladen von '%s' ging nicht (%s) - klappt vielleicht beim "
                     "Abspielen.", track.title, f"{exc}".replace("\n", " ")[:100])
            return
        if self._session_gen != sitzung or track.stream_url:
            return          # gestoppt, oder jemand war schneller
        for feld in ("title", "stream_url", "webpage_url", "duration", "thumbnail",
                     "geloest_um", "kopfzeilen"):
            setattr(track, feld, getattr(frisch, feld))
        log.info("Vorgeladen: '%s'", track.title)

    async def _vorladen_abwarten(self, track):
        """Wird genau dieser Song gerade vorgeladen? Dann darauf warten, statt
        ihn ein zweites Mal aufzuloesen."""
        task = self._vorlade_task
        if task is None or task.done() or self._vorlade_track is not track:
            return
        await asyncio.wait({task}, timeout=VORLADEN_WARTEN)

    def _vorladen_stoppen(self):
        if self._vorlade_timer is not None:
            self._vorlade_timer.cancel()
            self._vorlade_timer = None
        if self._vorlade_task is not None and not self._vorlade_task.done():
            self._vorlade_task.cancel()
        self._vorlade_task = self._vorlade_track = None

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
            self._selbst_trennen_ankuendigen()
            try:
                await asyncio.wait_for(stale.disconnect(force=True), timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.voice = None
        self.voice = await channel.connect(self_deaf=True, reconnect=True)

    def _selbst_trennen_ankuendigen(self):
        """Gleich trennen WIR selbst (Neuaufbau) - flo_getrennt soll das nicht
        fuer einen Rauswurf halten. 30 s reichen fuer Discords Meldung."""
        self._selbst_getrennt_bis = time.monotonic() + 30.0

    def sitzung_offen(self):
        """Laeuft hier eine Musik-Sitzung - oder soll eine laufen?"""
        return (self.voice is not None or self.active_channel_id is not None
                or bool(self.queue) or self.current is not None)

    def nichts_zu_tun(self):
        """Kein Song, keine Pause, nichts in der Warteschlange - oder nur eine,
        die _advance aufgegeben hat und die seitdem niemand angestossen hat."""
        return (self.current is None and not self.ist_pausiert()
                and not self._advancing
                and (not self.queue or self._advance_aufgegeben))

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
        # Jeder NEUE Song startet immer auf Normaltempo - der Effekt wird pro Song
        # einzeln gewaehlt (keep_speed nur beim Neustart DESSELBEN Songs).
        speed = self.speed if keep_speed else 1.0
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
        af = _build_audio_filter(speed)
        if af is not None:
            # Speed-up: atempo (Tonhoehe bleibt). Slow: slowed + reverb (siehe _build_audio_filter).
            opts = f"{_FFMPEG_OPTS} -filter:a {af}"
        source = discord.FFmpegPCMAudio(
            track.stream_url, before_options=before, options=opts
        )
        # Der Zustand wird erst UEBERNOMMEN, wenn play() geklappt hat - vorher
        # stand hier alles VOR voice.play() (AUDIT 'music.py:691'). Warf
        # play(), hiess der Player trotzdem "spielt: <track>": der Watchdog
        # hielt das fuer einen Zombie und startete den nie gelaufenen Song
        # immer wieder, und die hochgezaehlte Generation hatte den
        # after-Callback eines noch laufenden Songs entwertet. Die Generation
        # MUSS aber vor play() stehen (der Callback kann sofort feuern) - also
        # setzen und bei einem Fehler alles zuruecknehmen.
        vorher = (self.current, self._played, self._seg_start, self._stall_ticks,
                  self._play_gen, self.speed, self.pausiert, self._neustart_versuche,
                  self._leer_seit)
        self.current = track
        self._played = seek          # Positions-Uhr auf die Startstelle setzen
        self._seg_start = time.monotonic()
        self._stall_ticks = 0        # frisch gestartet (buffert evtl. kurz) -> kein Zombie-Alarm
        self._leer_seit = None       # es laeuft wieder was -> Leerlauf-Uhr aus
        self.speed = speed
        if not keep_speed:
            # Und er startet spielend: eine alte Pause-Absicht gilt nur fuer
            # den Song, bei dem sie gesetzt wurde.
            self.pausiert = False
            # Neuer Song -> die Wiederbelebungs-Versuche gelten wieder frisch.
            # (Der Watchdog-Neustart laeuft mit keep_speed=True und zaehlt hier
            # bewusst NICHT zurueck, sonst koennte er sich ewig selbst verlaengern.)
            self._neustart_versuche = 0
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
            (self.current, self._played, self._seg_start, self._stall_ticks,
             self._play_gen, self.speed, self.pausiert, self._neustart_versuche,
             self._leer_seit) = vorher
            raise
        if not keep_speed:
            # Jeden NEU gestarteten Song in den Verlauf legen (fuer 'flo nochmal').
            # Effekt-/Tempo-Neustarts (keep_speed) zaehlen nicht als neuer Song.
            self.history.append(track)
            del self.history[:-30]   # nur die letzten 30 behalten
            # UND dauerhaft: der Verlauf oben ist nach einem Neustart weg.
            try:
                instance.verlauf_notieren(self.guild_id, track)
            except Exception:  # noqa: BLE001 - Mitschreiben darf nie die Musik kippen
                log.debug("Verlauf konnte nicht notiert werden", exc_info=True)
            # Und schon mal den naechsten holen (nur bei einem NEUEN Song -
            # ein Tempo-Neustart aendert an der Warteschlange nichts).
            self._vorladen_planen()

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
        loop_wieder = False     # ist der naechste Pop ein Loop-Durchlauf?
        try:
            # Kam der Callback, weil der Song ZU ENDE ist - oder weil FFmpeg
            # gestorben ist? Nur beim echten Ende wird weitergeschaltet.
            # (Bei gen=None hat ein Mensch 'skip' gedrueckt - der will weiter.)
            if gen is not None and await self._nach_abbruch_fortsetzen():
                return
            # Loop: den gerade beendeten Song noch einmal vorne einreihen.
            # Die Stelle ist mit Bedacht gewaehlt:
            #  - NACH _nach_abbruch_fortsetzen: ein FFmpeg-Absturz ist kein
            #    fertiger Durchlauf und darf keine Wiederholung verbrauchen.
            #  - VOR dem 'not self.queue'-Ausstieg unten: der raeumt sonst
            #    Panel und current weg, bevor der Loop ueberhaupt drankommt.
            #  - NUR bei gen is not None, also nur am echten Songende. 'skip'
            #    ruft _advance() ohne gen - taete der Loop dort auch etwas,
            #    waere der Skip wirkungslos.
            if (gen is not None and self.loop_rest and self.current is not None
                    and _loop_key(self.current) == self.loop_key):
                if self.loop_rest > 0:
                    self.loop_rest -= 1
                self.queue.insert(0, replace(self.current))
                loop_wieder = True
            while True:
                if gen is not None and gen != self._play_gen:
                    return          # jemand hat inzwischen selbst gestartet
                if not self.voice or not self.voice.is_connected() or not self.queue:
                    self.current = None
                    await _retire_panel(self)
                    return
                track = self.queue.pop(0)
                # Nur der ERSTE Pop ist der Loop-Durchlauf. Faellt er durch
                # (nicht ladbar), holt die Schleife den naechsten echten Song.
                wiederholung, loop_wieder = loop_wieder, False
                try:
                    if track.stream_url and track.query and _adresse_alt(track):
                        # Die Adresse lag zu lange herum (siehe STREAM_MAX_ALTER):
                        # frisch holen, sonst startet ein Song, der nie Ton macht.
                        log.info("Stream-Adresse von '%s' ist veraltet - hole eine "
                                 "frische.", track.title)
                        track.stream_url = ""
                    if not track.stream_url and track.query:
                        await self._vorladen_abwarten(track)
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
                    # keep_speed beim Loop-Durchlauf: sonst setzte start()
                    # _neustart_versuche zurueck - die Schwelle, die einen
                    # kaputten Song aufgibt, koennte sich so ewig selbst
                    # verlaengern. Nebenbei ueberlebt damit ein eingestelltes
                    # 'slowed + reverb' die Wiederholung.
                    self.start(track, keep_speed=wiederholung)
                except Exception:
                    if wiederholung:
                        # Ein Song, der nicht laeuft, wird nicht ewig
                        # wiederholt - Loop aus, dann ganz normal weiter.
                        self.loop_rest = 0
                        self.loop_key = ""
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
                    if wiederholung and self.panel_message is not None:
                        # Loop-Durchlauf: das vorhandene Panel nur auffrischen.
                        # Jedes Mal ein neues zu posten waere bei einem kurzen
                        # Song sichtbares Geflacker plus Rate-Limit-Risiko.
                        await _panel_auffrischen(self, track)
                    else:
                        await _send_panel(self, track)
                except Exception:
                    log.exception("Now-Playing-Panel nach Advance fehlgeschlagen (egal)")
                return
        finally:
            self._advancing = False

    async def disconnect(self):
        self._vorladen_stoppen()
        self.queue.clear()
        self.current = None
        self.speed = 1.0           # frische Session startet wieder mit Normaltempo
        self.loop_rest = 0         # sonst wiederholt die naechste Sitzung ungefragt
        self.loop_key = ""
        self._seg_start = None
        self._played = 0.0
        self.active_channel_id = None   # bewusst raus -> Watchdog soll NICHT zurueckholen
        self._leer_seit = None
        self._advance_aufgegeben = False
        self._session_gen += 1          # alles, was noch laeuft, gehoert zur ALTEN Sitzung
        self._stall_ticks = 0
        self._frozen_ticks = 0
        self._last_frames = -1
        self.pausiert = False
        self._play_gen += 1             # alte after-Callbacks entwerten
        await _retire_panel(self)
        if self.voice is not None:
            try:
                await instance._kanal_status(self, None)
            except Exception:  # noqa: BLE001
                pass
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

    def loop_setzen(self, anzahl):
        """Loop fuer den LAUFENDEN Song setzen.

        anzahl: 0 = aus, N = noch N Wiederholungen, -1 = endlos.
        Rueckgabe: False, wenn gerade gar nichts laeuft (dann gibt es nichts zu
        wiederholen). Den Song merkt sich der Loop ueber _loop_key - laeuft
        spaeter etwas anderes, greift er nicht mehr."""
        anzahl = int(anzahl)
        if anzahl == 0:
            self.loop_rest = 0
            self.loop_key = ""
            return True
        if self.current is None:
            return False
        self.loop_rest = anzahl
        self.loop_key = _loop_key(self.current)
        return True

    async def skip(self):
        """Zum naechsten Song - und zwar VERLAESSLICH.

        Frueher stand hier nur voice.stop() und der Rest hing am
        after-Callback. Der wird aber entwertet, sobald die Wiedergabe-
        Generation zwischendurch hochzaehlt (Watchdog-Neustart, Tempo-Wechsel,
        Reconnect). Fiel der Skip in so ein Fenster, meldete Flo
        'uebersprungen' - und es passierte nichts. Jetzt entwerten wir den
        Callback selbst und stossen den naechsten Song direkt an, damit es
        genau EINEN Weg gibt und der immer laeuft."""
        # Wer skippt, will WEG von diesem Song - ein laufender Loop wuerde ihn
        # sonst sofort wieder vorne einreihen und der Skip verpuffte.
        self.loop_rest = 0
        self.loop_key = ""
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
        if time.monotonic() < self._rauswurf_bis:
            return      # flo_getrennt sieht gerade nach, ob ihn jemand rausgeworfen hat
        channel = guild.get_channel(self.active_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            self.active_channel_id = None   # Kanal gibt es nicht mehr -> aufgeben
            return
        if await self._leerlauf(channel):
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

    async def _leerlauf(self, channel):
        """Leerlauf-Uhr des Watchdogs. True = heal() ist hier fertig.

        Nichts mehr zu tun (kein Song, keine Warteschlange, keine Pause) oder
        nur noch Bots im Kanal: dann laeuft die Uhr, und nach
        MUSIC_IDLE_SEKUNDEN geht Flo von selbst. Ohne das blieb er fuer immer
        drin - und weil der Watchdog ihn "am Leben hielt", holte er ihn sogar
        zurueck, wenn ihn jemand rausgeworfen hatte."""
        leer = self.nichts_zu_tun()
        allein = _nur_bots_im_kanal(channel)
        if not (leer or allein) or MUSIC_IDLE_SEKUNDEN <= 0:
            self._leer_seit = None
            return False
        jetzt = time.monotonic()
        if self._leer_seit is None:
            self._leer_seit = jetzt
        if jetzt - self._leer_seit >= MUSIC_IDLE_SEKUNDEN:
            log.info("Musik: seit %.0f s %s in '%s' - verlasse den Sprachkanal.",
                     jetzt - self._leer_seit,
                     "nichts zu tun" if leer else "keiner mehr da", channel.name)
            await self.disconnect()
            if not leer:
                # Es lief noch was - dann sagen, warum es jetzt still ist.
                await self._sag("🔇 Keiner mehr da, der zuhört. Musik aus, ich bin raus.")
            return True
        # Nichts zu spielen = nichts zu heilen. Vor allem KEIN Reconnect, nur um
        # in einem Kanal herumzusitzen, aus dem die Verbindung gerade gefallen ist.
        return leer

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
            self._selbst_trennen_ankuendigen()
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


class _VerlaufSelect(discord.ui.Select):
    """Die Songs der aktuellen Seite als Dropdown - ein Klick spielt."""

    def __init__(self, eintraege, start_nr):
        optionen = []
        for i, e in enumerate(eintraege):
            nr = start_nr + i
            titel = (e.get("t") or "Unbekannt")[:90]
            wer = e.get("w") or ""
            beschreibung = " · ".join(x for x in (
                wer, Music._vor_wie_lange(e.get("ts"))) if x)[:95]
            optionen.append(discord.SelectOption(
                label=f"{nr}. {titel}"[:100], value=str(nr),
                description=beschreibung or None))
        super().__init__(placeholder="Song anklicken zum Nochmal-Spielen …",
                         min_values=1, max_values=1,
                         options=optionen or [discord.SelectOption(label="—", value="0")],
                         disabled=not optionen)

    async def callback(self, interaction):
        await instance.verlauf_abspielen(interaction, int(self.values[0]))


class VerlaufView(discord.ui.View):
    """Blaettern durch den Musik-Verlauf + Direktauswahl.

    Wer darf bedienen: der Aufrufer ODER wer Nachrichten verwalten darf -
    genau wie bei QueuePositionView. Ein Verlauf ist nichts Privates, aber
    wenn zwei Leute gleichzeitig blaettern, springt die Seite unter den
    Fingern weg; deshalb dieselbe Regel wie bei den anderen Musik-Knoepfen."""

    def __init__(self, gid, owner_id, seite=0, *, timeout=VERLAUF_TIMEOUT):
        super().__init__(timeout=timeout)
        self.gid = gid
        self.owner_id = owner_id
        self.seite = seite
        self.message = None
        self._aufbauen()

    # --- Aufbau ---
    def _daten(self):
        eintraege = instance.verlauf(self.gid)
        seiten = max(1, (len(eintraege) + VERLAUF_SEITE - 1) // VERLAUF_SEITE)
        self.seite = max(0, min(self.seite, seiten - 1))
        start = self.seite * VERLAUF_SEITE
        return eintraege, seiten, start, eintraege[start:start + VERLAUF_SEITE]

    def _aufbauen(self):
        self.clear_items()
        eintraege, seiten, start, seite_eintraege = self._daten()
        if seite_eintraege:
            self.add_item(_VerlaufSelect(seite_eintraege, start + 1))
        if seiten > 1:
            zurueck = discord.ui.Button(emoji="◀", style=discord.ButtonStyle.secondary,
                                        disabled=self.seite <= 0)
            zurueck.callback = self._zurueck
            vor = discord.ui.Button(emoji="▶", style=discord.ButtonStyle.secondary,
                                    disabled=self.seite >= seiten - 1)
            vor.callback = self._vor
            self.add_item(zurueck)
            self.add_item(vor)

    def embed(self):
        eintraege, seiten, start, seite_eintraege = self._daten()
        if not eintraege:
            return instance._embed(
                "Noch keine Songs gespielt. Leg was auf: "
                f"`{instance._bot_name} spiel <titel>` 🎵",
                title="🕘  Musik-Verlauf", color=_COL_INFO)
        zeilen = []
        for i, e in enumerate(seite_eintraege):
            nr = start + i + 1
            titel = e.get("t") or "Unbekannt"
            url = e.get("u") or ""
            name = f"[{titel}]({url})" if url.startswith("http") else titel
            teile = [instance._vor_wie_lange(e.get("ts"))]
            if e.get("w"):
                teile.append(f"von {e['w']}")
            dauer = instance._fmt_dur(e.get("d"))
            if dauer:
                teile.append(dauer)
            zeilen.append(f"**{nr}.** {name}\n_{' · '.join(teile)}_")
        emb = instance._embed("\n".join(zeilen), title="🕘  Musik-Verlauf",
                              color=_COL_QUEUE)
        emb.set_footer(text=(f"Seite {self.seite + 1}/{seiten} · "
                             f"{len(eintraege)} Songs · "
                             f"{instance._bot_name} nochmal <nr>"))
        return emb

    # --- Bedienung ---
    async def interaction_check(self, interaction):
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id == self.owner_id or (perms and perms.manage_messages):
            return True
        await interaction.response.send_message(
            f"Das ist nicht dein Verlauf – tipp dir mit "
            f"`{instance._bot_name} history` einen eigenen. 🕘",
            ephemeral=True)
        return False

    async def _blaettern(self, interaction, delta):
        self.seite += delta
        self._aufbauen()
        try:
            await interaction.response.edit_message(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass

    async def _zurueck(self, interaction):
        await self._blaettern(interaction, -1)

    async def _vor(self, interaction):
        await self._blaettern(interaction, +1)

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


def _tempo_optionen(aktuell=1.0):
    """Die Eintraege des Tempo-Menues; das laufende Tempo ist vorgewaehlt."""
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
                                        description=desc,
                                        default=abs(s - aktuell) < 1e-3))
    return out


class MusikTempo(discord.ui.DynamicItem[discord.ui.Select],
                 template=r"flo:musik:tempo"):
    """Das Tempo-Menue im Panel. Stellt den laufenden Song an der aktuellen
    Stelle um (FFmpeg atempo bzw. slowed + reverb).

    Ein DynamicItem wie die Knoepfe: der Zustand kommt beim Klick aus dem
    Player des Servers, nicht aus der Nachricht - so geht es auch nach einem
    Neustart."""

    def __init__(self, aktuell=1.0):
        super().__init__(discord.ui.Select(
            custom_id="flo:musik:tempo", placeholder="🎚️ Geschwindigkeit wählen …",
            min_values=1, max_values=1, options=_tempo_optionen(aktuell)))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction):
        await instance._panel_tempo(interaction, float(self.item.values[0]))


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


# Was der Loop-Knopf zur Auswahl stellt. -1 = endlos, 0 = aus.
_LOOP_WAHL = (
    (2, "2× wiederholen", "der Song läuft noch 2 Mal"),
    (3, "3× wiederholen", "der Song läuft noch 3 Mal"),
    (5, "5× wiederholen", "der Song läuft noch 5 Mal"),
    (10, "10× wiederholen", "der Song läuft noch 10 Mal"),
    (-1, "endlos", "bis jemand Skip drückt oder den Loop ausmacht"),
)


class _LoopSelect(discord.ui.Select):
    """Wie oft soll der laufende Song wiederholt werden?

    Erscheint ephemer nach einem Klick auf den Loop-Knopf im Panel - genau der
    Ablauf 'Knopf -> Frage -> Anzahl waehlen'. 'Aus' steht nur drin, wenn
    ueberhaupt ein Loop laeuft."""

    def __init__(self, player):
        self.player = player
        optionen = []
        if player.loop_rest:
            optionen.append(discord.SelectOption(
                label="Loop aus", value="0", emoji="⏹️",
                description="nach diesem Durchlauf ganz normal weiter"))
        for wert, label, beschreibung in _LOOP_WAHL:
            optionen.append(discord.SelectOption(
                label=label, value=str(wert),
                emoji="♾️" if wert < 0 else "🔁",
                description=beschreibung,
                default=(player.loop_rest == wert)))
        super().__init__(placeholder="Wie oft? 🔁", min_values=1, max_values=1,
                         options=optionen)

    async def callback(self, interaction):
        self.view.stop()
        anzahl = int(self.values[0])
        if not self.player.loop_setzen(anzahl):
            await interaction.response.edit_message(
                content="Gerade läuft nichts, was ich wiederholen könnte.", view=None)
            return
        titel = getattr(self.player.current, "title", "") or "der Song"
        if anzahl == 0:
            text = "Loop aus – nach dem Durchlauf geht es normal weiter."
        elif anzahl < 0:
            text = (f"🔁 **{instance._short(titel, 70)}** läuft jetzt in "
                    f"Dauerschleife. `{instance._bot_name} loop aus` beendet sie.")
        else:
            text = (f"🔁 **{instance._short(titel, 70)}** läuft noch "
                    f"**{anzahl}×**.")
        await interaction.response.edit_message(content=text, view=None)
        # Das oeffentliche Panel soll den neuen Zustand sofort zeigen.
        try:
            await _panel_auffrischen(self.player)
        except Exception:  # noqa: BLE001 - Panel ist Deko, der Loop steht schon
            log.debug("Panel nach Loop-Wahl nicht aufgefrischt", exc_info=True)


class _LoopView(discord.ui.View):
    """Die ephemere Auswahl hinter dem Loop-Knopf. Nur der Klickende sieht sie,
    ein Timeout raeumt sie weg."""

    def __init__(self, player, owner_id, *, timeout = 60.0):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.add_item(_LoopSelect(player))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Das ist nicht deine Auswahl – drück dir einen eigenen Loop-Knopf. 🔁",
            ephemeral=True)
        return False


class MusikKnopf(discord.ui.DynamicItem[discord.ui.Button],
                template=r"flo:musik:(?P<aktion>pause|skip|stop|queue|lyrics|loop)"):
    """Ein Knopf am Musik-Panel. Die feste custom_id (flo:musik:<aktion>) macht
    ihn neustartfest: vorher war jedes Panel nach einem Neustart von Flo tot
    ("Diese Interaktion ist fehlgeschlagen"), obwohl die Musik weiterlief."""

    def __init__(self, aktion, *, label=None, emoji=None,
                 style=discord.ButtonStyle.secondary):
        self.aktion = aktion
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style,
                                           custom_id=f"flo:musik:{aktion}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["aktion"], label=item.label, emoji=item.emoji, style=item.style)

    async def callback(self, interaction):
        await instance._panel_klick(interaction, self.aktion)


def _knopfreihen(player):
    """Die Knoepfe des Panels - fuer V2 und fuer den klassischen Notweg gleich."""
    pausiert = player.ist_pausiert()
    oben = [
        MusikKnopf("pause", label="Weiter" if pausiert else "Pause",
                   emoji="▶️" if pausiert else "⏸️",
                   style=(discord.ButtonStyle.success if pausiert
                          else discord.ButtonStyle.secondary)),
        MusikKnopf("skip", label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary),
        MusikKnopf("stop", label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger),
        MusikKnopf("queue", label="Queue", emoji="🎶"),
        MusikKnopf("lyrics", label="Lyrics", emoji="🎤"),
    ]
    unten = [MusikKnopf("loop", label="Loop", emoji="🔁",
                        style=(discord.ButtonStyle.success if player.loop_rest
                               else discord.ButtonStyle.secondary))]
    return oben, unten


def _fortschritt(player, track):
    """Balken + Zeit + 'endet in ...'. Das Ende steht als Discord-Zeitstempel
    da (<t:...:R>) - den zaehlt Discord SELBST live herunter. Frueher haette das
    ein Edit alle paar Sekunden gebraucht, und jeder Edit schliesst offene
    Menues (Tempo) unter den Fingern der Leute weg."""
    dauer = track.duration or 0
    pos = min(player.position(), dauer) if dauer else player.position()
    if not dauer:
        return f"-# 📻 läuft seit {_fmt_dur(int(pos)) or '0:00'}"
    felder = 18
    voll = min(felder, int(round(pos / dauer * felder)))
    balken = "▰" * voll + "▱" * (felder - voll)
    text = f"`{balken}`  {_fmt_dur(int(pos)) or '0:00'} / {_fmt_dur(dauer)}"
    if player.ist_pausiert():
        return text + "  ·  ⏸️ pausiert"
    rest = max(0.0, (dauer - pos) / max(player.speed, 0.1))
    return text + f"  ·  endet <t:{int(time.time() + rest)}:R>"


class MusikPanel(discord.ui.LayoutView):
    """Das 'Jetzt laeuft'-Panel (Components V2).

    Cover und Titel oben, darunter der Fortschritt mit Live-Endzeit, was als
    Naechstes kommt, dann die Knoepfe und das Tempo-Menue. Alles Interaktive
    sind DynamicItems -> das Panel ueberlebt Neustarts.

    timeout=None und NIE stop(): stop() nimmt die DynamicItem-Vorlagen global
    aus dem Register (ViewStore.remove_view) - danach waere JEDES Panel tot.
    Weil die View nur aus DynamicItems besteht, merkt discord.py sie sich auch
    nicht je Nachricht - es gibt nichts, was sich ansammeln koennte (das alte
    Panel leckte je Song einen Eintrag, siehe test_musik_panel_leckt_nicht)."""

    def __init__(self, player, track=None, *, extra=""):
        super().__init__(timeout=None)
        track = track or player.current
        teile = []
        if track is not None:
            teile.extend(self._kopf(player, track))
            teile.append(discord.ui.TextDisplay(_fortschritt(player, track)))
        if extra:
            teile.append(discord.ui.TextDisplay(f"-# {extra}"))
        naechste = self._als_naechstes(player)
        if naechste:
            teile.append(discord.ui.Separator())
            teile.append(discord.ui.TextDisplay(naechste))
        oben, unten = _knopfreihen(player)
        teile.append(discord.ui.ActionRow(*oben))
        teile.append(discord.ui.ActionRow(*unten))
        teile.append(discord.ui.ActionRow(MusikTempo(player.speed)))
        self.add_item(discord.ui.Container(
            *teile, accent_colour=_COL_CTRL if player.ist_pausiert() else _COL_PLAY))

    @staticmethod
    def _kopf(player, track):
        kopf = "### ⏸️ Pausiert" if player.ist_pausiert() else "### ▶️ Jetzt läuft"
        meta = []
        if track.requested_by:
            meta.append(f"🙋 {track.requested_by}")
        if player.speed < 1.0 - 1e-3:
            meta.append(f"🌌 slowed + reverb {player.speed:g}×")
        elif player.speed > 1.0 + 1e-3:
            meta.append(f"🚀 {player.speed:g}×")
        lt = _loop_text(player.loop_rest)
        if lt:
            meta.append(lt)
        text = f"{kopf}\n{instance._title_value(track)}"
        if meta:
            text += "\n-# " + "  ·  ".join(meta)
        if track.thumbnail:
            return [discord.ui.Section(discord.ui.TextDisplay(text),
                                       accessory=discord.ui.Thumbnail(track.thumbnail))]
        return [discord.ui.TextDisplay(text)]

    @staticmethod
    def _als_naechstes(player):
        if not player.queue:
            return ""
        zeilen = ["**Als Nächstes**"]
        for i, t in enumerate(player.queue[:3], start=1):
            dur = _fmt_dur(t.duration)
            zeilen.append(f"`{i}.` {_short(t.title, 60)}" + (f" · `{dur}`" if dur else ""))
        mehr = len(player.queue) - 3
        if mehr > 0:
            zeilen.append(f"-# …und {mehr} weitere")
        return "\n".join(zeilen)


# Der alte Name bleibt (bot.py/Tests/Inventar kennen ihn) - dahinter steht
# jetzt das neue Panel.
PlaybackControlView = MusikPanel


def _klassisches_panel(player):
    """Notweg, falls Discord das V2-Panel ablehnt: das alte Embed-Layout,
    aber mit denselben neustartfesten Knoepfen."""
    view = discord.ui.View(timeout=None)
    oben, unten = _knopfreihen(player)
    for knopf in oben:
        view.add_item(knopf)
    for knopf in unten:
        knopf.row = 1
        view.add_item(knopf)
    tempo = MusikTempo(player.speed)
    tempo.row = 2
    view.add_item(tempo)
    return view


def _gestoppt_panel(wer):
    """Was aus dem Panel wird, wenn jemand Stop drueckt."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"### ⏹️ Gestoppt\nMusik aus, ich bin raus.\n"
                               f"-# gestoppt von {wer}"),
        accent_colour=_COL_INFO))
    return view


# Werden in bot.setup_hook angemeldet (neustartfeste Knoepfe).
DYNAMISCHE_KNOEPFE = (MusikKnopf, MusikTempo)

# Wer nicht im Voice sitzt, bekommt eine dieser Abfuhren.
_ZAUNGAST = (
    "Du sitzt nicht mal im Voice, also Finger weg vom Panel, du Zaungast.",
    "Erst in den Sprachkanal, dann mitreden. Vorher drückst du hier gar nix.",
    "Nicht im Voice, aber am Panel rumfummeln? Setz dich erst dazu, du Lauch.",
)


class Music(FeatureBasis):
    """Buendelt Zustand und Logik des Musik-Features (frueher freie
    Modul-Funktionen und globale Variablen dieses Moduls)."""

    def __init__(self):
        # --- Konfiguration (in setup() aus der .env gelesen) ---------------------
        self._enabled = False
        self._guter_client = ""   # player_client, der zuletzt durchkam
        self._nebenbei = set()    # kleine Hintergrund-Tasks (siehe _hintergrund)
        self._spotify_id = ""
        self._spotify_secret = ""
        # --- Spotify-Token (Client-Credentials, 1 h gueltig, hier gecached) ------
        self._sp_token = {"value": "", "exp": 0.0}
        # Player-/Queue-Zustand pro Server (guild_id -> GuildPlayer).
        self._players = {}
        # Dauerhafter Musik-Verlauf je Server (ueberlebt Neustarts).
        self._store = None
        self._verlauf_dirty = False
        self._verlauf_task = None

    # --- Musik-Verlauf: dauerhaft, je Server --------------------------------
    def _verlauf_liste(self, gid):
        """Die Liste dieses Servers - NEUESTER zuerst (Index 0 = Nummer 1)."""
        if self._store is None:
            return []
        alle = self._store.data.setdefault("guilds", {})
        if not isinstance(alle, dict):
            alle = self._store.data["guilds"] = {}
        liste = alle.setdefault(str(int(gid or 0)), [])
        if not isinstance(liste, list):
            liste = alle[str(int(gid or 0))] = []
        return liste

    def verlauf(self, gid):
        """Der gespielte Verlauf, neuester zuerst. Nur brauchbare Eintraege."""
        return [e for e in self._verlauf_liste(gid)
                if isinstance(e, dict) and e.get("t")]

    def verlauf_notieren(self, gid, track):
        """Einen gestarteten Song dauerhaft festhalten. Synchron und billig.

        Wird aus GuildPlayer.start() gerufen - das ist kein async-Kontext, also
        wird hier nur die Liste angefasst und das Speichern verzoegert."""
        if self._store is None or not gid or track is None:
            return
        eintrag = {
            "t": (getattr(track, "title", "") or "Unbekannter Titel")[:150],
            "u": (getattr(track, "webpage_url", "") or "")[:400],
            "q": (getattr(track, "query", "") or "")[:200],
            "w": (getattr(track, "requested_by", "") or "")[:64],
            "d": getattr(track, "duration", None),
            "ts": time.time(),
        }
        liste = self._verlauf_liste(gid)
        # Direkt hintereinander derselbe Song (Neustart nach Stall, Seek,
        # 'weiter') soll den Verlauf nicht zumuellen - dann nur die Zeit
        # auffrischen, damit "vor wie lange" stimmt.
        if liste and isinstance(liste[0], dict) and liste[0].get("t") == eintrag["t"]:
            liste[0]["ts"] = eintrag["ts"]
        else:
            liste.insert(0, eintrag)
        del liste[VERLAUF_MAX:]
        self._verlauf_merken()

    def _verlauf_merken(self):
        """Speichern verzoegern - bei jedem Songwechsel sofort auf die Platte
        zu schreiben waere teuer und braechte nichts."""
        self._verlauf_dirty = True
        if self._verlauf_task is None or self._verlauf_task.done():
            try:
                self._verlauf_task = asyncio.get_running_loop().create_task(
                    self._verlauf_spaeter())
            except RuntimeError:
                pass        # kein Loop (Tests) - der Stand steht trotzdem im RAM

    async def _verlauf_spaeter(self):
        await asyncio.sleep(15)
        await self.verlauf_speichern()

    async def verlauf_speichern(self):
        if self._store is not None and self._verlauf_dirty:
            self._verlauf_dirty = False
            await self._store.save()

    @staticmethod
    def _vor_wie_lange(ts):
        """'vor 12 Min' - grob und lesbar, nicht auf die Sekunde genau."""
        try:
            sek = max(0, int(time.time() - float(ts or 0)))
        except (TypeError, ValueError):
            return "gerade eben"
        if sek < 60:
            return "gerade eben"
        if sek < 3600:
            return f"vor {sek // 60} Min"
        if sek < 86400:
            std = sek // 3600
            return f"vor {std} Std" if std > 1 else "vor 1 Std"
        tage = sek // 86400
        return f"vor {tage} Tagen" if tage > 1 else "vor 1 Tag"

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

    def _voice_kaputt(self, exc, wo):
        """Die EINE Antwort, wenn player.connect scheitert - an jeder Stelle
        gleich. Der echte Grund (Rechte, Zeitueberschreitung, fehlendes davey)
        gehoert ins Log, nicht in den Chat."""
        log.error("Voice-Connect (%s) fehlgeschlagen: %s: %s", wo,
                  type(exc).__name__, exc)
        return self._embed(VOICE_KAPUTT, color=_COL_ERR)

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
        # Der Verlauf muss Neustarts ueberleben - der Player haelt nur die
        # letzten 30 im Arbeitsspeicher, und ausgerechnet nach einem Neustart
        # fragt man "was lief denn gestern?".
        self._store = JsonStore("musikverlauf.json", default={"guilds": {}})
        self._spotify_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self._spotify_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

        if yt_dlp is None:
            log.warning("Musik-Feature aus: Paket 'yt-dlp' ist nicht installiert.")
            return False
        if shutil.which("ffmpeg") is None:
            log.warning("Musik-Feature aus: 'ffmpeg' nicht gefunden (z. B. 'apt install ffmpeg').")
            return False
        fehlt = voice_fehlt()
        if fehlt:
            log.warning("Musik-Feature aus: %s", fehlt)
            return False

        self._enabled = True
        self._ytdlp_umgebung()
        spotify_ok = bool(self._spotify_id and self._spotify_secret)
        log.info(
            "Musik-Feature aktiv (YouTube: ja, Spotify: %s).",
            "ja" if spotify_ok else "nein - nur YouTube-Links",
        )
        return True

    @staticmethod
    def _deno_pfad():
        """Wo liegt deno? Erst im PATH, dann das pip-Paket (venv/bin).

        Unter systemd steht venv/bin NICHT im PATH des Dienstes - yt-dlp fand
        die JS-Laufzeit dann nicht, und YouTube spielte still nicht mehr ab."""
        pfad = shutil.which("deno")
        if pfad:
            return pfad
        neben_python = os.path.join(os.path.dirname(sys.executable), "deno")
        if os.path.isfile(neben_python) and os.access(neben_python, os.X_OK):
            return neben_python
        try:
            import deno
            return deno.find_deno_bin()
        except Exception:  # noqa: BLE001 - kein pip-deno: dann eben ohne
            return ""

    def _ytdlp_umgebung(self):
        """Einmal beim Start: Cache-Ordner und JS-Laufzeit fuer yt-dlp.

        cachedir: yt-dlp merkt sich dort, wie es YouTubes Player-Skript
        entschluesselt. Ohne Cache (vorher cachedir=False) rechnete es das fuer
        JEDEN Song neu aus - eine bis zwei Sekunden, bevor ueberhaupt Ton kam.
        Der Ordner liegt in data/, damit er Neustarts (und Docker) ueberlebt."""
        from store import DATA_DIR
        cache = os.path.join(str(DATA_DIR), "yt-dlp-cache")
        try:
            os.makedirs(cache, exist_ok=True)
            _YDL_OPTS["cachedir"] = cache
        except OSError as exc:
            log.warning("Musik: yt-dlp-Cache %s geht nicht (%s) - ohne Cache.", cache, exc)
        deno = self._deno_pfad()
        if deno:
            _YDL_OPTS["js_runtimes"] = {"deno": {"path": deno}}
        else:
            log.warning("Musik: keine JS-Laufzeit (deno) gefunden - YouTube laesst "
                        "dann viele Songs nicht durch. Abhilfe:  venv/bin/pip install "
                        "-r requirements.txt")

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
        """True, wenn die Musik den Voice-Channel dieses Servers WIRKLICH belegt:
        es laeuft ein Song, er ist pausiert, es wartet etwas in der Schlange oder
        gerade laeuft ein Songwechsel. Auch beim Tempo-Wechsel und waehrend eines
        Reconnects - da steht der Song ja noch in 'current'. voicegags fragt das,
        um nicht in den Musik-Voice-Client reinzugraetschen.

        Vorher reichte es, dass Flo in einem Kanal sein SOLLTE. Nach dem letzten
        Song blieb das fuer immer so - und Soundboard und TTS sagten bis zum
        Neustart "Gerade läuft was im Voice", obwohl da nur Stille war."""
        player = self._players.get(guild_id)
        if player is None:
            return False
        if (player.current is not None or player.queue or player.ist_pausiert()
                or player._advancing):
            return True
        vc = player.voice
        return vc is not None and (vc.is_playing() or vc.is_paused())

    async def flo_getrennt(self, guild_id, channel_id=None):
        """Flo ist aus dem Sprachkanal geflogen - und zwar nicht durch uns.

        bot.on_voice_state_update ruft das, wenn Flo selbst den Kanal verlassen
        hat (after.channel is None). Vorher hat der Watchdog ihn einfach wieder
        reingeholt, samt Musik: ein Moderator trennt Flo, 15 s spaeter sitzt er
        wieder drin. Jetzt gilt der Rauswurf - Warteschlange und Loop weg,
        active_channel_id aus, der Watchdog laesst ihn draussen.

        Nicht jede Trennung ist ein Rauswurf, deshalb wird erst geprueft:
          - 'Flo stop' hat active_channel_id schon vorher auf None gesetzt;
          - _fresh_connect/_reconnect haben sich angekuendigt;
          - discord.py trennt bei manchen Aussetzern selbst und verbindet neu -
            dann haelt der Server den Voice-Client aber weiter fest. Das zeigt
            sich erst nach einer kurzen Frist (VOICE_RAUSWURF_FRIST).
        guild_id darf auch das Guild-Objekt selbst sein.
        Rueckgabe: True = als Rauswurf behandelt."""
        gid = int(getattr(guild_id, "id", guild_id) or 0)
        player = self._players.get(gid)
        if player is None or player.active_channel_id is None:
            return False          # nichts offen, oder es war unser eigenes 'stop'
        if time.monotonic() < player._selbst_getrennt_bis:
            return False          # wir bauen gerade selbst neu auf
        player._rauswurf_bis = time.monotonic() + VOICE_RAUSWURF_FRIST + 10.0
        try:
            await asyncio.sleep(VOICE_RAUSWURF_FRIST)
            if player.active_channel_id is None:
                return False      # inzwischen selbst beendet
            if self._voice_client_lebt(guild_id, gid, player):
                return False      # discord.py hat ihn noch - nur ein Aussetzer
            war_musik = player.current is not None or bool(player.queue)
            log.warning("Flo wurde aus dem Sprachkanal %s geworfen (Server %s) - "
                        "Musik aus, Warteschlange (%d) geleert, bleibt draussen.",
                        channel_id or player.active_channel_id, gid, len(player.queue))
            await player.disconnect()
            if war_musik:
                await player._sag("Rausgekickt, echt jetzt? Na gut – Musik aus, "
                                  "Schlange weg. Viel Spaß mit der Stille.")
            return True
        finally:
            player._rauswurf_bis = float("-inf")

    @staticmethod
    def _voice_client_lebt(guild_ref, gid, player):
        """Haelt discord.py fuer diesen Server noch einen Voice-Client?

        Nach einem echten Rauswurf raeumt discord.py ihn weg (guild.voice_client
        wird None); bei seinem eigenen Neuaufbau bleibt er stehen."""
        guild = guild_ref if hasattr(guild_ref, "voice_client") else None
        if guild is None:
            import laufzeit
            client = laufzeit.client
            guild = client.get_guild(gid) if client is not None else None
        if guild is not None:
            return guild.voice_client is not None
        # Kein Server-Objekt zu bekommen (Tests, Werkzeuge): unser eigener Client.
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

    def _versuche(self):
        """In welcher Reihenfolge die player_clients gefragt werden.

        Hat einer zuletzt funktioniert, kommt ER zuerst - vorher fragte Flo
        bei JEDEM Song erst yt-dlps Standard, wartete 1-3 s auf dessen Absage
        und nahm dann erst den, von dem er schon wusste, dass er geht."""
        fest = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
        if fest:
            return [fest]
        reihe = self.client_reihe()
        if self._guter_client and reihe and reihe[0] == self._guter_client:
            return [reihe[0], None, *reihe[1:]]
        return [None, *reihe]

    def _client_merken(self, client, nr):
        """Der Client, der gerade durchkam, ist ab jetzt der gute. Kam der
        Standard (None) erst NACH dem gemerkten durch, taugt der gemerkte nicht
        mehr - vergessen, sonst waere er bei jedem Song wieder die Bremse."""
        if client:
            neu = client != self._guter_client
            self._guter_client = client
            return neu
        if nr > 0:
            self._guter_client = ""
        return False

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
        versuche = self._versuche()
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
            self._client_merken(client, nr)
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

        # Erst der, der zuletzt ging; dann so, wie yt-dlp es selbst fuer
        # richtig haelt (ausser es ist festgenagelt); dann die Ausweichliste.
        versuche = self._versuche()
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
                        if self._client_merken(client, nr):
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
            if self._client_merken(client, nr):
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
        if player.current is not None and len(player.queue) == 1:
            vorladen = getattr(player, "_vorladen_planen", None)
            if vorladen is not None:
                vorladen()

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

    def _link_ist_befehl(self, text):
        """Ist ein Musik-Link in diesem Text ein ABSPIEL-Auftrag - oder nur
        Gespraech ueber einen Link?

        Vorher reichte der Link allein: 'Flo was hältst du von dem Video
        https://youtu.be/…' spielte das Video ab (oder bekam "Geh erst in einen
        Sprachkanal"), statt dass Flo seine Meinung sagt. Jetzt zaehlt ein Link
        nur, wenn ausser ihm und dem Botnamen nichts dasteht (nackter Link), wenn
        ein Abspiel-Verb vorn steht ('spiel <link>') oder er in einer der
        natuerlichen Abspiel-Formen steckt ('mach mal <link> an', 'schau mal
        <link> an'). In allen anderen Faellen beantwortet die KI den Satz."""
        rest = self._clean_lead(_URL_RE.sub(f" {_LINK_PLATZ} ", text or ""))
        if _PLAY_TEXT_RE.match(rest):
            return True
        for pat in _NAT_PLAY_RES:
            nm = pat.match(rest)
            if nm and _LINK_PLATZ in nm.group(1).lower():
                return True
        woerter = [w for w in _restwoerter(rest) if w != _LINK_PLATZ]
        if all(w in _LINK_BEIWERK_NACKT for w in woerter):
            return True
        return (woerter[0] in _LINK_VERBEN
                and all(w in _LINK_BEIWERK for w in woerter[1:]))

    @staticmethod
    def _link_aktion(url):
        """Welche Aktion gehoert zu diesem (gesaeuberten) Link? None = kein
        Musik-Link (dann bleibt es beim Text-Befehl bzw. bei der KI)."""
        low = url.lower()
        if _SPOTIFY_KURZ_RE.match(url):
            # Kurzlink der Handy-App - das Ziel kennt erst der Redirect.
            return "spotify_kurz"
        m = _SPOTIFY_LIST_RE.search(url)
        if m:
            kind = (m.group(1) or m.group(2) or "").lower()
            return "spotify_album" if kind == "album" else "spotify_playlist"
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
                return "yt_playlist"
            return "play"
        if _SPOTIFY_TRACK_RE.search(url):
            return "play"
        if _AUDIO_DATEI_RE.search(url):
            # Direkter Audio-Link (.mp3/.m4a/...) - den kann FFmpeg selbst
            # abspielen. Vorher landete auch der in der YouTube-TEXTSUCHE,
            # also in einer Suche nach der URL-Zeichenkette. Bewusst NUR
            # bei eindeutigen Audio-Endungen: eine beliebige Webseite im
            # Satz ("was haeltst du von https://…") darf die Musik nicht
            # an sich reissen.
            return "play"
        if "spotify.com" in low or low.startswith("spotify:"):
            # Auffangnetz: JEDE Spotify-Adresse, die keiner der Zweige
            # oben kennt (Podcast-Episode, Show, Kuenstler-Seite, die alte
            # /user/<name>/playlist/-Form), landete bisher in der
            # YouTube-TEXTSUCHE - Flo suchte woertlich nach der URL und
            # spielte irgendein fremdes Video. Lieber ehrlich sagen, dass
            # es nicht geht.
            return "spotify_unbekannt"
        if _SC_RE.match(url):
            # Set -> Playlist, alles andere ganz normal als Track. Ein
            # Kurzlink (on.soundcloud.com) KANN auch ein Set sein - das
            # sieht man erst nach dem Redirect; das faengt _extract ab.
            return "sc_playlist" if _SC_SET_RE.match(url) else "play"
        return None

    def parse_command(self, text):
        """Erkennt einen Musik-Befehl. Rueckgabe: (aktion, argument) oder None.

        Aktionen: play, search, spotify_album, spotify_playlist, spotify_kurz,
                  yt_playlist, sc_playlist,
                  volume, skip, pause, resume, stop, leave, queue.
        """
        # 1) Link in der Nachricht? (staerkstes Signal) - aber nur, wenn er
        #    auch zum ABSPIELEN dasteht (siehe _link_ist_befehl).
        for roh in _URL_RE.findall(text):
            url = _url_saeubern(roh)
            aktion = self._link_aktion(url)
            if aktion is None:
                continue           # fremder Link - vielleicht kommt noch ein Musik-Link
            if not self._link_ist_befehl(text):
                return None        # Gespraech UEBER einen Link -> die KI antwortet
            return (aktion, url)

        cleaned = self._clean_lead(text)
        if not cleaned:
            return None

        # 2a0) Verlauf? MUSS vor dem Replay stehen: "nochmal verlauf" wuerde
        #      sonst als "nochmal" ohne Nummer gelesen und spielte den letzten
        #      Song, statt die Liste zu zeigen.
        if verlauf_befehl(cleaned):
            return ("history", "")

        # 2a) Wiederholen? (vor der Freitext-Suche, sonst wuerde "spiel nochmal"
        #     als Suche nach "nochmal" gedeutet.)
        rm = _REPLAY_RE.match(cleaned)
        if rm:
            rest = rm.group("rest") or ""
            # Ohne 'spiel' davor: hinter 'nochmal' darf GAR NICHTS stehen
            # ausser der Nummer - 'nochmal bitte' ist "sag's nochmal". Passt es
            # nicht, geht es unten weiter ('spiel nochmal despacito' ist eine
            # Suche, 'wiederhol das' landet bei der KI).
            eindeutig = (not _restwoerter(rest) if rm.group("spiel")
                         else not re.findall(r"[^\W_]+", rest))
            if eindeutig:
                return ("replay", rm.group("nr") or "1")

        # 2b) Loop? MUSS nach dem Replay stehen - sonst nichts, die Woerter
        #     ueberschneiden sich nicht.
        lo = _LOOP_RE.match(cleaned)
        if lo:
            return ("loop", (lo.group(1) or lo.group(2) or "").lower())

        # 2) Steuerbefehl am Satzanfang - und NUR, wenn dahinter kein Satz steht.
        steuer = _steuerbefehl(cleaned)
        if steuer == _EIN_SATZ:
            return None
        if steuer is not None:
            return steuer

        # 3) Lautstaerke? Relativ (lauter/leiser) oder absolut ("ls 30", "vol 80",
        #    Tippfehler ...). Ohne Zahl -> aktuelle Lautstaerke anzeigen ("?").
        for muster, richtung in ((_VOLUME_UP_RE, "+"), (_VOLUME_DOWN_RE, "-")):
            vr = muster.match(cleaned)
            if vr:
                rest = _restwoerter(cleaned[vr.end():])
                if rest and not re.fullmatch(_VOLUME_REL_OBJ, " ".join(rest), re.I):
                    return None     # 'leise rieselt der schnee'
                return ("volume", richtung)
        vm = _VOLUME_ARG_RE.match(cleaned)
        if vm and self._is_volume_word(vm.group(1)):
            return ("volume", vm.group(2) or "?")

        # 3b) "random" / "zufall" / "überrasch mich" -> Genre-Auswahl per Dropdown.
        #     (vor der Freitext-Suche, sonst wuerde nach "random" gesucht.)
        zm = _RANDOM_RE.match(cleaned)
        if zm:
            rest = _restwoerter(zm.group("rest") or "")
            if rest and not re.fullmatch(_RANDOM_OBJ, " ".join(rest), re.I):
                return None         # 'random frage', 'zufall oder nicht'
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

    def verlauf_eintrag(self, gid, nummer):
        """Eintrag Nummer N aus dem Verlauf (1 = neuester). (eintrag, fehler)."""
        eintraege = self.verlauf(gid)
        if not eintraege:
            return None, ("Noch keine Songs gespielt. Leg was auf: "
                          f"`{self._bot_name} spiel <titel>` 🎵")
        try:
            nr = int(nummer)
        except (TypeError, ValueError):
            return None, f"Das ist keine Nummer. `{self._bot_name} nochmal 3`"
        if nr < 1 or nr > len(eintraege):
            return None, (f"Nummer **{nr}** gibt es nicht – mein Verlauf hat "
                          f"**{len(eintraege)}** Songs. "
                          f"`{self._bot_name} history` zeigt sie dir.")
        return eintraege[nr - 1], ""

    @staticmethod
    def _verlauf_quelle(eintrag):
        """Womit sich der Eintrag wieder abspielen laesst - oder ""."""
        return ((eintrag.get("u") or "").strip()
                or (eintrag.get("q") or "").strip()
                or (eintrag.get("t") or "").strip())

    async def verlauf_abspielen(self, interaction, nummer):
        """Klick im Verlauf-Dropdown: diesen Song noch einmal spielen."""
        if not self._enabled or interaction.guild is None:
            await interaction.response.send_message("Musik ist gerade aus.", ephemeral=True)
            return
        eintrag, fehler = self.verlauf_eintrag(interaction.guild.id, nummer)
        if eintrag is None:
            await interaction.response.send_message(fehler, ephemeral=True)
            return
        quelle = self._verlauf_quelle(eintrag)
        if not quelle:
            await interaction.response.send_message(
                "Zu diesem Eintrag habe ich keine Quelle mehr – den kann ich "
                "leider nicht nochmal laden. 😕", ephemeral=True)
            return

        voice_state = getattr(interaction.user, "voice", None)
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Geh erst in einen Sprachkanal, dann leg ich los. 🎧", ephemeral=True)
            return

        # Aufloesen + Connect dauert laenger als Discords 3s-Frist.
        await interaction.response.defer()
        player = self._player_for(interaction.guild.id)
        player.text_channel = interaction.channel
        try:
            track = await self._extract(quelle, ausweich_text=eintrag.get("t") or "")
        except Exception as exc:  # noqa: BLE001 - yt-dlp wirft viele Fehlerarten
            art, satz = self.yt_fehler_deuten(exc)
            log.warning("Verlauf: %r nicht mehr abspielbar (%s)",
                        eintrag.get("t"), art)
            await interaction.followup.send(embed=self._embed(
                f"**{self._short(eintrag.get('t') or 'Der Song', 80)}** geht "
                f"nicht mehr: {satz}", color=_COL_ERR))
            return
        track.requested_by = interaction.user.display_name
        try:
            await player.connect(voice_state.channel)
        except VOICE_CONNECT_FEHLER as exc:
            await interaction.followup.send(embed=self._voice_kaputt(exc, "Verlauf"))
            return
        if player.is_active():
            self._einreihen(player, track)
            await interaction.followup.send(
                embed=self._added_embed(track, len(player.queue), len(player.queue)))
            return
        try:
            player.start(track)
        except Exception:  # noqa: BLE001
            log.exception("Verlauf-Track nicht abspielbar: %s", track.title)
            await interaction.followup.send(embed=self._embed(
                "Den Song konnte ich gerade nicht abspielen.", color=_COL_ERR))
            return
        await self._send_panel(player, track)

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
        except VOICE_CONNECT_FEHLER as exc:
            await interaction.followup.send(embed=self._voice_kaputt(exc, "Random"))
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
        except VOICE_CONNECT_FEHLER as exc:
            return self._voice_kaputt(exc, "Mehrfach")

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
                           speed = 1.0, loop = 0):
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
        # Ohne diese Anzeige sieht niemand, warum derselbe Song wiederkommt und
        # die Warteschlange stehenbleibt.
        lt = _loop_text(loop)
        if lt:
            e.add_field(name="Loop", value=lt, inline=True)
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
        """Raeumt das zuletzt gepostete Panel weg - der Song dazu ist vorbei bzw.
        wird gleich durch einen neuen ersetzt.

        Geloescht wird IM HINTERGRUND: vorher wartete der Songwechsel auf
        Discords Antwort zum Loeschen, bevor das neue Panel kam.

        KEIN view.stop(): das Panel besteht aus DynamicItems, und stop() nimmt
        deren Vorlagen global aus dem Register - danach waere jedes Panel tot.
        Merken muss sich discord.py so eine View ohnehin nicht (siehe MusikPanel)."""
        msg = player.panel_message
        player.panel_message = None
        player.panel_view = None
        if msg is not None:
            self._hintergrund(self._panel_loeschen(msg))

    @staticmethod
    async def _panel_loeschen(msg):
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

    async def _panel_auffrischen(self, player, track = None):
        """Das VORHANDENE Panel neu zeichnen - Nachricht bleibt stehen.

        Fuer den Loop: jeder Durchlauf laeuft ueber _advance und wuerde sonst
        ein komplett neues Panel posten (altes loeschen, neues senden). Bei
        einem kurzen Song ist das sichtbares Geflacker und laeuft ins
        Rate-Limit.

        Sendet AUSDRUECKLICH nie ein neues Panel: 'flo loop 3' beantwortet Flo
        schon selbst - ein zweites, ungefragtes Panel dazu waere Spam. Ist das
        Panel weg, meldet sich der naechste echte Songwechsel ohnehin mit
        einem frischen."""
        msg = player.panel_message
        track = track or player.current
        if msg is None or track is None:
            return
        try:
            await msg.edit(view=MusikPanel(player, track))
        except discord.HTTPException:
            # Panel geloescht -> abmelden. Der naechste Songwechsel postet
            # dann ganz normal ein neues.
            player.panel_message = None

    async def _send_panel(self, player, track, *,
                         reply_to = None, extra = ""):
        """Postet das 'Jetzt laeuft'-Panel (das alte kommt weg).

        Vom Auto-Loeschen ausgenommen ist es ueber ist_panel() in bot.py - an
        seinen Knoepfen erkennbar, nicht mehr am Embed-Titel (V2-Nachrichten
        haben keine Embeds)."""
        # Sende-Generation: zwischen dem Absenden und der Antwort von Discord
        # koennen Sekunden liegen. Startet in dieser Luecke schon der naechste
        # Song (Doppel-Skip), speicherte frueher der SPAETER zurueckkehrende,
        # aeltere Aufruf sein Panel - das neuere blieb als Zombie mit
        # klickbaren Knoepfen im Kanal stehen.
        player._panel_gen += 1
        meine_gen = player._panel_gen
        await self._retire_panel(player)
        msg = await self._panel_posten(player, track, reply_to, extra)
        if msg is None:
            return
        if meine_gen != player._panel_gen:
            # Ueberholt worden: das hier ist das ALTE Panel. Selbst wegraeumen,
            # statt das aktuelle zu ueberschreiben.
            self._hintergrund(self._panel_loeschen(msg))
            return
        player.panel_message = msg
        self._hintergrund(self._kanal_status(player, f"🎵 {_short(track.title, 90)}"))

    async def _panel_posten(self, player, track, reply_to, extra):
        """Erst als V2-Panel; lehnt Discord das ab, das alte Embed mit
        denselben (neustartfesten) Knoepfen. Antwort auf den Befehl, und wenn
        der schon weg ist (Aufraeum-Kanal), eben ohne Bezug."""
        versuche = (
            {"view": MusikPanel(player, track, extra=extra)},
            {"embed": self._now_playing_embed(track, len(player.queue), extra=extra,
                                              speed=player.speed, loop=player.loop_rest),
             "view": _klassisches_panel(player)},
        )
        for nr, kw in enumerate(versuche):
            if reply_to is not None:
                msg = await basis.antworte(reply_to, None, **kw)
            elif player.text_channel is not None:
                try:
                    msg = await player.text_channel.send(**kw)
                except discord.HTTPException as exc:
                    log.warning("Musik-Panel ging nicht raus: %s", exc)
                    msg = None
            else:
                return None
            if msg is not None:
                return msg
            if nr == 0:
                log.warning("Musik-Panel (V2) ging nicht raus - nehme das alte Layout.")
        log.error("Now-Playing-Panel fehlgeschlagen.")
        return None

    # --- Panel-Klicks -----------------------------------------------------------
    @staticmethod
    def _darf_steuern(interaction, player):
        """Nur wer mit im Voice sitzt, dreht an Flos Musik - Mods immer.

        Vorher konnte jeder im Textkanal die Musik stoppen oder skippen,
        waehrend er selbst gar nicht zuhoerte."""
        wer = interaction.user
        rechte = getattr(wer, "guild_permissions", None)
        if rechte is not None and (rechte.administrator or rechte.manage_guild
                                   or rechte.manage_channels or rechte.move_members):
            return True
        kanal = player.active_channel_id or getattr(
            getattr(player.voice, "channel", None), "id", None)
        if kanal is None:
            return True     # Flo ist gar nicht drin - da gibt es nichts zu schuetzen
        eigener = getattr(getattr(wer, "voice", None), "channel", None)
        return eigener is not None and eigener.id == kanal

    def ist_panel(self, message):
        """Ist diese Nachricht ein Musik-Panel? (bot.py nimmt es vom Auto-Loeschen
        aus.) Erkannt an den Knoepfen - das klappt auch, bevor Discords Antwort
        zum Senden da ist, und fuer Panels von vor einem Neustart."""
        return any(str(getattr(teil, "custom_id", "") or "").startswith("flo:musik:")
                   for teil in basis.bausteine(message))

    async def _panel_klick(self, interaction, aktion):
        player = self._players.get(interaction.guild_id)
        laeuft = player is not None and (player.current is not None or player.queue)
        if not laeuft:
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        if aktion in ("pause", "skip", "stop", "loop") and not self._darf_steuern(
                interaction, player):
            await interaction.response.send_message(random.choice(_ZAUNGAST),
                                                    ephemeral=True)
            return
        with ai.guild_kontext(interaction.guild_id or 0):
            if aktion == "pause":
                await self._klick_pause(interaction, player)
            elif aktion == "skip":
                # Genau derselbe Weg wie der Textbefehl (siehe player.skip).
                await interaction.response.defer()
                await player.skip()
            elif aktion == "stop":
                await self._klick_stop(interaction, player)
            elif aktion == "queue":
                await interaction.response.send_message(
                    embed=_queue_embed(player), ephemeral=True)
            elif aktion == "lyrics":
                await self._klick_lyrics(interaction, player)
            elif aktion == "loop":
                if player.current is None:
                    await interaction.response.send_message(
                        "Gerade läuft nichts, was ich wiederholen könnte.", ephemeral=True)
                    return
                await interaction.response.send_message(
                    f"Wie oft soll **{self._short(player.current.title, 70)}** "
                    f"wiederholt werden?",
                    view=_LoopView(player, interaction.user.id), ephemeral=True)

    async def _klick_pause(self, interaction, player):
        v = player.voice
        if v is None or not (v.is_playing() or v.is_paused()):
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        if player.ist_pausiert():
            player.fortsetzen()
        else:
            player.pausieren()
        await interaction.response.edit_message(view=MusikPanel(player))

    async def _klick_stop(self, interaction, player):
        # Dieses Panel wird gleich zur 'Gestoppt'-Anzeige - aus der Verwaltung
        # nehmen, damit disconnect()->_retire_panel es NICHT loescht.
        if player.panel_message is not None and interaction.message is not None \
                and player.panel_message.id == interaction.message.id:
            player.panel_message = None
        await player.disconnect()
        await interaction.response.edit_message(
            view=_gestoppt_panel(interaction.user.display_name))

    async def _klick_lyrics(self, interaction, player):
        track = player.current
        if track is None:
            await interaction.response.send_message("Gerade läuft nichts. 🤔", ephemeral=True)
            return
        # Nur der Klickende sieht den Text (ephemer) - kein Zuspammen des Channels.
        # Abruf kann dauern -> defer, sonst reisst die 3s-Frist.
        await interaction.response.defer(ephemeral=True)
        emb, view = await self._build_lyrics(
            track.title, getattr(track, "thumbnail", "") or None)
        if view is not None:
            await interaction.followup.send(embed=emb, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=emb, ephemeral=True)

    async def _panel_tempo(self, interaction, tempo):
        player = self._players.get(interaction.guild_id)
        v = getattr(player, "voice", None)
        if player is None or v is None or not (v.is_playing() or v.is_paused()):
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        if not self._darf_steuern(interaction, player):
            await interaction.response.send_message(random.choice(_ZAUNGAST),
                                                    ephemeral=True)
            return
        await interaction.response.defer()        # Tempo-Wechsel kann ~1s dauern
        await player.apply_speed(tempo)
        try:
            await interaction.edit_original_response(view=MusikPanel(player))
        except discord.HTTPException:
            pass

    # --- Sprachkanal-Status ("🎵 Titel" unter dem Kanalnamen) -------------------
    async def _kanal_status(self, player, text):
        """Setzt (oder loescht, text=None) den Status des Sprachkanals.

        Reine Zugabe: fehlt das Recht 'Sprachkanal-Status festlegen', passiert
        einfach nichts."""
        kanal = getattr(player.voice, "channel", None)
        if kanal is None or getattr(player, "_kanal_status_text", None) == text:
            return
        try:
            ich = kanal.guild.me
            if not kanal.permissions_for(ich).set_voice_channel_status:
                return
            await kanal.edit(status=text)
            player._kanal_status_text = text
        except Exception:  # noqa: BLE001 - Deko, darf nie etwas kippen
            log.debug("Sprachkanal-Status nicht gesetzt", exc_info=True)

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
        if arg == _NUR_MIT_MUSIK:
            # 'Flo halt', 'Flo hau ab', 'Flo weiter' sind auch Alltagsdeutsch.
            # Laeuft hier keine Musik, ist das eine Ansage an Flo und keine an
            # den Player - dann antwortet die KI, statt "Ich bin gerade in
            # keinem Sprachkanal." Und zwar BEVOR ein Player angelegt wird.
            arg = ""
            vorhanden = self._players.get(message.guild.id)
            if vorhanden is None or not vorhanden.sitzung_offen():
                return None
            if action == "resume" and not (vorhanden.ist_pausiert() or vorhanden.queue):
                return None
        player = self._player_for(message.guild.id)
        player.text_channel = message.channel

        # --- Wiederholen: den (N-t-)letzten Song aus dem Verlauf erneut spielen ---
        if action == "replay":
            # Aus dem DAUERHAFTEN Verlauf, nicht aus player.history: nur so
            # meint 'nochmal 3' denselben Song wie die 3 in 'flo history' -
            # und nur so ueberlebt es einen Neustart.
            try:
                idx = max(1, int(arg))
            except (TypeError, ValueError):
                idx = 1
            eintrag, fehler = self.verlauf_eintrag(message.guild.id, idx)
            if eintrag is None:
                return self._embed(fehler, color=_COL_ERR)
            again = self._verlauf_quelle(eintrag)
            if not again:
                return self._embed("Diesen Song kann ich leider nicht nochmal laden.",
                                   color=_COL_ERR)
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

        # --- Loop: den LAUFENDEN Song wiederholen ---------------------------
        if action == "loop":
            aus = ("aus", "off", "stop", "weg")
            endlos = ("an", "ein", "on", "endlos", "unendlich", "dauerhaft")
            if arg in aus:
                anzahl = 0
            elif arg in endlos:
                anzahl = -1
            elif numfmt.ist_zahl(arg):
                # 0 heisst 'aus'; nach oben gedeckelt, damit 'loop 99999' kein
                # Versehen ist, aus dem man nur noch per Stop rauskommt.
                anzahl = min(int(arg), LOOP_MAX) or 0
            else:
                # Nacktes 'flo loop' schaltet um: laeuft einer, ist er weg;
                # laeuft keiner, wird es die Dauerschleife.
                anzahl = 0 if player.loop_rest else -1
            if anzahl == 0:
                if not player.loop_rest:
                    return self._embed(
                        f"Es läuft gar kein Loop. `{self._bot_name} loop 3` startet einen.",
                        title="🔁  Loop", color=_COL_INFO)
                player.loop_setzen(0)
                await self._panel_auffrischen(player)
                return self._embed("Loop aus – nach diesem Durchlauf geht es normal weiter.",
                                   title="🔁  Loop aus", color=_COL_CTRL)
            if not player.loop_setzen(anzahl):
                return self._embed("Ich spiele gerade nichts, was ich wiederholen könnte.",
                                   color=_COL_ERR)
            await self._panel_auffrischen(player)
            titel = self._short(player.current.title, 90)
            if anzahl < 0:
                return self._embed(
                    f"**{titel}** läuft jetzt in Dauerschleife. "
                    f"`{self._bot_name} loop aus` beendet sie, Skip auch.",
                    title="🔁  Dauerschleife", color=_COL_PLAY)
            return self._embed(
                f"**{titel}** läuft noch **{anzahl}×**. "
                f"Solange wartet die Warteschlange.",
                title="🔁  Loop", color=_COL_PLAY)

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

        # "history"/"nochmal verlauf" -> die zuletzt gespielten Songs, blaetterbar.
        if action == "history":
            view = VerlaufView(message.guild.id, message.author.id)
            emb = view.embed()
            if not self.verlauf(message.guild.id):
                return emb              # leer -> kein Menue, nur der Hinweis
            try:
                view.message = await message.reply(embed=emb, view=view,
                                                   mention_author=False)
            except discord.HTTPException as exc:
                log.error("Verlauf konnte nicht gesendet werden: %s", exc)
                return self._embed("Den Verlauf konnte ich gerade nicht posten.",
                                   color=_COL_ERR)
            return HANDLED

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
            except VOICE_CONNECT_FEHLER as exc:
                return self._voice_kaputt(exc, "join")
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

        # Aufloesen (1-5 s) und Verbinden (0,5-2 s) brauchen einander nicht -
        # vorher lief beides nacheinander. Jetzt verbindet Flo schon, waehrend
        # er sucht, und zeigt sofort ein 🔎, damit man sieht, dass was passiert.
        vorab = self._vorab_verbinden(player, voice_state.channel)
        such_zeichen = asyncio.ensure_future(self._such_zeichen(message, True))
        try:
            return await self._einzeln_spielen(player, message, action, arg,
                                               voice_state)
        finally:
            such_zeichen.add_done_callback(
                lambda _t: self._hintergrund(self._such_zeichen(message, False)))
            if vorab is not None:
                self._hintergrund(self._vorab_aufraeumen(player, vorab))

    # --- Schneller Start: Suche und Voice gleichzeitig ------------------------
    def _hintergrund(self, coro):
        """Kleine Nebenarbeit (Reaktion weg, aufraeumen) - Referenz halten,
        sonst sammelt der Garbage Collector den Task mittendrin ein."""
        task = asyncio.ensure_future(coro)
        self._nebenbei.add(task)
        task.add_done_callback(self._nebenbei.discard)
        return task

    @staticmethod
    def _vorab_verbinden(player, kanal):
        """Schon mal verbinden, falls Flo noch nicht drin ist. Gibt den Task
        zurueck (oder None). Der spaetere player.connect wartet am Lock und
        findet die fertige Verbindung - doppelt verbunden wird nie."""
        voice = getattr(player, "voice", None)
        if voice is not None and voice.is_connected():
            return None
        return asyncio.ensure_future(player.connect(kanal))

    async def _vorab_aufraeumen(self, player, vorab):
        """Vorab verbunden, aber es kam kein Song zustande (nichts gefunden,
        gesperrt, Fehler) -> wieder raus, statt fuenf Minuten stumm im Kanal zu
        sitzen."""
        try:
            await vorab
        except Exception:  # noqa: BLE001 - der eigentliche connect meldet es
            return
        if (player.nichts_zu_tun() and not player.is_active()
                and not player._advancing):
            log.info("Musik: vorab verbunden, aber kein Song - gehe wieder raus.")
            await player.disconnect()

    async def _such_zeichen(self, message, an):
        """🔎 an die Nachricht, solange Flo sucht (und danach wieder weg)."""
        try:
            if an:
                await message.add_reaction("🔎")
            else:
                ich = getattr(getattr(message, "guild", None), "me", None)
                if ich is not None and hasattr(message, "remove_reaction"):
                    await message.remove_reaction("🔎", ich)
        except Exception:  # noqa: BLE001 - reine Deko, darf nie etwas kippen
            pass

    async def _einzeln_spielen(self, player, message, action, arg, voice_state):
        """Ein einzelner Song (Link oder Suche): aufloesen, verbinden, spielen
        oder einreihen."""
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
        except VOICE_CONNECT_FEHLER as exc:
            return self._voice_kaputt(exc, "play")

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
flo_getrennt = instance.flo_getrennt
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
_panel_auffrischen = instance._panel_auffrischen
ist_panel = instance.ist_panel
handle = instance.handle
verlauf = instance.verlauf
verlauf_notieren = instance.verlauf_notieren
verlauf_eintrag = instance.verlauf_eintrag
verlauf_speichern = instance.verlauf_speichern
