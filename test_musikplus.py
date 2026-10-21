"""Musik-Plus: Endlos-Radio, Abstimmung zum Ueberspringen, die Warteschlange,
die einen Neustart ueberlebt (musikzustand.py), und Musik-Wrapped (musikstats.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_musikplus.py      nur diese Datei

Das Fundament legt hier nur das Geruest fest - der Musik-Teil fuellt die
Datei mit seinen Tests.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module

import musikstats
import musikzustand

#: Befehle fuer den gemeinsamen Rauchtest (test_fundament.py). musikzustand
#: hat keinen Befehl und steht deshalb nicht hier.
RAUCHPROBEN = (
    (musikstats, ()),
)


def test_musikplus_geruest_hat_die_schnittstelle():
    """music.py und bot.py rufen diese Namen - es muss sie geben, auch bevor
    sie etwas tun. merken/vergessen/notieren laufen mitten in der Wiedergabe
    und sind deshalb synchron."""
    import inspect
    for name in ("setup", "is_enabled", "merken", "vergessen", "takt", "sichern",
                 "wiederherstellen"):
        assert callable(getattr(musikzustand, name, None)), name
    for name in ("setup", "is_enabled", "handle", "notieren", "tick"):
        assert callable(getattr(musikstats, name, None)), name
    for synchron in (musikzustand.merken, musikzustand.vergessen, musikstats.notieren):
        assert not inspect.iscoroutinefunction(synchron), synchron.__name__


if __name__ == "__main__":
    run(globals())
