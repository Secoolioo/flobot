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
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field

import aiohttp
import discord

import ai

try:  # Optional: Bot soll auch ohne yt-dlp starten.
    import yt_dlp
except ImportError:  # pragma: no cover - nur relevant ohne Paket
    yt_dlp = None  # type: ignore[assignment]

log = logging.getLogger("dcbot.music")

# --- Konfiguration (in setup() aus der .env gelesen) ---------------------
_enabled: bool = False
_bot_name: str = "Flo"
_spotify_id: str = ""
_spotify_secret: str = ""

# Sentinel: das Modul hat selbst geantwortet (Embed + Buttons direkt gesendet).
# bot.py erkennt das und schickt KEINE zusaetzliche Antwort.
HANDLED = object()

MAX_QUEUE = 50          # Schutz: maximale Laenge der Warteschlange pro Server
DEFAULT_VOLUME = 0.5    # 0.0 - 1.0

# --- Optik: Farben + Embed-Helfer ----------------------------------------
_COL_PLAY = 0x1DB954     # Gruen  - laeuft / spielt
_COL_QUEUE = 0x5865F2    # Blurple - Warteschlange / hinzugefuegt
_COL_CTRL = 0xFEE75C     # Gelb   - Steuerung (Pause/Skip/Lautstaerke)
_COL_INFO = 0x95A5A6     # Grau   - neutrale Info
_COL_ERR = 0xED4245      # Rot    - geht gerade nicht


