"""Playlists je Server: playlists.py und musikquellen.py.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_playlists.py      nur diese Datei

Das Fundament legt hier nur das Geruest fest - der Playlist-Teil fuellt die
Datei mit seinen Tests.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module

import basis
import musikquellen
import playlists

#: Befehle, die der gemeinsame Rauchtest (test_fundament.py) durch das echte
#: handle() schickt. Leer, solange das Modul nur ein Geruest ist - sobald
#: setup() es einschaltet, verlangt der Rauchtest hier Befehle.
RAUCHPROBEN = (
    (playlists, ()),
)


def test_playlists_geruest_hat_die_schnittstelle():
    """bot.py und music.py rufen diese Namen - es muss sie geben, auch bevor
    sie etwas tun."""
    for name in ("setup", "is_enabled", "handle", "on_message_passive", "tick",
                 "offene_sichern"):
        assert callable(getattr(playlists, name, None)), name
    assert isinstance(playlists.DYNAMISCHE_KNOEPFE, tuple)
    assert playlists.HANDLED is basis.HANDLED
    for name in ("eintraege", "spotify_track_ohne_keys"):
        assert callable(getattr(musikquellen, name, None)), name


if __name__ == "__main__":
    run(globals())
