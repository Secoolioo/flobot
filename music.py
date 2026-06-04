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

MAX_QUEUE = 50          # Schutz: maximale Laenge der Warteschlange pro Server
DEFAULT_VOLUME = 0.5    # 0.0 - 1.0

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


@dataclass
class GuildPlayer:
    """Haelt Voice-Verbindung und Warteschlange fuer EINEN Server."""
    loop: asyncio.AbstractEventLoop
    queue: list[Track] = field(default_factory=list)
    voice: discord.VoiceClient | None = None
    current: Track | None = None
    text_channel: discord.abc.Messageable | None = None
    volume: float = DEFAULT_VOLUME   # 0.0 - 2.0, per Befehl aenderbar

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
            if self.text_channel is not None:
                await self.text_channel.send(f"▶️ Jetzt: **{track.title}**")
        except Exception:
            log.exception("Konnte naechsten Track nicht starten: %s", track.title)
            await self._advance()  # einen weiter

    async def disconnect(self) -> None:
        self.queue.clear()
        self.current = None
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
    """Entfernt @-Mentions und den fuehrenden Botnamen ('Flo, spiel ...')."""
    t = re.sub(r"<@!?\d+>", " ", text).strip()
    t = re.sub(rf"^\s*{re.escape(_bot_name)}\b[\s,:!.\-]*", "", t, flags=re.IGNORECASE)
    return t.strip()


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
) -> str:
    """Spielt mehrere Songs: ersten sofort, Rest lazy in die Warteschlange.

    items = Liste (yt-dlp-Eingabe, Anzeigetitel), label z. B. 'aus dem Album'.
    """
    try:
        await player.connect(channel)
    except discord.ClientException as exc:
        log.error("Voice-Connect fehlgeschlagen: %s", exc)
        return "Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?)."

    space = MAX_QUEUE - len(player.queue)
    if space <= 0:
        return f"Die Warteschlange ist voll ({MAX_QUEUE}). Warte kurz."
    items = items[:space]

    if player.is_active():
        for inp, title in items:
            player.queue.append(_lazy_track(inp, title, requested_by))
        return f"➕ {len(items)} Songs {label} in die Warteschlange."

    first_inp, _first_title = items[0]
    rest = items[1:]
    try:
        track = await _extract(first_inp)
    except Exception:  # noqa: BLE001
        log.exception("Erster Track nicht ladbar: %s", first_inp)
        return "Den ersten Song konnte ich nicht laden."
    track.requested_by = requested_by
    track.query = first_inp
    for inp, title in rest:
        player.queue.append(_lazy_track(inp, title, requested_by))
    player.start(track)
    if rest:
        return f"▶️ Spiele: **{track.title}** (+{len(rest)} weitere {label})"
    return f"▶️ Spiele: **{track.title}**"


# --- Oeffentlicher Einstieg ----------------------------------------------
async def handle(message: discord.Message) -> str | None:
    """Prueft, ob die Nachricht ein Musik-Befehl ist, und fuehrt ihn aus.

    Rueckgabe:
    - str  -> es war ein Musik-Befehl; der String ist die Antwort an den Chat.
    - None -> kein Musik-Befehl; die KI soll uebernehmen.
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
        return f"🔊 Lautstärke: {new}%."

    if action in ("stop", "leave"):
        if player.voice is None or not player.voice.is_connected():
            return "Ich bin gerade in keinem Sprachkanal."
        await player.disconnect()
        return "⏹️ Gestoppt und raus aus dem Sprachkanal."

    if action == "skip":
        if not player.is_active():
            return "Ich spiele gerade nichts."
        player.voice.stop()  # type: ignore[union-attr]  -> loest _after -> naechster Track
        return "⏭️ Übersprungen."

    if action == "pause":
        if player.voice is None or not player.voice.is_playing():
            return "Ich spiele gerade nichts."
        player.voice.pause()
        return "⏸️ Pausiert. (`Flo weiter` zum Fortsetzen)"

    if action == "resume":
        if player.voice is None or not player.voice.is_paused():
            return "Da ist nichts pausiert."
        player.voice.resume()
        return "▶️ Weiter geht's."

    if action == "queue":
        if not player.current and not player.queue:
            return "Die Warteschlange ist leer."
        lines = []
        if player.current:
            lines.append(f"▶️ Jetzt: **{player.current.title}**")
        for i, t in enumerate(player.queue[:10], start=1):
            lines.append(f"{i}. {t.title}")
        if len(player.queue) > 10:
            lines.append(f"... und {len(player.queue) - 10} weitere")
        return "\n".join(lines)

    # --- Abspielen: Nutzer muss im Sprachkanal sein ---
    voice_state = getattr(message.author, "voice", None)
    if voice_state is None or voice_state.channel is None:
        return "Geh erst in einen Sprachkanal, dann spiele ich dort."

    # --- Mehrere Songs auf einmal (Spotify-Album / YouTube-Playlist) ---
    if action == "spotify_album":
        queries = await _spotify_list_tracks(arg)
        if not queries:
            return "Das Spotify-Album konnte ich nicht laden (Token, privat oder leer?)."
        items = [(f"ytsearch1:{q}", q) for q in queries]
        return await _play_many(
            player, voice_state.channel, items,
            message.author.display_name, "aus dem Album",
        )

    if action == "spotify_playlist":
        queries = await _spotify_playlist_via_embed(arg)
        if not queries:
            return ("An diese Spotify-**Playlist** komme ich nicht ran - Spotify sperrt "
                    "den Playlist-Zugriff fuer Bots. Was sicher geht: ein Spotify-"
                    "**Album**, ein einzelner Song-Link oder eine **YouTube-Playlist**.")
        items = [(f"ytsearch1:{q}", q) for q in queries]
        return await _play_many(
            player, voice_state.channel, items,
            message.author.display_name, "aus der Playlist",
        )

    if action == "yt_playlist":
        entries = await _youtube_playlist(arg)
        if not entries:
            return "Die YouTube-Playlist konnte ich nicht laden (leer oder privat?)."
        return await _play_many(
            player, voice_state.channel, entries,
            message.author.display_name, "aus der Playlist",
        )

    if len(player.queue) >= MAX_QUEUE:
        return f"Die Warteschlange ist voll ({MAX_QUEUE}). Warte kurz."

    # Track aufloesen (Spotify -> Suchtext, sonst Link/Text direkt)
    try:
        if action == "play" and _SPOTIFY_TRACK_RE.search(arg):
            query = await _spotify_to_query(arg)
            if not query:
                return "Den Spotify-Link konnte ich nicht auflesen (Keys/Token?)."
            track = await _extract(f"ytsearch1:{query}")
        elif action == "play":
            track = await _extract(arg)
        else:  # search
            track = await _extract(f"ytsearch1:{arg}")
    except Exception:  # noqa: BLE001 - yt-dlp wirft viele verschiedene Fehler
        log.exception("Track konnte nicht aufgeloest werden: %s", arg)
        return "Den Song konnte ich nicht laden. Probier einen anderen Link oder Suchbegriff."

    track.requested_by = message.author.display_name

    try:
        await player.connect(voice_state.channel)
    except discord.ClientException as exc:
        log.error("Voice-Connect fehlgeschlagen: %s", exc)
        return "Ich komme gerade nicht in den Sprachkanal (Rechte? Schon verbunden?)."

    if player.is_active():
        player.queue.append(track)
        return f"➕ In die Warteschlange (#{len(player.queue)}): **{track.title}**"
    player.start(track)
    return f"▶️ Spiele: **{track.title}**"