def _fmt_dur(secs: int | None) -> str:
    """Sekunden -> 'm:ss' bzw. 'h:mm:ss' (leer, wenn unbekannt)."""
    if not secs or secs <= 0:
        return ""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _short(text: str, limit: int = 60) -> str:
    """Kuerzt lange Titel fuer Listen (haelt Embed-Felder unter dem 1024er-Limit)."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _embed(desc: str = "", *, title: str | None = None, color: int = _COL_INFO) -> discord.Embed:
    """Kleiner Embed-Baukasten fuer einzeilige Antworten."""
    e = discord.Embed(color=color)
    if title:
        e.title = title
    if desc:
        e.description = desc
    return e

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
_PLAY_TEXT_RE = re.compile(r"^(?:spiele?|play)\s+(?:mal\s+)?(.+)", re.I)

# Lautstaerke: "flo lautstaerke 30", "flo volume 80", "flo lauter", "flo leiser".
_VOLUME_SET_RE = re.compile(
    r"^(?:lautstaerke|lautstärke|volume|vol)\s*(?:auf\s*)?(\d{1,3})", re.I
)
_VOLUME_UP_RE = re.compile(r"^(?:lauter|louder)\b", re.I)
_VOLUME_DOWN_RE = re.compile(r"^(?:leiser|quieter)\b", re.I)


# --- Spotify-Token (Client-Credentials, 1 h gueltig, hier gecached) ------
_sp_token = {"value": "", "exp": 0.0}


def setup() -> bool:
    """Liest die Konfiguration und prueft die Voraussetzungen.

    Rueckgabe: True, wenn das Musik-Feature aktiv ist.
    """
    global _enabled, _bot_name, _spotify_id, _spotify_secret

    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    _spotify_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    _spotify_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

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

    _enabled = True
    spotify_ok = bool(_spotify_id and _spotify_secret)
    log.info(
        "Musik-Feature aktiv (YouTube: ja, Spotify: %s).",
        "ja" if spotify_ok else "nein - nur YouTube-Links",
    )
    return True


def is_enabled() -> bool:
    return _enabled


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


@dataclass
class GuildPlayer:
    """Haelt Voice-Verbindung und Warteschlange fuer EINEN Server."""
    loop: asyncio.AbstractEventLoop
    queue: list[Track] = field(default_factory=list)
    voice: discord.VoiceClient | None = None
    current: Track | None = None
    text_channel: discord.abc.Messageable | None = None
    volume: float = DEFAULT_VOLUME   # 0.0 - 2.0, per Befehl aenderbar
    panel_message: "discord.Message | None" = None  # aktuelles Steuer-Panel

    async def connect(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        if self.voice and self.voice.is_connected():
            if self.voice.channel.id != channel.id:
                await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect(self_deaf=True)
        return self.voice

    def is_active(self) -> bool:
        return self.voice is not None and (self.voice.is_playing() or self.voice.is_paused())

    def start(self, track: Track) -> None:
        """Startet einen Track sofort (nutzt die bereits aufgeloeste Stream-URL)."""
        source = discord.FFmpegPCMAudio(
            track.stream_url, before_options=_FFMPEG_BEFORE, options=_FFMPEG_OPTS
        )
        self.current = track
        self.voice.play(  # type: ignore[union-attr]
            discord.PCMVolumeTransformer(source, self.volume),
            after=self._after,
        )

    def _after(self, error: Exception | None) -> None:
        # Laeuft in einem FFmpeg-Thread -> Arbeit zurueck in den Event-Loop schieben.
        if error:
            log.error("FFmpeg/Player-Fehler: %s", error)
        asyncio.run_coroutine_threadsafe(self._advance(), self.loop)

    async def _advance(self) -> None:
        if not self.voice or not self.voice.is_connected() or not self.queue:
            self.current = None
            return
        track = self.queue.pop(0)
        try:
            if not track.stream_url and track.query:
                track = await _resolve_track(track)  # Playlist-Track erst jetzt aufloesen
            self.start(track)
            await _send_panel(self, track)
        except Exception:
            log.exception("Konnte naechsten Track nicht starten: %s", track.title)
            await self._advance()  # einen weiter

    async def disconnect(self) -> None:
        self.queue.clear()
        self.current = None
        await _retire_panel(self)
        if self.voice is not None:
            try:
                await self.voice.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
            self.voice = None


_players: dict[int, GuildPlayer] = {}


def _player_for(guild_id: int) -> GuildPlayer:
    player = _players.get(guild_id)
    if player is None:
        player = GuildPlayer(loop=asyncio.get_running_loop())
        _players[guild_id] = player
    return player


# --- yt-dlp / Spotify Helfer ---------------------------------------------
async def _extract(query_or_url: str) -> Track:
    """Loest einen YouTube-Link ODER Suchtext zu einem abspielbaren Track auf."""
    loop = asyncio.get_running_loop()

    def work() -> dict:
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


async def _resolve_track(track: Track) -> Track:
    """Loest einen vorgemerkten Track auf. track.query = komplette yt-dlp-Eingabe
    (direkte URL ODER 'ytsearch1:Kuenstler - Titel')."""
    resolved = await _extract(track.query)
    resolved.requested_by = track.requested_by
    resolved.query = track.query
    return resolved


def _lazy_track(extract_input: str, title: str, requested_by: str) -> Track:
    """Noch nicht aufgeloester Track (wird erst beim Abspielen geladen).
    extract_input = yt-dlp-Eingabe (URL oder 'ytsearch1:...'), title = Anzeigename.
    """
    return Track(
        title=title, stream_url="", query=extract_input, requested_by=requested_by
    )


async def _youtube_playlist(url: str) -> list[tuple[str, str]] | None:
    """YouTube-Playlist -> Liste (video_url, titel). Schnell via extract_flat;
    die einzelnen Videos werden erst beim Abspielen aufgeloest."""
    loop = asyncio.get_running_loop()
    opts = dict(_YDL_OPTS)
    opts["noplaylist"] = False
    opts["extract_flat"] = "in_playlist"
    opts["playlistend"] = MAX_QUEUE
    opts["ignoreerrors"] = True  # einzelne kaputte Videos ueberspringen, nicht crashen

    def work() -> dict | None:
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
    out: list[tuple[str, str]] = []
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


async def _spotify_token() -> str:
    """Holt (und cached) ein Spotify-App-Token (Client-Credentials-Flow)."""
    if not (_spotify_id and _spotify_secret):
        return ""
    now = time.time()
    if _sp_token["value"] and _sp_token["exp"] > now + 30:
        return _sp_token["value"]  # type: ignore[return-value]

    auth = base64.b64encode(f"{_spotify_id}:{_spotify_secret}".encode()).decode()
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

    _sp_token["value"] = data.get("access_token", "")
    _sp_token["exp"] = now + float(data.get("expires_in", 3600))
    return _sp_token["value"]  # type: ignore[return-value]


async def _spotify_to_query(url: str) -> str | None:
    """Spotify-Track-Link -> 'Kuenstler - Titel' (fuer die YouTube-Suche)."""
    m = _SPOTIFY_TRACK_RE.search(url)
    if not m:
        return None
    token = await _spotify_token()
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

    name = data.get("name", "")
    artists = ", ".join(a.get("name", "") for a in data.get("artists", []))
    query = f"{artists} - {name}".strip(" -")
    return query or None


async def _spotify_list_tracks(url: str) -> list[str] | None:
    """Spotify-Playlist-/Album-Link -> Liste 'Kuenstler - Titel' (max. MAX_QUEUE)."""
    m = _SPOTIFY_LIST_RE.search(url)
    if not m:
        return None
    kind = (m.group(1) or m.group(2) or "").lower()
    list_id = m.group(3)
    token = await _spotify_token()
    if not token:
        return None

    if kind == "playlist":
        next_url = (
            f"https://api.spotify.com/v1/playlists/{list_id}/tracks"
            "?limit=100&fields=items(track(name,artists(name))),next"
        )
    else:  # album
        next_url = f"https://api.spotify.com/v1/albums/{list_id}/tracks?limit=50"

    queries: list[str] = []
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            while next_url and len(queries) < MAX_QUEUE:
                async with s.get(next_url, headers=headers) as r:
                    if r.status != 200:
                        log.error("Spotify-%s-Abruf fehlgeschlagen (HTTP %s).", kind, r.status)
                        break
                    data = await r.json()
                for item in data.get("items", []):
                    tr = item.get("track") if kind == "playlist" else item
                    if not tr:
                        continue
                    name = tr.get("name", "")
                    artists = ", ".join(a.get("name", "") for a in tr.get("artists", []))
                    q = f"{artists} - {name}".strip(" -")
                    if q:
                        queries.append(q)
                next_url = data.get("next")
    except (aiohttp.ClientError, OSError) as exc:
        log.error("Spotify nicht erreichbar: %s", exc)
        return None
    return queries


def _deep_find(obj: object, key: str) -> object:
    """Sucht rekursiv den ersten Wert zu 'key' in verschachtelten dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _deep_find(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _deep_find(value, key)
            if found is not None:
                return found
    return None


async def _spotify_playlist_via_embed(url: str) -> list[str] | None:
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

    track_list = _deep_find(data, "trackList")
    if not isinstance(track_list, list) or not track_list:
        log.error("Spotify-Embed: keine Songliste im JSON gefunden.")
        return None

    queries: list[str] = []
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
def _clean_lead(text: str) -> str:
    """Entfernt @-Mentions und den fuehrenden Botnamen/Alias ('Florian, spiel ...'
    -> 'spiel ...'). Zentral in ai.strip_lead, damit alle Module gleich reagieren
    (so gehen Musik-Befehle auch mit dem Alias 'Florian', nicht nur 'Flo')."""
    return ai.strip_lead(text)


def parse_command(text: str) -> tuple[str, str] | None:
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

    cleaned = _clean_lead(text)
    if not cleaned:
        return None

    # 2) Steuerbefehl am Satzanfang?
    for action, pattern in _CONTROL:
        if pattern.match(cleaned):
            return (action, "")

    # 3) Lautstaerke? ("lautstaerke 30", "volume 80", "lauter", "leiser")
    m = _VOLUME_SET_RE.match(cleaned)
    if m:
        return ("volume", m.group(1))
    if _VOLUME_UP_RE.match(cleaned):
        return ("volume", "+")
    if _VOLUME_DOWN_RE.match(cleaned):
        return ("volume", "-")

    # 4) "spiel <suchbegriff>" ohne Link -> YouTube-Suche
    m = _PLAY_TEXT_RE.match(cleaned)
    if m:
        return ("search", m.group(1).strip())

    return None


async def _play_many(
    player: GuildPlayer,
    channel: discord.VoiceChannel,
    items: list[tuple[str, str]],
    requested_by: str,
    label: str,
    reply_to: "discord.Message | None" = None,
) -> "discord.Embed | object":
    """Spielt mehrere Songs: ersten sofort, Rest lazy in die Warteschlange.

    items = Liste (yt-dlp-Eingabe, Anzeigetitel), label z. B. 'aus dem Album'.
    Rueckgabe: Embed (eingereiht/Fehler) ODER HANDLED (frisch gestartet -> Panel).
    """
    try:
        await player.connect(channel)
    except discord.ClientException as exc:
        log.error("Voice-Connect fehlgeschlagen: %s", exc)
        return _embed("Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                      color=_COL_ERR)

    space = MAX_QUEUE - len(player.queue)
    if space <= 0:
        return _embed(f"Die Warteschlange ist voll ({MAX_QUEUE}). Warte kurz.", color=_COL_ERR)
    items = items[:space]

    if player.is_active():
        for inp, title in items:
            player.queue.append(_lazy_track(inp, title, requested_by))
        return _embed(
            f"**{len(items)}** Songs {label} eingereiht – ab **#{len(player.queue) - len(items) + 1}** "
            f"in der Warteschlange.",
            title="➕  Zur Warteschlange hinzugefügt", color=_COL_QUEUE,
        )

    first_inp, _first_title = items[0]
    rest = items[1:]
    try:
        track = await _extract(first_inp)
    except Exception:  # noqa: BLE001
        log.exception("Erster Track nicht ladbar: %s", first_inp)
        return _embed("Den ersten Song konnte ich nicht laden.", color=_COL_ERR)
    track.requested_by = requested_by
    track.query = first_inp
    for inp, title in rest:
        player.queue.append(_lazy_track(inp, title, requested_by))
    player.start(track)
    extra = f"+{len(rest)} weitere {label}" if rest else ""
    await _send_panel(player, track, reply_to=reply_to, extra=extra)
    return HANDLED


# --- Optik: groessere Embeds ---------------------------------------------
def _title_value(track: "Track") -> str:
    """Titel als Link (falls webpage_url bekannt), sonst fett."""
    if track.webpage_url:
        return f"**[{_short(track.title, 90)}]({track.webpage_url})**"
    return f"**{_short(track.title, 90)}**"


def _now_playing_embed(track: "Track", queue_len: int = 0, extra: str = "") -> discord.Embed:
    """Schoenes 'Jetzt laeuft'-Embed mit Dauer, Wunsch-Person und Thumbnail."""
    e = discord.Embed(title="▶️  Jetzt läuft", description=_title_value(track),
                      color=_COL_PLAY)
    dur = _fmt_dur(track.duration)
    if dur:
        e.add_field(name="Länge", value=f"`{dur}`", inline=True)
    if track.requested_by:
        e.add_field(name="Gewünscht von", value=track.requested_by, inline=True)
    if queue_len > 0:
        e.add_field(name="In der Schlange", value=f"{queue_len} Song(s)", inline=True)
    if extra:
        e.set_footer(text=extra)
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    return e


def _added_embed(track: "Track", position: int, total: int, *,
                title: str = "➕  Zur Warteschlange hinzugefügt",
                footer: str | None = None) -> discord.Embed:
    """Embed fuer einen frisch eingereihten Song."""
    e = discord.Embed(title=title, description=_title_value(track), color=_COL_QUEUE)
    e.add_field(name="Position", value=f"**#{position}** von {total}", inline=True)
    dur = _fmt_dur(track.duration)
    if dur:
        e.add_field(name="Länge", value=f"`{dur}`", inline=True)
    if track.requested_by:
        e.add_field(name="Von", value=track.requested_by, inline=True)
    if footer:
        e.set_footer(text=footer)
    if track.thumbnail:
        e.set_thumbnail(url=track.thumbnail)
    return e


def _gone_embed(track: "Track") -> discord.Embed:
    return _embed(f"**{_short(track.title, 90)}** ist nicht mehr in der Warteschlange.",
                  title="⌛  Schon durch", color=_COL_INFO)


def _queue_embed(player: "GuildPlayer") -> discord.Embed:
    """Uebersichtliche Warteschlange: aktueller Song + naechste 10."""
    e = discord.Embed(title="🎶  Warteschlange", color=_COL_QUEUE)
    if player.current:
        dur = _fmt_dur(player.current.duration)
        cur = f"**{_short(player.current.title, 80)}**"
        if dur:
            cur += f"  ·  `{dur}`"
        e.add_field(name="▶️  Jetzt", value=cur, inline=False)
    if player.queue:
        lines = []
        for i, t in enumerate(player.queue[:10], start=1):
            dur = _fmt_dur(t.duration)
            line = f"`{i:>2}.`  {_short(t.title, 55)}"
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


# --- Interaktiv: Position in der Warteschlange aendern --------------------
class _PositionModal(discord.ui.Modal):
    """Tippfeld fuer eine konkrete Wunsch-Position."""

    def __init__(self, view: "QueuePositionView") -> None:
        super().__init__(title="Position in der Warteschlange")
        self._view = view
        self.feld = discord.ui.TextInput(
            label="Position (1 = als Nächstes)",
            placeholder=f"1 – {max(1, len(view.player.queue))}",
            required=True, max_length=3,
        )
        self.add_item(self.feld)

    async def on_submit(self, interaction: discord.Interaction) -> None:
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


class QueuePositionView(discord.ui.View):
    """Buttons unter einem frisch hinzugefuegten Song: an Position vorziehen."""

    def __init__(self, player: "GuildPlayer", track: "Track", owner_id: int,
                *, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.player = player
        self.track = track
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id == self.owner_id or (perms and perms.manage_messages):
            return True
        await interaction.response.send_message(
            "Nur wer den Song hinzugefügt hat (oder das Team) darf die Position ändern.",
            ephemeral=True)
        return False

    def _index(self) -> int | None:
        """Aktuelle Stelle des Tracks (per Identitaet, da er weiterrueckt)."""
        for i, t in enumerate(self.player.queue):
            if t is self.track:
                return i
        return None

    def apply_move(self, target_index: int) -> discord.Embed | None:
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
    async def _next(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        emb = self.apply_move(0)
        if emb is None:
            await interaction.response.edit_message(embed=_gone_embed(self.track), view=None)
            self.stop()
            return
        await interaction.response.edit_message(embed=emb, view=self)

    @discord.ui.button(label="Position wählen", emoji="📍", style=discord.ButtonStyle.secondary)
    async def _choose(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self._index() is None:
            await interaction.response.edit_message(embed=_gone_embed(self.track), view=None)
            self.stop()
            return
        await interaction.response.send_modal(_PositionModal(self))

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PlaybackControlView(discord.ui.View):
    """Steuerpanel unter 'Jetzt laeuft': Pause/Weiter, Skip, Stop, Queue.

    timeout=None: bleibt fuer die ganze (ggf. lange) Songdauer aktiv. Beim Posten
    eines neuen Panels wird das alte ueber _send_panel sauber entschaerft.
    """

    def __init__(self, player: "GuildPlayer") -> None:
        super().__init__(timeout=None)
        self.player = player
        self.message: discord.Message | None = None
        self._sync_pause()

    def _sync_pause(self) -> None:
        """Pause-Button passend zum aktuellen Zustand beschriften."""
        v = self.player.voice
        paused = bool(v and v.is_paused())
        self._pause.label = "Weiter" if paused else "Pause"
        self._pause.emoji = "▶️" if paused else "⏸️"
        self._pause.style = (discord.ButtonStyle.success if paused
                             else discord.ButtonStyle.secondary)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def _pause(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        v = self.player.voice
        if v is None or not (v.is_playing() or v.is_paused()):
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        if v.is_paused():
            v.resume()
        else:
            v.pause()
        self._sync_pause()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.primary)
    async def _skip(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if not self.player.is_active():
            await interaction.response.send_message("Gerade läuft nichts.", ephemeral=True)
            return
        # stop() loest _after -> _advance aus; _advance postet ein frisches Panel
        # und entschaerft dabei dieses hier. Darum nur kurz bestaetigen.
        self.player.voice.stop()  # type: ignore[union-attr]
        await interaction.response.defer()

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def _stop(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if self.player.voice is None or not self.player.voice.is_connected():
            await interaction.response.send_message("Ich bin in keinem Sprachkanal.", ephemeral=True)
            return
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
    async def _queue(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=_queue_embed(self.player), ephemeral=True)


async def _retire_panel(player: "GuildPlayer") -> None:
    """Entfernt die Buttons unter dem zuletzt geposteten Steuer-Panel."""
    msg = player.panel_message
    player.panel_message = None
    if msg is not None:
        try:
            await msg.edit(view=None)
        except discord.HTTPException:
            pass


async def _send_panel(player: "GuildPlayer", track: "Track", *,
                     reply_to: "discord.Message | None" = None, extra: str = "") -> None:
    """Postet ein 'Jetzt laeuft'-Panel mit Steuer-Buttons (altes wird entschaerft)."""
    await _retire_panel(player)
    emb = _now_playing_embed(track, len(player.queue), extra=extra)
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
async def handle(message: discord.Message) -> "str | discord.Embed | object | None":
    """Prueft, ob die Nachricht ein Musik-Befehl ist, und fuehrt ihn aus.

    Rueckgabe:
    - discord.Embed -> es war ein Musik-Befehl; bot.py schickt das Embed.
    - HANDLED        -> das Modul hat selbst geantwortet (Embed + Buttons).
    - None           -> kein Musik-Befehl; die KI soll uebernehmen.
    """
    if not _enabled or message.guild is None:
        return None

    cmd = parse_command(message.content or "")
    if cmd is None:
        return None
    action, arg = cmd
    player = _player_for(message.guild.id)
    player.text_channel = message.channel

    # --- Steuerbefehle, die keine Voice-Verbindung voraussetzen ---
    if action == "volume":
        cur = int(round(player.volume * 100))
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
        return _embed(f"Lautstärke steht jetzt auf **{new}%**.",
                      title=f"{bar}  Lautstärke", color=_COL_CTRL)

    if action in ("stop", "leave"):
        if player.voice is None or not player.voice.is_connected():
            return _embed("Ich bin gerade in keinem Sprachkanal.", color=_COL_ERR)
        await player.disconnect()
        return _embed("Musik gestoppt, Warteschlange geleert und raus aus dem Sprachkanal.",
                      title="⏹️  Gestoppt", color=_COL_INFO)

    if action == "skip":
        if not player.is_active():
            return _embed("Ich spiele gerade nichts.", color=_COL_ERR)
        skipped = player.current.title if player.current else ""
        player.voice.stop()  # type: ignore[union-attr]  -> loest _after -> naechster Track
        desc = f"**{_short(skipped, 90)}** übersprungen." if skipped else "Übersprungen."
        return _embed(desc, title="⏭️  Skip", color=_COL_CTRL)

    if action == "pause":
        if player.voice is None or not player.voice.is_playing():
            return _embed("Ich spiele gerade nichts.", color=_COL_ERR)
        player.voice.pause()
        return _embed(f"Pausiert. Sag `{_bot_name} weiter`, wenn's weitergehen soll.",
                      title="⏸️  Pause", color=_COL_CTRL)

    if action == "resume":
        if player.voice is None or not player.voice.is_paused():
            return _embed("Da ist nichts pausiert.", color=_COL_ERR)
        player.voice.resume()
        return _embed("Weiter geht's.", title="▶️  Fortgesetzt", color=_COL_PLAY)

    if action == "queue":
        if not player.current and not player.queue:
            return _embed("Die Warteschlange ist leer – wirf was rein!",
                          title="🎶  Warteschlange", color=_COL_INFO)
        return _queue_embed(player)

    if action == "join":
        # Nur in den Sprachkanal kommen (ohne etwas abzuspielen).
        voice_state = getattr(message.author, "voice", None)
        if voice_state is None or voice_state.channel is None:
            return _embed("Geh erst in einen Sprachkanal, dann komme ich dazu.", color=_COL_ERR)
        try:
            await player.connect(voice_state.channel)
        except RuntimeError as exc:  # discord.py >= 2.7 ohne davey
            log.error("Voice nicht moeglich (join): %s", exc)
            return _embed("Voice ist hier gerade nicht eingerichtet "
                          "(auf dem Server fehlt vermutlich `davey`).", color=_COL_ERR)
        except discord.ClientException as exc:
            log.error("Voice-Connect (join) fehlgeschlagen: %s", exc)
            return _embed("Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                          color=_COL_ERR)
        return _embed(f"Bin da in **{voice_state.channel.name}**. "
                      f"Sag z. B. `{_bot_name} spiel <song>`.",
                      title="👋  Eingeklinkt", color=_COL_PLAY)

    # --- Abspielen: Nutzer muss im Sprachkanal sein ---
    voice_state = getattr(message.author, "voice", None)
    if voice_state is None or voice_state.channel is None:
        return _embed("Geh erst in einen Sprachkanal, dann spiele ich dort.", color=_COL_ERR)

    # --- Mehrere Songs auf einmal (Spotify-Album / YouTube-Playlist) ---
    if action == "spotify_album":
        queries = await _spotify_list_tracks(arg)
        if not queries:
            return _embed("Das Spotify-Album konnte ich nicht laden (Token, privat oder leer?).",
                          color=_COL_ERR)
        items = [(f"ytsearch1:{q}", q) for q in queries]
        return await _play_many(
            player, voice_state.channel, items,
            message.author.display_name, "aus dem Album", reply_to=message,
        )

    if action == "spotify_playlist":
        queries = await _spotify_playlist_via_embed(arg)
        if not queries:
            return _embed(
                "An diese Spotify-**Playlist** komme ich nicht ran – Spotify sperrt den "
                "Playlist-Zugriff für Bots. Was sicher geht: ein Spotify-**Album**, ein "
                "einzelner Song-Link oder eine **YouTube-Playlist**.",
                title="🚫  Playlist gesperrt", color=_COL_ERR)
        items = [(f"ytsearch1:{q}", q) for q in queries]
        return await _play_many(
            player, voice_state.channel, items,
            message.author.display_name, "aus der Playlist", reply_to=message,
        )

    if action == "yt_playlist":
        entries = await _youtube_playlist(arg)
        if not entries:
            return _embed("Die YouTube-Playlist konnte ich nicht laden (leer oder privat?).",
                          color=_COL_ERR)
        return await _play_many(
            player, voice_state.channel, entries,
            message.author.display_name, "aus der Playlist", reply_to=message,
        )

    if len(player.queue) >= MAX_QUEUE:
        return _embed(f"Die Warteschlange ist voll ({MAX_QUEUE}). Warte kurz.", color=_COL_ERR)

    # Track aufloesen (Spotify -> Suchtext, sonst Link/Text direkt)
    try:
        if action == "play" and _SPOTIFY_TRACK_RE.search(arg):
            query = await _spotify_to_query(arg)
            if not query:
                return _embed("Den Spotify-Link konnte ich nicht auflösen (Keys/Token?).",
                              color=_COL_ERR)
            track = await _extract(f"ytsearch1:{query}")
        elif action == "play":
            track = await _extract(arg)
        else:  # search
            track = await _extract(f"ytsearch1:{arg}")
    except Exception:  # noqa: BLE001 - yt-dlp wirft viele verschiedene Fehler
        log.exception("Track konnte nicht aufgeloest werden: %s", arg)
        return _embed("Den Song konnte ich nicht laden. Probier einen anderen Link "
                      "oder Suchbegriff.", color=_COL_ERR)

    track.requested_by = message.author.display_name

    try:
        await player.connect(voice_state.channel)
    except discord.ClientException as exc:
        log.error("Voice-Connect fehlgeschlagen: %s", exc)
        return _embed("Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?).",
                      color=_COL_ERR)

    # Es laeuft schon was -> einreihen. Ab >=2 wartenden Songs gibt's Buttons,
    # mit denen die Person ihren frischen Song an eine Wunsch-Position zieht.
    if player.is_active():
        player.queue.append(track)
        pos = len(player.queue)
        if pos >= 2:
            view = QueuePositionView(player, track, message.author.id)
            emb = _added_embed(track, pos, pos,
                               footer="⏭️ = als Nächstes · 📍 = Position wählen")
            try:
                view.message = await message.reply(embed=emb, view=view, mention_author=False)
            except discord.HTTPException as exc:
                log.error("Queue-Embed mit Buttons fehlgeschlagen: %s", exc)
                return emb  # Notfall: wenigstens das Embed ohne Buttons
            log.info("In Warteschlange (#%d) + Position-Buttons: %s", pos, track.title)
            return HANDLED
        return _added_embed(track, pos, pos)

    player.start(track)
    await _send_panel(player, track, reply_to=message)
    return HANDLED
