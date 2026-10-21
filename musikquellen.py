"""Aus eingefuegten Zeilen werden Playlist-Eintraege (Links, Alben, Suchtext).

GERUEST fuer den Playlist-Teil. Der fuellt diese Datei; aufgerufen wird sie vom
Playlist-Assistenten (playlists.py) und von music._einzeln_spielen fuer
Spotify-Links ohne Schluessel. Beim Import passiert nichts.

    eintraege(zeile) -> (songs, art, fehler)
        songs  Liste von Eintraegen {"i": yt-dlp-Eingabe, "t": Titel,
               "h": Match-Hinweis oder None, "d": Dauer, "q": die Zeile}
        art    was die Zeile war ('youtube', 'spotify_album', 'text', ...)
        fehler ein Satz fuer den Chat, wenn nichts zu holen war - sonst None
    spotify_track_ohne_keys(url) -> dict | None
        Titel/Kuenstler/Dauer eines Spotify-Songs ohne API-Schluessel
"""

import logging

log = logging.getLogger("dcbot.musikquellen")


async def eintraege(zeile):
    """Eine eingefuegte Zeile -> (songs, art, fehler). Noch nichts."""
    return [], "", None


async def spotify_track_ohne_keys(url):
    """Spotify-Song ohne Schluessel nachschlagen. Noch nichts."""
    return None
