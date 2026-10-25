"""Aus eingefuegten Zeilen werden Playlist-Eintraege (Links, Alben, Suchtext).

Aufgerufen vom Playlist-Assistenten (playlists.py) und von
music._einzeln_spielen / dem Album-Zweig in music.handle fuer Spotify ohne
Schluessel. Beim Import passiert nichts - kein Netz, kein Speicher.

    eintraege(zeile, platz=...) -> (songs, art, fehler)
        songs  Liste von Eintraegen {"i": yt-dlp-Eingabe, "t": Titel,
               "h": Match-Hinweis oder None, "d": Dauer, "q": die Zeile}
        art    was die Zeile war ('youtube', 'soundcloud', 'datei', 'spotify',
               'yt_playlist', 'sc_playlist', 'spotify_album',
               'spotify_playlist', 'text', 'unbekannt')
        fehler ein Satz fuer den Chat, wenn nichts zu holen war - sonst None
    spotify_track_ohne_keys(url) -> dict | None
        Titel/Kuenstler/Dauer eines Spotify-Songs ohne API-Schluessel
    spotify_liste_ohne_keys(url, ende=None) -> list | None
        dasselbe fuer ein ganzes Album / eine Playlist

Spotify ohne Schluessel
=======================
Die Web-API will ein App-Token - ohne SPOTIFY_CLIENT_ID/SECRET gibt es keins,
und fuer Playlists sperrt sie Client-Credentials-Apps ohnehin (HTTP 403). Die
oeffentliche Einbettungsseite (open.spotify.com/embed/<art>/<id>) braucht
nichts davon: sie liefert Titel, Kuenstler, Dauer und bei Alben/Playlists die
ganze Songliste als JSON im Seitenquelltext (__NEXT_DATA__).

Der Aufbau dieses JSON gehoert Spotify und aendert sich ohne Ankuendigung.
Deshalb wird hier nichts an festen Pfaden gelesen: gesucht wird der erste
'entity'-Block (oder eine 'trackList') irgendwo im Baum, jedes Feld mit
Ausweichnamen. Klappt das nicht, bleibt fuer Songs noch Spotifys oEmbed (nur
der Titel). Ob die Strecke auf dem Server WIRKLICH geht, zeigt die Live-Probe
in tools_musik_check.py ('bash k m').

Titel einzelner YouTube-/SoundCloud-Links kommen ueber oEmbed (ein kleiner
JSON-Abruf, kein yt-dlp): 401/403/404 heisst privat oder geloescht - dann sagt
Flo das gleich beim Einfuegen statt erst Wochen spaeter beim Abspielen.
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import urllib.parse

import aiohttp

import ai
import music

log = logging.getLogger("dcbot.musikquellen")


def _env_sekunden(name, vorgabe):
    """Eine Frist aus der .env - Tippfehler dort duerfen den Import nicht kippen."""
    try:
        wert = float(os.getenv(name, "") or vorgabe)
    except ValueError:
        return vorgabe
    return wert if wert > 0 else vorgabe


#: So lange darf ein einzelner Abruf (oEmbed, Einbettungsseite) dauern.
FRIST = _env_sekunden("MUSIKQUELLEN_FRIST", 5.0)
#: Hoechstens so viele Songs holt Flo aus EINEM Album / einer Playlist.
ERWEITERUNG_MAX = 50
#: Die Einbettungsseite eines Albums/einer Playlist ist gross - so lange darf
#: sie dauern (so lange wie frueher music._spotify_playlist_via_embed).
LISTEN_FRIST = 15.0

#: Die Arten, bei denen aus einer Zeile viele Songs werden.
ERWEITERUNGEN = frozenset(("yt_playlist", "sc_playlist", "spotify_album",
                           "spotify_playlist"))

_KOPF = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "de,en;q=0.8",
}

# Die Video-ID in allen Schreibweisen, die YouTube benutzt (watch?v=,
# youtu.be/, /shorts/, /live/, music.youtube.com).
_YT_ID_RE = re.compile(
    r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{6,20})", re.I)
# Das Embed der Spotify-Hauptseiten (nicht des Players) kommt als Base64 in
# einem <script id="initial-state">. Fuer den Fall, dass Spotify das Embed
# irgendwann genauso baut.
_INITIAL_STATE_RE = re.compile(
    r'<script[^>]*id="initial-state"[^>]*>(.*?)</script>', re.DOTALL)

_UNBRAUCHBAR = ("Mit dem Link kann ich nix anfangen – ich nehm Songs, Alben und "
                "Playlists von YouTube, SoundCloud und Spotify. Kein Podcast, "
                "keine Künstlerseite, keine Kochrezepte.")
_PRIVAT = "Der {was} ist privat oder gelöscht. Netter Versuch."


# --- Netz ----------------------------------------------------------------------
async def _holen(url, *, frist=None):
    """GET -> (status, text). (0, "") bei Netzfehler/Zeitueberschreitung.

    Ueber die geteilte Session aus ai.py (Keep-Alive, wird beim Beenden
    geschlossen) - keine eigene je Abruf. Tests ersetzen genau diese Funktion."""
    zeit = aiohttp.ClientTimeout(total=frist or FRIST)
    try:
        sitzung = ai.http_session()
        async with sitzung.get(url, headers=_KOPF, timeout=zeit,
                               allow_redirects=True) as antwort:
            return antwort.status, await antwort.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        log.info("Abruf fehlgeschlagen (%s): %s", url[:80], exc)
        return 0, ""


async def _ziel(url):
    """Wohin eine Weiterleitung (Kurzlink) zeigt - oder None. Tests ersetzen das."""
    zeit = aiohttp.ClientTimeout(total=FRIST)
    try:
        sitzung = ai.http_session()
        async with sitzung.get(url, headers=_KOPF, timeout=zeit,
                               allow_redirects=True) as antwort:
            return str(antwort.url)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        log.info("Weiterleitung nicht aufloesbar (%s): %s", url[:80], exc)
        return None


#: Bei welchem oEmbed-Status ein Link WIRKLICH weg ist. Nicht jeder 4xx heisst
#: das: YouTube antwortet mit 401 auch fuer oeffentliche Videos, deren
#: Hochlader das Einbetten verboten hat (bei Labels/VEVO haeufig) - yt-dlp
#: spielt die trotzdem. SoundCloud sagt 403 zu privaten Tracks.
_WEG = {"youtube": (400, 404), "soundcloud": (403, 404)}


async def oembed(url, anbieter):
    """Titel eines Links ueber oEmbed. -> (titel | None, status).

    status 0 = Netzfehler (dann weiss man nichts), 200 = alles gut; was davon
    "geloescht/privat" heisst, steht je Anbieter in _WEG."""
    ziel = urllib.parse.quote(url, safe="")
    adresse = {
        "youtube": f"https://www.youtube.com/oembed?format=json&url={ziel}",
        "soundcloud": f"https://soundcloud.com/oembed?format=json&url={ziel}",
        "spotify": f"https://open.spotify.com/oembed?url={ziel}",
    }[anbieter]
    status, text = await _holen(adresse)
    if status != 200:
        return None, status
    try:
        daten = json.loads(text)
    except ValueError:
        return None, status
    titel = daten.get("title") if isinstance(daten, dict) else None
    titel = re.sub(r"\s+", " ", str(titel or "")).strip()
    return (titel[:100] or None), status


# --- Spotify-Einbettung lesen (rein, ohne Netz - testbar mit Fixtures) -----------
def _json_aus_html(html):
    """Das Daten-JSON aus einer Spotify-Seite - oder None."""
    if not html:
        return None
    m = music._NEXT_DATA_RE.search(html)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            log.info("Spotify-Embed: __NEXT_DATA__ ist kein JSON.")
    m = _INITIAL_STATE_RE.search(html)
    if m:
        roh = m.group(1).strip()
        try:
            return json.loads(base64.b64decode(roh + "=" * (-len(roh) % 4)))
        except (ValueError, binascii.Error):
            pass
        try:
            return json.loads(roh)
        except ValueError:
            pass
    return None


def _finde(obj, pruefe, tiefe=0):
    """Erstes verschachteltes dict, auf das pruefe(dict) zutrifft."""
    if tiefe > 40:
        return None
    if isinstance(obj, dict):
        if pruefe(obj):
            return obj
        werte = obj.values()
    elif isinstance(obj, list):
        werte = obj
    else:
        return None
    for wert in werte:
        treffer = _finde(wert, pruefe, tiefe + 1)
        if treffer is not None:
            return treffer
    return None


def _dauer(wert):
    """Spotify-Dauer (ms als Zahl oder {'totalMilliseconds': ...}) -> Sekunden."""
    if isinstance(wert, dict):
        wert = (wert.get("totalMilliseconds") or wert.get("milliseconds")
                or wert.get("ms"))
    if isinstance(wert, bool) or not isinstance(wert, (int, float)) or wert <= 0:
        return None
    return int(round(wert / 1000))


def _text(wert):
    return re.sub(r"\s+", " ", str(wert or "")).strip()


def _kuenstler(eintrag):
    """-> (alle Kuenstler als Text, Haupt-Kuenstler)."""
    namen = []
    for a in eintrag.get("artists") or []:
        if isinstance(a, dict):
            name = _text(a.get("name") or (a.get("profile") or {}).get("name"))
            if name:
                namen.append(name)
        elif isinstance(a, str) and a.strip():
            namen.append(_text(a))
    if namen:
        return ", ".join(namen), namen[0]
    unter = _text(eintrag.get("subtitle") or eintrag.get("artist")
                  or eintrag.get("artistName"))
    # Ein geschuetztes Leerzeichen trennt bei Spotify manchmal die Namen.
    unter = unter.replace(" ", " ")
    return unter, unter.split(",")[0].strip()


def _meta(name, alle, haupt, dur):
    """Dieselbe Form wie music._spotify_track_meta / _spotify_list_tracks -
    auch dieselbe Suche: Haupt-Kuenstler + Titel (ohne die Komma-Liste aller
    Kuenstler trifft die YouTube-Suche zuverlaessiger). Angezeigt werden alle."""
    name = _text(name)
    if not name:
        return None
    query = f"{haupt or alle} {name}".strip()
    return {"query": query[:200], "name": name, "artist": haupt, "dur": dur,
            "artists": alle, "display": (f"{alle} - {name}".strip(" -"))[:100]}


def track_aus_embed(html):
    """Einbettungsseite eines Songs -> Metadaten oder None."""
    daten = _json_aus_html(html)
    if daten is None:
        return None
    entity = music.instance._deep_find(daten, "entity")
    if not isinstance(entity, dict):
        entity = _finde(daten, lambda d: d.get("type") == "track"
                        and (d.get("name") or d.get("title")))
    if not isinstance(entity, dict):
        return None
    alle, haupt = _kuenstler(entity)
    dur = _dauer(entity.get("duration") or entity.get("duration_ms")
                 or entity.get("durationMs"))
    return _meta(entity.get("name") or entity.get("title"), alle, haupt, dur)


def liste_aus_embed(html, ende=None):
    """Einbettungsseite eines Albums/einer Playlist -> Liste Metadaten (oder None)."""
    daten = _json_aus_html(html)
    if daten is None:
        return None
    liste = music.instance._deep_find(daten, "trackList")
    if not isinstance(liste, list):
        tracks = music.instance._deep_find(daten, "tracks")
        if isinstance(tracks, dict):
            tracks = tracks.get("items")
        liste = tracks if isinstance(tracks, list) else None
    if not liste:
        return None
    ende = ende or ERWEITERUNG_MAX
    raus = []
    for eintrag in liste:
        if isinstance(eintrag, dict) and isinstance(eintrag.get("track"), dict):
            eintrag = eintrag["track"]
        if not isinstance(eintrag, dict):
            continue
        alle, haupt = _kuenstler(eintrag)
        dur = _dauer(eintrag.get("duration") or eintrag.get("duration_ms")
                     or eintrag.get("durationMs"))
        meta = _meta(eintrag.get("title") or eintrag.get("name"), alle, haupt, dur)
        if meta is not None:
            raus.append(meta)
        if len(raus) >= ende:
            break
    return raus or None


async def spotify_track_ohne_keys(url):
    """Spotify-Song ohne Schluessel nachschlagen: erst die Einbettungsseite
    (Titel, Kuenstler, Dauer), dann Spotifys oEmbed (nur der Titel).
    -> {query, name, artist, dur, artists} oder None."""
    m = music._SPOTIFY_TRACK_RE.search(url or "")
    if not m:
        return None
    sid = m.group(1)
    status, html = await _holen(f"https://open.spotify.com/embed/track/{sid}")
    if status == 200:
        meta = track_aus_embed(html)
        if meta is not None:
            return meta
        log.warning("Spotify-Embed (Song %s): nichts Lesbares gefunden - Aufbau "
                    "geaendert? Probe: bash k m", sid)
    titel, _status = await oembed(f"https://open.spotify.com/track/{sid}", "spotify")
    if titel:
        return _meta(titel, "", "", None)
    return None


async def spotify_liste_ohne_keys(url, ende=None):
    """Spotify-Album/-Playlist ohne Schluessel ueber die Einbettungsseite.
    -> Liste {query, name, artist, dur, display} oder None."""
    m = music._SPOTIFY_LIST_RE.search(url or "")
    if not m:
        return None
    art = (m.group(1) or m.group(2) or "playlist").lower()
    status, html = await _holen(f"https://open.spotify.com/embed/{art}/{m.group(3)}",
                                frist=max(FRIST, LISTEN_FRIST))
    if status != 200:
        log.info("Spotify-Embed (%s): HTTP %s", art, status)
        return None
    liste = liste_aus_embed(html, ende)
    if liste is None:
        log.warning("Spotify-Embed (%s %s): keine Songliste gefunden - Aufbau "
                    "geaendert? Probe: bash k m", art, m.group(3))
    return liste


# --- Eine Zeile -> Eintraege -----------------------------------------------------
def _yt_id(url):
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _sc_sauber(url):
    """SoundCloud-Adresse ohne Tracking (?si=, utm_...) und ohne Schraegstrich am Ende."""
    teile = urllib.parse.urlsplit(url)
    host = teile.netloc.lower()
    for vorne in ("www.", "m."):
        if host.startswith(vorne):
            host = host[len(vorne):]
    return f"https://{host}{teile.path.rstrip('/')}"


def _song(i, t, q, *, h=None, d=None):
    return {"i": i, "t": _text(t)[:100] or i[:100], "h": h, "d": d, "q": q[:200]}


def _song_aus_meta(meta, q):
    hinweis = {"query": meta["query"], "dur": meta.get("dur"),
               "title": meta.get("name") or meta["query"],
               "artist": meta.get("artist", "")}
    titel = (meta.get("display")
             or f"{meta.get('artists') or meta.get('artist') or ''} - {meta.get('name')}")
    return _song(f"ytsearch1:{meta['query']}", titel.strip(" -"), q,
                 h=hinweis, d=meta.get("dur"))


def art_von(url):
    """Was ein Link ist - ohne Netz ('yt_playlist', 'play', ...)."""
    return music.Music._link_aktion(music._url_saeubern(url))


def ist_erweiterung(zeile):
    """Wird aus dieser Zeile ein ganzes Album / eine ganze Playlist? (ohne Netz)"""
    return any(art_von(u) in ERWEITERUNGEN for u in music._URL_RE.findall(zeile or ""))


async def _flach(url, quelle, platz, q, art):
    eintraege = await music.instance._flache_playlist(url, quelle=quelle, ende=platz)
    if not eintraege:
        return [], art, (f"An die {quelle} komm ich nicht ran – leer, privat oder "
                         "YouTube zickt mal wieder.")
    return [_song(u, t, q) for u, t in eintraege[:platz]], art, None


async def _ein_link(url, q, platz):
    """Ein (gesaeuberter) Link -> (songs, art, fehler)."""
    aktion = music.Music._link_aktion(url)
    if aktion == "spotify_kurz":
        ziel = await music.instance._spotify_kurzlink(url)
        if not ziel:
            return [], "spotify", ("Den Spotify-Kurzlink krieg ich nicht aufgelöst. "
                                   "Schick den langen (open.spotify.com/…).")
        url = music._url_saeubern(ziel)
        aktion = music.Music._link_aktion(url)
        if aktion in ("spotify_kurz", "spotify_unbekannt", None):
            return [], "unbekannt", _UNBRAUCHBAR
    if aktion is None or aktion == "spotify_unbekannt":
        return [], "unbekannt", _UNBRAUCHBAR
    if aktion == "yt_playlist":
        return await _flach(url, "YouTube-Playlist", platz, q, "yt_playlist")
    if aktion == "sc_playlist":
        return await _flach(url, "SoundCloud-Set", platz, q, "sc_playlist")
    if aktion in ("spotify_album", "spotify_playlist"):
        metas = None
        if aktion == "spotify_album":
            # Mit Schluesseln die API (genaue Dauer), sonst die Einbettung.
            metas = await music.instance._spotify_list_tracks(url)
        if not metas:
            metas = await spotify_liste_ohne_keys(url, ende=platz)
        if not metas:
            was = "das Album" if aktion == "spotify_album" else "die Playlist"
            return [], aktion, (f"An {was} komm ich bei Spotify nicht ran – privat, "
                                "leer oder Spotify mauert.")
        return [_song_aus_meta(mt, q) for mt in metas[:platz]], aktion, None
    # --- ab hier ein einzelner Song ('play') --------------------------------------
    if music._SPOTIFY_TRACK_RE.search(url):
        meta = (await music.instance._spotify_track_meta(url)
                or await spotify_track_ohne_keys(url))
        if not meta:
            return [], "spotify", ("Den Spotify-Song krieg ich nicht aufgelöst. "
                                   "Schreib Künstler und Titel, dann such ich selber.")
        return [_song_aus_meta(meta, q)], "spotify", None
    low = url.lower()
    if "youtube.com" in low or "youtu.be" in low:
        vid = _yt_id(url)
        if not vid:
            return [], "unbekannt", ("Das ist kein Video-Link, sondern irgendeine "
                                     "YouTube-Seite. Ich brauch das Video.")
        sauber = f"https://www.youtube.com/watch?v={vid}"
        titel, status = await oembed(sauber, "youtube")
        if status in _WEG["youtube"]:
            return [], "youtube", _PRIVAT.format(was="YouTube-Link")
        return [_song(sauber, titel or f"YouTube-Video {vid}", q)], "youtube", None
    if music._SC_RE.match(url):
        if "on.soundcloud.com" in low:
            # Der Kurzlink aus der App kann auch ein SET sein - das sieht man
            # erst nach dem Redirect (wie in music._einzeln_spielen).
            menge = await music.instance._flache_playlist(url, quelle="SoundCloud-Set",
                                                          ende=platz)
            if menge and len(menge) > 1:
                return [_song(u, t, q) for u, t in menge[:platz]], "sc_playlist", None
            if menge:
                u, t = menge[0]
                return [_song(_sc_sauber(u), t, q)], "soundcloud", None
            # Ein einzelner Track: yt-dlp liefert dafuer keine Liste. Dann dem
            # Redirect selbst folgen - oEmbed kennt den Kurzlink nicht.
            ziel = await _ziel(url)
            if (not ziel or not music._SC_RE.match(ziel)
                    or "on.soundcloud.com" in ziel.lower()):
                # Unbekannt, aber kein Beweis fuer "weg": yt-dlp folgt dem
                # Kurzlink beim Abspielen selbst.
                return [_song(url.split("?")[0], "SoundCloud-Track", q)], "soundcloud", None
            url = ziel
        sauber = _sc_sauber(url)
        titel, status = await oembed(sauber, "soundcloud")
        if status in _WEG["soundcloud"]:
            return [], "soundcloud", _PRIVAT.format(was="SoundCloud-Track")
        rest = urllib.parse.urlsplit(sauber).path.strip("/").replace("/", " – ")
        return [_song(sauber, titel or rest.replace("-", " "), q)], "soundcloud", None
    # Direkt verlinkte Audiodatei: der Dateiname ist der Titel.
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    return [_song(url, name or url, q)], "datei", None


async def eintraege(zeile, platz=ERWEITERUNG_MAX):
    """Eine eingefuegte Zeile -> (songs, art, fehler).

    Ohne Link wird die Zeile zur YouTube-Suche - LAZY, hier laeuft noch kein
    yt-dlp (das passiert erst beim Abspielen). Mit Links: je Link ein Eintrag
    bzw. bei Alben/Playlists hoechstens 'platz' Songs."""
    zeile = (zeile or "").strip()
    if not zeile:
        return [], "", None
    q = zeile[:200]
    platz = max(0, min(int(platz), ERWEITERUNG_MAX))
    links = [music._url_saeubern(u) for u in music._URL_RE.findall(zeile)]
    links = [u for u in links if u]
    if not links:
        text = _text(zeile)[:200]
        return [_song(f"ytsearch1:{text}", text, q)], "text", None
    songs, art, fehler = [], "", None
    for url in links:
        frei = platz - len(songs)
        if frei <= 0:
            break
        try:
            neu, a, f = await _ein_link(url, q, frei)
        except Exception:  # noqa: BLE001 - eine kaputte Quelle darf den Rest nicht kippen
            log.exception("Playlist-Zeile nicht aufloesbar: %s", url[:120])
            neu, a, f = [], "unbekannt", "Da ist was schiefgelaufen. Der Link bleibt draußen."
        songs.extend(neu)
        art = art or a
        fehler = fehler or f
    return songs, art, fehler
